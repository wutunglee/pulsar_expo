"""PulsarPump 博覽會補給站任務卡。Flask＋SQLite。"""
import csv
import hmac
import io
import os
import secrets
import time
from datetime import timedelta
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
        # 選好距離時主動把補給時間表傳到他的 LINE（每人一則，計入每月訊息則數）
        PUSH_PLAN=env("PUSH_PLAN", "1") == "1",
        PUBLIC_URL=env("PUBLIC_URL", "").rstrip("/"),
        STAFF_PIN=env("STAFF_PIN", ""),
        ADMIN_TOKEN=env("ADMIN_TOKEN", ""),
        ONSITE_OFFER=env("ONSITE_OFFER", "全品項 88 折"),   # 現場優惠券上的文字
        COUPON_ABSENT=env("COUPON_ABSENT", ""),   # 沒到攤位的人，活動後看到的官網折扣碼
        COUPON_ABSENT_TEXT=env("COUPON_ABSENT_TEXT", "滿 2,000 元折 250 元，2027/1/19 前有效"),
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

    dbm.init(app.config["DB_PATH"])
    card_signer = URLSafeSerializer(app.config["SECRET_KEY"], salt="card-link")
    pin_fails = {}  # ip -> (次數, 解鎖時間)

    def now():
        fn = app.config["NOW_FN"]
        return fn() if fn else rules.now()

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
        return rules.state(db(), session["uid"], now(), app.config, content.QUIZ)

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
            "dev": c["DEV_MODE"], "oa_id": c["LINE_OA_ID"],
            "identity": c["IDENTITY"], "friend_check": c["FRIEND_CHECK"],
            "keyword": c["LINE_KEYWORD"],
            "has_pending_ref": bool(request.cookies.get("pending_ref")),
            "event": content.EVENT, "plans": content.PLANS,
            "quiz_after": content.QUIZ_AFTER,
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
            return err("邀請碼不對，請再向朋友確認一次")
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

    def by_code(code):
        return db().execute("SELECT * FROM users WHERE code=?",
                            (str(code or "").strip().upper(),)).fetchone()

    def staff_view(u):
        u = rules.get_user(db(), u["id"])
        s = me = rules.state(db(), u["id"], now(), app.config, content.QUIZ)
        return {"name": u["name"], "code": u["code"], "is_friend": s["is_friend"],
                "is_new": s["is_new"], "distance": u["distance"], "arrived": s["arrived"],
                "quiz_done": me["quiz_done"], "rewards": s["rewards"], "today": s["today"]}

    @app.post("/api/staff/lookup")
    @need_staff
    def staff_lookup():
        u = by_code(body().get("code"))
        if not u:
            return err("找不到這個代碼", 404)
        return jsonify({"ok": True, "user": staff_view(u)})

    @app.post("/api/staff/friend")
    @need_staff
    def staff_friend():
        """FRIEND_CHECK=staff 時，人員看過對方手機上的好友畫面後按下確認。"""
        if app.config["FRIEND_CHECK"] != "staff":
            return err("目前的設定不是由攤位人員確認好友")
        u = by_code(body().get("code"))
        if not u:
            return err("找不到這個代碼", 404)
        db().execute("UPDATE users SET is_friend=1 WHERE id=?", (u["id"],))
        return jsonify({"ok": True, "user": staff_view(u)})

    @app.post("/api/staff/checkin")
    @need_staff
    def staff_checkin():
        """到攤位報到。記下到場，並把此刻可以領的實體贈品一次發完。
        答題加碼、勁速補若晚一點才達成，再查一次、再按一次即可，不會重複發。"""
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
        given = []
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
            }
            for reward in rules.PHYSICAL:
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
        return jsonify({"ok": True, "given": given, "mates_ready": mates, "user": staff_view(u)})

    @app.post("/api/staff/onsite")
    @need_staff
    def staff_onsite():
        """客人用現場優惠券結帳時記錄一筆。不限次數。會順便記下到場。"""
        n, conn = now(), db()
        if not rules.event_day(n):
            return err("現場優惠券只能在博覽會期間使用")
        u = by_code(body().get("code"))
        if not u:
            return err("找不到這個代碼", 404)
        if rules.onsite_status(u, n) != "ready":
            return err("對方還沒加好友或還沒選距離", 409, reason="locked", user=staff_view(u))
        conn.execute("INSERT INTO onsite_uses(user_id, day, at) VALUES(?,?,?)",
                     (u["id"], rules.day_key(n), rules.iso(n)))
        conn.execute("UPDATE users SET arrived_at=COALESCE(arrived_at, ?) WHERE id=?",
                     (rules.iso(n), u["id"]))
        return jsonify({"ok": True, "user": staff_view(u)})

    @app.get("/api/staff/today")
    @need_staff
    def staff_today():
        n, d = now(), rules.event_day(now())
        out = {"day": rules.day_key(n), "is_event_day": bool(d),
               "friend_check": app.config["FRIEND_CHECK"]}
        for r in rules.PHYSICAL:
            out[r] = {"cap": d[r] if d else 0, "left": rules.left_today(db(), r, n)}
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
            "onsite_uses": one("SELECT COUNT(*) FROM onsite_uses"),
            "onsite_buyers": one("SELECT COUNT(DISTINCT user_id) FROM onsite_uses"),
            "onsite_by_day": rows("SELECT day, COUNT(*) n, COUNT(DISTINCT user_id) buyers "
                                  "FROM onsite_uses GROUP BY day ORDER BY day"),
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

    @app.get("/api/admin/questions.csv")
    @need_admin
    def admin_questions():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["時間", "代碼", "名稱", "距離", "問題"])
        for r in db().execute(
                "SELECT q.at, u.code, u.name, u.distance, q.text FROM questions q "
                "JOIN users u ON u.id=q.user_id ORDER BY q.at"):
            w.writerow(list(r))
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=questions.csv"})

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
