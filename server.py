import os
import time
import json
import uuid
import random
import hashlib
import secrets
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO
import redis
from redis import ConnectionPool

# ============================================================
#                 基礎設定（Redis & Flask）
# ============================================================

REDIS_HOST = os.environ.get("REDIS_HOST")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_USERNAME = os.environ.get("REDIS_USERNAME", "default")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ROOT_PASSWORD = os.environ.get("ROOT_PASSWORD", "root123")  # Super Admin

FAILED_LIMIT = 5              # 連續密碼錯誤上限
FAILED_TTL = 3600            # 錯誤次數記錄有效期（秒）
SESSION_TTL = 86400          # 玩家 Session 有效期

ACTIONS_STREAM = "stream:actions"
BATTLE_STREAM = "stream:battles"

pool = ConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    username=REDIS_USERNAME,
    password=REDIS_PASSWORD,
    decode_responses=True,
    max_connections=50,
)
r = redis.Redis(connection_pool=pool)

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

def now_iso():
    return datetime.utcnow().isoformat()

def sha256(s: str):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

# ============================================================
#                        行為紀錄
# ============================================================

def log_action(user: str, action: str, detail: dict = None):
    """寫入 Stream 行為紀錄"""
    try:
        payload = {
            "ts": now_iso(),
            "user": user or "",
            "action": action,
            "detail": json.dumps(detail, ensure_ascii=False) if detail else ""
        }
        r.xadd(ACTIONS_STREAM, payload, maxlen=20000, approximate=True)
    except Exception as e:
        print("[log_action error]", e)

# ============================================================
#                     使用者 / 註冊 / 密碼
# ============================================================

def user_key(username: str):
    return f"user:{username}"

def session_key(username: str):
    return f"session:{username}"

def session_meta_key(username: str):
    return f"session_meta:{username}"

def failed_key(username: str):
    return f"login_failed:{username}"

def lock_key(username: str):
    return f"lock:{username}"

def get_user(username: str):
    if not r.exists(user_key(username)):
        return None
    data = r.hgetall(user_key(username))
    data["gold"] = int(data.get("gold", 0))
    data["level"] = int(data.get("level", 1))
    data["exp"] = int(data.get("exp", 0))
    data["banned"] = data.get("banned", "0")
    return data

def register_user(username: str, pw: str):
    key = user_key(username)
    if r.exists(key):
        return False, "帳號已存在"

    now = now_iso()
    r.hset(key, mapping={
        "username": username,
        "password_hash": sha256(pw),
        "gold": 100,
        "level": 1,
        "exp": 0,
        "created_at": now,
        "last_login_at": now,
        "banned": "0",
    })

    log_action(username, "register", {"username": username})
    return True, None

# ============================================================
#                   密碼錯誤次數 & 帳號鎖定
# ============================================================

def increase_failed(username: str):
    """增加錯誤次數"""
    k = failed_key(username)
    count = r.incr(k)
    r.expire(k, FAILED_TTL)
    return count

def clear_failed(username: str):
    r.delete(failed_key(username))

def is_locked(username: str):
    """檢查是否被鎖"""
    return r.exists(lock_key(username))

def lock_account(username: str):
    r.set(lock_key(username), "1")
    log_action(username, "account_locked", {"reason": "too many failed attempts"})

def unlock_account(username: str):
    r.delete(lock_key(username))
    clear_failed(username)
    log_action(username, "account_unlocked_by_root", None)

# ============================================================
#                       Session 系統
# ============================================================

def create_session(username: str, device: str, ip: str):
    """只允許一個 Session。後登入會被拒絕。"""
    sess = session_key(username)

    if r.exists(sess):
        # 已登入
        meta = r.hgetall(session_meta_key(username)) or {}
        return False, meta

    token = secrets.token_hex(32)
    now = now_iso()
    pipe = r.pipeline()

    pipe.set(sess, token)
    pipe.expire(sess, SESSION_TTL)

    pipe.hset(session_meta_key(username), mapping={
        "device": device,
        "ip": ip,
        "login_time": now
    })
    pipe.expire(session_meta_key(username), SESSION_TTL)

    pipe.set(f"token:{token}", username)
    pipe.expire(f"token:{token}", SESSION_TTL)

    pipe.execute()

    log_action(username, "login", {"device": device, "ip": ip})

    return True, {"token": token}

def destroy_session(username: str):
    sess = session_key(username)
    token = r.get(sess)
    r.delete(sess)
    r.delete(session_meta_key(username))
    if token:
        r.delete(f"token:{token}")
    log_action(username, "logout", None)

def get_username_from_request():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.split(" ", 1)[1]
    return r.get(f"token:{token}")

# ============================================================
#                     Root / Admin 登入
# ============================================================

def admin_token_key(token: str):
    return f"admin_token:{token}"

def root_token_key(token: str):
    return f"root_token:{token}"

def create_admin_token(is_root=False):
    token = secrets.token_hex(32)
    key = root_token_key(token) if is_root else admin_token_key(token)
    r.setex(key, 3600, "1")
    return token

def check_admin():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, False
    token = auth.split(" ", 1)[1]
    if r.get(admin_token_key(token)) == "1":
        return token, False
    if r.get(root_token_key(token)) == "1":
        return token, True
    return None, False

# ============================================================
#                      API：註冊 / 登入
# ============================================================

@app.route("/auth/register", methods=["POST"])
def api_register():
    data = request.json or {}
    username = data.get("username", "").strip()
    pw = data.get("password", "")
    pw2 = data.get("confirm_password", "")

    if not username or not pw:
        return {"success": False, "message": "請輸入帳號密碼"}, 400

    if pw != pw2:
        return {"success": False, "message": "兩次密碼不一致"}, 400

    ok, msg = register_user(username, pw)
    if not ok:
        return {"success": False, "message": msg}, 400

    return {"success": True, "message": "註冊成功"}

@app.route("/auth/login", methods=["POST"])
def api_login():
    data = request.json or {}
    username = data.get("username", "").strip()
    pw = data.get("password", "")
    device = data.get("device", "unknown")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)

    # 鎖定檢查
    if is_locked(username):
        return {"success": False, "locked": True, "message": "帳號已被鎖定，請聯絡管理員"}, 403

    user = get_user(username)
    if not user:
        return {"success": False, "message": "帳號不存在"}, 404

    if sha256(pw) != user["password_hash"]:
        failed = increase_failed(username)
        if failed >= FAILED_LIMIT:
            lock_account(username)
            return {"success": False, "locked": True, "message": "帳號已被鎖定"}, 403
        return {"success": False, "message": f"密碼錯誤（{failed}/{FAILED_LIMIT}）"}, 401

    # 密碼正確 → 清除錯誤紀錄
    clear_failed(username)

    # 單裝置登入
    ok, info = create_session(username, device, ip)
    if not ok:
        return {
            "success": False,
            "code": "ALREADY_LOGGED_IN",
            "info": info,
            "message": "帳號已在其他裝置登入"
        }, 409

    return {
        "success": True,
        "username": username,
        "token": info["token"]
    }

