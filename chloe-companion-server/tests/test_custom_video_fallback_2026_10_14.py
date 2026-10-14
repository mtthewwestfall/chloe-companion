"""Custom-video fallback fix (2026-10-14): a definitively refused
render must not leave the request in an eternal, unexplained
`pending` — production showed all 8 historical requests stuck there
after Sogni's "5000: Sensitive content detected." refusals while the
client sheet kept saying "queued".

Pinned here:
- refusal -> honest terminal state + recorded reason + the closest
  preset from THAT GIRL'S OWN library delivered into the chat with
  her in-character swap line;
- throttle / session-dead / missing session -> parked back in
  `pending` with the waiting note recorded — NOT terminal, no
  fallback;
- the render-success path is unchanged;
- the backlog sweep never re-renders an already-refused request;
- the fallback never crosses girls (and is `failed`, not a stranger's
  clip, when she has no library of her own).
The render seam is mocked throughout — no real renders.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, config, custom_video, db, store  # noqa: E402
from app import main as api  # noqa: E402

USER = "cv_user"
SESSION = {"token": "tok-aaa", "refreshToken": "ref-bbb"}
REFUSAL = "5000: Sensitive content detected."
BROOKLYN_URL = "/media/cv-brooklyn-s1.mp4"
MIA_URL = "/media/cv-mia-s1.mp4"


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


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self._nc = _NoClose(self.conn)
        self._connect_patch = mock.patch.object(
            db, "connect", return_value=self._nc)
        self._connect_patch.start()
        store.get_or_create_user(self.conn, USER)
        self.conn.commit()

    def tearDown(self):
        self._connect_patch.stop()
        self.conn.close()

    # -- helpers ------------------------------------------------------
    def _req(self, girl, desc):
        return store.save_custom_request(self.conn, USER, girl, desc)

    def _row(self, rid):
        return dict(self.conn.execute(
            "SELECT * FROM custom_video_requests WHERE id = ?",
            (rid,)).fetchone())

    def _seed_preset(self, girl, url, stage=1):
        self.conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset, stage) "
            "VALUES (?, 'give', ?, 'give', ?)", (girl, url, stage))
        self.conn.commit()

    def _turns(self, girl):
        return [dict(r) for r in self.conn.execute(
            "SELECT role, text, attachments FROM conversation "
            "WHERE user_id = ? AND character_id = ?",
            (USER, girl)).fetchall()]

    def _login_run(self, req_id, outcome, extra_patches=()):
        patches = [
            mock.patch.object(assist, "stored_sogni_session",
                              return_value=SESSION),
            mock.patch.object(assist, "_fetch_first_frame",
                              return_value=None),
            mock.patch.object(assist, "_sogni_plan_run",
                              return_value=(outcome, None)),
            *extra_patches,
        ]
        for p in patches:
            p.start()
        try:
            return custom_video.run_custom_generation_with_login(req_id)
        finally:
            for p in patches:
                p.stop()


class RefusalTest(_Base):
    def test_refusal_closes_with_fallback_and_records_reason(self):
        self._seed_preset("brooklyn", BROOKLYN_URL)
        req = self._req("brooklyn", "Topless video")
        token = self._login_run(req["id"], {
            "status": "failed", "detail": REFUSAL, "path": None,
            "project_id": None})
        self.assertEqual(token, "fallback")
        row = self._row(req["id"])
        self.assertEqual(row["status"], "fallback")
        self.assertEqual(row["error"], REFUSAL)
        self.assertEqual(row["fallback_url"], BROOKLYN_URL)
        self.assertEqual(row["video_url"], "")
        turns = self._turns("brooklyn")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], custom_video._FALLBACK_LINE)
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["kind"], "video")
        self.assertEqual(atts[0]["url"], BROOKLYN_URL)

    def test_api_key_path_refusal_closes_with_fallback(self):
        self._seed_preset("brooklyn", BROOKLYN_URL)
        req = self._req("brooklyn", "Topless video")
        with mock.patch.object(
                custom_video, "_generate_custom_async",
                side_effect=RuntimeError(REFUSAL)):
            token = custom_video.run_custom_generation(req["id"])
        self.assertEqual(token, "fallback")
        row = self._row(req["id"])
        self.assertEqual(row["status"], "fallback")
        self.assertIn("Sensitive content detected", row["error"])
        self.assertEqual(row["fallback_url"], BROOKLYN_URL)


class ParkTest(_Base):
    def test_throttle_parks_not_terminal_no_fallback(self):
        self._seed_preset("brooklyn", BROOKLYN_URL)
        req = self._req("brooklyn", "Topless video")
        token = self._login_run(req["id"], {
            "status": "throttled",
            "detail": "4056: Connection temporarily blocked due to "
                      "excessive reconnections",
            "throttle_seconds": 1800, "path": None,
            "project_id": None})
        self.assertEqual(token, "parked")
        row = self._row(req["id"])
        self.assertEqual(row["status"], "pending")
        self.assertEqual(
            row["error"], "waiting: Sogni throttled (retry after ~30 min)")
        self.assertEqual(row["fallback_url"], "")
        self.assertEqual(self._turns("brooklyn"), [])

    def test_session_dead_parks_after_resolve(self):
        self._seed_preset("brooklyn", BROOKLYN_URL)
        req = self._req("brooklyn", "Topless video")
        resolve = mock.patch.object(
            assist, "resolve_dead_sogni_session", return_value=None)
        with resolve as resolve_mock:
            token = self._login_run(req["id"], {
                "status": "session_dead", "detail": "", "path": None,
                "project_id": None})
            # The runner resolves the dead session (existing recovery
            # step) before classifying the outcome as a park.
            self.assertEqual(resolve_mock.call_count, 1)
        self.assertEqual(token, "parked")
        row = self._row(req["id"])
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["error"], "waiting: session unavailable")
        self.assertEqual(row["fallback_url"], "")
        self.assertEqual(self._turns("brooklyn"), [])

    def test_missing_session_parks_and_never_renders(self):
        req = self._req("brooklyn", "Topless video")
        with mock.patch.object(assist, "stored_sogni_session",
                               return_value=None), \
                mock.patch.object(assist, "_sogni_plan_run") as run_mock:
            token = custom_video.run_custom_generation_with_login(
                req["id"])
        self.assertEqual(token, "parked")
        self.assertEqual(run_mock.call_count, 0)
        row = self._row(req["id"])
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["error"], "waiting: session unavailable")


class SuccessTest(_Base):
    def test_success_path_unchanged(self):
        req = self._req("brooklyn", "dancing in a red dress")
        with mock.patch("os.path.getsize", return_value=10):
            token = self._login_run(req["id"], {
                "status": "done", "path": "/tmp/cv_done.mp4",
                "project_id": "p1", "detail": ""})
        self.assertEqual(token, "fulfilled")
        row = self._row(req["id"])
        self.assertEqual(row["status"], "fulfilled")
        self.assertEqual(row["video_url"], "/generated/cv_done.mp4")
        self.assertEqual(row["error"], "")
        self.assertEqual(row["fallback_url"], "")
        turns = self._turns("brooklyn")
        self.assertEqual(len(turns), 1)
        self.assertIn("I made that special video for you",
                      turns[0]["text"])


class CrossGirlTest(_Base):
    def test_fallback_never_crosses_girls(self):
        self._seed_preset("brooklyn", BROOKLYN_URL)
        self._seed_preset("mia", MIA_URL)
        refusal = {"status": "failed", "detail": REFUSAL, "path": None,
                   "project_id": None}
        req_b = self._req("brooklyn", "Topless video")
        self.assertEqual(
            self._login_run(req_b["id"], refusal), "fallback")
        self.assertEqual(self._row(req_b["id"])["fallback_url"],
                         BROOKLYN_URL)
        self.assertEqual(self._turns("mia"), [])
        req_m = self._req("mia", "Topless video")
        self.assertEqual(
            self._login_run(req_m["id"], refusal), "fallback")
        self.assertEqual(self._row(req_m["id"])["fallback_url"], MIA_URL)

    def test_no_library_of_her_own_closes_failed_not_a_strangers(self):
        self._seed_preset("mia", MIA_URL)  # only Mia has a library
        req = self._req("brooklyn", "Topless video")
        token = self._login_run(req["id"], {
            "status": "failed", "detail": REFUSAL, "path": None,
            "project_id": None})
        self.assertEqual(token, "failed")
        row = self._row(req["id"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], REFUSAL)
        self.assertEqual(row["fallback_url"], "")
        self.assertEqual(self._turns("brooklyn"), [])
        self.assertEqual(self._turns("mia"), [])


class SweepTest(_Base):
    def test_known_refused_is_not_rerendered(self):
        self._seed_preset("brooklyn", BROOKLYN_URL)
        known = self._req("brooklyn", "Topless video")
        other = self._req("mia", "dancing slowly")
        calls = []

        def fake_runner(request_id):
            calls.append(request_id)
            return "parked"

        with mock.patch.object(assist, "stored_sogni_session",
                               return_value=None), \
                mock.patch.object(custom_video,
                                  "run_custom_generation_with_login",
                                  side_effect=fake_runner), \
                mock.patch.object(custom_video, "run_custom_generation",
                                  side_effect=fake_runner):
            report = custom_video.run_custom_video_backlog_sweep(
                known_refused_ids=[known["id"]],
                known_refused_error=REFUSAL)
        # The known-refused row never reached a render runner; the
        # other pending row got its one attempt (and parked).
        self.assertEqual(calls, [other["id"]])
        row = self._row(known["id"])
        self.assertEqual(row["status"], "fallback")
        self.assertEqual(row["error"], REFUSAL)
        self.assertEqual(row["fallback_url"], BROOKLYN_URL)
        turns = self._turns("brooklyn")
        self.assertEqual(len(turns), 1)
        self.assertEqual(report["processed"], 2)
        self.assertEqual(report["fallback"], 1)
        self.assertEqual(report["parked"], 1)
        self.assertEqual(report["results"][known["id"]], "fallback")
        self.assertEqual(report["results"][other["id"]], "parked")
        other_row = self._row(other["id"])
        self.assertEqual(other_row["status"], "pending")


class StoreAndMigrationTest(_Base):
    def test_update_custom_request_records_error_and_fallback(self):
        req = self._req("brooklyn", "Topless video")
        store.update_custom_request(
            self.conn, req["id"], "fallback", error=REFUSAL,
            fallback_url=BROOKLYN_URL)
        row = self._row(req["id"])
        self.assertEqual(row["status"], "fallback")
        self.assertEqual(row["error"], REFUSAL)
        self.assertEqual(row["fallback_url"], BROOKLYN_URL)
        with self.assertRaises(ValueError):
            store.update_custom_request(self.conn, req["id"], "bogus")

    def test_migration_adds_columns_to_pre_fix_table(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE custom_video_requests ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id TEXT NOT NULL, girl_id TEXT NOT NULL,"
            " description TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " video_url TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        db._migrate_custom_video_columns(conn)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(custom_video_requests)").fetchall()}
        self.assertIn("error", cols)
        self.assertIn("fallback_url", cols)
        db._migrate_custom_video_columns(conn)  # idempotent
        conn.close()


class ClientContractTest(unittest.TestCase):
    def test_sheet_renders_terminal_states_as_finished(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "chloe-companion-app", "js", "app.js")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("r.status === 'fallback'", src)
        self.assertIn("she sent you one of hers instead", src)
        self.assertIn("r.status === 'failed'", src)


class SweepEndpointTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_token = config.ADMIN_TOKEN
        config.ADMIN_TOKEN = "cv-admin-token"
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, USER)
            self.req = store.save_custom_request(
                conn, USER, "brooklyn", "Topless video")
        finally:
            conn.close()

    def tearDown(self):
        api._conn = self._real_conn
        config.ADMIN_TOKEN = self._real_token
        self.tmp.cleanup()

    def test_sweep_endpoint_spawns_with_known_refused(self):
        from fastapi.testclient import TestClient
        seen = {}

        def fake_spawn(ids, error):
            seen["ids"] = ids
            seen["error"] = error
            return 1

        with mock.patch.object(custom_video,
                               "spawn_custom_video_backlog_sweep",
                               side_effect=fake_spawn):
            client = TestClient(api.app)
            resp = client.post(
                "/api/v1/admin/custom-videos/sweep-backlog",
                headers={"x-admin-token": "cv-admin-token"},
                json={"known_refused_ids": [self.req["id"]],
                      "known_refused_error": REFUSAL})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "queued": 1})
        self.assertEqual(seen["ids"], [self.req["id"]])
        self.assertEqual(seen["error"], REFUSAL)

    def test_sweep_endpoint_requires_admin(self):
        from fastapi.testclient import TestClient
        client = TestClient(api.app)
        resp = client.post("/api/v1/admin/custom-videos/sweep-backlog",
                           json={})
        self.assertEqual(resp.status_code, 401)

    def test_admin_list_accepts_terminal_status_filters(self):
        from fastapi.testclient import TestClient
        client = TestClient(api.app)
        for status in ("fallback", "failed"):
            resp = client.get(
                f"/api/v1/admin/custom-videos?status={status}",
                headers={"x-admin-token": "cv-admin-token"})
            self.assertEqual(resp.status_code, 200, status)
            self.assertEqual(resp.json(), {"requests": []})


if __name__ == "__main__":
    unittest.main()
