"""Conversion fix Item D tests (2026-10-18): the trial offer at depth.

Pins the due logic (crossing config.TRIAL_OFFER_AT, one gentle repeat
after 7+ days, never stacked on a retention-send day, never for
owner/test/paid users) and both deliveries (telegram message in the
bot flow; response flag on web). Brain + telegram are stubbed — no
Gemini pool, no Bot API.

Run:  python -m pytest tests/test_conversion_item_d.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, brain, characters, config, db, proactive, store  # noqa: E402
from app import main as api  # noqa: E402


class _Client:
    host = "127.0.0.1"


class _Req:
    def __init__(self, token=None):
        self.headers = {"authorization": f"Bearer {token}"} if token else {}
        self.client = _Client()


class ItemD(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._old_owners = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        self._real_cache = characters._cache
        characters._cache = {"ts": time.time(), "chars": [], "local": []}
        self._real_compose = brain.compose_reply
        self._real_context = brain.build_brain_context
        self._real_learn = brain.learn_from_turn
        self._real_assist = assist.maybe_assist
        self._real_gen = brain.gemini_generate
        self._real_tg = proactive.send_telegram_message
        brain.compose_reply = lambda m, c, cid: "a real reply"
        brain.build_brain_context = lambda conn, uid, user, msg, char: {}
        brain.learn_from_turn = lambda conn, uid, msg, cid: None
        assist.maybe_assist = lambda conn, user, msg, cid: None
        brain.gemini_generate = lambda prompt, ctx, cid: (
            "i love talking with you 💛 there's a little $0.99 starter "
            "with 100 messages if you want us to keep going: "
            + prompt.rsplit(" ", 1)[-1])
        self.tg_sent = []
        proactive.send_telegram_message = lambda chat_id, text: (
            self.tg_sent.append((chat_id, text)) or True)

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        if self._old_owners is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._old_owners
        characters._cache = self._real_cache
        brain.compose_reply = self._real_compose
        brain.build_brain_context = self._real_context
        brain.learn_from_turn = self._real_learn
        assist.maybe_assist = self._real_assist
        brain.gemini_generate = self._real_gen
        proactive.send_telegram_message = self._real_tg
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _mk(self, user_id, total=0, bank=60, created_days_ago=2,
            paid=False):
        conn = db.connect(self.db_path)
        try:
            user = store.get_or_create_user(conn, user_id)
            created = (datetime.utcnow()
                       - timedelta(days=created_days_ago)).strftime(
                           "%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE users SET messages_used_total = ?, "
                "messages_remaining = ?, created_at = ? WHERE user_id = ?",
                (total, bank, created, user_id))
            if paid:
                conn.execute("INSERT OR IGNORE INTO trial_grants "
                             "(user_id) VALUES (?)", (user_id,))
            conn.commit()
            return store.get_or_create_user(conn, user_id)
        finally:
            conn.close()

    def _due(self, user, prev, new):
        conn = db.connect(self.db_path)
        try:
            return api._trial_offer_due(conn, user, prev, new)
        finally:
            conn.close()

    def _show(self, user_id, days_ago=0):
        conn = db.connect(self.db_path)
        try:
            store.record_trial_offer_shown(conn, user_id)
            if days_ago:
                ts = (datetime.utcnow()
                      - timedelta(days=days_ago)).strftime(
                          "%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "UPDATE trial_offer_state SET first_shown_at = ?, "
                    "last_shown_at = ? WHERE user_id = ?",
                    (ts, ts, user_id))
                conn.commit()
        finally:
            conn.close()

    # -- due logic ---------------------------------------------------------
    def test_crossing_30_is_due(self):
        user = self._mk("u_cross", total=29)
        self.assertTrue(self._due(user, 29, 30))

    def test_below_threshold_is_not_due(self):
        user = self._mk("u_below", total=28)
        self.assertFalse(self._due(user, 28, 29))

    def test_old_signup_past_threshold_never_shown_is_not_due(self):
        user = self._mk("u_old", total=35, created_days_ago=40)
        self.assertFalse(self._due(user, 35, 36))

    def test_recent_signup_past_threshold_is_due(self):
        user = self._mk("u_recent", total=35, created_days_ago=3)
        self.assertTrue(self._due(user, 35, 36))

    def test_shown_twice_is_never_due(self):
        user = self._mk("u_twice", total=29)
        self._show("u_twice", days_ago=10)
        self._show("u_twice", days_ago=9)
        self.assertFalse(self._due(user, 29, 30))

    def test_repeat_due_after_7_days(self):
        user = self._mk("u_rep", total=40)
        self._show("u_rep", days_ago=8)
        self.assertTrue(self._due(user, 40, 41))

    def test_repeat_not_due_before_7_days(self):
        user = self._mk("u_rep2", total=40)
        self._show("u_rep2", days_ago=3)
        self.assertFalse(self._due(user, 40, 41))

    def test_paid_user_is_not_due(self):
        user = self._mk("u_paid", total=29, paid=True)
        self.assertFalse(self._due(user, 29, 30))

    def test_owner_is_not_due(self):
        user = self._mk("owner_1", total=29)
        self.assertFalse(self._due(user, 29, 30))

    def test_retention_send_today_blocks(self):
        user = self._mk("u_stack", total=29)
        conn = db.connect(self.db_path)
        try:
            local_today = proactive._user_local_now(
                "America/New_York").date().isoformat()
            proactive._record_retention(conn, "u_stack", "chloe",
                                        "winback", local_today)
        finally:
            conn.close()
        self.assertFalse(self._due(user, 29, 30))

    def test_flag_off_is_not_due(self):
        user = self._mk("u_flag", total=29)
        old = config.CONV_TRIAL_OFFER
        config.CONV_TRIAL_OFFER = False
        try:
            self.assertFalse(self._due(user, 29, 30))
        finally:
            config.CONV_TRIAL_OFFER = old

    # -- delivery ---------------------------------------------------------
    def test_web_delivery_returns_flag_and_records(self):
        user = self._mk("u_web", total=30)
        conn = db.connect(self.db_path)
        try:
            out = api._fire_trial_offer(conn, user, {"id": "chloe"}, {})
            state = store.get_trial_offer_state(conn, "u_web")
            ev = conn.execute(
                "SELECT kind, meta FROM conversion_events "
                "WHERE user_id = 'u_web'").fetchone()
        finally:
            conn.close()
        self.assertIn("client_reference_id=u_web", out["url"])
        self.assertEqual(state["shown_count"], 1)
        self.assertEqual(ev["kind"], "trial_offer_shown")
        self.assertIn('"web"', ev["meta"])

    def test_telegram_delivery_sends_her_message(self):
        user = self._mk("tg_4242", total=30)
        conn = db.connect(self.db_path)
        try:
            out = api._fire_trial_offer(conn, user, {"id": "chloe"}, {})
            turns = conn.execute(
                "SELECT COUNT(*) AS c FROM conversation "
                "WHERE user_id = 'tg_4242'").fetchone()["c"]
        finally:
            conn.close()
        self.assertIsNone(out)
        self.assertEqual(len(self.tg_sent), 1)
        chat_id, text = self.tg_sent[0]
        self.assertEqual(chat_id, "4242")
        self.assertIn("client_reference_id=tg_4242", text)
        self.assertEqual(turns, 1)  # logged into her thread

    # -- end to end through chat ------------------------------------------
    def test_chat_crossing_adds_flag_once(self):
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, "u_e2e")
            conn.execute(
                "UPDATE users SET messages_used_total = 29, "
                "messages_remaining = 60 WHERE user_id = 'u_e2e'")
            conn.commit()
            store.create_session(conn, "u_e2e", "tok_e2e")
        finally:
            conn.close()
        body = api.ChatIn(message="hi", character_id="chloe")
        resp1 = api.chat(body, _Req("tok_e2e"))
        self.assertIn("trial_offer", resp1)
        resp2 = api.chat(body, _Req("tok_e2e"))
        self.assertNotIn("trial_offer", resp2)


if __name__ == "__main__":
    unittest.main()
