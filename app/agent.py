"""Lumi Agent：模型通过 function calling 自主决定调用哪个工具，后端执行后把结果交回模型。

对外只暴露 run_turn()，它是一个生成器，逐条产出给前端的 SSE 事件：
  text        模型回复的增量文字
  tool_start  开始调用某个工具（含参数）
  tool_end    工具返回（含一句摘要）
  card        结构化卡片（帖子 / 效果表）
  paywall     需要弹出付费墙
  error       出错提示
  done        本轮结束
"""
import json
import os
import re
from collections import Counter
from typing import Any, Dict, Iterator, List, Optional, Tuple

from openai import OpenAI

import store

MAX_TOOL_ROUNDS = 6
MAX_HISTORY = 40
BANNED_WORDS = ("治愈", "根治", "100%", "永久", "最好的", "第一", "cure", "guarantee")

# 每个用户的多轮对话（含工具调用记录），演示用，保存在内存中
CONVERSATIONS: Dict[str, List[Dict]] = {}


def get_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ.get("LLM_API_KEY", ""),
        base_url=os.environ.get("LLM_BASE_URL") or None,
        timeout=30,
        max_retries=1,  # PRD 7.4：超时或 5xx 自动重试 1 次
    )


def model_name() -> str:
    return os.environ.get("LLM_MODEL", "deepseek-chat")


SYSTEM_PROMPT = """你是 Lumi，一个帮海外中小电商卖家写社媒帖子、并告诉他们帖子带来了多少订单的 AI 营销助手。
今天是 {today}。商家店铺：{store}（{plan} 套餐，{connected}）。

工作规则：
1. 涉及商品、评价、帖子表现的任何事实，都必须先调用工具获取，绝不编造数字。
2. 写帖子必须调用 generate_post，不要自己在回复里写帖子正文。写之前先用 get_products 确认商品；如果合适，再用 analyze_reviews 找卖点、用 get_post_performance 看哪种风格转化最好，据此选择 style 和 key_points。
3. 商家只是问问题（例如哪条帖子卖得好、下一条发什么）时，只调用需要的查询工具，不要生成帖子。
4. 商家要求重新生成某条帖子时，把那条帖子的 post_id 传给 generate_post 的 regenerate_post_id，以便沿用原来的追踪链接。
5. 工具返回错误时按错误说明处理：quota_exceeded 就告诉商家本月额度已用完并建议升级；pro_required 说明这是 Pro 功能；store_not_connected 建议先接入 Shopify；product_not_found 就追问商家指的是哪个商品。
6. 帖子卡片和效果表格会由界面直接展示，回复里不要重复帖子全文或整张表，只用一两句话说明你的判断和下一步建议。
7. 用商家使用的语言回复，简洁、具体。"""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_products",
            "description": "按关键词查找店铺里的商品，返回商品 id、名称、价格和描述。关键词为空时返回全部商品。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "商品关键词，例如“防晒”"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_reviews",
            "description": "分析某个商品的用户评价，返回平均评分、被提到最多的卖点、顾虑和代表性原话。",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "string"}},
                "required": ["product_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_post_performance",
            "description": "查询已发布帖子的点击、归因订单和营业额，以及各内容风格的点击→下单转化率。可查最近 N 天或某一条帖子。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "最近多少天，默认 30", "default": 30},
                    "post_id": {"type": "string", "description": "只查某一条帖子时填写"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_post",
            "description": "为某个商品生成一条社媒帖子（消耗 1 次生成额度）。Pro 且已接入店铺的商家会自动挂上追踪短链和专属优惠码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "platform": {"type": "string", "enum": ["instagram", "tiktok", "facebook"]},
                    "language": {"type": "string", "description": "帖子语言，例如 zh、en、es"},
                    "style": {"type": "string", "enum": ["ugc", "promo", "educational", "unboxing"],
                              "description": "ugc=真实用户口吻实拍；promo=限时优惠；educational=科普；unboxing=开箱"},
                    "key_points": {"type": "array", "items": {"type": "string"}, "description": "要突出的卖点，最好来自评价分析"},
                    "regenerate_post_id": {"type": "string", "description": "重新生成某条已有帖子时填写它的 post_id"},
                },
                "required": ["product_id", "platform", "language", "style"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_tracked_link",
            "description": "给一条已有帖子补上追踪短链和专属优惠码（仅 Pro 且已接入店铺）。已有追踪时直接返回原来的。",
            "parameters": {
                "type": "object",
                "properties": {"post_id": {"type": "string"}},
                "required": ["post_id"],
            },
        },
    },
]


