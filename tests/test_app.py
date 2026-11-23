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

    def conn(self):
        return __import__("db").connect(self.app.config["DB_PATH"])

    def prize(self, who, key):
        """讓某人抽到指定的獎（直接改他那副牌的第一張，再選第一張）。"""
        self.me(who)                                    # 確保牌已經發了
        hand = ",".join([key] + ["p88"] * 15)
        self.conn().execute("UPDATE draws SET hand=? WHERE user_id=?", (hand, "dev:" + who))
        r = self.post(who, "/api/draw", pick=0).get_json()["state"]["rewards"]
        self.assertEqual(r["prize"], key)

    def quote(self, code, **cart):
        return self.staff("/api/staff/quote", code=code,
                          items=[{"id": k, "qty": v} for k, v in cart.items()])

    def sale(self, code, pay="cash", name="阿志", total=None, **cart):
        items = [{"id": k, "qty": v} for k, v in cart.items()]
        if total is None:
            total = self.staff("/api/staff/quote", code=code, items=items).get_json()["total"]
        return self.staff("/api/staff/sale", name=name, code=code, items=items, pay=pay, total=total)


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
        r = self.post("a", "/api/distance", distance="half")                 # 不是好友不能選距離
        self.assertEqual((r.status_code, r.get_json()["reason"]), (403, "not_friend"))
        s = self.me("a")
        self.assertEqual((s["distance"], s["rewards"]["gummy"], s["rewards"]["cards"]), (None, "locked", 0))
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

    def test_onsite_visible_early_and_ends_after_event(self):
        self.assertEqual(self.join("x", distance=None)["rewards"]["onsite"], "locked")
        a = self.join("a")
        self.assertEqual((a["rewards"]["onsite"], a["rewards"]["prize"]), ("ready", None))
        self.clock = at("2026-12-20 09:00")
        self.assertEqual(self.me("a")["rewards"]["onsite"], "ended")

    def test_draw_deals_first_then_pick_reveals_real_hand(self):
        self.assertEqual(self.post("x", "/api/draw", pick=0).status_code, 401)
        self.join("n", distance=None)
        self.assertEqual(self.post("n", "/api/draw", pick=0).status_code, 400)   # 還沒選距離
        a = self.join("a")["rewards"]                                       # 展期前在家就能抽
        self.assertEqual((a["cards"], a["prize"]), (16, None))
        self.assertNotIn("hand", a)                                         # 選之前不給牌面
        stored = self.conn().execute("SELECT hand FROM draws WHERE user_id='dev:a'").fetchone()["hand"].split(",")
        self.assertEqual(sorted(stored), sorted(k for k, n in rules.DECK for _ in range(n)))
        for bad in (16, -1, "3", None, True):
            self.assertEqual(self.post("a", "/api/draw", pick=bad).status_code, 400, bad)
        r = self.post("a", "/api/draw", pick=5).get_json()["state"]["rewards"]
        self.assertEqual((r["hand"], r["pick"], r["prize"]), (stored, 5, stored[5]))
        again = self.post("a", "/api/draw", pick=0).get_json()["state"]["rewards"]   # 重選不會變
        self.assertEqual((again["pick"], again["prize"]), (5, stored[5]))
        cfg = json.dumps(self.c("a").get("/api/config").get_json())
        self.assertNotIn("DECK", cfg)

    def test_deck_distribution(self):
        conn = self.conn()
        with mock.patch.dict(rules.GIFT_QUOTA, {k: 10 ** 6 for k in rules.GIFT_QUOTA}):
            for i in range(1600):
                conn.execute("INSERT INTO users(id, name, code, created_at) VALUES(?,?,?,?)",
                             (f"s{i}", "", f"S{i:04d}", "x"))
                rules.deal(conn, f"s{i}", self.clock)
                rules.pick_card(conn, f"s{i}", i % 16, self.clock)
        got = dict(conn.execute("SELECT prize, COUNT(*) FROM draws GROUP BY prize").fetchall())
        self.assertEqual(set(got), {k for k, _ in rules.DECK})
        self.assertTrue(340 < got["p88"] < 460 and 240 < got["gum200"] < 360
                        and 60 < got["half"] < 140 and 60 < got["g_noxx"] < 140, got)

    def test_gift_quota_stops_new_gift_cards(self):
        with mock.patch.dict(rules.GIFT_QUOTA, {"g_gummy": 1}):
            self.join("a")
            self.prize("a", "g_gummy")                                      # 名額用掉
            self.join("b")
            hand = self.conn().execute("SELECT hand FROM draws WHERE user_id='dev:b'").fetchone()["hand"].split(",")
        self.assertEqual((len(hand), hand.count("g_gummy"), hand.count("p88")), (16, 0, 6))
        self.assertEqual(hand.count("g_cramp"), 1)                          # 其他贈品不受影響

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
        self.prize("a", "p88")
        self.clock = at("2026-12-17 13:00")
        c1 = self.staff("/api/staff/checkin", name="小美", code=a["code"]).get_json()["log_id"]
        o1 = self.sale(a["code"], name="小美", plum=1).get_json()["log_id"]
        self.assertEqual(self.staff("/api/staff/undo", name="小美", log_id=c1).status_code, 409)   # 後面還有一筆
        flags = {i["id"]: i["can_undo"] for i in self.staff_get("/api/staff/recent", name="小美")["items"]}
        self.assertEqual((flags[c1], flags[o1]), (False, True))                 # 畫面只讓最新那筆可取消
        self.assertEqual(self.staff("/api/staff/undo", name="小美", log_id=o1).status_code, 200)
        self.assertEqual(self.me("a")["rewards"]["onsite_uses"], 0)
        self.assertTrue(self.me("a")["arrived"])                         # 到場是報到那筆記的，不受影響
        self.clock = at("2026-12-17 13:11")
        self.assertEqual(self.staff("/api/staff/undo", name="小美", log_id=c1).status_code, 409)   # 超過 10 分鐘

    def test_undo_sale_that_marked_arrival(self):
        a = self.join("a")
        self.prize("a", "p88")
        self.clock = at("2026-12-17 13:00")
        o = self.sale(a["code"], name="小美", plum=1).get_json()["log_id"]
        self.assertTrue(self.me("a")["arrived"])                         # 結帳也算到場
        self.staff("/api/staff/undo", name="小美", log_id=o)
        self.assertFalse(self.me("a")["arrived"])

    def test_second_checkin_with_nothing_new_is_not_logged(self):
        a = self.join("a")
        self.clock = at("2026-12-17 13:00")
        self.staff("/api/staff/checkin", name="小美", code=a["code"])
        self.assertIsNone(self.staff("/api/staff/checkin", name="小美", code=a["code"]).get_json()["log_id"])
        self.assertEqual(len(self.staff_get("/api/staff/recent", name="小美")["items"]), 1)



