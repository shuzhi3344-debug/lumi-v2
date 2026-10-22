"""数据层：SQLite 表结构、额度、埋点、帖子、短链点击与订单归因。

所有涉及钱、额度、归因的规则都在这里实现（对应 docs/PRD.md 第 6、7 节），
前端只负责展示。
"""
import json
import os
import random
import secrets
import sqlite3
import string
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("LUMI_DB_PATH", os.path.join(BASE_DIR, "lumi.db"))

PLAN_LIMITS = {"free": 10, "pro": 500, "business": 2000}
PLAN_PRICES = {"free": 0, "pro": 29, "business": 79}
ATTRIBUTION_WINDOW_DAYS = 7
# 只认爬虫和链接预览抓取；IG / TikTok 内置浏览器的真人点击 UA 里也带 instagram / tiktok，不能算机器人
BOT_UA_MARKERS = ("bot", "crawler", "spider", "facebookexternalhit", "preview", "curl", "python-requests")

_lock = threading.Lock()


def _load(name: str) -> Any:
    with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
        return json.load(f)


PRODUCTS: List[Dict] = _load("products.json")
REVIEWS: Dict[str, List[Dict]] = _load("reviews.json")
HISTORY_POSTS: List[Dict] = _load("history_posts.json")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def month_key(dt: Optional[datetime] = None) -> str:
    return (dt or now()).strftime("%Y-%m")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  store_name TEXT NOT NULL,
  store_domain TEXT UNIQUE,
  plan TEXT NOT NULL DEFAULT 'free',
  quota_used INTEGER NOT NULL DEFAULT 0,
  quota_month TEXT NOT NULL,
  attribution TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  product_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  language TEXT NOT NULL,
  style TEXT NOT NULL,
  title TEXT NOT NULL,
  caption TEXT NOT NULL,
  hashtags TEXT NOT NULL DEFAULT '[]',
  link_code TEXT UNIQUE,
  discount_code TEXT UNIQUE,
  status TEXT NOT NULL DEFAULT 'draft',
  base_clicks INTEGER NOT NULL DEFAULT 0,
  base_orders INTEGER NOT NULL DEFAULT 0,
  base_gmv REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  published_at TEXT
);
CREATE TABLE IF NOT EXISTS clicks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id TEXT NOT NULL REFERENCES posts(id),
  ts TEXT NOT NULL,
  ip TEXT,
  ua TEXT,
  is_bot INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  post_id TEXT REFERENCES posts(id),
  product_id TEXT,
  revenue REAL NOT NULL,
  currency TEXT NOT NULL,
  match_method TEXT NOT NULL,
  refunded_amount REAL NOT NULL DEFAULT 0,
  ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  side TEXT NOT NULL,
  user_id TEXT,
  props TEXT NOT NULL,
  ts TEXT NOT NULL
);
"""


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


# ---------- 埋点 ----------

def track(name: str, side: str, user_id: Optional[str], props: Dict) -> None:
    """记录一条事件。side 为 server / client；公共属性 plan 在这里统一补上。"""
    with db() as conn:
        if user_id and "plan" not in props:
            row = conn.execute("SELECT plan FROM users WHERE id=?", (user_id,)).fetchone()
            if row:
                props = {**props, "plan": row["plan"]}
        conn.execute(
            "INSERT INTO events(name, side, user_id, props, ts) VALUES (?,?,?,?,?)",
            (name, side, user_id, json.dumps(props, ensure_ascii=False), iso(now())),
        )


def list_events(user_id: Optional[str], limit: int = 200) -> List[Dict]:
    with db() as conn:
        if user_id:
            rows = conn.execute(
                "SELECT * FROM events WHERE user_id=? OR user_id IS NULL ORDER BY id DESC LIMIT ?", (user_id, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "props": json.loads(r["props"])} for r in rows]


# ---------- 用户与额度 ----------

def _user_dict(row: sqlite3.Row) -> Dict:
    u = dict(row)
    u["attribution"] = json.loads(u["attribution"])
    u["quota_limit"] = PLAN_LIMITS[u["plan"]]
    u["connected"] = bool(u["store_domain"])
    return u


def create_user(email: str, store_name: str, attribution: Dict) -> Dict:
    uid = "u_" + secrets.token_hex(6)
    with db() as conn:
        conn.execute(
            "INSERT INTO users(id, email, store_name, quota_month, attribution, created_at) VALUES (?,?,?,?,?,?)",
            (uid, email, store_name, month_key(), json.dumps(attribution, ensure_ascii=False), iso(now())),
        )
    return get_user(uid)


def get_user(uid: Optional[str]) -> Optional[Dict]:
    if not uid:
        return None
    with db() as conn:
        # 自然月重置额度（UTC）
        conn.execute("UPDATE users SET quota_used=0, quota_month=? WHERE id=? AND quota_month<>?", (month_key(), uid, month_key()))
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return _user_dict(row) if row else None


def reserve_quota(uid: str) -> bool:
    """在调用模型前原子地占用 1 次额度；用条件 UPDATE 保证并发时不会超额。"""
    get_user(uid)  # 先触发月度重置
    with db() as conn:
        user = conn.execute("SELECT plan FROM users WHERE id=?", (uid,)).fetchone()
        cur = conn.execute(
            "UPDATE users SET quota_used = quota_used + 1 WHERE id=? AND quota_used < ?",
            (uid, PLAN_LIMITS[user["plan"]]),
        )
        return cur.rowcount == 1


def release_quota(uid: str) -> None:
    """生成失败时退回额度（PRD 7.1：失败不扣）。"""
    with db() as conn:
        conn.execute("UPDATE users SET quota_used = MAX(quota_used - 1, 0) WHERE id=?", (uid,))


def set_plan(uid: str, plan: str) -> Dict:
    if plan not in PLAN_LIMITS:
        raise ValueError("unknown plan")
    with db() as conn:
        conn.execute("UPDATE users SET plan=? WHERE id=?", (plan, uid))
    return get_user(uid)


def connect_store(uid: str, domain: str) -> Dict:
    """模拟 Shopify 只读授权：记下店铺域名，并导入近 30 天的历史帖子表现。"""
    with db() as conn:
        conn.execute("UPDATE users SET store_domain=? WHERE id=?", (domain, uid))
        # 只看是否导入过历史帖子；接入前生成的草稿不影响导入
        already = conn.execute("SELECT COUNT(*) FROM posts WHERE user_id=? AND base_clicks>0", (uid,)).fetchone()[0]
    user = get_user(uid)
    if not already:
        for h in HISTORY_POSTS:
            published = now() - timedelta(days=h["days_ago"])
            post_id = _insert_post(
                uid, h["product_id"], h["platform"], "zh", h["style"], h["title"], h["title"], [],
                status="published", created_at=published,
                base=(h["clicks"], h["orders"], h["gmv"]),
            )
            ensure_tracking(post_id, user)
    return user


# ---------- 帖子 ----------

def product(pid: str) -> Optional[Dict]:
    return next((p for p in PRODUCTS if p["id"] == pid), None)


def _insert_post(uid, product_id, platform, language, style, title, caption, hashtags,
                 status="draft", created_at=None, base=(0, 0, 0)) -> str:
    post_id = "pst_" + secrets.token_hex(4)
    created_at = created_at or now()
    with db() as conn:
        conn.execute(
            """INSERT INTO posts(id, user_id, product_id, platform, language, style, title, caption, hashtags,
                                 status, base_clicks, base_orders, base_gmv, created_at, published_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (post_id, uid, product_id, platform, language, style, title, caption,
             json.dumps(hashtags, ensure_ascii=False), status, base[0], base[1], base[2],
             iso(created_at), iso(created_at) if status == "published" else None),
        )
    return post_id