# ---------- 工具实现 ----------

class ToolContext:
    def __init__(self, uid: str, base_url: str):
        self.uid = uid
        self.base_url = base_url.rstrip("/")
        self.events: List[Dict] = []  # 工具执行过程中要推给前端的事件

    @property
    def user(self) -> Dict:
        return store.get_user(self.uid)

    def short_url(self, code: Optional[str]) -> Optional[str]:
        return f"{self.base_url}/l/{code}" if code else None


def tool_get_products(ctx: ToolContext, query: str = "") -> Tuple[Dict, str]:
    q = (query or "").strip().lower()
    items = [p for p in store.PRODUCTS
             if not q or q in p["name"].lower() or any(k in q or q in k for k in p["keywords"])]
    result = [{k: p[k] for k in ("id", "name", "price", "category", "description")} for p in items]
    return {"products": result}, f"找到 {len(result)} 个商品" + (f"：{result[0]['name']}" if len(result) == 1 else "")


def tool_analyze_reviews(ctx: ToolContext, product_id: str) -> Tuple[Dict, str]:
    p = store.product(product_id)
    reviews = store.REVIEWS.get(product_id, [])
    if not p or not reviews:
        return {"error": "product_not_found", "available": [x["id"] for x in store.PRODUCTS]}, "没找到这个商品"
    positives = Counter(t for r in reviews if r["rating"] >= 4 for t in r["tags"])
    concerns = Counter(t for r in reviews if r["rating"] <= 3 for t in r["tags"])
    concerns.update(t for r in reviews if r["rating"] >= 4 for t in r["tags"] if t in ("容量小", "需避光", "清洁力一般"))
    result = {
        "product": p["name"],
        "review_count": len(reviews),
        "avg_rating": round(sum(r["rating"] for r in reviews) / len(reviews), 2),
        "top_selling_points": [{"point": k, "mentions": v} for k, v in positives.most_common(4)],
        "concerns": [{"point": k, "mentions": v} for k, v in concerns.most_common(3)],
        "quotes": [r["text"] for r in sorted(reviews, key=lambda r: -r["rating"])[:3]],
    }
    top = result["top_selling_points"][0]["point"] if result["top_selling_points"] else "—"
    return result, f"{len(reviews)} 条评价，提到最多的卖点：{top}"


def tool_get_post_performance(ctx: ToolContext, days: int = 30, post_id: Optional[str] = None) -> Tuple[Dict, str]:
    user = ctx.user
    if not user["connected"]:
        return {"error": "store_not_connected"}, "店铺未接入"
    if user["plan"] == "free":
        ctx.events.append({"type": "paywall", "trigger": "feature_tracking"})
        return {"error": "pro_required", "message": "效果数据是 Pro 功能"}, "效果数据需要 Pro"
    stats = store.post_stats(ctx.uid, days=days or 30, post_id=post_id)
    totals = {k: round(sum(s[k] for s in stats), 2) for k in ("clicks", "orders", "gmv")}
    styles = store.style_conversion(stats)
    ctx.events.append({"type": "card", "kind": "performance", "data": {"posts": stats, "totals": totals, "days": days or 30}})
    best = f"，转化最高的风格：{styles[0]['style']}（{styles[0]['conversion']}%）" if styles else ""
    return ({"days": days or 30, "totals": totals, "posts": stats, "style_conversion": styles},
            f"{len(stats)} 条帖子，{totals['orders']:.0f} 单{best}")


def _copywrite(client: OpenAI, product: Dict, platform: str, language: str, style: str,
               key_points: List[str], discount_code: Optional[str]) -> Tuple[Dict, int]:
    """单独调用一次模型写帖子正文，返回 ({title, caption, hashtags}, tokens)。"""
    prompt = (
        f"为商品写一条 {platform} 帖子。\n商品：{product['name']}，${product['price']}。{product['description']}\n"
        f"语言：{language}；风格：{style}；要突出的卖点：{'、'.join(key_points) or '自行判断'}。\n"
        + (f"文末用一句话引导点击主页链接，并提到优惠码 {discount_code} 立减 15%。\n" if discount_code else "")
        + "不要使用夸大功效的说法（例如治愈、根治、100%、永久、最好的）。\n"
        '只输出 JSON：{"title": "不超过 20 字的标题", "caption": "正文，可含 emoji 和换行", "hashtags": ["3 到 6 个话题标签，不带 #"]}'
    )
    resp = client.chat.completions.create(
        model=model_name(),
        messages=[{"role": "system", "content": "你是资深社媒文案，只输出 JSON。"}, {"role": "user", "content": prompt}],
        temperature=0.8,
    )
    raw = resp.choices[0].message.content or ""
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0)) if m else {}
    if not isinstance(data, dict) or not str(data.get("caption", "")).strip():
        raise ValueError("empty_or_invalid_output")
    tags = [str(t).lstrip("#") for t in data.get("hashtags", []) if str(t).strip()][:6]
    tokens = resp.usage.total_tokens if getattr(resp, "usage", None) else 0
    return {"title": str(data.get("title") or product["name"])[:40], "caption": str(data["caption"]).strip(), "hashtags": tags}, tokens


