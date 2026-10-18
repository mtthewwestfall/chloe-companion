"""Roleplay-lock assist gate (2026-10-18).

Bug: Brooklyn is roleplay-locked, but the capability lane
(assist.maybe_assist) still claimed her in-scene turns. The intent
detector treats ANY message containing "?" as a web-search ask, so his
in-scene line "With her face buried in your pussy?" ran a grounded
search and the reply came back in helpdesk voice: "I can't do that,
babe. I tried to look into it for you, but that's just not something I
can help with." (Production usage rows 2026-10-18 04:27:13-21 UTC:
whispr-search x7, then whispr-chat, then whispr-voice.)

Fix: while a girl's roleplay_locked flag is on, maybe_assist returns
None for her turns — the whole capability/intent lane is skipped and
her brain answers in-scene. The credential intakes (GitHub token,
Sogni login) still run first: a pasted secret is scrubbed even
mid-scene. The flag is read fresh per turn, so the release word
(which build_brain_context flips before maybe_assist runs) restores
the lane on the very same turn.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, db, store  # noqa: E402

OWNER = "owner_1"
OWNER_USER = {"user_id": OWNER, "tier": "basic"}
DUMMY_TOKEN = "".join(map(chr, [103, 104, 112, 95])) + "Zz9" * 12

BROOKLYN = {
    "id": "brooklyn",
    "name": "Brooklyn",
    "persona": "Brooklyn is his new neighbor.",
    "owner_user_id": OWNER,
}

IN_SCENE_QUESTION = "With her face buried in your pussy?"


def _set_flag(conn, cid, value):
    conn.execute(
        "UPDATE local_characters SET roleplay_locked = ? WHERE id = ?",
        (value, cid),
    )
    conn.commit()


class AssistLockGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)
        assist.ensure_tables(self.conn)
        store.upsert_local_character(self.conn, dict(BROOKLYN))
        self.old_env = {k: os.environ.get(k)
                        for k in ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = OWNER
        # No pool spend: web_search is a recorder, never the network.
        self.search_calls: list[str] = []
        self._real_search = assist.web_search

        def fake_search(query):
            self.search_calls.append(query)
            return {"answer": "stub search answer", "sources": []}

        assist.web_search = fake_search

    def tearDown(self):
        assist.web_search = self._real_search
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()
        self.tmp.cleanup()

    def test_lock_flag_helper(self):
        self.assertFalse(assist._roleplay_locked(self.conn, "brooklyn"))
        _set_flag(self.conn, "brooklyn", 1)
        self.assertTrue(assist._roleplay_locked(self.conn, "brooklyn"))
        _set_flag(self.conn, "brooklyn", 0)
        self.assertFalse(assist._roleplay_locked(self.conn, "brooklyn"))
        # No local row -> never locked; empty id -> never locked.
        self.assertFalse(assist._roleplay_locked(self.conn, "chloe"))
        self.assertFalse(assist._roleplay_locked(self.conn, ""))

    def test_locked_in_scene_question_never_reaches_search(self):
        """The defect itself: locked + in-scene "?" line -> no assist
        claim at all, and the search lane is never touched."""
        _set_flag(self.conn, "brooklyn", 1)
        hit = assist.maybe_assist(self.conn, OWNER_USER,
                                  IN_SCENE_QUESTION, "brooklyn")
        self.assertIsNone(hit)
        self.assertEqual(self.search_calls, [])

    def test_unlocked_same_question_does_search(self):
        """Control: the identical line with the lock OFF flows to the
        search lane exactly as before — the gate is the lock, not the
        text."""
        hit = assist.maybe_assist(self.conn, OWNER_USER,
                                  IN_SCENE_QUESTION, "brooklyn")
        self.assertIsNotNone(hit)
        self.assertIn("brief", hit)
        self.assertEqual(len(self.search_calls), 1)

    def test_locked_reminder_ask_is_not_claimed(self):
        _set_flag(self.conn, "brooklyn", 1)
        hit = assist.maybe_assist(
            self.conn, OWNER_USER,
            "remind me to water the plants tomorrow at 9am", "brooklyn")
        self.assertIsNone(hit)
        n = self.conn.execute(
            "SELECT COUNT(*) AS c FROM reminders").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_locked_credential_intake_still_claims(self):
        """Security carve-out: a pasted GitHub token is still claimed
        and scrubbed by the intake even while she is locked."""
        _set_flag(self.conn, "brooklyn", 1)
        hit = assist.maybe_assist(self.conn, OWNER_USER,
                                  DUMMY_TOKEN, "brooklyn")
        self.assertIsNotNone(hit)


if __name__ == "__main__":
    unittest.main()
