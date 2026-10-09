"""Chat owner-bypass tests (2026-10-09).

Bug: the owner (ASSIST_OWNER_IDS) hit the customer empty-bank paywall in
chat — his message/voice banks drained like any customer's and, once at
zero, characters served him the "$0.99 trial / $9.99 plan" upsell. The
media-request flow already had an owner bypass; the chat flow did not.

These tests pin the fix:
  * owner + any character -> real reply, NO bank consumption, NO upsell
    (even with an empty bank), while messages_used_total still advances;
  * non-owner with an empty bank -> the upsell still fires, nothing
    consumed, nothing goes negative;
  * non-owner with a balance -> consumes exactly as before (incl. the
    voice-note bank).

The brain/assist network paths are stubbed — no Gemini pool is touched.

Run:  python -m unittest discover -s tests -v   (from the project root)
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

OWNER = "owner_1"
UPSELL_MARK = "$0.99"


def _char(cid, name):
    return {"id": cid, "name": name, "avatar_url": "", "persona": "",
            "companion_voice": "liora", "blurb": "", "door_title": "",
            "sort_order": 1}


class _Req:
    def __init__(self, token):
        self.headers = {"authorization": f"Bearer {token}"} if token else {}


class ChatOwnerBypassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path

        self._old_owners = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = OWNER

        # Roster: seed the character cache directly — hermetic (no Keyhole
        # fetch, no local-characters table). Chloe is builtin fallback.
        self._real_cache = characters._cache
        characters._cache = {"ts": time.time(), "chars": [], "local": [
            _char("hannah", "Hannah"), _char("mara", "Mara")]}

        # No pool spend: stub the brain's LLM paths and assist tools.
        self._real_compose = brain.compose_reply
        self._real_context = brain.build_brain_context
        self._real_learn = brain.learn_from_turn
        self._real_assist = assist.maybe_assist
        brain.compose_reply = lambda message, context, cid: "a real reply"
        brain.build_brain_context = lambda conn, uid, user, msg, char: {}
        brain.learn_from_turn = lambda conn, uid, msg, cid: None
        assist.maybe_assist = lambda conn, user, msg, cid: None

        self.owner_tok = self._mk_user(OWNER, msgs=5, voice=7)
        self.cust_tok = self._mk_user("cust_1", msgs=5, voice=7)
        self.broke_tok = self._mk_user("cust_broke", msgs=0, voice=7)

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
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _mk_user(self, uid, msgs, voice):
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, uid)
            conn.execute(
                "UPDATE users SET messages_remaining = ?, "
                "voice_notes_remaining = ? WHERE user_id = ?",
                (msgs, voice, uid))
            conn.commit()
            token = f"tok-{uid}"
            store.create_session(conn, uid, token)
            return token
        finally:
            conn.close()

    def _banks(self, uid):
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT messages_remaining, voice_notes_remaining, "
                "messages_used_total FROM users WHERE user_id = ?",
                (uid,)).fetchone()
            return dict(row)
        finally:
            conn.close()

    def _chat(self, token, character_id, message="hi there"):
        body = api.ChatIn(message=message, character_id=character_id)
        return api.chat(body, _Req(token))

    # -- owner: never metered, never upsold ------------------------------
    def test_owner_hannah_no_consumption_no_upsell(self):
        resp = self._chat(self.owner_tok, "hannah")
        self.assertEqual(resp["reply_text"], "a real reply")
        self.assertNotIn(UPSELL_MARK, resp["reply_text"])
        banks = self._banks(OWNER)
        self.assertEqual(banks["messages_remaining"], 5)   # bank untouched
        self.assertEqual(banks["voice_notes_remaining"], 7)
        self.assertEqual(banks["messages_used_total"], 1)  # cadence only

    def test_owner_mara_no_consumption_no_upsell(self):
        resp = self._chat(self.owner_tok, "mara")
        self.assertEqual(resp["reply_text"], "a real reply")
        self.assertNotIn(UPSELL_MARK, resp["reply_text"])
        banks = self._banks(OWNER)
        self.assertEqual(banks["messages_remaining"], 5)
        self.assertEqual(banks["voice_notes_remaining"], 7)
        self.assertEqual(banks["messages_used_total"], 1)

    def test_owner_empty_bank_still_gets_real_reply(self):
        conn = db.connect(self.db_path)
        try:
            conn.execute("UPDATE users SET messages_remaining = 0, "
                         "voice_notes_remaining = 0 WHERE user_id = ?",
                         (OWNER,))
            conn.commit()
        finally:
            conn.close()
        resp = self._chat(self.owner_tok, "hannah")
        self.assertEqual(resp["reply_text"], "a real reply")
        self.assertNotIn(UPSELL_MARK, resp["reply_text"])
        banks = self._banks(OWNER)
        self.assertEqual(banks["messages_remaining"], 0)  # never negative
        self.assertEqual(banks["messages_used_total"], 1)

    # -- customers: exactly as before -------------------------------------
    def test_customer_consumes_message_and_voice(self):
        resp = self._chat(self.cust_tok, "mara")
        self.assertEqual(resp["reply_text"], "a real reply")
        banks = self._banks("cust_1")
        self.assertEqual(banks["messages_remaining"], 4)
        self.assertEqual(banks["voice_notes_remaining"], 6)
        self.assertEqual(banks["messages_used_total"], 1)

    def test_broke_customer_hannah_gets_upsell(self):
        resp = self._chat(self.broke_tok, "hannah")
        self.assertIn("out of messages", resp["reply_text"])
        self.assertIn(UPSELL_MARK, resp["reply_text"])
        banks = self._banks("cust_broke")
        self.assertEqual(banks["messages_remaining"], 0)
        self.assertEqual(banks["messages_used_total"], 0)

    def test_broke_customer_chloe_gets_upsell(self):
        resp = self._chat(self.broke_tok, "chloe")
        self.assertIn(UPSELL_MARK, resp["reply_text"])
        self.assertEqual(resp["messages_remaining"], 0)
        banks = self._banks("cust_broke")
        self.assertEqual(banks["messages_remaining"], 0)
        self.assertEqual(banks["messages_used_total"], 0)


if __name__ == "__main__":
    unittest.main()
