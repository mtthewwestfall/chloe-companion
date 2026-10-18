"""Tests for Stage 2 of the key-manager overhaul (2026-10-18):
reserved lanes for interactive traffic, the storm breaker with a
successes-proportional attempts ceiling, single-flight coalescing,
and manager-issued leases (incl. the proxy endpoint wiring).

Same style as tests/test_gemini_keys.py: temp DB per test, mocked
_http_post, threads only where the feature is concurrency itself.
"""
import io
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config, gemini_keys

try:
    from app import main as api
    from fastapi import HTTPException
    _HAVE_API = True
except Exception:  # pragma: no cover - deps not installed
    api = None
    HTTPException = None
    _HAVE_API = False


def _http_error(code):
    return urllib.error.HTTPError("https://example.test/gen", code,
                                  "err", None, io.BytesIO(b""))


class Stage2Base(unittest.TestCase):
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
                     "GEMINI_BULK_SOURCES", "GEMINI_INTERACTIVE_SOURCES"):
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

    def _trip_storm(self, fails=20):
        with gemini_keys._lock:
            for _ in range(fails):
                gemini_keys._note_storm_event_locked(False)


class TestSourceClass(Stage2Base):
    def test_classes(self):
        self.assertEqual(gemini_keys._source_class("roof-rover"), "bulk")
        self.assertEqual(
            gemini_keys._source_class("roof-rover-scan"), "bulk")
        self.assertEqual(
            gemini_keys._source_class("roof-rover-voice"), "interactive")
        self.assertEqual(
            gemini_keys._source_class("whispr-chat"), "interactive")
        self.assertEqual(gemini_keys._source_class("prober"), "bulk")
        self.assertEqual(
            gemini_keys._source_class("gods-greek"), "interactive")

    def test_env_override(self):
        os.environ["GEMINI_BULK_SOURCES"] = "gods-greek"
        self.assertEqual(
            gemini_keys._source_class("gods-greek"), "bulk")
        os.environ["GEMINI_INTERACTIVE_SOURCES"] = "roof-rover"
        self.assertEqual(
            gemini_keys._source_class("roof-rover"), "interactive")


class TestLanes(Stage2Base):
    def _six_keys(self):
        return [self._add(f"key-{i}", f"k{i}", False) for i in range(6)]

    def test_reserve_ids_are_top_eligible_free(self):
        keys = self._six_keys()
        os.environ["GEMINI_RESERVE_INTERACTIVE"] = "2"
        conn = gemini_keys.db.connect()
        try:
            gemini_keys._ensure_table(conn)
            ids = gemini_keys._lane_reserve_ids(conn)
        finally:
            conn.close()
        self.assertEqual(ids, {keys[4]["id"], keys[5]["id"]})

    def test_bulk_never_draws_reserve(self):
        keys = self._six_keys()
        os.environ["GEMINI_RESERVE_INTERACTIVE"] = "2"
        reserve_vals = {"key-4", "key-5"}
        calls = self._patch_http(
            lambda kv: {"ok": True} if kv in reserve_vals
            else (_ for _ in ()).throw(_http_error(500)))
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="roof-rover")
        self.assertIsNone(result)
        self.assertTrue(all(c not in reserve_vals for c in calls))
        self.assertTrue(len(calls) >= 1)  # general keys were tried

    def test_bulk_pool_busy_when_general_down(self):
        keys = self._six_keys()
        os.environ["GEMINI_RESERVE_INTERACTIVE"] = "2"
        # General keys (first four) parked; reserve keys healthy.
        for k in keys[:4]:
            gemini_keys.report_failure(k["id"], "quota")
        calls = self._patch_http(lambda kv: {"ok": True})
        status = {}
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="roof-rover",
            status_out=status)
        self.assertIsNone(result)
        self.assertEqual(calls, [])  # reserve never touched
        self.assertEqual(status.get("error"), "pool_busy")
        self.assertGreaterEqual(status.get("retry_after_s"), 5)
        # Interactive IS served by the reserve.
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="whispr-chat")
        self.assertIsNotNone(result)
        self.assertTrue(set(calls) <= {"key-4", "key-5"})

    def test_lanes_flag_off(self):
        keys = self._six_keys()
        os.environ["GEMINI_RESERVE_INTERACTIVE"] = "2"
        os.environ["GEMINI_LANES"] = "0"
        for k in keys[:4]:
            gemini_keys.report_failure(k["id"], "quota")
        self._patch_http(lambda kv: {"ok": True})
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="roof-rover")
        self.assertIsNotNone(result)  # reserve usable again


