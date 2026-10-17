"""Tests for the Gemini API key manager (app/gemini_keys.py).

Covers: table creation, masked admin listing, candidate ordering
(free-first, health-first, reserve pool), quota backoff windows,
payment → needs_attention, set_active re-enable clearing flags,
post_json failover (mocked HTTP), the fail-fast believed-healthy path,
attempt caps, optional hedging, and the background prober.

Efficiency pass (2026-10-15): error-class separation (transient
failures pause briefly with no health damage; request-class failures
blame no key and stop the walk), daily-usage pacing in candidate
ordering, the unconditional fail-fast (no all-keys last-resort
sweep), the leaner prober (per-cycle cap, near-expiry probing only),
and error_class recorded on usage rows.

Model fallback (2026-10-17): per-model overload tracking ('model'
error class, no key blame), GEMINI_MODEL_CHAIN-ordered failover
inside post_json, idle-cycle prober coverage for every chain model,
and per-model status in degradation_info()["models"].
"""
import io
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
        gemini_keys._transient_streaks.clear()
        # Reset per-model fallback state (2026-10-17).
        gemini_keys._model_hot_until.clear()
        gemini_keys._model_overload_events.clear()
        gemini_keys._model_probe_at.clear()
        gemini_keys._model_belief.clear()
        self._env = {}
        for name in ("GEMINI_MAX_ATTEMPTS", "GEMINI_FAST_TIMEOUT_S",
                     "GEMINI_ATTEMPT_BUDGET_S", "GEMINI_HEDGE_AFTER_S",
                     "GEMINI_PER_KEY_RPM", "GEMINI_RESERVE_COUNT",
                     "GEMINI_PROBE_INTERVAL_S", "GEMINI_PROBER",
                     "GEMINI_MODEL_CHAIN", "GEMINI_MODEL_COOLDOWN_S"):
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


class TestReserveFallback(GeminiKeysTestBase):
    """Phase-3 fallback reachability (2026-10-16 fix).

    The fallback pass (held-back reserve keys, then any untried paid
    key) shares the global attempt cap; when the active pool has at
    least as many eligible keys as the cap and the request is
    quota-walled on all of them, the cap used to be spent before the
    fallback could fire — the fallback was unreachable in exactly
    the failure it exists for. On a pure quota wall the pass now
    earns a bounded extra allowance. A transient wall earns none:
    the cap test (test_attempt_cap) must keep holding exactly.
    """

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

    def test_quota_wall_reaches_reserve(self):
        # 12 free keys -> 4 reserve (highest ids), 8 active. The cap
        # is 8: the active sweep alone spends it all on 429s.
        for i in range(12):
            self._add(f"qw{i}", f"q{i}", False)
        active_vals = {f"qw{i}" for i in range(8)}

        def behavior(key_value):
            if key_value in active_vals:
                raise _http_error(429)
            return {"ok": True}

        calls = self._patch_http(behavior)
        result = gemini_keys.post_json("https://example.com/gen", {"x": 1})
        self.assertIsNotNone(result)
        self.assertEqual(len([c for c in calls if c in active_vals]), 8)
        self.assertGreater(len(calls), 8)  # fallback fired past the cap

    def test_quota_wall_reaches_paid_when_reserve_walled(self):
        # Every free key 429s (actives and reserves alike); the paid
        # key must still be attempted by the fallback pass.
        for i in range(12):
            self._add(f"pw{i}", f"p{i}", False)
        self._add("pw-paid", "paid", True)

        def behavior(key_value):
            if key_value == "pw-paid":
                return {"ok": True}
            raise _http_error(429)

        calls = self._patch_http(behavior)
        result = gemini_keys.post_json("https://example.com/gen", {"x": 1})
        self.assertIsNotNone(result)
        self.assertIn("pw-paid", calls)

    def test_transient_wall_earns_no_extension(self):
        os.environ["GEMINI_MAX_ATTEMPTS"] = "3"
        for i in range(6):
            self._add(f"tw{i}", f"t{i}", False)

        def behavior(key_value):
            raise _http_error(500)

        calls = self._patch_http(behavior)
        result = gemini_keys.post_json("https://example.com/gen", {"x": 1})
        self.assertIsNone(result)
        self.assertEqual(len(calls), 3)  # cap holds exactly


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
        # Error-class separation: a 500 is transient — the key is
        # briefly paused (out of the fast path) but takes NO health
        # damage: no consecutive-failure streak, never degraded.
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertIsNotNone(row["quota_exhausted_until"])
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
        # Model fallback (2026-10-17): per-model status is exposed.
        self.assertIn("models", info)
        models = {m["model"]: m for m in info["models"]}
        self.assertIn("gemini-3.1-flash-lite", models)
        self.assertIn("gemini-2.5-flash-lite", models)
        primary = models["gemini-3.1-flash-lite"]
        self.assertTrue(primary["primary"])
        self.assertTrue(primary["serving"])
        self.assertFalse(primary["hot"])


