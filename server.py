import os
import time
import json
import random
import hashlib
import secrets
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO
import redis
from redis import ConnectionPool

# ==============================
# 環境變數 & Redis 連線
# ==============================
REDIS_HOST = os.environ.get("REDIS_HOST")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_USERNAME = os.environ.get("REDIS_USERNAME", "default")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ADMIN_TOKEN_TTL = 3600  # 1 小時

# stream 名稱
ACTIONS_STREAM = "stream:actions"
BIDS_STREAM = "stream:auction:bids"
SOLD_STREAM = "stream:auction:sold"

# Redis pool
pool = ConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    username=REDIS_USERNAME,
    password=REDIS_PASSWORD,
    decode_responses=True,
    max_connections=30,
)
r = redis.Redis(connection_pool=pool)

try:
    r.ping()
    print("[Redis] connected")
except Exception as e:
    print("[Redis] connect failed:", e)

# ==============================
# Flask + Socket.IO
# ==============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")


# ==============================
# 工具函數
# ==============================

def now_iso() -> str:
    return datetime.utcnow().isoformat()


def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def log_action(user: str, action: str, detail=None):
    """寫入行為紀錄 stream:actions"""
    try:
        info = {
            "ts": now_iso(),
            "user": user or "",
            "action": action,
            "detail": json.dumps(detail, ensure_ascii=False)
            if isinstance(detail, (dict, list))
            else (str(detail) if detail is not None else ""),
        }
        r.xadd(ACTIONS_STREAM, info, maxlen=10000, approximate=True)
    except Exception as e:
        print("[log_action] error:", e)


def log_bid(auction_id: int, bidder: str, amount: int):
    try:
        info = {
            "ts": now_iso(),
            "auction_id": str(auction_id),
            "bidder": bidder,
            "amount": str(amount),
        }
        r.xadd(BIDS_STREAM, info, maxlen=5000, approximate=True)
    except Exception as e:
        print("[log_bid] error:", e)


def log_sold(auction_id: int, seller: str, buyer: str, item_id: str, qty: int, price: int):
    try:
        info = {
            "ts": now_iso(),
            "auction_id": str(auction_id),
            "seller": seller,
            "buyer": buyer,
            "item_id": item_id,
            "qty": str(qty),
            "price": str(price),
        }
        r.xadd(SOLD_STREAM, info, maxlen=5000, approximate=True)
    except Exception as e:
        print("[log_sold] error:", e)


def broadcast(event, data):
    """目前前端還沒接 socket，可以先保留"""
    try:
        socketio.emit(event, data, broadcast=True)
    except Exception as e:
        print("[socketio] error:", e)


# ==============================
# 使用者 / Session
# ==============================

def get_user_key(username: str) -> str:
    return f"user:{username}"


def get_inventory_key(username: str) -> str:
    return f"inventory:{username}"


def create_user(username: str, password: str):
    key = get_user_key(username)
    if r.exists(key):
        return False, "帳號已存在"

    now = now_iso()
    data = {
        "username": username,
        "password_hash": hash_password(password),
        "gold": "100",
        "level": "1",
        "exp": "0",
        "created_at": now,
        "last_login_at": now,
        "banned": "0",
    }
    r.hset(key, mapping=data)
    log_action(username, "register", {"gold": 100})
    return True, data


def get_user(username: str):
    key = get_user_key(username)
    if not r.exists(key):
        return None
    data = r.hgetall(key)
    # 型別轉換
    data["gold"] = int(data.get("gold", 0))
    data["level"] = int(data.get("level", 1))
    data["exp"] = int(data.get("exp", 0))
    data["banned"] = data.get("banned", "0")
    return data


def set_user_field(username: str, field: str, value):
    key = get_user_key(username)
    r.hset(key, field, str(value))


