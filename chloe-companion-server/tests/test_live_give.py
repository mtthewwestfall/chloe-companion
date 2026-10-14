"""Live give creation (2026-10-14): the four private girls create a
fresh give scene in a heated owner chat instead of only serving
presets. The render seam is mocked throughout — no real renders."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, characters, config, db, give_gen, live_give  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
