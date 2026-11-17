"""活動規則：展期、每日份數、組隊判定、獎勵資格。

所有「能不能領」的判斷都集中在這裡，app.py 只負責收發。

2026-11-17 改版（Wutung 決定）：目的是把人帶到攤位。
  - 加好友＋選距離 → 兌換券。到攤位報到：實體補給時間表、試喝、軟糖 2 條。
  - 加碼一：答完 3 題真的假的（不論對錯）→ 再 1 條軟糖。
  - 加碼二：揪一位「新加入的好友」，兩人都到攤位 → 各 1 包勁速補。
  - 到過攤位的人有現場優惠券；沒來的人活動後看到官網折扣碼。
"""
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")

# 展期與每日份數。gummy＝報到的軟糖（份，每份 2 條）；bonus＝答題加碼（條）；crampeace＝包
EVENT_DAYS = {
    "2026-12-17": {"open": "12:00", "close": "20:00", "gummy": 150, "bonus": 150, "crampeace": 80},
    "2026-12-18": {"open": "12:00", "close": "20:00", "gummy": 150, "bonus": 150, "crampeace": 80},
    "2026-12-19": {"open": "10:00", "close": "18:00", "gummy": 300, "bonus": 300, "crampeace": 160},
}
PHYSICAL = ("gummy", "bonus", "crampeace")
QUIZ_SIZE = 3
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


def quiz_ids(uid, pool_size):
    """每個人固定分到 QUIZ_SIZE 題，重新整理不會換題。"""
    order = sorted(range(pool_size),
                   key=lambda i: hashlib.sha256(f"{uid}:{i}".encode()).hexdigest())
    return order[:min(QUIZ_SIZE, pool_size)]


def quiz_answers(db, uid):
    return {r["q"]: bool(r["answer"]) for r in db.execute(
        "SELECT q, answer FROM quiz WHERE user_id=?", (uid,))}


def quiz_done(db, uid, pool_size):
    ans = quiz_answers(db, uid)
    return all(i in ans for i in quiz_ids(uid, pool_size))


def team(db, u):
    """組隊加碼只算「被揪的人是新好友」的那一對。

    回傳 (有沒有任何隊友, 有沒有符合資格的一對, 那一對的對方是否已到攤位)。
    """
    uid = u["id"]
    invitees = [get_user(db, r["id"]) for r in db.execute(
        "SELECT id FROM users WHERE invited_by=?", (uid,))]
    inviter = get_user(db, u["invited_by"]) if u["invited_by"] else None
    others = []                       # 符合資格的對方
    for v in invitees:
        if v["is_new"] and v["is_friend"]:
            others.append(v)
    if inviter and u["is_new"] and inviter["is_friend"]:
        others.append(inviter)
    return (bool(invitees or inviter), bool(others),
            any(o["arrived_at"] for o in others))


def can_set_inviter(db, uid, inviter_id):
    """只在還沒有邀請人時設定；不能邀請自己，也不能互相邀請。"""
    u, inv = get_user(db, uid), get_user(db, inviter_id)
    if not u or not inv or u["invited_by"] or uid == inviter_id:
        return False
    return inv["invited_by"] != uid


# ---- 獎勵資格 ----
# 狀態：locked 還沒資格／waiting_partner 等隊友／ready 可以領（展期前代表已保留）
#       out_today 今日送完／ended 活動結束／claimed 已領

def _stock(db, reward, n):
    if event_over(n):
        return "ended"
    if event_day(n) and left_today(db, reward, n) == 0:
        return "out_today"
    return "ready"


def gummy_status(db, u, claims, n):
    if "gummy" in claims:
        return "claimed"
    if not u["is_friend"] or not u["distance"]:
        return "locked"
    return _stock(db, "gummy", n)


def bonus_status(db, u, claims, n, pool_size):
    if "bonus" in claims:
        return "claimed"
    if not u["is_friend"] or not u["distance"] or not quiz_done(db, u["id"], pool_size):
        return "locked"
    return _stock(db, "bonus", n)


def crampeace_status(db, u, claims, n):
    if "crampeace" in claims:
        return "claimed"
    _any, qualified, other_arrived = team(db, u)
    if not u["is_friend"] or not qualified:
        return "locked"
    if not u["arrived_at"] or not other_arrived:
        return "waiting_partner"
    return _stock(db, "crampeace", n)


def onsite_status(u, claims):
    """現場優惠券：到攤位報到後出現，結帳時由工作人員按「已使用」。"""
    if "onsite" in claims:
        return "used"
    return "ready" if u["arrived_at"] else "locked"


def absent_coupon(u, n):
    """領了券但沒到攤位的好友，活動結束後看到官網折扣碼。"""
    return bool(event_over(n) and u["is_friend"] and u["distance"] and not u["arrived_at"])


def state(db, uid, n, cfg, quiz_pool):
    u = get_user(db, uid)
    claims = claims_of(db, uid)
    d = event_day(n)
    ans = quiz_answers(db, uid)
    ids = quiz_ids(uid, len(quiz_pool))
    any_partner, qualified, other_arrived = team(db, u)
    absent = absent_coupon(u, n)
    quiz = []
    for i in ids:
        q = {"id": i, "text": quiz_pool[i]["text"], "answered": i in ans}
        if i in ans:   # 答過才給正解與解說
            q.update(correct=ans[i] == quiz_pool[i]["answer"], truth=quiz_pool[i]["answer"],
                     explain=quiz_pool[i]["explain"])
        quiz.append(q)
    return {
        "name": u["name"],
        "code": u["code"],
        "is_friend": bool(u["is_friend"]),
        "is_new": bool(u["is_new"]),
        "consent": bool(u["consent_at"]),
        "distance": u["distance"],
        "arrived": bool(u["arrived_at"]),
        "asked": bool(db.execute("SELECT 1 FROM questions WHERE user_id=?", (uid,)).fetchone()),
        "quiz": quiz,
        "quiz_done": all(q["answered"] for q in quiz),
        "has_partner": any_partner,
        "team_qualified": qualified,
        "partner_arrived": other_arrived,
        "invited": bool(u["invited_by"]),
        "rewards": {
            "gummy": gummy_status(db, u, claims, n),
            "bonus": bonus_status(db, u, claims, n, len(quiz_pool)),
            "crampeace": crampeace_status(db, u, claims, n),
            "onsite": onsite_status(u, claims),
            "onsite_text": cfg.get("ONSITE_OFFER", ""),
            "absent": absent,
            "absent_code": cfg.get("COUPON_ABSENT", "") if absent else "",
        },
        "today": {
            "is_event_day": bool(d),
            "over": event_over(n),
            "left": {r: left_today(db, r, n) for r in PHYSICAL} if d else None,
        },
    }