class TestStormBreaker(Stage2Base):
    def test_breaker_trips_and_bulk_fails_fast(self):
        self._add("s-key", "s", False)
        self._trip_storm(20)
        stats = gemini_keys._storm_stats()
        self.assertTrue(stats["open"])
        calls = self._patch_http(lambda kv: {"ok": True})
        status = {}
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="roof-rover",
            status_out=status)
        self.assertIsNone(result)
        self.assertEqual(calls, [])  # zero upstream attempts
        self.assertEqual(status.get("error"), "pool_busy")
        self.assertEqual(status.get("reason"), "storm")

    def test_interactive_shortened_walk_in_storm(self):
        for i in range(4):
            self._add(f"ik-{i}", f"ik{i}", False)
        self._trip_storm(20)
        calls = self._patch_http(
            lambda kv: (_ for _ in ()).throw(_http_error(500)))
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="whispr-chat")
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)  # shortened walk, not 4+

    def test_success_ceiling_without_trip(self):
        for i in range(5):
            self._add(f"ck-{i}", f"ck{i}", False)
        # 10 window events, 1 success: below the trip count (20) but
        # the successes ceiling clamps attempts to max(2, 1) = 2.
        with gemini_keys._lock:
            gemini_keys._note_storm_event_locked(True)
            for _ in range(9):
                gemini_keys._note_storm_event_locked(False)
        self.assertFalse(gemini_keys._storm_stats()["open"])
        calls = self._patch_http(
            lambda kv: (_ for _ in ()).throw(_http_error(500)))
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="whispr-chat")
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)

    def test_storm_flag_off(self):
        os.environ["GEMINI_STORM_BREAKER"] = "0"
        self._add("off-key", "off", False)
        self._trip_storm(20)
        self.assertFalse(gemini_keys._storm_stats()["open"])


class TestSingleFlight(Stage2Base):
    def test_concurrent_identical_coalesce(self):
        self._add("sf-key", "sf", False)
        upstream = []

        def behavior(kv):
            upstream.append(kv)
            time.sleep(0.4)
            return {"candidates": [{"text": "hi"}]}

        self._patch_http(behavior)
        results = [None] * 4
        barrier = threading.Barrier(4)

        def worker(i):
            barrier.wait()
            results[i] = gemini_keys.post_json(
                "https://example.com/gen", {"x": 1}, source="whispr-chat")

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(len(upstream), 1)  # one upstream execution
        self.assertTrue(all(r == {"candidates": [{"text": "hi"}]}
                            for r in results))
        # Waiters get deep copies, not the leader's object.
        self.assertIsNot(results[0], results[1])
        info = gemini_keys.degradation_info()
        self.assertGreaterEqual(info["singleflight"]["hits"], 1)

    def test_failed_leader_waiter_runs_itself(self):
        self._add("sf2-key", "sf2", False)
        upstream = []

        def behavior(kv):
            upstream.append(kv)
            time.sleep(0.2)
            # A 'request'-class failure (400) pauses nothing, so the
            # waiter's own run can really attempt the key again —
            # with a 500 the key would be paused and the second run
            # would (correctly) find the pool down with 0 attempts.
            raise _http_error(400)

        self._patch_http(behavior)
        results = [None] * 2
        barrier = threading.Barrier(2)

        def worker(i):
            barrier.wait()
            results[i] = gemini_keys.post_json(
                "https://example.com/gen", {"y": 2}, source="whispr-chat")

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertTrue(all(r is None for r in results))
        self.assertEqual(len(upstream), 2)  # leader + waiter's own run

    def test_different_payloads_do_not_coalesce(self):
        self._add("sf3-key", "sf3", False)
        upstream = []
        self._patch_http(
            lambda kv: (upstream.append(kv), {"ok": True})[1])
        gemini_keys.post_json("https://example.com/gen", {"a": 1},
                              source="whispr-chat")
        gemini_keys.post_json("https://example.com/gen", {"a": 2},
                              source="whispr-chat")
        self.assertEqual(len(upstream), 2)


