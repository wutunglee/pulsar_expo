"""PulsarPump 博覽會補給站兌換券。Flask＋SQLite。"""
import csv
import hmac
import io
import json
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Response, g, jsonify, redirect, render_template, request,
                   session)
from itsdangerous import BadSignature, URLSafeSerializer

import content
import db as dbm
import line_api
import rules

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉容易看錯的 0 O 1 I L
DEFAULT_SECRET = "dev-only-change-me"


def load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def err(msg, status=400, **extra):
    return jsonify({"ok": False, "error": msg, **extra}), status


def create_app(test_config=None):
    app = Flask(__name__)
    load_dotenv(os.path.join(app.root_path, ".env"))
    env = os.environ.get
    app.config.update(
        SECRET_KEY=env("SECRET_KEY", DEFAULT_SECRET),
        DEV_MODE=env("DEV_MODE", "0") == "1",
        DB_PATH=env("DB_PATH", os.path.join(app.root_path, "data", "expo.db")),
        LIFF_ID=env("LIFF_ID", ""),
        LINE_LOGIN_CHANNEL_ID=env("LINE_LOGIN_CHANNEL_ID", ""),
        LINE_OA_ID=env("LINE_OA_ID", ""),
        # 認人方式：oa＝官方帳號 webhook 發專屬連結；liff＝LINE Login／LIFF
        IDENTITY=env("IDENTITY", "oa"),
        # 好友確認：oa＝webhook 的加入／封鎖通知；line＝LIFF 開卡時向 LINE 查；staff＝攤位人員確認
        FRIEND_CHECK=env("FRIEND_CHECK", ""),
        LINE_CHANNEL_ID=env("LINE_CHANNEL_ID", ""),
        LINE_CHANNEL_SECRET=env("LINE_CHANNEL_SECRET", ""),
        LINE_KEYWORD=env("LINE_KEYWORD", "任務卡"),
        LINE_KEYWORD_PLAN=env("LINE_KEYWORD_PLAN", "補給表"),
        # 有人新加好友時，是否自動回一則兌換券連結。活動開始前設 0，只有輸入關鍵字的人才會收到。
        REPLY_ON_FOLLOW=env("REPLY_ON_FOLLOW", "1") == "1",
        # 選好距離時主動把補給時間表傳到他的 LINE（每人一則，計入每月訊息則數）
        PUSH_PLAN=env("PUSH_PLAN", "1") == "1",
        PUBLIC_URL=env("PUBLIC_URL", "").rstrip("/"),
        # 收到 LINE 事件後，原封不動再轉一份到這個網址（原本接 webhook 的系統，例如官網外掛）
        WEBHOOK_FORWARD_URL=env("WEBHOOK_FORWARD_URL", ""),
        STAFF_PIN=env("STAFF_PIN", ""),
        ADMIN_TOKEN=env("ADMIN_TOKEN", ""),
        COUPON_ABSENT=env("COUPON_ABSENT", ""),   # 沒到攤位的人，活動後看到的官網折扣碼
        COUPON_ABSENT_TEXT=env("COUPON_ABSENT_TEXT", "滿 2,000 元折 250 元，2027/1/19 前有效"),
        # 排練用：填展期的某一天（例如 2026-12-17），系統就把今天當成那一天，可以試報到與結帳。
        # 正式上線前一定要拿掉。
        REHEARSAL_DAY=env("REHEARSAL_DAY", ""),
        NOW_FN=None,  # 測試時可換成固定時間
        PERMANENT_SESSION_LIFETIME=timedelta(days=60),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)
    app.config["SESSION_COOKIE_SECURE"] = not app.config["DEV_MODE"]
    if app.config["IDENTITY"] not in ("oa", "liff"):
        raise RuntimeError("IDENTITY 只能是 oa 或 liff")
    if not app.config["FRIEND_CHECK"]:
        app.config["FRIEND_CHECK"] = "line" if app.config["IDENTITY"] == "liff" else "oa"
    if app.config["FRIEND_CHECK"] not in ("oa", "line", "staff"):
        raise RuntimeError("FRIEND_CHECK 只能是 oa、line 或 staff")
    if app.config["FRIEND_CHECK"] == "line" and app.config["IDENTITY"] != "liff":
        raise RuntimeError("FRIEND_CHECK=line 需要 IDENTITY=liff")
    if not app.config["DEV_MODE"] and app.config["SECRET_KEY"] == DEFAULT_SECRET:
        raise RuntimeError("正式環境必須在 .env 設定 SECRET_KEY")

    if app.config["REHEARSAL_DAY"] and app.config["REHEARSAL_DAY"] not in rules.EVENT_DAYS:
        raise RuntimeError("REHEARSAL_DAY 要填展期中的一天，例如 2026-12-17")

    dbm.init(app.config["DB_PATH"], content.PRODUCTS)
    _conn = dbm.connect(app.config["DB_PATH"])
    _purged = rules.purge_stale_hands(_conn, set(content.PRIZES))
    _conn.close()
    if _purged:
        app.logger.warning("清掉 %d 副舊版的牌，這些人可以重新抽", _purged)
    card_signer = URLSafeSerializer(app.config["SECRET_KEY"], salt="card-link")
    pin_fails = {}  # ip -> (次數, 解鎖時間)

    def now():
        fn = app.config["NOW_FN"]
        if fn:
            return fn()
        n = rules.now()
        if app.config["REHEARSAL_DAY"]:
            d = datetime.strptime(app.config["REHEARSAL_DAY"], "%Y-%m-%d")
            n = n.replace(year=d.year, month=d.month, day=d.day)
        return n

    def db():
        if "db" not in g:
            g.db = dbm.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def me_state():
        uid, n = session["uid"], now()
        # 有資格抽現場優惠的人，在回傳畫面資料之前先把牌洗好存檔
        if rules.onsite_status(rules.get_user(db(), uid), n) == "ready":
            rules.deal(db(), uid, n)
        return rules.state(db(), uid, n, app.config, content.QUIZ)

    def need_user(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            uid = session.get("uid")
            if not uid or not rules.get_user(db(), uid):
                return err("請重新開啟任務卡", 401)
            return fn(*a, **kw)
        return wrapper

    def need_consent(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not rules.get_user(db(), session["uid"])["consent_at"]:
                return err("請先同意資料使用說明", 403)
            return fn(*a, **kw)
        return wrapper

    def need_staff(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            pin = app.config["STAFF_PIN"]
            if not pin:
                return err("尚未設定 STAFF_PIN", 503)
            ip = request.remote_addr or "?"
            fails, until = pin_fails.get(ip, (0, 0))
            if time.time() < until:
                return err("嘗試次數過多，請 10 分鐘後再試", 429)
            given = request.headers.get("X-Staff-Pin", "")
            if not hmac.compare_digest(given.encode(), pin.encode()):
                fails += 1
                pin_fails[ip] = (fails, time.time() + 600 if fails >= 10 else 0)
                return err("PIN 不正確", 401)
            pin_fails.pop(ip, None)
            return fn(*a, **kw)
        return wrapper

    def need_admin(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            token = app.config["ADMIN_TOKEN"]
            if not token:
                return err("尚未設定 ADMIN_TOKEN", 503)
            given = request.args.get("token", "")
            if not hmac.compare_digest(given.encode(), token.encode()):
                return err("沒有權限", 401)
            return fn(*a, **kw)
        return wrapper

    def new_code():
        for _ in range(20):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
            if not db().execute("SELECT 1 FROM users WHERE code=?", (code,)).fetchone():
                return code
        raise RuntimeError("無法產生代碼")

    def body():
        return request.get_json(silent=True) or {}

    def upsert_user(uid, name, friend=None, is_new=False):
        """建立或更新參加者。friend=None 表示這次不動好友狀態。
        is_new 只在第一次建立時寫入：活動期間才加入的好友為 True。"""
        conn = db()
        if rules.get_user(conn, uid):
            if name:
                conn.execute("UPDATE users SET name=? WHERE id=?", (name[:60], uid))
            if friend is not None:
                conn.execute("UPDATE users SET is_friend=? WHERE id=?", (int(friend), uid))
        else:
            conn.execute(
                "INSERT INTO users(id, name, code, is_friend, is_new, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (uid, name[:60], new_code(), int(bool(friend)), int(bool(is_new)),
                 rules.iso(now())))

    def set_inviter(uid, code):
        """回傳是否成功設定邀請人。"""
        code = str(code or "").strip().upper()
        if not code:
            return False
        conn = db()
        inv = conn.execute("SELECT id FROM users WHERE code=?", (code,)).fetchone()
        if not inv or not rules.can_set_inviter(conn, uid, inv["id"]):
            return False
        conn.execute("UPDATE users SET invited_by=? WHERE id=?", (inv["id"], uid))
        return True

    def login(uid):
        session.clear()
        session.permanent = True
        session["uid"] = uid

    def send_plan_once(uid, dist):
        """第一次選好距離時，把補給時間表傳到他的 LINE。之後改距離不再主動傳（輸入關鍵字可再拿）。"""
        c = app.config
        u = rules.get_user(db(), uid)
        if (not c["PUSH_PLAN"] or u["plan_sent_at"] or not u["is_friend"]
                or not c["LINE_CHANNEL_SECRET"] or not uid.startswith("U")):
            return
        try:
            token = line_api.channel_token(c["LINE_CHANNEL_ID"], c["LINE_CHANNEL_SECRET"])
            line_api.push(token, uid, content.plan_message(dist))
        except line_api.LineError:
            app.logger.warning("補給時間表傳送失敗")
            return
        db().execute("UPDATE users SET plan_sent_at=? WHERE id=?", (rules.iso(now()), uid))

    def card_link(uid):
        c = app.config
        if c["IDENTITY"] == "liff" and c["LIFF_ID"]:
            return f"https://liff.line.me/{c['LIFF_ID']}"     # LIFF 自己會認人，不用專屬連結
        base = app.config["PUBLIC_URL"] or request.url_root.rstrip("/")
        return f"{base}/t/{card_signer.dumps(uid)}"

    # ---------- 頁面 ----------

    @app.get("/")
    def index():
        resp = Response(render_template("index.html"))
        ref = request.args.get("ref", "").strip().upper()
        if ref.isalnum() and len(ref) == 5:
            # 朋友點邀請連結時還沒有身分，先記住邀請碼，等他拿到專屬連結再套用
            resp.set_cookie("pending_ref", ref, max_age=30 * 86400, httponly=True,
                            samesite="Lax", secure=app.config["SESSION_COOKIE_SECURE"])
        return resp

    @app.get("/t/<token>")
    def open_card(token):
        """官方帳號傳給每個人的專屬連結：點開就是他的任務卡。"""
        try:
            uid = card_signer.loads(token)
        except BadSignature:
            return redirect("/")
        if not rules.get_user(db(), uid):
            return redirect("/")
        login(uid)
        resp = redirect("/")
        set_inviter(uid, request.cookies.get("pending_ref"))
        resp.delete_cookie("pending_ref")
        return resp

    @app.get("/staff")
    def staff_page():
        return render_template("staff.html")

    @app.get("/admin")
    def admin_page():
        return render_template("admin.html")

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    # ---------- 參加者 ----------

    @app.get("/api/config")
    def api_config():
        c = app.config
        return jsonify({
            "liff_id": c["LIFF_ID"] if c["IDENTITY"] == "liff" else "",
            "dev": c["DEV_MODE"], "oa_id": c["LINE_OA_ID"], "rehearsal": bool(c["REHEARSAL_DAY"]),
            "identity": c["IDENTITY"], "friend_check": c["FRIEND_CHECK"],
            "keyword": c["LINE_KEYWORD"],
            "has_pending_ref": bool(request.cookies.get("pending_ref")),
            "event": content.EVENT, "plans": content.PLANS,
            "quiz_after": content.QUIZ_AFTER,
            "prizes": content.PRIZES, "prize_base": content.PRIZE_BASE,
            "invite_text": content.INVITE_TEXT, "privacy": content.PRIVACY,
            "privacy_url": content.PRIVACY_URL, "disclaimer": content.DISCLAIMER,
            "reviewed": content.REVIEWED_BY_DOCTOR,
            "caps": {d: {r: v[r] for r in rules.PHYSICAL} for d, v in rules.EVENT_DAYS.items()},
        })

    @app.post("/api/session")
    def api_session():
        b, c = body(), app.config
        if c["DEV_MODE"] and b.get("dev_user"):
            uid = "dev:" + str(b["dev_user"])[:30]
            name, friend = str(b["dev_user"])[:30], bool(b.get("dev_friend"))
            is_new = bool(b.get("dev_new", True))
        elif c["IDENTITY"] != "liff":
            return err("請從 LINE 官方帳號傳給你的連結開啟任務卡", 401)
        else:
            if not c["LINE_LOGIN_CHANNEL_ID"]:
                return err("尚未設定 LINE 頻道", 503)
            try:
                uid, name = line_api.verify_id_token(b.get("id_token", ""),
                                                     c["LINE_LOGIN_CHANNEL_ID"])
                friend = (line_api.is_friend(b.get("access_token", ""))
                          if c["FRIEND_CHECK"] == "line" else None)
            except line_api.LineError as e:
                return err(f"LINE 登入失敗：{e}", 401)
            is_new = friend is False   # 第一次開券時還不是好友，才算新好友
        upsert_user(uid, name, friend, is_new)
        set_inviter(uid, b.get("ref"))
        login(uid)
        return jsonify({"ok": True, "state": me_state()})

    @app.post("/api/ref")
    @need_user
    def api_ref():
        """手動輸入朋友的邀請碼（點連結時沒帶到邀請碼的補救）。"""
        uid = session["uid"]
        if rules.get_user(db(), uid)["invited_by"]:
            return err("你已經有隊友了")
        if not set_inviter(uid, body().get("code")):
            return err("代碼不對，請再向朋友確認一次")
        return jsonify({"ok": True, "state": me_state()})

    # ---------- LINE 官方帳號 webhook ----------

    @app.post("/line/webhook")
    def line_webhook():
        c = app.config
        if not c["LINE_CHANNEL_SECRET"]:
            return err("尚未設定 LINE_CHANNEL_SECRET", 503)
        raw = request.get_data()
        if not line_api.verify_signature(raw, request.headers.get("X-Line-Signature", ""),
                                         c["LINE_CHANNEL_SECRET"]):
            return err("簽章不正確", 401)
        if c["WEBHOOK_FORWARD_URL"]:
            try:   # 先轉給原本的接收端，失敗不影響我們自己的處理
                line_api.forward(c["WEBHOOK_FORWARD_URL"], raw,
                                 request.headers.get("X-Line-Signature", ""))
            except line_api.LineError:
                app.logger.warning("webhook 轉送失敗")
        try:
            events = (request.get_json(silent=True) or {}).get("events") or []
        except AttributeError:
            events = []
        for ev in events:
            try:
                handle_event(ev)
            except Exception:  # 單一事件失敗不影響其他事件，也不讓 LINE 重送整批
                app.logger.exception("webhook 事件處理失敗")
        return jsonify({"ok": True})

    def handle_event(ev):
        c = app.config
        src = ev.get("source") or {}
        uid = src.get("userId")
        if src.get("type") != "user" or not uid:
            return
        kind = ev.get("type")
        if kind == "unfollow":
            db().execute("UPDATE users SET is_friend=0 WHERE id=?", (uid,))
            return
        want_plan = False
        if kind == "message":
            m = ev.get("message") or {}
            text = (m.get("text") or "").strip() if m.get("type") == "text" else ""
            want_plan = text == c["LINE_KEYWORD_PLAN"]
            if text != c["LINE_KEYWORD"] and not want_plan:
                return  # 其他訊息一律不回，留給真人客服
        elif kind != "follow":
            return
        token = ""
        try:
            token = line_api.channel_token(c["LINE_CHANNEL_ID"], c["LINE_CHANNEL_SECRET"])
        except line_api.LineError:
            app.logger.warning("拿不到 channel access token，這次無法回覆")
        first = not rules.get_user(db(), uid)
        name = line_api.get_profile(token, uid) if token and first else ""
        # 加好友事件進來的是新好友；輸入關鍵字的是原本就在的好友
        upsert_user(uid, name, True, is_new=(kind == "follow"))
        if kind == "follow" and not first:
            # LIFF 模式下，網頁可能比加好友通知早幾秒建立這個人：剛建立就收到加好友，算新好友
            u = rules.get_user(db(), uid)
            if not u["is_new"] and (now() - rules.parse(u["created_at"])).total_seconds() <= 600:
                db().execute("UPDATE users SET is_new=1 WHERE id=?", (uid,))
        if kind == "follow" and not c["REPLY_ON_FOLLOW"]:
            return
        if token and ev.get("replyToken"):
            try:
                u = rules.get_user(db(), uid)
                if want_plan and u["distance"]:
                    text = content.plan_message(u["distance"])     # 回覆訊息不計則數
                elif want_plan:
                    text = content.PLAN_NOT_YET.format(link=card_link(uid))
                else:
                    text = content.CARD_MESSAGE.format(link=card_link(uid))
                line_api.reply(token, ev["replyToken"], text)
            except line_api.LineError:
                app.logger.warning("回覆任務卡連結失敗")

    @app.get("/api/me")
    @need_user
    def api_me():
        return jsonify({"ok": True, "state": me_state()})

    @app.post("/api/consent")
    @need_user
    def api_consent():
        db().execute("UPDATE users SET consent_at=COALESCE(consent_at, ?) WHERE id=?",
                     (rules.iso(now()), session["uid"]))
        return jsonify({"ok": True, "state": me_state()})

    @app.post("/api/distance")
    @need_user
    @need_consent
    def set_distance():
        dist = body().get("distance")
        if dist not in rules.DISTANCES:
            return err("請選擇距離")
        uid = session["uid"]
        if app.config["FRIEND_CHECK"] != "staff" and not rules.get_user(db(), uid)["is_friend"]:
            return err("請先加入 PulsarPump 的 LINE 好友", 403, reason="not_friend")
        db().execute("UPDATE users SET distance=? WHERE id=?", (dist, uid))
        send_plan_once(uid, dist)
        return jsonify({"ok": True, "state": me_state()})

    @app.post("/api/quiz")
    @need_user
    @need_consent
    def answer_quiz():
        b, uid = body(), session["uid"]
        q, ans = b.get("q"), b.get("answer")
        if q not in rules.quiz_ids(uid, len(content.QUIZ)) or not isinstance(ans, bool):
            return err("請選擇答案")
        # 第一次的答案算數，重送不會改
        db().execute("INSERT OR IGNORE INTO quiz(user_id, q, answer, at) VALUES(?,?,?,?)",
                     (uid, q, int(ans), rules.iso(now())))
        return jsonify({"ok": True, "state": me_state()})

    @app.post("/api/draw")
    @need_user
    @need_consent
    def draw():
        """選一張牌。牌在他看到牌背之前就洗好存檔了，這裡只記錄他選的位置。每人一次。"""
        n, uid = now(), session["uid"]
        me_state()                                   # 確保牌已經發好
        if not rules.get_draw(db(), uid):
            return err("先加好友、選好距離，才能抽現場優惠")
        if rules.pick_card(db(), uid, body().get("pick"), n) is None:
            return err("請選一張牌")
        return jsonify({"ok": True, "state": me_state()})

    @app.post("/api/ask")
    @need_user
    @need_consent
    def ask():
        text = str(body().get("text") or "").strip()
        if not text:
            return err("請先寫下你的問題")
        if len(text) > 200:
            return err("問題請在 200 字以內")
        db().execute("INSERT INTO questions(user_id, text, at) VALUES(?,?,?)",
                     (session["uid"], text, rules.iso(now())))
        return jsonify({"ok": True, "state": me_state()})

    # ---------- 攤位人員 ----------

    UNDO_MINUTES = 10

    def staff_name():
        return (request.headers.get("X-Staff-Name") or "").strip()[:20]

    def need_name(fn):
        """會改資料的操作要帶工作人員的名字（中文要先編碼，所以從 header 解碼）。"""
        @wraps(fn)
        def wrapper(*a, **kw):
            g.staff = urllib.parse.unquote(staff_name())[:20].strip()
            if not g.staff:
                return err("請先輸入你的名字", 400, reason="no_name")
            return fn(*a, **kw)
        return wrapper

    def log_action(action, uid, **detail):
        cur = db().execute(
            "INSERT INTO staff_log(at, staff, action, user_id, detail) VALUES(?,?,?,?,?)",
            (rules.iso(now()), g.staff, action, uid, json.dumps(detail, ensure_ascii=False)))
        return cur.lastrowid

    def by_code(code):
        return db().execute("SELECT * FROM users WHERE code=?",
                            (str(code or "").strip().upper(),)).fetchone()

    def staff_view(u):
        u = rules.get_user(db(), u["id"])
        s = rules.state(db(), u["id"], now(), app.config, content.QUIZ)
        r = s["rewards"]
        give = [k for k in rules.PHYSICAL if r[k] == "ready"]
        if r["prize_gift"] == "ready":
            give.append("prize")
        pz = content.PRIZES.get(r["prize"]) if r["prize"] else None
        return {"name": u["name"], "code": u["code"], "is_friend": s["is_friend"],
                "is_new": s["is_new"], "distance": u["distance"], "arrived": s["arrived"],
                "quiz_done": s["quiz_done"], "rewards": r, "today": s["today"],
                "member": r["onsite"] != "locked",      # 有券（加好友＋選距離）才有現場價
                "give": give, "gift_text": pz.get("gift", "") if pz else ""}

    @app.post("/api/staff/lookup")
    @need_staff
    def staff_lookup():
        u = by_code(body().get("code"))
        if not u:
            return err("找不到這個代碼", 404)
        return jsonify({"ok": True, "user": staff_view(u)})

    @app.post("/api/staff/search")
    @need_staff
    def staff_search():
        """客人手機打不開時，用 LINE 名稱找人。"""
        q = str(body().get("q") or "").strip()
        if len(q) < 1:
            return err("請輸入名稱")
        like = "%" + q.replace("%", "").replace("_", "") + "%"
        rs = db().execute("SELECT name, code, distance, arrived_at FROM users WHERE name LIKE ? "
                          "ORDER BY created_at DESC LIMIT 8", (like,)).fetchall()
        return jsonify({"ok": True, "items": [
            {"name": r["name"], "code": r["code"], "distance": r["distance"],
             "arrived": bool(r["arrived_at"])} for r in rs]})

    @app.post("/api/staff/friend")
    @need_staff
    @need_name
    def staff_friend():
        """FRIEND_CHECK=staff 時，人員看過對方手機上的好友畫面後按下確認。"""
        if app.config["FRIEND_CHECK"] != "staff":
            return err("目前的設定不是由攤位人員確認好友")
        u = by_code(body().get("code"))
        if not u:
            return err("找不到這個代碼", 404)
        db().execute("UPDATE users SET is_friend=1 WHERE id=?", (u["id"],))
        log_id = log_action("friend", u["id"], was_friend=bool(u["is_friend"]))
        return jsonify({"ok": True, "log_id": log_id, "user": staff_view(u)})

    @app.post("/api/staff/checkin")
    @need_staff
    @need_name
    def staff_checkin():
        """到攤位報到。記下到場，並把此刻可以領的東西一次發完：報到軟糖、答題加碼、
        揪朋友的勁速補、抽到的贈品。晚一點才達成的，再掃一次、再按一次即可，不會重複發。"""
        n, conn = now(), db()
        if not rules.event_day(n):
            return err("只能在博覽會期間報到")
        u = by_code(body().get("code"))
        if not u:
            return err("找不到這個代碼", 404)
        if not u["is_friend"]:
            return err("對方還不是好友，請他先加入", 409, reason="not_friend", user=staff_view(u))
        if not u["distance"]:
            return err("請他先在兌換券上選距離", 409, reason="no_distance", user=staff_view(u))
        given, first = [], not u["arrived_at"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE users SET arrived_at=COALESCE(arrived_at, ?) WHERE id=?",
                         (rules.iso(n), u["id"]))
            u = rules.get_user(conn, u["id"])
            claims = rules.claims_of(conn, u["id"])
            status = {
                "gummy": rules.gummy_status(conn, u, claims, n),
                "bonus": rules.bonus_status(conn, u, claims, n, len(content.QUIZ)),
                "crampeace": rules.crampeace_status(conn, u, claims, n),
                "prize": rules.prize_gift_status(conn, u["id"], claims, n),
            }
            for reward in rules.PHYSICAL + ("prize",):
                if status[reward] == "ready":
                    conn.execute("INSERT INTO claims(user_id, reward, day, at) VALUES(?,?,?,?)",
                                 (u["id"], reward, rules.day_key(n), rules.iso(n)))
                    given.append(reward)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        # 這個人到了以後，隊友的勁速補可能也跟著可以領：提示工作人員
        mates = []
        ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE invited_by=?", (u["id"],))]
        if u["invited_by"]:
            ids.append(u["invited_by"])
        for pid in ids:
            p = rules.get_user(conn, pid)
            if rules.crampeace_status(conn, p, rules.claims_of(conn, pid), n) == "ready":
                mates.append({"name": p["name"], "code": p["code"]})
        log_id = log_action("checkin", u["id"], given=given, first=first) if (given or first) else None
        return jsonify({"ok": True, "given": given, "first": first, "log_id": log_id,
                        "mates_ready": mates, "user": staff_view(u)})

    # ---------- 結帳 ----------

    def product_rows(only_active=True):
        q = "SELECT * FROM products" + (" WHERE active=1" if only_active else "") + " ORDER BY sort, name"
        return [dict(r) for r in db().execute(q)]

    def read_cart(conn):
        """回傳 (客人或 None, 是否有券, 抽到的獎, 商品明細)。客人代碼空白＝散客。"""
        b = body()
        code, u = str(b.get("code") or "").strip(), None
        if code:
            u = by_code(code)
            if not u:
                return None, err("找不到這個代碼", 404)
        prods = {p["id"]: p for p in product_rows()}
        items = []
        raw = b.get("items")
        for it in raw if isinstance(raw, list) else []:
            p = prods.get(it.get("id")) if isinstance(it, dict) else None
            qty = it.get("qty") if p else None
            if not p or not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= 50:
                return None, err("商品或數量不正確，請重新點選")
            items.append((p, qty))
        if not items:
            return None, err("請先點選商品")
        member = bool(u) and rules.onsite_status(u, now()) != "locked"
        prize = rules.prize_of(conn, u["id"]) if member else None
        used = rules.special_used(conn, u["id"]) if member else 0
        return (u, member, prize, rules.price_cart(items, prize, member, used)), None

    @app.get("/api/staff/products")
    @need_staff
    def staff_products():
        return jsonify({"ok": True, "items": product_rows()})

    @app.post("/api/staff/quote")
    @need_staff
    def staff_quote():
        """試算，不寫入。"""
        got, bad = read_cart(db())
        if bad:
            return bad
        _u, member, prize, cart = got
        return jsonify({"ok": True, "member": member, "prize": prize, **cart})

    @app.post("/api/staff/sale")
    @need_staff
    @need_name
    def staff_sale():
        """收到錢之後記錄一筆。金額由主機依客人的獎計算；畫面上的金額和主機算的不同就不寫入。"""
        n, conn = now(), db()
        if not rules.event_day(n):
            return err("只能在博覽會期間結帳")
        pay = body().get("pay")
        if pay not in ("cash", "linepay"):
            return err("請選擇現金或 LINE Pay")
        conn.execute("BEGIN IMMEDIATE")
        try:
            got, bad = read_cart(conn)
            if not bad:
                u, member, prize, cart = got
                if body().get("total") != cart["total"]:
                    bad = err("金額有變動，請再確認一次", 409, reason="changed", **cart)
            if bad:
                conn.execute("ROLLBACK")
                return bad
            cur = conn.execute(
                "INSERT INTO sales(at, day, staff, user_id, prize, pay, total, list_total) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (rules.iso(n), rules.day_key(n), g.staff, u["id"] if u else None, prize, pay,
                 cart["total"], cart["list_total"]))
            sale_id = cur.lastrowid
            for ln in cart["lines"]:
                conn.execute(
                    "INSERT INTO sale_items(sale_id, product_id, name, qty, unit_list, amount, "
                    "special_qty, how) VALUES(?,?,?,?,?,?,?,?)",
                    (sale_id, ln["id"], ln["name"], ln["qty"], ln["list"], ln["amount"],
                     ln["special"], ln["how"]))
            first = bool(u) and not u["arrived_at"]
            if u:
                conn.execute("UPDATE users SET arrived_at=COALESCE(arrived_at, ?) WHERE id=?",
                             (rules.iso(n), u["id"]))
            conn.execute(
                "INSERT INTO staff_log(at, staff, action, user_id, detail) VALUES(?,?,?,?,?)",
                (rules.iso(n), g.staff, "sale", u["id"] if u else None,
                 json.dumps({"sale_id": sale_id, "total": cart["total"], "pay": pay,
                             "first": first}, ensure_ascii=False)))
            log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return jsonify({"ok": True, "sale_id": sale_id, "log_id": log_id, "total": cart["total"],
                        "user": staff_view(u) if u else None})

    def log_view(r, n):
        age = (n - rules.parse(r["at"])).total_seconds() / 60
        d = json.loads(r["detail"])
        pay = {"cash": "現金", "linepay": "LINE Pay"}.get(d.get("pay"), "")
        what = {"checkin": "報到" + ("並發放" if d.get("given") else ""),
                "sale": f"結帳 {d.get('total', 0):,} 元（{pay}）", "onsite": "記錄現場優惠一筆",
                "friend": "確認好友"}[r["action"]]
        same = ("SELECT 1 FROM staff_log WHERE user_id=? AND id>? AND undone_at IS NULL"
                if r["user_id"] else "SELECT 1 WHERE ? IS NOT NULL AND ? IS NULL")
        return {"id": r["id"], "at": r["at"][11:16], "name": r["name"] or ("" if r["user_id"] else "散客"),
                "code": r["code"] or "",
                "action": r["action"], "given": d.get("given", []), "what": what,
                "undone": bool(r["undone_at"]),
                # 只有這位客人最新的一筆可以取消，要先取消新的才能取消舊的
                "can_undo": (not r["undone_at"] and age <= UNDO_MINUTES and r["staff"] == g.staff
                             and not db().execute(same, (r["user_id"], r["id"])).fetchone())}

    @app.get("/api/staff/recent")
    @need_staff
    @need_name
    def staff_recent():
        """這位工作人員最近的操作，用來取消按錯的那一筆。"""
        n = now()
        rs = db().execute(
            "SELECT l.*, u.name, u.code FROM staff_log l LEFT JOIN users u ON u.id=l.user_id "
            "WHERE l.staff=? ORDER BY l.id DESC LIMIT 6", (g.staff,)).fetchall()
        return jsonify({"ok": True, "items": [log_view(r, n) for r in rs],
                        "undo_minutes": UNDO_MINUTES})

    @app.post("/api/staff/undo")
    @need_staff
    @need_name
    def staff_undo():
        """取消自己剛才按錯的一筆。限 10 分鐘內，且之後沒有人再對這位客人操作過。"""
        n, conn = now(), db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            r = conn.execute("SELECT * FROM staff_log WHERE id=?", (body().get("log_id"),)).fetchone()
            why = None
            if not r:
                why = "找不到這筆紀錄"
            elif r["undone_at"]:
                why = "這筆已經取消過了"
            elif r["staff"] != g.staff:
                why = f"這筆是 {r['staff']} 操作的，請他本人取消"
            elif (n - rules.parse(r["at"])).total_seconds() > UNDO_MINUTES * 60:
                why = f"超過 {UNDO_MINUTES} 分鐘，不能取消了。請告訴負責人"
            elif r["user_id"] and conn.execute(
                    "SELECT 1 FROM staff_log WHERE user_id=? AND id>? AND undone_at IS NULL",
                    (r["user_id"], r["id"])).fetchone():
                why = "這位客人之後還有別的操作，請先取消比較新的那一筆"
            if not why:
                d, uid = json.loads(r["detail"]), r["user_id"]
                if r["action"] == "checkin":
                    for reward in d.get("given", []):
                        conn.execute("DELETE FROM claims WHERE user_id=? AND reward=?", (uid, reward))
                elif r["action"] == "sale":
                    conn.execute("UPDATE sales SET voided_at=? WHERE id=?",
                                 (rules.iso(n), d.get("sale_id")))
                elif r["action"] == "friend" and not d.get("was_friend"):
                    conn.execute("UPDATE users SET is_friend=0 WHERE id=?", (uid,))
                if d.get("first") and uid:
                    conn.execute("UPDATE users SET arrived_at=NULL WHERE id=?", (uid,))
                conn.execute("UPDATE staff_log SET undone_at=? WHERE id=?", (rules.iso(n), r["id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if why:
            return err(why, 409)
        return jsonify({"ok": True, "user": staff_view(rules.get_user(conn, r["user_id"]))
                        if r["user_id"] else None})

    @app.get("/api/staff/today")
    @need_staff
    def staff_today():
        n, d = now(), rules.event_day(now())
        out = {"day": rules.day_key(n), "is_event_day": bool(d),
               "friend_check": app.config["FRIEND_CHECK"]}
        for r in rules.PHYSICAL:
            out[r] = {"cap": d[r] if d else 0, "left": rules.left_today(db(), r, n)}
        s = db().execute(
            "SELECT COUNT(*) n, COALESCE(SUM(total),0) total, "
            "COALESCE(SUM(CASE WHEN pay='cash' THEN total END),0) cash, "
            "COALESCE(SUM(CASE WHEN pay='linepay' THEN total END),0) linepay "
            "FROM sales WHERE day=? AND voided_at IS NULL", (rules.day_key(n),)).fetchone()
        out["sales"] = dict(s)
        out["rehearsal"] = bool(app.config["REHEARSAL_DAY"])
        return jsonify({"ok": True, **out})

    # ---------- 統計 ----------

    @app.get("/api/admin/stats")
    @need_admin
    def admin_stats():
        conn = db()
        one = lambda q, *a: conn.execute(q, a).fetchone()[0]
        rows = lambda q, *a: [dict(r) for r in conn.execute(q, a)]
        return jsonify({
            "ok": True,
            "users": one("SELECT COUNT(*) FROM users"),
            "friends": one("SELECT COUNT(*) FROM users WHERE is_friend=1"),
            "new_friends": one("SELECT COUNT(*) FROM users WHERE is_friend=1 AND is_new=1"),
            "reserved": one("SELECT COUNT(*) FROM users WHERE distance IS NOT NULL"),
            "arrived": one("SELECT COUNT(*) FROM users WHERE arrived_at IS NOT NULL"),
            "quiz_done": one("SELECT COUNT(*) FROM (SELECT user_id FROM quiz GROUP BY user_id "
                             "HAVING COUNT(*) >= ?)", min(rules.QUIZ_SIZE, len(content.QUIZ))),
            "draws": rows("SELECT prize, COUNT(*) n FROM draws WHERE prize IS NOT NULL "
                         "GROUP BY prize ORDER BY n DESC"),
            "prize_names": {k: v["title"] for k, v in content.PRIZES.items()},
            "sales_n": one("SELECT COUNT(*) FROM sales WHERE voided_at IS NULL"),
            "sales_total": one("SELECT COALESCE(SUM(total),0) FROM sales WHERE voided_at IS NULL"),
            "sales_list_total": one("SELECT COALESCE(SUM(list_total),0) FROM sales WHERE voided_at IS NULL"),
            "buyers": one("SELECT COUNT(DISTINCT user_id) FROM sales WHERE voided_at IS NULL "
                          "AND user_id IS NOT NULL"),
            "walkin_n": one("SELECT COUNT(*) FROM sales WHERE voided_at IS NULL AND user_id IS NULL"),
            "sales_by_day": rows(
                "SELECT day, COUNT(*) n, SUM(total) total, "
                "COALESCE(SUM(CASE WHEN pay='cash' THEN total END),0) cash, "
                "COALESCE(SUM(CASE WHEN pay='linepay' THEN total END),0) linepay "
                "FROM sales WHERE voided_at IS NULL GROUP BY day ORDER BY day"),
            "by_product": rows(
                "SELECT i.name, SUM(i.qty) qty, SUM(i.amount) amount FROM sale_items i "
                "JOIN sales s ON s.id=i.sale_id WHERE s.voided_at IS NULL "
                "GROUP BY i.product_id ORDER BY amount DESC"),
            "by_prize": rows(
                "SELECT d.prize, COUNT(*) drawn, "
                "(SELECT COUNT(*) FROM claims c JOIN draws x ON x.user_id=c.user_id "
                " WHERE c.reward='prize' AND x.prize=d.prize) gifts, "
                "(SELECT COUNT(DISTINCT s.user_id) FROM sales s JOIN draws x ON x.user_id=s.user_id "
                " WHERE s.voided_at IS NULL AND x.prize=d.prize) buyers, "
                "(SELECT COALESCE(SUM(s.total),0) FROM sales s JOIN draws x ON x.user_id=s.user_id "
                " WHERE s.voided_at IS NULL AND x.prize=d.prize) revenue "
                "FROM draws d WHERE d.prize IS NOT NULL GROUP BY d.prize ORDER BY drawn DESC"),
            "gift_quota": [{"prize": k, "cap": cap, "drawn": rules.gift_drawn(conn)[k]}
                           for k, cap in rules.GIFT_QUOTA.items()],
            "sales_by_staff": rows(
                "SELECT staff, COUNT(*) n, SUM(total) total FROM sales WHERE voided_at IS NULL "
                "GROUP BY staff ORDER BY total DESC"),
            "questions_open": one("SELECT COUNT(*) FROM questions WHERE answered_at IS NULL"),
            "by_staff": rows("SELECT staff, COUNT(*) n, SUM(undone_at IS NOT NULL) undone "
                             "FROM staff_log GROUP BY staff ORDER BY n DESC"),
            "plans_sent": one("SELECT COUNT(*) FROM users WHERE plan_sent_at IS NOT NULL"),
            "invited": one("SELECT COUNT(*) FROM users WHERE invited_by IS NOT NULL"),
            "invited_new": one("SELECT COUNT(*) FROM users WHERE invited_by IS NOT NULL AND is_new=1"),
            "questions": one("SELECT COUNT(*) FROM questions"),
            "arrived_by_day": rows("SELECT substr(arrived_at,1,10) day, COUNT(*) n FROM users "
                                   "WHERE arrived_at IS NOT NULL GROUP BY day ORDER BY day"),
            "by_distance": rows("SELECT distance, COUNT(*) n FROM users WHERE distance IS NOT NULL GROUP BY distance"),
            "new_by_day": rows("SELECT substr(created_at,1,10) day, COUNT(*) n FROM users GROUP BY day ORDER BY day"),
            "claims": rows("SELECT day, reward, COUNT(*) n FROM claims GROUP BY day, reward ORDER BY day, reward"),
            "top_inviters": rows(
                "SELECT u.name, u.code, COUNT(*) n FROM users v JOIN users u ON u.id=v.invited_by "
                "GROUP BY u.id ORDER BY n DESC LIMIT 20"),
        })

    def csv_response(name, head, rows_):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(head)
        w.writerows(rows_)
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.get("/api/admin/questions.csv")
    @need_admin
    def admin_questions():
        rs = db().execute(
            "SELECT q.at, u.code, u.name, u.distance, q.text, "
            "CASE WHEN q.answered_at IS NULL THEN '' ELSE '已回覆' END, COALESCE(q.answered_at, '') "
            "FROM questions q JOIN users u ON u.id=q.user_id ORDER BY q.at")
        return csv_response("questions.csv", ["時間", "代碼", "名稱", "距離", "問題", "狀態", "回覆時間"],
                            [list(r) for r in rs])

    @app.get("/api/admin/questions")
    @need_admin
    def admin_question_list():
        rs = db().execute(
            "SELECT q.id, q.at, q.text, q.answered_at, u.code, u.name, u.distance FROM questions q "
            "JOIN users u ON u.id=q.user_id ORDER BY q.answered_at IS NOT NULL, q.at")
        return jsonify({"ok": True, "items": [dict(r) for r in rs]})

    @app.post("/api/admin/question")
    @need_admin
    def admin_question_mark():
        """勾選或取消「已在 LINE 回覆」。"""
        b = body()
        done = rules.iso(now()) if b.get("answered") else None
        cur = db().execute("UPDATE questions SET answered_at=? WHERE id=?", (done, b.get("id")))
        if not cur.rowcount:
            return err("找不到這則提問", 404)
        return jsonify({"ok": True})

    @app.get("/api/admin/participants.csv")
    @need_admin
    def admin_participants():
        """每位參加者一列：活動後看誰來了、誰沒來、抽到什麼、買了幾次。不含 LINE 識別碼。"""
        conn, out = db(), []
        for u in conn.execute("SELECT * FROM users ORDER BY created_at").fetchall():
            uid, claims = u["id"], rules.claims_of(conn, u["id"])
            inviter = rules.get_user(conn, u["invited_by"]) if u["invited_by"] else None
            one = lambda sql: conn.execute(sql, (uid,)).fetchone()[0]
            prize = rules.prize_of(conn, uid)
            out.append([
                u["code"], u["name"], "是" if u["is_friend"] else "否",
                "新好友" if u["is_new"] else "原本的好友",
                content.DIST_NAME.get(u["distance"], ""), u["created_at"][:16].replace("T", " "),
                inviter["code"] if inviter else "",
                one("SELECT COUNT(*) FROM users WHERE invited_by=?"),
                (u["arrived_at"] or "")[:16].replace("T", " "),
                one("SELECT COUNT(*) FROM quiz WHERE user_id=?"),
                content.PRIZES[prize]["title"] if prize else "",
                claims.get("gummy", ""), claims.get("bonus", ""), claims.get("crampeace", ""),
                claims.get("prize", ""),
                one("SELECT COUNT(*) FROM sales WHERE user_id=? AND voided_at IS NULL"),
                one("SELECT COALESCE(SUM(total),0) FROM sales WHERE user_id=? AND voided_at IS NULL"),
                one("SELECT COUNT(*) FROM questions WHERE user_id=?"),
            ])
        return csv_response("participants.csv", [
            "代碼", "LINE 名稱", "目前是好友", "好友類型", "距離", "領券時間", "邀請人代碼", "他帶進幾人",
            "到攤位時間", "答了幾題", "抽到的現場優惠", "軟糖領取日", "加碼軟糖領取日", "勁速補領取日",
            "抽獎贈品領取日", "結帳次數", "消費金額", "提問數"], out)

    @app.get("/api/admin/sales.csv")
    @need_admin
    def admin_sales():
        """銷售明細：一列一項商品。取消的也列出來並註明。"""
        rs = db().execute(
            "SELECT s.at, s.id, s.staff, COALESCE(u.code,''), COALESCE(u.name,'散客'), s.prize, s.pay, "
            "i.name, i.qty, i.unit_list, i.amount, i.how, s.total, s.voided_at "
            "FROM sales s JOIN sale_items i ON i.sale_id=s.id LEFT JOIN users u ON u.id=s.user_id "
            "ORDER BY s.id, i.rowid")
        pay = {"cash": "現金", "linepay": "LINE Pay"}
        out = [[r[0][:16].replace("T", " "), r[1], r[2], r[3], r[4],
                content.PRIZES[r[5]]["title"] if r[5] in content.PRIZES else "", pay.get(r[6], r[6]),
                r[7], r[8], r[9], r[10], r[11], r[12], "已取消" if r[13] else ""] for r in rs]
        return csv_response("sales.csv", [
            "時間", "單號", "工作人員", "客人代碼", "客人名稱", "抽到的獎", "付款方式",
            "商品", "數量", "官網單價", "實收小計", "計價方式", "整單金額", "狀態"], out)

    @app.get("/api/admin/products")
    @need_admin
    def admin_products():
        return jsonify({"ok": True, "items": product_rows(only_active=False)})

    @app.post("/api/admin/product")
    @need_admin
    def admin_product_save():
        """改價、上下架，或新增一項商品。已賣出的紀錄不受影響（明細存的是當時的品名與金額）。"""
        b, conn = body(), db()

        def money(v, optional=False):
            if optional and v in (None, "", 0):
                return None
            if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 100000:
                raise ValueError
            return v
        try:
            name = str(b.get("name") or "").strip()[:40]
            price, onsite, multi = money(b.get("price")), money(b.get("onsite"), True), money(b.get("multi"), True)
            if not name:
                raise ValueError
        except ValueError:
            return err("品名和價格要填，價格請填整數")
        tag = b.get("tag") if b.get("tag") in ("gummy", "flask", "cramp", "noxx") else ""
        pid = str(b.get("id") or "").strip()
        if pid:
            cur = conn.execute(
                "UPDATE products SET name=?, cat=?, tag=?, price=?, onsite=?, multi=?, active=? WHERE id=?",
                (name, str(b.get("cat") or "")[:20], tag, price, onsite, multi,
                 int(bool(b.get("active", True))), pid))
            if not cur.rowcount:
                return err("找不到這項商品", 404)
        else:
            pid = "p" + secrets.token_hex(4)
            top = conn.execute("SELECT COALESCE(MAX(sort),0)+1 FROM products").fetchone()[0]
            conn.execute(
                "INSERT INTO products(id, name, cat, tag, price, onsite, multi, active, sort) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (pid, name, str(b.get("cat") or "")[:20], tag, price, onsite, multi, 1, top))
        return jsonify({"ok": True, "items": product_rows(only_active=False)})

    # ---------- 個資刪除（命令列）----------

    @app.cli.command("delete-user")
    def delete_user():
        """flask delete-user：依 5 碼代碼刪除一位參加者的所有資料。"""
        import click
        code = click.prompt("參加者代碼").strip().upper()
        conn = dbm.connect(app.config["DB_PATH"])
        u = conn.execute("SELECT id, name FROM users WHERE code=?", (code,)).fetchone()
        if not u:
            click.echo("找不到這個代碼")
            return
        if click.confirm(f"確定刪除 {u['name']}（{code}）的所有資料？"):
            conn.execute("UPDATE users SET invited_by=NULL WHERE invited_by=?", (u["id"],))
            conn.execute("DELETE FROM users WHERE id=?", (u["id"],))
            click.echo("已刪除")

    return app


if __name__ == "__main__":
    os.environ.setdefault("DEV_MODE", "1")
    create_app().run(debug=True, port=5057)
