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
            "DAY_CODES": {"2026-12-17": "THU17", "2026-12-18": "FRI18", "2026-12-19": "SAT19"},
            "COUPON_5": "FIVE100", "COUPON_FALLBACK": "BACK100",
            "NOW_FN": lambda: self.clock, **self.extra_config,
        })
        self.clients = {}

    def tearDown(self):
        self.tmp.cleanup()

    def c(self, who):
        if who not in self.clients:
            self.clients[who] = self.app.test_client()
        return self.clients[who]

    def join(self, who, friend=True, ref=None, consent=True):
        r = self.c(who).post("/api/session", json={"dev_user": who, "dev_friend": friend, "ref": ref})
        self.assertEqual(r.status_code, 200, r.get_json())
        if consent:
            self.c(who).post("/api/consent")
        return self.me(who)

    def me(self, who):
        return self.c(who).get("/api/me").get_json()["state"]

    def post(self, who, path, **data):
        return self.c(who).post(path, json=data)

    def staff(self, path, pin="4321", **data):
        return self.app.test_client().post(path, json=data, headers={"X-Staff-Pin": pin})

    def online3(self, who):
        """做完第 1、2 站，再加上第 5 站，共 3 站。"""
        self.post(who, "/api/station/1", distance="half")
        self.post(who, "/api/station/2", answer=False)
        self.post(who, "/api/station/5", text="賽前一晚吃什麼？")


class TestFlow(Base):
    def test_consent_required(self):
        self.join("a", consent=False)
        r = self.post("a", "/api/station/1", distance="half")
        self.assertEqual(r.status_code, 403)

    def test_progress_persists_across_sessions(self):
        self.join("a")
        self.post("a", "/api/station/1", distance="full")
        self.clients.pop("a")          # 換一台裝置
        s = self.join("a")
        self.assertTrue(s["stations"]["1"])
        self.assertEqual(s["distance"], "full")

    def test_myth_answer(self):
        self.join("a")
        r = self.post("a", "/api/station/2", answer=True).get_json()
        self.assertFalse(r["correct"])
        self.assertTrue(r["state"]["stations"]["2"])

    def test_station4_only_on_event_day_with_right_code(self):
        self.join("a")
        self.assertEqual(self.post("a", "/api/station/4", code="THU17").status_code, 400)
        self.clock = at("2026-12-17 13:00")
        self.assertEqual(self.post("a", "/api/station/4", code="FRI18").status_code, 400)
        r = self.post("a", "/api/station/4", code="thu17")
        self.assertTrue(r.get_json()["state"]["stations"]["4"])

    def test_station4_attempt_limit(self):
        self.join("a")
        self.clock = at("2026-12-17 13:00")
        for _ in range(rules.MAX_CODE_ATTEMPTS):
            self.post("a", "/api/station/4", code="WRONG")
        self.assertEqual(self.post("a", "/api/station/4", code="THU17").status_code, 429)

    def test_question_validation(self):
        self.join("a")
        self.assertEqual(self.post("a", "/api/station/5", text=" ").status_code, 400)
        self.assertEqual(self.post("a", "/api/station/5", text="x" * 201).status_code, 400)


class TestTeam(Base):
    def test_station3_completes_for_both_when_invitee_qualifies(self):
        a = self.join("a")
        self.join("b", ref=a["code"])
        self.assertFalse(self.me("a")["stations"]["3"])
        self.post("b", "/api/station/1", distance="half")
        self.assertTrue(self.me("a")["stations"]["3"])
        self.assertTrue(self.me("b")["stations"]["3"])

    def test_non_friend_invitee_does_not_count_until_friend(self):
        a = self.join("a")
        self.join("b", friend=False, ref=a["code"])
        self.post("b", "/api/station/1", distance="half")
        self.assertFalse(self.me("a")["stations"]["3"])
        self.join("b", friend=True)
        self.assertTrue(self.me("a")["stations"]["3"])

    def test_cannot_invite_self_or_form_cycle(self):
        a = self.join("a")
        self.join("a", ref=a["code"])
        self.post("a", "/api/station/1", distance="half")
        self.assertFalse(self.me("a")["stations"]["3"])
        b = self.join("b", ref=a["code"])
        self.join("a", ref=b["code"])  # a 已是 b 的邀請人，不可反向
        with self.app.app_context():
            import db as dbm
            conn = dbm.connect(self.app.config["DB_PATH"])
            self.assertIsNone(conn.execute(
                "SELECT invited_by FROM users WHERE id='dev:a'").fetchone()[0])