class TestLeases(Stage2Base):
    def setUp(self):
        super().setUp()
        # Isolate lease mechanics from the lanes (few keys here —
        # with lanes on they would all sit in the reserve).
        os.environ["GEMINI_LANES"] = "0"

    def test_request_and_idempotent(self):
        lease = gemini_keys.request_lease("roof-rover", "scan-1", 100)
        self.assertIsNotNone(lease)
        self.assertEqual(lease["budget_total"], 100)
        again = gemini_keys.request_lease("roof-rover", "scan-1", 100)
        self.assertEqual(again["lease_id"], lease["lease_id"])

    def test_request_denied_too_big(self):
        os.environ["GEMINI_LEASE_MAX_CALLS"] = "10"
        self.assertIsNone(
            gemini_keys.request_lease("roof-rover", "scan-x", 11))

    def test_request_denied_too_many_active(self):
        os.environ["GEMINI_LEASE_MAX_ACTIVE_PER_SOURCE"] = "1"
        self.assertIsNotNone(
            gemini_keys.request_lease("roof-rover", "job-a", 5))
        self.assertIsNone(
            gemini_keys.request_lease("roof-rover", "job-b", 5))
        # A different source is unaffected.
        self.assertIsNotNone(
            gemini_keys.request_lease("warmup", "job-b", 5))

    def test_lease_exhaustion_stops_walk(self):
        for i in range(4):
            self._add(f"lk-{i}", f"lk{i}", False)
        lease = gemini_keys.request_lease("roof-rover", "scan-2", 2)
        calls = self._patch_http(
            lambda kv: (_ for _ in ()).throw(_http_error(500)))
        status = {}
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="roof-rover",
            lease_id=lease["lease_id"], status_out=status)
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)  # budget spent, walk stopped
        self.assertEqual(status.get("error"), "lease_exhausted")
        st = gemini_keys.lease_status(lease["lease_id"])
        self.assertEqual(st["budget_used"], 2)
        self.assertEqual(st["budget_left"], 0)

    def test_lease_charges_per_attempt(self):
        self._add("first-key", "f", False)
        self._add("second-key", "s", False)
        lease = gemini_keys.request_lease("roof-rover", "scan-3", 10)

        def behavior(kv):
            if kv == "first-key":
                raise _http_error(429)
            return {"ok": True}

        self._patch_http(behavior)
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="roof-rover",
            lease_id=lease["lease_id"])
        self.assertIsNotNone(result)
        st = gemini_keys.lease_status(lease["lease_id"])
        self.assertEqual(st["budget_used"], 2)  # failed + successful

    def test_default_source_lease_accounting(self):
        self._add("d-key", "d", False)
        self._patch_http(lambda kv: {"ok": True})
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="roof-rover")
        self.assertIsNotNone(result)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT budget_used FROM gemini_leases"
                " WHERE source = 'roof-rover'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row[0], 1)

    def test_leases_flag_off_ignores_bogus_id(self):
        os.environ["GEMINI_LEASES"] = "0"
        self.assertIsNone(
            gemini_keys.request_lease("roof-rover", "scan-9", 5))
        self._add("off-key", "off", False)
        self._patch_http(lambda kv: {"ok": True})
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="roof-rover",
            lease_id="lease_doesnotexist")
        self.assertIsNotNone(result)

    def test_release_lease(self):
        lease = gemini_keys.request_lease("roof-rover", "scan-4", 5)
        self.assertTrue(gemini_keys.release_lease(lease["lease_id"]))
        st = gemini_keys.lease_status(lease["lease_id"])
        self.assertEqual(st["status"], "released")


@unittest.skipUnless(_HAVE_API, "fastapi/app.main unavailable")
class TestProxyLeaseWiring(Stage2Base):
    class _Req:
        def __init__(self, headers=None):
            self.headers = headers or {}

    def setUp(self):
        super().setUp()
        os.environ["GEMINI_LANES"] = "0"  # see TestLeases
        self._orig_token = api._GEMINI_PROXY_TOKEN
        api._GEMINI_PROXY_TOKEN = "test-proxy-token"

    def tearDown(self):
        api._GEMINI_PROXY_TOKEN = self._orig_token
        super().tearDown()

    def _req(self, extra=None):
        headers = {"authorization": "Bearer test-proxy-token"}
        headers.update(extra or {})
        return self._Req(headers)

    def test_lease_endpoint_issues_and_denies(self):
        lease = api.proxy_pool_lease(
            {"source": "roof-rover", "job_id": "scan-p", "calls": 3},
            self._req())
        self.assertIn("lease_id", lease)
        with self.assertRaises(HTTPException) as cm:
            api.proxy_pool_lease(
                {"source": "roof-rover", "job_id": "", "calls": 3},
                self._req())
        self.assertEqual(cm.exception.status_code, 429)

    def test_generate_content_lease_exhausted_is_429(self):
        self._add("px-key", "px", False)
        lease = gemini_keys.request_lease("roof-rover", "scan-q", 1)
        self._patch_http(
            lambda kv: (_ for _ in ()).throw(_http_error(500)))
        req = self._req({"x-source": "roof-rover",
                         "x-pool-lease": lease["lease_id"]})
        with self.assertRaises(HTTPException) as cm:
            api.proxy_gemini_generate_content(
                "gemini-3.1-flash-lite",
                {"contents": [{"parts": [{"text": "hi"}]}]}, req)
        self.assertEqual(cm.exception.status_code, 429)
        self.assertEqual(cm.exception.detail, "lease_exhausted")


if __name__ == "__main__":
    unittest.main()
