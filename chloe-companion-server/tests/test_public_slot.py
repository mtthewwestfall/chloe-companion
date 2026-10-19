"""Public feed slot tests (2026-10-19).

GET /today shows ONE pictured post per character per day. A global
post (admin-posted to everyone, user_id = store.HER_DAY_GLOBAL_USER)
wins her day's slot over auto-generated per-user renders; the
earliest global wins among globals. With no global post, the
earliest pictured post wins, exactly as before. Returned rows never
expose user_id.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import db, store  # noqa: E402


class PublicSlotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _post(self, user_id, media, created, caption="x"):
        row = store.save_her_day_post(
            self.conn, user_id, "regan", post_type="image",
            media_url=media, caption=caption)
        store.set_her_day_post_times(
            self.conn, row["id"], created,
            created[:10] + " 23:59:59" if False else
            "2026-10-20 " + created[11:])
        return row

    def _public_media(self):
        posts = store.get_public_her_day_posts(self.conn, limit=24)
        regan = [p for p in posts if p["girl_id"] == "regan"]
        return regan

    def test_global_later_beats_personal_earlier(self):
        self._post("u1", "/generated/auto.jpg", "2026-10-19 08:00:00")
        self._post(store.HER_DAY_GLOBAL_USER, "/generated/admin.jpg",
                   "2026-10-19 09:00:00")
        got = self._public_media()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["media_url"], "/generated/admin.jpg")

    def test_earliest_global_wins_among_globals(self):
        self._post(store.HER_DAY_GLOBAL_USER, "/generated/g2.jpg",
                   "2026-10-19 09:00:00")
        self._post(store.HER_DAY_GLOBAL_USER, "/generated/g1.jpg",
                   "2026-10-19 08:30:00")
        got = self._public_media()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["media_url"], "/generated/g1.jpg")

    def test_personal_only_keeps_earliest(self):
        self._post("u1", "/generated/p2.jpg", "2026-10-19 09:00:00")
        self._post("u2", "/generated/p1.jpg", "2026-10-19 08:00:00")
        got = self._public_media()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["media_url"], "/generated/p1.jpg")

    def test_global_earlier_stays(self):
        self._post(store.HER_DAY_GLOBAL_USER, "/generated/admin.jpg",
                   "2026-10-19 08:00:00")
        self._post("u1", "/generated/auto.jpg", "2026-10-19 09:00:00")
        got = self._public_media()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["media_url"], "/generated/admin.jpg")

    def test_no_user_id_exposed(self):
        self._post(store.HER_DAY_GLOBAL_USER, "/generated/admin.jpg",
                   "2026-10-19 08:00:00")
        got = self._public_media()
        self.assertEqual(len(got), 1)
        self.assertNotIn("user_id", got[0])


if __name__ == "__main__":
    unittest.main()
