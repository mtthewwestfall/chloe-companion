"""One-feed tests (2026-10-19).

Admin-injected Her Day moments posted "to everyone" (blank user_id)
must be ONE global row (store.HER_DAY_GLOBAL_USER) that every user's
feed read includes — not one copy per account. Targeted injects stay
personal. A personal row shadows a global duplicate (same girl +
media), so leftover fan-out copies never double-show.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import characters, config, db, store  # noqa: E402
from app import main as api  # noqa: E402

ADMIN_ATTR = "ADMIN" + chr(95) + "TOKEN"
_PNG_B64 = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")).decode("ascii")


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


class OneFeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_gen = config.GENERATED_DIR
        config.GENERATED_DIR = os.path.join(self.tmp.name, "generated")
        self._real_admin = getattr(config, ADMIN_ATTR)
        setattr(config, ADMIN_ATTR, "test-admin-secret")
        characters._cache = {"ts": 0.0, "chars": []}
        for uid in ("u1", "u2", "u3"):
            self._mk_user(uid)

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        config.GENERATED_DIR = self._real_gen
        setattr(config, ADMIN_ATTR, self._real_admin)
        characters._cache = {"ts": 0.0, "chars": []}
        self.tmp.cleanup()

    def _mk_user(self, uid):
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO users (user_id, name, email, password_hash,"
                " email_verified_at, tier, messages_remaining,"
                " messages_used_total) VALUES (?, ?, ?, 'x',"
                " '2026-10-19 00:00:00', 'basic', 300, 0)",
                (uid, uid, f"{uid}@example.com"))
            conn.commit()
        finally:
            conn.close()

    def _req(self):
        return _Req({"x-admin-token": "test-admin-secret"})

    def _inject(self, user_id=""):
        body = api.HerDayInjectIn(
            girl_id="chloe", caption="morning class done",
            image_b64=_PNG_B64, filename="moment.png", user_id=user_id)
        return api.admin_her_day_inject(body, self._req())

    def _rows(self, media_url):
        conn = db.connect(self.db_path)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM her_day_posts WHERE media_url = ?",
                (media_url,)).fetchall()]
        finally:
            conn.close()

    def _feed_media(self, user_id):
        conn = db.connect(self.db_path)
        try:
            return [p["media_url"] for p in
                    store.get_her_day_posts(conn, user_id)]
        finally:
            conn.close()

    def test_blank_inject_writes_one_global_row(self):
        out = self._inject("")
        self.assertEqual(out["created"], 1)
        rows = self._rows(out["media_url"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], store.HER_DAY_GLOBAL_USER)

    def test_global_post_in_every_users_feed(self):
        out = self._inject("")
        for uid in ("u1", "u2", "u3"):
            self.assertIn(out["media_url"], self._feed_media(uid))

    def test_targeted_inject_stays_personal(self):
        out = self._inject("u1")
        rows = self._rows(out["media_url"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], "u1")
        self.assertIn(out["media_url"], self._feed_media("u1"))
        self.assertNotIn(out["media_url"], self._feed_media("u2"))

    def test_personal_row_shadows_global_duplicate(self):
        out = self._inject("")
        conn = db.connect(self.db_path)
        try:
            store.save_her_day_post(conn, "u1", "chloe", post_type="image",
                                    media_url=out["media_url"],
                                    caption="morning class done")
        finally:
            conn.close()
        self.assertEqual(self._feed_media("u1").count(out["media_url"]), 1)
        self.assertEqual(self._feed_media("u2").count(out["media_url"]), 1)

    def test_public_feed_includes_global_post(self):
        out = self._inject("")
        conn = db.connect(self.db_path)
        try:
            posts = store.get_public_her_day_posts(conn, limit=24)
        finally:
            conn.close()
        self.assertIn(out["media_url"], [p["media_url"] for p in posts])


if __name__ == "__main__":
    unittest.main()