class TestRewards(Base):
    def test_gummy_needs_friend_and_three_stations_and_event_day(self):
        a = self.join("a", friend=False)
        self.online3("a")
        self.assertEqual(self.me("a")["rewards"]["gummy"], "locked")
        a = self.join("a", friend=True)
        self.assertEqual(a["rewards"]["gummy"], "ready")
        r = self.staff("/api/staff/claim", code=a["code"], reward="gummy")
        self.assertEqual(r.status_code, 400)  # 還沒到展期
        self.clock = at("2026-12-17 13:00")
        r = self.staff("/api/staff/claim", code=a["code"], reward="gummy")
        self.assertEqual(r.status_code, 200)
        r = self.staff("/api/staff/claim", code=a["code"], reward="gummy")
        self.assertEqual(r.status_code, 409)  # 不能領第二次
        self.clock = at("2026-12-18 13:00")
        r = self.staff("/api/staff/claim", code=a["code"], reward="gummy")
        self.assertEqual(r.status_code, 409)  # 隔天也不行

    def test_staff_pin_required(self):
        a = self.join("a")
        self.assertEqual(self.staff("/api/staff/lookup", pin="0000", code=a["code"]).status_code, 401)

    def test_daily_cap_then_fallback_coupon(self):
        rules.EVENT_DAYS["2026-12-17"]["gummy"] = 2
        try:
            self.clock = at("2026-12-17 13:00")
            codes = []
            for who in ("a", "b", "c"):
                s = self.join(who)
                self.online3(who)
                codes.append(s["code"])
            self.assertEqual(self.staff("/api/staff/claim", code=codes[0], reward="gummy").status_code, 200)
            self.assertEqual(self.staff("/api/staff/claim", code=codes[1], reward="gummy").status_code, 200)
            r = self.staff("/api/staff/claim", code=codes[2], reward="gummy")
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.get_json()["reason"], "out_today")
            s = self.me("c")
            self.assertEqual(s["rewards"]["fallback"], "available")
            s = self.post("c", "/api/reward/fallback").get_json()["state"]
            self.assertEqual(s["rewards"]["fallback_code"], "BACK100")
            self.assertEqual(s["rewards"]["gummy"], "switched")
            # 改領折扣券後，隔天份數回補也不能再領軟糖
            self.clock = at("2026-12-18 13:00")
            self.assertEqual(self.staff("/api/staff/claim", code=codes[2], reward="gummy").status_code, 409)
            # 已領軟糖的人不能再拿備案折扣券
            self.assertEqual(self.post("a", "/api/reward/fallback").status_code, 400)
        finally:
            rules.EVENT_DAYS["2026-12-17"]["gummy"] = 150

    def test_fallback_after_event_for_online_only(self):
        self.join("a")
        self.online3("a")
        self.assertEqual(self.me("a")["rewards"]["fallback"], "none")
        self.clock = at("2026-12-19 18:01")
        self.assertEqual(self.me("a")["rewards"]["fallback"], "available")
        self.assertEqual(self.me("a")["rewards"]["gummy"], "ended")

    def test_crampeace_needs_both_at_booth(self):
        a = self.join("a")
        b = self.join("b", ref=a["code"])
        self.post("a", "/api/station/1", distance="half")
        self.post("b", "/api/station/1", distance="half")
        self.clock = at("2026-12-19 11:00")
        self.post("a", "/api/station/4", code="SAT19")
        self.assertEqual(self.me("a")["rewards"]["crampeace"], "waiting_partner")
        r = self.staff("/api/staff/claim", code=a["code"], reward="crampeace")
        self.assertEqual(r.status_code, 409)
        self.post("b", "/api/station/4", code="SAT19")
        for who in (a, b):
            r = self.staff("/api/staff/claim", code=who["code"], reward="crampeace")
            self.assertEqual(r.status_code, 200)

    def test_coupon5_when_all_done(self):
        a = self.join("a")
        self.join("b", ref=a["code"])
        self.post("b", "/api/station/1", distance="half")
        self.online3("a")
        self.assertFalse(self.me("a")["rewards"]["coupon5"])
        self.clock = at("2026-12-18 13:00")
        s = self.post("a", "/api/station/4", code="FRI18").get_json()["state"]
        self.assertTrue(s["rewards"]["coupon5"])
        self.assertEqual(s["rewards"]["coupon5_code"], "FIVE100")

    def test_staff_can_mark_station_4_and_5(self):
        a = self.join("a")
        self.clock = at("2026-12-17 13:00")
        self.staff("/api/staff/mark", code=a["code"], station=4)
        self.staff("/api/staff/mark", code=a["code"], station=5)
        s = self.me("a")
        self.assertTrue(s["stations"]["4"] and s["stations"]["5"])
        self.assertEqual(self.staff("/api/staff/mark", code=a["code"], station=1).status_code, 400)


