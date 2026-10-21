"""Tests for Key Manager v2 (2026-10-20): project grouping + group
RPD parks, the pre-call quota ledger, per-key circuit breakers,
retry discipline, usage-based selection, priority lanes, paid-key
guardrails, reset-aware scheduling facts, and the admin rollup.

Same style as tests/test_gemini_keys_stage2.py: temp DB per test,
mocked _http_post, env snapshotted and restored per test.
"""
import io
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config, gemini_keys

PRIMARY = "gemini-3.1-flash-lite"
URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
       + PRIMARY + ":generateContent")

_RPD_BODY = (
    b'{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",'
    b' "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",'
    b' "violations": [{"quotaId": '
    b'"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},'
    b' {"@type": "type.googleapis.com/google.rpc.RetryInfo",'
    b' "retryDelay": "3600s"}]}}')

_RPM_BODY = (
    b'{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",'
    b' "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",'
    b' "violations": [{"quotaId": '
    b'"GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},'
    b' {"@type": "type.googleapis.com/google.rpc.RetryInfo",'
    b' "retryDelay": "39s"}]}}')

_ENV_NAMES = (
    "GEMINI_KEY_V2", "GEMINI_DAILY_LIMITS", "GEMINI_RPM_LIMITS",
    "GEMINI_PAID_DAILY_CEILING", "GEMINI_PAID_TRICKLE_DAILY",
    "GEMINI_CROSS_KEY_RETRIES", "GEMINI_BREAKER",
    "GEMINI_BREAKER_MIN_ATTEMPTS", "GEMINI_BREAKER_COOLDOWN_S",
    "GEMINI_LANE_MIN_BUDGET_BATCH", "GEMINI_LANE_MIN_BUDGET_AMBIENT",
    "GEMINI_LANE_MIN_BUDGET_MORNING", "GEMINI_MAX_ATTEMPTS",
    "GEMINI_ATTEMPT_BUDGET_S", "GEMINI_RESERVE_COUNT",
    "GEMINI_MODEL_CHAIN", "GEMINI_BURN_TRACKING", "GEMINI_STORM_BREAKER",
    "GEMINI_LANES", "GEMINI_LEASES", "GEMINI_SINGLEFLIGHT",
    "GEMINI_KEY_PROJECTS", "GEMINI_PROBER", "GEMINI_QUOTA_CLASSIFY",
    "GEMINI_HEALTH_GRID", "GEMINI_PER_KEY_RPM", "GEMINI_HEDGE_AFTER_S",
    "GEMINI_RESERVE_INTERACTIVE", "GEMINI_STICKY_SESSIONS",
    "GEMINI_PROBER_SLIM",
)


def _http_error(code, body=b""):
    return urllib.error.HTTPError("https://example.test/gen", code,
                                  "err", None, io.BytesIO(body))


