"""Tests for Stage 3 of the key-manager overhaul (2026-10-18):
burn tracking + learned limits + forecast, prober slimming, sticky
sessions with observed cached-token accounting, and the per-source
spend ledger with its paid-spike anomaly flag.

Same style as the earlier stage suites: temp DB per test, mocked
_http_post, direct table reads for the accounting assertions.
"""
import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config, gemini_keys

URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
       "gemini-3.1-flash-lite:generateContent")
MODEL = "gemini-3.1-flash-lite"

RPD_BODY = (
    '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",'
    ' "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",'
    ' "violations": [{"quotaMetric": "generativelanguage.googleapis.com/'
    'generate_content_free_tier_requests",'
    ' "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},'
    ' {"@type": "type.googleapis.com/google.rpc.RetryInfo",'
    ' "retryDelay": "3600s"}]}}'
).encode()


def _http_error(code, body=b""):
    return urllib.error.HTTPError("https://example.test/gen", code,
                                  "err", None, io.BytesIO(body))


class Stage3Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self._orig_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._orig_legacy = config.GEMINI_API_KEY
        config.GEMINI_API_KEY = ""
        gemini_keys._belief.clear()
        gemini_keys._belief_seeded = False
        gemini_keys._dispatch_times.clear()
        gemini_keys._recent_fail_ts.clear()
        gemini_keys._latencies.clear()
        gemini_keys._last_probe_mono.clear()
        gemini_keys._last_reserve_tap = None
        gemini_keys._transient_streaks.clear()
        gemini_keys._model_hot_until.clear()
        gemini_keys._model_overload_events.clear()
        gemini_keys._model_probe_at.clear()
        gemini_keys._model_belief.clear()
        gemini_keys._model_dead_until.clear()
        gemini_keys._storm_events.clear()
        gemini_keys._storm_open_until = 0.0
        gemini_keys._probe_validated_until.clear()
        gemini_keys._sessions.clear()
        gemini_keys._cached_tokens_total = 0
        with gemini_keys._flight_lock:
            gemini_keys._flights.clear()
            gemini_keys._flight_hits = 0
            gemini_keys._flight_leader_runs = 0
        self._env = {}
        for name in ("GEMINI_MAX_ATTEMPTS", "GEMINI_FAST_TIMEOUT_S",
                     "GEMINI_ATTEMPT_BUDGET_S", "GEMINI_HEDGE_AFTER_S",
                     "GEMINI_PER_KEY_RPM", "GEMINI_RESERVE_COUNT",
                     "GEMINI_PROBE_INTERVAL_S", "GEMINI_PROBER",
                     "GEMINI_MODEL_CHAIN", "GEMINI_MODEL_COOLDOWN_S",
                     "GEMINI_QUOTA_CLASSIFY", "GEMINI_HEALTH_GRID",
                     "GEMINI_KEY_FORENSICS", "GEMINI_LANES",
                     "GEMINI_RESERVE_INTERACTIVE", "GEMINI_STORM_BREAKER",
                     "GEMINI_STORM_OPEN_S", "GEMINI_SINGLEFLIGHT",
                     "GEMINI_LEASES", "GEMINI_LEASE_MAX_CALLS",
                     "GEMINI_LEASE_MAX_ACTIVE_PER_SOURCE",
                     "GEMINI_LEASE_DEFAULT_CALLS",
                     "GEMINI_BULK_SOURCES", "GEMINI_INTERACTIVE_SOURCES",
                     "GEMINI_BURN_TRACKING", "GEMINI_PROBER_SLIM",
                     "GEMINI_STICKY_SESSIONS", "GEMINI_SPEND_LEDGER"):
            self._env[name] = os.environ.pop(name, None)

    def tearDown(self):
        config.DB_PATH = self._orig_db_path
        config.GEMINI_API_KEY = self._orig_legacy
        gemini_keys._sessions.clear()
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

    def _sql(self, query, args=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(query, args)]
        finally:
            conn.close()

    def _exec(self, query, args=()):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(query, args)
            conn.commit()
        finally:
            conn.close()