def create_post(uid: str, product_id: str, platform: str, language: str, style: str,
                title: str, caption: str, hashtags: List[str]) -> str:
    return _insert_post(uid, product_id, platform, language, style, title, caption, hashtags)


def update_post_content(post_id: str, title: str, caption: str, hashtags: List[str]) -> None:
    with db() as conn:
        conn.execute("UPDATE posts SET title=?, caption=?, hashtags=? WHERE id=?",
                     (title, caption, json.dumps(hashtags, ensure_ascii=False), post_id))


def delete_post(post_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM posts WHERE id=? AND status='draft'", (post_id,))


def get_post(post_id: str, uid: Optional[str] = None) -> Optional[Dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row or (uid and row["user_id"] != uid):
        return None
    p = dict(row)
    p["hashtags"] = json.loads(p["hashtags"])
    return p


def tracking_allowed(user: Dict) -> bool:
    """追踪只给 Pro / Business 且已接入店铺的商家（PRD 第 5 节、7.4）。"""
    return user["plan"] in ("pro", "business") and user["connected"]


def ensure_tracking(post_id: str, user: Dict) -> Dict:
    """给帖子挂上短链和专属优惠码；已有则复用（重新生成不新建，PRD 7.2）。"""
    post = get_post(post_id)
    if post["link_code"]:
        return post
    prefix = "".join(c for c in user["store_name"].upper() if c.isalpha())[:4] or "LUMI"
    with _lock, db() as conn:
        while True:
            code = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
            if not conn.execute("SELECT 1 FROM posts WHERE link_code=?", (code,)).fetchone():
                break
        n = conn.execute("SELECT COUNT(*) FROM posts WHERE discount_code IS NOT NULL").fetchone()[0]
        conn.execute("UPDATE posts SET link_code=?, discount_code=? WHERE id=?",
                     (code, f"{prefix}15-{101 + n}", post_id))
    return get_post(post_id)


def publish_post(post_id: str) -> Dict:
    with db() as conn:
        conn.execute("UPDATE posts SET status='published', published_at=? WHERE id=? AND status<>'published'",
                     (iso(now()), post_id))
    return get_post(post_id)


# ---------- 点击 ----------

def is_bot(ua: str) -> bool:
    ua = (ua or "").lower()
    return any(m in ua for m in BOT_UA_MARKERS)


def record_click(link_code: str, ip: str, ua: str) -> Optional[Dict]:
    """返回 {post, counted, is_bot}；同一 IP 10 秒内重复点击只计一次。"""
    with db() as conn:
        row = conn.execute("SELECT * FROM posts WHERE link_code=?", (link_code,)).fetchone()
        if not row:
            return None
        bot = is_bot(ua)
        recent = conn.execute(
            "SELECT 1 FROM clicks WHERE post_id=? AND ip=? AND ts>? AND is_bot=0",
            (row["id"], ip, iso(now() - timedelta(seconds=10))),
        ).fetchone()
        counted = not bot and not recent
        if bot or counted:
            conn.execute("INSERT INTO clicks(post_id, ts, ip, ua, is_bot) VALUES (?,?,?,?,?)",
                         (row["id"], iso(now()), ip, ua, int(bot)))
    return {"post": dict(row), "counted": counted, "is_bot": bot}


# ---------- 订单归因（PRD 7.3） ----------

def attribute_order(shop_domain: str, payload: Dict) -> Dict:
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE store_domain=?", (shop_domain,)).fetchone()
        if not user:
            return {"ok": False, "reason": "unknown_shop"}
        order_id = str(payload["id"])
        existing = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if existing:  # 重复 webhook 幂等
            return {"ok": True, "duplicate": True, "order": dict(existing)}

        uid = user["id"]
        codes = [c.upper() for c in payload.get("discount_codes", []) if c]
        code_post = None
        for c in codes:
            code_post = conn.execute("SELECT * FROM posts WHERE upper(discount_code)=? AND user_id=?", (c, uid)).fetchone()
            if code_post:
                break

        utm_post, click_hours = None, None
        campaign = parse_qs(urlparse(payload.get("landing_site") or "").query).get("utm_campaign", [None])[0]
        if campaign:
            candidate = conn.execute("SELECT * FROM posts WHERE id=? AND user_id=?", (campaign, uid)).fetchone()
            if candidate:
                last_click = conn.execute(
                    "SELECT ts FROM clicks WHERE post_id=? AND is_bot=0 ORDER BY ts DESC LIMIT 1", (candidate["id"],)
                ).fetchone()
                if last_click:
                    gap = now() - parse_iso(last_click["ts"])
                    if gap <= timedelta(days=ATTRIBUTION_WINDOW_DAYS):
                        utm_post, click_hours = candidate, round(gap.total_seconds() / 3600, 2)

        # 两者都命中且指向不同帖子时，以优惠码为准（用户主动输入，意图更明确）
        if code_post:
            post, method = code_post, "code"
        elif utm_post:
            post, method = utm_post, "utm"
        else:
            post, method = None, "none"

        conn.execute(
            "INSERT INTO orders(order_id, user_id, post_id, product_id, revenue, currency, match_method, ts) VALUES (?,?,?,?,?,?,?,?)",
            (order_id, uid, post["id"] if post else None, payload.get("product_id"),
             float(payload["total_price"]), payload.get("currency", "USD"), method, iso(now())),
        )
    result = {"ok": True, "duplicate": False, "user_id": uid, "order_id": order_id,
              "post_id": post["id"] if post else None, "match_method": method,
              "revenue": float(payload["total_price"]), "currency": payload.get("currency", "USD"),
              "click_to_order_hours": click_hours if method == "utm" else None}
    return result


def refund_order(shop_domain: str, order_id: str, amount: float) -> Dict:
    with db() as conn:
        row = conn.execute(
            "SELECT o.* FROM orders o JOIN users u ON u.id=o.user_id WHERE o.order_id=? AND u.store_domain=?",
            (order_id, shop_domain),
        ).fetchone()
        if not row:
            return {"ok": False, "reason": "unknown_order"}
        refunded = min(row["revenue"], row["refunded_amount"] + amount)
        conn.execute("UPDATE orders SET refunded_amount=? WHERE order_id=?", (refunded, order_id))
    return {"ok": True, "order_id": order_id, "refunded_amount": refunded, "fully_refunded": refunded >= row["revenue"]}


# ---------- 效果数据 ----------

def post_stats(uid: str, days: int = 30, post_id: Optional[str] = None) -> List[Dict]:
    since = iso(now() - timedelta(days=days))
    with db() as conn:
        q = """
        SELECT p.*,
          p.base_clicks + (SELECT COUNT(*) FROM clicks c WHERE c.post_id=p.id AND c.is_bot=0) AS clicks,
          p.base_orders + (SELECT COUNT(*) FROM orders o WHERE o.post_id=p.id AND o.refunded_amount < o.revenue) AS orders,
          p.base_gmv + (SELECT COALESCE(SUM(o.revenue - o.refunded_amount), 0) FROM orders o WHERE o.post_id=p.id) AS gmv
        FROM posts p
        WHERE p.user_id=? AND p.status='published' AND p.published_at>=?
        """
        args: List[Any] = [uid, since]
        if post_id:
            q += " AND p.id=?"
            args.append(post_id)
        rows = conn.execute(q + " ORDER BY p.published_at DESC", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "post_id": d["id"], "title": d["title"], "product_id": d["product_id"], "platform": d["platform"],
            "style": d["style"], "published_at": d["published_at"], "link_code": d["link_code"],
            "discount_code": d["discount_code"], "clicks": d["clicks"], "orders": d["orders"],
            "gmv": round(d["gmv"], 2), "fresh": d["base_clicks"] == 0 and d["clicks"] == 0,
        })
    return out


def style_conversion(stats: List[Dict]) -> List[Dict]:
    agg: Dict[str, Dict] = {}
    for s in stats:
        a = agg.setdefault(s["style"], {"style": s["style"], "clicks": 0, "orders": 0})
        a["clicks"] += s["clicks"]
        a["orders"] += s["orders"]
    for a in agg.values():
        a["conversion"] = round(a["orders"] / a["clicks"] * 100, 2) if a["clicks"] else 0
    return sorted(agg.values(), key=lambda a: a["conversion"], reverse=True)


def weekly_orders(uid: str, weeks: int = 4) -> List[Dict]:
    """历史帖子的订单按发布日计入，真实订单按下单时间计入。"""
    start = now() - timedelta(weeks=weeks)
    buckets = [0] * weeks
    with db() as conn:
        for r in conn.execute("SELECT base_orders, published_at FROM posts WHERE user_id=? AND published_at>=?", (uid, iso(start))):
            i = min(int((parse_iso(r["published_at"]) - start).days // 7), weeks - 1)
            buckets[i] += r["base_orders"]
        for r in conn.execute("SELECT ts FROM orders WHERE user_id=? AND post_id IS NOT NULL AND refunded_amount < revenue AND ts>=?", (uid, iso(start))):
            i = min(int((parse_iso(r["ts"]) - start).days // 7), weeks - 1)
            buckets[i] += 1
    return [{"week": f"W-{weeks - 1 - i}" if i < weeks - 1 else "本周", "orders": n} for i, n in enumerate(buckets)]
