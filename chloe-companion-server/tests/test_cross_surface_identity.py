"""Improvement #8 (2026-10-19): one identity across web + Telegram.

The bot is a thin client of this backend — it keeps NO relationship
state of its own (its user_data holds only creator drafts, duo picks
and the current girl). These tests pin the backend half of that
contract:

  * relationship reads (streaks, trust, memories, scene) are keyed by
    the account alone: activity written under one surface's session
    reads back identically through the other surface's session;
  * sessions remember which surface minted them (origin stamped by
    the minting endpoint — the one place the backend KNOWS) and when
    they were last used;
  * the cross-surface continuity hint fires ONLY from those stored
    facts: his latest pre-turn activity was on the other surface,
    within 24h. Legacy sessions, single-surface users, and the
    synthetic Telegram group/event accounts (shared, not a person)
    never produce a hint;
  * the chat endpoint turns a live hint into context
    (cross_surface_line) the brain renders into her system text.

Run:  python -m pytest tests/test_cross_surface_identity.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from app import brain, characters, character_trust, config, db, store  # noqa: E402
from app import main as api  # noqa: E402

TG_USER = "tg_777001"
EVENT_USER = "tg_900000000123"  # synthetic group/event account (cap 3)


class _Client:
    host = "127.0.0.1"


class _Req:
    """Fake request with a state bag, like Starlette's."""

    def __init__(self, token=None):
        self.headers = {"authorization": f"Bearer {token}"} if token else {}
        self.client = _Client()
        self.state = types.SimpleNamespace()


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_cache = characters._cache
        characters._cache = {"ts": time.time(), "chars": [], "local": []}

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        characters._cache = self._real_cache
        self.tmp.cleanup()

    def _conn(self):
        return db.connect(self.db_path)

    def _session(self, user_id, token, origin, last_seen: datetime):
        conn = self._conn()
        try:
            store.get_or_create_user(conn, user_id)
            store.create_session(conn, user_id, token, origin=origin)
            conn.execute(
                "UPDATE auth_sessions SET last_seen_at = ? "
                "WHERE token = ?", (_ts(last_seen), token))
            conn.commit()
        finally:
            conn.close()


class SessionOriginTest(_Base):
    def test_create_session_stamps_origin_and_last_seen(self):
        conn = self._conn()
        try:
            store.get_or_create_user(conn, TG_USER)
            store.create_session(conn, TG_USER, "tok-tg", origin="telegram")
            s = store.get_session(conn, "tok-tg")
            self.assertEqual(s["origin"], "telegram")
            self.assertTrue(s["last_seen_at"])
            store.create_session(conn, TG_USER, "tok-plain")
            self.assertEqual(store.get_session(conn, "tok-plain")["origin"], "")
        finally:
            conn.close()

    def test_telegram_auth_mints_telegram_origin(self):
        out = api.auth_telegram({"telegram_id": "777001", "name": "T"})
        conn = self._conn()
        try:
            s = store.get_session(conn, out["token"])
            self.assertEqual(s["user_id"], TG_USER)
            self.assertEqual(s["origin"], "telegram")
        finally:
            conn.close()

    def test_redeem_link_mints_web_origin_for_same_account(self):
        link = api.auth_telegram_link({"telegram_id": "777001", "name": "T"})
        out = api.auth_redeem_link({"code": link["code"]})
        conn = self._conn()
        try:
            s = store.get_session(conn, out["token"])
            self.assertEqual(s["user_id"], TG_USER)
            self.assertEqual(s["origin"], "web")
        finally:
            conn.close()

    def test_note_session_activity_returns_origin_and_previous_seen(self):
        old = datetime.utcnow() - timedelta(hours=3)
        self._session(TG_USER, "tok-a", "telegram", old)
        conn = self._conn()
        try:
            origin, prev = store.note_session_activity(conn, "tok-a")
            self.assertEqual(origin, "telegram")
            self.assertEqual(prev, _ts(old))
            after = store.get_session(conn, "tok-a")["last_seen_at"]
            self.assertGreater(after, _ts(old))
        finally:
            conn.close()


