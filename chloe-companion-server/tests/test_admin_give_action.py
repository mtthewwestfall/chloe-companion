"""Admin custom give-action tests (2026-10-17).

Covers POST /api/v1/admin/girls/{girl_id}/requested-actions — the
admin write path that pairs with the requested-actions viewer: it
saves the action (same store the chat pipeline writes) and queues a
stage-99 give generation through the studio lane's spawn. Spawn is
mocked throughout: no Sogni session is ever touched here.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import characters, config, db, give_gen, store  # noqa: E402
from app import main as api  # noqa: E402

ADMIN_ATTR = "ADMIN" + chr(95) + "TOKEN"
ACTION = "bending over, exposing her thong"


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


class AdminGiveActionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_admin = getattr(config, ADMIN_ATTR)
        setattr(config, ADMIN_ATTR, "test-admin-secret")
        characters._cache = {"ts": 0.0, "chars": []}
        api._admin_create_girl({"id": "nova", "name": "Nova"})

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        setattr(config, ADMIN_ATTR, self._real_admin)
        characters._cache = {"ts": 0.0, "chars": []}
        self.tmp.cleanup()

    def _req(self, tok="test-admin-secret"):
        return _Req({"x-admin-token": tok} if tok else {})

    def _post(self, girl_id="nova", action=ACTION, tok="test-admin-secret"):
        body = api.RequestedActionIn(action=action)
        return api.admin_create_requested_action(girl_id, body, self._req(tok))

    def _gens(self, girl_id="nova"):
        conn = db.connect(self.db_path)
        try:
            return give_gen.list_give_generations(conn, girl_id=girl_id)
        finally:
            conn.close()

    # -- guard ----------------------------------------------------------
    def test_post_rejects_wrong_token(self):
        with mock.patch.object(give_gen, "spawn_give_generation_job"):
            with self.assertRaises(Exception) as ctx:
                self._post(tok="nope")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(self._gens(), [])

    # -- the queue itself -------------------------------------------------
    def test_queues_stage99_pending_and_spawns(self):
        with mock.patch.object(give_gen, "spawn_give_generation_job") as spawn:
            res = self._post()
        self.assertTrue(res["ok"])
        self.assertFalse(res["already_queued"])
        spawn.assert_called_once_with(res["generation_id"])
        gen = res["generation"]
        self.assertEqual(gen["girl_id"], "nova")
        self.assertEqual(gen["stage"], 99)
        self.assertEqual(gen["status"], "pending")
        self.assertEqual(gen["prompt"], ACTION)
        self.assertEqual(res["action"]["girl_id"], "nova")
        self.assertEqual(res["action"]["action_text"], ACTION)

    def test_action_saved_in_requested_actions(self):
        with mock.patch.object(give_gen, "spawn_give_generation_job"):
            self._post()
        conn = db.connect(self.db_path)
        try:
            actions = store.get_requested_actions(conn, "nova")
        finally:
            conn.close()
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["request_count"], 1)

    def test_repeat_call_dedups_generation_but_counts_action(self):
        with mock.patch.object(give_gen, "spawn_give_generation_job") as spawn:
            first = self._post()
            second = self._post()
        self.assertEqual(first["generation_id"], second["generation_id"])
        self.assertTrue(second["already_queued"])
        spawn.assert_called_once_with(first["generation_id"])
        self.assertEqual(len(self._gens()), 1)
        self.assertEqual(second["action"]["request_count"], 2)

    # -- validation -------------------------------------------------------
    def test_unknown_girl_rejected(self):
        with mock.patch.object(give_gen, "spawn_give_generation_job"):
            with self.assertRaises(Exception) as ctx:
                self._post(girl_id="ghost")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "unknown_character")

    def test_empty_action_rejected(self):
        with mock.patch.object(give_gen, "spawn_give_generation_job") as spawn:
            with self.assertRaises(Exception) as ctx:
                self._post(action="   ")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "action_required")
        spawn.assert_not_called()
        self.assertEqual(self._gens(), [])


if __name__ == "__main__":
    unittest.main()
