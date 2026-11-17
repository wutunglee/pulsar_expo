"""主要規則的自動化測試。執行：python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
from unittest import mock  # noqa: E402

import line_api  # noqa: E402
import rules  # noqa: E402
from app import create_app  # noqa: E402


def at(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=rules.TZ)


class Base(unittest.TestCase):
    extra_config = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = at("2026-12-05 10:00")
        self.app = create_app({
            "DEV_MODE": True, "DB_PATH": os.path.join(self.tmp.name, "t.db"),
            "STAFF_PIN": "4321", "ADMIN_TOKEN": "adm",
            "COUPON_ABSENT": "MISS100",
            "NOW_FN": lambda: self.clock, **self.extra_config,
        })
        self.clients = {}

    def tearDown(self):
        self.tmp.cleanup()

    def c(self, who):
        if who not in self.clients:
            self.clients[who] = self.app.test_client()
        return self.clients[who]

    def join(self, who, friend=True, ref=None, consent=True, new=True, distance="half"):
        r = self.c(who).post("/api/session", json={
            "dev_user": who, "dev_friend": friend, "dev_new": new, "ref": ref})
        self.assertEqual(r.status_code, 200, r.get_json())
        if consent:
            self.c(who).post("/api/consent")
            if distance:
                self.post(who, "/api/distance", distance=distance)
        return self.me(who)

    def me(self, who):
        return self.c(who).get("/api/me").get_json()["state"]

    def post(self, who, path, **data):
        return self.c(who).post(path, json=data)

    def staff(self, path, pin="4321", name="阿志", **data):
        from urllib.parse import quote
        return self.app.test_client().post(path, json=data, headers={
            "X-Staff-Pin": pin, "X-Staff-Name": quote(name)})

    def staff_get(self, path, name="阿志"):
        from urllib.parse import quote
        return self.app.test_client().get(path, headers={
            "X-Staff-Pin": "4321", "X-Staff-Name": quote(name)}).get_json()

    def checkin(self, who):
        return self.staff("/api/staff/checkin", code=self.me(who)["code"])

    def quiz(self, who):
        for q in self.me(who)["quiz"]:
            self.post(who, "/api/quiz", q=q["id"], answer=True)


class TestVoucher(Base):
    def test_consent_required(self):
        self.join("a", consent=False)
        self.assertEqual(self.post("a", "/api/distance", distance="half").status_code, 403)

    def test_distance_reserves_gummy(self):
        s = self.join("a", distance=None)
        self.assertEqual(s["rewards"]["gummy"], "locked")
        r = self.post("a", "/api/distance", distance="full").get_json()["state"]
        self.assertEqual(r["rewards"]["gummy"], "ready")
        self.assertEqual(self.post("a", "/api/distance", distance="ultra").status_code, 400)

    def test_progress_persists_across_devices(self):
        self.join("a", distance="full")
        self.clients.pop("a")
        self.assertEqual(self.join("a", distance=None)["distance"], "full")

    def test_not_friend_cannot_reserve_or_checkin(self):
        self.join("a", friend=False)
        self.assertEqual(self.me("a")["rewards"]["gummy"], "locked")
        self.clock = at("2026-12-17 13:00")
        r = self.checkin("a")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "not_friend")
        self.assertFalse(self.me("a")["arrived"])

    def test_checkin_only_on_event_days(self):
        self.join("a")
        self.assertEqual(self.checkin("a").status_code, 400)
        self.clock = at("2026-12-17 13:00")
        r = self.checkin("a").get_json()
        self.assertEqual(r["given"], ["gummy"])
        s = self.me("a")
        self.assertTrue(s["arrived"])
        self.assertEqual(s["rewards"]["gummy"], "claimed")

    def test_checkin_needs_distance(self):
        self.join("a", distance=None)
        self.clock = at("2026-12-17 13:00")
        self.assertEqual(self.checkin("a").get_json()["reason"], "no_distance")

    def test_checkin_twice_gives_nothing_more(self):
        self.join("a")
        self.clock = at("2026-12-17 13:00")
        self.checkin("a")
        self.assertEqual(self.checkin("a").get_json()["given"], [])
        self.clock = at("2026-12-18 13:00")
        self.assertEqual(self.checkin("a").get_json()["given"], [])

    def test_question_validation(self):
        self.join("a")
        self.assertEqual(self.post("a", "/api/ask", text=" ").status_code, 400)
        self.assertEqual(self.post("a", "/api/ask", text="x" * 201).status_code, 400)
        self.assertTrue(self.post("a", "/api/ask", text="賽前吃什麼").get_json()["state"]["asked"])


class TestQuiz(Base):
    def test_three_fixed_questions_no_answer_leak(self):
        s = self.join("a")
        self.assertEqual(len(s["quiz"]), 3)
        self.assertTrue(all("explain" not in q and "truth" not in q for q in s["quiz"]))
        self.assertEqual([q["id"] for q in s["quiz"]], [q["id"] for q in self.me("a")["quiz"]])

    def test_bonus_after_all_answered_regardless_of_correctness(self):
        self.join("a")
        qs = self.me("a")["quiz"]
        self.post("a", "/api/quiz", q=qs[0]["id"], answer=True)
        self.assertEqual(self.me("a")["rewards"]["bonus"], "locked")
        for q in qs[1:]:
            self.post("a", "/api/quiz", q=q["id"], answer=True)
        s = self.me("a")
        self.assertTrue(s["quiz_done"])
        self.assertEqual(s["rewards"]["bonus"], "ready")
        self.assertTrue(all("explain" in q for q in s["quiz"]))

    def test_first_answer_counts_and_foreign_question_rejected(self):
        self.join("a")
        q = self.me("a")["quiz"][0]
        first = self.post("a", "/api/quiz", q=q["id"], answer=True).get_json()["state"]["quiz"][0]["correct"]
        again = self.post("a", "/api/quiz", q=q["id"], answer=False).get_json()["state"]["quiz"][0]["correct"]
        self.assertEqual(first, again)
        mine = {x["id"] for x in self.me("a")["quiz"]}
        other = next(i for i in range(6) if i not in mine)
        self.assertEqual(self.post("a", "/api/quiz", q=other, answer=True).status_code, 400)

    def test_bonus_given_at_checkin_or_later(self):
        self.join("a")
        self.quiz("a")
        self.clock = at("2026-12-17 13:00")
        self.assertEqual(self.checkin("a").get_json()["given"], ["gummy", "bonus"])
        self.join("b")
        self.assertEqual(self.checkin("b").get_json()["given"], ["gummy"])
        self.quiz("b")                      # 排隊時才答完
        self.assertEqual(self.checkin("b").get_json()["given"], ["bonus"])


class TestTeam(Base):
    def test_new_friend_pair_both_get_crampeace(self):
        a = self.join("a", new=False)                 # 原本的好友揪人
        self.join("b", ref=a["code"], new=True)       # 被揪的是新好友
        self.assertEqual(self.me("a")["rewards"]["crampeace"], "waiting_partner")
        self.clock = at("2026-12-17 13:00")
        self.assertEqual(self.checkin("a").get_json()["given"], ["gummy"])
        r = self.checkin("b").get_json()
        self.assertEqual(r["given"], ["gummy", "crampeace"])
        self.assertEqual(r["mates_ready"], [{"name": "a", "code": a["code"]}])
        self.assertEqual(self.checkin("a").get_json()["given"], ["crampeace"])

    def test_inviting_existing_friend_does_not_count(self):
        a = self.join("a")
        self.join("b", ref=a["code"], new=False)
        sa, sb = self.me("a"), self.me("b")
        self.assertTrue(sa["has_partner"])
        self.assertFalse(sa["team_qualified"])
        self.assertEqual(sa["rewards"]["crampeace"], "locked")
        self.assertEqual(sb["rewards"]["crampeace"], "locked")
        self.clock = at("2026-12-17 13:00")
        self.checkin("a")
        self.assertEqual(self.checkin("b").get_json()["given"], ["gummy"])

    def test_invitee_must_be_friend(self):
        a = self.join("a")
        self.join("b", ref=a["code"], friend=False)
        self.assertFalse(self.me("a")["team_qualified"])

    def test_no_self_or_mutual_invite(self):
        a = self.join("a")
        self.join("a", ref=a["code"])
        self.assertFalse(self.me("a")["invited"])
        b = self.join("b", ref=a["code"])
        self.clients.pop("a")
        self.join("a", ref=b["code"])
        self.assertFalse(self.me("a")["invited"])

    def test_crampeace_once_even_with_many_invitees(self):
        a = self.join("a")
        self.join("b", ref=a["code"]); self.join("c", ref=a["code"])
        self.clock = at("2026-12-17 13:00")
        for w in ("a", "b", "c"):
            self.checkin(w)
        self.assertEqual(self.checkin("a").get_json()["given"], ["crampeace"])
        self.assertEqual(self.checkin("a").get_json()["given"], [])


class TestCapsAndCoupons(Base):
    def test_daily_cap_then_next_day(self):
        cap = rules.EVENT_DAYS["2026-12-17"]["gummy"]
        self.clock = at("2026-12-17 13:00")
        for i in range(cap):
            self.join(f"u{i}")
            self.assertEqual(self.checkin(f"u{i}").get_json()["given"], ["gummy"])
        self.join("late")
        self.assertEqual(self.me("late")["rewards"]["gummy"], "out_today")
        r = self.checkin("late").get_json()
        self.assertEqual(r["given"], [])
        self.assertTrue(self.me("late")["arrived"])          # 沒拿到軟糖，但到場仍算數
        self.clock = at("2026-12-18 13:00")
        self.assertEqual(self.checkin("late").get_json()["given"], ["gummy"])

    def test_onsite_coupon_visible_early_recorded_each_use(self):
        self.assertEqual(self.join("x", distance=None)["rewards"]["onsite"], "locked")
        a = self.join("a")
        self.assertEqual((a["rewards"]["onsite"], a["rewards"]["prize"]), ("ready", None))
        self.assertEqual(self.staff("/api/staff/onsite", code=a["code"]).status_code, 400)  # 還沒到展期
        self.clock = at("2026-12-17 13:00")
        self.assertEqual(self.staff("/api/staff/onsite", code=a["code"]).get_json()["reason"], "no_prize")
        self.post("a", "/api/draw", pick=2)
        self.assertEqual(self.staff("/api/staff/onsite", code=a["code"]).status_code, 200)
        self.assertEqual(self.staff("/api/staff/onsite", code=a["code"]).status_code, 200)  # 不限次數
        s = self.me("a")
        self.assertEqual((s["rewards"]["onsite"], s["rewards"]["onsite_uses"]), ("ready", 2))
        self.assertTrue(s["arrived"])                       # 結帳也算到場
        d = self.app.test_client().get("/api/admin/stats?token=adm").get_json()
        self.assertEqual((d["onsite_uses"], d["onsite_buyers"]), (2, 1))
        self.assertEqual(self.staff("/api/staff/onsite", code=self.me("x")["code"]).status_code, 409)
        self.assertEqual(self.checkin("a").get_json()["given"], ["gummy"])   # 軟糖照樣可以領
        self.join("b")
        self.clock = at("2026-12-20 09:00")
        self.assertEqual(self.me("b")["rewards"]["onsite"], "ended")

    def test_draw_deals_first_then_pick_reveals_real_hand(self):
        self.assertEqual(self.post("x", "/api/draw", pick=0).status_code, 401)
        self.join("n", distance=None)
        self.assertEqual(self.post("n", "/api/draw", pick=0).status_code, 400)   # 還沒選距離
        a = self.join("a")["rewards"]
        self.assertEqual((a["cards"], a["prize"]), (8, None))
        self.assertNotIn("hand", a)                                         # 選之前不給牌面
        conn = __import__("db").connect(self.app.config["DB_PATH"])
        stored = conn.execute("SELECT hand FROM draws WHERE user_id='dev:a'").fetchone()["hand"].split(",")
        self.assertEqual(sorted(stored), sorted(["p88"] * 4 + ["gift"] * 3 + ["half"]))
        for bad in (8, -1, "3", None, True):
            self.assertEqual(self.post("a", "/api/draw", pick=bad).status_code, 400, bad)
        r = self.post("a", "/api/draw", pick=5).get_json()["state"]["rewards"]
        self.assertEqual((r["hand"], r["pick"], r["prize"]), (stored, 5, stored[5]))
        again = self.post("a", "/api/draw", pick=0).get_json()["state"]["rewards"]   # 重選不會變
        self.assertEqual((again["pick"], again["prize"]), (5, stored[5]))
        cfg = json.dumps(self.c("a").get("/api/config").get_json())
        self.assertNotIn("DECK", cfg)

    def test_deck_distribution(self):
        conn = __import__("db").connect(self.app.config["DB_PATH"])
        for i in range(800):
            conn.execute("INSERT INTO users(id, name, code, created_at) VALUES(?,?,?,?)",
                         (f"s{i}", "", f"S{i:04d}", "x"))
            rules.deal(conn, f"s{i}", self.clock)
            rules.pick_card(conn, f"s{i}", i % 8, self.clock)
        got = dict(conn.execute("SELECT prize, COUNT(*) FROM draws GROUP BY prize").fetchall())
        self.assertTrue(340 < got["p88"] < 460 and 240 < got["gift"] < 360 and 60 < got["half"] < 140, got)

    def test_absent_coupon_only_after_event_for_no_shows(self):
        self.join("came"); self.join("missed"); self.join("nofriend", friend=False)
        self.assertFalse(self.me("missed")["rewards"]["absent"])
        self.clock = at("2026-12-17 13:00")
        self.checkin("came")
        self.clock = at("2026-12-20 09:00")
        self.assertEqual(self.me("missed")["rewards"]["absent_code"], "MISS100")
        self.assertFalse(self.me("came")["rewards"]["absent"])
        self.assertFalse(self.me("nofriend")["rewards"]["absent"])
        self.assertEqual(self.me("missed")["rewards"]["gummy"], "ended")


class TestUndoAndStaffName(Base):
    def test_name_required_for_actions_not_lookup(self):
        a = self.join("a")
        self.clock = at("2026-12-17 13:00")
        r = self.staff("/api/staff/checkin", name="", code=a["code"])
        self.assertEqual((r.status_code, r.get_json()["reason"]), (400, "no_name"))
        self.assertEqual(self.staff("/api/staff/lookup", name="", code=a["code"]).status_code, 200)
        self.assertFalse(self.me("a")["arrived"])

    def test_undo_checkin_restores_everything(self):
        a = self.join("a")
        self.quiz("a")
        self.clock = at("2026-12-17 13:00")
        r = self.staff("/api/staff/checkin", name="小美", code=a["code"]).get_json()
        self.assertEqual(r["given"], ["gummy", "bonus"])
        left = self.app.test_client().get("/api/staff/today", headers={"X-Staff-Pin": "4321"}).get_json()
        self.assertEqual(left["gummy"]["left"], 149)
        recent = self.staff_get("/api/staff/recent", name="小美")["items"]
        self.assertEqual((recent[0]["id"], recent[0]["can_undo"]), (r["log_id"], True))
        self.assertEqual(self.staff("/api/staff/undo", name="阿明", log_id=r["log_id"]).status_code, 409)   # 別人不能取消
        self.assertEqual(self.staff("/api/staff/undo", name="小美", log_id=r["log_id"]).status_code, 200)
        s = self.me("a")
        self.assertEqual((s["arrived"], s["rewards"]["gummy"], s["rewards"]["bonus"]), (False, "ready", "ready"))
        left = self.app.test_client().get("/api/staff/today", headers={"X-Staff-Pin": "4321"}).get_json()
        self.assertEqual(left["gummy"]["left"], 150)                                              # 份數還回去
        self.assertEqual(self.staff("/api/staff/undo", name="小美", log_id=r["log_id"]).status_code, 409)   # 不能取消兩次
        self.assertEqual(self.staff("/api/staff/checkin", name="小美", code=a["code"]).get_json()["given"],
                         ["gummy", "bonus"])                                                      # 可以重新報到

    def test_undo_time_limit_and_order(self):
        a = self.join("a")
        self.post("a", "/api/draw", pick=0)
        self.clock = at("2026-12-17 13:00")
        c1 = self.staff("/api/staff/checkin", name="小美", code=a["code"]).get_json()["log_id"]
        o1 = self.staff("/api/staff/onsite", name="小美", code=a["code"]).get_json()["log_id"]
        self.assertEqual(self.staff("/api/staff/undo", name="小美", log_id=c1).status_code, 409)   # 後面還有一筆
        flags = {i["id"]: i["can_undo"] for i in self.staff_get("/api/staff/recent", name="小美")["items"]}
        self.assertEqual((flags[c1], flags[o1]), (False, True))                 # 畫面只讓最新那筆可取消
        self.assertEqual(self.staff("/api/staff/undo", name="小美", log_id=o1).status_code, 200)
        self.assertEqual(self.me("a")["rewards"]["onsite_uses"], 0)
        self.assertTrue(self.me("a")["arrived"])                         # 到場是報到那筆記的，不受影響
        self.clock = at("2026-12-17 13:11")
        self.assertEqual(self.staff("/api/staff/undo", name="小美", log_id=c1).status_code, 409)   # 超過 10 分鐘

    def test_undo_onsite_that_marked_arrival(self):
        a = self.join("a")
        self.post("a", "/api/draw", pick=3)
        self.clock = at("2026-12-17 13:00")
        o = self.staff("/api/staff/onsite", name="小美", code=a["code"]).get_json()["log_id"]
        self.assertTrue(self.me("a")["arrived"])
        self.staff("/api/staff/undo", name="小美", log_id=o)
        self.assertFalse(self.me("a")["arrived"])

    def test_second_checkin_with_nothing_new_is_not_logged(self):
        a = self.join("a")
        self.clock = at("2026-12-17 13:00")
        self.staff("/api/staff/checkin", name="小美", code=a["code"])
        self.assertIsNone(self.staff("/api/staff/checkin", name="小美", code=a["code"]).get_json()["log_id"])
        self.assertEqual(len(self.staff_get("/api/staff/recent", name="小美")["items"]), 1)


class TestAdminExports(Base):
    def test_participants_csv(self):
        a = self.join("a", new=False)
        self.join("b", ref=a["code"], distance="full")
        self.post("b", "/api/draw", pick=1)
        self.clock = at("2026-12-17 13:00")
        self.checkin("b")
        cl = self.app.test_client()
        self.assertEqual(cl.get("/api/admin/participants.csv?token=x").status_code, 401)
        text = cl.get("/api/admin/participants.csv?token=adm").get_data(as_text=True)
        lines = text.lstrip("﻿").strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("原本的好友", lines[1])
        row_b = lines[2].split(",")
        self.assertEqual((row_b[3], row_b[4], row_b[6], row_b[8][:10], row_b[11]),
                         ("新好友", "全馬", a["code"], "2026-12-17", "2026-12-17"))
        self.assertNotIn("dev:b", text)                       # 不含識別碼

    def test_question_reply_status(self):
        self.join("a")
        self.post("a", "/api/ask", text="賽前吃什麼")
        self.post("a", "/api/ask", text="抽筋怎麼辦")
        cl = self.app.test_client()
        items = cl.get("/api/admin/questions?token=adm").get_json()["items"]
        self.assertEqual([i["answered_at"] for i in items], [None, None])
        self.assertEqual(cl.post("/api/admin/question?token=x", json={"id": items[0]["id"], "answered": True}).status_code, 401)
        cl.post("/api/admin/question?token=adm", json={"id": items[0]["id"], "answered": True})
        d = cl.get("/api/admin/stats?token=adm").get_json()
        self.assertEqual((d["questions"], d["questions_open"]), (2, 1))
        items = cl.get("/api/admin/questions?token=adm").get_json()["items"]
        self.assertIsNone(items[0]["answered_at"])            # 沒回的排前面
        self.assertIn("已回覆", cl.get("/api/admin/questions.csv?token=adm").get_data(as_text=True))
        cl.post("/api/admin/question?token=adm", json={"id": items[1]["id"], "answered": False})
        self.assertEqual(cl.get("/api/admin/stats?token=adm").get_json()["questions_open"], 2)


class TestStaffAndAdmin(Base):
    def test_pages_render(self):
        for path in ("/", "/staff", "/admin", "/healthz"):
            self.assertEqual(self.app.test_client().get(path).status_code, 200, path)

    def test_pin_required(self):
        a = self.join("a")
        self.assertEqual(self.staff("/api/staff/lookup", pin="0000", code=a["code"]).status_code, 401)
        r = self.staff("/api/staff/lookup", code=a["code"].lower()).get_json()["user"]
        self.assertEqual((r["distance"], r["arrived"]), ("half", False))
        self.assertEqual(self.staff("/api/staff/lookup", code="ZZZZZ").status_code, 404)

    def test_today_counts(self):
        self.join("a")
        self.clock = at("2026-12-19 11:00")
        self.checkin("a")
        r = self.app.test_client().get("/api/staff/today", headers={"X-Staff-Pin": "4321"}).get_json()
        self.assertEqual(r["gummy"], {"cap": 300, "left": 299})
        self.assertEqual(r["bonus"]["cap"], 300)

    def test_admin_stats(self):
        a = self.join("a", new=False)
        self.join("b", ref=a["code"])
        self.quiz("b")
        self.clock = at("2026-12-17 13:00")
        self.checkin("b")
        cl = self.app.test_client()
        self.assertEqual(cl.get("/api/admin/stats?token=x").status_code, 401)
        d = cl.get("/api/admin/stats?token=adm").get_json()
        self.assertEqual((d["users"], d["new_friends"], d["arrived"], d["quiz_done"], d["invited_new"]),
                         (2, 1, 1, 1, 1))
        self.assertEqual(cl.get("/api/admin/questions.csv?token=adm").status_code, 200)


class TestOAIdentity(Base):
    """官方帳號 webhook 認人：加好友 → 回專屬連結 → 點開就是自己的卡。"""
    extra_config = {"IDENTITY": "oa", "FRIEND_CHECK": "oa", "LINE_CHANNEL_ID": "123",
                    "LINE_CHANNEL_SECRET": "s3cret", "PUBLIC_URL": "https://expo.example"}

    def setUp(self):
        super().setUp()
        self.sent = []
        self.pushed = []
        for name, fake in (("push", lambda tok, uid, text: self.pushed.append((uid, text))),
                           ("channel_token", lambda *a: "tok"),
                           ("get_profile", lambda tok, uid: "名" + uid[-1]),
                           ("reply", lambda tok, rt, text: self.sent.append(text))):
            p = mock.patch.object(line_api, name, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)

    def hook(self, *events, secret="s3cret"):
        raw = json.dumps({"destination": "x", "events": list(events)}).encode()
        sig = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
        return self.app.test_client().post("/line/webhook", data=raw, headers={
            "X-Line-Signature": sig, "Content-Type": "application/json"})

    def ev(self, kind, uid, text=None):
        e = {"type": kind, "source": {"type": "user", "userId": uid}, "replyToken": "rt"}
        if text is not None:
            e["message"] = {"type": "text", "text": text}
        return e

    def link_path(self):
        url = [w for w in self.sent[-1].split() if w.startswith("https://")][0]
        self.assertTrue(url.startswith("https://expo.example/t/"))
        return url[len("https://expo.example"):]

    def open(self, who, uid, ref=None):
        if ref:
            self.c(who).get("/?ref=" + ref)
        self.hook(self.ev("follow", uid))
        r = self.c(who).get(self.link_path())
        self.assertEqual(r.status_code, 302)
        self.c(who).post("/api/consent")
        return self.me(who)

    def test_bad_signature_rejected(self):
        self.assertEqual(self.hook(self.ev("follow", "U1"), secret="wrong").status_code, 401)
        self.assertEqual(self.sent, [])

    def test_empty_events_ok(self):
        self.assertEqual(self.hook().status_code, 200)   # 後台的「Verify」按鈕

    def test_no_identity_without_link(self):
        self.assertEqual(self.c("x").get("/api/me").status_code, 401)
        self.assertEqual(self.c("x").post("/api/session", json={}).status_code, 401)

    def test_follow_gives_personal_card(self):
        s = self.open("a", "U1")
        self.assertTrue(s["is_friend"])
        self.assertEqual(s["name"], "名1")

    def test_keyword_for_existing_friend_and_other_text_ignored(self):
        self.hook(self.ev("message", "U2", "請問運費多少"))
        self.assertEqual(self.sent, [])
        self.hook(self.ev("message", "U2", " 任務卡 "))
        self.assertEqual(len(self.sent), 1)
        self.c("b").get(self.link_path())
        self.assertTrue(self.me("b")["is_friend"])
        self.assertFalse(self.me("b")["is_new"])            # 原本就是好友

    def test_same_user_same_card(self):
        a = self.open("a", "U1")
        self.post("a", "/api/distance", distance="half")
        self.hook(self.ev("message", "U1", "任務卡"))
        self.c("a2").get(self.link_path())
        self.assertEqual(self.me("a2")["code"], a["code"])
        self.assertEqual(self.me("a2")["distance"], "half")
        self.assertTrue(self.me("a2")["is_new"])            # 關鍵字不會把新好友改成舊好友

    def test_unfollow_then_follow(self):
        self.open("a", "U1")
        self.hook(self.ev("unfollow", "U1"))
        self.assertFalse(self.me("a")["is_friend"])
        self.hook(self.ev("follow", "U1"))
        self.assertTrue(self.me("a")["is_friend"])

    def test_tampered_link(self):
        self.open("a", "U1")
        r = self.c("z").get(self.link_path()[:-2] + "xx")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c("z").get("/api/me").status_code, 401)

    def test_invite_carried_by_cookie(self):
        a = self.open("a", "U1")
        self.open("b", "U2", ref=a["code"])
        self.assertTrue(self.me("a")["team_qualified"])     # b 是新加入的好友
        self.assertTrue(self.me("b")["is_new"])

    def test_invite_by_manual_code(self):
        a = self.open("a", "U1")
        self.open("b", "U2")
        self.assertEqual(self.post("b", "/api/ref", code="ZZZZZ").status_code, 400)
        self.assertEqual(self.post("b", "/api/ref", code=a["code"].lower()).status_code, 200)
        self.assertTrue(self.me("a")["team_qualified"])
        self.assertEqual(self.post("b", "/api/ref", code=a["code"]).status_code, 400)

    def test_plan_pushed_once_and_keyword_replies_free(self):
        self.open("a", "U1")
        self.hook(self.ev("message", "U1", "補給表"))
        self.assertIn("還沒選距離", self.sent[-1])
        self.post("a", "/api/distance", distance="full")
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.pushed[0][0], "U1")
        self.assertIn("全馬補給時間表", self.pushed[0][1])
        self.assertTrue(self.me("a")["plan_sent"])
        self.post("a", "/api/distance", distance="half")       # 改距離不再主動傳
        self.assertEqual(len(self.pushed), 1)
        self.hook(self.ev("message", "U1", "補給表"))
        self.assertIn("半馬補給時間表", self.sent[-1])

    def test_plan_push_failure_does_not_block(self):
        self.open("a", "U1")
        with mock.patch.object(line_api, "push", side_effect=line_api.LineError("x")):
            self.assertEqual(self.post("a", "/api/distance", distance="full").status_code, 200)
        self.assertFalse(self.me("a")["plan_sent"])

    def test_reply_failure_still_registers(self):
        with mock.patch.object(line_api, "reply", side_effect=line_api.LineError("x")):
            self.assertEqual(self.hook(self.ev("follow", "U9")).status_code, 200)
        self.hook(self.ev("message", "U9", "任務卡"))
        self.c("n").get(self.link_path())
        self.assertTrue(self.me("n")["is_friend"])


class TestStaffFriendCheck(Base):
    extra_config = {"FRIEND_CHECK": "staff"}

    def test_staff_confirms_friend(self):
        a = self.join("a", friend=False)
        self.assertEqual(self.me("a")["rewards"]["gummy"], "locked")
        r = self.staff("/api/staff/friend", code=a["code"])
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["user"]["is_friend"])
        self.assertEqual(self.me("a")["rewards"]["gummy"], "ready")

    def test_staff_friend_needs_pin(self):
        a = self.join("a", friend=False)
        self.assertEqual(self.staff("/api/staff/friend", pin="0000", code=a["code"]).status_code, 401)


class TestFriendCheckModes(Base):
    def test_staff_friend_disabled_in_other_modes(self):
        a = self.join("a", friend=False)
        self.assertEqual(self.staff("/api/staff/friend", code=a["code"]).status_code, 400)

    def test_bad_combination_refused(self):
        with self.assertRaises(RuntimeError):
            create_app({"DEV_MODE": True, "DB_PATH": os.path.join(self.tmp.name, "x.db"),
                        "IDENTITY": "oa", "FRIEND_CHECK": "line"})


if __name__ == "__main__":
    unittest.main()