class IdenticalReadsTest(_Base):
    """Activity recorded under one surface's session reads back
    identically through the other surface's session — the reads are
    keyed by the account, never by the session that asks."""

    def setUp(self):
        super().setUp()
        now = datetime.utcnow()
        self._session(TG_USER, "tok-tg", "telegram", now)
        self._session(TG_USER, "tok-web", "web", now)
        conn = self._conn()
        try:
            user = store.get_or_create_user(conn, TG_USER)
            # Streak written the way the chat endpoint writes it
            # (dates in the user row's timezone) after "Telegram" use.
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(user.get("timezone") or "America/New_York")
            local = datetime.now(tz)
            store.touch_streak(
                conn, TG_USER, "chloe", local.strftime("%Y-%m-%d"),
                (local - timedelta(days=1)).strftime("%Y-%m-%d"))
            store.write_memory(conn, TG_USER, "fact",
                               "He is training for a marathon.",
                               character_id="chloe")
            brain._set_scene_state(conn, TG_USER, "chloe", "active",
                                   "rainy night in at her place")
        finally:
            conn.close()

    def test_streaks_identical_via_me_for_both_sessions(self):
        by_tg = api.auth_me(_Req("tok-tg"))
        by_web = api.auth_me(_Req("tok-web"))
        self.assertEqual(by_tg["streaks"], by_web["streaks"])
        self.assertEqual(by_web["streaks"]["chloe"]["current"], 1)

    def test_memory_trust_scene_reads_identical(self):
        conn = self._conn()
        try:
            user = store.get_or_create_user(conn, TG_USER)
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(user.get("timezone") or "America/New_York")
            local = datetime.now(tz)
            today = local.strftime("%Y-%m-%d")
            yday = (local - timedelta(days=1)).strftime("%Y-%m-%d")
            self.assertEqual(
                store.get_streak(conn, TG_USER, "chloe", today, yday), 1)
            mems = store.recall_memories(conn, TG_USER,
                                         character_id="chloe")
            self.assertTrue(any("marathon" in m["text"] for m in mems))
            state = character_trust.get_state(conn, TG_USER, "chloe")
            self.assertEqual(state["milestone"],
                             character_trust.trust.START_MILESTONE)
            scene = brain.get_scene_state(conn, TG_USER, "chloe")
            self.assertEqual(scene["state"], "active")
            self.assertIn("rainy night", scene["brief"])
        finally:
            conn.close()


class HintLogicTest(_Base):
    NOW = datetime(2026, 10, 19, 20, 0, 0)

    def _hint(self, user=TG_USER, surface="web", token="tok-web",
              prev_seen=""):
        conn = self._conn()
        try:
            return store.cross_surface_hint(
                conn, user, surface, token, prev_seen, now=self.NOW)
        finally:
            conn.close()

    def test_hint_when_other_surface_is_his_latest(self):
        self._session(TG_USER, "tok-web", "web", self.NOW - timedelta(hours=6))
        self._session(TG_USER, "tok-tg", "telegram",
                      self.NOW - timedelta(hours=2))
        hint = self._hint(prev_seen=_ts(self.NOW - timedelta(hours=6)))
        self.assertIsNotNone(hint)
        self.assertEqual(hint["surface"], "telegram")
        self.assertEqual(hint["last_seen_at"],
                         _ts(self.NOW - timedelta(hours=2)))

    def test_no_hint_when_this_surface_is_his_latest(self):
        self._session(TG_USER, "tok-web", "web", self.NOW - timedelta(hours=1))
        self._session(TG_USER, "tok-tg", "telegram",
                      self.NOW - timedelta(hours=2))
        self.assertIsNone(
            self._hint(prev_seen=_ts(self.NOW - timedelta(hours=1))))

    def test_no_hint_when_other_surface_is_stale(self):
        self._session(TG_USER, "tok-web", "web", self.NOW - timedelta(hours=6))
        self._session(TG_USER, "tok-tg", "telegram",
                      self.NOW - timedelta(hours=30))
        self.assertIsNone(
            self._hint(prev_seen=_ts(self.NOW - timedelta(hours=6))))

    def test_no_hint_for_single_surface_user(self):
        self._session(TG_USER, "tok-web", "web", self.NOW - timedelta(hours=6))
        self.assertIsNone(
            self._hint(prev_seen=_ts(self.NOW - timedelta(hours=6))))

    def test_no_hint_for_legacy_session(self):
        self._session(TG_USER, "tok-web", "", self.NOW - timedelta(hours=6))
        self._session(TG_USER, "tok-tg", "telegram",
                      self.NOW - timedelta(hours=2))
        self.assertIsNone(self._hint(surface="", token="tok-web",
                                     prev_seen=""))

    def test_no_hint_for_synthetic_event_account(self):
        self._session(EVENT_USER, "tok-web", "web",
                      self.NOW - timedelta(hours=6))
        self._session(EVENT_USER, "tok-tg", "telegram",
                      self.NOW - timedelta(hours=2))
        self.assertIsNone(self._hint(
            user=EVENT_USER, prev_seen=_ts(self.NOW - timedelta(hours=6))))

    def test_hint_reverse_direction(self):
        self._session(TG_USER, "tok-tg", "telegram",
                      self.NOW - timedelta(hours=6))
        self._session(TG_USER, "tok-web", "web", self.NOW - timedelta(hours=1))
        hint = self._hint(surface="telegram", token="tok-tg",
                          prev_seen=_ts(self.NOW - timedelta(hours=6)))
        self.assertIsNotNone(hint)
        self.assertEqual(hint["surface"], "web")


