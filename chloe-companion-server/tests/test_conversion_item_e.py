"""Conversion fix Item E tests (2026-10-18): the first-session package.

Pins:
  E1 — first-session generations retry before any fallback; inside
       the first 10 lifetime messages the canned banks are NEVER
       served (wait line instead); outside the window the canned
       path is untouched.
  E2 — the first-session guidance (lead, hooks, plant, open-loop
       callback) rides in the system text only when flagged.
  E3 — the early taste fires once per user around messages 4-6 when
       she has give media and he has never received any.
  E4 — her planted thread is extracted into open_loops and the
       Item B win-back prompt picks it up first.
  E5 — the warm-hour gate: first-day user, his last message 60-120
       minutes ago, lifetime 1-14, once ever, never stacked with B.
  E6 — milestone events at lifetime crossings 1/3/5/10/30.

Run:  python -m pytest tests/test_conversion_item_e.py -q
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, brain, capability_hooks, characters, config, db  # noqa: E402
from app import proactive, store  # noqa: E402
from app import main as api  # noqa: E402

NOW = datetime(2026, 10, 18, 17, 0, 0)  # naive UTC


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# E1 / E2 — brain-level
# ---------------------------------------------------------------------------
class BrainFirstSession(unittest.TestCase):
    def setUp(self):
        self._real_gen = brain.gemini_generate
        self._real_mouth = capability_hooks.mouth_with_hook
        self._real_gmouth = capability_hooks.generic_mouth_with_hook
        capability_hooks.mouth_with_hook = lambda t, c: "CANNED_LINE"
        capability_hooks.generic_mouth_with_hook = \
            lambda t, c, cid: "CANNED_LINE"
        self.calls = 0

    def tearDown(self):
        brain.gemini_generate = self._real_gen
        capability_hooks.mouth_with_hook = self._real_mouth
        capability_hooks.generic_mouth_with_hook = self._real_gmouth

    def _gen(self, results):
        def fake(user_text, context, cid):
            self.calls += 1
            return results.pop(0) if results else None
        return fake

    def test_first10_never_serves_canned(self):
        brain.gemini_generate = self._gen([None, None, None, None])
        out = brain.compose_reply(
            "hi", {"first_session": True, "lifetime_before": 3}, "chloe")
        self.assertNotEqual(out, "CANNED_LINE")
        self.assertIn("one sec", out)
        # initial + extra + two breath retries = 4 attempts
        self.assertEqual(self.calls, 4)

    def test_first10_retry_recovers_model_reply(self):
        brain.gemini_generate = self._gen([None, None, "real model line"])
        out = brain.compose_reply(
            "hi", {"first_session": True, "lifetime_before": 3}, "chloe")
        self.assertEqual(out, "real model line")
        self.assertEqual(self.calls, 3)

    def test_first_session_10_to_14_gets_extra_retry_then_canned(self):
        brain.gemini_generate = self._gen([None, None])
        out = brain.compose_reply(
            "hi", {"first_session": True, "lifetime_before": 12}, "chloe")
        self.assertEqual(self.calls, 2)  # initial + one extra
        self.assertEqual(out, "CANNED_LINE")  # canned allowed at 12

    def test_established_user_gets_no_extra_retry(self):
        brain.gemini_generate = self._gen([None])
        out = brain.compose_reply("hi", {}, "chloe")
        self.assertEqual(self.calls, 1)
        self.assertEqual(out, "CANNED_LINE")

    def test_guidance_only_when_flagged(self):
        ctx = {"character": {"id": "chloe", "name": "Chloe"},
               "name": "Sam", "first_session": True,
               "plant_thread": True,
               "open_loop": "the story about her neighbor's dog"}
        text = brain._gemini_system_text(ctx, "chloe")
        self.assertIn("FIRST DAYS TOGETHER", text)
        self.assertIn("neighbor's dog", text)
        plain = brain._gemini_system_text(
            {"character": {"id": "chloe", "name": "Chloe"},
             "name": "Sam"}, "chloe")
        self.assertNotIn("FIRST DAYS TOGETHER", plain)


# ---------------------------------------------------------------------------
# E5 — warm-hour gate (in-memory harness, same style as Item B)
# ---------------------------------------------------------------------------
def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE users (
        user_id TEXT PRIMARY KEY, name TEXT DEFAULT '', email TEXT,
        tier TEXT DEFAULT 'basic', messages_remaining INTEGER DEFAULT 0,
        messages_used_total INTEGER DEFAULT 0,
        proactive_enabled INTEGER DEFAULT 0,
        timezone TEXT DEFAULT 'America/New_York',
        proactive_day TEXT DEFAULT '',
        proactive_sent_today INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("""CREATE TABLE conversation (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
        character_id TEXT DEFAULT 'chloe', role TEXT NOT NULL,
        text TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("CREATE TABLE stripe_customers (user_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE trial_grants (user_id TEXT PRIMARY KEY)")
    conn.execute("""CREATE TABLE jvzoo_subscriptions (
        user_id TEXT PRIMARY KEY, status TEXT DEFAULT 'active')""")
    conn.commit()
    return conn


class WarmHourGate(unittest.TestCase):
    def setUp(self):
        self.conn = _memdb()
        self._old_owners = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"

    def tearDown(self):
        if self._old_owners is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._old_owners
        self.conn.close()

    def _due(self, user_id="u_new", total=5, created_hours_ago=3,
             last_role="user", last_minutes_ago=90, **kw):
        d = dict(user_id=user_id, name="Sam", email="sam@realmail.org",
                 tier="basic", messages_remaining=45,
                 messages_used_total=total, proactive_enabled=1,
                 timezone="America/New_York", proactive_day="",
                 proactive_sent_today=0,
                 created_at=_fmt(NOW - timedelta(hours=created_hours_ago)))
        d.update(kw)
        cols = ", ".join(d.keys())
        vals = ", ".join(["?"] * len(d))
        self.conn.execute(f"INSERT INTO users ({cols}) VALUES ({vals})",
                          tuple(d.values()))
        if last_role is not None:
            self.conn.execute(
                "INSERT INTO conversation (user_id, character_id, role, "
                "text, created_at) VALUES (?, 'chloe', ?, 'hi', ?)",
                (user_id, last_role,
                 _fmt(NOW - timedelta(minutes=last_minutes_ago))))
        self.conn.commit()
        return proactive.should_send_warmhour(self.conn, user_id,
                                              "chloe", now=NOW)

    def test_base_case_is_due(self):
        self.assertTrue(self._due())

    def test_her_last_message_is_not_due(self):
        self.assertFalse(self._due(last_role="chloe"))

    def test_too_fresh_is_not_due(self):
        self.assertFalse(self._due(last_minutes_ago=30))

    def test_too_stale_is_not_due(self):
        self.assertFalse(self._due(last_minutes_ago=150))

    def test_window_edges_are_due(self):
        self.assertTrue(self._due(user_id="u_edge1", last_minutes_ago=60))
        self.assertTrue(self._due(user_id="u_edge2", last_minutes_ago=120))

    def test_signed_up_yesterday_is_not_due(self):
        self.assertFalse(self._due(created_hours_ago=30))

    def test_zero_messages_is_not_due(self):
        self.assertFalse(self._due(total=0))

    def test_14_is_due_15_is_not(self):
        self.assertTrue(self._due(user_id="u_14", total=14))
        self.assertFalse(self._due(user_id="u_15", total=15))

    def test_already_warmhoured_is_not_due(self):
        self.assertTrue(self._due(user_id="u_once"))
        proactive._record_retention(self.conn, "u_once", "chloe",
                                    "warmhour", "2026-10-18")
        self.assertFalse(proactive.should_send_warmhour(
            self.conn, "u_once", "chloe", now=NOW))

    def test_winback_today_blocks_warmhour(self):
        self.assertTrue(self._due(user_id="u_stack"))
        local_today = proactive._user_local_now(
            "America/New_York", NOW).date().isoformat()
        proactive._record_retention(self.conn, "u_stack", "chloe",
                                    "winback", local_today)
        self.assertFalse(proactive.should_send_warmhour(
            self.conn, "u_stack", "chloe", now=NOW))

    def test_record_sent_marks_warmhour(self):
        self.assertTrue(self._due(user_id="u_rec"))
        proactive._record_sent(self.conn, "u_rec", "chloe", "warmhour",
                               "2026-10-18")
        self.assertIsNotNone(
            proactive._retention_row(self.conn, "u_rec", "warmhour"))


# ---------------------------------------------------------------------------
# E3 / E4 / E6 — end to end through chat
# ---------------------------------------------------------------------------
class _Client:
    host = "127.0.0.1"


class _Req:
    def __init__(self, token=None):
        self.headers = {"authorization": f"Bearer {token}"} if token else {}
        self.client = _Client()


class ChatFirstSession(unittest.TestCase):
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
        brain.compose_reply = lambda m, c, cid: "her warm reply"
        brain.build_brain_context = lambda conn, uid, user, msg, char: {}
        brain.learn_from_turn = lambda conn, uid, msg, cid: None
        assist.maybe_assist = lambda conn, user, msg, cid: None

        def fake_gen(prompt, context, cid):
            if "UNFINISHED" in prompt:
                return "the story about her neighbor's dog"
            if "surprise clip" in prompt:
                return "just because I like you 💛"
            return None
        brain.gemini_generate = fake_gen
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO girl_footage (girl_id, label, url, "
                "sort_order) VALUES ('chloe', 'give', "
                "'/generated/taste.mp4', 1)")
            conn.commit()
        finally:
            conn.close()

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
        self.tmp.cleanup()

    def _mk(self, user_id, total):
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, user_id)
            conn.execute(
                "UPDATE users SET messages_used_total = ?, "
                "messages_remaining = 47 WHERE user_id = ?",
                (total, user_id))
            conn.commit()
            store.create_session(conn, user_id, "tok_" + user_id)
        finally:
            conn.close()

    def _chat(self, user_id):
        body = api.ChatIn(message="tell me more", character_id="chloe")
        return api.chat(body, _Req("tok_" + user_id))

    def test_taste_fires_once_and_loop_is_stored(self):
        self._mk("u_taste", total=3)
        resp = self._chat("u_taste")
        self.assertIn("give_media", resp)
        self.assertEqual(resp["give_media"]["url"], "/generated/taste.mp4")
        self.assertEqual(resp["give_media"]["media_type"], "video")
        conn = db.connect(self.db_path)
        try:
            self.assertTrue(store.taste_was_sent(conn, "u_taste"))
            self.assertEqual(
                store.get_open_loop(conn, "u_taste", "chloe"),
                "the story about her neighbor's dog")
            gives = conn.execute(
                "SELECT COUNT(*) AS c FROM user_give_history "
                "WHERE user_id = 'u_taste'").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(gives, 1)
        # Next message: no second taste.
        resp2 = self._chat("u_taste")
        self.assertNotIn("give_media", resp2)

    def test_milestone_event_at_crossing_3(self):
        self._mk("u_mile", total=2)
        self._chat("u_mile")
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT kind FROM conversion_events "
                "WHERE user_id = 'u_mile'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "milestone_3")

    def test_no_taste_after_window(self):
        self._mk("u_late", total=9)
        resp = self._chat("u_late")
        self.assertNotIn("give_media", resp)

    def test_winback_prompt_picks_up_open_loop(self):
        conn = db.connect(self.db_path)
        store.get_or_create_user(conn, "u_loop")
        store.save_open_loop(conn, "u_loop", "chloe",
                             "the story about her neighbor's dog")
        captured = {}
        real_ctx = brain.build_brain_context
        real_gen = brain.gemini_generate
        real_char = characters.get_character
        brain.build_brain_context = lambda c, uid, user, seed, ch: {}
        brain.gemini_generate = lambda prompt, ctx, cid: (
            captured.__setitem__("prompt", prompt), "warm line")[1]
        characters.get_character = lambda cid, uid=None: {
            "id": "chloe", "name": "Chloe"}
        try:
            out = proactive.generate_proactive_message(
                conn, "u_loop", "chloe", "winback")
        finally:
            brain.build_brain_context = real_ctx
            brain.gemini_generate = real_gen
            characters.get_character = real_char
            conn.close()
        self.assertEqual(out, "warm line")
        self.assertIn("neighbor's dog", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
