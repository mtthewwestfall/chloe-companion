"""Voice notes with a memory (improvement #5, 2026-10-20).

The Her Day voice note is the one thing she proactively SAYS to a
user out loud. Its spoken text must cite ONE real detail from that
user's actual relationship with her — the active roleplay scene,
their streak (the store's own count), a stored memory fact, or his
name — read from the same stores chat reads. Nothing stored → the
spoken text is the caption verbatim. Synthetic Telegram group/event
accounts (shared surfaces) are never personalized. The feed caption
itself never changes; only the spoken words do.

Run:  python -m unittest tests.test_her_day_voice_memory -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import brain, config, db, her_day, proactive, store  # noqa: E402

CAPTION = "okay I need to vent about my day"

_SCENES_DDL = """CREATE TABLE IF NOT EXISTS roleplay_scenes (
    user_id      TEXT NOT NULL,
    character_id TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'awaiting',
    brief        TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, character_id))"""


def _db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    conn.execute(_SCENES_DDL)
    return conn


def _mk_user(conn, uid, name=None, timezone="UTC"):
    conn.execute(
        "INSERT INTO users (user_id, name, timezone) VALUES (?, ?, ?)",
        (uid, name, timezone))
    conn.commit()


def _build_streak(conn, uid, gid, days, tz="UTC"):
    """A real streak of `days` ending on the user's local today,
    built through the streak store itself. Returns the store count."""
    local = proactive._user_local_now(tz)
    for i in reversed(range(days)):
        d = local - timedelta(days=i)
        store.touch_streak(conn, uid, gid, d.strftime("%Y-%m-%d"),
                           (d - timedelta(days=1)).strftime("%Y-%m-%d"))
    return store.get_streak(
        conn, uid, gid, local.strftime("%Y-%m-%d"),
        (local - timedelta(days=1)).strftime("%Y-%m-%d"))


class VoiceMemoryDetailTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def tearDown(self):
        self.conn.close()

    def _spoken(self, uid, gid="chloe"):
        return her_day._voice_note_text(self.conn, uid, gid, CAPTION)

    def test_scene_is_cited(self):
        _mk_user(self.conn, "u_scene", name="Dan")
        brain._set_scene_state(self.conn, "u_scene", "chloe", "active",
                               "we're slow dancing in a cabin kitchen")
        out = self._spoken("u_scene")
        self.assertIn("that scene of ours", out)
        self.assertIn("slow dancing in a cabin kitchen", out)
        self.assertTrue(out.endswith("vent about my day"))

    def test_scene_wins_over_streak_and_memory(self):
        _mk_user(self.conn, "u_prio", name="Dan")
        brain._set_scene_state(self.conn, "u_prio", "chloe", "active",
                               "a midnight drive along the coast")
        self.assertEqual(_build_streak(self.conn, "u_prio", "chloe", 9), 9)
        store.write_memory(self.conn, "u_prio", "preference",
                           "He likes craft beer.", character_id="chloe")
        kind, _opener = her_day._voice_memory_detail(
            self.conn, "u_prio", "chloe")
        self.assertEqual(kind, "scene")

    def test_awaiting_scene_is_not_cited(self):
        _mk_user(self.conn, "u_wait")
        brain._set_scene_state(self.conn, "u_wait", "chloe", "awaiting",
                               "a midnight drive along the coast")
        self.assertEqual(self._spoken("u_wait"), CAPTION)

    def test_streak_count_matches_store(self):
        _mk_user(self.conn, "u_streak")
        n = _build_streak(self.conn, "u_streak", "chloe", 12)
        self.assertEqual(n, 12)
        out = self._spoken("u_streak")
        self.assertIn(f"Day {n} of us talking every single day", out)

    def test_short_streak_is_not_cited(self):
        _mk_user(self.conn, "u_short")
        self.assertEqual(_build_streak(self.conn, "u_short", "chloe", 2), 2)
        self.assertEqual(self._spoken("u_short"), CAPTION)

    def test_memory_preference_is_cited(self):
        _mk_user(self.conn, "u_mem")
        store.write_memory(self.conn, "u_mem", "preference",
                           "He likes craft beer.", character_id="chloe")
        out = self._spoken("u_mem")
        self.assertIn("how much you like craft beer", out)

    def test_shared_memory_is_cited(self):
        _mk_user(self.conn, "u_shared")
        store.write_memory(
            self.conn, "u_shared", "fact",
            "He shared: I have a big presentation on Friday.",
            character_id="chloe")
        out = self._spoken("u_shared")
        self.assertIn("what you told me", out)
        self.assertIn("big presentation on Friday", out)

    def test_memory_of_another_girl_is_not_cited(self):
        _mk_user(self.conn, "u_iso")
        store.write_memory(self.conn, "u_iso", "preference",
                           "He likes craft beer.", character_id="mara")
        self.assertEqual(self._spoken("u_iso", "chloe"), CAPTION)

    def test_boundary_memory_is_not_cited(self):
        _mk_user(self.conn, "u_bound")
        store.write_memory(self.conn, "u_bound", "boundary",
                           "He asked: don't bring up his ex.",
                           character_id="chloe")
        self.assertEqual(self._spoken("u_bound"), CAPTION)

    def test_account_name_is_cited_when_nothing_else(self):
        _mk_user(self.conn, "u_name", name="Marcus Lee")
        out = self._spoken("u_name")
        self.assertTrue(out.startswith("Hey Marcus. "))

    def test_memory_name_beats_account_name(self):
        _mk_user(self.conn, "u_rn", name="Bob Jones")
        store.write_memory(self.conn, "u_rn", "core",
                           "His name is Robert.", character_id="chloe")
        out = self._spoken("u_rn")
        self.assertTrue(out.startswith("Hey Robert. "))

    def test_no_detail_returns_caption_verbatim(self):
        _mk_user(self.conn, "u_plain")  # no name, nothing stored
        self.assertEqual(self._spoken("u_plain"), CAPTION)
        _mk_user(self.conn, "u_mail", name="someone@example.com")
        self.assertEqual(self._spoken("u_mail"), CAPTION)

    def test_synthetic_event_account_is_never_personalized(self):
        uid = "tg_900000000001"
        _mk_user(self.conn, uid, name="Group Chat")
        brain._set_scene_state(self.conn, uid, "chloe", "active",
                               "a midnight drive along the coast")
        self.assertEqual(_build_streak(self.conn, uid, "chloe", 12), 12)
        store.write_memory(self.conn, uid, "preference",
                           "He likes craft beer.", character_id="chloe")
        self.assertEqual(self._spoken(uid), CAPTION)
        self.assertIsNone(
            her_day._voice_memory_detail(self.conn, uid, "chloe"))

    def test_private_telegram_user_still_personalized(self):
        uid = "tg_123456789"  # a normal DM account, not a group surface
        _mk_user(self.conn, uid, name="Sam")
        out = self._spoken(uid)
        self.assertTrue(out.startswith("Hey Sam. "))


class VoicePostIntegrationTest(unittest.TestCase):
    """End to end through generate_for_user_girl: the TTS receives
    the personalized text; the saved post keeps the plain caption."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test.db")
        db.init_db(self.db_path)
        self._old_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._old_gen = config.GENERATED_DIR
        config.GENERATED_DIR = self._tmp.name

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        config.GENERATED_DIR = self._old_gen
        self._tmp.cleanup()

    def test_voice_post_speaks_the_memory_not_the_caption(self):
        conn = db.connect(self.db_path)
        try:
            _mk_user(conn, "u_int")
            self.assertEqual(_build_streak(conn, "u_int", "chloe", 12), 12)
        finally:
            conn.close()
        heard = {}

        def fake_tts(text, user_id, character_id=""):
            heard["text"] = text
            heard["user_id"] = user_id
            return "/generated/fake-voice.wav"

        env = {k: v for k, v in os.environ.items()
               if k != "GEMINI_PROXY_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("app.voice.maybe_generate_voice_note",
                           side_effect=fake_tts):
            posts = her_day.generate_for_user_girl(
                "u_int", "chloe", "Chloe",
                plan=[("voice_rant", False, [], [])],
                girl_map={"chloe": {"id": "chloe", "name": "Chloe",
                                    "avatar_url": ""}},
                date_str="2026-10-20")
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["post_type"], "voice")
        self.assertEqual(post["voice_url"], "/generated/fake-voice.wav")
        # She SAID the streak — with the store's own count …
        self.assertIn("Day 12 of us talking every single day",
                      heard["text"])
        # … and the rant itself followed.
        self.assertIn("vent about my day", heard["text"])
        # The feed caption is untouched by the personalization.
        self.assertNotIn("Day 12", post["caption"] or "")


if __name__ == "__main__":
    unittest.main()