# ============================================================
#                Root / Admin API（登入階段）
# ============================================================

@app.route("/admin/login", methods=["POST"])
def api_admin_login():
    data = request.json or {}
    pw = data.get("password", "")

    if pw == ROOT_PASSWORD:
        token = create_admin_token(is_root=True)
        return {"success": True, "root": True, "token": token}

    if pw == ADMIN_PASSWORD:
        token = create_admin_token(is_root=False)
        return {"success": True, "root": False, "token": token}

    return {"success": False, "message": "管理密碼錯誤"}, 401

@app.route("/admin/unlock/<username>", methods=["POST"])
def api_root_unlock(username):
    token, is_root = check_admin()
    if not token or not is_root:
        return {"success": False, "message": "需要 root 權限"}, 403

    unlock_account(username)
    return {"success": True, "message": f"{username} 已解鎖"}

# ============================================================
# Part 1 完成
# ============================================================

@app.route("/part1_status")
def api_status():
    return {"success": True, "message": "Part1 loaded"}
# ============================================================
#                   Part 2：裝備系統（核心）
# ============================================================

EQUIP_SLOTS = ["weapon", "head", "body", "hands", "feet"]

RARITY_LIST = ["gray", "white", "green", "blue", "purple", "gold"]
RARITY_MULTIPLIER = {
    "gray": 0.8,
    "white": 1.0,
    "green": 1.2,
    "blue": 1.5,
    "purple": 2.0,
    "gold": 3.0,
}

# 建立裝備的隨機基礎屬性範圍（你選的 10~150）
ATTR_MIN = 10
ATTR_MAX = 150

def equip_key(username, uid):
    return f"equip:{username}:{uid}"

def equip_slot_key(username):
    return f"equip_slot:{username}"

def guard_key(username):
    """強化保護券"""
    return f"equip_guard:{username}"

# ------------------------------
# 生成一件裝備（給掉落 / 任務 / root）
# ------------------------------
def generate_equipment(username: str, equip_type: str):
    """產生一件裝備（隨機屬性 + 稀有度）"""
    rarity = random.choice(RARITY_LIST)

    multiplier = RARITY_MULTIPLIER[rarity]

    def roll():
        return int(random.randint(ATTR_MIN, ATTR_MAX) * multiplier)

    eq = {
        "equip_type": equip_type,
        "rarity": rarity,
        "enhance": 0,
        "atk": roll(),
        "def": roll(),
        "hp": roll(),
        "spd": roll(),
        "crit": roll(),
        "crit_dmg": roll(),
        "created_at": now_iso(),
    }

    uid = str(uuid.uuid4())
    r.hset(equip_key(username, uid), mapping=eq)

    log_action(username, "equip_generated", eq)

    return uid, eq

# ------------------------------
# 取得裝備資料
# ------------------------------
def get_equipment(username, uid):
    key = equip_key(username, uid)
    if not r.exists(key):
        return None
    eq = r.hgetall(key)
    # 數值轉型
    for k in ["enhance", "atk", "def", "hp", "spd", "crit", "crit_dmg"]:
        eq[k] = int(eq[k])
    return eq

# ------------------------------
# 刪除裝備（爆炸、root 操作）
# ------------------------------
def delete_equipment(username, uid):
    r.delete(equip_key(username, uid))
    log_action(username, "equip_deleted", {"uid": uid})

# ============================================================
#                   穿裝 / 脫裝
# ============================================================

@app.route("/equip/wear", methods=["POST"])
def api_equip_wear():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}
    uid = data.get("uid")
    slot = data.get("slot")

    if slot not in EQUIP_SLOTS:
        return {"success": False, "message": "非法裝備欄位"}, 400

    eq = get_equipment(username, uid)
    if not eq:
        return {"success": False, "message": "裝備不存在"}, 404

    # 設定穿戴
    r.hset(equip_slot_key(username), slot, uid)

    log_action(username, "equip_wear", {"slot": slot, "equip_uid": uid})

    return {"success": True, "message": "已穿戴裝備"}

@app.route("/equip/unwear", methods=["POST"])
def api_equip_unwear():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}
    slot = data.get("slot")

    if slot not in EQUIP_SLOTS:
        return {"success": False, "message": "非法欄位"}, 400

    r.hdel(equip_slot_key(username), slot)

    log_action(username, "equip_unwear", {"slot": slot})

    return {"success": True, "message": "已卸下裝備"}

# ============================================================
#                   強化系統（+0 ~ +20）
# ============================================================

# 強化成功率
ENHANCE_TABLE = {
    0: 0.80, 1: 0.80, 2: 0.80, 3: 0.80, 4: 0.80, 5: 0.80,
    6: 0.60, 7: 0.60, 8: 0.60, 9: 0.60, 10: 0.60,
    11: 0.40, 12: 0.40, 13: 0.40, 14: 0.40, 15: 0.40,
    16: 0.25, 17: 0.25, 18: 0.25,
    19: 0.10, 20: 0.10,
}

# 失敗掉階規則
def enhance_fail_result(level):
    if level <= 5:
        return ("no_drop", 0)
    elif level <= 10:
        return ("drop", -1)
    elif level <= 15:
        return ("drop", -2)
    elif level <= 18:
        return ("reset", 0)    # 回 +0
    else:
        return ("explode", 0)

@app.route("/equip/enhance", methods=["POST"])
def api_equip_enhance():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}
    uid = data.get("uid")
    use_guard = data.get("use_guard", False)

    eq = get_equipment(username, uid)
    if not eq:
        return {"success": False, "message": "裝備不存在"}, 404

    lv = eq["enhance"]
    if lv >= 20:
        return {"success": False, "message": "已達上限"}, 400

    # 成功率
    success_rate = ENHANCE_TABLE.get(lv, 0.1)
    success = (random.random() < success_rate)

    # 保護券可用範圍：16〜20
    has_guard = r.get(guard_key(username))
    guard_active = False

    if use_guard and has_guard and lv >= 16:
        guard_active = True
        # consume guard
        r.decr(guard_key(username))

    # -------------------------
    # 成功
    # -------------------------
    if success:
        new_lv = lv + 1

        # 成長係數（1.05 ~ 1.12）
        growth = random.uniform(1.05, 1.12)

        new_attr = {
            "enhance": new_lv,
            "atk": int(eq["atk"] * growth),
            "def": int(eq["def"] * growth),
            "hp": int(eq["hp"] * growth),
            "spd": int(eq["spd"] * growth),
            "crit": int(eq["crit"] * growth),
            "crit_dmg": int(eq["crit_dmg"] * growth),
        }

        r.hset(equip_key(username, uid), mapping=new_attr)

        log_action(username, "enhance_success", {"uid": uid, "new_level": new_lv})
        return {"success": True, "enhance": new_lv, "message": "強化成功"}

    # -------------------------
    # 失敗
    # -------------------------
    fail_type, fail_value = enhance_fail_result(lv)

    # 保護券生效
    if guard_active and lv >= 16:
        if fail_type == "explode" or fail_type == "reset":
            fail_type = "drop"
            fail_value = -1

    # 依失敗結果處理
    if fail_type == "no_drop":
        new_lv = lv
    elif fail_type == "drop":
        new_lv = max(0, lv + fail_value)
        r.hset(equip_key(username, uid), "enhance", new_lv)
    elif fail_type == "reset":
        new_lv = 0
        r.hset(equip_key(username, uid), "enhance", 0)
    elif fail_type == "explode":
        delete_equipment(username, uid)
        return {"success": False, "explode": True, "message": "裝備爆炸消失！"}

    log_action(username, "enhance_fail", {
        "uid": uid,
        "old_level": lv,
        "new_level": new_lv,
        "fail_type": fail_type
    })

    return {"success": False, "enhance": new_lv, "message": "強化失敗"}