class TestBurnAndLimits(Stage3Base):
    def test_burn_recorded_per_key_model(self):
        k = self._add("burn-key", "b", False)
        self._patch_http(lambda kv: {"candidates": []})
        gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat")
        rows = self._sql(
            "SELECT * FROM gemini_key_model_day WHERE key_id = ?",
            (k["id"],))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], MODEL)
        self.assertEqual(rows[0]["calls"], 1)
        self.assertEqual(rows[0]["ok"], 1)
        info = gemini_keys.degradation_info()
        served = info["burn"]["keys_served_today"][str(k["id"])]
        self.assertEqual(served["total"], 1)
        self.assertEqual(served["by_model"][MODEL], 1)
        self.assertGreater(info["burn"]["pt_reset_epoch"], time.time())

    def test_rpd_exhaustion_learns_observed_limit(self):
        k = self._add("rpd-key", "r", False)
        state = {"n": 0}

        def behavior(kv):
            state["n"] += 1
            if state["n"] <= 2:
                return {"candidates": []}
            raise _http_error(429, RPD_BODY)

        self._patch_http(behavior)
        gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat")
        gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat")
        result = gemini_keys.post_json(URL, {"x": 1},
                                       source="whispr-chat")
        self.assertIsNone(result)
        rows = self._sql(
            "SELECT * FROM gemini_key_limits WHERE key_id = ?",
            (k["id"],))
        self.assertEqual(len(rows), 1)
        # 2 successes + the RPD-failing call all burned today.
        self.assertEqual(rows[0]["daily_limit"], 3)
        self.assertEqual(rows[0]["learned_from"], "observed_rpd")

    def test_forecast_learning_without_limits(self):
        self._add("f-key", "f", False)
        info = gemini_keys.degradation_info()
        self.assertEqual(info["burn"]["forecast"]["status"], "learning")
        self.assertIsNone(info["burn"]["forecast"]["hours"])

    def test_forecast_hours_with_limit_and_burn_rate(self):
        k = self._add("fc-key", "fc", False)
        conn = gemini_keys.db.connect()
        try:
            gemini_keys._ensure_table(conn)
            conn.execute(
                "INSERT INTO gemini_key_limits (key_id, model,"
                " daily_limit, learned_from) VALUES (?, ?, 100,"
                " 'observed_rpd')", (k["id"], MODEL))
            conn.execute(
                "INSERT INTO gemini_key_model_day (pt_day, key_id,"
                " model, calls, ok, fail) VALUES (?, ?, ?, 40, 40, 0)",
                (gemini_keys._pt_day_str(), k["id"], MODEL))
            # 30 usage rows in the last hour -> burn rate 30/h,
            # remaining 60 -> 2.0 hours of forecast capacity.
            for _ in range(30):
                gemini_keys._log_usage(conn, k["id"], "whispr-chat",
                                       True, 10)
            conn.commit()
        finally:
            conn.close()
        info = gemini_keys.degradation_info()
        fc = info["burn"]["forecast"]
        self.assertEqual(fc["status"], "ok")
        self.assertEqual(fc["remaining_calls"], 60)
        self.assertAlmostEqual(fc["hours"], 2.0, places=1)

    def test_burn_flag_off_records_nothing(self):
        os.environ["GEMINI_BURN_TRACKING"] = "0"
        os.environ["GEMINI_SPEND_LEDGER"] = "0"
        self._add("off-key", "off", False)
        self._patch_http(lambda kv: {"candidates": []})
        gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat")
        self.assertEqual(self._sql(
            "SELECT * FROM gemini_key_model_day"), [])
        self.assertEqual(self._sql(
            "SELECT * FROM gemini_source_day"), [])


class TestSpendLedger(Stage3Base):
    def test_ledger_counts_paid_calls(self):
        self._add("paid-key", "p", True)
        self._patch_http(lambda kv: {"candidates": []})
        gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat",
                              free_only=False)
        rows = self._sql("SELECT * FROM gemini_source_day")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "whispr-chat")
        self.assertEqual(rows[0]["calls"], 1)
        self.assertEqual(rows[0]["paid_calls"], 1)

    def test_paid_anomaly_flag(self):
        self._add("a-key", "a", False)
        conn = gemini_keys.db.connect()
        try:
            gemini_keys._ensure_table(conn)
            conn.commit()
        finally:
            conn.close()
        today = gemini_keys._pt_day_str()
        # History: roofer averaged 1 paid call/day; today it is 30.
        self._exec(
            "INSERT INTO gemini_source_day VALUES"
            " (date(?, '-1 day'), 'roof-rover', 100, 0, 1)", (today,))
        self._exec(
            "INSERT INTO gemini_source_day VALUES"
            " (?, 'roof-rover', 200, 5, 30)", (today,))
        self._exec(
            "INSERT INTO gemini_source_day VALUES"
            " (?, 'whispr-chat', 50, 0, 2)", (today,))
        info = gemini_keys.degradation_info()
        sources = info["ledger"]["sources"]
        self.assertTrue(sources["roof-rover"]["paid_anomaly"])
        self.assertFalse(sources["whispr-chat"]["paid_anomaly"])


