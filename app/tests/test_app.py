"""端到端测试：用脚本化的假模型代替真实大模型，其余（Agent 循环、工具、额度、埋点、短链、归因）全部走真实代码。

运行：.venv/bin/python -m pytest tests -q   或   .venv/bin/python tests/test_app.py
"""
import json
import os
import sys
import tempfile
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["LUMI_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["LLM_API_KEY"] = "test-key"

from fastapi.testclient import TestClient  # noqa: E402

import agent  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402


# ---------- 假模型 ----------

def text_turn(text):
    """流式文字回复，拆成两段以测试增量拼接。"""
    half = len(text) // 2
    return [NS(choices=[NS(delta=NS(content=text[:half], tool_calls=None))]),
            NS(choices=[NS(delta=NS(content=text[half:], tool_calls=None))])]


def tool_turn(*calls):
    """流式工具调用，每个调用的参数拆成两段，测试按 index 累积。"""
    chunks = []
    for i, (name, args) in enumerate(calls):
        raw = json.dumps(args, ensure_ascii=False)
        cut = len(raw) // 2
        chunks.append(NS(choices=[NS(delta=NS(content=None, tool_calls=[
            NS(index=i, id=f"call_{i}_{name}", function=NS(name=name, arguments=raw[:cut]))]))]))
        chunks.append(NS(choices=[NS(delta=NS(content=None, tool_calls=[
            NS(index=i, id=None, function=NS(name=None, arguments=raw[cut:]))]))]))
    return chunks


def copy_reply(caption="早上 8 点出门，补涂一次就够 🙌", fail=False):
    if fail:
        return ValueError("boom")
    content = json.dumps({"title": "通勤 8 小时依然亮", "caption": caption, "hashtags": ["防晒", "#通勤"]}, ensure_ascii=False)
    return NS(choices=[NS(message=NS(content="```json\n" + content + "\n```"))], usage=NS(total_tokens=321))


class FakeClient:
    """stream=True 的调用按顺序取 chat_script；非流式（写正文）按顺序取 copy_script。"""

    def __init__(self, chat_script, copy_script=()):
        self.chat_script = list(chat_script)
        self.copy_script = list(copy_script)
        self.calls = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if kw.get("stream"):
            return iter(self.chat_script.pop(0))
        item = self.copy_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def use_fake(client):
    agent.get_client = lambda: client
    return client


def chat(c, message):
    res = c.post("/api/chat", json={"message": message})
    assert res.status_code == 200
    return [json.loads(line[6:]) for line in res.text.split("\n\n") if line.startswith("data: ")]


def events(uid, name):
    return [e for e in store.list_events(uid) if e["name"] == name]


GENERATE_FLOW = [
    tool_turn(("get_products", {"query": "防晒"})),
    tool_turn(("analyze_reviews", {"product_id": "p1"}), ("get_post_performance", {"days": 30})),
    tool_turn(("generate_post", {"product_id": "p1", "platform": "instagram", "language": "zh", "style": "ugc",
                                 "key_points": ["不泛白", "不闷痘"]})),
    text_turn("写好了，用的是转化最高的 UGC 风格。"),
]


def test_full_flow():
    c = TestClient(main.app)

    # 1. 广告落地：服务端把 UTM 写进 cookie，注册时写入账户
    c.get("/?utm_source=tiktok&utm_campaign=spring_sale&ttclid=TT123")
    me = c.post("/api/signup", json={"email": "mia@example.com", "store_name": "Mia's Skincare"}).json()
    uid = me["id"]
    assert me["plan"] == "free" and me["quota_limit"] == 10
    signup = events(uid, "signup_completed")[0]["props"]
    assert signup["utm_source"] == "tiktok" and signup["ttclid"] == "TT123"

    # 2. Free + 未接入：Agent 自主调用 4 个工具，帖子生成但不挂追踪；效果查询被拦截
    fake = use_fake(FakeClient(GENERATE_FLOW, [copy_reply()]))
    evs = chat(c, "给防晒霜写一条 IG 帖子")
    tools_called = [e["name"] for e in evs if e["type"] == "tool_start"]
    assert tools_called == ["get_products", "analyze_reviews", "get_post_performance", "generate_post"]
    card = next(e for e in evs if e["type"] == "card" and e["kind"] == "post")["data"]
    assert card["lock"] == "free" and card["short_url"] is None
    assert card["hashtags"] == ["防晒", "通勤"]
    assert "".join(e["delta"] for e in evs if e["type"] == "text") == "写好了，用的是转化最高的 UGC 风格。"
    assert any(e.get("ok") is False and e["name"] == "get_post_performance" for e in evs if e["type"] == "tool_end")
    assert c.get("/api/me").json()["quota_used"] == 1
    assert fake.calls[0]["tools"] and fake.calls[0]["tool_choice"] == "auto"  # 真正走 function calling
    assert c.get("/api/dashboard").json() == {"locked": True}  # Free 在后端就拿不到效果数据

    # 3. 额度用尽：后端拦截，弹付费墙，不扣额度、不生成
    for _ in range(9):
        assert store.reserve_quota(uid)
    use_fake(FakeClient([tool_turn(("generate_post", {"product_id": "p1", "platform": "instagram", "language": "zh", "style": "promo"})),
                         text_turn("本月额度用完了，可以升级 Pro。")]))
    evs = chat(c, "再写一条")
    assert {"type": "paywall", "trigger": "quota"} in evs
    assert c.get("/api/me").json()["quota_used"] == 10
    assert len(events(uid, "post_generated")) == 1

    # 4. 接入店铺 + 升级 Pro：订阅事件带上首触来源
    c.post("/api/store/connect", json={"domain": "mias-skincare.myshopify.com"})
    me = c.post("/api/subscribe", json={"plan": "pro", "trigger": "quota"}).json()
    assert me["plan"] == "pro" and me["quota_limit"] == 500 and me["quota_used"] == 10
    sub = events(uid, "subscription_started")[0]["props"]
    assert sub["first_touch"]["utm_campaign"] == "spring_sale" and sub["price"] == 29

    # 5. Pro 生成：自动挂短链和优惠码
    use_fake(FakeClient(GENERATE_FLOW, [copy_reply()]))
    evs = chat(c, "给防晒霜写一条 IG 帖子")
    card = next(e for e in evs if e["type"] == "card" and e["kind"] == "post")["data"]
    assert card["lock"] is None and card["short_url"].startswith("http://testserver/l/") and card["discount_code"].startswith("MIAS15-")
    post_id, code = card["post_id"], card["short_url"].rsplit("/", 1)[1]
    perf = next(e for e in evs if e["type"] == "card" and e["kind"] == "performance")["data"]
    assert perf["totals"]["orders"] == 23, perf["totals"]  # 导入的 5 条历史帖子

    # 6. 重新生成：沿用同一条短链和优惠码，额度照扣 1 次
    used_before = c.get("/api/me").json()["quota_used"]
    use_fake(FakeClient([tool_turn(("generate_post", {"product_id": "p1", "platform": "instagram", "language": "zh",
                                                      "style": "ugc", "regenerate_post_id": post_id})),
                         text_turn("换了个开头。")], [copy_reply("换个开头：通勤族的防晒")]))
    evs = chat(c, f"重新生成这条帖子（post_id: {post_id}）")
    card2 = next(e for e in evs if e["type"] == "card")["data"]
    assert card2["post_id"] == post_id and card2["discount_code"] == card["discount_code"] and card2["reused_tracking"]
    assert c.get("/api/me").json()["quota_used"] == used_before + 1

    # 7. 发布 → 顾客点短链 → 302 跳到商品页并带上 utm_campaign
    assert c.post(f"/api/posts/{post_id}/publish", json={"channel": "instagram"}).json()["ok"]
    r = c.get(f"/l/{code}", follow_redirects=False, headers={"user-agent": "Mozilla/5.0 (iPhone) Instagram 300.0"})
    assert r.status_code == 302 and f"utm_campaign={post_id}" in r.headers["location"] and "shop=mias-skincare.myshopify.com" in r.headers["location"]
    c.get(f"/l/{code}", follow_redirects=False, headers={"user-agent": "facebookexternalhit/1.1"})  # 链接预览抓取
    c.get(f"/l/{code}", follow_redirects=False, headers={"user-agent": "Mozilla/5.0 (iPhone) Instagram 300.0"})  # 10 秒内重复
    clicks = events(uid, "link_clicked")
    assert len(clicks) == 2 and sorted(e["props"]["is_bot"] for e in clicks) == [False, True]

    # 8. 订单 webhook：按 UTM 归因；重复推送幂等；优惠码和 UTM 冲突时以优惠码为准
    hook = {"X-Shopify-Shop-Domain": "mias-skincare.myshopify.com"}
    landing = r.headers["location"]
    res = c.post("/webhooks/shopify/orders", headers=hook, json={"id": "1001", "total_price": 28, "landing_site": landing}).json()
    assert res["post_id"] == post_id and res["match_method"] == "utm" and res["click_to_order_hours"] is not None
    assert c.post("/webhooks/shopify/orders", headers=hook, json={"id": "1001", "total_price": 28, "landing_site": landing}).json()["duplicate"]
    history_code = next(p for p in store.post_stats(uid) if p["post_id"] != post_id)
    res = c.post("/webhooks/shopify/orders", headers=hook, json={
        "id": "1002", "total_price": 34, "landing_site": landing, "discount_codes": [history_code["discount_code"].lower()]}).json()
    assert res["post_id"] == history_code["post_id"] and res["match_method"] == "code"
    res = c.post("/webhooks/shopify/orders", headers=hook, json={"id": "1003", "total_price": 18}).json()
    assert res["post_id"] is None and res["match_method"] == "none"
    assert len(events(uid, "order_attributed")) == 2

    # 9. 效果面板：真实点击和订单叠加在历史数据上；退款后扣回
    d = c.get("/api/dashboard").json()
    new = next(p for p in d["posts"] if p["post_id"] == post_id)
    assert new["clicks"] == 1 and new["orders"] == 1 and new["gmv"] == 28
    assert d["totals"]["orders"] == 25, (d["totals"], d["styles"])
    assert d["suggestion"]["best"]["style"] == d["styles"][0]["style"]
    c.post("/webhooks/shopify/refunds", headers=hook, json={"order_id": "1001", "amount": 28})
    assert c.get("/api/dashboard").json()["totals"]["orders"] == 24

    # 10. 生成失败：重试 1 次仍失败 → 不扣额度、不留草稿、不记 post_generated
    used_before = c.get("/api/me").json()["quota_used"]
    gen_before = len(events(uid, "post_generated"))
    use_fake(FakeClient([tool_turn(("generate_post", {"product_id": "p2", "platform": "tiktok", "language": "en", "style": "educational"})),
                         text_turn("生成失败了，请重试。")], [copy_reply(fail=True), copy_reply(fail=True)]))
    evs = chat(c, "给维 C 精华写一条 TikTok")
    assert any(e["type"] == "tool_end" and e["summary"] == "生成失败，未扣额度" for e in evs)
    assert c.get("/api/me").json()["quota_used"] == used_before
    assert len(events(uid, "post_generated")) == gen_before

    # 11. 命中违禁词时改写一次
    use_fake(FakeClient([tool_turn(("generate_post", {"product_id": "p3", "platform": "instagram", "language": "zh", "style": "unboxing"})),
                         text_turn("好了。")], [copy_reply("100% 根治干皮"), copy_reply("熬夜后急救补水")]))
    evs = chat(c, "给面膜写一条开箱")
    assert next(e for e in evs if e["type"] == "card")["data"]["caption"] == "熬夜后急救补水"

    # 12. 工具连续调用超过 6 轮：第 7 次请求不再提供工具，强制作答
    loop = [tool_turn(("get_products", {}))] * 6 + [text_turn("先到这里。")]
    fake = use_fake(FakeClient(loop))
    chat(c, "随便看看")
    assert "tools" not in fake.calls[-1] and len(fake.calls) == 7

    # 13. 多轮对话：上一轮的工具调用和结果都留在历史里
    assert any(m.get("role") == "tool" for m in agent.CONVERSATIONS[uid])

    # 14. 服务端事件不能由前端伪造
    assert c.post("/api/events", json={"name": "order_attributed", "props": {}}).status_code == 400
    assert c.post("/api/events", json={"name": "paywall_viewed", "props": {"trigger": "quota"}}).status_code == 200


def test_model_error_rolls_back_turn():
    c = TestClient(main.app)
    uid = c.post("/api/signup", json={"email": "a@b.com", "store_name": "Shop"}).json()["id"]

    class Broken(FakeClient):
        def create(self, **kw):
            raise TimeoutError("timeout")

    use_fake(Broken([]))
    evs = chat(c, "你好")
    assert evs[0]["type"] == "error" and evs[-1]["type"] == "done"
    assert agent.CONVERSATIONS.get(uid) == []


def test_missing_key():
    c = TestClient(main.app)
    c.post("/api/signup", json={"email": "x@y.com", "store_name": "S"})
    os.environ["LLM_API_KEY"] = ""
    try:
        evs = chat(c, "你好")
    finally:
        os.environ["LLM_API_KEY"] = "test-key"
    assert evs[0]["type"] == "error" and "LLM_API_KEY" in evs[0]["message"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("✓", name)
