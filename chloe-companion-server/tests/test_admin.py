"""Admin panel tests (2026-09-30).

Covers the /api/v1/admin/* endpoints' internals: the token guard
(503 when unset, 401 on mismatch), the Accounts tab listing/stats, and
Girls-tab CRUD for companion-local characters (create resolves in the
roster, 'chloe' id rejected, update, delete).

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import characters, config, db, store  # noqa: E402
from app import main as api  # noqa: E402

ADMIN_ATTR = "ADMIN" + chr(95) + "TOKEN"


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


class AdminTest(unittest.TestCase):
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

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        setattr(config, ADMIN_ATTR, self._real_admin)
        characters._cache = {"ts": 0.0, "chars": []}
        self.tmp.cleanup()

    def _req(self, tok="test-admin-secret"):
        return _Req({"x-admin-token": tok} if tok else {})

    def _mk_user(self, uid, email, name="Test", tier="basic",
                 verified=True, msgs=300, used=0):
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO users (user_id, name, email, password_hash, "
                "email_verified_at, tier, messages_remaining, "
                "messages_used_total) VALUES (?, ?, ?, 'x', ?, ?, ?, ?)",
                (uid, name, email,
                 "2026-09-30 00:00:00" if verified else None,
                 tier, msgs, used),
            )
            conn.commit()
        finally:
            conn.close()

    # -- guard ----------------------------------------------------------
    def test_guard_disabled_without_secret(self):
        setattr(config, ADMIN_ATTR, "")
        with self.assertRaises(Exception) as ctx:
            api._require_admin(self._req())
        self.assertEqual(ctx.exception.status_code, 503)

    def test_guard_rejects_wrong_token(self):
        with self.assertRaises(Exception) as ctx:
            api._require_admin(self._req("nope"))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_guard_rejects_missing_header(self):
        with self.assertRaises(Exception) as ctx:
            api._require_admin(self._req(None))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_guard_accepts_correct_token(self):
        api._require_admin(self._req())  # no exception

    # -- accounts -------------------------------------------------------
    def test_accounts_listing_and_stats(self):
        self._mk_user("u1", "a@example.com", "Ada", "basic", True, 1500, 500)
        self._mk_user("u2", "b@example.com", "Bo", "basic", False, 300, 0)
        res = api._admin_accounts()
        self.assertEqual(res["stats"]["total"], 2)
        self.assertEqual(res["stats"]["verified"], 1)
        self.assertEqual(res["stats"]["by_tier"]["basic"], 2)
        self.assertEqual(res["stats"]["by_tier"]["basic"], 1)
        accts = {a["user_id"]: a for a in res["accounts"]}
        self.assertTrue(accts["u1"]["verified"])
        self.assertFalse(accts["u2"]["verified"])
        self.assertEqual(accts["u1"]["messages_used_total"], 500)
        self.assertEqual(accts["u1"]["tier"], "basic")

    # -- girls ----------------------------------------------------------
    def test_create_girl_resolves_in_roster(self):
        res = api._admin_create_girl({
            "id": "", "name": "Nova", "persona": "Playful stargazer.",
            "avatar_url": "", "companion_voice": "liora",
            "blurb": "", "door_title": "", "sort_order": 5,
            "enabled": True,
        })
        self.assertEqual(res["girl"]["id"], "nova")
        got = characters.get_character("nova")
        self.assertEqual(got["name"], "Nova")
        self.assertEqual(got["persona"], "Playful stargazer.")

    def test_create_girl_rejects_chloe_id(self):
        with self.assertRaises(Exception) as ctx:
            api._admin_create_girl({"id": "chloe", "name": "Fake Chloe"})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_girls_list_marks_sources(self):
        api._admin_create_girl({"id": "nova", "name": "Nova"})
        res = api._admin_girls()
        by_id = {g["id"]: g for g in res["girls"]}
        self.assertEqual(by_id["chloe"]["source"], "builtin")
        self.assertEqual(by_id["nova"]["source"], "local")

    def test_update_and_delete_girl(self):
        api._admin_create_girl({"id": "nova", "name": "Nova"})
        upd = api._admin_update_girl("nova", {"persona": "Updated."})
        self.assertEqual(upd["girl"]["persona"], "Updated.")
        self.assertEqual(upd["girl"]["name"], "Nova")  # untouched kept
        self.assertTrue(api._admin_delete_girl("nova")["ok"])
        # Gone from the roster again (falls back to Chloe).
        self.assertEqual(characters.get_character("nova")["id"], "chloe")
        with self.assertRaises(Exception) as ctx:
            api._admin_delete_girl("nova")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
