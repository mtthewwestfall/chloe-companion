"""Tests for the Gemini API key manager (app/gemini_keys.py).

Covers: table creation, masked admin listing, candidate ordering
(free-first, health-first, reserve pool), quota backoff windows,
payment → needs_attention, set_active re-enable clearing flags,
post_json failover (mocked HTTP), the fail-fast believed-healthy path,
attempt caps, optional hedging, and the background prober.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

# Make app/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config, gemini_keys


def _http_error(code):
    return urllib.error.HTTPError("https://example.test/gen", code,
                                  "err", None, None)


class GeminiKeysTestBase(unittest.TestCase):
    def setUp(self):
        # Fresh temp DB per test, wired through config.DB_PATH (the
        # module connects via app.db.connect()).
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self._orig_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._orig_legacy = config.GEMINI_API_KEY
        config.GEMINI_API_KEY = ""  # no legacy-env seeding in tests
        # Reset in-memory module state.
        gemini_keys._belief.clear()
        gemini_keys._belief_seeded = False
        gemini_keys._dispatch_times.clear()
        gemini_keys._recent_fail_ts.clear()
        gemini_keys._latencies.clear()
        gemini_keys._last_probe_mono.clear()
        gemini_keys._last_reserve_tap = None
        gemini_keys._probes_total = 0
        gemini_keys._probes_ok = 0
        self._env = {}
        for name in ("GEMINI_MAX_ATTEMPTS", "GEMINI_FAST_TIMEOUT_S",
                     "GEMINI_ATTEMPT_BUDGET_S", "GEMINI_HEDGE_AFTER_S",
                     "GEMINI_PER_KEY_RPM", "GEMINI_RESERVE_COUNT",
                     "GEMINI_PROBER"):
            self._env[name] = os.environ.pop(name, None)

    def tearDown(self):
        config.DB_PATH = self._orig_db_path
        config.GEMINI_API_KEY = self._orig_legacy
        for name, val in self._env.items():
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _add(self, key="test-key-1", label="test", is_paid=False):
        return gemini_keys.add_key(key, label, is_paid)

    def _sql(self, query, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(query, params)
            conn.commit()
        finally:
            conn.close()


class TestTableCreation(GeminiKeysTestBase):
    def test_tables_exist(self):
        conn = gemini_keys.db.connect()
        gemini_keys._ensure_table(conn)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("gemini_keys", names)
        self.assertIn("gemini_key_usage", names)
        conn.close()

    def test_columns(self):
        conn = gemini_keys.db.connect()
        gemini_keys._ensure_table(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(gemini_keys)")}
        for expected in ("key_hash", "key_value", "label", "is_paid",
                         "is_active", "consecutive_failures",
                         "quota_exhausted_until", "needs_attention",
                         "last_used_at"):
            self.assertIn(expected, cols)
        ucols = {r[1] for r in conn.execute(
            "PRAGMA table_info(gemini_key_usage)")}
        self.assertIn("latency_ms", ucols)  # efficiency measurement
        conn.close()


class TestAddKey(GeminiKeysTestBase):
    def test_add_and_list_masked(self):
        self._add("key-abc", "free-1", False)
        keys = gemini_keys.list_keys()
        self.assertEqual(len(keys), 1)
        self.assertNotIn("key-abc", json.dumps(keys))  # masked only

    def test_duplicate_rejected(self):
        self._add("dup-key", "one", False)
        with self.assertRaises(Exception):
            self._add("dup-key", "two", False)

    def test_delete(self):
        row = self._add("del-key", "t", False)
        self.assertTrue(gemini_keys.delete_key(row["id"]))
        self.assertEqual(len(gemini_keys.list_keys()), 0)


class TestCandidateOrdering(GeminiKeysTestBase):
    def test_free_before_paid(self):
        paid = self._add("paid-key", "paid", True)
        free = self._add("free-key", "free", False)
        self._add("free-key-2", "free2", False)
        self._add("free-key-3", "free3", False)  # highest id = reserve
        ids = [c[0] for c in gemini_keys.get_candidate_keys()]
        self.assertLess(ids.index(free["id"]), ids.index(paid["id"]))

    def test_healthier_free_first(self):
        k1 = self._add("k1", "a", False)
        k2 = self._add("k2", "b", False)
        gemini_keys.report_failure(k1["id"], "other")
        ids = [c[0] for c in gemini_keys.get_candidate_keys()]
        # k1 is in transient backoff; k2 must be eligible, k1 must not
        # lead (it is either absent or behind k2).
        self.assertIn(k2["id"], ids)
        if k1["id"] in ids:
            self.assertLess(ids.index(k2["id"]), ids.index(k1["id"]))

    def test_inactive_excluded(self):
        k = self._add("inactive-key", "x", False)
        gemini_keys.set_active(k["id"], False)
        ids = [c[0] for c in gemini_keys.get_candidate_keys()]
        self.assertNotIn(k["id"], ids)

    def test_reserve_only_when_active_exhausted(self):
        keys = [self._add(f"rk{i}", f"r{i}", False) for i in range(4)]
        # 4 keys -> 1 reserve (highest id). Exhaust the actives.
        for k in keys[:3]:
            gemini_keys.report_failure(k["id"], "quota")
        ids = [c[0] for c in gemini_keys.get_candidate_keys()]
        self.assertEqual(ids, [keys[3]["id"]])


class TestBackoff(GeminiKeysTestBase):
    def test_quota_backoff_sets_window(self):
        k = self._add("quota-key", "q", False)
        gemini_keys.report_failure(k["id"], "quota")
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertIsNotNone(row["quota_exhausted_until"])
        self.assertEqual(row["status"], "cooling_down")

    def test_recovery_after_window(self):
        k = self._add("retry-key", "r", False)
        gemini_keys.report_failure(k["id"], "quota")
        self._sql("UPDATE gemini_keys SET quota_exhausted_until="
                  "'2000-01-01 00:00:00' WHERE id=?", (k["id"],))
        reopened = gemini_keys.retry_exhausted()
        self.assertGreaterEqual(reopened, 1)
        ids = [c[0] for c in gemini_keys.get_candidate_keys()]
        self.assertIn(k["id"], ids)


class TestNeedsAttention(GeminiKeysTestBase):
    def test_payment_flags_attention(self):
        k = self._add("pay-key", "p", True)
        gemini_keys.report_failure(k["id"], "payment")
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertEqual(row["needs_attention"], 1)
        self.assertEqual(row["status"], "needs_attention")

    def test_attention_key_excluded(self):
        k = self._add("attn-key", "a", False)
        gemini_keys.report_failure(k["id"], "payment")
        ids = [c[0] for c in gemini_keys.get_candidate_keys()]
        self.assertNotIn(k["id"], ids)

    def test_disabled_shows_disabled_even_when_flagged(self):
        k = self._add("flag-off-key", "f", True)
        gemini_keys.report_failure(k["id"], "payment")
        gemini_keys.set_active(k["id"], False)
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertEqual(row["status"], "disabled")

    def test_set_active_clears_flags(self):
        k = self._add("clear-key", "c", False)
        gemini_keys.report_failure(k["id"], "payment")
        gemini_keys.report_failure(k["id"], "quota")
        gemini_keys.set_active(k["id"], True)  # re-enable
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertEqual(row["needs_attention"], 0)
        self.assertIsNone(row["quota_exhausted_until"])


class TestSuccessReset(GeminiKeysTestBase):
    def test_success_clears_failures_and_backoff(self):
        k = self._add("s-key", "s", False)
        gemini_keys.report_failure(k["id"], "other")
        gemini_keys.report_failure(k["id"], "quota")
        gemini_keys.report_success(k["id"])
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertIsNone(row["quota_exhausted_until"])
        self.assertEqual(row["status"], "healthy")


class TestPostJson(GeminiKeysTestBase):
    def _patch_http(self, behavior):
        """behavior: fn(key_value) -> dict (or raises). Returns call log."""
        calls = []

        def fake(url, payload, key_value, timeout):
            calls.append(key_value)
            return behavior(key_value)

        patcher = mock.patch.object(gemini_keys, "_http_post",
                                    side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_failover_on_429(self):
        self._add("fail-key", "f", False)
        self._add("good-key", "g", False)

        def behavior(key_value):
            if key_value == "fail-key":
                raise _http_error(429)
            return {"candidates": []}

        calls = self._patch_http(behavior)
        result = gemini_keys.post_json("https://example.com/gen", {"x": 1})
        self.assertIsNotNone(result)
        self.assertEqual(len(calls), 2)  # one wasted attempt, one success

    def test_all_fail_returns_none(self):
        self._add("bad1", "b1", False)
        self._add("bad2", "b2", False)

        def behavior(key_value):
            raise _http_error(500)

        self._patch_http(behavior)
        result = gemini_keys.post_json("https://example.com/gen", {"x": 1})
        self.assertIsNone(result)

    def test_attempt_cap(self):
        os.environ["GEMINI_MAX_ATTEMPTS"] = "3"
        for i in range(5):
            self._add(f"cap{i}", f"c{i}", False)

        def behavior(key_value):
            raise _http_error(500)

        calls = self._patch_http(behavior)
        result = gemini_keys.post_json("https://example.com/gen", {"x": 1})
        self.assertIsNone(result)
        self.assertEqual(len(calls), 3)  # capped, no 17-key death march

    def test_usage_records_latency(self):
        self._add("lat-key", "l", False)
        self._patch_http(lambda kv: {"ok": True})
        gemini_keys.post_json("https://example.com/gen", {"x": 1},
                              source="test-src")
        usage = gemini_keys.get_usage(5)
        mine = [u for u in usage if u["source"] == "test-src"]
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0]["success"])
        self.assertIsNotNone(mine[0]["latency_ms"])
        self.assertGreaterEqual(mine[0]["latency_ms"], 0)


class TestFastPath(GeminiKeysTestBase):
    def test_believed_healthy_tried_first(self):
        keys = [self._add(f"fk{i}", f"f{i}", False) for i in range(4)]
        good = keys[1]
        # Only fk1 is believed healthy.
        gemini_keys.report_success(good["id"])
        calls = []

        def fake(url, payload, key_value, timeout):
            calls.append(key_value)
            return {"ok": True}

        with mock.patch.object(gemini_keys, "_http_post",
                               side_effect=fake):
            result = gemini_keys.post_json("https://example.com/gen",
                                           {"x": 1})
        self.assertIsNotNone(result)
        self.assertEqual(calls, ["fk1"])  # straight to the healthy key

    def test_failed_fast_key_falls_through_to_sweep(self):
        keys = [self._add(f"sk{i}", f"s{i}", False) for i in range(4)]
        stale = keys[0]
        gemini_keys.report_success(stale["id"])  # believed healthy...
        calls = []

        def fake(url, payload, key_value, timeout):
            calls.append(key_value)
            if key_value == "sk0":
                raise _http_error(500)  # ...but actually dead now
            return {"ok": True}

        with mock.patch.object(gemini_keys, "_http_post",
                               side_effect=fake):
            result = gemini_keys.post_json("https://example.com/gen",
                                           {"x": 1})
        self.assertIsNotNone(result)
        self.assertEqual(calls[0], "sk0")
        self.assertGreaterEqual(len(calls), 2)  # failed over, no sleep

    def test_belief_seeded_from_persisted_health(self):
        k = self._add("seed-key", "seed", False)
        self._sql("UPDATE gemini_keys SET last_success_at=datetime('now'),"
                  " consecutive_failures=0 WHERE id=?", (k["id"],))
        gemini_keys._belief.clear()
        gemini_keys._belief_seeded = False
        fast = gemini_keys._fast_candidates()
        self.assertIn(k["id"], [c[0] for c in fast])


class TestHedging(GeminiKeysTestBase):
    def test_hedge_races_second_key(self):
        os.environ["GEMINI_HEDGE_AFTER_S"] = "0.2"
        k1 = self._add("hk1", "h1", False)
        k2 = self._add("hk2", "h2", False)
        self._add("hk3", "h3", False)  # highest id = reserve pool
        for k in (k1, k2):
            gemini_keys.report_success(k["id"])
        calls = []

        def fake(url, payload, key_value, timeout):
            calls.append(key_value)
            if key_value == "hk1":
                time.sleep(2.0)  # slow first key
            return {"ok": True}

        start = time.monotonic()
        with mock.patch.object(gemini_keys, "_http_post",
                               side_effect=fake):
            result = gemini_keys.post_json("https://example.com/gen",
                                           {"x": 1})
        elapsed = time.monotonic() - start
        self.assertIsNotNone(result)
        self.assertEqual(set(calls), {"hk1", "hk2"})
        self.assertLess(elapsed, 1.5)  # didn't wait out the slow key


class TestProber(GeminiKeysTestBase):
    def _patch_http(self, behavior):
        calls = []

        def fake(url, payload, key_value, timeout):
            calls.append(key_value)
            return behavior(key_value)

        patcher = mock.patch.object(gemini_keys, "_http_post",
                                    side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_prober_recovers_expired_key(self):
        k = self._add("probe-key", "p", False)
        gemini_keys.report_failure(k["id"], "quota")
        # Backoff now in the past: recovery should be discovered by the
        # prober, not by the next live request.
        self._sql("UPDATE gemini_keys SET quota_exhausted_until="
                  "'2000-01-01 00:00:00' WHERE id=?", (k["id"],))
        calls = self._patch_http(lambda kv: {"ok": True})
        sent = gemini_keys._probe_cycle()
        self.assertEqual(sent, 1)
        self.assertEqual(calls, ["probe-key"])
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertEqual(row["consecutive_failures"], 0)
        fast = [c[0] for c in gemini_keys._fast_candidates()]
        self.assertIn(k["id"], fast)

    def test_prober_skips_attention_and_cooling(self):
        attn = self._add("attn-key", "a", False)
        gemini_keys.report_failure(attn["id"], "payment")
        cool = self._add("cool-key", "c", False)
        gemini_keys.report_failure(cool["id"], "quota")  # future window
        calls = self._patch_http(lambda kv: {"ok": True})
        sent = gemini_keys._probe_cycle()
        self.assertEqual(sent, 0)
        self.assertEqual(calls, [])

    def test_prober_marks_dead_key_bad(self):
        k = self._add("dead-key", "d", False)

        def behavior(key_value):
            raise _http_error(500)

        self._patch_http(behavior)
        sent = gemini_keys._probe_cycle()
        self.assertEqual(sent, 1)
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertEqual(row["consecutive_failures"], 1)
        fast = [c[0] for c in gemini_keys._fast_candidates()]
        self.assertNotIn(k["id"], fast)

    def test_prober_quiet_when_real_traffic_fresh(self):
        k = self._add("busy-key", "b", False)
        gemini_keys.report_success(k["id"])
        # Belief is stale (>TTL) but a dispatch happened just now:
        # real traffic is the fresher probe, so the prober stays quiet.
        gemini_keys._belief[k["id"]]["at"] = time.time() - 1000
        with gemini_keys._lock:
            gemini_keys._record_dispatch_locked(k["id"])
        calls = self._patch_http(lambda kv: {"ok": True})
        gemini_keys._probe_cycle()
        self.assertEqual(calls, [])


class TestHealthInfo(GeminiKeysTestBase):
    def test_degradation_info_shape(self):
        self._add("info-key", "i", False)
        info = gemini_keys.degradation_info()
        self.assertIn("degraded", info)
        self.assertIn("effective_per_key_rpm", info)
        self.assertIn("believed_healthy_key_ids", info)
        self.assertIn("pool", info)
        self.assertIn("prober", info)
        self.assertIn("running", info["prober"])


class TestMasking(GeminiKeysTestBase):
    def test_mask_key(self):
        masked = gemini_keys.mask_key("ABCDEFGHIJKLMNOP")
        self.assertNotIn("ABCDEFGHIJKL", masked)
        self.assertIn("MNOP", masked)  # last 4 visible


if __name__ == "__main__":
    unittest.main()