# ============================================================
#                 Stronghold: Root 給保護券
# ============================================================

@app.route("/root/give_guard/<target>", methods=["POST"])
def api_root_give_guard(target):
    token, is_root = check_admin()
    if not token or not is_root:
        return {"success": False, "message": "需要 root"}, 403

    n = int(request.args.get("n", 1))
    r.incrby(guard_key(target), n)

    log_action("root", "give_guard", {"target": target, "count": n})

    return {"success": True, "message": f"已給 {target} 強化保護券 x{n}"}
# ============================================================
#            Part 3：等級系統 + 屬性成長 + 戰力系統
# ============================================================

ROLE_KEY = "role:{username}"

def role_key(username):
    return f"role:{username}"

# ------------------------------------------------------------
# 初始化角色（在註冊後可被呼叫）
# ------------------------------------------------------------
def init_role(username):
    base_stats = {
        "atk": 10,
        "def": 10,
        "hp": 100,
        "spd": 10,
        "crit": 5,
        "crit_dmg": 50,
    }
    r.hset(role_key(username), mapping=base_stats)
    log_action(username, "init_role", base_stats)

# ------------------------------------------------------------
# 升級 EXP 需求
# ------------------------------------------------------------
def level_exp_required(level):
    """
    EXP 需求採用遞增成長：
    L1→L2: 100
    L2→L3: 300
    L3→L4: 600
    L4→L5: 1000
    之後使用 (level^2)*100 做平滑曲線
    """
    if level == 1:
        return 100
    elif level == 2:
        return 300
    elif level == 3:
        return 600
    elif level == 4:
        return 1000
    else:
        return level * level * 100

# ------------------------------------------------------------
# 加 EXP 並自動升級
# ------------------------------------------------------------
def add_exp(username, exp_gain):
    user = get_user(username)
    if not user:
        return None

    old_exp = user["exp"]
    new_exp = old_exp + exp_gain
    level = user["level"]

    # 升級迴圈
    leveled_up = False
    while True:
        required = level_exp_required(level)
        if new_exp >= required:
            new_exp -= required
            level += 1
            leveled_up = True

            # 屬性成長
            grow_stats = {
                "atk": random.randint(2, 5),
                "def": random.randint(2, 5),
                "hp": random.randint(10, 30),
                "spd": random.randint(1, 3),
            }
            # 加到角色 base stats
            r.hincrby(role_key(username), "atk", grow_stats["atk"])
            r.hincrby(role_key(username), "def", grow_stats["def"])
            r.hincrby(role_key(username), "hp", grow_stats["hp"])
            r.hincrby(role_key(username), "spd", grow_stats["spd"])

            log_action(username, "level_up", {
                "new_level": level,
                "growth": grow_stats
            })
        else:
            break

    # 儲存 EXP 和 等級
    set_user_field(username, "exp", new_exp)
    set_user_field(username, "level", level)

    return {
        "old_exp": old_exp,
        "new_exp": new_exp,
        "level": level,
        "leveled_up": leveled_up
    }

# ------------------------------------------------------------
# 合併所有來源屬性 → 計算角色最終屬性
# ------------------------------------------------------------
def compute_final_stats(username):
    """角色最終屬性 = 基礎屬性 + 裝備屬性加總"""
    # 1. Base Stats
    base = r.hgetall(role_key(username))
    if not base:
        init_role(username)
        base = r.hgetall(role_key(username))

    for k in base:
        base[k] = int(base[k])

    total = base.copy()

    # 2. 裝備屬性加總
    slots = r.hgetall(equip_slot_key(username))
    for slot, uid in slots.items():
        eq = get_equipment(username, uid)
        if not eq:
            continue

        for attr in ["atk", "def", "hp", "spd", "crit", "crit_dmg"]:
            total[attr] += eq[attr]

    return total

# ------------------------------------------------------------
# 戰力計算 (你給的公式)
# ------------------------------------------------------------
def compute_power(stats):
    power = (
        stats["atk"] * 2 +
        stats["def"] * 1.5 +
        stats["hp"] * 1 +
        stats["spd"] * 1 +
        stats["crit"] * 10 +
        stats["crit_dmg"] * 5
    )
    return int(power)

# ------------------------------------------------------------
# API：取得玩家最終屬性 + 戰力
# ------------------------------------------------------------
@app.route("/player/stats")
def api_player_stats():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    stats = compute_final_stats(username)
    power = compute_power(stats)

    return {
        "success": True,
        "stats": stats,
        "power": power
    }

# ------------------------------------------------------------
# API：獲得 EXP（戰鬥 / 任務 / 活動 使用）
# ------------------------------------------------------------
@app.route("/player/exp", methods=["POST"])
def api_player_add_exp():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}
    exp_gain = int(data.get("exp", 0))

    result = add_exp(username, exp_gain)

    return {
        "success": True,
        "result": result
    }

# ============================================================
# Part 3 完成
# ============================================================
# ============================================================
#              Part 4：好友系統（Friend System）
# ============================================================

def friends_key(username):
    return f"friends:{username}"

def friend_requests_key(username):
    return f"friend_requests:{username}"

# ------------------------------------------------------------
# API：送出好友申請
# ------------------------------------------------------------
@app.route("/friend/request", methods=["POST"])
def api_friend_request():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}
    target = data.get("target", "").strip()

    if not target:
        return {"success": False, "message": "無效目標"}, 400
    if target == username:
        return {"success": False, "message": "不能加自己為好友"}, 400
    if not get_user(target):
        return {"success": False, "message": "玩家不存在"}, 404

    # 是否已經是好友
    if r.sismember(friends_key(username), target):
        return {"success": False, "message": "已經是好友"}, 400

    # 對方已申請過 → 自動成為好友
    if r.sismember(friend_requests_key(username), target):
        r.sadd(friends_key(username), target)
        r.sadd(friends_key(target), username)

        r.srem(friend_requests_key(username), target)
        log_action(username, "friend_auto_accept", {"target": target})
        return {"success": True, "auto_accept": True, "message": "已成為好友"}

    # 正常申請流程
    r.sadd(friend_requests_key(target), username)
    log_action(username, "friend_request", {"target": target})
    return {"success": True, "message": "好友申請已送出"}

# ------------------------------------------------------------
# API：取得收到的好友申請列表
# ------------------------------------------------------------
@app.route("/friend/requests")
def api_friend_requests():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    reqs = list(r.smembers(friend_requests_key(username)))
    return {"success": True, "requests": reqs}

