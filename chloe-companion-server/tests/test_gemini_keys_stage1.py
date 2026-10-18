"""Tests for Stage 1 of the key-manager overhaul (2026-10-18):
two-quota 429 classification (RPM vs RPD parking), the per-(key,
model, capability) health grid with global dead-model marking, and
dead-key forensics (needs_attention with a plain cause label).

Same base style as tests/test_gemini_keys.py: fresh temp DB per test,
mocked _http_post, no network.
"""
import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config, gemini_keys


def _http_error_body(code, body: str):
    return urllib.error.HTTPError(
        "https://example.test/gen", code, "err", None,
        io.BytesIO(body.encode("utf-8")))


_RPD_BODY = (
    '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": ['
    '{"@type": "type.googleapis.com/google.rpc.QuotaFailure", '
    '"violations": [{"quotaMetric": '
    '"generativelanguage.googleapis.com/generate_content_free_tier_requests", '
    '"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},'
    '{"@type": "type.googleapis.com/google.rpc.RetryInfo", '
    '"retryDelay": "3600s"}]}}')

_RPM_BODY = (
    '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": ['
    '{"@type": "type.googleapis.com/google.rpc.QuotaFailure", '
    '"violations": [{"quotaMetric": '
    '"generativelanguage.googleapis.com/generate_content_free_tier_requests", '
    '"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},'
    '{"@type": "type.googleapis.com/google.rpc.RetryInfo", '
    '"retryDelay": "39s"}]}}')