def create_session(username: str, device: str, ip: str):
    """建立單一登入 Session，如果已有則回傳 False 與原 Session 資訊。"""
    sess_key = f"session:{username}"
    meta_key = f"session_meta:{username}"

    if r.exists(sess_key):
        meta = r.hgetall(meta_key) if r.exists(meta_key) else {}
        return False, meta

    token = secrets.token_hex(32)
    now = now_iso()
    pipe = r.pipeline()
    pipe.set(sess_key, token)
    pipe.hset(
        meta_key,
        mapping={
            "device": device or "",
            "ip": ip or "",
            "login_time": now,
        },
    )
    pipe.set(f"token:{token}", username)
    pipe.execute()
    return True, {
        "token": token,
        "device": device,
        "ip": ip,
        "login_time": now,
    }


def destroy_session(username: str, token: str | None = None):
    sess_key = f"session:{username}"
    meta_key = f"session_meta:{username}"
    existing = r.get(sess_key)
    if not existing:
        return

    # 若有傳 token，可檢查是否一致；這裡允許前端只要帶 username 就能登出自己
    r.delete(sess_key)
    r.delete(meta_key)
    if existing:
        r.delete(f"token:{existing}")


def auth_from_request():
    """從 Authorization: Bearer 解析玩家 username，失敗回 None"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    username = r.get(f"token:{token}")
    return username


# ==============================
# 商店與背包
# ==============================

DEFAULT_ITEMS = {
    "potion_small": {
        "name": "小型生命藥水",
        "price": 50,
        "desc": "回復少量生命值",
        "max_stack": 99,
    },
    "potion_big": {
        "name": "大型生命藥水",
        "price": 150,
        "desc": "回復大量生命值",
        "max_stack": 99,
    },
    "sword_bronze": {
        "name": "青銅劍",
        "price": 200,
        "desc": "基礎近戰武器",
        "max_stack": 1,
    },
    "sword_silver": {
        "name": "白銀劍",
        "price": 800,
        "desc": "高級近戰武器",
        "max_stack": 1,
    },
    "armor_leather": {
        "name": "皮甲",
        "price": 300,
        "desc": "新手防具",
        "max_stack": 1,
    },
}


def init_game_data():
    """初始化商店物品到 Redis"""
    for item_id, data in DEFAULT_ITEMS.items():
        key = f"item:{item_id}"
        if not r.exists(key):
            mapping = {
                "name": data["name"],
                "price": str(data["price"]),
                "desc": data["desc"],
                "max_stack": str(data["max_stack"]),
            }
            r.hset(key, mapping=mapping)
    print("✓ 初始化商店物品完成")


def get_inventory(username: str) -> dict:
    key = get_inventory_key(username)
    inv = r.hgetall(key)  # {item_id: qty_str}
    return {item_id: int(qty) for item_id, qty in inv.items()}


def change_inventory(username: str, item_id: str, delta_qty: int):
    key = get_inventory_key(username)
    current = int(r.hget(key, item_id) or 0)
    new_qty = current + delta_qty
    if new_qty <= 0:
        r.hdel(key, item_id)
        return 0
    r.hset(key, item_id, new_qty)
    return new_qty


# ==============================
# 拍賣系統
# ==============================

def get_next_auction_id() -> int:
    return int(r.incr("auction:next_id"))


def get_auction_key(aid: int) -> str:
    return f"auction:{aid}"


def load_auction(aid: int) -> dict | None:
    key = get_auction_key(aid)
    if not r.exists(key):
        return None
    data = r.hgetall(key)
    data["auction_id"] = aid
    # 型別調整
    for k in ("qty", "start_price", "current_price", "buyout_price"):
        if k in data and data[k] is not None:
            try:
                data[k] = int(data[k])
            except ValueError:
                data[k] = 0
    return data


def save_auction(aid: int, mapping: dict):
    key = get_auction_key(aid)
    mapping = {k: (str(v) if v is not None else "") for k, v in mapping.items()}
    r.hset(key, mapping=mapping)


# ==============================
# Rate limit & 點擊邏輯
# ==============================

CLICK_WINDOW_MS = 10000
CLICK_MAX_HITS = 20


def check_click_rate(username: str):
    """滑動視窗限流"""
    key = f"rate:click:{username}"
    now_ms = int(time.time() * 1000)
    window_start = now_ms - CLICK_WINDOW_MS

    # 移除過期紀錄
    r.zremrangebyscore(key, 0, window_start)

    current_hits = r.zcard(key)
    if current_hits >= CLICK_MAX_HITS:
        oldest = r.zrange(key, 0, 0, withscores=True)
        retry_after_ms = 0
        if oldest:
            _, oldest_ts = oldest[0]
            retry_after_ms = max(0, int(oldest_ts + CLICK_WINDOW_MS - now_ms))

        log_action(
            username,
            "violation:click_rate",
            {"retry_after_ms": retry_after_ms, "window_ms": CLICK_WINDOW_MS, "max_hits": CLICK_MAX_HITS},
        )

        return False, retry_after_ms

    # 允許這次點擊
    r.zadd(key, {str(now_ms): now_ms})
    return True, 0


def process_click(username: str):
    """計算 combo / critical / 金幣"""

    combo_key = f"combo:{username}"
    total_clicks_key = f"total_clicks:{username}"

    # combo: 若 3 秒內連點，combo+1，否則重置
    prev_ts = r.get(f"combo_ts:{username}")
    now_ms = int(time.time() * 1000)
    if prev_ts and now_ms - int(prev_ts) <= 3000:
        combo = int(r.get(combo_key) or 1) + 1
    else:
        combo = 1

    r.set(combo_key, combo)
    r.set(f"combo_ts:{username}", now_ms)

    # critical 機率 10%
    critical = random.random() < 0.1

    # 金幣
    gain = combo
    if critical:
        gain *= 2

    # 更新玩家金幣 & 總點擊數
    user = get_user(username)
    if not user:
        return None

    new_gold = user["gold"] + gain
    set_user_field(username, "gold", new_gold)

    total_clicks = int(r.get(total_clicks_key) or 0) + 1
    r.set(total_clicks_key, total_clicks)

    # 排行榜（目前先只是累計）
    r.zadd("leaderboard:gold", {username: new_gold})
    r.zadd("leaderboard:clicks", {username: total_clicks})

    log_action(username, "click", {"gain": gain, "combo": combo, "critical": critical})

    return {
        "gold": new_gold,
        "combo": combo,
        "critical": critical,
        "total_clicks": total_clicks,
        "cooldown_ms": 500,  # 給前端參考用
    }


# ==============================
# 靜態頁面
# ==============================

@app.route("/")
def index_page():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/admin")
def admin_page():
    return send_from_directory(BASE_DIR, "admin.html")


# ==============================
# Auth APIs
# ==============================

@app.route("/auth/register", methods=["POST"])
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    confirm = data.get("confirm_password") or ""

    if not username or not password or not confirm:
        return jsonify({"success": False, "message": "請填寫完整資料"}), 400

    if password != confirm:
        return jsonify({"success": False, "message": "兩次密碼不一致"}), 400

    ok, result = create_user(username, password)
    if not ok:
        return jsonify({"success": False, "message": result}), 400

    return jsonify({"success": True, "message": "註冊成功"})


@app.route("/auth/login", methods=["POST"])
@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    device = data.get("device") or ""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""

    if not username or not password:
        return jsonify({"success": False, "message": "請輸入帳號與密碼"}), 400

    user = get_user(username)
    if not user:
        return jsonify({"success": False, "message": "帳號不存在"}), 404

    if user.get("banned") == "1":
        return jsonify({"success": False, "message": "此帳號已被封禁"}), 403

    if hash_password(password) != user["password_hash"]:
        return jsonify({"success": False, "message": "密碼錯誤"}), 401

    ok, sess = create_session(username, device=device, ip=ip)
    if not ok:
        # 已經有 session → 回傳 ALREADY_LOGGED_IN
        meta = sess or {}
        return (
            jsonify(
                {
                    "success": False,
                    "code": "ALREADY_LOGGED_IN",
                    "message": "此帳號已在其他裝置登入",
                    "device": meta.get("device", ""),
                    "ip": meta.get("ip", ""),
                    "login_time": meta.get("login_time", ""),
                }
            ),
            409,
        )

    set_user_field(username, "last_login_at", now_iso())
    log_action(username, "login", {"device": device, "ip": ip})

    user = get_user(username)
    resp = {
        "success": True,
        "message": "登入成功",
        "username": username,
        "token": sess["token"],
        "player": {
            "username": username,
            "gold": user["gold"],
            "level": user["level"],
            "exp": user["exp"],
        },
    }
    return jsonify(resp)


@app.route("/auth/logout", methods=["POST"])
@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    token = data.get("token") or ""

    if not username:
        return jsonify({"success": False, "message": "缺少 username"}), 400

    destroy_session(username, token)
    log_action(username, "logout", None)
    return jsonify({"success": True, "message": "已登出"})


@app.route("/player/<username>")
def api_player_info(username):
    """給前端刷新玩家資料"""
    user = get_user(username)
    if not user:
        return jsonify({"success": False, "message": "玩家不存在"}), 404

    inv = get_inventory(username)
    return jsonify(
        {
            "success": True,
            "player": {
                "username": username,
                "gold": user["gold"],
                "level": user["level"],
                "exp": user["exp"],
                "inventory": [{"item_id": k, "qty": v} for k, v in inv.items()],
            },
        }
    )


# ==============================
# 點擊金幣
# ==============================

@app.route("/click/<username>", methods=["POST"])
@app.route("/api/click/<username>", methods=["POST"])
def api_click(username):
    auth_user = auth_from_request()
    if not auth_user or auth_user != username:
        log_action(auth_user or "", "violation:unauthorized_click", {"target": username})
        return jsonify({"success": False, "message": "未授權"}), 401

    # 限流
    ok, retry_after_ms = check_click_rate(username)
    if not ok:
        return (
            jsonify(
                {
                    "success": False,
                    "message": "點擊過於頻繁（滑動視窗限流）",
                    "retry_after_ms": retry_after_ms,
                    "limit_window_ms": CLICK_WINDOW_MS,
                    "limit_max_hits": CLICK_MAX_HITS,
                }
            ),
            429,
        )

    result = process_click(username)
    if not result:
        return jsonify({"success": False, "message": "玩家不存在"}), 404

    return jsonify({"success": True, **result})


# ==============================
# 商店
# ==============================

@app.route("/shop/items")
@app.route("/api/shop/items")
def api_shop_items():
    items = []
    for item_id, base in DEFAULT_ITEMS.items():
        items.append(
            {
                "item_id": item_id,
                "name": base["name"],
                "price": base["price"],
                "desc": base["desc"],
                "max_stack": base["max_stack"],
            }
        )
    return jsonify({"success": True, "items": items})


@app.route("/shop/inventory/<username>")
@app.route("/api/shop/inventory/<username>")
def api_inventory(username):
    auth_user = auth_from_request()
    # 嚴格要求只能看自己的背包（Admin route 另外實作）
    if not auth_user or auth_user != username:
        log_action(auth_user or "", "violation:inventory_read", {"target": username})
        return jsonify({"success": False, "message": "未授權"}), 401

    inv = get_inventory(username)
    return jsonify(
        {
            "success": True,
            "inventory": {"items": [{"item_id": k, "qty": v} for k, v in inv.items()]},
        }
    )


@app.route("/shop/buy", methods=["POST"])
@app.route("/api/shop/buy", methods=["POST"])
def api_shop_buy():
    auth_user = auth_from_request()
    data = request.json or {}
    username = (data.get("username") or "").strip()
    item_id = data.get("item_id")
    qty = int(data.get("qty") or 0)

    if not auth_user or auth_user != username:
        log_action(auth_user or "", "violation:shop_buy_unauthorized", {"target": username})
        return jsonify({"success": False, "message": "未授權"}), 401

    if not item_id or qty <= 0:
        return jsonify({"success": False, "message": "參數錯誤"}), 400

    base = DEFAULT_ITEMS.get(item_id)
    if not base:
        return jsonify({"success": False, "message": "物品不存在"}), 404

    user = get_user(username)
    if not user:
        return jsonify({"success": False, "message": "玩家不存在"}), 404

    cost = base["price"] * qty
    if user["gold"] < cost:
        log_action(username, "violation:insufficient_gold", {"cost": cost, "gold": user["gold"]})
        return jsonify({"success": False, "message": "金幣不足"}), 400

    # 扣金幣、加背包
    new_gold = user["gold"] - cost
    set_user_field(username, "gold", new_gold)
    new_qty = change_inventory(username, item_id, qty)

    log_action(username, "shop_buy", {"item_id": item_id, "qty": qty, "cost": cost})

    return jsonify({"success": True, "gold": new_gold, "new_qty": new_qty})


# ==============================
# 拍賣 APIs
# ==============================

@app.route("/auction/list")
@app.route("/api/auction/list")
def api_auction_list():
    limit = int(request.args.get("limit", 50))
    ids = r.zrevrange("auction:open", 0, limit - 1)
    result = []
    for sid in ids:
        aid = int(sid)
        a = load_auction(aid)
        if a:
            result.append(a)
    return jsonify({"success": True, "items": result})


@app.route("/auction/<int:auction_id>")
@app.route("/api/auction/<int:auction_id>")
def api_auction_detail(auction_id):
    a = load_auction(auction_id)
    if not a:
        return jsonify({"success": False, "message": "拍賣不存在"}), 404
    return jsonify({"success": True, "auction": a})


@app.route("/auction/create", methods=["POST"])
@app.route("/api/auction/create", methods=["POST"])
def api_auction_create():
    auth_user = auth_from_request()
    data = request.json or {}
    username = (data.get("username") or "").strip()
    item_id = data.get("item_id")
    qty = int(data.get("qty") or 0)
    start_price = int(data.get("start_price") or 0)
    buyout_price = data.get("buyout_price")
    buyout_price = int(buyout_price) if buyout_price not in (None, "") else None

    if not auth_user or auth_user != username:
        log_action(auth_user or "", "violation:auction_create_unauthorized", {"target": username})
        return jsonify({"success": False, "message": "未授權"}), 401

    if not item_id or qty <= 0 or start_price <= 0:
        return jsonify({"success": False, "message": "參數錯誤"}), 400

    base = DEFAULT_ITEMS.get(item_id)
    if not base:
        return jsonify({"success": False, "message": "物品不存在"}), 404

    inv = get_inventory(username)
    if inv.get(item_id, 0) < qty:
        return jsonify({"success": False, "message": "背包數量不足"}), 400

    # 背包扣除
    change_inventory(username, item_id, -qty)

    aid = get_next_auction_id()
    created_at = now_iso()
    mapping = {
        "seller": username,
        "item_id": item_id,
        "qty": qty,
        "start_price": start_price,
        "current_price": start_price,
        "current_bidder": "",
        "buyout_price": buyout_price if buyout_price is not None else "",
        "status": "open",
        "created_at": created_at,
    }
    save_auction(aid, mapping)
    r.zadd("auction:open", {str(aid): time.time()})

    log_action(username, "auction_create", {"auction_id": aid, "item_id": item_id, "qty": qty})

    return jsonify({"success": True, "auction_id": aid})


@app.route("/auction/bid", methods=["POST"])
@app.route("/api/auction/bid", methods=["POST"])
def api_auction_bid():
    auth_user = auth_from_request()
    data = request.json or {}
    username = (data.get("username") or "").strip()
    auction_id = int(data.get("auction_id") or 0)
    bid_amount = int(data.get("bid_amount") or 0)

    if not auth_user or auth_user != username:
        log_action(auth_user or "", "violation:auction_bid_unauthorized", {"target": username})
        return jsonify({"success": False, "message": "未授權"}), 401

    if auction_id <= 0 or bid_amount <= 0:
        return jsonify({"success": False, "message": "參數錯誤"}), 400

    a = load_auction(auction_id)
    if not a or a.get("status") != "open":
        return jsonify({"success": False, "message": "拍賣不存在或已結束"}), 400

    if a["seller"] == username:
        return jsonify({"success": False, "message": "不能對自己的拍賣出價"}), 400

    if bid_amount <= a["current_price"]:
        return jsonify({"success": False, "message": "出價需高於目前價格"}), 400

    user = get_user(username)
    if not user:
        return jsonify({"success": False, "message": "玩家不存在"}), 404

    if user["gold"] < bid_amount:
        log_action(username, "violation:auction_bid_gold", {"need": bid_amount, "gold": user["gold"]})
        return jsonify({"success": False, "message": "金幣不足"}), 400

    # 不凍結金幣，只檢查有沒有足夠（簡化）
    a["current_price"] = bid_amount
    a["current_bidder"] = username
    save_auction(auction_id, a)

    log_bid(auction_id, username, bid_amount)
    log_action(username, "auction_bid", {"auction_id": auction_id, "amount": bid_amount})

    return jsonify({"success": True})


@app.route("/auction/buy_now", methods=["POST"])
@app.route("/api/auction/buy_now", methods=["POST"])
def api_auction_buy_now():
    auth_user = auth_from_request()
    data = request.json or {}
    username = (data.get("username") or "").strip()
    auction_id = int(data.get("auction_id") or 0)

    if not auth_user or auth_user != username:
        log_action(auth_user or "", "violation:auction_buy_unauthorized", {"target": username})
        return jsonify({"success": False, "message": "未授權"}), 401

    a = load_auction(auction_id)
    if not a or a.get("status") != "open":
        return jsonify({"success": False, "message": "拍賣不存在或已結束"}), 400

    if a["seller"] == username:
        return jsonify({"success": False, "message": "不能直購自己的拍賣"}), 400

    price = a.get("buyout_price") or a.get("current_price")
    price = int(price or 0)
    if price <= 0:
        return jsonify({"success": False, "message": "此拍賣未設定可直購"}), 400

    buyer = get_user(username)
    seller = get_user(a["seller"])
    if not buyer or not seller:
        return jsonify({"success": False, "message": "買家或賣家不存在"}), 404

    if buyer["gold"] < price:
        log_action(username, "violation:auction_buy_gold", {"need": price, "gold": buyer["gold"]})
        return jsonify({"success": False, "message": "金幣不足"}), 400

    # 金幣轉移
    buyer_gold = buyer["gold"] - price
    seller_gold = seller["gold"] + price
    set_user_field(username, "gold", buyer_gold)
    set_user_field(a["seller"], "gold", seller_gold)

    # 物品給買家
    change_inventory(username, a["item_id"], a["qty"])

    # 更新拍賣狀態
    a["status"] = "sold"
    a["current_price"] = price
    a["current_bidder"] = username
    save_auction(auction_id, a)
    r.zrem("auction:open", str(auction_id))

    log_sold(auction_id, a["seller"], username, a["item_id"], a["qty"], price)
    log_action(username, "auction_buy_now", {"auction_id": auction_id, "price": price})

    return jsonify(
        {
            "success": True,
            "buyer_gold_after": buyer_gold,
            "seller_gold_after": seller_gold,
        }
    )


@app.route("/auction/cancel/<int:auction_id>", methods=["POST"])
@app.route("/api/auction/cancel/<int:auction_id>", methods=["POST"])
def api_auction_cancel(auction_id):
    auth_user = auth_from_request()
    username = request.headers.get("X-Username") or auth_user

    if not auth_user or not username or auth_user != username:
        log_action(auth_user or "", "violation:auction_cancel_unauthorized", {"target": username})
        return jsonify({"success": False, "message": "未授權"}), 401

    a = load_auction(auction_id)
    if not a or a.get("status") != "open":
        return jsonify({"success": False, "message": "拍賣不存在或已結束"}), 400

    if a["seller"] != username:
        return jsonify({"success": False, "message": "只有賣家可以取消"}), 403

    if a.get("current_bidder"):
        return jsonify({"success": False, "message": "已有出價，無法取消"}), 400

    # 退回物品
    change_inventory(username, a["item_id"], a["qty"])

    # 刪除拍賣
    r.delete(get_auction_key(auction_id))
    r.zrem("auction:open", str(auction_id))

    log_action(username, "auction_cancel", {"auction_id": auction_id})

    return jsonify({"success": True, "message": "拍賣已取消，物品已退回背包"})


# ==============================
# Admin APIs
# ==============================

def admin_from_request():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    key = f"admin:token:{token}"
    if r.get(key) == "1":
        return token
    return None


@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.json or {}
    pwd = data.get("password") or ""
    if pwd != ADMIN_PASSWORD:
        return jsonify({"success": False, "detail": "後台密碼錯誤"}), 401

    token = secrets.token_hex(32)
    r.setex(f"admin:token:{token}", ADMIN_TOKEN_TTL, "1")
    return jsonify({"success": True, "admin_token": token})


@app.route("/admin/players")
def admin_players():
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    players = []
    for key in r.scan_iter("user:*"):
        username = key.split(":", 1)[1]
        u = get_user(username)
        if not u:
            continue
        online = r.exists(f"session:{username}")
        players.append(
            {
                "username": username,
                "gold": u["gold"],
                "level": u["level"],
                "exp": u["exp"],
                "online": bool(online),
                "created_at": u.get("created_at", ""),
                "last_login_at": u.get("last_login_at", ""),
            }
        )
    players.sort(key=lambda x: x["username"])
    return jsonify({"success": True, "players": players})


@app.route("/admin/online")
def admin_online():
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    online = []
    for key in r.scan_iter("session:*"):
        username = key.split(":", 1)[1]
        online.append(username)
    return jsonify({"success": True, "online_players": online})


@app.route("/admin/gold/<username>", methods=["POST"])
def admin_gold(username):
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    amount = int(request.args.get("amount", 0))
    u = get_user(username)
    if not u:
        return jsonify({"success": False, "detail": "玩家不存在"}), 404

    new_gold = max(0, u["gold"] + amount)
    set_user_field(username, "gold", new_gold)
    log_action("admin", "admin_adjust_gold", {"target": username, "amount": amount, "new_gold": new_gold})
    return jsonify({"success": True, "gold_after": new_gold})


@app.route("/admin/inventory/<username>/<item_id>", methods=["POST"])
def admin_inventory(username, item_id):
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    qty = int(request.args.get("qty", 0))
    new_qty = change_inventory(username, item_id, qty)
    log_action("admin", "admin_adjust_inventory", {"target": username, "item_id": item_id, "qty": qty, "new_qty": new_qty})
    return jsonify({"success": True, "new_qty": new_qty})


@app.route("/admin/ban/<username>", methods=["POST"])
def admin_ban(username):
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    set_user_field(username, "banned", 1)
    destroy_session(username, None)
    log_action("admin", "admin_ban", {"target": username})
    return jsonify({"success": True, "message": "已封禁"})


@app.route("/admin/unban/<username>", methods=["POST"])
def admin_unban(username):
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    set_user_field(username, "banned", 0)
    log_action("admin", "admin_unban", {"target": username})
    return jsonify({"success": True, "message": "已解除封禁"})


@app.route("/admin/auctions")
def admin_auctions():
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    auctions = []
    for key in r.scan_iter("auction:*"):
        if key == "auction:next_id":
            continue
        aid = int(key.split(":", 1)[1])
        a = load_auction(aid)
        if a:
            auctions.append(a)
    auctions.sort(key=lambda x: x["auction_id"])
    return jsonify({"success": True, "auctions": auctions})


@app.route("/admin/auction/<int:auction_id>", methods=["DELETE"])
def admin_delete_auction(auction_id):
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    a = load_auction(auction_id)
    if not a:
        return jsonify({"success": False, "detail": "拍賣不存在"}), 404

    # 退回物品給賣家
    change_inventory(a["seller"], a["item_id"], a["qty"])

    # 刪除
    r.delete(get_auction_key(auction_id))
    r.zrem("auction:open", str(auction_id))

    log_action("admin", "admin_delete_auction", {"auction_id": auction_id, "seller": a["seller"]})
    return jsonify({"success": True, "message": "拍賣已刪除，物品已退回賣家"})


@app.route("/admin/logs")
def admin_logs():
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    limit = int(request.args.get("limit", 100))
    try:
        records = r.xrevrange(ACTIONS_STREAM, count=limit)
    except redis.exceptions.ResponseError:
        records = []

    actions = []
    for sid, fields in records:
        actions.append(fields)
    return jsonify({"success": True, "actions": actions})


@app.route("/admin/bids")
def admin_bids():
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    limit = int(request.args.get("limit", 100))
    try:
        records = r.xrevrange(BIDS_STREAM, count=limit)
    except redis.exceptions.ResponseError:
        records = []

    bids = []
    for sid, fields in records:
        bids.append(fields)
    return jsonify({"success": True, "bids": bids})


@app.route("/admin/sold")
def admin_sold():
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    limit = int(request.args.get("limit", 100))
    try:
        records = r.xrevrange(SOLD_STREAM, count=limit)
    except redis.exceptions.ResponseError:
        records = []

    sold = []
    for sid, fields in records:
        sold.append(fields)
    return jsonify({"success": True, "sold": sold})


# 公告：用 List 儲存
ANNOUNCE_KEY = "announcements"


@app.route("/admin/announce", methods=["POST"])
def admin_add_announce():
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    msg = request.args.get("msg") or ""
    msg = msg.strip()
    if not msg:
        return jsonify({"success": False, "message": "公告內容不得為空"}), 400

    r.rpush(ANNOUNCE_KEY, msg)
    log_action("admin", "admin_add_announce", {"msg": msg})
    return jsonify({"success": True, "message": "公告已新增"})


@app.route("/admin/announce", methods=["GET"])
def admin_get_announce():
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    items = r.lrange(ANNOUNCE_KEY, 0, -1)
    return jsonify({"success": True, "announcements": items})


@app.route("/admin/announce/<int:index>", methods=["DELETE"])
def admin_delete_announce(index):
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    items = r.lrange(ANNOUNCE_KEY, 0, -1)
    if index < 0 or index >= len(items):
        return jsonify({"success": False, "detail": "索引超出範圍"}), 400

    # Redis 沒有直接按 index 刪除，只好重組
    target = items[index]
    r.lset(ANNOUNCE_KEY, index, "__DEL__")
    r.lrem(ANNOUNCE_KEY, 1, "__DEL__")

    log_action("admin", "admin_delete_announce", {"index": index, "msg": target})
    return jsonify({"success": True, "message": "公告已刪除"})


@app.route("/admin/announce", methods=["DELETE"])
def admin_clear_announce():
    if not admin_from_request():
        return jsonify({"success": False, "detail": "未授權"}), 401

    r.delete(ANNOUNCE_KEY)
    log_action("admin", "admin_clear_announce", None)
    return jsonify({"success": True, "message": "公告已全部清除"})


# ==============================
# Main
# ==============================

if __name__ == "__main__":
    print("==== GAME SERVER START ====")
    print(f"[Redis] host={REDIS_HOST}, port={REDIS_PORT}, user={REDIS_USERNAME}")
    try:
        init_game_data()
    except Exception as e:
        print("init_game_data error (server still starts):", e)

    port = int(os.environ.get("PORT", 5000))
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        allow_unsafe_werkzeug=True,
    )