# ------------------------------------------------------------
# API：接受好友
# ------------------------------------------------------------
@app.route("/friend/accept", methods=["POST"])
def api_friend_accept():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}
    target = data.get("target", "")

    if not r.sismember(friend_requests_key(username), target):
        return {"success": False, "message": "對方沒有申請"}, 400

    # 雙方成為好友
    r.sadd(friends_key(username), target)
    r.sadd(friends_key(target), username)

    # 移除申請
    r.srem(friend_requests_key(username), target)

    log_action(username, "friend_accept", {"target": target})
    return {"success": True, "message": "已成為好友"}

# ------------------------------------------------------------
# API：拒絕好友申請
# ------------------------------------------------------------
@app.route("/friend/reject", methods=["POST"])
def api_friend_reject():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}
    target = data.get("target", "")

    if not r.sismember(friend_requests_key(username), target):
        return {"success": False, "message": "對方沒有申請"}, 400

    r.srem(friend_requests_key(username), target)

    log_action(username, "friend_reject", {"target": target})
    return {"success": True, "message": "已拒絕"}

# ------------------------------------------------------------
# API：刪除好友
# ------------------------------------------------------------
@app.route("/friend/remove", methods=["POST"])
def api_friend_remove():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}
    target = data.get("target", "")

    if not r.sismember(friends_key(username), target):
        return {"success": False, "message": "不是好友"}, 400

    r.srem(friends_key(username), target)
    r.srem(friends_key(target), username)

    log_action(username, "friend_remove", {"target": target})
    return {"success": True, "message": "好友已刪除"}

# ------------------------------------------------------------
# API：好友列表 + 在線狀態 + 戰力
# ------------------------------------------------------------
@app.route("/friend/list")
def api_friend_list():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    friend_list = list(r.smembers(friends_key(username)))
    result = []

    for f in friend_list:
        stats = compute_final_stats(f)
        power = compute_power(stats)
        online = r.exists(session_key(f))

        result.append({
            "username": f,
            "power": power,
            "online": bool(online)
        })

    result.sort(key=lambda x: x["power"], reverse=True)

    return {"success": True, "friends": result}

# ------------------------------------------------------------
# API：好友搜尋（玩家查詢用）
# ------------------------------------------------------------
@app.route("/friend/search")
def api_friend_search():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    q = request.args.get("q", "").strip()
    if not q:
        return {"success": False, "message": "查詢字串為空"}, 400

    matched = []
    for key in r.scan_iter("user:*"):
        uname = key.split(":", 1)[1]
        if q.lower() in uname.lower():
            matched.append(uname)

    return {"success": True, "result": matched[:20]}

# ------------------------------------------------------------
# API：好友對戰入口（戰鬥 Part 5 會用）
# ------------------------------------------------------------
@app.route("/friend/battle", methods=["POST"])
def api_friend_battle():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}
    target = data.get("target", "")

    if not r.sismember(friends_key(username), target):
        return {"success": False, "message": "不是好友，無法對戰"}, 400

    # 實際戰鬥在 Part 5
    return {"success": True, "message": "可進行好友對戰", "battle_ready": True}
# ============================================================
#          Part 5：自動戰鬥系統 + ELO + 掉落 + 戰鬥紀錄
# ============================================================

def elo_key(username):
    return f"elo:{username}"

def get_elo(username):
    val = r.get(elo_key(username))
    if not val:
        r.set(elo_key(username), 1000)
        return 1000
    return int(val)

def set_elo(username, val):
    r.set(elo_key(username), val)

# ------------------------------------------------------------
# 裝備掉落（依稀有度機率）
# ------------------------------------------------------------

DROP_TABLE = [
    ("gray", 0.40),
    ("white", 0.30),
    ("green", 0.15),
    ("blue", 0.10),
    ("purple", 0.04),
    ("gold", 0.01),
]

def random_drop_rarity():
    roll = random.random()
    acc = 0
    for rarity, rate in DROP_TABLE:
        acc += rate
        if roll <= acc:
            return rarity
    return "gray"

def battle_drop(username):
    """打贏有機率掉裝備"""
    rarity = random_drop_rarity()
    equip_type = random.choice(EQUIP_SLOTS)
    uid, eq = generate_equipment(username, equip_type)
    return uid, eq

# ------------------------------------------------------------
# 產生一場戰鬥（核心）
# ------------------------------------------------------------
def fight_auto(attacker: str, defender: str):
    stats_a = compute_final_stats(attacker)
    stats_d = compute_final_stats(defender)

    hp_a = stats_a["hp"]
    hp_d = stats_d["hp"]

    # SPD 判先後
    turn = "A" if stats_a["spd"] >= stats_d["spd"] else "D"
    log = []

    round_num = 0

    while hp_a > 0 and hp_d > 0:
        round_num += 1

        if turn == "A":
            crit = (random.random() < stats_a["crit"] / 100)
            dmg = max(1, int(stats_a["atk"] * 1.2 - stats_d["def"] * 0.6))
            if crit:
                dmg = int(dmg * 2)

            hp_d -= dmg

            log.append({
                "round": round_num,
                "attacker": attacker,
                "defender": defender,
                "damage": dmg,
                "crit": crit,
                "defender_hp": max(0, hp_d)
            })

            turn = "D"

        else:
            crit = (random.random() < stats_d["crit"] / 100)
            dmg = max(1, int(stats_d["atk"] * 1.2 - stats_a["def"] * 0.6))
            if crit:
                dmg = int(dmg * 2)

            hp_a -= dmg

            log.append({
                "round": round_num,
                "attacker": defender,
                "defender": attacker,
                "damage": dmg,
                "crit": crit,
                "defender_hp": max(0, hp_a)
            })

            turn = "A"

        if round_num >= 50:
            # 避免無窮迴圈（超肉）
            break

    # 判定勝負
    if hp_a > 0 and hp_d <= 0:
        winner = attacker
        loser = defender
    elif hp_d > 0 and hp_a <= 0:
        winner = defender
        loser = attacker
    else:
        # 平手 → 戰力較高者勝
        pow_a = compute_power(stats_a)
        pow_d = compute_power(stats_d)
        if pow_a >= pow_d:
            winner = attacker
            loser = defender
        else:
            winner = defender
            loser = attacker

    # 回傳完整結果
    return {
        "winner": winner,
        "loser": loser,
        "log": log,
        "stats_a": stats_a,
        "stats_d": stats_d,
    }

# ------------------------------------------------------------
# ELO 計算
# ------------------------------------------------------------
def calc_elo(winner, loser):
    E_w = get_elo(winner)
    E_l = get_elo(loser)

    # 預期勝率
    def expected(a, b):
        return 1 / (1 + 10 ** ((b - a) / 400))

    exp_w = expected(E_w, E_l)
    exp_l = expected(E_l, E_w)

    K = 32

    new_Ew = E_w + K * (1 - exp_w)
    new_El = E_l + K * (0 - exp_l)

    set_elo(winner, int(new_Ew))
    set_elo(loser, int(new_El))

    return new_Ew, new_El