class Stage1Base(unittest.TestCase):
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
        gemini_keys._probe_validated_until.clear()
        gemini_keys._sessions.clear()
        gemini_keys._cached_tokens_total = 0
        self._env = {}
        for name in ("GEMINI_MAX_ATTEMPTS", "GEMINI_FAST_TIMEOUT_S",
                     "GEMINI_ATTEMPT_BUDGET_S", "GEMINI_HEDGE_AFTER_S",
                     "GEMINI_PER_KEY_RPM", "GEMINI_RESERVE_COUNT",
                     "GEMINI_PROBE_INTERVAL_S", "GEMINI_PROBER",
                     "GEMINI_MODEL_CHAIN", "GEMINI_MODEL_COOLDOWN_S",
                     "GEMINI_QUOTA_CLASSIFY", "GEMINI_HEALTH_GRID",
                     "GEMINI_KEY_FORENSICS"):
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

    def _row(self, key_id):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM gemini_keys WHERE id = ?",
                (key_id,)).fetchone()
        finally:
            conn.close()

    def _patch_http(self, behavior):
        calls = []

        def fake(url, payload, key_value, timeout):
            calls.append((key_value, url))
            return behavior(key_value, url)

        patcher = mock.patch.object(gemini_keys, "_http_post",
                                    side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


class TestQuotaClassification(Stage1Base):
    def test_classify_rpd_by_quota_id(self):
        kind, delay = gemini_keys._classify_quota(_RPD_BODY)
        self.assertEqual(kind, "rpd")
        self.assertEqual(delay, 3600.0)

    def test_classify_rpm_by_quota_id(self):
        kind, delay = gemini_keys._classify_quota(_RPM_BODY)
        self.assertEqual(kind, "rpm")
        self.assertEqual(delay, 39.0)

    def test_classify_long_delay_is_rpd(self):
        kind, _ = gemini_keys._classify_quota(
            '{"error": {"details": [{"retryDelay": "900s"}]}}')
        self.assertEqual(kind, "rpd")

    def test_classify_ambiguous(self):
        kind, delay = gemini_keys._classify_quota(
            '{"error": {"code": 429, "message": "quota exceeded"}}')
        self.assertIsNone(kind)
        self.assertIsNone(delay)

    def test_next_pt_midnight(self):
        now = time.time()
        mid = gemini_keys._next_pt_midnight_epoch(now)
        self.assertGreater(mid, now)
        self.assertLess(mid, now + 25 * 3600)
        dt = datetime.fromtimestamp(mid, tz=timezone.utc)
        # Pacific midnight == 07:00 or 08:00 UTC depending on DST.
        self.assertIn(dt.hour, (7, 8))
        self.assertEqual((dt.minute, dt.second), (0, 0))


class TestTwoQuotaParking(Stage1Base):
    def test_rpd_parks_until_pt_midnight(self):
        k = self._add("rpd-key", "rpd", False)
        before = time.time()
        parked = gemini_keys.report_failure(
            k["id"], "quota", quota_kind="rpd", retry_delay_s=3600.0)
        row = self._row(k["id"])
        self.assertEqual(row["quota_park_kind"], "rpd")
        expected = gemini_keys._next_pt_midnight_epoch(before)
        self.assertAlmostEqual(parked, expected, delta=120)
        until = gemini_keys._parse_ts(row["quota_exhausted_until"])
        self.assertAlmostEqual(until, expected, delta=120)
        # A daily wall is hours long, never the 5-minute ladder step.
        self.assertGreater(until - before, 3600)

    def test_rpm_parks_by_retry_delay_capped(self):
        k = self._add("rpm-key", "rpm", False)
        before = time.time()
        parked = gemini_keys.report_failure(
            k["id"], "quota", quota_kind="rpm", retry_delay_s=45.0)
        self.assertEqual(self._row(k["id"])["quota_park_kind"], "rpm")
        self.assertAlmostEqual(parked - before, 45.0, delta=5)
        k2 = self._add("rpm-key-2", "rpm2", False)
        before2 = time.time()
        parked2 = gemini_keys.report_failure(
            k2["id"], "quota", quota_kind="rpm", retry_delay_s=99999.0)
        self.assertLessEqual(parked2 - before2,
                             gemini_keys._RPM_PARK_CAP_S + 5)

    def test_ambiguous_quota_keeps_ladder(self):
        k = self._add("amb-key", "amb", False)
        before = time.time()
        parked = gemini_keys.report_failure(k["id"], "quota")
        self.assertIsNone(self._row(k["id"])["quota_park_kind"])
        self.assertAlmostEqual(parked - before, 300, delta=30)

    def test_classify_flag_off_restores_ladder(self):
        os.environ["GEMINI_QUOTA_CLASSIFY"] = "0"
        k = self._add("off-key", "off", False)
        before = time.time()
        parked = gemini_keys.report_failure(
            k["id"], "quota", quota_kind="rpd", retry_delay_s=3600.0)
        self.assertAlmostEqual(parked - before, 300, delta=30)
        self.assertIsNone(self._row(k["id"])["quota_park_kind"])

    def test_rpd_via_attempt(self):
        k1 = self._add("wall-key", "w", False)
        self._add("good-key", "g", False)

        def behavior(kv, url):
            if kv == "wall-key":
                raise _http_error_body(429, _RPD_BODY)
            return {"candidates": []}

        self._patch_http(behavior)
        result = gemini_keys.post_json("https://example.com/gen",
                                       {"x": 1})
        self.assertIsNotNone(result)
        row = self._row(k1["id"])
        self.assertEqual(row["quota_park_kind"], "rpd")


class TestForensics(Stage1Base):
    def test_service_account_signature(self):
        reason = gemini_keys._permanent_failure_reason(
            403, "The service account has been deleted or disabled.")
        self.assertIn("service account", reason)

    def test_invalid_key_signature(self):
        reason = gemini_keys._permanent_failure_reason(
            400, '{"status": "INVALID_ARGUMENT", "message": '
                 '"API key not valid. Please pass a valid API key."}')
        self.assertIn("not valid", reason)

    def test_api_not_enabled_signature(self):
        reason = gemini_keys._permanent_failure_reason(
            403, "Gemini API has not been used in project 123 before "
                 "or it is disabled.")
        self.assertIn("not enabled", reason)

    def test_ordinary_403_has_no_signature(self):
        self.assertIsNone(gemini_keys._permanent_failure_reason(
            403, "Forbidden: quota project mismatch"))

    def test_attempt_marks_attention_with_reason(self):
        k = self._add("dead-key", "dead", False)
        self._patch_http(
            lambda kv, url: (_ for _ in ()).throw(_http_error_body(
                403, "error: service account deleted")))
        result = gemini_keys.post_json("https://example.com/gen",
                                       {"x": 1})
        self.assertIsNone(result)
        row = self._row(k["id"])
        self.assertEqual(row["needs_attention"], 1)
        self.assertIn("service account", row["attention_reason"])

    def test_plain_403_attention_without_reason(self):
        k = self._add("plain-key", "plain", False)
        self._patch_http(
            lambda kv, url: (_ for _ in ()).throw(_http_error_body(
                403, "Forbidden")))
        gemini_keys.post_json("https://example.com/gen", {"x": 1})
        row = self._row(k["id"])
        self.assertEqual(row["needs_attention"], 1)
        self.assertIsNone(row["attention_reason"])

    def test_forensics_flag_off(self):
        os.environ["GEMINI_KEY_FORENSICS"] = "0"
        k = self._add("off-dead", "offd", False)
        gemini_keys.report_failure(
            k["id"], "payment",
            attention_reason="Google service account deleted or disabled")
        self.assertIsNone(self._row(k["id"])["attention_reason"])

    def test_set_active_clears_reason(self):
        k = self._add("re-key", "re", False)
        gemini_keys.report_failure(
            k["id"], "payment", attention_reason="API key not valid")
        self.assertIsNotNone(self._row(k["id"])["attention_reason"])
        gemini_keys.set_active(k["id"], True)
        row = self._row(k["id"])
        self.assertIsNone(row["attention_reason"])
        self.assertEqual(row["needs_attention"], 0)


class TestHealthGrid(Stage1Base):
    _SEARCH = {"contents": [{"parts": [{"text": "hi"}]}],
               "tools": [{"google_search": {}}]}
    _TEXT = {"contents": [{"parts": [{"text": "hi"}]}]}

    def test_capability_detection(self):
        url = ("https://generativelanguage.googleapis.com/v1beta/"
               "models/gemini-3.1-flash-lite:generateContent")
        self.assertEqual(
            gemini_keys._request_capability(url, self._SEARCH),
            "grounding")
        self.assertEqual(
            gemini_keys._request_capability(url, self._TEXT), "text")
        img_url = ("https://generativelanguage.googleapis.com/v1beta/"
                   "models/gemini-2.5-flash-image:generateContent")
        self.assertEqual(
            gemini_keys._request_capability(img_url, self._TEXT),
            "image")
        tts_url = ("https://generativelanguage.googleapis.com/v1beta/"
                   "models/gemini-2.5-flash-preview-tts:generateContent")
        self.assertEqual(
            gemini_keys._request_capability(tts_url, self._TEXT), "tts")

    def test_dead_grounding_cell_blocks_only_grounding(self):
        k1 = self._add("g-key", "g1", False)
        self._add("other-key", "g2", False)
        gemini_keys._learn_cell_conn(k1["id"], None, "grounding", "dead")
        served = []

        def behavior(kv, url):
            served.append(kv)
            return {"candidates": []}

        self._patch_http(behavior)
        url = ("https://generativelanguage.googleapis.com/v1beta/"
               "models/gemini-3.1-flash-lite:generateContent")
        result = gemini_keys.post_json(url, dict(self._SEARCH))
        self.assertIsNotNone(result)
        self.assertNotIn("g-key", served)  # grounding skips the dead cell
        served.clear()
        result = gemini_keys.post_json(url, dict(self._TEXT))
        self.assertIsNotNone(result)
        # Text may use either key; the point is the dead grounding cell
        # did not remove the key from text service entirely. Run once
        # more with only the grounded-dead key present to prove it.
        conn = gemini_keys.db.connect()
        try:
            gemini_keys._ensure_table(conn)
            conn.execute("DELETE FROM gemini_keys WHERE label = 'g2'")
            conn.commit()
        finally:
            conn.close()
        served.clear()
        result = gemini_keys.post_json(url, dict(self._TEXT))
        self.assertIsNotNone(result)
        self.assertIn("g-key", served)

    def test_cell_cooling_is_per_model(self):
        k1 = self._add("m-key", "m1", False)
        gemini_keys._learn_cell_conn(
            k1["id"], "model-a", "text", "cooling", time.time() + 600)
        self._patch_http(lambda kv, url: {"candidates": []})
        blocked = gemini_keys.post_json(
            "https://x.test/models/model-a:generateContent",
            dict(self._TEXT))
        self.assertIsNone(blocked)  # only key, its model-a cell cools
        ok = gemini_keys.post_json(
            "https://x.test/models/model-b:generateContent",
            dict(self._TEXT))
        self.assertIsNotNone(ok)  # other model unaffected

    def test_grounding_payment_failure_kills_cell(self):
        k1 = self._add("sg-key", "sg", False)
        self._patch_http(
            lambda kv, url: (_ for _ in ()).throw(_http_error_body(
                403, "Forbidden")))
        url = ("https://generativelanguage.googleapis.com/v1beta/"
               "models/gemini-3.1-flash-lite:generateContent")
        gemini_keys.post_json(url, dict(self._SEARCH))
        blocked = gemini_keys._cell_blocked_ids(
            "gemini-3.1-flash-lite", "grounding")
        self.assertIn(k1["id"], blocked)

    def test_dead_model_skipped_globally(self):
        primary, fallback = gemini_keys.model_chain()
        self._add("dm-key", "dm", False)

        def behavior(kv, url):
            if primary in url:
                raise _http_error_body(
                    404, "This model models/%s is no longer available "
                         "to new users" % primary)
            return {"candidates": []}

        calls = self._patch_http(behavior)
        result = gemini_keys.post_json(
            gemini_keys.model_url(primary), dict(self._TEXT))
        self.assertIsNotNone(result)
        self.assertTrue(gemini_keys._model_is_dead(primary))
        self.assertEqual(gemini_keys.current_model(), fallback)
        # Next call routes straight to the fallback model.
        calls.clear()
        result = gemini_keys.post_json(
            gemini_keys.model_url(primary), dict(self._TEXT))
        self.assertIsNotNone(result)
        self.assertTrue(all(fallback in u for _kv, u in calls))

    def test_degradation_info_stage1_fields(self):
        k = self._add("info-key", "info", False)
        gemini_keys.report_failure(
            k["id"], "quota", quota_kind="rpd", retry_delay_s=10.0)
        gemini_keys.report_failure(
            k["id"], "payment", attention_reason="API key not valid")
        info = gemini_keys.degradation_info()
        self.assertEqual(info["quota_parks"].get("rpd"), 1)
        self.assertEqual(info["attention_reasons"].get(str(k["id"])),
                         "API key not valid")
        self.assertIn("dead_models", info)
        self.assertIn("health_cells", info)
        pub = gemini_keys.list_keys()[0]
        self.assertEqual(pub["attention_reason"], "API key not valid")


if __name__ == "__main__":
    unittest.main()
