"""Tests for the Gemini API key manager (app/gemini_keys.py).

Covers: table creation, legacy env seeding, candidate ordering
(free-first, health-first), quota backoff windows, payment →
needs_attention, set_active re-enable clearing flags, and post_json
failover (with mocked urllib).
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

# Make app/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import gemini_keys


class GeminiKeysTestBase(unittest.TestCase):
    def setUp(self):
        # Fresh temp DB per test
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        # Patch the module's DB path if it uses one
        self._orig = getattr(gemini_keys, "DB_PATH", None)
        if hasattr(gemini_keys, "DB_PATH"):
            gemini_keys.DB_PATH = self.db_path
        # Ensure table exists
        gemini_keys._ensure_table(self.db_path)

    def tearDown(self):
        if hasattr(gemini_keys, "DB_PATH") and self._orig is not None:
            gemini_keys.DB_PATH = self._orig
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _add(self, key="test-key-1", label="test", is_paid=False):
        return gemini_keys.add_key(key, label, is_paid)


class TestTableCreation(GeminiKeysTestBase):
    def test_table_exists(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gemini_keys'"
        )
        self.assertIsNotNone(cur.fetchone())
        conn.close()

    def test_columns(self):
        conn = sqlite3.connect(self.db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(gemini_keys)")}
        for expected in ("key_hash", "key_value", "label", "is_paid",
                         "is_active", "consecutive_failures",
                         "quota_exhausted_until", "needs_attention"):
            self.assertIn(expected, cols)
        conn.close()


class TestAddKey(GeminiKeysTestBase):
    def test_add_and_list(self):
        self._add("key-abc", "free-1", False)
        keys = gemini_keys.list_keys()
        self.assertEqual(len(keys), 1)
        # Key value must be masked, never full
        self.assertNotIn("key-abc", json.dumps(keys))

    def test_duplicate_rejected(self):
        self._add("dup-key", "one", False)
        with self.assertRaises(Exception):
            self._add("dup-key", "two", False)

    def test_delete(self):
        row = self._add("del-key", "t", False)
        gemini_keys.delete_key(row["id"])
        self.assertEqual(len(gemini_keys.list_keys()), 0)


class TestCandidateOrdering(GeminiKeysTestBase):
    def test_free_before_paid(self):
        paid = self._add("paid-key", "paid", True)
        free = self._add("free-key", "free", False)
        cands = gemini_keys.get_candidate_keys()
        ids = [c["id"] for c in cands]
        self.assertLess(ids.index(free["id"]), ids.index(paid["id"]))

    def test_healthier_free_first(self):
        k1 = self._add("k1", "a", False)
        k2 = self._add("k2", "b", False)
        # k1 gets failures, k2 stays clean
        gemini_keys.report_failure(k1["id"], "500")
        cands = gemini_keys.get_candidate_keys()
        ids = [c["id"] for c in cands]
        self.assertLess(ids.index(k2["id"]), ids.index(k1["id"]))

    def test_inactive_excluded(self):
        k = self._add("inactive-key", "x", False)
        gemini_keys.set_active(k["id"], False)
        cands = gemini_keys.get_candidate_keys()
        self.assertNotIn(k["id"], [c["id"] for c in cands])

    def test_paid_only_when_free_exhausted(self):
        free = self._add("free-ex", "f", False)
        paid = self._add("paid-ex", "p", True)
        # Exhaust the free key with quota errors
        for _ in range(5):
            gemini_keys.report_failure(free["id"], "429")
        cands = gemini_keys.get_candidate_keys()
        ids = [c["id"] for c in cands]
        # Paid should now come first (free is in backoff)
        self.assertLess(ids.index(paid["id"]), ids.index(free["id"]))


class TestBackoff(GeminiKeysTestBase):
    def test_quota_backoff_sets_window(self):
        k = self._add("quota-key", "q", False)
        gemini_keys.report_failure(k["id"], "429")
        keys = {x["id"]: x for x in gemini_keys.list_keys()}
        self.assertIsNotNone(keys[k["id"]].get("quota_exhausted_until"))

    def test_backoff_escalates(self):
        k = self._add("esc-key", "e", False)
        gemini_keys.report_failure(k["id"], "429")
        first = gemini_keys.list_keys()[0]["quota_exhausted_until"]
        gemini_keys.report_failure(k["id"], "429")
        second = gemini_keys.list_keys()[0]["quota_exhausted_until"]
        # Second backoff should be >= first (escalating ladder)
        self.assertGreaterEqual(second, first)

    def test_retry_exhausted_clears(self):
        k = self._add("retry-key", "r", False)
        gemini_keys.report_failure(k["id"], "429")
        # Force the window to the past
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE gemini_keys SET quota_exhausted_until='2000-01-01 00:00:00' WHERE id=?",
            (k["id"],),
        )
        conn.commit()
        conn.close()
        gemini_keys.retry_exhausted()
        cands = gemini_keys.get_candidate_keys()
        self.assertIn(k["id"], [c["id"] for c in cands])


class TestNeedsAttention(GeminiKeysTestBase):
    def test_payment_flags_attention(self):
        k = self._add("pay-key", "p", True)
        gemini_keys.report_failure(k["id"], "402")
        keys = {x["id"]: x for x in gemini_keys.list_keys()}
        self.assertEqual(keys[k["id"]].get("needs_attention"), 1)

    def test_attention_key_excluded(self):
        k = self._add("attn-key", "a", False)
        gemini_keys.report_failure(k["id"], "402")
        cands = gemini_keys.get_candidate_keys()
        self.assertNotIn(k["id"], [c["id"] for c in cands])

    def test_set_active_clears_flags(self):
        k = self._add("clear-key", "c", False)
        gemini_keys.report_failure(k["id"], "402")
        gemini_keys.report_failure(k["id"], "429")
        gemini_keys.set_active(k["id"], True)  # re-enable
        keys = {x["id"]: x for x in gemini_keys.list_keys()}
        row = keys[k["id"]]
        self.assertEqual(row.get("needs_attention"), 0)
        self.assertIsNone(row.get("quota_exhausted_until"))
        self.assertEqual(row.get("consecutive_failures"), 0)


class TestSuccessReset(GeminiKeysTestBase):
    def test_success_clears_failures(self):
        k = self._add("s-key", "s", False)
        gemini_keys.report_failure(k["id"], "500")
        gemini_keys.report_failure(k["id"], "500")
        gemini_keys.report_success(k["id"])
        keys = {x["id"]: x for x in gemini_keys.list_keys()}
        self.assertEqual(keys[k["id"]].get("consecutive_failures"), 0)


class TestPostJsonFailover(GeminiKeysTestBase):
    def _fake_response(self, payload):
        m = mock.MagicMock()
        m.read.return_value = json.dumps(payload).encode()
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        return m

    def test_failover_on_429(self):
        self._add("fail-key", "f", False)
        self._add("good-key", "g", False)
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            # First key 429s, second succeeds
            if len(calls) == 1:
                raise FakeHTTPError(429)
            return self._fake_response({"candidates": []})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            # Patch post_json to use our DB
            with mock.patch.object(gemini_keys, "DB_PATH", self.db_path):
                result = gemini_keys.post_json(
                    "https://example.com/gen", {"x": 1})
        self.assertEqual(len(calls), 2)
        self.assertIsNotNone(result)

    def test_all_fail_returns_none(self):
        self._add("bad1", "b1", False)
        self._add("bad2", "b2", False)

        def fake_urlopen(req, timeout=None):
            raise FakeHTTPError(500)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch.object(gemini_keys, "DB_PATH", self.db_path):
                result = gemini_keys.post_json(
                    "https://example.com/gen", {"x": 1})
        self.assertIsNone(result)


class FakeHTTPError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


class TestMasking(GeminiKeysTestBase):
    def test_mask_key(self):
        masked = gemini_keys.mask_key("ABCDEFGHIJKLMNOP")
        self.assertNotIn("ABCDEFGHIJKL", masked)
        self.assertIn("MNOP", masked)  # last 4 visible


if __name__ == "__main__":
    unittest.main()