# ------------------------------------------------------------
# API：玩家對戰（自動）
# ------------------------------------------------------------
@app.route("/battle/pvp", methods=["POST"])
def api_battle_pvp():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}
    target = data.get("target", "")

    if not get_user(target):
        return {"success": False, "message": "對手不存在"}, 404

    result = fight_auto(username, target)

    # ELO & EXP & 掉落
    winner = result["winner"]
    loser = result["loser"]

    new_elo_w, new_elo_l = calc_elo(winner, loser)

    # EXP 獎勵（winner 才會得到）
    exp_reward = random.randint(20, 40)
    gold_reward = random.randint(20, 60)

    add_exp(winner, exp_reward)

    # 金幣
    u = get_user(winner)
    set_user_field(winner, "gold", u["gold"] + gold_reward)

    # 掉落裝備
    drop_uid = None
    drop_eq = None
    if random.random() < 0.30:  # 30% 機率掉裝備
        drop_uid, drop_eq = battle_drop(winner)

    # 記錄到 stream
    r.xadd(BATTLE_STREAM, {
        "ts": now_iso(),
        "winner": winner,
        "loser": loser,
        "log": json.dumps(result["log"], ensure_ascii=False),
        "drop_uid": drop_uid if drop_uid else "",
    }, maxlen=5000, approximate=True)

    log_action(username, "battle_pvp", {
        "target": target,
        "winner": winner,
        "exp": exp_reward,
        "gold": gold_reward,
        "drop": drop_uid,
    })

    return {
        "success": True,
        "winner": winner,
        "loser": loser,
        "log": result["log"],
        "elo": {
            "winner": new_elo_w,
            "loser": new_elo_l,
        },
        "reward": {
            "exp": exp_reward,
            "gold": gold_reward,
            "drop_uid": drop_uid,
            "drop_eq": drop_eq,
        }
    }

# ------------------------------------------------------------
# API：查看某玩家 ELO
# ------------------------------------------------------------
@app.route("/battle/elo/<username>")
def api_get_elo(username):
    if not get_user(username):
        return {"success": False, "message": "玩家不存在"}, 404
    return {"success": True, "elo": get_elo(username)}
# ============================================================
#             Part 6：排行榜系統（Power / ELO / Weekly）
# ============================================================

POWER_RANK = "rank:power"
ELO_RANK = "rank:elo"
WEEKLY_RANK = "rank:weekly"      # 當週
WEEKLY_PREFIX = "rank:weekly:"   # 歷史

# ------------------------------------------------------------
# 更新玩家戰力到排行榜（每次屬性變動時呼叫）
# ------------------------------------------------------------
def update_power_rank(username):
    stats = compute_final_stats(username)
    power = compute_power(stats)
    r.zadd(POWER_RANK, {username: power})

    # 每週排行也更新
    r.zadd(WEEKLY_RANK, {username: power})
    return power

# ------------------------------------------------------------
# 更新 ELO 排行榜
# ------------------------------------------------------------
def update_elo_rank(username):
    elo = get_elo(username)
    r.zadd(ELO_RANK, {username: elo})
    return elo

# ------------------------------------------------------------
# 切換週排行（root 介面執行或定時器）
# ------------------------------------------------------------
def rollover_weekly_rank():
    """將當週排行榜存為歷史，並清空當週排行榜"""
    week_num = datetime.utcnow().strftime("%Y%W")
    archive_key = f"{WEEKLY_PREFIX}{week_num}"

    # 複製
    ranks = r.zrevrange(WEEKLY_RANK, 0, -1, withscores=True)
    for user, score in ranks:
        r.zadd(archive_key, {user: score})

    # 清空當週
    r.delete(WEEKLY_RANK)

    log_action("system", "weekly_rank_rollover", {"archive": archive_key})
    return archive_key

# ------------------------------------------------------------
# API：查詢全服戰力排行
# ------------------------------------------------------------
@app.route("/rank/power")
def api_rank_power():
    top = r.zrevrange(POWER_RANK, 0, 99, withscores=True)
    result = [{"username": u, "power": int(s)} for u, s in top]
    return {"success": True, "rank": result}

# ------------------------------------------------------------
# API：查詢 ELO 排行
# ------------------------------------------------------------
@app.route("/rank/elo")
def api_rank_elo():
    top = r.zrevrange(ELO_RANK, 0, 99, withscores=True)
    result = [{"username": u, "elo": int(s)} for u, s in top]
    return {"success": True, "rank": result}

# ------------------------------------------------------------
# API：查詢當週排行
# ------------------------------------------------------------
@app.route("/rank/weekly")
def api_rank_weekly():
    top = r.zrevrange(WEEKLY_RANK, 0, 99, withscores=True)
    result = [{"username": u, "power": int(s)} for u, s in top]
    return {"success": True, "rank": result}

# ------------------------------------------------------------
# API：手動進行週排行輪替（需 root）
# ------------------------------------------------------------
@app.route("/rank/weekly/rollover", methods=["POST"])
def api_weekly_rollover():
    token, is_root = check_admin()
    if not token or not is_root:
        return {"success": False, "message": "需要 root 權限"}, 403

    archive_key = rollover_weekly_rank()
    return {"success": True, "archive": archive_key}

# ------------------------------------------------------------
# API：歷史週排行
# ------------------------------------------------------------
@app.route("/rank/weekly/<week>")
def api_weekly_history(week):
    key = f"{WEEKLY_PREFIX}{week}"
    if not r.exists(key):
        return {"success": False, "message": "該週排行不存在"}, 404

    top = r.zrevrange(key, 0, 99, withscores=True)
    result = [{"username": u, "power": int(s)} for u, s in top]
    return {"success": True, "rank": result}

# ------------------------------------------------------------
# API：好友戰力排行
# ------------------------------------------------------------
@app.route("/rank/friends")
def api_rank_friends():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    friends = list(r.smembers(friends_key(username)))
    result = []

    for f in friends:
        stats = compute_final_stats(f)
        power = compute_power(stats)
        online = r.exists(session_key(f))
        result.append({
            "username": f,
            "power": power,
            "online": bool(online)
        })

    result.sort(key=lambda x: x["power"], reverse=True)
    return {"success": True, "rank": result}
# ============================================================
#        Part 7：商店（Shop）＋拍賣行（Auction House）
# ============================================================

SHOP_ITEMS = {
    "potion_small": {
        "name": "小型治療藥水",
        "price": 20,
        "desc": "回復少量 HP",
        "type": "consumable"
    },
    "refine_stone": {
        "name": "強化石",
        "price": 50,
        "desc": "強化裝備所需材料",
        "type": "material"
    },
    "skill_shard": {
        "name": "技能碎片",
        "price": 30,
        "desc": "技能卡強化素材（C版使用）",
        "type": "material"
    }
}

def shop_item_key(username):
    return f"inventory:{username}"


# ============================================================
#                     商店購買系統
# ============================================================

@app.route("/shop/list")
def api_shop_list():
    return {"success": True, "items": SHOP_ITEMS}


