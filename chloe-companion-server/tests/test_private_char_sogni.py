"""Owner's private characters get the Sogni media lane (2026-10-11)."""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, characters, give_gen  # noqa: E402

OWNER = {"user_id": "owner_1", "tier": "basic"}
TOKENS = {"token": "tok-aaa", "refreshToken": "ref-bbb"}


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE conversation (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id TEXT, character_id TEXT, role TEXT, text TEXT,"
        " created_at TEXT, attachments TEXT)")
    assist.ensure_tables(conn)
    return conn


class _NoClose:
    def __init__(self, conn):
        self._c = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._c, name)


class PrivateCharacterSogniTest(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in
                     ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS", "SOGNI_API_KEY",
                      "SOGNI_PLAN_SESSION")}
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        os.environ.pop("SOGNI_API_KEY", None)
        os.environ.pop("SOGNI_PLAN_SESSION", None)
        self.conn = _conn()
        assist.store_sogni_session(self.conn, TOKENS)
        self.patches = [
            mock.patch.object(characters, "owner_of",
                              side_effect=lambda cid: "owner_1"
                              if cid == "luna" else None),
            mock.patch.object(assist, "_spawn_sogni_plan_job"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _jobs(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM sogni_plan_jobs ORDER BY id")]

    def test_lane_open_for_owned_private_character_only(self):
        self.assertTrue(assist.sogni_lane_character("owner_1", "luna"))
        self.assertTrue(assist.sogni_lane_character("owner_1", "mia"))
        self.assertFalse(assist.sogni_lane_character("someone", "luna"))
        self.assertFalse(assist.sogni_lane_character("owner_1", "chloe"))

    def test_video_ask_queues_plan_job_for_private_character(self):
        hit = assist.maybe_assist(
            self.conn, OWNER,
            "make a video of you dancing on the beach at sunset", "luna")
        self.assertEqual((hit or {}).get("sogni_media_kind"), "video")
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["character_id"], "luna")
        self.assertEqual(jobs[0]["kind"], "video")

    def test_picture_ask_queues_plan_image_job(self):
        hit = assist.maybe_assist(
            self.conn, OWNER,
            "make a picture of you in a red dress at a cafe", "luna")
        self.assertEqual((hit or {}).get("sogni_media_kind"), "image")
        self.assertEqual(self._jobs()[0]["kind"], "image")

    def test_adult_ask_blocked_unless_verified_fictional(self):
        with mock.patch.object(assist, "_private_character_verified_fictional",
                               return_value=False):
            hit = assist.maybe_assist(self.conn, OWNER,
                                      "send me a nude", "luna")
        self.assertTrue((hit or {}).get("sogni_adult_blocked"))
        self.assertEqual(self._jobs(), [])

    def test_image_job_renders_as_image(self):
        job_id = assist._sogni_plan_enqueue(
            self.conn, "owner_1", "luna", "red dress", kind="image",
            target_character_id="luna")
        girl = {"id": "luna", "name": "Luna", "avatar_url": ""}
        outcome = {"status": "done", "project_id": "p1",
                   "path": "/tmp/luna.jpg", "detail": ""}
        with mock.patch.object(assist.db, "connect",
                               return_value=_NoClose(self.conn)), \
             mock.patch.object(characters, "get_character", return_value=girl), \
             mock.patch.object(assist, "_sogni_plan_image_run",
                               return_value=(outcome, None)) as img_run, \
             mock.patch.object(give_gen, "spawn_give_generation_job") as gg, \
             mock.patch.object(assist, "_sogni_plan_deliver"):
            assist._run_sogni_plan_job(job_id)
        img_run.assert_called_once()
        gg.assert_not_called()
        row = self._jobs()[0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["media_url"], "/generated/luna.jpg")

    def test_give_worker_resolves_private_character_with_owner(self):
        with mock.patch.object(characters, "get_character",
                               return_value={"name": "Luna"}) as gc:
            gen = {"girl_id": "luna", "status": "pending", "stage": 1,
                   "prompt": "x"}
            with mock.patch.object(give_gen, "get_give_generation",
                                   return_value=gen), \
                 mock.patch.object(give_gen.db, "connect"), \
                 mock.patch.object(give_gen.assist, "stored_sogni_session",
                                   return_value=TOKENS), \
                 mock.patch.object(give_gen, "update_give_generation_status"), \
                 mock.patch.object(give_gen.assist, "_fetch_first_frame",
                                   return_value=None), \
                 mock.patch.object(give_gen.assist, "_sogni_plan_run",
                                   return_value=({"status": "failed"}, None)):
                give_gen._run_give_generation_job(1)
        gc.assert_called_with("luna", user_id="owner_1")


if __name__ == "__main__":
    unittest.main()
