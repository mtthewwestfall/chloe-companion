"""Live give creation (2026-10-14): the four private girls create a
fresh give scene in a heated owner chat instead of only serving
presets. The render seam is mocked throughout — no real renders."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, brain, characters, config, db, give_gen, live_give, store  # noqa: E402
from app import main as api  # noqa: E402

OWNER_ID = "owner_1"
TOKENS = {"token": "tok-aaa", "refreshToken": "ref-bbb"}
TOKENS2 = {"token": "tok-ccc", "refreshToken": "ref-ddd"}
GIRLS = ("brooklyn", "hannah", "jordan", "mia")


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
    conn.executescript(db.SCHEMA)
    return conn


class _NoClose:
    def __init__(self, conn):
        self._c = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._c, name)


class MatcherTest(unittest.TestCase):
    def test_explicit_ask_hits(self):
        cases = {
            "make me one": "video",
            "send me something new": "video",
            "give me something new, baby": "video",
            "make me a fresh video of you": "video",
            "make a new video for me": "video",
            "make me a new pic, baby": "image",
            "make me a fresh picture": "image",
            "show me you taking your top off": "video",
            "show me you naked": "video",
            "show me a new pic of you": "image",
        }
        for text, kind in cases.items():
            det = live_give.detect_live_give_ask(text)
            self.assertIsNotNone(det, text)
            self.assertEqual(det["kind"], kind, text)
            self.assertFalse(det["initiative"], text)

    def test_explicit_ask_misses_ordinary_chat(self):
        for text in (
            "how was your day, baby",
            "make me laugh",
            "send me the grocery list",
            # generic media asks belong to the assistant lane
            "make me a video of you dancing on the beach",
            "send me a pic of the sunset for the site",
            "let's video call tonight",
            "show me your code for the login page",
            "i want to see you",
            "you're so hot tonight",
            "",
        ):
            self.assertIsNone(live_give.detect_live_give_ask(text), text)

    def test_explicit_ask_hits_plain_of_you_action_asks(self):
        # The routing fix (2026-10-14): the natural way the owner
        # asks a private girl — plain "make me a video/pic of you
        # <explicit action>", imperative or gerund — is owned by
        # this detector now. The first case is his exact phrasing
        # from the failed live test.
        cases = {
            "Make me a video of you taking off your bra": "video",
            "make me a pic of you taking off your bra": "image",
            "send me a video of you taking off your top": "video",
            "make a video of you taking off your bra": "video",
            "record a video of you sucking my cock": "video",
            "make me a video of you topless": "video",
        }
        for text, kind in cases.items():
            det = live_give.detect_live_give_ask(text)
            self.assertIsNotNone(det, text)
            self.assertEqual(det["kind"], kind, text)
            self.assertFalse(det["initiative"], text)

    def test_plain_scene_asks_still_belong_to_assistant_lane(self):
        # No explicit action / adult signal -> not a give ask, even
        # in the widened shape.
        for text in (
            "make me a video of you dancing on the beach",
            "make a video of you waving",
            "send me a pic of you smiling in the garden",
        ):
            self.assertIsNone(live_give.detect_live_give_ask(text), text)

    def test_media_choice_first_word_wins(self):
        det = live_give.detect_live_give_ask("make me a new video and a pic")
        self.assertEqual(det["kind"], "video")
        det = live_give.detect_live_give_ask("make me a fresh pic, or a video")
        self.assertEqual(det["kind"], "image")

    def test_initiative_hits_on_existing_heat_signal(self):
        for text in ("show me your tits", "take off your bra",
                     "suck my cock", "let me see your body"):
            det = live_give.detect_initiative_ask(text)
            self.assertIsNotNone(det, text)
            self.assertEqual(det["kind"], "image", text)
            self.assertTrue(det["initiative"], text)

    def test_initiative_hits_on_gerund_actions(self):
        # Gerund forms (2026-10-14): the action detector's verbs are
        # no longer literal-only — "taking off" mid-sentence matches
        # like the bare imperative did.
        for text in ("taking off your bra",
                     "she is taking off her bra",
                     "touching your boobs",
                     "stripping your panties off",
                     "sucking my dick"):
            det = live_give.detect_initiative_ask(text)
            self.assertIsNotNone(det, text)
            self.assertEqual(det["kind"], "image", text)
            self.assertTrue(det["initiative"], text)

    def test_initiative_misses(self):
        for text in ("how was your day", "make me one",
                     "you're beautiful tonight", "show me your smile"):
            self.assertIsNone(live_give.detect_initiative_ask(text), text)


class HookScopeTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = OWNER_ID
        live_give._reset_state_for_tests()
        self.conn = _db()
        self.spawns: list = []
        self._spawn_patch = mock.patch.object(
            live_give, "_spawn_live_give",
            side_effect=lambda *a: self.spawns.append(a))
        self._spawn_patch.start()

    def tearDown(self):
        self._spawn_patch.stop()
        self.conn.close()
        if self._env is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._env

    def test_owner_and_girl_scoping(self):
        for girl in GIRLS:
            hit = live_give.maybe_start_live_give(
                self.conn, OWNER_ID, girl, "make me one", now=1000.0)
            self.assertIsNotNone(hit, girl)
            self.assertEqual(hit["kind"], "video")
            self.assertIn("brand-new", hit["brief"])
            live_give._reset_state_for_tests()
        self.assertEqual(len(self.spawns), 4)

    def test_non_owner_never_triggers(self):
        for girl in GIRLS:
            hit = live_give.maybe_start_live_give(
                self.conn, "someone_else", girl, "make me one", now=1000.0)
            self.assertIsNone(hit, girl)
        self.assertEqual(self.spawns, [])

    def test_other_girls_never_trigger(self):
        for girl in ("chloe", "mara", "rocky", "isabella", "jake"):
            hit = live_give.maybe_start_live_give(
                self.conn, OWNER_ID, girl, "make me one", now=1000.0)
            self.assertIsNone(hit, girl)
        self.assertEqual(self.spawns, [])

    def test_assist_claimed_turn_yields(self):
        hit = live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "mia", "make me one",
            assist_claimed=True, now=1000.0)
        self.assertIsNone(hit)
        self.assertEqual(self.spawns, [])

    def test_ordinary_chat_no_trigger(self):
        self.assertIsNone(live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "mia", "how was your day", now=1000.0))
        self.assertEqual(self.spawns, [])

    def test_initiative_starts_an_image(self):
        hit = live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "hannah", "show me your tits", now=1000.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["kind"], "image")
        self.assertTrue(hit["initiative"])
        self.assertEqual(self.spawns[0][2], "image")


class DedupCooldownTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = OWNER_ID
        live_give._reset_state_for_tests()
        self.conn = _db()
        self.spawns: list = []
        self._spawn_patch = mock.patch.object(
            live_give, "_spawn_live_give",
            side_effect=lambda *a: self.spawns.append(a))
        self._spawn_patch.start()
        self.conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset, stage) "
            "VALUES ('brooklyn', 'give', '/media/brooklyn-s1.mp4', 'give', 1)")
        self.conn.commit()

    def tearDown(self):
        self._spawn_patch.stop()
        self.conn.close()
        if self._env is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._env

    def _turns(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT role, text, attachments FROM conversation "
            "WHERE user_id = ? AND character_id = 'brooklyn'",
            (OWNER_ID,)).fetchall()]

    def test_second_trigger_in_flight_serves_preset_once(self):
        first = live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "brooklyn", "make me one", now=1000.0)
        self.assertIsNotNone(first)
        second = live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "brooklyn", "make me a new video",
            now=1005.0)
        self.assertTrue(second["fallback_preset"])
        turns = self._turns()
        self.assertEqual(len(turns), 1)
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["url"], "/media/brooklyn-s1.mp4")
        self.assertEqual(atts[0]["kind"], "video")
        # a third trigger in the same flight serves nothing more
        third = live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "brooklyn", "make me one", now=1010.0)
        self.assertIsNone(third)
        self.assertEqual(len(self._turns()), 1)
        self.assertEqual(len(self.spawns), 1)

    def test_cooldown_blocks_restart_after_completion(self):
        live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "brooklyn", "make me one", now=1000.0)
        # simulate the worker finishing
        live_give._STATE[(OWNER_ID, "brooklyn")]["in_flight"] = False
        self.assertIsNone(live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "brooklyn", "make me one", now=1060.0))
        hit = live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "brooklyn", "make me one", now=1121.0)
        self.assertIsNotNone(hit)
        self.assertEqual(len(self.spawns), 2)

    def test_initiative_cooldown_is_longer(self):
        hit = live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "brooklyn", "show me your tits",
            now=2000.0)
        self.assertTrue(hit["initiative"])
        live_give._STATE[(OWNER_ID, "brooklyn")]["in_flight"] = False
        # 5 minutes later: past the plain cooldown, inside the
        # initiative cooldown — initiative stays quiet...
        self.assertIsNone(live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "brooklyn", "show me your tits",
            now=2300.0))
        # ...while an explicit ask at the same moment fires.
        hit = live_give.maybe_start_live_give(
            self.conn, OWNER_ID, "brooklyn", "make me one", now=2300.0)
        self.assertIsNotNone(hit)
        self.assertFalse(hit["initiative"])


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = OWNER_ID
        live_give._reset_state_for_tests()
        self.conn = _db()
        self._nc = _NoClose(self.conn)
        fd, self.out_path = tempfile.mkstemp(suffix=".mp4")
        os.write(fd, b"fake-video-bytes")
        os.close(fd)
        fd, self.ref_path = tempfile.mkstemp(suffix=".jpg")
        os.write(fd, b"fake-ref")
        os.close(fd)
        self.done_video = ({"status": "done", "project_id": "p1",
                            "path": self.out_path, "detail": ""}, None)
        self.failed = ({"status": "failed", "project_id": None,
                        "path": None, "detail": "boom"}, None)
        self.patches = [
            # Isolate the static media-dir fallback scan: the repo
            # ships a real brooklyn library, tests seed their own.
            mock.patch.object(config, "DATA_DIR",
                              tempfile.mkdtemp(prefix="live-give-data-")),
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
            mock.patch.object(give_gen, "_hardwired_login",
                              return_value=None),
            mock.patch.object(give_gen, "_hardwired_sogni_session",
                              return_value=None),
            mock.patch.object(give_gen, "_topless_reference_frame",
                              return_value=None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.conn.close()
        for path in (self.out_path, self.ref_path):
            try:
                os.unlink(path)
            except OSError:
                pass
        if self._env is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._env

    def _turns(self, girl="brooklyn"):
        return [dict(r) for r in self.conn.execute(
            "SELECT role, text, attachments FROM conversation "
            "WHERE user_id = ? AND character_id = ?",
            (OWNER_ID, girl)).fetchall()]

    def _seed_preset(self, girl="brooklyn", url="/media/brooklyn-s2.mp4",
                     stage=2):
        self.conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset, stage) "
            "VALUES (?, 'give', ?, 'give', ?)", (girl, url, stage))
        self.conn.commit()

    def test_video_render_delivered_into_chat(self):
        live_give._STATE[(OWNER_ID, "brooklyn")] = {
            "in_flight": True, "last_start": 0.0, "last_initiative": 0.0,
            "preset_sent_in_flight": False}
        with mock.patch.object(assist, "_sogni_plan_run",
                               return_value=self.done_video) as run, \
                mock.patch.object(assist, "_sogni_plan_image_run") as irun:
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        run.assert_called_once()
        irun.assert_not_called()
        turns = self._turns()
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["role"], "brooklyn")
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["kind"], "video")
        self.assertEqual(atts[0]["url"],
                         "/generated/" + os.path.basename(self.out_path))
        self.assertEqual(atts[0]["size"], len(b"fake-video-bytes"))
        self.assertFalse(
            live_give._STATE[(OWNER_ID, "brooklyn")]["in_flight"])

    def test_image_render_uses_image_seam(self):
        with mock.patch.object(assist, "_sogni_plan_image_run",
                               return_value=self.done_video) as irun, \
                mock.patch.object(assist, "_sogni_plan_run") as run:
            live_give._run_live_give_job(
                OWNER_ID, "mia", "image", 1, "show me your tits")
        irun.assert_called_once()
        run.assert_not_called()
        atts = json.loads(self._turns("mia")[0]["attachments"])
        self.assertEqual(atts[0]["kind"], "image")

    def test_render_failure_falls_back_to_preset(self):
        self._seed_preset()
        with mock.patch.object(assist, "_sogni_plan_run",
                               return_value=self.failed):
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        turns = self._turns()
        self.assertEqual(len(turns), 1)
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["url"], "/media/brooklyn-s2.mp4")
        prog = self.conn.execute(
            "SELECT gives_received FROM user_give_progress "
            "WHERE user_id = ? AND girl_id = 'brooklyn'",
            (OWNER_ID,)).fetchone()
        self.assertEqual(prog[0], 1)

    def test_render_failure_without_library_surfaces_nothing(self):
        with mock.patch.object(assist, "_sogni_plan_run",
                               return_value=self.failed):
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        self.assertEqual(self._turns(), [])

    def test_no_session_falls_back_without_rendering(self):
        self._seed_preset()
        with mock.patch.object(assist, "stored_sogni_session",
                               return_value=None), \
                mock.patch.object(assist, "_sogni_plan_run") as run:
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        run.assert_not_called()
        self.assertEqual(len(self._turns()), 1)

    def test_session_dead_resolves_and_retries_once(self):
        dead = ({"status": "session_dead", "project_id": None,
                 "path": None, "detail": "account changed"}, None)
        with mock.patch.object(
                assist, "_sogni_plan_run",
                side_effect=[dead, self.done_video]) as run, \
                mock.patch.object(assist, "resolve_dead_sogni_session",
                                  return_value=dict(TOKENS2)) as resolve:
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        self.assertEqual(run.call_count, 2)
        resolve.assert_called_once()
        atts = json.loads(self._turns()[0]["attachments"])
        self.assertEqual(atts[0]["kind"], "video")

    def test_session_dead_unresolved_falls_back_to_preset(self):
        self._seed_preset()
        dead = ({"status": "session_dead", "project_id": None,
                 "path": None, "detail": "account changed"}, None)
        with mock.patch.object(assist, "_sogni_plan_run",
                               return_value=dead), \
                mock.patch.object(assist, "resolve_dead_sogni_session",
                                  return_value=None):
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        atts = json.loads(self._turns()[0]["attachments"])
        self.assertEqual(atts[0]["url"], "/media/brooklyn-s2.mp4")

    def test_session_dead_healed_at_seam_skips_second_recovery(self):
        # The seam already resolved + re-ran once and marked the
        # outcome ("seam_healed"); live_give must not stack its own
        # resolve + second render (and another sign-in) on top —
        # the still-dead outcome falls back to her preset like any
        # other render miss.
        self._seed_preset()
        healed_dead = ({"status": "session_dead", "project_id": None,
                        "path": None, "detail": "account changed",
                        "seam_healed": True}, None)
        with mock.patch.object(assist, "_sogni_plan_run",
                               return_value=healed_dead) as run, \
                mock.patch.object(assist,
                                  "resolve_dead_sogni_session") as resolve:
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        run.assert_called_once()
        resolve.assert_not_called()
        atts = json.loads(self._turns()[0]["attachments"])
        self.assertEqual(atts[0]["url"], "/media/brooklyn-s2.mp4")

    def test_throttled_falls_back_to_preset_without_heal(self):
        # A reconnection throttle is a cooldown, not a dead
        # session: live_give must not resolve/re-login over it
        # (reconnecting is the behaviour being punished) — the
        # render miss falls back to her preset like any other.
        self._seed_preset()
        throttled = ({"status": "throttled", "project_id": None,
                      "path": None,
                      "detail": "4056: Connection temporarily blocked",
                      "throttle_seconds": 2160}, None)
        with mock.patch.object(assist, "_sogni_plan_run",
                               return_value=throttled) as run, \
                mock.patch.object(assist,
                                  "resolve_dead_sogni_session") as resolve:
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        run.assert_called_once()
        resolve.assert_not_called()
        atts = json.loads(self._turns()[0]["attachments"])
        self.assertEqual(atts[0]["url"], "/media/brooklyn-s2.mp4")

    def test_reference_is_her_own_avatar(self):
        with mock.patch.object(assist, "_sogni_plan_run",
                               return_value=self.done_video), \
                mock.patch.object(assist, "_fetch_first_frame",
                                  return_value=self.ref_path) as fetch:
            live_give._run_live_give_job(
                OWNER_ID, "jordan", "video", 1, "make me one")
        fetch.assert_called_once_with("http://x/avatar.jpg")

    def test_stage_over_two_prefers_topless_reference(self):
        with mock.patch.object(give_gen, "_topless_reference_frame",
                               return_value=self.ref_path), \
                mock.patch.object(assist, "_sogni_plan_run",
                                  return_value=self.done_video) as run, \
                mock.patch.object(assist, "_fetch_first_frame") as fetch:
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 5, "make me one")
        fetch.assert_not_called()
        self.assertEqual(run.call_args[0][2], self.ref_path)

    def test_render_holds_live_priority_signal(self):
        # Priority (2026-10-14): the whole render attempt runs with
        # assist's live-render signal up (the give_gen queue pauses
        # between jobs while it is), and the signal is released when
        # the job ends.
        seen = []

        def fake_run(*a, **k):
            seen.append(assist.live_render_active())
            return self.done_video

        with mock.patch.object(assist, "_sogni_plan_run",
                               side_effect=fake_run):
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        self.assertEqual(seen, [True])
        self.assertFalse(assist.live_render_active())


class SceneTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def tearDown(self):
        self.conn.close()

    def test_scene_uses_stage_description_plus_rephrased_setting(self):
        from app import store
        store.log_turn(self.conn, OWNER_ID, "user",
                       "god I love you in the shower", "mia")
        scene = live_give._build_scene(
            self.conn, OWNER_ID, "mia", 1, "make me one in the shower")
        self.assertTrue(scene.startswith(
            give_gen.stage_description_for("mia", 1)))
        self.assertIn("in the shower", scene)
        # his wording beyond the stage script never enters the scene
        self.assertNotIn("god I love you", scene)

    def test_scene_without_setting_is_the_stage_description(self):
        scene = live_give._build_scene(
            self.conn, OWNER_ID, "mia", 3, "make me one")
        self.assertEqual(scene, give_gen.stage_description_for("mia", 3))


class WorkerHardeningTest(unittest.TestCase):
    """Exit-path contract (2026-10-14 fix): whatever dies inside the
    worker — the render seam raising, db.connect failing — the
    (user, girl) in-flight entry is ALWAYS cleared and, when nothing
    was delivered, the preset fallback is attempted exactly once.
    Before the fix an exception skipped the fallback entirely, and a
    connect failure stranded in_flight=True forever (every later
    turn: "still processing")."""

    def setUp(self):
        self._env = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = OWNER_ID
        live_give._reset_state_for_tests()
        self.conn = _db()
        self._nc = _NoClose(self.conn)
        fd, self.ref_path = tempfile.mkstemp(suffix=".jpg")
        os.write(fd, b"fake-ref")
        os.close(fd)
        self.patches = [
            mock.patch.object(config, "DATA_DIR",
                              tempfile.mkdtemp(prefix="live-give-data-")),
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
            mock.patch.object(give_gen, "_hardwired_login",
                              return_value=None),
            mock.patch.object(give_gen, "_hardwired_sogni_session",
                              return_value=None),
            mock.patch.object(give_gen, "_topless_reference_frame",
                              return_value=None),
        ]
        for p in self.patches:
            p.start()
        live_give._STATE[(OWNER_ID, "brooklyn")] = {
            "in_flight": True, "last_start": 0.0, "last_initiative": 0.0,
            "preset_sent_in_flight": False}

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

    def _turns(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT role, text, attachments FROM conversation "
            "WHERE user_id = ? AND character_id = 'brooklyn'",
            (OWNER_ID,)).fetchall()]

    def _seed_preset(self):
        self.conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset, stage) "
            "VALUES ('brooklyn', 'give', '/media/brooklyn-s2.mp4', "
            "'give', 2)")
        self.conn.commit()

    def _in_flight(self):
        return live_give._STATE[(OWNER_ID, "brooklyn")]["in_flight"]

    def test_seam_crash_delivers_preset_and_clears_registry(self):
        self._seed_preset()
        with mock.patch.object(assist, "_sogni_plan_run",
                               side_effect=RuntimeError("seam exploded")):
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        turns = self._turns()
        self.assertEqual(len(turns), 1)
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["url"], "/media/brooklyn-s2.mp4")
        self.assertFalse(self._in_flight())

    def test_seam_crash_without_library_still_clears_registry(self):
        with mock.patch.object(assist, "_sogni_plan_run",
                               side_effect=RuntimeError("seam exploded")):
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        self.assertEqual(self._turns(), [])
        self.assertFalse(self._in_flight())

    def test_db_connect_failure_falls_back_and_clears_registry(self):
        self._seed_preset()
        # First connect (the worker's own) dies; the fallback's
        # connect succeeds against the same in-memory db.
        with mock.patch.object(
                db, "connect",
                side_effect=[RuntimeError("db down"), self._nc]):
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        turns = self._turns()
        self.assertEqual(len(turns), 1)
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["url"], "/media/brooklyn-s2.mp4")
        self.assertFalse(self._in_flight())

    def test_preset_attempted_exactly_once_when_delivery_raises(self):
        self._seed_preset()
        failed = ({"status": "failed", "project_id": None,
                   "path": None, "detail": "boom"}, None)
        with mock.patch.object(assist, "_sogni_plan_run",
                               return_value=failed), \
                mock.patch.object(
                    live_give, "_deliver_preset",
                    side_effect=RuntimeError("log write died")) as deliver:
            live_give._run_live_give_job(
                OWNER_ID, "brooklyn", "video", 1, "make me one")
        self.assertEqual(deliver.call_count, 1)
        self.assertFalse(self._in_flight())


class HookSpawnFailureTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = OWNER_ID
        live_give._reset_state_for_tests()
        self.conn = _db()

    def tearDown(self):
        self.conn.close()
        if self._env is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._env

    def test_spawn_failure_clears_in_flight(self):
        # The flag is set in the hook and cleared only by the worker;
        # a worker that never starts must not strand it.
        with mock.patch.object(live_give, "_spawn_live_give",
                               side_effect=RuntimeError("no threads")):
            hit = live_give.maybe_start_live_give(
                self.conn, OWNER_ID, "brooklyn", "make me one",
                now=1000.0)
        self.assertIsNone(hit)
        st = live_give._STATE[(OWNER_ID, "brooklyn")]
        self.assertFalse(st["in_flight"])


class MainHookupTest(unittest.TestCase):
    def test_main_imports_and_wires_live_give(self):
        from app import main as api
        self.assertIs(api.live_give, live_give)


class HookLoggingTest(unittest.TestCase):
    """Start logging (2026-10-14): EVERY hook evaluation that passes
    the girl/owner gates leaves one [live_give] eval line — detector
    outcome, assist_claimed, and the action or the reason not. Before
    this, the hook printed only when a job ended, so eaten asks were
    indistinguishable from silence."""

    def setUp(self):
        self._env = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = OWNER_ID
        live_give._reset_state_for_tests()
        self.conn = _db()
        self.spawns: list = []
        self._spawn_patch = mock.patch.object(
            live_give, "_spawn_live_give",
            side_effect=lambda *a: self.spawns.append(a))
        self._spawn_patch.start()

    def tearDown(self):
        self._spawn_patch.stop()
        self.conn.close()
        if self._env is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._env

    def _eval(self, *args, **kwargs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            hit = live_give.maybe_start_live_give(*args, **kwargs)
        return hit, buf.getvalue()

    def test_start_evaluation_logs_detector_and_stage(self):
        hit, out = self._eval(
            self.conn, OWNER_ID, "brooklyn",
            "Make me a video of you taking off your bra", now=1000.0)
        self.assertIsNotNone(hit)
        self.assertIn("[live_give] eval girl=brooklyn", out)
        self.assertIn("detector=explicit", out)
        self.assertIn("assist_claimed=False", out)
        self.assertIn("action=start", out)
        self.assertIn("stage=1", out)

    def test_unmatched_evaluation_logs_no_match(self):
        hit, out = self._eval(
            self.conn, OWNER_ID, "mia", "how was your day", now=1000.0)
        self.assertIsNone(hit)
        self.assertIn("[live_give] eval girl=mia", out)
        self.assertIn("detector=none", out)
        self.assertIn("action=no_match", out)

    def test_assist_claimed_evaluation_logs_the_yield(self):
        hit, out = self._eval(
            self.conn, OWNER_ID, "mia", "show me your tits",
            assist_claimed=True, now=1000.0)
        self.assertIsNone(hit)
        self.assertIn("assist_claimed=True", out)
        self.assertIn("action=yield_assist_claimed", out)
        self.assertEqual(self.spawns, [])

    def test_cooldown_evaluation_logs_the_reason(self):
        self._eval(self.conn, OWNER_ID, "brooklyn", "make me one",
                   now=1000.0)
        live_give._STATE[(OWNER_ID, "brooklyn")]["in_flight"] = False
        hit, out = self._eval(
            self.conn, OWNER_ID, "brooklyn", "make me one", now=1060.0)
        self.assertIsNone(hit)
        self.assertIn("action=cooldown", out)


def _char(cid, name):
    return {"id": cid, "name": name, "avatar_url": "", "persona": "",
            "companion_voice": "liora", "blurb": "", "door_title": "",
            "sort_order": 1}


class _Req:
    def __init__(self, token):
        self.headers = {"authorization": f"Bearer {token}"} if token \
            else {}


class MainRoutingTest(unittest.TestCase):
    """Chat-path routing (2026-10-14): an owner's plain media ask to
    one of the four private girls is claimed by the live-give hook
    BEFORE maybe_assist runs — the assistant Sogni lane matches the
    same phrasing and used to eat the turn upstream, so the hook
    only ever saw assist_claimed=True and yielded silently."""

    PHRASING = "Make me a video of you taking off your bra"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._old_owners = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = OWNER_ID
        self._real_cache = characters._cache
        characters._cache = {"ts": time.time(), "chars": [], "local": [
            _char("brooklyn", "Brooklyn"), _char("mara", "Mara")]}
        self.contexts: list = []
        self.assist_calls: list = []
        self.spawns: list = []

        def fake_compose(message, context, cid):
            self.contexts.append(dict(context))
            return "a real reply"

        def fake_assist(conn, user, msg, cid, attachments=None):
            self.assist_calls.append((cid, msg))
            return {"brief": "ASSIST LANE CLAIM",
                    "sogni_media_kind": "video"}

        self._real_compose = brain.compose_reply
        self._real_context = brain.build_brain_context
        self._real_learn = brain.learn_from_turn
        self._real_assist = assist.maybe_assist
        brain.compose_reply = fake_compose
        brain.build_brain_context = \
            lambda conn, uid, user, msg, char: {}
        brain.learn_from_turn = lambda conn, uid, msg, cid: None
        assist.maybe_assist = fake_assist
        self._spawn_patch = mock.patch.object(
            live_give, "_spawn_live_give",
            side_effect=lambda *a: self.spawns.append(a))
        self._spawn_patch.start()
        live_give._reset_state_for_tests()
        self.owner_tok = self._mk_user(OWNER_ID)
        self.cust_tok = self._mk_user("cust_1")

    def tearDown(self):
        self._spawn_patch.stop()
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
        live_give._reset_state_for_tests()
        self.tmp.cleanup()

    def _mk_user(self, uid):
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, uid)
            conn.execute(
                "UPDATE users SET messages_remaining = 5, "
                "voice_notes_remaining = 0 WHERE user_id = ?", (uid,))
            conn.commit()
            token = f"tok-{uid}"
            store.create_session(conn, uid, token)
            return token
        finally:
            conn.close()

    def _chat(self, token, character_id, message):
        body = api.ChatIn(message=message, character_id=character_id)
        return api.chat(body, _Req(token))

    def test_owner_exact_phrasing_wins_first_claim(self):
        resp = self._chat(self.owner_tok, "brooklyn", self.PHRASING)
        self.assertEqual(resp["reply_text"], "a real reply")
        # The assistant lane never ran: the hook claimed the turn.
        self.assertEqual(self.assist_calls, [])
        self.assertEqual(len(self.spawns), 1)
        brief = self.contexts[0].get("assist_result") or ""
        self.assertIn("brand-new", brief)
        self.assertNotIn("ASSIST LANE CLAIM", brief)

    def test_plain_chat_still_flows_through_assist(self):
        resp = self._chat(self.owner_tok, "brooklyn", "how was your day")
        self.assertEqual(resp["reply_text"], "a real reply")
        self.assertEqual(len(self.assist_calls), 1)
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.contexts[0].get("assist_result"),
                         "ASSIST LANE CLAIM")

    def test_initiative_still_yields_to_an_assist_claim(self):
        # Only EXPLICIT asks got first claim; her initiative path
        # still runs after maybe_assist and yields to its claim.
        self._chat(self.owner_tok, "brooklyn", "show me your tits")
        self.assertEqual(len(self.assist_calls), 1)
        self.assertEqual(self.spawns, [])
        self.assertEqual(self.contexts[0].get("assist_result"),
                         "ASSIST LANE CLAIM")

    def test_non_owner_phrasing_stays_with_assist(self):
        self._chat(self.cust_tok, "brooklyn", self.PHRASING)
        self.assertEqual(len(self.assist_calls), 1)
        self.assertEqual(self.spawns, [])

    def test_owner_other_girl_phrasing_stays_with_assist(self):
        self._chat(self.owner_tok, "mara", self.PHRASING)
        self.assertEqual(len(self.assist_calls), 1)
        self.assertEqual(self.spawns, [])


if __name__ == "__main__":
    unittest.main()
