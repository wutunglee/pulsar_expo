"""活動規則：展期、每日份數、組隊判定、獎勵資格。

所有「能不能領」的判斷都集中在這裡，app.py 只負責收發。

2026-11-17 改版（Wutung 決定）：目的是把人帶到攤位。
  - 加好友＋選距離 → 兌換券。到攤位報到：實體補給時間表、軟糖 2 條。
  - 加碼一：答完 3 題真的假的（不論對錯）→ 再 1 條軟糖。
  - 加碼二：揪一位「新加入的好友」，兩人都到攤位 → 各 1 包勁速補。
  - 現場優惠用抽的：主機先洗好牌，客人選一張後全部翻開；
    限博覽會現場使用；沒來的人活動後看到官網折扣碼。

2026-11-23 改版（Aibo 提案、Wutung 定案）：
  - 獎項 8 種、一副 16 張（見 DECK）。領券後在家就能抽，到攤位出示 QR code 兌換。
  - 贈品類的獎抽到就送、不用消費，每人一次；發出的張數有總量上限（GIFT_QUOTA）。
  - 折扣類三天都能用；指定商品特價的獎每人合計最多 2 件，其他商品 93 折。
  - 勁速補現場一律 601 元、不再打折（抽到 500 元的人除外）。沒有券的人用官網價。
  - 結帳由核銷頁點選商品、系統算錢、記錄每一筆（price_cart）。
"""
import hashlib
import secrets
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

# 現場優惠抽獎：一副牌裡每種獎各幾張。機率＝張數／總張數。改這裡只影響之後才發的牌。
# 文字在 content.PRIZES。保底：有券的人沒被獎項涵蓋的商品一律 93 折。
DECK = (("p88", 4), ("gum200", 3), ("flask100", 2), ("g_gummy", 2), ("deal500", 2),
        ("g_cramp", 1), ("g_noxx", 1), ("half", 1))
# 贈品類的獎：抽出幾張之後就不再放進新發的牌（換成 88 折）。抽到沒來的人也佔名額，所以實送只會更少。
GIFT_QUOTA = {"g_gummy": 80, "g_cramp": 40, "g_noxx": 40}
GIFT_FALLBACK = "p88"
# 指定商品特價的獎：(適用的商品標籤, 特價, 每人合計上限件數)
SPECIAL = {"gum200": (("gummy",), 200, 2), "flask100": (("flask",), 100, 2),
           "deal500": (("cramp", "noxx"), 500, 2)}
BASE_RATE = 0.93          # 有券的人的保底折扣
QUIZ_SIZE = 3
DISTANCES = ("half", "full", "cheer")


def now():
    return datetime.now(TZ)


def iso(n):
    return n.astimezone(TZ).isoformat(timespec="seconds")


def parse(s):
    return datetime.fromisoformat(s)


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


def get_draw(db, uid):
    return db.execute("SELECT * FROM draws WHERE user_id=?", (uid,)).fetchone()


def prize_of(db, uid):
    r = get_draw(db, uid)
    return r["prize"] if r else None


def gift_drawn(db):
    """各種贈品獎已經被抽出幾張。"""
    got = {k: 0 for k in GIFT_QUOTA}
    for r in db.execute("SELECT prize, COUNT(*) c FROM draws WHERE prize IS NOT NULL GROUP BY prize"):
        if r["prize"] in got:
            got[r["prize"]] = r["c"]
    return got


def deal(db, uid, n):
    """洗一副牌存起來。在客人看到牌背之前就做，之後他選哪一張都對應這副牌。
    已經抽滿名額的贈品不放進新的牌，位置換成 88 折。"""
    if get_draw(db, uid):
        return
    drawn = gift_drawn(db)
    full = {k for k, cap in GIFT_QUOTA.items() if drawn[k] >= cap}
    cards = [GIFT_FALLBACK if key in full else key for key, count in DECK for _ in range(count)]
    for i in range(len(cards) - 1, 0, -1):          # Fisher–Yates，用系統亂數
        k = secrets.randbelow(i + 1)
        cards[i], cards[k] = cards[k], cards[i]
    db.execute("INSERT OR IGNORE INTO draws(user_id, hand, dealt_at) VALUES(?,?,?)",
               (uid, ",".join(cards), iso(n)))


def pick_card(db, uid, pick, n):
    """選一張。已經選過就不變。回傳抽到的獎，位置不合法回傳 None。"""
    r = get_draw(db, uid)
    if not r:
        return None
    if r["prize"]:
        return r["prize"]
    hand = r["hand"].split(",")
    if not isinstance(pick, int) or isinstance(pick, bool) or not 0 <= pick < len(hand):
        return None
    db.execute("UPDATE draws SET pick=?, prize=?, at=? WHERE user_id=? AND prize IS NULL",
               (pick, hand[pick], iso(n), uid))
    return prize_of(db, uid)


def draw_view(db, uid):
    """給前端的抽獎狀態。還沒選之前只給張數，不給牌面。"""
    r = get_draw(db, uid)
    if not r:
        return {"cards": 0, "prize": None}
    hand = r["hand"].split(",")
    if not r["prize"]:
        return {"cards": len(hand), "prize": None}
    return {"cards": len(hand), "prize": r["prize"], "pick": r["pick"], "hand": hand}


# ---- 結帳計價 ----

def _round(x):
    return int(x + 0.5)


