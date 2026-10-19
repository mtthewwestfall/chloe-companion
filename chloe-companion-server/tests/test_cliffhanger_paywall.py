"""Improvement #7 tests (2026-10-19): paywall at the cliffhanger,
everywhere — the backend half.

The web chat wall already lands as her in-character line
(_empty_bank_reply) instead of an error. These tests pin the pieces
the other surfaces consume:

- the empty-bank chat reply carries paywall_due — True ONLY for a
  never-paid account (paid users who drained a monthly bank see the
  renewal line and no upsell signal, anywhere);
- the duo 402 detail is structured {code, paywall_due} (the web duo
  client keys on the status alone; the Telegram bot needs the flag);
- a media-pool entitlement refusal (monthly cap / empty pool) is a
  structured 402 whose tease names what she's holding back, in her
  voice, instead of the old bare "no_requests_left" string.

Run:  python -m pytest tests/test_cliffhanger_paywall.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException  # noqa: E402

from app import characters, config, db, duo, store  # noqa: E402
from app import main as api  # noqa: E402


class _Client:
    host = "127.0.0.1"


class _Req:
    def __init__(self, token=None):
        self.headers = {"authorization": f"Bearer {token}"} if token else {}
        self.client = _Client()


class CliffhangerPaywall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._old_owners = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        self._real_cache = characters._cache
        characters._cache = {"ts": time.time(), "chars": [], "local": []}
        self._real_sogni = config.SOGNI_API_KEY
        config.SOGNI_API_KEY = "test-key"
        self._real_girl_info = duo.girl_info
        duo.girl_info = lambda cid, user_id=None: {  # noqa: E731
            "id": cid, "name": cid.title(), "persona": "p"}

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        config.SOGNI_API_KEY = self._real_sogni
        duo.girl_info = self._real_girl_info
        if self._old_owners is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._old_owners
        characters._cache = self._real_cache
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _mk(self, user_id, bank=0, paid=False, token=None):
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, user_id)
            conn.execute(
                "UPDATE users SET messages_remaining = ? "
                "WHERE user_id = ?", (bank, user_id))
            if paid:
                conn.execute(
                    "INSERT OR IGNORE INTO trial_grants (user_id) "
                    "VALUES (?)", (user_id,))
            if token:
                store.create_session(conn, user_id, token)
            conn.commit()
        finally:
            conn.close()

    # -- chat wall ---------------------------------------------------------
    def test_chat_wall_reply_carries_paywall_due_for_unpaid(self):
        self._mk("tg_111", bank=0, token="tok_tg")
        body = api.ChatIn(message="hi", character_id="chloe")
        resp = api.chat(body, _Req("tok_tg"))
        self.assertEqual(resp["messages_remaining"], 0)
        self.assertTrue(resp["reply_text"])  # her line, not an error
        self.assertIs(resp["paywall_due"], True)

    def test_chat_wall_no_paywall_for_paid_user(self):
        self._mk("u_paid", bank=0, paid=True, token="tok_paid")
        body = api.ChatIn(message="hi", character_id="chloe")
        resp = api.chat(body, _Req("tok_paid"))
        self.assertEqual(resp["messages_remaining"], 0)
        self.assertIs(resp["paywall_due"], False)

    # -- duo wall ----------------------------------------------------------
    def _duo_402(self, user_id, token):
        self._mk(user_id, bank=0,
                 paid=(user_id == "u_duo_paid"), token=token)
        body = api.DuoChatIn(character_a="chloe", character_b="mara",
                             message="hi")
        with self.assertRaises(HTTPException) as ctx:
            api.chat_duo(body, _Req(token))
        return ctx.exception

    def test_duo_402_detail_structured_unpaid(self):
        exc = self._duo_402("tg_222", "tok_duo")
        self.assertEqual(exc.status_code, 402)
        self.assertIsInstance(exc.detail, dict)
        self.assertEqual(exc.detail["code"], "out_of_messages")
        self.assertIs(exc.detail["paywall_due"], True)

    def test_duo_402_detail_no_paywall_for_paid(self):
        exc = self._duo_402("u_duo_paid", "tok_duop")
        self.assertEqual(exc.status_code, 402)
        self.assertIs(exc.detail["paywall_due"], False)

    # -- media-pool entitlement refusals -----------------------------------
    def test_monthly_regular_cap_tease(self):
        self._mk("u_cap", bank=50, token="tok_cap")
        conn = db.connect(self.db_path)
        try:
            store.create_media_request(conn, "u_cap", "chloe",
                                       "regular", "video",
                                       "a cheerful hello video")
        finally:
            conn.close()
        body = api.MediaRequestIn(girl_id="chloe", kind="regular",
                                  media="video",
                                  prompt="another cheerful hello")
        with self.assertRaises(HTTPException) as ctx:
            api.create_media_request(body, _Req("tok_cap"))
        exc = ctx.exception
        self.assertEqual(exc.status_code, 402)
        self.assertEqual(exc.detail["code"], "monthly_regular_cap")
        self.assertEqual(exc.detail["character_id"], "chloe")
        self.assertIn("custom one", exc.detail["tease"])

    def test_empty_pool_tease(self):
        self._mk("u_pool", bank=50, token="tok_pool")
        conn = db.connect(self.db_path)
        try:
            store.get_request_pool(conn, "u_pool")
            conn.execute(
                "UPDATE request_pools SET included_remaining = 0, "
                "overage_regular_videos = 0, overage_give_videos = 0 "
                "WHERE user_id = 'u_pool'")
            conn.commit()
        finally:
            conn.close()
        body = api.MediaRequestIn(girl_id="chloe", kind="regular",
                                  media="video",
                                  prompt="a cheerful hello video")
        with self.assertRaises(HTTPException) as ctx:
            api.create_media_request(body, _Req("tok_pool"))
        exc = ctx.exception
        self.assertEqual(exc.status_code, 402)
        self.assertEqual(exc.detail["code"], "no_requests_left")
        self.assertTrue(exc.detail["tease"])

    # -- the tease line itself ---------------------------------------------
    def test_tease_voice_split(self):
        chloe = api._entitlement_tease({"id": "chloe"}, "no_requests_left")
        mara = api._entitlement_tease({"id": "mara"}, "no_requests_left")
        self.assertIn("babe", chloe)       # Chloe's exact voice
        self.assertNotIn("babe", mara)     # generic warm for the rest
        for line in (chloe, mara):
            self.assertIn("other side of my plan", line)


if __name__ == "__main__":
    unittest.main()
