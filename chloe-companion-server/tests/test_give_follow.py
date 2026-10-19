"""Improvement #3 (2026-10-19): gives follow the conversation.

A give lands mid-conversation, so its stage and her line read the
conversation it lands in:

  * scene_stage_hint reads the active roleplay scene (brain's
    get_scene_state) and his recent turns through the one
    ask->stage mapping (live_give.stage_for_ask_text) — her own
    turns never escalate a pick;
  * stage selection (preset endpoint walk, live-give progression)
    continues the hinted stage instead of the bare counter, clamped
    to store.give_stage_cap — under scene pressure a group/event
    account still never serves past stage 3;
  * give_caption generates one in-character line referencing what
    they were just talking about, and falls back to the caller's
    standing line on ANY failure (model down, policed, no
    conversation, synthetic event account).

The model seam (brain.gemini_generate) is mocked throughout.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import brain, characters, config, db, live_give, store  # noqa: E402
from app import main as api  # noqa: E402

USER = "user_1"
EVENT_USER = "tg_900000000123"  # synthetic group/event account (cap 3)


def _db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db._migrate_character_columns(conn)
    db._migrate_auth_columns(conn)
    db._migrate_submission_columns(conn)
    db._migrate_creator_columns(conn)
    db._migrate_owner_column(conn)
    db._migrate_referral_columns(conn)
    db._migrate_conversation_attachments(conn)
    db._migrate_girl_media_stage(conn)
    db._migrate_roleplay_scenes_table(conn)
    conn.executescript(db.SCHEMA)
    return conn


def _seed_girl_media(conn, girl="chloe", stages=(1, 2, 3, 4, 5, 6, 7, 8, 9)):
    for st in stages:
        conn.execute(
            "INSERT INTO girl_media (girl_id, url, kind, preset, stage)"
            " VALUES (?, ?, 'give', 'give', ?)",
            (girl, f"https://cdn.test/{girl}-stage{st}.mp4", st))
    conn.commit()


class HintTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def tearDown(self):
        self.conn.close()

    def test_hint_from_active_scene_brief(self):
        brain._set_scene_state(
            self.conn, USER, "chloe", "active",
            "you are a lifeguard at the pool and I am sinking — "
            "take off your shirt and dive in")
        self.assertEqual(
            live_give.scene_stage_hint(self.conn, USER, "chloe"), 2)

    def test_hint_from_recent_user_turns(self):
        store.log_turn(self.conn, USER, "user",
                       "hey, take your top off for me", "chloe")
        store.log_turn(self.conn, USER, "chloe", "mmm, maybe…", "chloe")
        self.assertEqual(
            live_give.scene_stage_hint(self.conn, USER, "chloe"), 2)

    def test_hint_ignores_her_turns(self):
        store.log_turn(self.conn, USER, "chloe",
                       "you love my nipples, don't you", "chloe")
        self.assertIsNone(
            live_give.scene_stage_hint(self.conn, USER, "chloe"))

    def test_hint_none_without_conversation(self):
        self.assertIsNone(
            live_give.scene_stage_hint(self.conn, USER, "chloe"))

    def test_hint_ignores_awaiting_scene(self):
        brain._set_scene_state(self.conn, USER, "chloe", "awaiting",
                               "take off your shirt")
        self.assertIsNone(
            live_give.scene_stage_hint(self.conn, USER, "chloe"))


class NextStageTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def tearDown(self):
        self.conn.close()

    def test_next_stage_follows_the_scene(self):
        brain._set_scene_state(
            self.conn, USER, "chloe", "active",
            "we are in bed at the cabin, I want you to fuck me")
        # Progression alone would serve stage 1; the scene is at 9.
        self.assertEqual(live_give._next_stage(self.conn, USER, "chloe"), 9)

    def test_next_stage_progression_without_scene(self):
        self.assertEqual(live_give._next_stage(self.conn, USER, "chloe"), 1)
        self.conn.execute(
            "INSERT INTO user_give_progress (user_id, girl_id,"
            " gives_received) VALUES (?, 'chloe', 3)", (USER,))
        self.conn.commit()
        self.assertEqual(live_give._next_stage(self.conn, USER, "chloe"), 4)

    def test_group_cap_under_scene_pressure_next_stage(self):
        brain._set_scene_state(
            self.conn, EVENT_USER, "chloe", "active",
            "we are in bed at the cabin, I want you to fuck me")
        self.assertEqual(
            live_give._next_stage(self.conn, EVENT_USER, "chloe"), 3)

    def test_group_cap_under_scene_pressure_pick(self):
        _seed_girl_media(self.conn)
        pick = live_give._pick_preset(
            self.conn, EVENT_USER, "chloe", "video", want_stage=9)
        self.assertIsNotNone(pick)
        self.assertLessEqual(pick["stage"], 3)


class CaptionTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self.calls = []

    def tearDown(self):
        self.conn.close()

    def _fake_generate(self, result):
        def fake(prompt, context, character_id="chloe"):
            self.calls.append((prompt, context, character_id))
            return result
        return fake

    def test_caption_references_the_scene(self):
        brain._set_scene_state(
            self.conn, USER, "chloe", "active",
            "you are a lifeguard at the pool and I am sinking")
        with mock.patch.object(
                brain, "gemini_generate",
                self._fake_generate("right here in the pool with you, "
                                    "just like you set it up, babe")):
            line = live_give.give_caption(self.conn, USER, "chloe",
                                          fallback="generic")
        self.assertEqual(
            line, "right here in the pool with you, "
                  "just like you set it up, babe")
        prompt, context, cid = self.calls[0]
        self.assertIn("lifeguard", prompt)
        self.assertEqual(cid, "chloe")

    def test_caption_from_recent_turns(self):
        store.log_turn(self.conn, USER, "user",
                       "that story about your neighbor's dog was wild",
                       "chloe")
        store.log_turn(self.conn, USER, "chloe",
                       "he ate the whole fence, I swear", "chloe")
        with mock.patch.object(
                brain, "gemini_generate",
                self._fake_generate("still thinking about that dog, "
                                    "aren't you — here, babe")):
            line = live_give.give_caption(self.conn, USER, "chloe",
                                          fallback="generic")
        self.assertTrue(line.startswith("still thinking about that dog"))
        prompt, context, _ = self.calls[0]
        self.assertTrue(context["recent"])

    def test_caption_policed_line_falls_back(self):
        store.log_turn(self.conn, USER, "user", "hey you", "chloe")
        with mock.patch.object(
                brain, "gemini_generate",
                self._fake_generate("ugh, I'm just on repeat again")):
            line = live_give.give_caption(self.conn, USER, "chloe",
                                          fallback="generic")
        self.assertEqual(line, "generic")

    def test_caption_model_down_falls_back(self):
        store.log_turn(self.conn, USER, "user", "hey you", "chloe")
        with mock.patch.object(brain, "gemini_generate",
                               self._fake_generate(None)):
            line = live_give.give_caption(self.conn, USER, "chloe",
                                          fallback="generic")
        self.assertEqual(line, "generic")

    def test_caption_no_conversation_falls_back_without_model(self):
        with mock.patch.object(
                brain, "gemini_generate",
                self._fake_generate("unused")):
            line = live_give.give_caption(self.conn, USER, "chloe",
                                          fallback="generic")
        self.assertEqual(line, "generic")
        self.assertEqual(self.calls, [])  # the model was never called

    def test_caption_event_account_never_generates(self):
        brain._set_scene_state(
            self.conn, EVENT_USER, "chloe", "active",
            "we are in bed at the cabin, I want you to fuck me")
        with mock.patch.object(
                brain, "gemini_generate",
                self._fake_generate("should never be used")):
            line = live_give.give_caption(self.conn, EVENT_USER, "chloe",
                                          fallback="generic")
        self.assertEqual(line, "generic")
        self.assertEqual(self.calls, [])


class _Req:
    def __init__(self, token):
        self.headers = {"authorization": f"Bearer {token}"}
        self.base_url = "https://example.test/"


def _char(cid, name, owner=None):
    return {"id": cid, "name": name, "avatar_url": "", "persona": "",
            "companion_voice": "", "blurb": "", "door_title": "",
            "sort_order": 1, "owner_user_id": owner}


class PresetVideoSceneTest(unittest.TestCase):
    """End to end through POST /api/v1/preset-video's handler: the
    stage walk anchors on the conversation, and the response carries
    her caption line."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_cache = characters._cache
        characters._cache = {"ts": time.time(), "chars": [], "local": [
            _char("chloe", "Chloe")]}
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, USER, "Matty")
            self.token = "tok-user"
            store.create_session(conn, USER, self.token)
            _seed_girl_media(conn, stages=(1, 2, 3))
        finally:
            conn.close()

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        characters._cache = self._real_cache
        self.tmp.cleanup()

    def _call(self):
        return asyncio.run(api.preset_video(
            api.PresetVideoIn(girl_id="chloe", preset="give"),
            _Req(self.token)))

    def test_stage_follows_scene_and_caption_rides_along(self):
        conn = db.connect(self.db_path)
        try:
            brain._set_scene_state(
                conn, USER, "chloe", "active",
                "you are a lifeguard at the pool — take off your shirt "
                "and dive in")
        finally:
            conn.close()
        with mock.patch.object(
                brain, "gemini_generate",
                lambda prompt, context, character_id="chloe":
                    "the pool's ours now, lifeguard — come here"):
            resp = self._call()
        # Progression alone would serve stage 1; the scene is at 2.
        self.assertEqual(resp["stage"], 2)
        self.assertEqual(resp["video_url"],
                         "https://cdn.test/chloe-stage2.mp4")
        self.assertEqual(resp["caption"],
                         "the pool's ours now, lifeguard — come here")

    def test_no_scene_keeps_progression_and_no_caption(self):
        with mock.patch.object(
                brain, "gemini_generate",
                lambda prompt, context, character_id="chloe": "unused"):
            resp = self._call()
        self.assertEqual(resp["stage"], 1)
        self.assertEqual(resp["video_url"],
                         "https://cdn.test/chloe-stage1.mp4")
        self.assertNotIn("caption", resp)


if __name__ == "__main__":
    unittest.main()
