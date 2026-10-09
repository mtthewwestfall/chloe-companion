"""Owner GitHub-token intake tests (2026-10-08): the owner can hand
Hannah a GitHub token in chat; she stores it server-side for her repo
reviews, confirms with FIXED text (no model call), and the token never
survives into chat history, prompts, or long-term memory.

Run:  python -m unittest discover -s tests -v   (from the project root,
      with the venv's python)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, brain  # noqa: E402

DUMMY = "ghp_" + "Zz9" * 12              # 40 chars, realistic shape, fake
DUMMY_FG = "github_pat_" + "Ab1_" * 20   # fine-grained shape, fake
OWNER_USER = {"user_id": "owner_1", "tier": "basic"}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE conversation (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id TEXT, character_id TEXT, role TEXT, text TEXT,"
        " created_at TEXT)")
    assist.ensure_tables(conn)
    return conn


def _log_user_turn(conn, user_id: str, text: str, cid: str = "hannah"):
    conn.execute(
        "INSERT INTO conversation (user_id, character_id, role, text)"
        " VALUES (?, ?, 'user', ?)", (user_id, cid, text))
    conn.commit()


class TokenIntakeTest(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS",
                         "GITHUB_TOKEN")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        os.environ.pop("GITHUB_TOKEN", None)
        self.conn = _conn()

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def test_extract_shapes(self):
        self.assertEqual(assist.extract_github_token(f"key: {DUMMY}"), DUMMY)
        self.assertEqual(assist.extract_github_token(DUMMY_FG), DUMMY_FG)
        self.assertIsNone(assist.extract_github_token("ghp_short"))
        self.assertIsNone(assist.extract_github_token("hello there"))
        self.assertIsNone(assist.extract_github_token(""))

    def test_owner_intake_stores_and_sanitizes(self):
        msg = f"here's my github key {DUMMY} for the reviews"
        _log_user_turn(self.conn, "owner_1", msg)
        hit = assist.maybe_assist(self.conn, OWNER_USER, msg, "hannah")
        self.assertIsNotNone(hit)
        self.assertTrue(hit["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        self.assertNotIn(DUMMY, hit["brief"])
        # Stored raw server-side (that's the point) — read straight back.
        self.assertEqual(assist.stored_github_token(self.conn), DUMMY)
        # Persisted turn is scrubbed, exactly the placeholder remains.
        rows = self.conn.execute(
            "SELECT text FROM conversation WHERE user_id = 'owner_1'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(DUMMY, rows[0][0])
        self.assertIn("[github token]", rows[0][0])

    def test_non_owner_token_message_stores_nothing(self):
        msg = f"here's my github key {DUMMY}"
        _log_user_turn(self.conn, "customer_9", msg, "chloe")
        hit = assist.maybe_assist(
            self.conn, {"user_id": "customer_9", "tier": "basic"}, msg,
            "chloe")
        self.assertIsNone(hit)  # plain fall-through to normal chat
        self.assertEqual(assist.stored_github_token(self.conn), "")

    def test_rotation_replaces(self):
        assist.store_github_token(self.conn, DUMMY)
        assist.store_github_token(self.conn, DUMMY_FG)
        self.assertEqual(assist.stored_github_token(self.conn), DUMMY_FG)

    def test_revoke_deletes_and_confirms(self):
        assist.store_github_token(self.conn, DUMMY)
        hit = assist.maybe_assist(
            self.conn, OWNER_USER, "forget my token", "hannah")
        self.assertIsNotNone(hit)
        self.assertTrue(hit["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        self.assertEqual(assist.stored_github_token(self.conn), "")
        # Second revoke: nothing-to-wipe copy, still fixed.
        hit2 = assist.maybe_assist(
            self.conn, OWNER_USER, "revoke the token", "hannah")
        self.assertIsNotNone(hit2)
        self.assertIn("nothing to wipe", hit2["brief"])

    def test_revoke_with_paste_stores_nothing(self):
        assist.store_github_token(self.conn, DUMMY_FG)
        msg = f"forget this token {DUMMY}"
        _log_user_turn(self.conn, "owner_1", msg)
        hit = assist.maybe_assist(self.conn, OWNER_USER, msg, "hannah")
        self.assertTrue(hit["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        self.assertEqual(assist.stored_github_token(self.conn), "")
        row = self.conn.execute(
            "SELECT text FROM conversation WHERE user_id = 'owner_1'"
        ).fetchone()
        self.assertNotIn(DUMMY, row[0])

    def test_resolve_prefers_stored_then_env(self):
        os.environ["GITHUB_TOKEN"] = DUMMY_FG
        self.assertEqual(assist.resolve_github_token(self.conn), DUMMY_FG)
        assist.store_github_token(self.conn, DUMMY)
        self.assertEqual(assist.resolve_github_token(self.conn), DUMMY)

    def test_fixed_replies_have_no_token_fragments(self):
        for reply in (assist._TOKEN_SAVED_REPLY, assist._TOKEN_REVOKED_REPLY,
                      assist._TOKEN_NOTHING_REPLY):
            self.assertNotIn(DUMMY[:8], reply)
            self.assertNotIn("ghp_", reply)


class FixedReplyBrainTest(unittest.TestCase):
    def test_compose_reply_speaks_fixed_verbatim(self):
        calls = []
        orig = brain.gemini_generate
        brain.gemini_generate = lambda *a, **k: (
            calls.append(1), "MODEL SHOULD NEVER SAY THIS")[1]
        try:
            text = brain.compose_reply(
                f"key {DUMMY}",
                {"assist_result": "FIXED_REPLY:: saved, babe — done."},
                "hannah")
        finally:
            brain.gemini_generate = orig
        self.assertEqual(calls, [])  # no model call happened at all
        self.assertEqual(text, "saved, babe — done.")

    def test_normal_turns_still_use_model(self):
        orig = brain.gemini_generate
        brain.gemini_generate = lambda *a, **k: "model text"
        try:
            text = brain.compose_reply("hey", {"assist_result": "a brief"},
                                       "hannah")
        finally:
            brain.gemini_generate = orig
        self.assertEqual(text, "model text")


class MemoryScrubTest(unittest.TestCase):
    def test_extract_memories_masks_token(self):
        mems = brain.extract_memories(f"i have a github token {DUMMY} ok")
        blob = " ".join(t for _, t in mems)
        self.assertNotIn(DUMMY, blob)

    def test_no_credential_param_names(self):
        import inspect
        bad = ("password", "passwd", "oauth", "refresh_token",
               "access_token", "bearer")
        for name, fn in inspect.getmembers(assist, inspect.isfunction):
            for param in inspect.signature(fn).parameters:
                pl = param.lower()
                self.assertFalse(
                    any(b in pl for b in bad),
                    f"assist.{name} takes credential-shaped param '{param}'")


if __name__ == "__main__":
    unittest.main()