def special_used(db, uid):
    """這個人已經用特價買了幾件（取消的不算）。"""
    return db.execute(
        "SELECT COALESCE(SUM(i.special_qty), 0) c FROM sale_items i JOIN sales s ON s.id=i.sale_id "
        "WHERE s.user_id=? AND s.voided_at IS NULL", (uid,)).fetchone()["c"]


def price_cart(items, prize=None, member=True, used=0):
    """算一張單。items：[(商品 dict, 數量)]。回傳每樣的小計、總額。

    沒有券（member=False）：官網價；有多件價的商品買 2 件以上用多件價。
    有券：有現場價的商品（勁速補）用現場價、不再打折；其他 93 折。再依抽到的獎調整：
      p88   兩件以上，可打折的商品全部 88 折
      half  兩件以上，可打折的商品裡最便宜的一件半價、其餘原價；若這樣反而比 93 折貴就用 93 折
      特價類 指定商品用特價，每人合計最多 cap 件（used 是之前用掉的）；省最多的先套
      贈品類 沒有額外折扣
    """
    units = []
    for p, qty in items:
        for _ in range(qty):
            units.append({"p": p, "list": p["price"]})
    n = len(units)
    if not member:
        qty_of = {}
        for u in units:
            qty_of[u["p"]["id"]] = qty_of.get(u["p"]["id"], 0) + 1
        for u in units:
            multi = u["p"].get("multi")
            if multi and qty_of[u["p"]["id"]] >= 2:
                u["price"], u["how"] = multi, "多件價"
            else:
                u["price"], u["how"] = u["list"], "官網價"
    else:
        for u in units:
            if u["p"].get("onsite"):
                u["fixed"], u["price"], u["how"] = True, u["p"]["onsite"], "現場價"
            else:
                u["fixed"], u["price"], u["how"] = False, _round(u["list"] * BASE_RATE), "93 折"
        free = [u for u in units if not u["fixed"]]
        if prize == "p88" and n >= 2:
            for u in free:
                u["price"], u["how"] = _round(u["list"] * 0.88), "88 折"
        elif prize == "half" and n >= 2 and free:
            cheapest = min(free, key=lambda u: u["list"])
            alt = sum(u["list"] for u in free) - cheapest["list"] + _round(cheapest["list"] * 0.5)
            if alt <= sum(u["price"] for u in free):
                for u in free:
                    u["price"], u["how"] = u["list"], "原價"
                cheapest["price"], cheapest["how"] = _round(cheapest["list"] * 0.5), "半價"
        elif prize in SPECIAL:
            tags, sp, cap = SPECIAL[prize]
            ok = sorted((u for u in units if u["p"].get("tag") in tags and u["price"] > sp),
                        key=lambda u: -u["price"])
            for u in ok[:max(0, cap - used)]:
                u["price"], u["how"], u["special"] = sp, f"{sp} 元", True
    lines, order = {}, []
    for u in units:
        pid = u["p"]["id"]
        if pid not in lines:
            order.append(pid)
            lines[pid] = {"id": pid, "name": u["p"]["name"], "qty": 0, "list": u["list"],
                          "amount": 0, "special": 0, "how": {}}
        ln = lines[pid]
        ln["qty"] += 1
        ln["amount"] += u["price"]
        ln["special"] += 1 if u.get("special") else 0
        ln["how"][u["how"]] = ln["how"].get(u["how"], 0) + 1
    out = []
    for pid in order:
        ln = lines[pid]
        ln["how"] = "、".join(k if ln["qty"] == 1 else f"{k} ×{c}" for k, c in ln["how"].items())
        out.append(ln)
    return {"lines": out, "total": sum(ln["amount"] for ln in out),
            "list_total": sum(u["list"] for u in units), "units": n}


def prize_gift_status(db, uid, claims, n):
    """抽到贈品的人：claimed 已領／ready 可以領／ended 活動結束。不是贈品獎回傳 None。"""
    if prize_of(db, uid) not in GIFT_QUOTA:
        return None
    if "prize" in claims:
        return "claimed"
    return "ended" if event_over(n) else "ready"


def special_left(db, uid):
    """抽到指定商品特價的人還能用特價買幾件。其他獎回傳 None。"""
    prize = prize_of(db, uid)
    if prize not in SPECIAL:
        return None
    return max(0, SPECIAL[prize][2] - special_used(db, uid))


def onsite_status(u, n):
    """現場優惠：領券後就能抽，限博覽會現場使用。"""
    if not u["is_friend"] or not u["distance"]:
        return "locked"
    return "ended" if event_over(n) else "ready"


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
        "plan_sent": bool(u["plan_sent_at"]),
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
            "onsite": onsite_status(u, n),
            "onsite_uses": db.execute(
                "SELECT COUNT(*) c FROM sales WHERE user_id=? AND voided_at IS NULL",
                (uid,)).fetchone()["c"],
            **draw_view(db, uid),
            "prize_gift": prize_gift_status(db, uid, claims, n),
            "special_left": special_left(db, uid),
            "absent": absent,
            "absent_code": cfg.get("COUPON_ABSENT", "") if absent else "",
            "absent_text": cfg.get("COUPON_ABSENT_TEXT", ""),
        },
        "today": {
            "is_event_day": bool(d),
            "over": event_over(n),
            "left": {r: left_today(db, r, n) for r in PHYSICAL} if d else None,
        },
    }