def _violates(text: str) -> bool:
    return any(w.lower() in text.lower() for w in BANNED_WORDS)


def tool_generate_post(ctx: ToolContext, product_id: str, platform: str = "instagram", language: str = "zh",
                       style: str = "ugc", key_points: Optional[List[str]] = None,
                       regenerate_post_id: Optional[str] = None) -> Tuple[Dict, str]:
    product = store.product(product_id)
    if not product:
        return {"error": "product_not_found", "available": [p["id"] for p in store.PRODUCTS]}, "没找到这个商品"
    existing = store.get_post(regenerate_post_id, ctx.uid) if regenerate_post_id else None
    if regenerate_post_id and not existing:
        return {"error": "post_not_found"}, "没找到要重新生成的帖子"

    if not store.reserve_quota(ctx.uid):
        user = ctx.user
        ctx.events.append({"type": "paywall", "trigger": "quota"})
        return ({"error": "quota_exceeded", "plan": user["plan"], "limit": user["quota_limit"]},
                f"本月 {user['quota_limit']} 次额度已用完")

    user = ctx.user
    tracked = store.tracking_allowed(user)
    post_id = existing["id"] if existing else store.create_post(ctx.uid, product_id, platform, language, style, product["name"], "", [])
    post = store.ensure_tracking(post_id, user) if tracked else store.get_post(post_id)

    content, tokens, last_err = None, 0, None
    for _ in range(2):  # 输出为空 / 格式不对 / 命中违禁词时再试 1 次
        try:
            content, used = _copywrite(get_client(), product, platform, language, style, key_points or [],
                                       post["discount_code"] if tracked else None)
            tokens += used
            if _violates(content["caption"] + content["title"]):
                last_err, content = "content_policy", None
                continue
            break
        except Exception as e:  # 超时、5xx、JSON 解析失败
            last_err, content = type(e).__name__, None
    if not content:
        store.release_quota(ctx.uid)
        if not existing:
            store.delete_post(post_id)
        return ({"error": "generation_failed", "reason": last_err, "quota_charged": False},
                "生成失败，未扣额度")

    store.update_post_content(post_id, content["title"], content["caption"], content["hashtags"])
    user = ctx.user
    remaining = user["quota_limit"] - user["quota_used"]
    store.track("post_generated", "server", ctx.uid, {
        "post_id": post_id, "product_id": product_id, "style": style, "language": language, "platform": platform,
        "model": model_name(), "tokens": tokens, "regenerate": bool(existing), "quota_remaining": remaining,
    })
    lock = None if tracked else ("free" if user["plan"] == "free" else "not_connected")
    card = {
        "post_id": post_id, "product": product["name"], "platform": platform, **content,
        "short_url": ctx.short_url(post["link_code"]) if tracked else None,
        "discount_code": post["discount_code"] if tracked else None,
        "reused_tracking": bool(existing) and tracked, "lock": lock, "status": post["status"],
    }
    ctx.events.append({"type": "card", "kind": "post", "data": card})
    result = {"post_id": post_id, "title": content["title"], "tracked": tracked,
              "short_url": card["short_url"], "discount_code": card["discount_code"],
              "quota_remaining": remaining, "lock_reason": lock}
    return result, f"已生成「{content['title']}」" + ("，已挂追踪" if tracked else "")


def tool_create_tracked_link(ctx: ToolContext, post_id: str) -> Tuple[Dict, str]:
    post = store.get_post(post_id, ctx.uid)
    if not post:
        return {"error": "post_not_found"}, "没找到这条帖子"
    user = ctx.user
    if not store.tracking_allowed(user):
        if user["plan"] == "free":
            ctx.events.append({"type": "paywall", "trigger": "feature_tracking"})
            return {"error": "pro_required"}, "追踪是 Pro 功能"
        return {"error": "store_not_connected"}, "店铺未接入"
    post = store.ensure_tracking(post_id, user)
    return ({"post_id": post_id, "short_url": ctx.short_url(post["link_code"]), "discount_code": post["discount_code"]},
            f"短链 {ctx.short_url(post['link_code'])}")


