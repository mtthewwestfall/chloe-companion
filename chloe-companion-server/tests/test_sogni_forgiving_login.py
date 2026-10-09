"""Forgiving Sogni credential intake (2026-10-09, owner-directed).

The owner pasted his Sogni login in natural words ("Username- X
password - Y") instead of the exact "sogni login u p" format; the
rigid parser missed it and the credential sat in his stored thread.
The intake now recognizes labeled pairs anywhere and bare pairs
inside an open Sogni topic — always owner-only, always scrubbed to
"[sogni login]" BEFORE the sign-in runs, with a retroactive sweep
for pastes an older parser missed. Ordinary talk about passwords
is never touched.

Run: python -m unittest tests.test_sogni_forgiving_login -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, db, store  # noqa: E402

OWNER = {"user_id": "owner_1", "tier": "basic"}
SESSION = {"token": "tok-aaa", "refreshToken": "ref-bbb"}
PAIR = ("skyler.r", "blu3-M00n!")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    assist.ensure_tables(conn)
    return conn


class ForgivingBase(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        self.conn = _conn()
        self.logins = []

        def fake_login(login):
            self.logins.append(login)
            # Scrub-before-signin ordering: by the time the sign-in
            # runs, the stored user turn must already be masked.
            row = self.conn.execute(
                "SELECT text FROM conversation WHERE role = 'user' "
                "ORDER BY id DESC LIMIT 1").fetchone()
            self.stored_at_signin = row["text"] if row else None
            return dict(SESSION)

        self.patcher = mock.patch.object(assist, "_sogni_plan_login",
                                         fake_login)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def say(self, text, cid="hannah", user=OWNER):
        # The chat path persists his raw turn before maybe_assist.
        store.log_turn(self.conn, user["user_id"], "user", text, cid)
        return assist.maybe_assist(self.conn, user, text, cid)

    def stored_texts(self):
        return [r["text"] for r in self.conn.execute(
            "SELECT text FROM conversation WHERE role = 'user' "
            "ORDER BY id").fetchall()]


class LabeledShapesTest(ForgivingBase):
    def test_dash_labeled_paste_signs_in_and_scrubs(self):
        res = self.say("Username- skyler.r password - blu3-M00n!")
        self.assertIsNotNone(res)
        self.assertIn("signed into your sogni", res["brief"])
        self.assertEqual(self.logins, [PAIR])
        self.assertEqual(self.stored_texts(), ["[sogni login]"])
        # Scrub happened BEFORE the sign-in call ran.
        self.assertEqual(self.stored_at_signin, "[sogni login]")
        self.assertIsNotNone(assist.stored_sogni_session(self.conn))

    def test_colon_labels_in_either_thread(self):
        res = self.say("user: skyler.r pass: blu3-M00n!", cid="mia")
        self.assertIn("signed into your sogni", res["brief"])
        self.assertEqual(self.logins, [PAIR])
        self.assertEqual(self.stored_texts(), ["[sogni login]"])
        res = self.say("user: skyler.r pass: blu3-M00n!", cid="jordan")
        self.assertIn("signed into your sogni", res["brief"])
        self.assertEqual(self.stored_texts(),
                         ["[sogni login]", "[sogni login]"])

    def test_is_phrasing(self):
        res = self.say("my username is skyler.r and my password "
                       "is blu3-M00n!")
        self.assertIn("signed into your sogni", res["brief"])
        self.assertEqual(self.logins, [PAIR])
        self.assertEqual(self.stored_texts(), ["[sogni login]"])

    def test_exact_format_still_works(self):
        res = self.say("sogni login skyler.r blu3-M00n!")
        self.assertIn("signed into your sogni", res["brief"])
        self.assertEqual(self.logins, [PAIR])


class BarePairTest(ForgivingBase):
    def test_slash_pair_inside_sogni_topic(self):
        assist._sogni_topic_touch(self.conn, "owner_1")
        res = self.say("skyler.r / blu3-M00n!")
        self.assertIn("signed into your sogni", res["brief"])
        self.assertEqual(self.logins, [PAIR])
        self.assertEqual(self.stored_texts(), ["[sogni login]"])

    def test_slash_pair_without_context_is_plain_chat(self):
        res = self.say("skyler.r / blu3-M00n!")
        if res is not None:
            self.assertNotIn("signed into your sogni", res["brief"])
        self.assertEqual(self.logins, [])
        self.assertEqual(self.stored_texts(), ["skyler.r / blu3-M00n!"])


class FalsePositiveTest(ForgivingBase):
    def test_password_talk_is_never_scrubbed(self):
        chats = [
            "i should change my password, it's getting old",
            "what's a good password manager?",
            "my password policy at work is brutal",
            "the lead store password reset thing is broken again",
            "forget password stuff, tell me about the warmup",
        ]
        for text in chats:
            res = self.say(text)
            self.assertEqual(self.logins, [])
            if res is not None:
                self.assertNotIn("signed into your sogni", res["brief"])
        self.assertEqual(self.stored_texts(), chats)

    def test_stranger_paste_gets_nothing(self):
        stranger = {"user_id": "someone_else", "tier": "vip"}
        res = self.say("Username- skyler.r password - blu3-M00n!",
                       user=stranger)
        self.assertIsNone(res)
        self.assertEqual(self.logins, [])


class RetroScrubTest(ForgivingBase):
    def _seed(self):
        store.log_turn(self.conn, "owner_1", "user",
                       "Username- skyler.r password - blu3-M00n!",
                       "hannah")
        store.log_turn(self.conn, "owner_1", "user",
                       "user: skyler.r pass: blu3-M00n!", "mia")
        store.log_turn(self.conn, "owner_1", "user",
                       "sogni login skyler.r blu3-M00n!", "jordan")
        store.log_turn(self.conn, "owner_1", "user",
                       "hey babe, how's the warmup?", "hannah")
        store.log_turn(self.conn, "owner_1", "user",
                       "Username- skyler.r password - blu3-M00n!",
                       "mara")  # not an assistant thread: untouched

    def test_sweep_counts_and_masks(self):
        self._seed()
        n = assist.scrub_sogni_credentials_from_history(self.conn)
        self.assertEqual(n, 3)
        texts = self.stored_texts()
        self.assertEqual(texts.count("[sogni login]"), 3)
        self.assertIn("hey babe, how's the warmup?", texts)
        # The mara-thread paste is outside the sweep's scope.
        self.assertIn("Username- skyler.r password - blu3-M00n!", texts)
        # Idempotent: a second sweep finds nothing left.
        self.assertEqual(
            assist.scrub_sogni_credentials_from_history(self.conn), 0)

    def test_successful_intake_sweeps_older_pastes(self):
        self._seed()
        res = self.say("sogni login skyler.r blu3-M00n!")
        self.assertIn("signed into your sogni", res["brief"])
        texts = self.stored_texts()
        # Seeded rows + the new paste: everything credential-shaped
        # in assistant threads is now the placeholder.
        self.assertEqual(texts.count("[sogni login]"), 4)
        self.assertIn("hey babe, how's the warmup?", texts)


if __name__ == "__main__":
    unittest.main()