class ChatEndpointHintTest(_Base):
    """End to end through POST /api/v1/chat (called in-process with
    the brain's model seams stubbed): the continuity line lands in
    her context exactly when the stored session facts support it."""

    def setUp(self):
        super().setUp()
        self._captured = {}
        self._real_compose = brain.compose_reply
        self._real_context = brain.build_brain_context
        self._real_learn = brain.learn_from_turn
        from app import assist
        self._assist = assist
        self._real_assist = assist.maybe_assist

        def _compose(message, context, cid):
            self._captured["context"] = context
            return "a real reply"

        brain.compose_reply = _compose
        brain.build_brain_context = lambda conn, uid, user, msg, char: {}
        brain.learn_from_turn = lambda conn, uid, msg, cid: None
        assist.maybe_assist = lambda conn, user, msg, cid: None

    def tearDown(self):
        brain.compose_reply = self._real_compose
        brain.build_brain_context = self._real_context
        brain.learn_from_turn = self._real_learn
        self._assist.maybe_assist = self._real_assist
        super().tearDown()

    def _user_with_bank(self, user_id):
        conn = self._conn()
        try:
            store.get_or_create_user(conn, user_id)
            conn.execute(
                "UPDATE users SET messages_remaining = 5 "
                "WHERE user_id = ?", (user_id,))
            conn.commit()
        finally:
            conn.close()

    def _chat(self, token):
        body = api.ChatIn(message="hey, it's me", character_id="chloe")
        return api.chat(body, _Req(token))

    def test_line_present_after_surface_switch(self):
        now = datetime.utcnow()
        self._user_with_bank(TG_USER)
        self._session(TG_USER, "tok-web", "web", now - timedelta(hours=6))
        self._session(TG_USER, "tok-tg", "telegram", now - timedelta(hours=2))
        resp = self._chat("tok-web")
        self.assertEqual(resp["reply_text"], "a real reply")
        line = self._captured["context"].get("cross_surface_line", "")
        self.assertIn("Telegram", line)
        # ...and her system text renders it.
        text = brain._gemini_system_text(
            {"cross_surface_line": line, "character": {"id": "chloe"}},
            "chloe")
        self.assertIn("Continuity:", text)

    def test_no_line_when_other_surface_stale(self):
        now = datetime.utcnow()
        self._user_with_bank(TG_USER)
        self._session(TG_USER, "tok-web", "web", now - timedelta(hours=6))
        self._session(TG_USER, "tok-tg", "telegram",
                      now - timedelta(hours=30))
        self._chat("tok-web")
        self.assertNotIn("cross_surface_line", self._captured["context"])

    def test_no_line_for_synthetic_event_account(self):
        now = datetime.utcnow()
        self._user_with_bank(EVENT_USER)
        self._session(EVENT_USER, "tok-web", "web", now - timedelta(hours=6))
        self._session(EVENT_USER, "tok-tg", "telegram",
                      now - timedelta(hours=2))
        self._chat("tok-web")
        self.assertNotIn("cross_surface_line", self._captured["context"])


if __name__ == "__main__":
    unittest.main()
