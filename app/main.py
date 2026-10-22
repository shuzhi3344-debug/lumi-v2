"""Lumi v2 可运行版本：FastAPI 入口。

启动：python main.py，然后打开 http://127.0.0.1:8000
"""
import json
import os
from typing import Dict, List, Optional

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import agent  # noqa: E402
import store  # noqa: E402

STATIC_DIR = os.path.join(BASE_DIR, "static")
ATTR_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ttclid")
CLIENT_EVENTS = {"dashboard_viewed", "paywall_viewed", "share_card_shared"}

store.init_db()
app = FastAPI(title="Lumi v2")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def current_user(request: Request) -> Dict:
    user = store.get_user(request.cookies.get("lumi_uid"))
    if not user:
        raise HTTPException(401, "not_logged_in")
    return user


def base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# ---------- 落地页：服务端捕获广告参数 ----------

@app.get("/")
def index(request: Request):
    resp = FileResponse(os.path.join(STATIC_DIR, "index.html"))
    params = {k: v for k, v in request.query_params.items() if k in ATTR_KEYS}
    if params:
        # 写入第一方 cookie：首触只记一次，末触每次覆盖（PRD 7.5 / A13）
        try:
            attr = json.loads(request.cookies.get("lumi_attr", "{}"))
        except json.JSONDecodeError:
            attr = {}
        touch = {**params, "landing_page": request.url.path}
        attr.setdefault("first_touch", touch)
        attr["last_touch"] = touch
        resp.set_cookie("lumi_attr", json.dumps(attr), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    return resp


@app.get("/events")
def events_page():
    return FileResponse(os.path.join(STATIC_DIR, "events.html"))


# ---------- 账户 ----------

class SignupBody(BaseModel):
    email: str
    store_name: str
    client_attribution: Optional[Dict] = None


@app.post("/api/signup")
def signup(body: SignupBody, request: Request):
    try:
        attr = json.loads(request.cookies.get("lumi_attr", "{}"))
    except json.JSONDecodeError:
        attr = {}
    if not attr and body.client_attribution:  # cookie 被拦截时，用前端 localStorage 的副本兜底
        attr = {"first_touch": body.client_attribution, "last_touch": body.client_attribution}
    user = store.create_user(body.email.strip(), body.store_name.strip() or "My Store", attr)
    first = attr.get("first_touch", {})
    store.track("signup_completed", "server", user["id"], {
        **{k: first.get(k) for k in ATTR_KEYS if first.get(k)},
        "landing_page": first.get("landing_page", "/"), "platform": "web",
    })
    resp = JSONResponse(user)
    resp.set_cookie("lumi_uid", user["id"], max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
    return resp


@app.get("/api/me")
def me(request: Request):
    return current_user(request)


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("lumi_uid")
    return resp


class ConnectBody(BaseModel):
    domain: str


@app.post("/api/store/connect")
def connect_store(body: ConnectBody, request: Request):
    user = current_user(request)
    domain = body.domain.strip().lower()
    if not domain.endswith(".myshopify.com"):
        raise HTTPException(400, "请输入 xxx.myshopify.com 格式的店铺地址")
    try:
        user = store.connect_store(user["id"], domain)
    except Exception:
        raise HTTPException(409, "这个店铺已被其他账号接入")
    store.track("store_connected", "server", user["id"], {
        "store_platform": "shopify", "store_id": domain.split(".")[0],
        "products_imported": len(store.PRODUCTS), "orders_last_30d": 214,
    })
    return user


# ---------- AI 对话（SSE 流式） ----------

class ChatBody(BaseModel):
    message: str


@app.post("/api/chat")
def chat(body: ChatBody, request: Request):
    user = current_user(request)
    url = base_url(request)

    def stream():
        for event in agent.run_turn(user["id"], body.message, url):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat/reset")
def chat_reset(request: Request):
    agent.reset(current_user(request)["id"])
    return {"ok": True}


# ---------- 帖子与效果 ----------

class PublishBody(BaseModel):
    channel: str = "instagram"


@app.post("/api/posts/{post_id}/publish")
def publish(post_id: str, body: PublishBody, request: Request):
    user = current_user(request)
    post = store.get_post(post_id, user["id"])
    if not post:
        raise HTTPException(404, "post_not_found")
    if not store.tracking_allowed(user) or not post["link_code"]:
        raise HTTPException(403, "只有带追踪的帖子才能从 Lumi 发布")
    post = store.publish_post(post_id)
    store.track("tracked_post_published", "server", user["id"], {
        "post_id": post_id, "short_link_id": post["link_code"], "discount_code": post["discount_code"], "channel": body.channel,
    })
    return {"ok": True, "short_url": f"{base_url(request)}/l/{post['link_code']}"}


@app.get("/api/dashboard")
def dashboard(request: Request, days: int = 30):
    user = current_user(request)
    if user["plan"] == "free":
        return {"locked": True}  # 效果数据在后端就不下发给 Free 用户
    if not user["connected"]:
        return {"locked": False, "connected": False}
    stats = store.post_stats(user["id"], days=days)
    totals = {k: round(sum(s[k] for s in stats), 2) for k in ("clicks", "orders", "gmv")}
    styles = store.style_conversion(stats)
    suggestion = None
    if len(styles) >= 2 and styles[-1]["conversion"]:
        best, worst = styles[0], styles[-1]
        suggestion = {"best": best, "worst": worst, "ratio": round(best["conversion"] / worst["conversion"], 2)}
    for s in stats:
        s["short_url"] = f"{base_url(request)}/l/{s['link_code']}" if s["link_code"] else None
    return {"locked": False, "connected": True, "days": days, "totals": totals, "posts": stats,
            "styles": styles, "weekly": store.weekly_orders(user["id"]), "suggestion": suggestion}


# ---------- 付费 ----------

class SubscribeBody(BaseModel):
    plan: str
    trigger: str = "pricing_page"


@app.post("/api/subscribe")
def subscribe(body: SubscribeBody, request: Request):
    """模拟支付成功回调：真实环境中由 Stripe webhook 触发，这里直接视为支付成功。"""
    user = current_user(request)
    if body.plan not in ("pro", "business"):
        raise HTTPException(400, "unknown_plan")
    user = store.set_plan(user["id"], body.plan)
    attr = user["attribution"]
    store.track("subscription_started", "server", user["id"], {
        "plan": body.plan, "price": store.PLAN_PRICES[body.plan], "platform": "web", "trigger": body.trigger,
        "first_touch": attr.get("first_touch"), "last_touch": attr.get("last_touch"),
    })
    return user


# ---------- 前端埋点 ----------

class EventBody(BaseModel):
    name: str
    props: Dict = {}


@app.post("/api/events")
def client_event(body: EventBody, request: Request):
    if body.name not in CLIENT_EVENTS:
        raise HTTPException(400, "该事件必须由服务端上报")
    store.track(body.name, "client", current_user(request)["id"], body.props)
    return {"ok": True}


@app.get("/api/events")
def get_events(request: Request, scope: str = "me"):
    uid = None if scope == "all" else request.cookies.get("lumi_uid")
    return store.list_events(uid)


# ---------- 短链跳转 ----------

@app.get("/l/{code}")
def short_link(code: str, request: Request):
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    res = store.record_click(code, ip, ua)
    if not res:
        raise HTTPException(404, "link_not_found")
    post = res["post"]
    if res["counted"] or res["is_bot"]:
        store.track("link_clicked", "server", post["user_id"], {
            "short_link_id": code, "post_id": post["id"], "referrer": request.headers.get("referer"),
            "device": "mobile" if "Mobile" in ua else "desktop", "is_bot": res["is_bot"],
        })
    owner = store.get_user(post["user_id"])
    return RedirectResponse(
        f"/shop/{post['product_id']}?shop={owner['store_domain']}&utm_source=lumi&utm_medium=social&utm_campaign={post['id']}",
        status_code=302,
    )


@app.get("/shop/{product_id}", response_class=HTMLResponse)
def shop_page(product_id: str):
    if not store.product(product_id):
        raise HTTPException(404, "product_not_found")
    return FileResponse(os.path.join(STATIC_DIR, "shop.html"))


@app.get("/api/products/{product_id}")
def get_product(product_id: str):
    p = store.product(product_id)
    if not p:
        raise HTTPException(404, "product_not_found")
    return p


# ---------- 模拟 Shopify webhook ----------

class OrderWebhook(BaseModel):
    id: str
    total_price: float
    currency: str = "USD"
    product_id: Optional[str] = None
    landing_site: Optional[str] = None
    discount_codes: List[str] = []


@app.post("/webhooks/shopify/orders")
def order_webhook(body: OrderWebhook, x_shopify_shop_domain: str = Header(...)):
    res = store.attribute_order(x_shopify_shop_domain, body.model_dump())
    if not res["ok"]:
        raise HTTPException(404, res["reason"])
    if not res["duplicate"] and res["post_id"]:
        store.track("order_attributed", "server", res["user_id"], {
            k: res[k] for k in ("order_id", "post_id", "revenue", "currency", "match_method", "click_to_order_hours")
        })
    return res


class RefundWebhook(BaseModel):
    order_id: str
    amount: float


@app.post("/webhooks/shopify/refunds")
def refund_webhook(body: RefundWebhook, x_shopify_shop_domain: str = Header(...)):
    res = store.refund_order(x_shopify_shop_domain, body.order_id, body.amount)
    if not res["ok"]:
        raise HTTPException(404, res["reason"])
    return res


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
