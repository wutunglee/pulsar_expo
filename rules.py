"""活動規則：展期、每日份數、組隊判定、獎勵資格。

所有「能不能領」的判斷都集中在這裡，app.py 只負責收發。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")

# 展期與每日份數（Wutung 2026-11-16 決定：週四週五 150/80，週六兩倍）
EVENT_DAYS = {
    "2026-12-17": {"open": "12:00", "close": "20:00", "gummy": 150, "crampeace": 80},
    "2026-12-18": {"open": "12:00", "close": "20:00", "gummy": 150, "crampeace": 80},
    "2026-12-19": {"open": "10:00", "close": "18:00", "gummy": 300, "crampeace": 160},
}
STATIONS = (1, 2, 3, 4, 5)
GUMMY_NEED = 3          # 完成幾站可領軟糖
MAX_CODE_ATTEMPTS = 10  # 每人每天通關碼可嘗試次數
DISTANCES = ("half", "full", "cheer")


def now():
    return datetime.now(TZ)


def iso(n):
    return n.astimezone(TZ).isoformat(timespec="seconds")


def day_key(n):
    return n.astimezone(TZ).strftime("%Y-%m-%d")


def _at(day, hm):
    return datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def event_day(n):
    """今天是展期的話回傳當日設定，否則 None。"""
    return EVENT_DAYS.get(day_key(n))


def event_over(n):
    last = max(EVENT_DAYS)
    return n > _at(last, EVENT_DAYS[last]["close"])


# ---- 查詢 ----

def get_user(db, uid):
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def stations_done(db, uid):
    return {r["station"] for r in db.execute(
        "SELECT station FROM completions WHERE user_id=?", (uid,))}


def claims_of(db, uid):
    return {r["reward"]: r["day"] for r in db.execute(
        "SELECT reward, day FROM claims WHERE user_id=?", (uid,))}


def left_today(db, reward, n):
    d = event_day(n)
    if not d:
        return 0
    used = db.execute("SELECT COUNT(*) c FROM claims WHERE reward=? AND day=?",
                      (reward, day_key(n))).fetchone()["c"]
    return max(0, d[reward] - used)


def partners(db, uid):
    """跟我同隊的人：邀請我的人，加上我邀請的人。"""
    u = get_user(db, uid)
    ids = [r["id"] for r in db.execute("SELECT id FROM users WHERE invited_by=?", (uid,))]
    if u and u["invited_by"]:
        ids.append(u["invited_by"])
    return ids


# ---- 寫入 ----

def complete(db, uid, station, n, meta=None):
    db.execute("INSERT OR IGNORE INTO completions(user_id, station, at, meta) VALUES(?,?,?,?)",
               (uid, station, iso(n), meta))


def settle_team(db, uid, n):
    """被邀請的人已加好友且完成第 1 站時，邀請雙方的第 3 站都算完成。"""
    u = get_user(db, uid)
    if not u:
        return
    pairs = [(uid, r["id"]) for r in db.execute(
        "SELECT id FROM users WHERE invited_by=?", (uid,))]
    if u["invited_by"]:
        pairs.append((u["invited_by"], uid))
    for inviter, invitee in pairs:
        v = get_user(db, invitee)
        if v and v["is_friend"] and 1 in stations_done(db, invitee) and get_user(db, inviter):
            complete(db, inviter, 3, n, "team")
            complete(db, invitee, 3, n, "team")


def can_set_inviter(db, uid, inviter_id):
    """只在還沒有邀請人時設定；不能邀請自己，也不能互相邀請。"""
    u, inv = get_user(db, uid), get_user(db, inviter_id)
    if not u or not inv or u["invited_by"] or uid == inviter_id:
        return False
    return inv["invited_by"] != uid


# ---- 獎勵資格 ----

def gummy_status(db, u, done, claims, n):
    if "gummy" in claims:
        return "claimed"
    if "fallback" in claims:
        return "switched"
    if not u["is_friend"] or len(done) < GUMMY_NEED:
        return "locked"
    if event_over(n):
        return "ended"
    if event_day(n) and left_today(db, "gummy", n) == 0:
        return "out_today"
    return "ready"


def crampeace_status(db, u, done, claims, n):
    if "crampeace" in claims:
        return "claimed"
    if not u["is_friend"] or 3 not in done or 4 not in done:
        return "locked"
    both = any(4 in stations_done(db, p) and get_user(db, p)["is_friend"]
               for p in partners(db, u["id"]))
    if not both:
        return "waiting_partner"
    if event_over(n):
        return "ended"
    if event_day(n) and left_today(db, "crampeace", n) == 0:
        return "out_today"
    return "ready"


def fallback_status(db, u, done, claims, n):
    """實體軟糖發完或活動結束後，完成 3 站的人可改領官網折扣券（與軟糖二選一）。"""
    if "fallback" in claims:
        return "taken"
    if "gummy" in claims or not u["is_friend"] or len(done) < GUMMY_NEED:
        return "none"
    if event_over(n) or (event_day(n) and left_today(db, "gummy", n) == 0):
        return "available"
    return "none"


def state(db, uid, n, cfg):
    u = get_user(db, uid)
    done = stations_done(db, uid)
    claims = claims_of(db, uid)
    d = event_day(n)
    all_done = u["is_friend"] and len(done) == len(STATIONS)
    if all_done and "coupon5" not in claims:
        db.execute("INSERT OR IGNORE INTO claims(user_id, reward, day, at) VALUES(?,?,?,?)",
                   (uid, "coupon5", day_key(n), iso(n)))
        claims["coupon5"] = day_key(n)
    fb = fallback_status(db, u, done, claims, n)
    return {
        "name": u["name"],
        "code": u["code"],
        "is_friend": bool(u["is_friend"]),
        "consent": bool(u["consent_at"]),
        "distance": u["distance"],
        "stations": {str(s): (s in done) for s in STATIONS},
        "count": len(done),
        "has_partner": bool(partners(db, uid)),
        "rewards": {
            "gummy": gummy_status(db, u, done, claims, n),
            "crampeace": crampeace_status(db, u, done, claims, n),
            "fallback": fb,
            "fallback_code": cfg.get("COUPON_FALLBACK", "") if fb == "taken" else "",
            "coupon5": bool(all_done),
            "coupon5_code": cfg.get("COUPON_5", "") if all_done else "",
        },
        "today": {
            "is_event_day": bool(d),
            "over": event_over(n),
            "gummy_left": left_today(db, "gummy", n) if d else None,
            "crampeace_left": left_today(db, "crampeace", n) if d else None,
        },
    }