@app.route("/shop/buy", methods=["POST"])
def api_shop_buy():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}
    item_id = data.get("item_id")
    qty = int(data.get("qty", 1))

    if item_id not in SHOP_ITEMS:
        return {"success": False, "message": "物品不存在"}, 404

    item = SHOP_ITEMS[item_id]
    cost = item["price"] * qty

    player = get_user(username)
    if player["gold"] < cost:
        return {"success": False, "message": "金幣不足"}, 400

    # 扣金幣
    set_user_field(username, "gold", player["gold"] - cost)

    # 增加物品數量
    r.hincrby(shop_item_key(username), item_id, qty)

    log_action(username, "shop_buy", {"item": item_id, "qty": qty})

    return {"success": True, "new_gold": player["gold"] - cost}


@app.route("/shop/inventory")
def api_shop_inventory():
    username = get_username_from_request()
    if not username:
        return {"success": False}, 401

    inv = r.hgetall(shop_item_key(username))
    result = {k: int(v) for k, v in inv.items()}
    return {"success": True, "inventory": result}


# ============================================================
#                     拍賣行系統 AH
# ============================================================

def auction_key(aid):
    return f"auction:{aid}"

def next_auction_id():
    return int(r.incr("auction:next_id"))

def list_open_auctions(limit=50):
    ids = r.zrevrange("auction:open", 0, limit - 1)
    result = []
    for sid in ids:
        aid = int(sid)
        a = r.hgetall(auction_key(aid))
        if a:
            # 轉型 numeric
            for k in ["start_price", "current_price", "buyout_price", "qty"]:
                if k in a and a[k] != "":
                    a[k] = int(a[k])
            result.append(a)
    return result


# ------------------------------------------------------------
# API：取得所有開放中的拍賣
# ------------------------------------------------------------
@app.route("/auction/list")
def api_auction_list():
    data = list_open_auctions()
    return {"success": True, "auctions": data}


# ------------------------------------------------------------
# API：上架（可支援裝備或普通物品）
# ------------------------------------------------------------
@app.route("/auction/create", methods=["POST"])
def api_auction_create():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}

    item_type = data.get("type")  # "item" or "equip"
    item_id = data.get("item_id")
    uid = data.get("uid")         # 裝備專用
    qty = int(data.get("qty", 1))

    start_price = int(data.get("start_price", 0))
    buyout_price = data.get("buyout_price")
    if buyout_price is not None:
        buyout_price = int(buyout_price)

    if start_price <= 0:
        return {"success": False, "message": "起標價必須大於 0"}, 400

    # -----------------------------
    # 處理一般物品
    # -----------------------------
    if item_type == "item":
        inv = r.hget(shop_item_key(username), item_id)
        if not inv or int(inv) < qty:
            return {"success": False, "message": "物品不足"}, 400

        # 扣除物品
        r.hincrby(shop_item_key(username), item_id, -qty)

    # -----------------------------
    # 處理裝備上架
    # -----------------------------
    elif item_type == "equip":
        eq = get_equipment(username, uid)
        if not eq:
            return {"success": False, "message": "裝備不存在"}, 404

        # 若裝備穿戴中，不可上架
        slots = r.hgetall(equip_slot_key(username))
        if uid in slots.values():
            return {"success": False, "message": "裝備穿戴中，不能上架"}, 400

        # 將裝備標記為 "locked"
        r.hset(equip_key(username, uid), "locked", "1")

    else:
        return {"success": False, "message": "未知物品類型"}, 400

    # -----------------------------
    # 創建拍賣
    # -----------------------------
    aid = next_auction_id()
    created_at = now_iso()

    auction = {
        "auction_id": aid,
        "seller": username,
        "type": item_type,
        "item_id": item_id or "",
        "uid": uid or "",
        "qty": qty,
        "start_price": start_price,
        "current_price": start_price,
        "current_bidder": "",
        "buyout_price": buyout_price if buyout_price else "",
        "status": "open",
        "created_at": created_at,
    }

    r.hset(auction_key(aid), mapping=auction)
    r.zadd("auction:open", {aid: time.time()})

    log_action(username, "auction_create", auction)

    return {"success": True, "auction_id": aid}


# ------------------------------------------------------------
# API：出價
# ------------------------------------------------------------
@app.route("/auction/bid", methods=["POST"])
def api_auction_bid():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}

    aid = int(data.get("auction_id", 0))
    bid_amount = int(data.get("bid_amount", 0))

    a = r.hgetall(auction_key(aid))
    if not a:
        return {"success": False, "message": "拍賣不存在"}, 404
    if a["status"] != "open":
        return {"success": False, "message": "拍賣已結束"}, 400
    if a["seller"] == username:
        return {"success": False, "message": "不能對自己的拍賣出價"}, 400
    if bid_amount <= int(a["current_price"]):
        return {"success": False, "message": "出價需高於目前價格"}, 400

    user = get_user(username)
    if user["gold"] < bid_amount:
        return {"success": False, "message": "金幣不足"}, 400

    # 更新最高出價
    r.hset(auction_key(aid), "current_price", bid_amount)
    r.hset(auction_key(aid), "current_bidder", username)

    log_action(username, "auction_bid", {"aid": aid, "amount": bid_amount})

    return {"success": True}


# ------------------------------------------------------------
# API：直購
# ------------------------------------------------------------
@app.route("/auction/buy", methods=["POST"])
def api_auction_buy():
    username = get_username_from_request()
    if not username:
        return {"success": False, "message": "未登入"}, 401

    data = request.json or {}

    aid = int(data.get("auction_id", 0))
    a = r.hgetall(auction_key(aid))

    if not a:
        return {"success": False, "message": "拍賣不存在"}, 404
    if a["status"] != "open":
        return {"success": False, "message": "拍賣已結束"}, 400
    if a["seller"] == username:
        return {"success": False, "message": "不能購買自己的拍賣"}, 400

    # 價格
    price = int(a["buyout_price"] or a["current_price"])

    buyer = get_user(username)
    seller = get_user(a["seller"])

    if buyer["gold"] < price:
        return {"success": False, "message": "金幣不足"}, 400

    # 金幣轉移
    set_user_field(username, "gold", buyer["gold"] - price)
    set_user_field(a["seller"], "gold", seller["gold"] + price)

    # 給物品或裝備
    if a["type"] == "item":
        r.hincrby(shop_item_key(username), a["item_id"], int(a["qty"]))
    else:
        # 裝備解除上鎖 → 傳給買家
        uid = a["uid"]
        r.hdel(equip_key(a["seller"], uid), "locked")

        # 移動裝備資料
        data = r.hgetall(equip_key(a["seller"], uid))
        r.delete(equip_key(a["seller"], uid))
        r.hset(equip_key(username, uid), mapping=data)

    # 更新拍賣
    r.hset(auction_key(aid), "status", "sold")
    r.zrem("auction:open", aid)

    # Stream：成交紀錄
    r.xadd("stream:auction:sold", {
        "ts": now_iso(),
        "aid": aid,
        "seller": seller["username"],
        "buyer": username,
        "item_id": a["item_id"],
        "uid": a["uid"],
        "qty": a["qty"],
        "price": price
    }, maxlen=5000, approximate=True)

    log_action(username, "auction_buy", {"aid": aid, "price": price})

    return {"success": True}


