"""Sogni login intake v2 + secure login box (2026-10-09).

Two live failures drove this: (1) "Sogni log in <u> <p>" (two-word
"log in") never matched the intake trigger, so the password sat
unscrubbed in the stored thread; (2) a correctly-shaped pair reached
the sign-in and was rejected with zero server-side trace — the SDK
error was swallowed, so nobody could tell a bad password from a
non-password (Google-created) account. And the owner asked for a
secure entry box instead of pasting credentials into chat.

Covered here: broadened triggers (two-word, brackets, labels after
the trigger), scrub-on-parse-failure, sanitized failure telemetry
(stage/code/message, canary-redacted), the retro sweep catching the
two-word shape, and POST /api/v1/assist/sogni-login — owner-only,
rate-limited, canary password in no DB row, log line, or response,
her fixed confirmation/failure lines landed in-thread. The chat
intake stays as fallback (its own suites pin that behavior).

Run: python -m unittest tests.test_sogni_login_box -v
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException  # noqa: E402

from app import assist, db, store  # noqa: E402
from app import main as api  # noqa: E402

OWNER = {"user_id": "owner_1", "tier": "basic"}
SESSION = {"token": "tok-aaa", "refreshToken": "ref-bbb"}
USER = "skyler.r"
CANARY = "CANARY-pw-9142!"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    assist.ensure_tables(conn)
    return conn


class IntakeBase(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        self.conn = _conn()
        self.logins = []

        def fake_login(login):
            self.logins.append(login)
            return dict(SESSION)

        self.patcher = mock.patch.object(assist, "_sogni_plan_login",
                                         fake_login)
        self.patcher.start()
        assist._sogni_login_fail_times.clear()

    def tearDown(self):
        self.patcher.stop()
        assist._sogni_login_fail_times.clear()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def say(self, text, cid="hannah", user=OWNER):
        store.log_turn(self.conn, user["user_id"], "user", text, cid)
        return assist.maybe_assist(self.conn, user, text, cid)

    def stored_texts(self):
        return [r["text"] for r in self.conn.execute(
            "SELECT text FROM conversation WHERE role = 'user' "
            "ORDER BY id").fetchall()]


class BroadTriggerTest(IntakeBase):
    def test_two_word_log_in_signs_in_and_scrubs(self):
        res = self.say(f"Sogni log in {USER} {CANARY}")
        self.assertIn("signed into your sogni", res["brief"])
        self.assertEqual(self.logins, [(USER, CANARY)])
        self.assertEqual(self.stored_texts(), ["[sogni login]"])

    def test_bracketed_pair_is_unwrapped(self):
        res = self.say(f"sogni login <{USER}> <{CANARY}>")
        self.assertIn("signed into your sogni", res["brief"])
        # The SDK must get the real pair, not the bracketed tokens.
        self.assertEqual(self.logins, [(USER, CANARY)])
        self.assertEqual(self.stored_texts(), ["[sogni login]"])

    def test_labels_after_trigger(self):
        res = self.say(f"sogni login username: {USER} password: {CANARY}")
        self.assertIn("signed into your sogni", res["brief"])
        self.assertEqual(self.logins, [(USER, CANARY)])

    def test_extractor_shapes(self):
        ex = assist.extract_sogni_login
        self.assertEqual(ex(f"sogni login {USER} {CANARY}"),
                         (USER, CANARY))
        self.assertEqual(ex(f"sogni log in {USER} {CANARY}"),
                         (USER, CANARY))
        self.assertEqual(ex(f"SOGNI LOG IN {USER} {CANARY}"),
                         (USER, CANARY))
        self.assertEqual(ex(f"sogni login <{USER}> <{CANARY}>"),
                         (USER, CANARY))
        self.assertEqual(ex(f"sogni login [{USER}] [{CANARY}]"),
                         (USER, CANARY))
        self.assertIsNone(ex("sign into sogni please"))
        self.assertIsNone(ex("sogni login"))
        self.assertIsNone(ex("what's a good password manager?"))

    def test_bracketed_stamp_in_retro_sweep(self):
        store.log_turn(self.conn, "owner_1", "user",
                       f"Sogni log in {USER} {CANARY}", "hannah")
        store.log_turn(self.conn, "owner_1", "user",
                       f"sogni login <{USER}> <{CANARY}>", "mia")
        store.log_turn(self.conn, "owner_1", "user",
                       "hey babe, how's the warmup?", "hannah")
        n = assist.scrub_sogni_credentials_from_history(self.conn)
        self.assertEqual(n, 2)
        self.assertEqual(self.stored_texts().count("[sogni login]"), 2)
        self.assertIn("hey babe, how's the warmup?",
                      self.stored_texts())


class ScrubOnParseFailureTest(IntakeBase):
    def test_single_token_after_trigger_is_scrubbed(self):
        res = self.say("sogni login onlyusername")
        self.assertIn("didn't take", res["brief"])
        self.assertEqual(self.logins, [])
        self.assertEqual(self.stored_texts(), ["[sogni login]"])
        self.assertIsNone(assist.stored_sogni_session(self.conn))

    def test_labels_without_values_are_scrubbed(self):
        res = self.say("sogni login username password")
        self.assertIn("didn't take", res["brief"])
        self.assertEqual(self.logins, [])
        self.assertEqual(self.stored_texts(), ["[sogni login]"])

    def test_unparseable_offers_the_box(self):
        res = self.say("sogni login onlyusername")
        self.assertTrue(res.get("sogni_login_box"))

    def test_password_talk_still_untouched(self):
        chats = [
            "i should change my password, it's getting old",
            "what's a good password manager?",
            "forget password stuff, tell me about the warmup",
        ]
        for text in chats:
            self.say(text)
        self.assertEqual(self.logins, [])
        self.assertEqual(self.stored_texts(), chats)


class TelemetryTest(IntakeBase):
    def test_failure_logs_stage_and_code_never_the_canary(self):
        def bad_login(login):
            raise assist._SogniPlanError(
                "login_failed", f"server said no to {CANARY}",
                stage="nonce", status=401, error_code=4001)

        with mock.patch.object(assist, "_sogni_plan_login", bad_login):
            out = io.StringIO()
            with redirect_stdout(out):
                res = self.say(f"sogni login {USER} {CANARY}")
        self.assertIn("didn't take", res["brief"])
        log = out.getvalue()
        self.assertIn("stage=nonce", log)
        self.assertIn("status=401", log)
        self.assertIn("code=4001", log)
        self.assertNotIn(CANARY, log)
        self.assertNotIn(USER, log)
        # The canary is nowhere persistent either.
        self.assertEqual(self.stored_texts(), ["[sogni login]"])
        raw = self.conn.execute(
            "SELECT value FROM assist_secrets").fetchall()
        self.assertTrue(all(CANARY not in r[0] for r in raw))

    def test_telemetry_redacts_credentials_from_sdk_message(self):
        class FakeApiError(Exception):
            status = 403
            error_code = 4100

        line = assist._sogni_failure_telemetry(
            FakeApiError(f"wallet rejected for {USER} / {CANARY}"),
            redact=(USER, CANARY))
        self.assertIn("error=FakeApiError", line)
        self.assertIn("status=403", line)
        self.assertIn("code=4100", line)
        self.assertNotIn(USER, line)
        self.assertNotIn(CANARY, line)


class BoxMarkerTest(IntakeBase):
    def test_no_session_ask_carries_the_box(self):
        hit = self.say("make a video of you waving")
        self.assertTrue(hit.get("sogni_login_box"))
        hit = self.say("use my sogni account for videos")
        self.assertTrue(hit.get("sogni_login_box"))

    def test_signed_in_reply_has_no_box(self):
        assist.store_sogni_session(self.conn, dict(SESSION))
        hit = self.say("use my sogni account for videos")
        self.assertFalse(hit.get("sogni_login_box"))


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


class SecureBoxEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        assist._sogni_login_fail_times.clear()
        conn = db.connect(self.db_path)
        try:
            for uid in ("owner_1", "someone_else"):
                conn.execute(
                    "INSERT INTO users (user_id, name, email, "
                    "password_hash, email_verified_at, tier, "
                    "messages_remaining, messages_used_total) "
                    "VALUES (?, 'T', ?, 'x', 'now', 'basic', 300, 0)",
                    (uid, f"{uid}@example.com"))
            store.create_session(conn, "owner_1", "tok-owner")
            store.create_session(conn, "someone_else", "tok-stranger")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        api._conn = self._real_conn
        assist._sogni_login_fail_times.clear()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _call(self, token=None, username=USER, password=CANARY,
              character="hannah"):
        headers = {"authorization": f"Bearer {token}"} if token else {}
        body = api.AssistSogniLoginIn(username=username,
                                      password=password,
                                      character=character)
        return api.assist_sogni_login(body, _Req(headers))

    def _all_stored_text(self):
        conn = db.connect(self.db_path)
        try:
            bits = [r[0] for r in conn.execute(
                "SELECT text FROM conversation").fetchall()]
            bits += [r[0] for r in conn.execute(
                "SELECT value FROM assist_secrets").fetchall()]
            return "\n".join(bits)
        finally:
            conn.close()

    def test_success_stores_session_and_confirms_in_thread(self):
        with mock.patch.object(assist, "_sogni_plan_login",
                               lambda login: dict(SESSION)) as _:
            out = io.StringIO()
            with redirect_stdout(out):
                res = self._call(token="tok-owner")
        self.assertEqual(res, {"ok": True,
                               "reply": assist.SOGNI_LOGIN_SAVED_REPLY})
        conn = db.connect(self.db_path)
        try:
            assist.ensure_tables(conn)
            self.assertEqual(assist.stored_sogni_session(conn), SESSION)
            turns = conn.execute(
                "SELECT role, text FROM conversation "
                "WHERE character_id = 'hannah'").fetchall()
        finally:
            conn.close()
        self.assertIn(("hannah", assist.SOGNI_LOGIN_SAVED_REPLY),
                      [(r[0], r[1]) for r in turns])
        self.assertNotIn(CANARY, self._all_stored_text())
        self.assertNotIn(CANARY, out.getvalue())
        self.assertNotIn(CANARY, str(res))

    def test_failure_is_honest_and_keeps_nothing(self):
        def bad_login(login):
            raise assist._SogniPlanError(
                "login_failed", f"nope {CANARY}", stage="login_post",
                status=401, error_code=4001)

        with mock.patch.object(assist, "_sogni_plan_login", bad_login):
            out = io.StringIO()
            with redirect_stdout(out):
                res = self._call(token="tok-owner")
        self.assertEqual(res["ok"], False)
        conn = db.connect(self.db_path)
        try:
            assist.ensure_tables(conn)
            self.assertIsNone(assist.stored_sogni_session(conn))
            turns = conn.execute(
                "SELECT text FROM conversation "
                "WHERE character_id = 'hannah'").fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in turns],
                         [assist.SOGNI_LOGIN_FAILED_REPLY])
        self.assertNotIn(CANARY, self._all_stored_text())
        self.assertNotIn(CANARY, out.getvalue())
        self.assertIn("stage=login_post", out.getvalue())

    def test_no_auth_is_401(self):
        with self.assertRaises(HTTPException) as cm:
            self._call(token=None)
        self.assertEqual(cm.exception.status_code, 401)

    def test_non_owner_is_403(self):
        with self.assertRaises(HTTPException) as cm:
            self._call(token="tok-stranger")
        self.assertEqual(cm.exception.status_code, 403)

    def test_non_assistant_character_is_400(self):
        with self.assertRaises(HTTPException) as cm:
            self._call(token="tok-owner", character="mara")
        self.assertEqual(cm.exception.status_code, 400)

    def test_rate_limit_locks_after_five_failures(self):
        def bad_login(login):
            raise assist._SogniPlanError("login_failed", "nope")

        with mock.patch.object(assist, "_sogni_plan_login", bad_login):
            for _ in range(5):
                res = self._call(token="tok-owner")
                self.assertEqual(res["ok"], False)
            with self.assertRaises(HTTPException) as cm:
                self._call(token="tok-owner")
        self.assertEqual(cm.exception.status_code, 429)

    def test_each_assistant_thread_accepted(self):
        with mock.patch.object(assist, "_sogni_plan_login",
                               lambda login: dict(SESSION)):
            for cid in ("mia", "jordan"):
                res = self._call(token="tok-owner", character=cid)
                self.assertEqual(res["ok"], True)


class FrontendMarkerTest(unittest.TestCase):
    def test_box_markup_and_wiring_present(self):
        root = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..",
            "chloe-companion-app"))
        index = open(os.path.join(root, "index.html")).read()
        appjs = open(os.path.join(root, "js", "app.js")).read()
        apijs = open(os.path.join(root, "js", "api.js")).read()
        self.assertIn('id="sogniLoginSheet"', index)
        self.assertIn('id="sogniLoginUser"', index)
        self.assertIn('id="sogniLoginPass"', index)
        self.assertIn('type="password"', index)
        self.assertIn('autocomplete="off"', index)
        self.assertIn("Enter Sogni login", appjs)
        self.assertIn("res.sogni_login_box", appjs)
        self.assertIn("addSogniLoginButton", appjs)
        self.assertIn("/api/v1/assist/sogni-login", apijs)


if __name__ == "__main__":
    unittest.main()