class TestMasking(GeminiKeysTestBase):
    def test_mask_key(self):
        masked = gemini_keys.mask_key("ABCDEFGHIJKLMNOP")
        self.assertNotIn("ABCDEFGHIJKL", masked)
        self.assertIn("MNOP", masked)  # last 4 visible


class TestErrorClasses(GeminiKeysTestBase):
    def test_classify_mapping(self):
        c = gemini_keys._classify_http_error
        self.assertEqual(c(429), "quota")
        self.assertEqual(c(401), "payment")
        self.assertEqual(c(402), "payment")
        self.assertEqual(c(403), "payment")
        self.assertEqual(c(500), "transient")
        self.assertEqual(c(502), "transient")
        self.assertEqual(c(503), "transient")
        self.assertEqual(c(400), "request")
        self.assertEqual(c(404), "request")

    def test_transient_streak_never_degrades(self):
        k = self._add("flaky-key", "f", False)
        for _ in range(6):
            gemini_keys.report_failure(k["id"], "transient")
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertEqual(row["needs_attention"], 0)
        self.assertNotEqual(row["status"], "degraded")
        self.assertNotEqual(row["status"], "needs_attention")
        # Brief pause only: the window is a minute or less, not the
        # minutes-to-hours quota ladder.
        self.assertIsNotNone(row["quota_exhausted_until"])
        # Once the pause passes the key is fully eligible again, and a
        # success restores it completely.
        self._sql("UPDATE gemini_keys SET quota_exhausted_until="
                  "'2000-01-01 00:00:00' WHERE id=?", (k["id"],))
        ids = [c[0] for c in gemini_keys.get_candidate_keys()]
        self.assertIn(k["id"], ids)
        gemini_keys.report_success(k["id"])
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertEqual(row["status"], "healthy")

    def test_transient_pause_ladder_caps_at_a_minute(self):
        k = self._add("ladder-key", "l", False)
        pauses = [gemini_keys._next_transient_pause_locked(k["id"])
                  for _ in range(5)]
        self.assertEqual(pauses, [15.0, 30.0, 60.0, 60.0, 60.0])

    def test_auth_failure_flags_attention_via_classify(self):
        k = self._add("auth-key", "a", False)
        gemini_keys.report_failure(
            k["id"], gemini_keys._classify_http_error(403))
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertEqual(row["needs_attention"], 1)
        self.assertEqual(row["status"], "needs_attention")

    def test_request_failure_blames_no_key(self):
        k = self._add("req-key", "r", False)
        gemini_keys.report_failure(k["id"], "request")
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertEqual(row["total_failures"], 0)
        self.assertIsNone(row["quota_exhausted_until"])
        self.assertEqual(row["total_requests"], 1)  # counted, not blamed

    def test_bad_request_stops_the_walk(self):
        # A 400 means the call itself was bad: post_json must try ONE
        # key, not sweep the pool at a request no key can serve.
        for i in range(4):
            self._add(f"br{i}", f"b{i}", False)
        calls = []

        def fake(url, payload, key_value, timeout):
            calls.append(key_value)
            raise _http_error(400)

        with mock.patch.object(gemini_keys, "_http_post",
                               side_effect=fake):
            result = gemini_keys.post_json("https://example.com/gen",
                                           {"x": 1})
        self.assertIsNone(result)
        self.assertEqual(len(calls), 1)

    def test_usage_rows_carry_error_class(self):
        self._add("quota-key", "q", False)
        self._add("flaky-key", "f", False)
        self._add("good-key", "g", False)
        self._add("spare-key", "s", False)  # highest id = reserve

        def behavior(url, payload, key_value, timeout):
            if key_value == "quota-key":
                raise _http_error(429)
            if key_value == "flaky-key":
                raise _http_error(503)
            return {"ok": True}

        with mock.patch.object(gemini_keys, "_http_post",
                               side_effect=behavior):
            result = gemini_keys.post_json("https://example.com/gen",
                                           {"x": 1}, source="class-src")
        self.assertIsNotNone(result)
        rows = [u for u in gemini_keys.get_usage(10)
                if u["source"] == "class-src"]
        failures = sorted(u["error_class"] for u in rows if not u["success"])
        self.assertEqual(failures, ["quota", "transient"])
        successes = [u for u in rows if u["success"]]
        self.assertEqual(len(successes), 1)
        self.assertIsNone(successes[0]["error_class"])


