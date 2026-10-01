"""Custom-companion submission tests: validation, AI-review verdict mapping,
approval-only flow, verified-fictional flag, and the explicit-lane rule.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import db, store, submit  # noqa: E402

TINY_PNG = ("data:image/png;base64," + base64.b64encode(b"\x89PNG" + b"x" * 2000).decode())
TINY_JPG = ("data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8" + b"y" * 2000).decode())


def _vision_response(per_image):
    parts_text = json.dumps(per_image)
    return {"candidates": [{"content": {"parts": [{"text": parts_text}]}}]}


class ValidationTest(unittest.TestCase):
    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            submit._clean_images([])

    def test_rejects_too_many(self):
        with self.assertRaises(ValueError):
            submit._clean_images([TINY_PNG] * 4)

    def test_rejects_non_data_uri(self):
        with self.assertRaises(ValueError):
            submit._clean_images(["https://example.com/pic.jpg"])

    def test_rejects_bad_base64(self):
        with self.assertRaises(ValueError):
            submit._clean_images(["data:image/png;base64,!!!notbase64!!!"])

    def test_accepts_up_to_three(self):
        out = submit._clean_images([TINY_PNG, TINY_JPG])
        self.assertEqual(len(out), 2)

    def test_policy_ack_required(self):
        conn = sqlite3.connect(":memory:")
        with self.assertRaises(ValueError):
            submit.create_submission(conn, "u1", {
                "name": "Alex", "images": [TINY_PNG], "policy_ack": False})

    def test_name_required(self):
        conn = sqlite3.connect(":memory:")
        with self.assertRaises(ValueError):
            submit.create_submission(conn, "u1", {
                "name": "  ", "images": [TINY_PNG], "policy_ack": True})


class ReviewVerdictTest(unittest.TestCase):
    def _review(self, per_image):
        with mock.patch.object(submit, "_gemini_vision_post",
                               return_value=_vision_response(per_image)):
            return submit.review_images([TINY_PNG] * len(per_image))

    def test_pass(self):
        r = self._review([{"nudity": "no", "all_adult": "yes", "note": "ok"}])
        self.assertEqual(r["verdict"], "pass")

    def test_nudity_rejects(self):
        r = self._review([{"nudity": "yes", "all_adult": "yes", "note": ""}])
        self.assertEqual(r["verdict"], "reject")
        self.assertIn("nudity", r["notes"])

    def test_not_adult_rejects(self):
        r = self._review([{"nudity": "no", "all_adult": "no", "note": ""}])
        self.assertEqual(r["verdict"], "reject")

    def test_uncertain_age_flags(self):
        r = self._review([{"nudity": "no", "all_adult": "uncertain", "note": ""}])
        self.assertEqual(r["verdict"], "flag")

    def test_api_failure_flags_never_passes(self):
        with mock.patch.object(submit, "_gemini_vision_post", return_value=None):
            r = submit.review_images([TINY_PNG])
        self.assertEqual(r["verdict"], "flag")

    def test_garbage_response_flags(self):
        with mock.patch.object(
                submit, "_gemini_vision_post",
                return_value={"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}):
            r = submit.review_images([TINY_PNG])
        self.assertEqual(r["verdict"], "flag")


class SubmissionFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        db.init_db(self.tmp.name)
        self.conn = db.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def _submit(self, verdict):
        per = {"pass": [{"nudity": "no", "all_adult": "yes", "note": ""}],
               "flag": [{"nudity": "no", "all_adult": "uncertain", "note": ""}],
               "reject": [{"nudity": "yes", "all_adult": "yes", "note": ""}]}[verdict]
        with mock.patch.object(submit, "_gemini_vision_post",
                               return_value=_vision_response(per)):
            return submit.create_submission(self.conn, "user1", {
                "name": "Derek", "persona": "A charming man.",
                "blurb": "Hey.", "images": [TINY_JPG], "policy_ack": True})

    def test_pass_becomes_pending(self):
        sub = self._submit("pass")
        self.assertEqual(sub["status"], "pending")

    def test_flag_becomes_flagged(self):
        sub = self._submit("flag")
        self.assertEqual(sub["status"], "flagged")

    def test_reject_becomes_rejected(self):
        sub = self._submit("reject")
        self.assertEqual(sub["status"], "rejected")

    def test_rejected_never_visible_as_live(self):
        sub = self._submit("reject")
        girls = store.list_local_characters(self.conn)
        self.assertNotIn("derek", {g["id"] for g in girls})

    def test_approve_creates_girl(self):
        sub = self._submit("pass")
        result = submit.approve_submission(self.conn, sub["id"], True)
        girl = result["girl"]
        self.assertEqual(girl["name"], "Derek")
        self.assertEqual(girl["verified_fictional"], 1)
        self.assertTrue(girl["avatar_url"].startswith("data:image/"))
        row = self.conn.execute("SELECT status FROM submissions WHERE id = ?",
                                (sub["id"],)).fetchone()
        self.assertEqual(row["status"], "approved")

    def test_approve_not_fictional(self):
        sub = self._submit("pass")
        result = submit.approve_submission(self.conn, sub["id"], False)
        self.assertEqual(result["girl"]["verified_fictional"], 0)
        self.assertFalse(result["verified_fictional"])

    def test_reject_submission(self):
        sub = self._submit("flag")
        out = submit.reject_submission(self.conn, sub["id"], "not a fit")
        self.assertEqual(out["status"], "rejected")

    def test_cannot_approve_rejected(self):
        sub = self._submit("reject")
        with self.assertRaises(ValueError):
            submit.approve_submission(self.conn, sub["id"], True)

    def test_male_name_works(self):
        # women can create men too — nothing gendered in the pipeline
        with mock.patch.object(
                submit, "_gemini_vision_post",
                return_value=_vision_response(
                    [{"nudity": "no", "all_adult": "yes", "note": ""}])):
            sub = submit.create_submission(self.conn, "user2", {
                "name": "Marcus", "persona": "A rugged firefighter.",
                "images": [TINY_JPG], "policy_ack": True})
        result = submit.approve_submission(self.conn, sub["id"], True)
        self.assertEqual(result["girl"]["name"], "Marcus")


class ExplicitLaneTest(unittest.TestCase):
    def test_explicit_blocked_by_default(self):
        self.assertEqual(store.moderate_prompt("a nude portrait"), "explicit_blocked")

    def test_explicit_allowed_when_verified(self):
        self.assertIsNone(store.moderate_prompt("a nude portrait", allow_explicit=True))

    def test_hard_block_never_opens(self):
        self.assertIsNotNone(store.moderate_prompt("nude teen", allow_explicit=True))
        self.assertIsNotNone(store.moderate_prompt("forced nude", allow_explicit=True))

    def test_clean_prompt_passes(self):
        self.assertIsNone(store.moderate_prompt("her smiling in a red dress"))

    def test_prompt_has_explicit(self):
        self.assertTrue(store.prompt_has_explicit("topless photo"))
        self.assertFalse(store.prompt_has_explicit("a nice portrait"))


class MigrationTest(unittest.TestCase):
    def test_verified_fictional_column_exists(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            db.init_db(tmp.name)
            conn = db.connect(tmp.name)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(local_characters)").fetchall()}
            self.assertIn("verified_fictional", cols)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("submissions", tables)
            conn.close()
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