class V2Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self._orig_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._orig_legacy = config.GEMINI_API_KEY
        config.GEMINI_API_KEY = None
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
        gemini_keys._probe_validated_until.clear()
        gemini_keys._sessions.clear()
        gemini_keys._outcomes.clear()
        gemini_keys._bench_until.clear()
        gemini_keys._paid_alert_day.clear()
        gemini_keys._storm_open_until = 0.0
        with gemini_keys._flight_lock:
            gemini_keys._flights.clear()
        self._env = {n: os.environ.get(n) for n in _ENV_NAMES}
        for n in _ENV_NAMES:
            os.environ.pop(n, None)
        # Suite hygiene: once any earlier test module has started
        # the background prober (app lifespan does), it ticks
        # against whatever DB config.DB_PATH currently names —
        # including this test's temp DB, with this test's mocked
        # _http_post — and its probes would park/count keys behind
        # the test's back. Park the seam for the test's duration.
        self._orig_probe_cycle = gemini_keys._probe_cycle
        gemini_keys._probe_cycle = lambda: 0
        # Deterministic pool shape for v2 tests: no reserve split,
        # no Stage-2 interactive key reserve (it would hold back
        # every key in these small pools).
        os.environ["GEMINI_RESERVE_COUNT"] = "0"
        os.environ["GEMINI_RESERVE_INTERACTIVE"] = "0"

    def tearDown(self):
        gemini_keys._probe_cycle = self._orig_probe_cycle
        config.DB_PATH = self._orig_db_path
        config.GEMINI_API_KEY = self._orig_legacy
        for n, v in self._env.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # -- helpers ------------------------------------------------------
    def _add(self, key="test-key-1", label="test", is_paid=False,
             project=""):
        return gemini_keys.add_key(key, label, is_paid, project)

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

    def _row(self, key_id):
        conn = gemini_keys.db.connect()
        try:
            gemini_keys._ensure_table(conn)
            return conn.execute(
                "SELECT * FROM gemini_keys WHERE id = ?",
                (key_id,)).fetchone()
        finally:
            conn.close()

    def _seed_burn(self, key_id, calls, model=PRIMARY):
        conn = gemini_keys.db.connect()
        try:
            gemini_keys._ensure_table(conn)
            conn.execute(
                """INSERT INTO gemini_key_model_day
                       (pt_day, key_id, model, calls, ok, fail)
                   VALUES (?, ?, ?, ?, 0, 0)
                   ON CONFLICT(pt_day, key_id, model) DO UPDATE SET
                       calls = excluded.calls""",
                (gemini_keys._pt_day_str(), key_id, model, calls))
            conn.commit()
        finally:
            conn.close()