# ------------------------------------------------------------
# API：取消拍賣（只有賣家或 root）
# ------------------------------------------------------------
@app.route("/auction/cancel", methods=["POST"])
def api_auction_cancel():
    username = get_username_from_request()
    if not username:
        return {"success": False}, 401

    data = request.json or {}
    aid = int(data.get("auction_id"))

    a = r.hgetall(auction_key(aid))
    if not a:
        return {"success": False, "message": "拍賣不存在"}, 404

    token, is_root = check_admin()

    if username != a["seller"] and not is_root:
        return {"success": False, "message": "無權限取消"}, 403

    if a["status"] != "open":
        return {"success": False, "message": "拍賣已結束"}, 400

    # 退回物品
    if a["type"] == "item":
        r.hincrby(shop_item_key(a["seller"]), a["item_id"], int(a["qty"]))
    else:
        uid = a["uid"]
        r.hdel(equip_key(a["seller"], uid), "locked")

    r.delete(auction_key(aid))
    r.zrem("auction:open", aid)

    log_action(username, "auction_cancel", {"aid": aid})

    return {"success": True}
# ============================================================
#              Part 8：Admin & Root 後台管理系統
# ============================================================

ANNOUNCE_KEY = "announcements"

# ------------------------------------------------------------
# Admin 權限檢查工具
# ------------------------------------------------------------
def require_admin():
    token, is_root = check_admin()
    if not token:
        return None, False, {"success": False, "message": "需要管理員權限"}
    return token, is_root, None

def require_root():
    token, is_root = check_admin()
    if not token or not is_root:
        return None, {"success": False, "message": "需要 root 權限"}
    return token, None

# ------------------------------------------------------------
# Admin：查看所有玩家
# ------------------------------------------------------------
@app.route("/admin/players")
def api_admin_players():
    token, is_root, err = require_admin()
    if err:
        return err, 403

    players = []
    for key in r.scan_iter("user:*"):
        username = key.split(":", 1)[1]
        u = get_user(username)
        if not u:
            continue

        online = r.exists(session_key(username))

        players.append({
            "username": username,
            "gold": u["gold"],
            "level": u["level"],
            "exp": u["exp"],
            "online": bool(online),
            "banned": u["banned"],
            "created_at": u.get("created_at", ""),
            "last_login_at": u.get("last_login_at", ""),
        })

    return {"success": True, "players": players}

# ------------------------------------------------------------
# Admin：封鎖玩家
# ------------------------------------------------------------
@app.route("/admin/ban/<target>", methods=["POST"])
def api_admin_ban(target):
    token, is_root, err = require_admin()
    if err:
        return err, 403

    set_user_field(target, "banned", 1)
    destroy_session(target)
    log_action("admin", "ban_player", {"target": target})
    return {"success": True, "message": f"{target} 已封鎖"}

# ------------------------------------------------------------
# Root：解除封鎖
# ------------------------------------------------------------
@app.route("/admin/unban/<target>", methods=["POST"])
def api_admin_unban(target):
    token, is_root, err = require_admin()
    if err:
        return err, 403

    set_user_field(target, "banned", 0)
    log_action("admin", "unban_player", {"target": target})
    return {"success": True, "message": f"{target} 已解除封鎖"}

# ------------------------------------------------------------
# Root：重置密碼
# ------------------------------------------------------------
@app.route("/admin/reset_password/<target>", methods=["POST"])
def api_admin_reset_password(target):
    token, err = require_root()
    if err:
        return err, 403

    new_pw = secrets.token_hex(4)
    r.hset(user_key(target), "password_hash", sha256(new_pw))

    log_action("root", "reset_password", {"target": target})
    return {"success": True, "message": "密碼已重置", "new_password": new_pw}

# ------------------------------------------------------------
# Admin：修改金幣
# ------------------------------------------------------------
@app.route("/admin/gold/<target>", methods=["POST"])
def api_admin_gold(target):
    token, is_root, err = require_admin()
    if err:
        return err, 403

    amount = int(request.args.get("amount", 0))
    user = get_user(target)
    if not user:
        return {"success": False, "message": "玩家不存在"}, 404

    new_gold = max(0, user["gold"] + amount)
    set_user_field(target, "gold", new_gold)

    log_action("admin", "modify_gold", {"target": target, "amount": amount})
    return {"success": True, "new_gold": new_gold}

# ------------------------------------------------------------
# Admin：給 EXP
# ------------------------------------------------------------
@app.route("/admin/exp/<target>", methods=["POST"])
def api_admin_exp(target):
    token, is_root, err = require_admin()
    if err:
        return err, 403

    amount = int(request.args.get("amount", 0))
    result = add_exp(target, amount)

    return {"success": True, "result": result}

# ------------------------------------------------------------
# Admin：列出玩家所有裝備
# ------------------------------------------------------------
@app.route("/admin/equip/<target>")
def api_admin_equip_list(target):
    token, is_root, err = require_admin()
    if err:
        return err, 403

    equips = []
    prefix = f"equip:{target}:"
    for key in r.scan_iter(prefix + "*"):
        parts = key.split(":")
        uid = parts[-1]
        eq = get_equipment(target, uid)
        if eq:
            eq["uid"] = uid
            equips.append(eq)

    return {"success": True, "equips": equips}

# ------------------------------------------------------------
# Root：修改裝備屬性（非常強力）
# ------------------------------------------------------------
@app.route("/admin/equip/edit/<target>/<uid>", methods=["POST"])
def api_admin_edit_equip(target, uid):
    token, err = require_root()
    if err:
        return err, 403

    data = request.json or {}

    allowed = ["atk", "def", "hp", "spd", "crit", "crit_dmg", "enhance"]
    update = {}

    for key, v in data.items():
        if key in allowed:
            update[key] = int(v)

    r.hset(equip_key(target, uid), mapping=update)

    log_action("root", "edit_equip", {"target": target, "uid": uid, "changes": update})
    return {"success": True}

# ------------------------------------------------------------
# Root：刪除裝備
# ------------------------------------------------------------
@app.route("/admin/equip/delete/<target>/<uid>", methods=["DELETE"])
def api_admin_delete_equip(target, uid):
    token, err = require_root()
    if err:
        return err, 403

    delete_equipment(target, uid)
    return {"success": True}

# ------------------------------------------------------------
# Root：發放裝備（GM 指令）
# ------------------------------------------------------------
@app.route("/admin/equip/give/<target>", methods=["POST"])
def api_admin_give_equip(target):
    token, err = require_root()
    if err:
        return err, 403

    equip_type = request.json.get("slot", "weapon")
    uid, eq = generate_equipment(target, equip_type)

    return {"success": True, "uid": uid, "equip": eq}

