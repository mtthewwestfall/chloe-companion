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

Second fix (2026-10-13, later the same day): ONE global Sogni gate +
ONE give processor. The churn fix serialized sign-ins, but production
then showed two give processors (boot auto-loop + manual
generate-daily) rendering concurrently: each created its own Sogni
client, the second client's session superseded the first's, and both
batches died at the SDK's account check — no sign-in involved, so the
sign-in lock could not help. Coverage for assist._SOGNI_GATE /
give_gen's single processor:
- two concurrent triggers collapse to exactly one processor thread
  and every queued row is processed exactly once, including a row
  queued mid-drain and a stray row no trigger handed over,
- a give render and a her-day render never overlap inside the gate,
- the account check inside a give render runs under the same gate,
- a sign-in never overlaps a render (sign-ins run inside the gate).

Run:  python -m unittest tests.test_sogni_session_churn -v
"""
from __future__ import annotations

import asyncio
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

from app import assist, characters, config, db, give_gen, her_day  # noqa: E402

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


class _Probe:
    """Overlap recorder for instrumented Sogni sections (client
    creation's account check, renders, sign-ins) across threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.events = []  # (kind, thread name) on entry

    def enter(self, kind):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.events.append((kind, threading.current_thread().name))

    def exit(self, kind):
        with self._lock:
            self.active -= 1


def _fake_render(probe, delay=0.35, account_check=False):
    """Instrumented stand-in for assist._sogni_plan_render: the gated
    section's content. With account_check, first records the SDK's
    session check (account.me inside set_tokens at client creation)
    as its own probe section, like the real render does."""
    async def render(session, prompt, ref_path, model=None, kind="video",
                     duration=None):
        if account_check:
            probe.enter("account_check")
            try:
                await asyncio.sleep(delay / 2)
            finally:
                probe.exit("account_check")
        probe.enter("render")
        try:
            await asyncio.sleep(delay)
        finally:
            probe.exit("render")
        return ({"status": "done", "project_id": "proj-1",
                 "path": "/tmp/sogni-fake.mp4", "detail": ""}, None)
    return render


class GateBase(unittest.TestCase):
    """Temp-file DB with a stored plan pair; db.connect rerouted to
    per-thread connections on it (workers open their own)."""

    def setUp(self):
        fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        seed = sqlite3.connect(self._path)
        seed.executescript(db.SCHEMA)
        seed.close()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in ("SOGNI_PLAN_SESSION", "SOGNI_API_KEY",
                    "SOGNI_LOGIN_USERNAME", "SOGNI_LOGIN_PASSWORD"):
            os.environ.pop(key, None)
        self._cfg = mock.patch.object(config, "SOGNI_API_KEY", "")
        self._cfg.start()
        conn = self._conn()
        try:
            assist.store_sogni_session(conn, PAIR_A)
        finally:
            conn.close()
        self._connect_patch = mock.patch.object(db, "connect", self._conn)
        self._connect_patch.start()

    def tearDown(self):
        self._connect_patch.stop()
        self._cfg.stop()
        self._env.stop()
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def _conn(self):
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _make_give_row(self, stage=1, day="2026-10-13"):
        conn = self._conn()
        try:
            return give_gen.create_give_generation(
                conn, "mara", stage, "test prompt", day, status="pending")
        finally:
            conn.close()

    def _run_give_job(self, gen_id):
        fd, ref = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        with mock.patch.object(characters, "get_character",
                               return_value={"name": "Mara",
                                             "avatar_url": "x"}), \
             mock.patch.object(characters, "owner_of",
                               return_value=None), \
             mock.patch.object(assist, "_fetch_first_frame",
                               return_value=ref):
            give_gen._run_give_generation_job(gen_id)

    def _run_herday_render(self):
        return her_day._render_image_plan(dict(PAIR_A), "a prompt", None)


class GateConcurrencyTest(GateBase):
    """A give render and a her-day render launched at the same
    moment must never be inside the gate together: max overlap 1."""

    def test_give_and_herday_renders_never_overlap(self):
        probe = _Probe()
        gen_id = self._make_give_row()
        results = {}

        def herday_side():
            results["herday"] = self._run_herday_render()

        with mock.patch.object(assist, "_sogni_plan_render",
                               _fake_render(probe, delay=0.35)):
            t1 = threading.Thread(target=self._run_give_job,
                                  args=(gen_id,), name="give-thread")
            t2 = threading.Thread(target=herday_side,
                                  name="herday-thread")
            t1.start()
            t2.start()
            t1.join(30)
            t2.join(30)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(probe.max_active, 1)
        self.assertEqual(results["herday"].get("status"), "done")
        conn = self._conn()
        try:
            row = give_gen.get_give_generation(conn, gen_id)
        finally:
            conn.close()
        self.assertTrue(row["url"])  # give job finished through the gate


class GateSessionCheckTest(GateBase):
    """The session check the SDK runs while a give render creates
    its client (account.me in set_tokens) is part of the gated
    section: it must not overlap another feature's render either."""

    def test_give_account_check_runs_under_gate(self):
        probe = _Probe()
        gen_id = self._make_give_row()
        with mock.patch.object(
                assist, "_sogni_plan_render",
                _fake_render(probe, delay=0.3, account_check=True)):
            t1 = threading.Thread(target=self._run_give_job,
                                  args=(gen_id,), name="give-thread")
            t2 = threading.Thread(target=self._run_herday_render,
                                  name="herday-thread")
            t1.start()
            t2.start()
            t1.join(30)
            t2.join(30)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(probe.max_active, 1)
        self.assertIn(("account_check", "give-thread"), probe.events)


class GateSignInTest(GateBase):
    """A sign-in creates a Sogni client too: it runs inside the
    gate, so it can never be live alongside a render."""

    def test_signin_never_overlaps_render(self):
        probe = _Probe()
        login_result = {}

        def fake_login(login):
            probe.enter("login")
            try:
                time.sleep(0.35)
                return dict(PAIR_B)
            finally:
                probe.exit("login")

        def do_login():
            conn = self._conn()
            try:
                login_result["session"] = \
                    assist.sogni_login_single_flight(conn, LOGIN)
            finally:
                conn.close()

        with mock.patch.object(assist, "_sogni_plan_login", fake_login), \
             mock.patch.object(assist, "_sogni_plan_render",
                               _fake_render(probe, delay=0.35)):
            t1 = threading.Thread(target=self._run_herday_render,
                                  name="render-thread")
            t2 = threading.Thread(target=do_login, name="login-thread")
            t1.start()
            t2.start()
            t1.join(30)
            t2.join(30)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(probe.max_active, 1)
        self.assertEqual(login_result.get("session"), PAIR_B)


class SingleProcessorTest(GateBase):
    """Two give triggers at once (the production pair: boot
    auto-loop + manual generate-daily) must collapse onto ONE
    processor thread, and every queued row — handed over, queued
    mid-drain, or stray in the DB — is processed exactly once."""

    def setUp(self):
        super().setUp()
        self.processed = []  # (gen_id, thread name)
        self._plock = threading.Lock()
        self._active = 0
        self.max_concurrent = 0

    def tearDown(self):
        deadline = time.time() + 10
        while give_gen._GIVE_PROCESSOR_ACTIVE and time.time() < deadline:
            time.sleep(0.05)
        super().tearDown()

    def _fake_job(self, gen_id):
        with self._plock:
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            time.sleep(0.15)
            with self._plock:
                self.processed.append(
                    (gen_id, threading.current_thread().name))
        finally:
            with self._plock:
                self._active -= 1

    def _wait_processed(self, count, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._plock:
                if len(self.processed) >= count:
                    return True
            time.sleep(0.05)
        return False

    def test_concurrent_triggers_one_processor_all_rows_once(self):
        with mock.patch.object(give_gen, "_run_give_generation_job",
                               self._fake_job), \
             mock.patch.object(characters, "get_characters",
                               return_value=[{"id": "mara",
                                              "name": "Mara"}]):
            self._make_give_row(stage=5, day="2026-10-12")  # stray row
            t1 = threading.Thread(
                target=give_gen.trigger_daily_give_generations,
                kwargs={"day": "2026-10-13"})
            t2 = threading.Thread(
                target=give_gen.trigger_daily_give_generations,
                kwargs={"day": "2026-10-14"})
            t1.start()
            t2.start()
            t1.join(30)
            t2.join(30)
            self.assertTrue(self._wait_processed(1),
                            "processor never started")
            # Queued mid-drain via a direct hand-over while the
            # processor is running: the SAME processor must pick it
            # up (this call must not become a second processor).
            extra = self._make_give_row(stage=6, day="2026-10-12")
            give_gen._process_queued_give_generations([extra])
            total = 9 + 9 + 2
            self.assertTrue(
                self._wait_processed(total),
                f"processed {len(self.processed)} of {total}")
            deadline = time.time() + 10
            while give_gen._GIVE_PROCESSOR_ACTIVE \
                    and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(give_gen._GIVE_PROCESSOR_ACTIVE)
        ids = [g for g, _t in self.processed]
        self.assertEqual(len(ids), total)
        self.assertEqual(len(ids), len(set(ids)))  # each exactly once
        threads = {t for _g, t in self.processed}
        self.assertEqual(len(threads), 1)  # one processor thread
        self.assertEqual(self.max_concurrent, 1)


if __name__ == "__main__":
    unittest.main()
