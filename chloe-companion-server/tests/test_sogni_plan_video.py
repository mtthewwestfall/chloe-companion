"""Hannah Sogni plan-video tests (2026-10-09, owner-directed).

She makes short videos of herself for the owner on his Sogni
Unlimited plan: every render is submitted with billingMode
"subscription", she signs in with a one-time in-chat login (only the
session token pair is kept; the password is scrubbed and discarded),
and the finished clip lands in the chat as a video bubble.

All Sogni calls are mocked at the _sogni_plan_run / _sogni_plan_login
seams — zero real renders, zero real sign-ins.

Run:  python -m unittest tests.test_sogni_plan_video -v
      (from the project root, with the venv's python)
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, characters, config  # noqa: E402

OWNER_USER = {"user_id": "owner_1", "tier": "basic"}
USERNAME = "Lockeddoor"
PASSWORD = "hunter2-fake"
TOKENS = {"token": "tok-aaa", "refreshToken": "ref-bbb"}
TOKENS_ROTATED = {"token": "tok-ccc", "refreshToken": "ref-ddd"}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE conversation (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id TEXT, character_id TEXT, role TEXT, text TEXT,"
        " created_at TEXT, attachments TEXT)")
    assist.ensure_tables(conn)
    return conn


def _log_user_turn(conn, user_id: str, text: str, cid: str = "hannah"):
    conn.execute(
        "INSERT INTO conversation (user_id, character_id, role, text)"
        " VALUES (?, ?, 'user', ?)", (user_id, cid, text))
    conn.commit()


class _Patched:
    """Tiny monkeypatch helper (unittest.mock would do; keep style)."""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self.value

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.old)
        return False


class SogniPlanCase(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS",
                         "HANNAH_SOGNI_PLAN_DAILY_CAP")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        os.environ.pop("HANNAH_SOGNI_PLAN_DAILY_CAP", None)
        self.conn = _conn()
        self.spawned: list[int] = []
        self._patches = [
            _Patched(assist, "_spawn_sogni_plan_job", self.spawned.append),
        ]
        for p in self._patches:
            p.__enter__()

    def tearDown(self):
        for p in reversed(self._patches):
            p.__exit__()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def _jobs(self):
        return self.conn.execute(
            "SELECT * FROM sogni_plan_jobs ORDER BY id").fetchall()

    def _turns(self):
        return self.conn.execute(
            "SELECT role, text, attachments FROM conversation "
            "ORDER BY id").fetchall()


class IntentTest(SogniPlanCase):
    def test_detect_subject(self):
        self.assertEqual(
            assist.detect_sogni_video_request(
                "Create a video of you taking off your top"),
            "you taking off your top")
        self.assertEqual(
            assist.detect_sogni_video_request(
                "hannah, make me a video of you waving at me"),
            "you waving at me")
        self.assertEqual(
            assist.detect_sogni_video_request("please make a video"), "")

    def test_detect_ignores_other_lanes(self):
        self.assertIsNone(
            assist.detect_sogni_video_request("hey beautiful"))
        self.assertIsNone(
            assist.detect_sogni_video_request("send me a video"))
        self.assertIsNone(
            assist.detect_sogni_video_request("make an image of a cat"))
        self.assertIsNone(
            assist.detect_sogni_video_request("draw me a picture"))


class ParamsGuardTest(SogniPlanCase):
    def test_params_always_plan_billed(self):
        params = assist._sogni_plan_params("Photorealistic Hannah, waving")
        self.assertEqual(params["billingMode"], "subscription")
        self.assertEqual(params["type"], "video")
        self.assertEqual(params["width"], 768)
        self.assertEqual(params["height"], 1024)
        self.assertEqual(params["numberOfMedia"], 1)
        # No credits-billing knobs, no H3-forbidden negative prompt.
        self.assertNotIn("tokenType", params)
        self.assertNotIn("negativePrompt", params)
        self.assertNotIn("referenceImage", params)

    def test_params_carry_reference(self):
        params = assist._sogni_plan_params("p", ref_path="/tmp/her.jpg")
        self.assertEqual(params["referenceImage"], "/tmp/her.jpg")

    def test_no_api_key_anywhere_in_the_plan_path(self):
        # The guard that keeps the credits route dead: none of the
        # plan-video entry points may touch the API-key config, and
        # the billing mode constant is the plan, full stop.
        for fn in (assist._sogni_plan_params, assist._sogni_plan_render,
                   assist._sogni_plan_login_async,
                   assist._handle_sogni_video_request):
            self.assertNotIn("SOGNI_API_KEY", inspect.getsource(fn),
                             fn.__name__)
        self.assertEqual(assist._SOGNI_PLAN_BILLING_MODE, "subscription")

    def test_prompt_folds_exclusions(self):
        prompt = assist._sogni_plan_prompt(
            "Hannah", "dancing in the kitchen with no shoes on")
        self.assertTrue(prompt.startswith("Photorealistic Hannah, "))
        self.assertIn("keeping the scene free of shoes on", prompt)
        prompt = assist._sogni_plan_prompt("Hannah", "")
        self.assertIn("smiling at the camera", prompt)

    def test_classify_failures(self):
        self.assertEqual(
            assist._classify_sogni_failure("Sensitive content detected"),
            "blocked")
        self.assertEqual(
            assist._classify_sogni_failure("billingMode rejected: "
                                           "subscription cannot cover"),
            "plan_rejected")
        self.assertEqual(
            assist._classify_sogni_failure("HTTP 401 unauthorized"),
            "session_dead")
        self.assertEqual(assist._classify_sogni_failure("boom"), "failed")


class SessionIntakeTest(SogniPlanCase):
    def test_login_intake_stores_tokens_and_scrubs(self):
        seen = {}

        def fake_login(login):
            seen["u"], seen["p"] = login
            return dict(TOKENS)

        msg = f"drop my sogni login here: sogni login {USERNAME} {PASSWORD}"
        _log_user_turn(self.conn, "owner_1", msg)
        with _Patched(assist, "_sogni_plan_login", fake_login):
            hit = assist.maybe_assist(self.conn, OWNER_USER, msg, "hannah")
        self.assertEqual(seen, {"u": USERNAME, "p": PASSWORD})
        self.assertIsNotNone(hit)
        self.assertTrue(hit["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        self.assertNotIn(PASSWORD, hit["brief"])
        self.assertNotIn(USERNAME, hit["brief"])
        # Tokens stored (that's the point) — password nowhere in the row.
        self.assertEqual(assist.stored_sogni_session(self.conn), TOKENS)
        raw = self.conn.execute(
            "SELECT value FROM assist_secrets WHERE name = ?",
            (assist._SOGNI_PLAN_SESSION,)).fetchone()[0]
        self.assertNotIn(PASSWORD, raw)
        # His persisted turn is the placeholder, nothing else.
        rows = self._turns()
        self.assertEqual(rows[-1]["text"], assist._SOGNI_PLAN_PLACEHOLDER)
        self.assertEqual(self.spawned, [])

    def test_login_failure_stores_nothing_but_still_scrubs(self):
        def bad_login(login):
            raise assist._SogniPlanError("login_failed")

        msg = f"sogni login {USERNAME} {PASSWORD}"
        _log_user_turn(self.conn, "owner_1", msg)
        with _Patched(assist, "_sogni_plan_login", bad_login):
            hit = assist.maybe_assist(self.conn, OWNER_USER, msg, "hannah")
        self.assertIsNotNone(hit)
        self.assertIn("didn't take", hit["brief"])
        self.assertNotIn(PASSWORD, hit["brief"])
        self.assertIsNone(assist.stored_sogni_session(self.conn))
        self.assertEqual(self._turns()[-1]["text"],
                         assist._SOGNI_PLAN_PLACEHOLDER)

    def test_logout_clears_session(self):
        assist.store_sogni_session(self.conn, dict(TOKENS))
        hit = assist.maybe_assist(
            self.conn, OWNER_USER, "forget my sogni login", "hannah")
        self.assertIsNotNone(hit)
        self.assertTrue(hit["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        self.assertIsNone(assist.stored_sogni_session(self.conn))

    def test_non_owner_gets_nothing(self):
        msg = f"sogni login {USERNAME} {PASSWORD}"
        hit = assist.maybe_assist(
            self.conn, {"user_id": "someone_else", "tier": "basic"},
            msg, "hannah")
        self.assertIsNone(hit)
        self.assertIsNone(assist.stored_sogni_session(self.conn))
        self.assertEqual(self._jobs(), [])


class VideoAskTest(SogniPlanCase):
    def test_ask_without_session_asks_for_login(self):
        hit = assist.maybe_assist(
            self.conn, OWNER_USER, "make a video of you waving", "hannah")
        self.assertIsNotNone(hit)
        self.assertTrue(hit["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        self.assertIn("sogni login <username> <password>", hit["brief"])
        self.assertEqual(self._jobs(), [])
        self.assertEqual(self.spawned, [])

    def test_ask_without_subject_asks_what(self):
        assist.store_sogni_session(self.conn, dict(TOKENS))
        hit = assist.maybe_assist(
            self.conn, OWNER_USER, "make a video", "hannah")
        self.assertIsNotNone(hit)
        self.assertIn("did not say what it should show", hit["brief"])
        self.assertEqual(self._jobs(), [])

    def test_video_ask_only_in_hannah_thread(self):
        assist.store_sogni_session(self.conn, dict(TOKENS))
        hit = assist.maybe_assist(
            self.conn, OWNER_USER, "make a video of you waving", "chloe")
        self.assertIsNone(hit)
        self.assertEqual(self._jobs(), [])

    def test_ask_queues_plan_billed_job(self):
        assist.store_sogni_session(self.conn, dict(TOKENS))
        hit = assist.maybe_assist(
            self.conn, OWNER_USER, "make a video of you waving", "hannah")
        self.assertIsNotNone(hit)
        self.assertIn("rendering now", hit["brief"])
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "queued")
        self.assertEqual(jobs[0]["prompt"], "you waving")
        self.assertEqual(jobs[0]["billing_mode"], "subscription")
        self.assertEqual(self.spawned, [jobs[0]["id"]])

    def test_one_at_a_time(self):
        assist.store_sogni_session(self.conn, dict(TOKENS))
        assist.maybe_assist(
            self.conn, OWNER_USER, "make a video of you waving", "hannah")
        hit = assist.maybe_assist(
            self.conn, OWNER_USER, "make a video of you dancing", "hannah")
        self.assertIn("still rendering", hit["brief"])
        self.assertEqual(len(self._jobs()), 1)

    def test_daily_cap(self):
        os.environ["HANNAH_SOGNI_PLAN_DAILY_CAP"] = "1"
        assist.store_sogni_session(self.conn, dict(TOKENS))
        assist.maybe_assist(
            self.conn, OWNER_USER, "make a video of you waving", "hannah")
        hit = assist.maybe_assist(
            self.conn, OWNER_USER, "make a video of you dancing", "hannah")
        # First job is still active, so the one-at-a-time lane answers
        # first; finish it and the cap answers instead.
        self.assertIn("still rendering", hit["brief"])
        self.conn.execute(
            "UPDATE sogni_plan_jobs SET status = 'done'")
        self.conn.commit()
        hit = assist.maybe_assist(
            self.conn, OWNER_USER, "make a video of you dancing", "hannah")
        self.assertIn("today's video limit", hit["brief"])
        self.assertEqual(len(self._jobs()), 1)


class SogniMentionLaneTest(SogniPlanCase):
    """2026-10-09 denial fix: ANY Sogni mention in Hannah's thread
    routes to the plan lane before the brain, and a short confirmation
    right after a Sogni exchange stays in the lane too."""
    MSG1 = "Use my sogni account from now on to generate pics and videos"
    MSG2 = "Yes they do I just had it put in"

    def test_screenshot_messages_no_session_get_login_ask(self):
        hit = assist.maybe_assist(self.conn, OWNER_USER, self.MSG1,
                                  "hannah")
        self.assertIsNotNone(hit)
        self.assertEqual(
            hit["brief"],
            assist.FIXED_REPLY_PREFIX + assist._SOGNI_NO_SESSION_REPLY)
        self.assertEqual(self._jobs(), [])
        self.assertEqual(self.spawned, [])
        # His pushback stays in the lane (the topic is open now).
        hit2 = assist.maybe_assist(self.conn, OWNER_USER, self.MSG2,
                                   "hannah")
        self.assertEqual(
            hit2["brief"],
            assist.FIXED_REPLY_PREFIX + assist._SOGNI_NO_SESSION_REPLY)
        self.assertEqual(self._jobs(), [])

    def test_screenshot_messages_with_session_confirm_signed_in(self):
        assist.store_sogni_session(self.conn, dict(TOKENS))
        hit = assist.maybe_assist(self.conn, OWNER_USER, self.MSG1,
                                  "hannah")
        self.assertEqual(
            hit["brief"],
            assist.FIXED_REPLY_PREFIX
            + assist._SOGNI_SIGNED_IN_REPLY)
        hit2 = assist.maybe_assist(self.conn, OWNER_USER, self.MSG2,
                                   "hannah")
        self.assertEqual(
            hit2["brief"],
            assist.FIXED_REPLY_PREFIX
            + assist._SOGNI_SIGNED_IN_REPLY)
        self.assertEqual(self._jobs(), [])

    def test_mention_without_make_ask_no_session(self):
        hit = assist.maybe_assist(
            self.conn, OWNER_USER, "are you on my sogni yet?", "hannah")
        self.assertIsNotNone(hit)
        self.assertEqual(
            hit["brief"],
            assist.FIXED_REPLY_PREFIX + assist._SOGNI_NO_SESSION_REPLY)

    def test_continuation_needs_open_topic(self):
        # The pushback shape alone must not hijack ordinary chat.
        hit = assist.maybe_assist(self.conn, OWNER_USER, self.MSG2,
                                  "hannah")
        self.assertIsNone(hit)

    def test_loose_media_ask_with_subject_renders(self):
        assist.store_sogni_session(self.conn, dict(TOKENS))
        hit = assist.maybe_assist(
            self.conn, OWNER_USER,
            "make me pics of you dancing through sogni", "hannah")
        self.assertIsNotNone(hit)
        self.assertIn("rendering now", hit["brief"])
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["prompt"], "you dancing")
        self.assertEqual(jobs[0]["billing_mode"], "subscription")

    def test_loose_media_ask_no_session_asks_login(self):
        hit = assist.maybe_assist(
            self.conn, OWNER_USER,
            "make me pics of you dancing through sogni", "hannah")
        self.assertEqual(
            hit["brief"],
            assist.FIXED_REPLY_PREFIX + assist._SOGNI_NO_SESSION_REPLY)
        self.assertEqual(self._jobs(), [])

    def test_mention_lane_only_in_hannah_thread(self):
        hit = assist.maybe_assist(self.conn, OWNER_USER, self.MSG1,
                                  "chloe")
        # Not the Sogni lane: no fixed login ask leaks to other girls.
        self.assertTrue(hit is None
                        or not hit["brief"].startswith(
                            assist.FIXED_REPLY_PREFIX))

    def test_non_owner_gets_nothing(self):
        hit = assist.maybe_assist(
            self.conn, {"user_id": "someone_else", "tier": "plus"},
            self.MSG1, "hannah")
        self.assertIsNone(hit)
        self.assertEqual(self._jobs(), [])

    def test_detect_sogni_media_ask(self):
        self.assertEqual(
            assist.detect_sogni_media_ask("generate pics and videos"),
            "")
        self.assertEqual(
            assist.detect_sogni_media_ask(
                "make me pics of you dancing through sogni"),
            "you dancing")
        self.assertIsNone(
            assist.detect_sogni_media_ask("use my sogni account"))
        self.assertIsNone(
            assist.detect_sogni_media_ask("hey beautiful"))

    def test_no_api_key_in_the_new_lane(self):
        for fn in (assist._handle_sogni_lane,
                   assist.detect_sogni_media_ask):
            self.assertNotIn("SOGNI_API_KEY", inspect.getsource(fn),
                             fn.__name__)

    def test_brief_states_sogni_truth(self):
        self.assertIn("Sogni Unlimited plan", assist._OWNER_BRIEF)
        self.assertIn("systems don't work that way", assist._OWNER_BRIEF)


class WorkerTest(SogniPlanCase):
    """The worker opens its own db.connect() — so these run against a
    real temp-file database (config.DB_PATH pointed at it), with the
    character roster stubbed (no network, no avatar fetch)."""

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.mkdtemp(prefix="sogni-plan-test-")
        self._db_path = os.path.join(self._tmpdir, "test.db")
        self._old_db_path = config.DB_PATH
        config.DB_PATH = self._db_path
        self.conn.close()
        self.conn = sqlite3.connect(self._db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE conversation (id INTEGER PRIMARY KEY "
            "AUTOINCREMENT, user_id TEXT, character_id TEXT, role TEXT,"
            " text TEXT, created_at TEXT, attachments TEXT)")
        assist.ensure_tables(self.conn)
        self.conn.commit()
        self._patches.append(_Patched(
            characters, "get_character",
            lambda cid, user_id=None: {"id": "hannah", "name": "Hannah",
                                       "avatar_url": ""}))
        self._patches[-1].__enter__()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        super().tearDown()

    def _enqueue_with_session(self, subject="you waving"):
        assist.store_sogni_session(self.conn, dict(TOKENS))
        hit = assist.maybe_assist(
            self.conn, OWNER_USER,
            f"make a video of {subject}", "hannah")
        self.assertIsNotNone(hit)
        return self._jobs()[0]["id"]

    def test_done_delivers_video_bubble_and_rotates_session(self):
        job_id = self._enqueue_with_session()
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.write(fd, b"\x00" * 2048)
        os.close(fd)
        outcome = {"status": "done", "project_id": "proj-123",
                   "path": path, "detail": "billingMode=subscription"}

        def fake_run(session, prompt, ref_path, model=None):
            self.assertEqual(session, TOKENS)
            self.assertIn("Photorealistic", prompt)
            self.assertIsNone(ref_path)  # no avatar fixture here
            return dict(outcome), dict(TOKENS_ROTATED)

        try:
            with _Patched(assist, "_sogni_plan_run", fake_run):
                assist._run_sogni_plan_job(job_id)
        finally:
            os.unlink(path)
        job = self._jobs()[0]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["project_id"], "proj-123")
        self.assertTrue(job["video_url"].startswith("/generated/"))
        self.assertEqual(job["delivered"], 1)
        # Rotated token pair persisted (refresh really refreshes).
        self.assertEqual(assist.stored_sogni_session(self.conn),
                         TOKENS_ROTATED)
        # The video landed in his thread as a video attachment.
        turns = self._turns()
        self.assertEqual(turns[-1]["role"], "hannah")
        import json as _json
        atts = _json.loads(turns[-1]["attachments"] or "[]")
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0]["kind"], "video")
        self.assertEqual(atts[0]["url"], job["video_url"])
        self.assertEqual(atts[0]["size"], 2048)

    def test_blocked_reports_filter_honestly(self):
        job_id = self._enqueue_with_session("you doing that thing")

        def fake_run(session, prompt, ref_path, model=None):
            return ({"status": "blocked", "project_id": "proj-9",
                     "path": None, "detail": "Sensitive content detected"},
                    None)

        with _Patched(assist, "_sogni_plan_run", fake_run):
            assist._run_sogni_plan_job(job_id)
        job = self._jobs()[0]
        self.assertEqual(job["status"], "blocked")
        self.assertIsNone(job["video_url"])
        turns = self._turns()
        self.assertIn("filter stopped", turns[-1]["text"])
        import json as _json
        self.assertEqual(_json.loads(turns[-1]["attachments"] or "[]"), [])

    def test_session_dead_clears_and_asks_again(self):
        job_id = self._enqueue_with_session()

        def fake_run(session, prompt, ref_path, model=None):
            return ({"status": "session_dead", "project_id": None,
                     "path": None, "detail": "Refresh token expired"},
                    None)

        with _Patched(assist, "_sogni_plan_run", fake_run):
            assist._run_sogni_plan_job(job_id)
        self.assertEqual(self._jobs()[0]["status"], "session_dead")
        self.assertIsNone(assist.stored_sogni_session(self.conn))
        self.assertIn("sign-in expired", self._turns()[-1]["text"])


if __name__ == "__main__":
    unittest.main()
