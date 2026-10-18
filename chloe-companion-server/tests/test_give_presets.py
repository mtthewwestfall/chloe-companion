"""Give-presets sheet counts (2026-10-18).

Bug: the 'Send a video' sheet showed Brooklyn "Love — 93 videos" —
Chloe's library size — next to a stale bundled "11", every row twice.
The endpoint half of that story lives here:

  * give_presets added the girl_media row count and the staged-file
    count raw, double-counting every clip that lives in both (a row
    points at the very file the dir scan finds). Brooklyn's real
    distinct library is her 40 rows; the endpoint said 53.
  * The count must be the chatting girl's OWN distinct clips only —
    never another girl's rows or files.

These tests pin: one Love row; distinct count (rows + staged files
not already registered as rows); strict per-girl scoping; owner-scoped
girls 404 for everyone but their owner.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException  # noqa: E402

from app import characters, config, db, store  # noqa: E402
from app import main as api  # noqa: E402

OWNER = "owner_1"


def _char(cid, name, owner=None):
    return {"id": cid, "name": name, "avatar_url": "", "persona": "",
            "companion_voice": "", "blurb": "", "door_title": "",
            "sort_order": 1, "owner_user_id": owner}


class _Req:
    def __init__(self, token=None):
        self.headers = {"authorization": f"Bearer {token}"} if token else {}


class GivePresetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path

        self._real_cache = characters._cache
        characters._cache = {"ts": time.time(), "chars": [], "local": [
            _char("brooklyn", "Brooklyn", owner=OWNER)]}

        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, OWNER, "Matty")
            self.token = "tok-owner"
            store.create_session(conn, OWNER, self.token)
            # characters.owner_of reads local_characters from the DB:
            # Brooklyn is owner-scoped to OWNER there.
            store.upsert_local_character(conn, {
                "id": "brooklyn", "name": "Brooklyn", "persona": "",
                "owner_user_id": OWNER})
            rows = [
                # Brooklyn: 3 rows. Row 1 points at a staged file that
                # also sits in the media dir (the double-count case).
                ("brooklyn",
                 "https://companion.lockeddoor.ai/media/brooklyn-give-stage1.mp4", 1),
                ("brooklyn",
                 "https://companion.lockeddoor.ai/generated/sogni-vid-aaa.mp4", 8),
                ("brooklyn",
                 "https://companion.lockeddoor.ai/generated/sogni-vid-bbb.mp4", 9),
                # Chloe: 2 rows, one of them also a staged file.
                ("chloe",
                 "https://companion.lockeddoor.ai/generated/sogni-vid-ccc.mp4", 8),
                ("chloe",
                 "https://companion.lockeddoor.ai/media/chloe-give-stage2.mp4", 2),
            ]
            for gid, url, stage in rows:
                conn.execute(
                    "INSERT INTO girl_media (girl_id, url, kind, preset, stage)"
                    " VALUES (?, ?, 'give', 'give', ?)", (gid, url, stage))
            conn.commit()
        finally:
            conn.close()

        # Staged media dir: brooklyn stage1 (dup of her row), brooklyn
        # stage5 (new), chloe stage2 (dup of her row), chloe stage3
        # (new), mara stage1 (another girl's — must never count).
        self.media_dir = os.path.join(self.tmp.name, "media")
        os.makedirs(self.media_dir)
        for fn in ("brooklyn-give-stage1.mp4", "brooklyn-give-stage5.mp4",
                   "chloe-give-stage2.mp4", "chloe-give-stage3.mp4",
                   "mara-give-stage1.mp4"):
            with open(os.path.join(self.media_dir, fn), "wb") as fh:
                fh.write(b"\x00")
        self._real_media_dir = api._pwa_media_dir
        api._pwa_media_dir = lambda: self.media_dir  # noqa: E731

    def tearDown(self):
        api._conn = self._real_conn
        api._pwa_media_dir = self._real_media_dir
        config.DB_PATH = self._real_db_path
        characters._cache = self._real_cache
        self.tmp.cleanup()

    def test_brooklyn_one_row_own_distinct_count(self):
        res = api.give_presets("brooklyn", _Req(self.token))
        self.assertEqual(len(res["presets"]), 1)
        row = res["presets"][0]
        self.assertEqual(row["name"], "love")
        # 3 rows + 1 staged file not already a row (stage5) = 4.
        # The raw double-count would have said 5.
        self.assertEqual(row["count"], 4)

    def test_brooklyn_payload_has_no_other_girls_media(self):
        res = api.give_presets("brooklyn", _Req(self.token))
        blob = json.dumps(res)
        self.assertNotIn("chloe", blob)
        self.assertNotIn("mara", blob)
        self.assertNotIn("sogni-vid-ccc", blob)

    def test_brooklyn_hidden_from_non_owner(self):
        with self.assertRaises(HTTPException) as cm:
            api.give_presets("brooklyn", _Req())
        self.assertEqual(cm.exception.status_code, 404)

    def test_chloe_public_count_distinct(self):
        res = api.give_presets("chloe", _Req())
        self.assertEqual(len(res["presets"]), 1)
        # 2 rows + 1 staged file not already a row (stage3) = 3.
        self.assertEqual(res["presets"][0]["count"], 3)

    def test_other_girl_counts_only_her_own_files(self):
        # Mara (public roster girl, no rows here) counts only her own
        # staged file — Brooklyn's and Chloe's files in the same dir
        # never leak into her total.
        res = api.give_presets("mara", _Req(self.token))
        self.assertEqual(len(res["presets"]), 1)
        self.assertEqual(res["presets"][0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