class TestPacing(GeminiKeysTestBase):
    def test_daily_count_spreads_candidates(self):
        keys = [self._add(f"pk{i}", f"p{i}", False) for i in range(4)]
        busy = keys[0]
        for _ in range(3):
            gemini_keys.report_success(busy["id"])
        # Neutralize the LRU tiebreak so only today's count decides.
        self._sql("UPDATE gemini_keys SET last_used_at=NULL")
        ids = [c[0] for c in gemini_keys.get_candidate_keys()]
        actives = ids[:3]  # 4 keys -> highest id is the reserve
        self.assertNotEqual(actives[0], busy["id"])
        self.assertEqual(actives[-1], busy["id"])

    def test_day_count_resets_on_new_day(self):
        k = self._add("day-key", "d", False)
        gemini_keys.report_success(k["id"])
        self._sql("UPDATE gemini_keys SET day_stamp='2000-01-01'"
                  " WHERE id=?", (k["id"],))
        conn = gemini_keys.db.connect()
        try:
            gemini_keys._ensure_table(conn)
            row = conn.execute(
                "SELECT * FROM gemini_keys WHERE id=?",
                (k["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(gemini_keys._day_count(row), 0)
        gemini_keys.report_success(k["id"])  # a fresh day starts at 1
        conn = gemini_keys.db.connect()
        try:
            row = conn.execute(
                "SELECT day_requests FROM gemini_keys WHERE id=?",
                (k["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["day_requests"], 1)


class TestFailFastEverywhere(GeminiKeysTestBase):
    def test_no_sweep_when_nothing_eligible(self):
        # Every key inside a quota backoff: post_json must make ONE
        # decision and return with zero attempts — for a plain source
        # too, not just fail-fast/voice ones (the all-keys last-resort
        # sweep is gone).
        for i in range(3):
            k = self._add(f"cool{i}", f"c{i}", False)
            gemini_keys.report_failure(k["id"], "quota")
        calls = []

        def fake(url, payload, key_value, timeout):
            calls.append(key_value)
            return {"ok": True}

        with mock.patch.object(gemini_keys, "_http_post",
                               side_effect=fake):
            result = gemini_keys.post_json("https://example.com/gen",
                                           {"x": 1}, source="plain-app")
        self.assertIsNotNone(result)
        self.assertEqual(calls, [])


class TestLeanProber(GeminiKeysTestBase):
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

    def test_prober_cycle_cap(self):
        for i in range(8):
            self._add(f"cap{i}", f"c{i}", False)
        calls = self._patch_http(lambda kv: {"ok": True})
        sent = gemini_keys._probe_cycle()
        self.assertEqual(sent, gemini_keys._PROBE_MAX_PER_CYCLE)
        self.assertEqual(len(calls), gemini_keys._PROBE_MAX_PER_CYCLE)

    def test_prober_skips_deep_cooldown(self):
        k = self._add("deep-key", "d", False)
        gemini_keys.report_failure(k["id"], "quota")  # 5-min window
        calls = self._patch_http(lambda kv: {"ok": True})
        sent = gemini_keys._probe_cycle()
        self.assertEqual(sent, 0)
        self.assertEqual(calls, [])

    def test_prober_confirm_window_is_lean(self):
        # Model fallback (2026-10-17): the prober now also covers every
        # model in GEMINI_MODEL_CHAIN. An idle cycle — no due primary
        # probe — sends one probe per due non-primary chain model instead
        # of staying fully quiet.
        k = self._add("idle-key", "i", False)
        gemini_keys.report_success(k["id"])
        # Belief is 300s old: fresher than the 420s confirm window, so
        # no primary re-confirm probe — but the fallback model is due.
        gemini_keys._belief[k["id"]]["at"] = time.time() - 300
        probed_models = []

        def fake(url, payload, key_value, timeout):
            probed_models.append(gemini_keys._model_from_url(url))
            return {"ok": True}

        with mock.patch.object(gemini_keys, "_http_post",
                               side_effect=fake):
            sent = gemini_keys._probe_cycle()
        self.assertEqual(sent, 1)
        self.assertEqual(probed_models, ["gemini-2.5-flash-lite"])

    def test_prober_reconfirms_after_confirm_window(self):
        # At 500s (> confirm window, < belief TTL) the primary IS due
        # for a re-confirm probe, so the cycle is not idle and no
        # fallback-model probe fires on top of it.
        k = self._add("idle-key", "i", False)
        gemini_keys.report_success(k["id"])
        gemini_keys._belief[k["id"]]["at"] = time.time() - 500
        probed_models = []

        def fake(url, payload, key_value, timeout):
            probed_models.append(gemini_keys._model_from_url(url))
            return {"ok": True}

        with mock.patch.object(gemini_keys, "_http_post",
                               side_effect=fake):
            sent = gemini_keys._probe_cycle()
        self.assertEqual(sent, 1)
        self.assertEqual(probed_models, ["gemini-3.1-flash-lite"])


class TestModelFallback(GeminiKeysTestBase):
    """Model fallback (2026-10-17): when the primary chain model is
    overloaded across keys, post_json routes to the next chain model;
    overload is never blamed on keys."""

    def _overload_error(self, code=503):
        # Real Gemini overload shape: 503 + "overloaded"/"UNAVAILABLE"
        # in the body, which _attempt reads via e.read().
        body = (b'{"error": {"code": 503, "message": "The model is '
                b'overloaded. Please try again later.", '
                b'"status": "UNAVAILABLE"}}')
        return urllib.error.HTTPError("https://example.test/gen", code,
                                      "err", None, io.BytesIO(body))

    def _patch_http(self, behavior):
        """behavior: fn(url, key_value) -> dict (or raises)."""
        calls = []

        def fake(url, payload, key_value, timeout):
            calls.append((gemini_keys._model_from_url(url), key_value))
            return behavior(url, key_value)

        patcher = mock.patch.object(gemini_keys, "_http_post",
                                    side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_chain_defaults(self):
        self.assertEqual(gemini_keys.model_chain(),
                         ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite"])
        self.assertEqual(gemini_keys.current_model(),
                         "gemini-3.1-flash-lite")
        self.assertIn("gemini-3.1-flash-lite:generateContent",
                      gemini_keys.model_url("gemini-3.1-flash-lite"))

    def test_chain_env_override(self):
        os.environ["GEMINI_MODEL_CHAIN"] = "a-model,b-model"
        self.assertEqual(gemini_keys.model_chain(), ["a-model", "b-model"])
        self.assertEqual(gemini_keys.current_model(), "a-model")

    def test_overload_marks_primary_hot_and_reroutes(self):
        # Two keys both hit an overloaded primary: the primary goes hot
        # and the walk reroutes to the fallback model mid-call.
        self._add("ok1", "a", False)
        self._add("ok2", "b", False)

        def behavior(url, key_value):
            if gemini_keys._model_from_url(url) == "gemini-3.1-flash-lite":
                raise self._overload_error()
            return {"ok": True}

        calls = self._patch_http(behavior)
        result = gemini_keys.post_json(
            gemini_keys.model_url("gemini-3.1-flash-lite"), {"x": 1})
        self.assertIsNotNone(result)
        models_hit = [m for m, _ in calls]
        self.assertIn("gemini-3.1-flash-lite", models_hit)
        self.assertIn("gemini-2.5-flash-lite", models_hit)
        # Primary is hot now: the next call goes straight to fallback.
        calls2 = self._patch_http(lambda url, kv: {"ok": True})
        gemini_keys.post_json(
            gemini_keys.model_url("gemini-3.1-flash-lite"), {"x": 1})
        self.assertTrue(all(m == "gemini-2.5-flash-lite"
                            for m, _ in calls2))

    def test_overload_blames_no_key(self):
        # 'model'-class failures take no key-health consequences.
        k = self._add("m-key", "m", False)
        gemini_keys.report_failure(k["id"], "model")
        row = {x["id"]: x for x in gemini_keys.list_keys()}[k["id"]]
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertEqual(row["total_failures"], 0)
        self.assertIsNone(row["quota_exhausted_until"])
        self.assertEqual(row["total_requests"], 1)

    def test_explicit_pin_passes_through(self):
        # A URL naming a non-primary model is never rewritten.
        self._add("pin-key", "p", False)
        calls = self._patch_http(lambda url, kv: {"ok": True})
        url = gemini_keys.model_url("gemini-2.5-flash-lite")
        gemini_keys.post_json(url, {"x": 1})
        self.assertEqual([m for m, _ in calls],
                         ["gemini-2.5-flash-lite"])


if __name__ == "__main__":
    unittest.main()