class TestAdmin(Base):
    def test_stats_and_csv(self):
        self.join("a")
        self.online3("a")
        c = self.app.test_client()
        self.assertEqual(c.get("/api/admin/stats").status_code, 401)
        s = c.get("/api/admin/stats?token=adm").get_json()
        self.assertEqual(s["users"], 1)
        self.assertEqual(s["questions"], 1)
        csv_text = c.get("/api/admin/questions.csv?token=adm").get_data(as_text=True)
        self.assertIn("賽前一晚吃什麼？", csv_text)

    def test_production_requires_secret(self):
        with self.assertRaises(RuntimeError):
            create_app({"DEV_MODE": False, "DB_PATH": os.path.join(self.tmp.name, "p.db"),
                        "SECRET_KEY": "dev-only-change-me"})


class TestOAIdentity(Base):
    """官方帳號 webhook 認人：加好友 → 回專屬連結 → 點開就是自己的卡。"""
    extra_config = {"IDENTITY": "oa", "FRIEND_CHECK": "oa", "LINE_CHANNEL_ID": "123",
                    "LINE_CHANNEL_SECRET": "s3cret", "PUBLIC_URL": "https://expo.example"}

    def setUp(self):
        super().setUp()
        self.sent = []
        for name, fake in (("channel_token", lambda *a: "tok"),
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

    def test_same_user_same_card(self):
        a = self.open("a", "U1")
        self.post("a", "/api/station/1", distance="half")
        self.hook(self.ev("message", "U1", "任務卡"))
        self.c("a2").get(self.link_path())
        self.assertEqual(self.me("a2")["code"], a["code"])
        self.assertTrue(self.me("a2")["stations"]["1"])

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
        self.post("b", "/api/station/1", distance="half")
        self.assertTrue(self.me("a")["stations"]["3"])
        self.assertTrue(self.me("b")["stations"]["3"])

    def test_invite_by_manual_code(self):
        a = self.open("a", "U1")
        self.open("b", "U2")
        self.post("b", "/api/station/1", distance="half")
        self.assertEqual(self.post("b", "/api/ref", code="ZZZZZ").status_code, 400)
        self.assertEqual(self.post("b", "/api/ref", code=a["code"].lower()).status_code, 200)
        self.assertTrue(self.me("a")["stations"]["3"])
        self.assertEqual(self.post("b", "/api/ref", code=a["code"]).status_code, 400)

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
        self.online3("a")
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
