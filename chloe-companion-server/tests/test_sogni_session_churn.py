"""Sogni session-churn fix (2026-10-13): shared-login discipline.

Production failure mode: features sharing the single Sogni plan login
(give renders, her-day images, chat renders) signed in concurrently;
a new login invalidated the token pair the other features were about
to render with ("The account changed. Submit this request again.");
the session_dead handler then cleared the stored pair and tombstoned
the env seed, wiping the session for everyone, and the retry churn
re-invalidated any restored pair.

Coverage for assist.resolve_dead_sogni_session /
assist.sogni_login_single_flight:
- concurrent sign-in attempts collapse to one login (single-flight),
- session_dead with a NEWER stored pair returns it for one retry and
  clears nothing (no tombstone),
- the env-seed tombstone is written only after a confirmed-dead pair
  plus a failed re-login.

Run:  python -m unittest tests.test_sogni_session_churn -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, config, db  # noqa: E402

PAIR_A = {"token": "tok-a", "refreshToken": "ref-a"}
PAIR_B = {"token": "tok-b", "refreshToken": "ref-b"}
PAIR_C = {"token": "tok-c", "refreshToken": "ref-c"}
LOGIN = ("lockeddoor", "pw")


def _db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    return conn


def _tombstone(conn):
    row = conn.execute(
        "SELECT value FROM assist_secrets WHERE name = ?",
        (assist._SOGNI_ENV_DEAD,)).fetchone()
    return row[0] if row else None


class SessionChurnBase(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in ("SOGNI_PLAN_SESSION", "SOGNI_API_KEY",
                    "SOGNI_LOGIN_USERNAME", "SOGNI_LOGIN_PASSWORD"):
            os.environ.pop(key, None)
        self._cfg = mock.patch.object(config, "SOGNI_API_KEY", "")
        self._cfg.start()

    def tearDown(self):
        self._cfg.stop()
        self._env.stop()
        self.conn.close()


class SingleFlightTest(SessionChurnBase):
    """Four features hit session_dead on the same dead pair at once:
    exactly one sign-in runs; everyone ends up with the fresh pair."""

    def setUp(self):
        super().setUp()
        fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        seed = sqlite3.connect(self._path)
        seed.executescript(db.SCHEMA)
        seed.close()
        conn = self._file_conn()
        try:
            assist.store_sogni_session(conn, PAIR_A)  # the dead pair
        finally:
            conn.close()

    def tearDown(self):
        super().tearDown()
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def _file_conn(self):
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def test_concurrent_signins_collapse_to_one_login(self):
        calls = []

        def fake_login(login):
            calls.append(login)
            time.sleep(0.3)  # hold the gate so the others queue on it
            return dict(PAIR_B)

        results, errors = [], []

        def worker():
            conn = self._file_conn()
            try:
                results.append(assist.resolve_dead_sogni_session(
                    conn, dict(PAIR_A), login=LOGIN))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                conn.close()

        with mock.patch.object(assist, "_sogni_plan_login", fake_login):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 4)
        for got in results:
            self.assertEqual(got, PAIR_B)
        conn = self._file_conn()
        try:
            self.assertEqual(assist.stored_sogni_session(conn), PAIR_B)
        finally:
            conn.close()


class NewerPairTest(SessionChurnBase):
    def test_session_dead_with_newer_stored_pair_keeps_it(self):
        # The env seed is the pair that just died on this feature; the
        # DB row already holds a NEWER pair from another feature's
        # refresh. Recovery must hand back the newer pair for one
        # retry, sign in zero times, and clear/tombstone nothing.
        os.environ["SOGNI_PLAN_SESSION"] = json.dumps(PAIR_A)
        assist.store_sogni_session(self.conn, PAIR_B)

        def forbidden_login(login):
            raise AssertionError("must not sign in")

        with mock.patch.object(assist, "_sogni_plan_login",
                               forbidden_login):
            got = assist.resolve_dead_sogni_session(
                self.conn, dict(PAIR_A), login=LOGIN)
        self.assertEqual(got, PAIR_B)
        self.assertEqual(assist.stored_sogni_session(self.conn), PAIR_B)
        self.assertIsNone(_tombstone(self.conn))


class TombstoneTest(SessionChurnBase):
    def test_tombstone_only_after_failed_relogin(self):
        # Stored pair (env seed) IS the dead pair, and the re-login
        # fails: only now may the pair be cleared and the seed
        # tombstoned.
        os.environ["SOGNI_PLAN_SESSION"] = json.dumps(PAIR_A)
        self.assertEqual(assist.stored_sogni_session(self.conn), PAIR_A)

        def dead_login(login):
            raise RuntimeError("sign-in refused")

        with mock.patch.object(assist, "_sogni_plan_login", dead_login):
            got = assist.resolve_dead_sogni_session(
                self.conn, dict(PAIR_A), login=LOGIN)
        self.assertIsNone(got)
        self.assertIsNone(assist.stored_sogni_session(self.conn))
        self.assertEqual(_tombstone(self.conn),
                         assist._session_fingerprint(PAIR_A))

    def test_successful_relogin_writes_no_tombstone(self):
        os.environ["SOGNI_PLAN_SESSION"] = json.dumps(PAIR_A)
        with mock.patch.object(assist, "_sogni_plan_login",
                               return_value=dict(PAIR_C)) as login:
            got = assist.resolve_dead_sogni_session(
                self.conn, dict(PAIR_A), login=LOGIN)
        login.assert_called_once()
        self.assertEqual(got, PAIR_C)
        self.assertEqual(assist.stored_sogni_session(self.conn), PAIR_C)
        self.assertIsNone(_tombstone(self.conn))


if __name__ == "__main__":
    unittest.main()