TOOL_IMPLS = {
    "get_products": tool_get_products,
    "analyze_reviews": tool_analyze_reviews,
    "get_post_performance": tool_get_post_performance,
    "generate_post": tool_generate_post,
    "create_tracked_link": tool_create_tracked_link,
}


def execute_tool(ctx: ToolContext, name: str, raw_args: str) -> Tuple[Dict, str]:
    impl = TOOL_IMPLS.get(name)
    if not impl:
        return {"error": "unknown_tool"}, f"未知工具 {name}"
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        return {"error": "invalid_arguments", "raw": raw_args}, "参数格式错误"
    try:
        return impl(ctx, **args)
    except TypeError as e:  # 模型传了不认识的参数
        return {"error": "invalid_arguments", "detail": str(e)}, "参数不合法"


# ---------- Agent 循环 ----------

def _system_message(uid: str) -> Dict:
    u = store.get_user(uid)
    return {"role": "system", "content": SYSTEM_PROMPT.format(
        today=store.now().strftime("%Y-%m-%d"), store=u["store_name"], plan=u["plan"],
        connected="已接入 Shopify" if u["connected"] else "尚未接入店铺")}


def _trim(history: List[Dict]) -> List[Dict]:
    """控制上下文长度，且保证从 user 消息开始，不把工具调用和结果拆开。"""
    if len(history) <= MAX_HISTORY:
        return history
    h = history[-MAX_HISTORY:]
    while h and h[0]["role"] != "user":
        h = h[1:]
    return h


def _stream_completion(client: OpenAI, messages: List[Dict], allow_tools: bool) -> Iterator[Tuple[str, Any]]:
    """流式调用模型，产出 ("text", delta)，结束时产出 ("final", (text, tool_calls))。"""
    kwargs: Dict[str, Any] = {"model": model_name(), "messages": messages, "stream": True, "temperature": 0.5}
    if allow_tools:
        kwargs.update(tools=TOOLS, tool_choice="auto")
    text, calls = "", {}
    for chunk in client.chat.completions.create(**kwargs):
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if getattr(delta, "content", None):
            text += delta.content
            yield "text", delta.content
        for tc in getattr(delta, "tool_calls", None) or []:
            c = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                c["id"] = tc.id
            if tc.function and tc.function.name:
                c["name"] += tc.function.name
            if tc.function and tc.function.arguments:
                c["arguments"] += tc.function.arguments
    yield "final", (text, [calls[i] for i in sorted(calls)])


def run_turn(uid: str, user_text: str, base_url: str) -> Iterator[Dict]:
    if not os.environ.get("LLM_API_KEY"):
        yield {"type": "error", "message": "后端未配置 LLM_API_KEY，请按 README 填写 app/.env 后重启。"}
        yield {"type": "done"}
        return

    history = CONVERSATIONS.setdefault(uid, [])
    snapshot = len(history)
    history.append({"role": "user", "content": user_text})
    ctx = ToolContext(uid, base_url)
    client = get_client()

    try:
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            messages = [_system_message(uid)] + _trim(history)
            allow_tools = round_no < MAX_TOOL_ROUNDS  # 超过 6 轮强制停止调用工具，直接作答
            text, calls = "", []
            for kind, payload in _stream_completion(client, messages, allow_tools):
                if kind == "text":
                    yield {"type": "text", "delta": payload}
                else:
                    text, calls = payload
            if not calls:
                history.append({"role": "assistant", "content": text})
                break
            history.append({
                "role": "assistant", "content": text or None,
                "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                               for c in calls],
            })
            for c in calls:
                yield {"type": "tool_start", "name": c["name"], "args": c["arguments"] or "{}"}
                result, summary = execute_tool(ctx, c["name"], c["arguments"])
                yield {"type": "tool_end", "name": c["name"], "summary": summary, "ok": "error" not in result}
                while ctx.events:
                    yield ctx.events.pop(0)
                history.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result, ensure_ascii=False)})
    except Exception as e:  # 模型不可用：回滚本轮对话，提示重试
        del history[snapshot:]
        yield {"type": "error", "message": f"模型暂时不可用（{type(e).__name__}），请稍后重试。"}
    yield {"type": "done"}


def reset(uid: str) -> None:
    CONVERSATIONS.pop(uid, None)