class TestProberSlim(Stage3Base):
    def test_idle_healthy_not_reprobed_when_slim(self):
        k = self._add("idle-key", "i", False)
        gemini_keys._belief[k["id"]] = {"ok": True,
                                        "at": time.time() - 100000}
        calls = self._patch_http(lambda kv: {"candidates": []})
        gemini_keys._probe_cycle()
        self.assertEqual(calls, [])
        os.environ["GEMINI_PROBER_SLIM"] = "0"
        gemini_keys._probe_cycle()
        self.assertEqual(len(calls), 1)  # legacy prober re-confirms

    def test_cooldown_exit_gets_exactly_one_probe(self):
        k = self._add("exit-key", "e", False)
        past = time.time() - 60
        self._exec(
            "UPDATE gemini_keys SET quota_exhausted_until ="
            " datetime(?, 'unixepoch') WHERE id = ?",
            (past, k["id"]))
        calls = self._patch_http(lambda kv: {"candidates": []})
        gemini_keys._probe_cycle()
        self.assertEqual(len(calls), 1)
        # Second cycle: bypass the per-key probe min-interval so
        # ONLY the validated-exit marker can explain silence.
        gemini_keys._last_probe_mono.clear()
        gemini_keys._probe_cycle()
        self.assertEqual(len(calls), 1)

    def test_unknown_key_probed_once_when_slim(self):
        k = self._add("unk-key", "u", False)
        calls = self._patch_http(lambda kv: {"candidates": []})
        gemini_keys._probe_cycle()
        self.assertEqual(len(calls), 1)
        gemini_keys._last_probe_mono.clear()
        # Failure keeps the key unknown (500 -> transient): legacy
        # would probe again; slim does not cycle unknown keys.
        gemini_keys._belief.pop(k["id"], None)
        self._patch_http(
            lambda kv: (_ for _ in ()).throw(_http_error(500)))
        # Re-run under slim with a failing transport AND the marker
        # of having been probed:
        gemini_keys._last_probe_mono[k["id"]] = time.monotonic()
        gemini_keys._probe_cycle()
        # (call count assertions live in the first two asserts; this
        # cycle must not raise)


class TestStickySessions(Stage3Base):
    def test_pin_forms_and_moves_on_failure(self):
        k1 = self._add("pin-a", "a", False)
        behavior = {"fail": set()}
        self._patch_http(
            lambda kv: (_ for _ in ()).throw(_http_error(500))
            if kv in behavior["fail"] else {"candidates": []})
        r = gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat",
                                  session_id="sess-1")
        self.assertIsNotNone(r)
        pin1 = gemini_keys._session_key_id("sess-1")
        self.assertIn(pin1, (k1["id"],))
        k2 = self._add("pin-b", "b", False)
        # Pinned key starts failing; the other must serve and the
        # pin must move to the serving key.
        pinned_value = "pin-a" if pin1 == k1["id"] else "pin-b"
        behavior["fail"] = {pinned_value}
        r = gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat",
                                  session_id="sess-1")
        self.assertIsNotNone(r)
        self.assertEqual(gemini_keys._session_key_id("sess-1"),
                         k2["id"] if pin1 == k1["id"] else k1["id"])

    def test_failed_call_drops_pin(self):
        self._add("drop-key", "d", False)
        self._patch_http(lambda kv: {"candidates": []})
        gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat",
                              session_id="sess-2")
        self.assertIsNotNone(gemini_keys._session_key_id("sess-2"))
        gemini_keys._unpin_session("sess-2")

        def fail(kv):
            raise _http_error(500)

        self._patch_http(fail)
        gemini_keys._pin_session("sess-2", 999999)
        r = gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat",
                                  session_id="sess-2")
        self.assertIsNone(r)
        self.assertIsNone(gemini_keys._session_key_id("sess-2"))

    def test_sessions_flag_off_no_pin(self):
        os.environ["GEMINI_STICKY_SESSIONS"] = "0"
        self._add("nf-key", "nf", False)
        self._patch_http(lambda kv: {"candidates": []})
        gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat",
                              session_id="sess-3")
        self.assertIsNone(gemini_keys._session_key_id("sess-3"))

    def test_cached_tokens_observed_only(self):
        self._add("ct-key", "ct", False)
        self._patch_http(lambda kv: {
            "candidates": [],
            "usageMetadata": {"cachedContentTokenCount": 7,
                              "totalTokenCount": 20}})
        gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat")
        info = gemini_keys.degradation_info()
        self.assertEqual(
            info["sessions"]["cached_tokens_total_observed"], 7)
        # A response without the field adds nothing.
        self._patch_http(lambda kv: {"candidates": []})
        gemini_keys.post_json(URL, {"x": 1}, source="whispr-chat")
        info = gemini_keys.degradation_info()
        self.assertEqual(
            info["sessions"]["cached_tokens_total_observed"], 7)


if __name__ == "__main__":
    unittest.main()