# ------------------------------------------------------------
# Admin：拍賣行管理
# ------------------------------------------------------------
@app.route("/admin/auction/all")
def api_admin_auction_all():
    token, is_root, err = require_admin()
    if err:
        return err, 403

    auctions = []
    for key in r.scan_iter("auction:*"):
        if key == "auction:next_id":
            continue
        aid = key.split(":")[1]
        a = r.hgetall(key)
        if a:
            auctions.append(a)

    return {"success": True, "auctions": auctions}

# ------------------------------------------------------------
# Root：強制下架拍賣
# ------------------------------------------------------------
@app.route("/admin/auction/remove/<aid>", methods=["POST"])
def api_admin_auction_remove(aid):
    token, err = require_root()
    if err:
        return err, 403

    a = r.hgetall(auction_key(aid))
    if not a:
        return {"success": False, "message": "拍賣不存在"}, 404

    # 退回物品
    seller = a["seller"]

    if a["type"] == "item":
        r.hincrby(shop_item_key(seller), a["item_id"], int(a["qty"]))
    else:
        uid = a["uid"]
        r.hdel(equip_key(seller), uid, "locked")

    r.delete(auction_key(aid))
    r.zrem("auction:open", aid)

    log_action("root", "remove_auction", {"aid": aid})
    return {"success": True}

# ------------------------------------------------------------
# Admin：查看拍賣成交紀錄
# ------------------------------------------------------------
@app.route("/admin/auction/sold")
def api_admin_sold():
    token, is_root, err = require_admin()
    if err:
        return err, 403

    records = r.xrevrange("stream:auction:sold", count=100)
    logs = [fields for sid, fields in records]
    return {"success": True, "sold": logs}

# ------------------------------------------------------------
# Admin：查看出價紀錄
# ------------------------------------------------------------
@app.route("/admin/auction/bids")
def api_admin_bids():
    token, is_root, err = require_admin()
    if err:
        return err, 403

    records = r.xrevrange("stream:auction:bids", count=100)
    logs = [fields for sid, fields in records]
    return {"success": True, "bids": logs}

# ------------------------------------------------------------
# Admin：公告系統
# ------------------------------------------------------------
@app.route("/admin/announce/add", methods=["POST"])
def api_admin_announce_add():
    token, is_root, err = require_admin()
    if err:
        return err, 403

    msg = request.json.get("msg", "").strip()
    if not msg:
        return {"success": False, "message": "公告內容不可空"}

    r.rpush(ANNOUNCE_KEY, msg)
    log_action("admin", "announce_add", {"msg": msg})

    return {"success": True}

@app.route("/admin/announce/list")
def api_admin_announce_list():
    items = r.lrange(ANNOUNCE_KEY, 0, -1)
    return {"success": True, "announcements": items}

@app.route("/admin/announce/delete/<int:index>", methods=["DELETE"])
def api_admin_announce_delete(index):
    items = r.lrange(ANNOUNCE_KEY, 0, -1)

    if index < 0 or index >= len(items):
        return {"success": False, "message": "索引錯誤"}

    target = items[index]
    r.lset(ANNOUNCE_KEY, index, "__DEL__")
    r.lrem(ANNOUNCE_KEY, 1, "__DEL__")

    return {"success": True, "deleted": target}

@app.route("/admin/announce/clear", methods=["POST"])
def api_admin_announce_clear():
    r.delete(ANNOUNCE_KEY)
    log_action("admin", "announce_clear", {})
    return {"success": True}

# ------------------------------------------------------------
# Admin：行為紀錄查詢
# ------------------------------------------------------------
@app.route("/admin/logs")
def api_admin_logs():
    token, is_root, err = require_admin()
    if err:
        return err, 403

    limit = int(request.args.get("limit", 100))
    action_filter = request.args.get("action")

    records = r.xrevrange(ACTIONS_STREAM, count=limit)

    result = []
    for sid, fields in records:
        if action_filter and fields.get("action") != action_filter:
            continue
        result.append(fields)

    return {"success": True, "logs": result}

# ------------------------------------------------------------
# Admin：戰鬥紀錄查詢
# ------------------------------------------------------------
@app.route("/admin/battles")
def api_admin_battles():
    token, is_root, err = require_admin()
    if err:
        return err, 403

    limit = int(request.args.get("limit", 50))
    records = r.xrevrange(BATTLE_STREAM, count=limit)
    logs = []

    for sid, fields in records:
        logs.append(fields)

    return {"success": True, "battles": logs}
# ============================================================
#          Part 9：主程式入口（Render / Flask / SocketIO）
# ============================================================

@app.route("/")
def serve_index():
    """前端遊戲頁面"""
    return send_from_directory(".", "index.html")

@app.route("/admin")
def serve_admin():
    """後台管理頁面"""
    return send_from_directory(".", "admin.html")

@app.route("/health")
def health():
    return {"success": True, "status": "running"}


# ------------------------------------------------------------
# 強化後自動更新戰力排行榜（包裝原本的強化 API）
# ------------------------------------------------------------
# 你前面 Part 2 定義了 /equip/enhance
# 我們將其包裝加入自動更新排行榜（movie-style）

original_enhance = app.view_functions["api_equip_enhance"]

def enhanced_enhance(*args, **kwargs):
    resp = original_enhance(*args, **kwargs)
    # 強化成功或失敗後 → 更新戰力
    try:
        # 取得使用者
        username = get_username_from_request()
        if username:
            update_power_rank(username)
    except:
        pass
    return resp

app.view_functions["api_equip_enhance"] = enhanced_enhance


# ------------------------------------------------------------
# 穿裝／卸裝後也要更新戦力
# ------------------------------------------------------------

original_wear = app.view_functions["api_equip_wear"]
original_unwear = app.view_functions["api_equip_unwear"]

def wrapped_wear(*args, **kwargs):
    resp = original_wear(*args, **kwargs)
    try:
        username = get_username_from_request()
        update_power_rank(username)
    except:
        pass
    return resp

def wrapped_unwear(*args, **kwargs):
    resp = original_unwear(*args, **kwargs)
    try:
        username = get_username_from_request()
        update_power_rank(username)
    except:
        pass
    return resp

app.view_functions["api_equip_wear"] = wrapped_wear
app.view_functions["api_equip_unwear"] = wrapped_unwear


# ------------------------------------------------------------
# EXP 變動也會影響戰力 → 統一更新
# ------------------------------------------------------------

original_add_exp = app.view_functions["api_player_add_exp"]

def wrapped_add_exp(*args, **kwargs):
    resp = original_add_exp(*args, **kwargs)
    try:
        username = get_username_from_request()
        update_power_rank(username)
    except:
        pass
    return resp

app.view_functions["api_player_add_exp"] = wrapped_add_exp


# ------------------------------------------------------------
# Render 相容啟動方式
# ------------------------------------------------------------
if __name__ == "__main__":
    print("===================================")
    print("     MMORPG Server Starting...")
    print("===================================")

    try:
        r.ping()
        print("✓ Connected to Redis")
    except Exception as e:
        print("✗ Redis 連線失敗:", e)

    port = int(os.environ.get("PORT", 5000))

    # 注意：Render 不允許 Flask Debugger，自動啟動 SocketIO
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        allow_unsafe_werkzeug=True
    )