class TestCheckout(Base):
    """結帳計價與銷售紀錄。"""

    P = {p["id"]: p for p in __import__("content").PRODUCTS}

    def cart(self, prize=None, member=True, used=0, **items):
        return rules.price_cart([(self.P[k], v) for k, v in items.items()], prize, member, used)

    def test_walkin_pays_list_price_with_web_multibuy(self):
        self.assertEqual(self.cart(member=False, noxx_e=1, cramp=1)["total"], 680 + 780)
        self.assertEqual(self.cart(member=False, cramp=2)["total"], 601 * 2)

    def test_member_baseline_and_crampeace_fixed(self):
        c = self.cart(noxx_e=1, cramp=1)
        self.assertEqual([(l["amount"], l["how"]) for l in c["lines"]], [(632, "93 折"), (601, "現場價")])
        self.assertEqual(self.cart(prize="g_gummy", noxx_e=1)["total"], 632)      # 贈品獎買東西是 93 折

    def test_p88_needs_two_and_never_discounts_crampeace(self):
        self.assertEqual(self.cart("p88", noxx_e=1)["total"], 632)
        self.assertEqual(self.cart("p88", noxx_e=1, plum=1)["total"], 598 + 703)
        self.assertEqual(self.cart("p88", noxx_e=1, cramp=1)["total"], 598 + 601)  # 勁速補算件數、不打折

    def test_half_cheapest_one_and_never_worse_than_baseline(self):
        c = self.cart("half", noxx_e=1, gum_m=1)
        self.assertEqual((c["total"], c["lines"][1]["how"]), (680 + 125, "半價"))
        self.assertEqual(self.cart("half", noxx_e=1)["total"], 632)
        self.assertEqual(self.cart("half", spm=8)["total"], 2120 * 8)             # 件數多時 93 折比較划算
        self.assertEqual(self.cart("half", cramp=2)["total"], 1202)               # 只有勁速補就沒得折

    def test_special_price_capped_per_person(self):
        c = self.cart("gum200", gum_m=2, gum_pc=1)
        self.assertEqual((c["total"], sum(l["special"] for l in c["lines"])), (200 + 200 + 232, 2))
        self.assertEqual(self.cart("gum200", used=1, gum_m=3)["total"], 200 + 232 + 232)
        self.assertEqual(self.cart("gum200", used=2, gum_m=1)["total"], 232)
        self.assertEqual(self.cart("flask100", flask=1, noxx_e=1)["total"], 100 + 632)   # 其他東西 93 折
        self.assertEqual(self.cart("flask100", gum_m=1)["total"], 232)                   # 沒買指定商品

    def test_deal500_covers_crampeace_and_noxx_best_saving_first(self):
        c = self.cart("deal500", cramp=2, noxx_p=1)
        self.assertEqual(c["total"], 500 + 500 + 601)
        self.assertEqual({l["id"]: l["special"] for l in c["lines"]}, {"cramp": 1, "noxx_p": 1})

    def test_sale_records_and_tracks_special_quota(self):
        a = self.join("a")
        self.prize("a", "flask100")
        self.assertEqual(self.sale(a["code"], total=100, flask=1).status_code, 400)       # 還沒到展期
        self.clock = at("2026-12-17 13:00")
        q = self.quote(a["code"], flask=1, plum=1).get_json()
        self.assertEqual((q["member"], q["prize"], q["total"], q["list_total"]), (True, "flask100", 100 + 743, 1098))
        r = self.sale(a["code"], total=999, flask=1, plum=1)                             # 畫面金額不對
        self.assertEqual((r.status_code, r.get_json()["reason"], r.get_json()["total"]), (409, "changed", 843))
        self.assertEqual(self.me("a")["rewards"]["onsite_uses"], 0)
        r = self.sale(a["code"], pay="linepay", flask=1, plum=1).get_json()
        self.assertEqual((r["total"], r["user"]["rewards"]["special_left"]), (843, 1))
        self.assertTrue(self.me("a")["arrived"])
        r2 = self.sale(a["code"], flask=2).get_json()                                    # 只剩 1 個特價
        self.assertEqual((r2["total"], r2["user"]["rewards"]["special_left"]), (100 + 278, 0))
        self.assertEqual(self.quote(a["code"], flask=1).get_json()["total"], 278)
        t = self.staff_get("/api/staff/today")["sales"]
        self.assertEqual((t["n"], t["total"], t["cash"], t["linepay"]), (2, 1221, 378, 843))
        self.assertEqual(self.staff("/api/staff/undo", log_id=r2["log_id"]).status_code, 200)
        self.assertEqual(self.me("a")["rewards"]["special_left"], 1)                     # 取消後額度還回去
        d = self.app.test_client().get("/api/admin/stats?token=adm").get_json()
        self.assertEqual((d["sales_n"], d["sales_total"], d["buyers"]), (1, 843, 1))
        self.assertEqual(d["by_prize"][0], {"prize": "flask100", "drawn": 1, "gifts": 0, "buyers": 1, "revenue": 843})
        self.assertEqual({x["name"]: x["qty"] for x in d["by_product"]}, {"搖滾話梅 沖泡飲": 1, "軟水壺": 1})
        csv_ = self.app.test_client().get("/api/admin/sales.csv?token=adm").get_data(as_text=True)
        self.assertIn("LINE Pay", csv_); self.assertIn("已取消", csv_); self.assertIn("100 元", csv_)

    def test_sale_needs_payment_method_and_valid_cart(self):
        a = self.join("a")
        self.clock = at("2026-12-17 13:00")
        self.assertEqual(self.sale(a["code"], pay="card", total=743, plum=1).status_code, 400)
        for items in ([], [{"id": "nope", "qty": 1}], [{"id": "plum", "qty": 0}], [{"id": "plum", "qty": "2"}], "x"):
            r = self.staff("/api/staff/sale", code=a["code"], items=items, pay="cash", total=0)
            self.assertEqual(r.status_code, 400, items)
        self.assertEqual(self.staff("/api/staff/quote", code="ZZZZZ", items=[{"id": "plum", "qty": 1}]).status_code, 404)

    def test_walkin_sale_without_voucher(self):
        self.clock = at("2026-12-17 13:00")
        q = self.quote("", cramp=1).get_json()
        self.assertEqual((q["member"], q["total"]), (False, 780))
        r = self.sale("", name="小美", cramp=2).get_json()
        self.assertEqual((r["total"], r["user"]), (1202, None))
        item = self.staff_get("/api/staff/recent", name="小美")["items"][0]
        self.assertEqual((item["name"], item["code"], item["can_undo"]), ("散客", "", True))
        self.assertEqual(self.staff("/api/staff/undo", name="小美", log_id=r["log_id"]).status_code, 200)
        self.assertEqual(self.staff_get("/api/staff/today")["sales"]["n"], 0)

    def test_code_without_voucher_gets_list_price(self):
        x = self.join("x", distance=None)                    # 加了好友但還沒選距離＝還沒領券
        self.clock = at("2026-12-17 13:00")
        q = self.quote(x["code"], noxx_e=1).get_json()
        self.assertEqual((q["member"], q["total"]), (False, 680))

    def test_undrawn_member_gets_baseline(self):
        a = self.join("a")
        self.clock = at("2026-12-17 13:00")
        q = self.quote(a["code"], noxx_e=2).get_json()
        self.assertEqual((q["member"], q["prize"], q["total"]), (True, None, 632 * 2))

    def test_gift_prize_handed_once_with_checkin(self):
        a = self.join("a")
        self.prize("a", "g_noxx")
        self.assertEqual(self.me("a")["rewards"]["prize_gift"], "ready")
        self.clock = at("2026-12-17 13:00")
        v = self.staff("/api/staff/lookup", code=a["code"]).get_json()["user"]
        self.assertEqual((v["give"], "氮能雙效膠" in v["gift_text"]), (["gummy", "prize"], True))
        r = self.checkin("a").get_json()
        self.assertEqual((r["given"], r["user"]["give"]), (["gummy", "prize"], []))
        self.assertEqual(self.me("a")["rewards"]["prize_gift"], "claimed")
        self.assertEqual(self.checkin("a").get_json()["given"], [])               # 第二次掃不會再給
        self.assertEqual(self.staff("/api/staff/undo", log_id=r["log_id"]).status_code, 200)
        self.assertEqual(self.me("a")["rewards"]["prize_gift"], "ready")
        self.join("b"); self.prize("b", "p88")
        self.assertIsNone(self.me("b")["rewards"]["prize_gift"])                   # 折扣類沒有贈品

    def test_gift_drawn_after_first_visit_can_be_collected_later(self):
        a = self.join("a")
        self.join("late"); self.prize("late", "g_cramp")
        self.clock = at("2026-12-17 13:00")
        self.assertEqual(self.checkin("a").get_json()["given"], ["gummy"])
        self.prize("a", "g_cramp")                                               # 到了現場才翻牌
        self.assertEqual(self.checkin("a").get_json()["given"], ["prize"])
        self.clock = at("2026-12-20 09:00")
        self.assertEqual(self.me("late")["rewards"]["prize_gift"], "ended")       # 沒來的人，贈品作廢

    def test_search_by_name(self):
        self.join("王小明"); self.join("陳大華")
        r = self.staff("/api/staff/search", q="小明").get_json()["items"]
        self.assertEqual([x["name"] for x in r], ["王小明"])
        self.assertEqual(self.staff("/api/staff/search", q="").status_code, 400)
        self.assertEqual(self.staff("/api/staff/search", pin="0", q="小明").status_code, 401)

    def test_admin_edits_products(self):
        cl = self.app.test_client()
        items = cl.get("/api/admin/products?token=adm").get_json()["items"]
        self.assertEqual(len(items), 18)
        self.assertEqual(cl.get("/api/admin/products?token=no").status_code, 401)
        plum = next(p for p in items if p["id"] == "plum")
        r = cl.post("/api/admin/product?token=adm", json={**plum, "price": 750, "active": True})
        self.assertEqual(r.status_code, 200)
        r = cl.post("/api/admin/product?token=adm", json={**next(p for p in items if p["id"] == "towel"), "active": False})
        r = cl.post("/api/admin/product?token=adm", json={"name": "帶勁瓶", "cat": "配件", "price": 199})
        self.assertEqual(len(r.get_json()["items"]), 19)
        self.assertEqual(cl.post("/api/admin/product?token=adm", json={"name": "", "price": 10}).status_code, 400)
        self.assertEqual(cl.post("/api/admin/product?token=adm", json={"name": "x", "price": "10"}).status_code, 400)
        shown = {p["name"]: p["price"] for p in self.staff_get("/api/staff/products")["items"]}
        self.assertEqual((shown["搖滾話梅 沖泡飲"], shown["帶勁瓶"], "冰感瞬涼巾" in shown), (750, 199, False))
        self.clock = at("2026-12-17 13:00")
        self.assertEqual(self.quote("", towel=1).status_code, 400)                # 下架的不能賣

    def test_hands_from_an_older_deck_are_redealt(self):
        a = self.join("a"); self.join("b"); self.prize("b", "p88")
        c = self.conn()
        c.execute("UPDATE draws SET hand='p88,gift,half,p88', pick=1, prize='gift', at='x' WHERE user_id='dev:a'")
        app2 = create_app({"DEV_MODE": True, "DB_PATH": self.app.config["DB_PATH"], "NOW_FN": lambda: self.clock})
        left = [r["user_id"] for r in c.execute("SELECT user_id FROM draws")]
        self.assertEqual(left, ["dev:b"])                                     # 現行的牌不受影響
        cl = app2.test_client()
        cl.post("/api/session", json={"dev_user": "a", "dev_friend": True})
        r = cl.get("/api/me").get_json()["state"]["rewards"]
        self.assertEqual((r["cards"], r["prize"]), (16, None))                 # 重新發牌、可以再抽

    def test_old_database_is_upgraded(self):
        import sqlite3
        import db as dbm
        path = os.path.join(self.tmp.name, "old.db")
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', code TEXT NOT NULL UNIQUE,
                is_friend INTEGER NOT NULL DEFAULT 0, is_new INTEGER NOT NULL DEFAULT 0, distance TEXT,
                invited_by TEXT, arrived_at TEXT, plan_sent_at TEXT, consent_at TEXT, created_at TEXT NOT NULL);
            CREATE TABLE staff_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, staff TEXT NOT NULL,
                action TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                detail TEXT NOT NULL DEFAULT '{}', undone_at TEXT);
            INSERT INTO users(id, code, created_at) VALUES('u1', 'AAAAA', 'x');
            INSERT INTO staff_log(at, staff, action, user_id) VALUES('t', '阿志', 'checkin', 'u1');
        """)
        c.commit(); c.close()
        dbm.init(path, __import__("content").PRODUCTS)
        conn = dbm.connect(path)
        cols = {r["name"]: r["notnull"] for r in conn.execute("PRAGMA table_info(staff_log)")}
        self.assertEqual(cols["user_id"], 0)
        self.assertEqual(conn.execute("SELECT staff FROM staff_log").fetchone()["staff"], "阿志")
        conn.execute("INSERT INTO staff_log(at, staff, action, user_id) VALUES('t', '阿志', 'sale', NULL)")
        self.assertEqual(conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"], 18)

    def test_rehearsal_day_lets_staff_try_before_the_event(self):
        app = create_app({"DEV_MODE": True, "DB_PATH": os.path.join(self.tmp.name, "r.db"),
                          "STAFF_PIN": "4321", "REHEARSAL_DAY": "2026-12-18"})
        c = app.test_client()
        c.post("/api/session", json={"dev_user": "a", "dev_friend": True})
        c.post("/api/consent"); c.post("/api/distance", json={"distance": "half"})
        code = c.get("/api/me").get_json()["state"]["code"]
        r = app.test_client().post("/api/staff/checkin", json={"code": code},
                                   headers={"X-Staff-Pin": "4321", "X-Staff-Name": "a"})
        self.assertEqual(r.get_json()["given"], ["gummy"])
        self.assertTrue(c.get("/api/config").get_json()["rehearsal"])
        with self.assertRaises(RuntimeError):
            create_app({"DEV_MODE": True, "DB_PATH": os.path.join(self.tmp.name, "r2.db"),
                        "REHEARSAL_DAY": "2026-11-23"})

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

    def test_webhook_forwarded_to_previous_receiver(self):
        self.app.config["WEBHOOK_FORWARD_URL"] = "https://old.example/hook"
        got = []
        with mock.patch.object(line_api, "forward", side_effect=lambda url, raw, sig: got.append((url, raw, sig))):
            self.hook(self.ev("follow", "U1"))
        self.assertEqual(got[0][0], "https://old.example/hook")
        self.assertIn(b'"U1"', got[0][1])
        self.assertTrue(got[0][2])
        with mock.patch.object(line_api, "forward", side_effect=line_api.LineError("down")):
            self.assertEqual(self.hook(self.ev("follow", "U2")).status_code, 200)     # 轉送失敗不影響
        self.assertEqual(len(self.sent), 2)

    def test_follow_right_after_page_created_user_counts_as_new(self):
        # LIFF 的情況：網頁先建立了這個人（當下還不知道是新好友），幾秒後才收到加好友通知
        self.join("p", new=False, consent=False)
        conn = __import__("db").connect(self.app.config["DB_PATH"])
        conn.execute("UPDATE users SET id='U7' WHERE id='dev:p'")
        self.hook(self.ev("follow", "U7"))
        self.assertEqual(conn.execute("SELECT is_new FROM users WHERE id='U7'").fetchone()[0], 1)
        conn.execute("UPDATE users SET id='U8', is_new=0, created_at='2026-11-01T10:00:00+08:00' WHERE id='U7'")
        self.hook(self.ev("follow", "U8"))                                           # 很久以前就有券的人重新加入
        self.assertEqual(conn.execute("SELECT is_new FROM users WHERE id='U8'").fetchone()[0], 0)

    def test_reply_failure_still_registers(self):
        with mock.patch.object(line_api, "reply", side_effect=line_api.LineError("x")):
            self.assertEqual(self.hook(self.ev("follow", "U9")).status_code, 200)
        self.hook(self.ev("message", "U9", "任務卡"))
        self.c("n").get(self.link_path())
        self.assertTrue(self.me("n")["is_friend"])


class TestLiffMode(Base):
    extra_config = {"IDENTITY": "liff", "LIFF_ID": "2006468465-abcd", "LINE_LOGIN_CHANNEL_ID": "2006468465",
                    "LINE_CHANNEL_ID": "123", "LINE_CHANNEL_SECRET": "s3cret", "DEV_MODE": False,
                    "SECRET_KEY": "k" * 32}

    def test_login_with_line_and_reply_points_to_liff(self):
        with mock.patch.object(line_api, "verify_id_token", return_value=("U1", "小明")), \
             mock.patch.object(line_api, "is_friend", return_value=False):
            r = self.c("a").post("/api/session", json={"id_token": "x", "access_token": "y"})
        self.assertEqual(r.status_code, 200)
        s = r.get_json()["state"]
        self.assertEqual((s["name"], s["is_friend"], s["is_new"]), ("小明", False, True))
        self.assertEqual(self.c("a").get("/api/config").get_json()["friend_check"], "line")
        sent = []
        raw = json.dumps({"events": [{"type": "follow", "source": {"type": "user", "userId": "U1"}, "replyToken": "rt"}]}).encode()
        sig = base64.b64encode(hmac.new(b"s3cret", raw, hashlib.sha256).digest()).decode()
        with mock.patch.object(line_api, "channel_token", return_value="tok"), \
             mock.patch.object(line_api, "reply", side_effect=lambda tok, rt, text: sent.append(text)):
            self.app.test_client().post("/line/webhook", data=raw, headers={"X-Line-Signature": sig, "Content-Type": "application/json"})
        self.assertIn("https://liff.line.me/2006468465-abcd", sent[0])
        self.assertNotIn("/t/", sent[0])
        self.assertTrue(self.me("a")["is_friend"])

    def test_bad_line_token_rejected(self):
        with mock.patch.object(line_api, "verify_id_token", side_effect=line_api.LineError("bad")):
            self.assertEqual(self.c("a").post("/api/session", json={"id_token": "x"}).status_code, 401)


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