class TestProjectIdentity(V2Base):
    def test_default_project_is_the_key_itself(self):
        k = self._add("k-one", "Free 1")
        self.assertEqual(k["project"], "key-%d" % k["id"])
        self.assertFalse(k["project_assigned"])

    def test_add_and_set_project(self):
        k = self._add("k-two", "Free 2", project="proj-alpha")
        self.assertEqual(k["project"], "proj-alpha")
        self.assertTrue(k["project_assigned"])
        updated = gemini_keys.set_project(k["id"], "proj-beta")
        self.assertEqual(updated["project"], "proj-beta")
        cleared = gemini_keys.set_project(k["id"], "")
        self.assertEqual(cleared["project"], "key-%d" % k["id"])

    def test_label_seed_from_env(self):
        os.environ["GEMINI_KEY_PROJECTS"] = "Free 35=proj-seven"
        k = self._add("k-three", "Free 35")
        # Seeding runs at ensure-table time (add_key already ensured);
        # force another ensure cycle the way a boot would.
        conn = gemini_keys.db.connect()
        try:
            gemini_keys._seed_projects_from_env(conn)
            row = conn.execute(
                "SELECT project FROM gemini_keys WHERE id = ?",
                (k["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["project"], "proj-seven")


class TestProjectGroupPark(V2Base):
    def test_rpd_parks_whole_project_not_others(self):
        # B shares A's project but sits in the RESERVE pool (highest
        # id), so the sweep never attempts it: if B ends up parked,
        # only the group rule can have done it. C is in another
        # project and serves the call, so the fallback pass (which
        # could have reached B) never runs either.
        os.environ["GEMINI_RESERVE_COUNT"] = "1"
        a = self._add("k-a", "A", project="proj-x")
        c = self._add("k-c", "C", project="proj-y")
        b = self._add("k-b", "B", project="proj-x")

        def behavior(kv):
            if kv == "k-a":
                raise _http_error(429, _RPD_BODY)
            return {"ok": True}

        self._patch_http(behavior)
        result = gemini_keys.post_json(URL, {"x": 1}, source="test")
        self.assertEqual(result, {"ok": True})
        ra, rb, rc = self._row(a["id"]), self._row(b["id"]), self._row(c["id"])
        self.assertEqual(ra["quota_park_kind"], "rpd")
        # Parked by the GROUP rule alone — B was never attempted.
        self.assertEqual(rb["quota_park_kind"], "rpd")
        self.assertIsNotNone(rb["quota_exhausted_until"])
        self.assertEqual(rb["total_requests"], 0)
        # A different project is untouched.
        self.assertIsNone(rc["quota_park_kind"])
        self.assertIsNone(rc["quota_exhausted_until"])

    def test_rpm_park_does_not_group(self):
        a = self._add("k-a", "A", project="proj-x")
        b = self._add("k-b", "B", project="proj-x")
        calls = self._patch_http(lambda kv: (_ for _ in ()).throw(
            _http_error(429, _RPM_BODY)))
        gemini_keys.post_json(URL, {"x": 1}, source="test")
        ra = self._row(a["id"])
        rb = self._row(b["id"])
        self.assertIn(ra["quota_park_kind"], ("rpm", None))
        # Seconds-scale park only — never the group RPD park.
        self.assertNotEqual(rb["quota_park_kind"], "rpd")

    def test_project_limit_learned_from_rpd(self):
        a = self._add("k-a", "A", project="proj-x")
        self._seed_burn(a["id"], 7)
        gemini_keys._learn_project_limit_from_rpd(a["id"], PRIMARY)
        conn = gemini_keys.db.connect()
        try:
            row = conn.execute(
                """SELECT daily_limit FROM gemini_project_limits
                   WHERE project = 'proj-x' AND model = ?""",
                (PRIMARY,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["daily_limit"], 7)


class TestQuotaLedger(V2Base):
    def test_daily_limit_refuses_before_any_call(self):
        os.environ["GEMINI_DAILY_LIMITS"] = '{"%s": 3}' % PRIMARY
        k = self._add("k-a", "A", project="proj-x")
        self._seed_burn(k["id"], 3)
        calls = self._patch_http(lambda kv: {"ok": True})
        status = {}
        result = gemini_keys.post_json(URL, {"x": 1}, source="test",
                                       status_out=status)
        self.assertIsNone(result)
        self.assertEqual(calls, [])  # refused BEFORE calling Google
        self.assertEqual(status.get("error"), "pool_busy")
        self.assertEqual(status.get("reason"), "ledger")
        self.assertGreater(status.get("retry_after_s", 0), 0)

    def test_under_limit_still_serves(self):
        os.environ["GEMINI_DAILY_LIMITS"] = '{"%s": 3}' % PRIMARY
        k = self._add("k-a", "A", project="proj-x")
        self._seed_burn(k["id"], 2)
        self._patch_http(lambda kv: {"ok": True})
        result = gemini_keys.post_json(URL, {"x": 1}, source="test")
        self.assertEqual(result, {"ok": True})

    def test_blocked_project_skipped_other_project_serves(self):
        os.environ["GEMINI_DAILY_LIMITS"] = '{"%s": 3}' % PRIMARY
        a = self._add("k-a", "A", project="proj-x")
        b = self._add("k-b", "B", project="proj-y")
        self._seed_burn(a["id"], 3)
        calls = self._patch_http(lambda kv: {"ok": True})
        result = gemini_keys.post_json(URL, {"x": 1}, source="test")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["k-b"])


class TestCircuitBreaker(V2Base):
    def test_breaker_benches_after_minute_of_failures(self):
        k = self._add("k-a", "A")
        other = self._add("k-b", "B")
        self._patch_http(lambda kv: (_ for _ in ()).throw(
            _http_error(500)))
        # Drive the real attempt seam: 5 failures inside a minute.
        # (post_json can't accumulate these on one key — a transient
        # failure pauses the key, which is exactly why the breaker
        # reads the attempt-level outcome stream instead.)
        for _ in range(5):
            ok, _d, _s, _e = gemini_keys._attempt(
                k["id"], "k-a", URL, {"x": 1}, 5, "test")
            self.assertFalse(ok)
        self.assertIn(k["id"], gemini_keys._benched_ids())
        # And the benched key leaves the candidate list entirely.
        cand_ids = [c[0] for c in gemini_keys.get_candidate_keys()]
        self.assertNotIn(k["id"], cand_ids)
        self.assertIn(other["id"], cand_ids)

    def test_few_failures_do_not_bench(self):
        k = self._add("k-a", "A")
        self._patch_http(lambda kv: (_ for _ in ()).throw(
            _http_error(500)))
        for _ in range(2):
            gemini_keys._attempt(k["id"], "k-a", URL, {"x": 1}, 5,
                                 "test")
        self.assertNotIn(k["id"], gemini_keys._benched_ids())

    def test_success_rehabilitates_breaker_and_attention(self):
        k = self._add("k-a", "A")
        gemini_keys.report_failure(k["id"], "payment")
        self.assertEqual(self._row(k["id"])["needs_attention"], 1)
        with gemini_keys._lock:
            gemini_keys._note_outcome_locked(k["id"], False)
            gemini_keys._bench_until[k["id"]] = time.monotonic() + 60
        gemini_keys.report_success(k["id"])
        row = self._row(k["id"])
        self.assertEqual(row["needs_attention"], 0)
        self.assertNotIn(k["id"], gemini_keys._benched_ids())


class TestRetryDiscipline(V2Base):
    def test_default_one_cross_key_retry(self):
        for i in range(5):
            self._add("k-%d" % i, "K%d" % i)
        calls = self._patch_http(lambda kv: (_ for _ in ()).throw(
            _http_error(500)))
        result = gemini_keys.post_json(URL, {"x": 1}, source="test")
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)  # 1 attempt + 1 retry, no storm

    def test_retry_env_widens(self):
        os.environ["GEMINI_CROSS_KEY_RETRIES"] = "3"
        for i in range(6):
            self._add("k-%d" % i, "K%d" % i)
        calls = self._patch_http(lambda kv: (_ for _ in ()).throw(
            _http_error(500)))
        gemini_keys.post_json(URL, {"x": 1}, source="test")
        self.assertEqual(len(calls), 4)

    def test_explicit_max_attempts_wins(self):
        for i in range(6):
            self._add("k-%d" % i, "K%d" % i)
        calls = self._patch_http(lambda kv: (_ for _ in ()).throw(
            _http_error(500)))
        gemini_keys.post_json(URL, {"x": 1}, source="test",
                              max_attempts=5)
        self.assertEqual(len(calls), 5)


class TestPriorityLanes(V2Base):
    def _drain_pool_to(self, used):
        os.environ["GEMINI_DAILY_LIMITS"] = '{"%s": 100}' % PRIMARY
        a = self._add("k-a", "A", project="proj-x")
        b = self._add("k-b", "B", project="proj-y")
        self._seed_burn(a["id"], used)
        self._seed_burn(b["id"], used)
        return a, b

    def test_batch_refused_first_chat_last(self):
        self._drain_pool_to(90)  # 10% of budget remains
        calls = self._patch_http(lambda kv: {"ok": True})
        status = {}
        result = gemini_keys.post_json(URL, {"x": 1}, source="test",
                                       lane="batch", status_out=status)
        self.assertIsNone(result)
        self.assertEqual(status.get("reason"), "lane_budget")
        self.assertEqual(status.get("lane"), "batch")
        self.assertEqual(calls, [])
        # Ambient floor (8%) is below the remaining 10%: still served.
        result = gemini_keys.post_json(URL, {"x": 1}, source="test",
                                       lane="proactive_ambient")
        self.assertEqual(result, {"ok": True})
        # Interactive is never budget-refused.
        result = gemini_keys.post_json(URL, {"x": 1}, source="test",
                                       lane="interactive")
        self.assertEqual(result, {"ok": True})

    def test_ambient_refused_before_morning(self):
        self._drain_pool_to(96)  # 4% remains
        self._patch_http(lambda kv: {"ok": True})
        status = {}
        result = gemini_keys.post_json(URL, {"x": 1}, source="test",
                                       lane="proactive_ambient",
                                       status_out=status)
        self.assertIsNone(result)
        self.assertEqual(status.get("reason"), "lane_budget")
        # Morning floor is 3%: 4% remaining still serves.
        result = gemini_keys.post_json(URL, {"x": 1}, source="test",
                                       lane="proactive_morning")
        self.assertEqual(result, {"ok": True})

    def test_lane_resolution_defaults(self):
        self.assertEqual(gemini_keys._resolve_lane("whispr-chat", None),
                         "interactive")
        self.assertEqual(gemini_keys._resolve_lane("roof-rover", None),
                         "batch")
        self.assertEqual(gemini_keys._resolve_lane("whispr-chat", "batch"),
                         "batch")
        self.assertEqual(gemini_keys._resolve_lane("x", "bogus"),
                         "interactive")


class TestPaidGuardrails(V2Base):
    def test_ceiling_removes_paid_key(self):
        os.environ["GEMINI_PAID_DAILY_CEILING"] = "2"
        paid = self._add("k-paid", "legacy-env", is_paid=True)
        self._seed_burn(paid["id"], 2)
        calls = self._patch_http(lambda kv: {"ok": True})
        result = gemini_keys.post_json(URL, {"x": 1}, source="test")
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        info = gemini_keys.degradation_info()
        self.assertTrue(info["paid"]["ceiling_hit"])
        self.assertEqual(info["paid"]["calls_today"], 2)

    def test_under_ceiling_paid_serves_as_overflow(self):
        os.environ["GEMINI_PAID_DAILY_CEILING"] = "10"
        paid = self._add("k-paid", "legacy-env", is_paid=True)
        self._seed_burn(paid["id"], 2)
        self._patch_http(lambda kv: {"ok": True})
        result = gemini_keys.post_json(URL, {"x": 1}, source="test")
        self.assertEqual(result, {"ok": True})

    def test_trickle_flag(self):
        os.environ["GEMINI_PAID_TRICKLE_DAILY"] = "1"
        paid = self._add("k-paid", "legacy-env", is_paid=True)
        self._seed_burn(paid["id"], 2)
        state = gemini_keys._paid_state()
        self.assertTrue(state["trickle_exceeded"])
        self.assertFalse(state["ceiling_hit"])


class TestUsageBasedSelection(V2Base):
    def test_most_remaining_budget_first(self):
        os.environ["GEMINI_DAILY_LIMITS"] = '{"%s": 100}' % PRIMARY
        a = self._add("k-a", "A", project="proj-x")
        b = self._add("k-b", "B", project="proj-y")
        self._seed_burn(a["id"], 90)   # 10% left
        self._seed_burn(b["id"], 10)   # 90% left
        cands = gemini_keys.get_candidate_keys()
        self.assertTrue(cands)
        self.assertEqual(cands[0][0], b["id"])


class TestResetAndRollup(V2Base):
    def test_reset_helpers(self):
        secs = gemini_keys.seconds_until_reset()
        self.assertGreater(secs, 0)
        self.assertLessEqual(secs, 86400)
        win = gemini_keys.batch_window()
        self.assertIn("in_preferred_window", win)
        self.assertEqual(win["next_reset_epoch"],
                         gemini_keys.next_reset_epoch())

    def test_admin_rollup_shape(self):
        self._add("k-a", "A", project="proj-x")
        self._add("k-paid", "legacy-env", is_paid=True)
        info = gemini_keys.degradation_info()
        self.assertIn("v2", info)
        self.assertIn("paid", info)
        projects = {p["project"] for p in info["v2"]["projects"]}
        self.assertIn("proj-x", projects)
        self.assertIn("keys_by_state", info["v2"])
        self.assertIn("projection", info["v2"])
        burn = info.get("burn") or {}
        self.assertIn("seconds_to_reset", burn)


if __name__ == "__main__":
    unittest.main()
