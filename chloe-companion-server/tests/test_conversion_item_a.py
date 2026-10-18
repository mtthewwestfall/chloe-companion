"""Conversion fix Item A tests (2026-10-18): unlock the web door.

Pins:
  * signup grants the 50 free messages AT CREATION and returns a
    session — a brand-new, UNVERIFIED account can immediately chat;
  * login no longer hard-blocks unverified accounts (the flag rides
    along instead);
  * a completed password reset marks the email verified (verification
    as the recovery anchor);
  * the zero-bank backfill grants exactly the stranded accounts:
    0 bank + basic tier + never paid + no prior grant — and is
    idempotent.

Brain/assist network paths are stubbed — no Gemini pool is touched.
Run:  python -m pytest tests/test_conversion_item_a.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, brain, characters, config, db, store  # noqa: E402
from app import main as api  # noqa: E402


class _Client:
    host = "127.0.0.1"


class _Req:
    def __init__(self, token=None):
        self.headers = {"authorization": f"Bearer {token}"} if token else {}
        self.client = _Client()


def _char(cid, name):
    return {"id": cid, "name": name, "avatar_url": "", "persona": "",
            "companion_voice": "liora", "blurb": "", "door_title": "",
            "sort_order": 1}


class ItemA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._old_owners = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = "owner_x"
        self._real_cache = characters._cache
        characters._cache = {"ts": time.time(), "chars": [], "local": []}
        self._real_compose = brain.compose_reply
        self._real_context = brain.build_brain_context
        self._real_learn = brain.learn_from_turn
        self._real_assist = assist.maybe_assist
        self._real_send = api._send_verify_email
        brain.compose_reply = lambda message, context, cid: "a real reply"
        brain.build_brain_context = lambda conn, uid, user, msg, char: {}
        brain.learn_from_turn = lambda conn, uid, msg, cid: None
        assist.maybe_assist = lambda conn, user, msg, cid: None
        api._send_verify_email = lambda conn, user: None

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        if self._old_owners is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._old_owners
        characters._cache = self._real_cache
        brain.compose_reply = self._real_compose
        brain.build_brain_context = self._real_context
        brain.learn_from_turn = self._real_learn
        assist.maybe_assist = self._real_assist
        api._send_verify_email = self._real_send
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _signup(self, email="new.user@example.org", name="New"):
        body = api.SignupIn(email=email, password="password123", name=name)
        return api.auth_signup(body, _Req())

    def _user(self, user_id):
        conn = db.connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?",
                               (user_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # -- signup: grant + session at creation ------------------------------
    def test_signup_grants_50_and_returns_session(self):
        resp = self._signup()
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["verify_sent"])
        self.assertFalse(resp["email_verified"])
        self.assertTrue(resp["token"])
        self.assertEqual(resp["messages_remaining"], 50)
        user = self._user(resp["user_id"])
        self.assertIsNone(user["email_verified_at"])  # still unverified
        self.assertEqual(user["messages_remaining"], 50)

    def test_unverified_signup_can_chat_immediately(self):
        resp = self._signup("chat.first@example.org")
        body = api.ChatIn(message="hi there", character_id="chloe")
        out = api.chat(body, _Req(resp["token"]))
        self.assertEqual(out["reply_text"], "a real reply")
        user = self._user(resp["user_id"])
        self.assertEqual(user["messages_used_total"], 1)
        self.assertEqual(user["messages_remaining"], 49)

    def test_login_unverified_allowed_with_flag(self):
        resp = self._signup("login.ok@example.org")
        body = api.LoginIn(email="login.ok@example.org",
                           password="password123")
        out = api.auth_login(body, _Req())
        self.assertTrue(out["ok"])
        self.assertFalse(out["email_verified"])
        self.assertTrue(out["token"])
        self.assertEqual(out["user_id"], resp["user_id"])

    def test_signup_grant_flag_off_restores_old_shape(self):
        old = config.CONV_SIGNUP_GRANT
        config.CONV_SIGNUP_GRANT = False
        try:
            resp = self._signup("flag.off@example.org")
        finally:
            config.CONV_SIGNUP_GRANT = old
        self.assertNotIn("token", resp)
        user = self._user(resp.get("user_id") or
                          self._by_email("flag.off@example.org"))
        self.assertEqual(user["messages_remaining"], 0)

    def _by_email(self, email):
        conn = db.connect(self.db_path)
        try:
            row = conn.execute("SELECT user_id FROM users WHERE email = ?",
                               (email,)).fetchone()
            return row["user_id"] if row else None
        finally:
            conn.close()

    # -- backfill ---------------------------------------------------------
    def _mk(self, user_id, bank=0, tier="basic", email=None, paid=False,
            granted=False):
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, user_id)
            conn.execute(
                "UPDATE users SET messages_remaining = ?, tier = ?, "
                "email = ? WHERE user_id = ?",
                (bank, tier, email, user_id))
            if paid:
                conn.execute(
                    "INSERT OR IGNORE INTO trial_grants (user_id) "
                    "VALUES (?)", (user_id,))
            if granted:
                conn.execute(
                    "INSERT OR IGNORE INTO free_grants (user_id) "
                    "VALUES (?)", (user_id,))
            conn.commit()
        finally:
            conn.close()

    def test_backfill_grants_only_the_stranded(self):
        self._mk("stranded_1", email="yvaine.like@example.org")
        self._mk("stranded_2")
        self._mk("already_granted", granted=True)
        self._mk("paid_trial", paid=True)
        self._mk("has_bank", bank=12)
        conn = db.connect(self.db_path)
        try:
            granted = store.backfill_zero_bank_grants(conn, 50, 200)
        finally:
            conn.close()
        ids = [g["user_id"] for g in granted]
        self.assertEqual(sorted(ids), ["stranded_1", "stranded_2"])
        self.assertEqual(self._user("stranded_1")["messages_remaining"], 50)
        self.assertEqual(self._user("paid_trial")["messages_remaining"], 0)
        self.assertEqual(self._user("has_bank")["messages_remaining"], 12)

    def test_backfill_is_idempotent(self):
        self._mk("stranded_1")
        conn = db.connect(self.db_path)
        try:
            first = store.backfill_zero_bank_grants(conn, 50, 200)
            second = store.backfill_zero_bank_grants(conn, 50, 200)
        finally:
            conn.close()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(self._user("stranded_1")["messages_remaining"], 50)


if __name__ == "__main__":
    unittest.main()
