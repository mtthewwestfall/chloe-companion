"""Fallback picker + size-label fix (2026-10-14).

Production showed two defects in the preset fallback both callers
share (live_give's render-miss fallback and custom_video's honest
close):

1. _pick_preset ignored the request entirely — it served wherever
   the user's give progression sat, so "take off your top" was
   answered with a stage-8 clip because the walk sat at 8. The pick
   is now ask-aware: the ask's stage is derived from its text
   (live_give.stage_for_ask_text — one mapping, shared by the live
   hook over his message and by custom_video over the request
   description), and the picker serves that stage, else the nearest
   stage (below before above). No derivable stage -> the legacy
   progression walk, unchanged.
2. Preset deliveries recorded size 0, so real tens-of-MB clips
   rendered as "0 B". Deliveries now record the file's real size
   when it resolves locally and omit the size when it cannot be
   determined; the client's formatBytes renders a missing/zero size
   as no label instead of a false "0 B".
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, characters, config, custom_video, db, give_gen  # noqa: E402
from app import live_give, store  # noqa: E402

OWNER_ID = "owner_1"
USER = "user_1"
TOKENS = {"token": "tok-aaa", "refreshToken": "ref-bbb"}


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
    db._migrate_give_gen_source(conn)
    db._migrate_custom_video_columns(conn)
    conn.executescript(db.SCHEMA)
    return conn


class _NoClose:
    def __init__(self, conn):
        self._c = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._c, name)


def _seed_media(conn, girl, url, stage, kind="give"):
    cur = conn.execute(
        "INSERT INTO girl_media (girl_id, kind, url, preset, stage) "
        "VALUES (?, ?, ?, 'give', ?)", (girl, kind, url, stage))
    conn.commit()
    return int(cur.lastrowid)


def _set_progress(conn, user, girl, gives):
    conn.execute(
        "INSERT INTO user_give_progress (user_id, girl_id, gives_received) "
        "VALUES (?, ?, ?)", (user, girl, gives))
    conn.commit()


def _turns(conn, user, girl):
    return [dict(r) for r in conn.execute(
        "SELECT role, text, attachments FROM conversation "
        "WHERE user_id = ? AND character_id = ?",
        (user, girl)).fetchall()]


class StageForAskTextTest(unittest.TestCase):
    def test_production_asks_map_to_their_stage(self):
        cases = {
            # the asks production actually served wrong
            "Take off your top": 2,
            "Topless video": 2,
            "Send me a video of you topless": 2,
            "Hannah taking off her top to expose her breasts": 2,
            "Hannah pinching her nipples": 4,
            "Suck my dick and moan my name": 8,
            "Have her suck my dick": 8,
            # the canonical live-give example from the routing fix
            "make me a video of you taking off your bra": 2,
        }
        for text, stage in cases.items():
            self.assertEqual(live_give.stage_for_ask_text(text), stage,
                             text)

    def test_stage_descriptions_map_to_their_own_stage(self):
        for stage in range(1, 10):
            desc = give_gen.stage_description_for("brooklyn", stage)
            self.assertEqual(live_give.stage_for_ask_text(desc), stage,
                             desc)

    def test_peak_of_the_ask_wins(self):
        self.assertEqual(
            live_give.stage_for_ask_text(
                "take off your top and play with yourself"), 6)
        self.assertEqual(
            live_give.stage_for_ask_text("suck my cock then fuck me"), 9)

    def test_titty_fuck_is_seven_not_nine(self):
        self.assertEqual(live_give.stage_for_ask_text("titty fuck"), 7)
        self.assertEqual(
            live_give.stage_for_ask_text("titty fuck me, baby"), 7)

    def test_no_stage_named_returns_none(self):
        for text in ("make me one", "send me something new",
                     "how was your day", "dancing slowly in a red dress",
                     "show me your thighs", "", "   "):
            self.assertIsNone(live_give.stage_for_ask_text(text), text)


class _PickBase(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def tearDown(self):
        self.conn.close()


class AskAwarePickTest(_PickBase):
    def test_ask_stage_beats_progression(self):
        # Progression at 8 (gives 7 -> next stage 8): the legacy walk
        # serves the stage-8 clip; the ask-aware pick serves the ask.
        _set_progress(self.conn, OWNER_ID, "brooklyn", 7)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s3.mp4", 3)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s8.mp4", 8)
        legacy = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video")
        self.assertEqual(legacy["url"], "/media/brooklyn-s8.mp4")
        asked = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video", 3)
        self.assertEqual(asked["url"], "/media/brooklyn-s3.mp4")
        self.assertEqual(asked["stage"], 3)

    def test_nearest_stage_below_before_above(self):
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s4.mp4", 4)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s6.mp4", 6)
        pick = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video", 5)
        self.assertEqual(pick["url"], "/media/brooklyn-s4.mp4")

    def test_nearest_stage_above_when_nothing_below(self):
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s7.mp4", 7)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s9.mp4", 9)
        pick = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video", 5)
        self.assertEqual(pick["url"], "/media/brooklyn-s7.mp4")

    def test_kind_preference_within_stage(self):
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s3.jpg", 3)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s3.mp4", 3)
        video = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video", 3)
        self.assertEqual(video["url"], "/media/brooklyn-s3.mp4")
        image = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "image", 3)
        self.assertEqual(image["url"], "/media/brooklyn-s3.jpg")

    def test_unseen_preference_within_stage(self):
        first = _seed_media(self.conn, "brooklyn",
                            "/media/brooklyn-s3a.mp4", 3)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s3b.mp4", 3)
        self.conn.execute(
            "INSERT INTO user_give_seen (user_id, girl_id, media_id, "
            "stage) VALUES (?, 'brooklyn', ?, 3)", (OWNER_ID, first))
        self.conn.commit()
        pick = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video", 3)
        self.assertEqual(pick["url"], "/media/brooklyn-s3b.mp4")

    def test_all_seen_falls_back_to_first_by_id(self):
        first = _seed_media(self.conn, "brooklyn",
                            "/media/brooklyn-s3a.mp4", 3)
        second = _seed_media(self.conn, "brooklyn",
                             "/media/brooklyn-s3b.mp4", 3)
        for mid in (first, second):
            self.conn.execute(
                "INSERT INTO user_give_seen (user_id, girl_id, "
                "media_id, stage) VALUES (?, 'brooklyn', ?, 3)",
                (OWNER_ID, mid))
        self.conn.commit()
        pick = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video", 3)
        self.assertEqual(pick["url"], "/media/brooklyn-s3a.mp4")

    def test_out_of_range_stage_is_legacy(self):
        _set_progress(self.conn, OWNER_ID, "brooklyn", 7)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s3.mp4", 3)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s8.mp4", 8)
        for bad in (0, 10, -1):
            pick = live_give._pick_preset(
                self.conn, OWNER_ID, "brooklyn", "video", bad)
            self.assertEqual(pick["url"], "/media/brooklyn-s8.mp4", bad)

    def test_ask_pick_never_crosses_girls(self):
        _seed_media(self.conn, "mia", "/media/mia-s3.mp4", 3)
        pick = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video", 3)
        self.assertIsNone(pick)


class LegacyPickTest(_PickBase):
    def test_no_stage_info_reproduces_progression_walk(self):
        # Progress stage 2, media only at stage 5: the legacy walk
        # goes UP from 2 and serves stage 5.
        _set_progress(self.conn, OWNER_ID, "brooklyn", 1)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s5.mp4", 5)
        pick = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video")
        self.assertEqual(pick["url"], "/media/brooklyn-s5.mp4")

    def test_no_stage_info_wraps_around(self):
        # Progress stage 8, media only below it: the walk wraps to 1..
        _set_progress(self.conn, OWNER_ID, "brooklyn", 7)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s2.mp4", 2)
        pick = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video")
        self.assertEqual(pick["url"], "/media/brooklyn-s2.mp4")


class StaticLibraryPickTest(_PickBase):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="picker-static-")
        self.media_dir = os.path.join(
            self.root, "chloe-companion-app", "media")
        os.makedirs(self.media_dir)
        for name in ("brooklyn-give-stage3.mp4",
                     "brooklyn-give-stage8.mp4",
                     "mia-give-stage3.mp4"):
            with open(os.path.join(self.media_dir, name), "wb") as f:
                f.write(b"x" * 64)
        self._data_patch = mock.patch.object(
            config, "DATA_DIR", os.path.join(self.root, "data"))
        self._data_patch.start()

    def tearDown(self):
        self._data_patch.stop()
        super().tearDown()

    def test_ask_pick_uses_static_library_by_stage(self):
        pick = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video", 3)
        self.assertEqual(pick["url"], "/media/brooklyn-give-stage3.mp4")
        pick = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video", 6)
        self.assertEqual(pick["url"], "/media/brooklyn-give-stage8.mp4")


class LiveJobAskStageTest(unittest.TestCase):
    """The live job threads the trigger text's stage into its
    fallback: progression sits at 8, the ask names stage 2, and the
    stage-2 preset is what lands."""

    def setUp(self):
        self._env = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = OWNER_ID
        live_give._reset_state_for_tests()
        self.conn = _db()
        self._nc = _NoClose(self.conn)
        fd, self.ref_path = tempfile.mkstemp(suffix=".jpg")
        os.write(fd, b"fake-ref")
        os.close(fd)
        failed = ({"status": "failed", "project_id": None,
                   "path": None, "detail": "5000"}, None)
        self.patches = [
            mock.patch.object(config, "DATA_DIR",
                              tempfile.mkdtemp(prefix="picker-data-")),
            mock.patch.object(db, "connect", return_value=self._nc),
            mock.patch.object(
                characters, "get_character",
                side_effect=lambda cid, user_id=None: {
                    "id": cid, "name": cid.title(),
                    "avatar_url": "http://x/avatar.jpg"}),
            mock.patch.object(assist, "stored_sogni_session",
                              return_value=dict(TOKENS)),
            mock.patch.object(assist, "_fetch_first_frame",
                              return_value=self.ref_path),
            mock.patch.object(assist, "_sogni_plan_run",
                              return_value=failed),
            mock.patch.object(give_gen, "_hardwired_login",
                              return_value=None),
            mock.patch.object(give_gen, "_hardwired_sogni_session",
                              return_value=None),
            mock.patch.object(give_gen, "_topless_reference_frame",
                              return_value=None),
        ]
        for p in self.patches:
            p.start()
        _set_progress(self.conn, OWNER_ID, "brooklyn", 7)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s2.mp4", 2)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s8.mp4", 8)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.conn.close()
        try:
            os.unlink(self.ref_path)
        except OSError:
            pass
        if self._env is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._env

    def test_failed_render_delivers_the_asked_stage(self):
        live_give._run_live_give_job(
            OWNER_ID, "brooklyn", "video", 8, "take off your top for me")
        turns = _turns(self.conn, OWNER_ID, "brooklyn")
        self.assertEqual(len(turns), 1)
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["url"], "/media/brooklyn-s2.mp4")

    def test_failed_render_without_stage_words_keeps_progression(self):
        live_give._run_live_give_job(
            OWNER_ID, "brooklyn", "video", 8, "make me one")
        turns = _turns(self.conn, OWNER_ID, "brooklyn")
        self.assertEqual(len(turns), 1)
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["url"], "/media/brooklyn-s8.mp4")


class CustomVideoAskStageTest(_PickBase):
    def setUp(self):
        super().setUp()
        store.get_or_create_user(self.conn, USER)
        self.conn.commit()
        _set_progress(self.conn, USER, "brooklyn", 7)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s2.mp4", 2)
        _seed_media(self.conn, "brooklyn", "/media/brooklyn-s8.mp4", 8)

    def _deliver(self, description):
        req = {"id": 1, "user_id": USER, "girl_id": "brooklyn",
               "description": description}
        return custom_video._deliver_preset_fallback(self.conn, req)

    def test_description_stage_drives_the_fallback(self):
        url = self._deliver("Take off your top")
        self.assertEqual(url, "/media/brooklyn-s2.mp4")
        turns = _turns(self.conn, USER, "brooklyn")
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["url"], "/media/brooklyn-s2.mp4")

    def test_description_without_stage_keeps_progression(self):
        url = self._deliver("dancing slowly in a red dress")
        self.assertEqual(url, "/media/brooklyn-s8.mp4")


class PresetSizeTest(_PickBase):
    def setUp(self):
        super().setUp()
        self.gen_dir = tempfile.mkdtemp(prefix="picker-gen-")
        self._gen_patch = mock.patch.object(
            config, "GENERATED_DIR", self.gen_dir)
        self._gen_patch.start()
        self.root = tempfile.mkdtemp(prefix="picker-size-")
        self.media_dir = os.path.join(
            self.root, "chloe-companion-app", "media")
        os.makedirs(self.media_dir)
        self._data_patch = mock.patch.object(
            config, "DATA_DIR", os.path.join(self.root, "data"))
        self._data_patch.start()

    def tearDown(self):
        self._gen_patch.stop()
        self._data_patch.stop()
        super().tearDown()

    def _write(self, directory, name, payload):
        with open(os.path.join(directory, name), "wb") as f:
            f.write(payload)

    def _deliver(self, preset):
        live_give._deliver_preset(
            self.conn, OWNER_ID, "brooklyn", preset, during_render=False)
        turns = _turns(self.conn, OWNER_ID, "brooklyn")
        self.assertEqual(len(turns), 1)
        return json.loads(turns[0]["attachments"])[0]

    def test_generated_file_size_recorded(self):
        self._write(self.gen_dir, "fresh-1.mp4", b"v" * 12345)
        att = self._deliver({"id": None, "url": "/generated/fresh-1.mp4",
                             "media_type": "video", "stage": 3})
        self.assertEqual(att["size"], 12345)

    def test_static_file_size_recorded(self):
        self._write(self.media_dir, "brooklyn-give-stage2.mp4",
                    b"v" * 777)
        att = self._deliver(
            {"id": None, "url": "/media/brooklyn-give-stage2.mp4",
             "media_type": "video", "stage": 2})
        self.assertEqual(att["size"], 777)

    def test_full_url_resolves_to_local_file(self):
        self._write(self.media_dir, "brooklyn-give-stage2.mp4",
                    b"v" * 777)
        att = self._deliver(
            {"id": None,
             "url": "https://companion.lockeddoor.ai/media/"
                    "brooklyn-give-stage2.mp4",
             "media_type": "video", "stage": 2})
        self.assertEqual(att["size"], 777)

    def test_remote_only_url_omits_size(self):
        att = self._deliver(
            {"id": None, "url": "https://example.com/media/whatever.mp4",
             "media_type": "video", "stage": 2})
        self.assertNotIn("size", att)

    def test_missing_local_file_omits_size(self):
        att = self._deliver({"id": None, "url": "/generated/nope.mp4",
                             "media_type": "video", "stage": 3})
        self.assertNotIn("size", att)

    def test_picked_static_preset_delivers_with_real_size(self):
        payload = b"v" * 4321
        self._write(self.media_dir, "brooklyn-give-stage3.mp4", payload)
        preset = live_give._pick_preset(
            self.conn, OWNER_ID, "brooklyn", "video", 3)
        self.assertIsNotNone(preset)
        att = self._deliver(preset)
        self.assertEqual(att["size"], len(payload))


class ClientContractTest(unittest.TestCase):
    def test_format_bytes_renders_no_label_for_unknown_size(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "chloe-companion-app", "js", "app.js")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        idx = src.index("function formatBytes")
        self.assertIn("if (n <= 0) return '';", src[idx:idx + 200])


if __name__ == "__main__":
    unittest.main()
