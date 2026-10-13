"""Sims game think endpoint: POST /api/sims/think (2026-10-13).

The browser game (godstown.lockeddoor.ai) sends a sim's situation and
gets ONE short in-character line back, generated through the shared
Gemini key manager. The endpoint is token-gated (X-Sims-Token vs env
SIMS_THINK_TOKEN), origin-restricted when an Origin/Referer is present,
rate-limited, and — critically — answers 200 with a fallback line on
ANY model failure so the game never breaks.

Run:  python -m unittest discover -s tests -v   (from the project root,
      with the venv's python)
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app import gemini_keys, sims  # noqa: E402
from app import main as api  # noqa: E402

TOKEN = "test-token-123"
GAME_ORIGIN = "https://godstown.lockeddoor.ai"


def _gemini_ok(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _payload() -> dict:
    return {
        "name": "Bob Newbie",
        "traits": ["Friendly", "Slob"],
        "needs": {"hunger": 30, "energy": 70, "fun": 55,
                  "bladder": 80, "social": 40},
        "situation": "chatting with Bella Goth in the kitchen",
        "partner": "Bella Goth",
        "memories": ["argued with Mortimer", "burned toast"],
        "kind": "chat",
    }


class SimsThinkTest(unittest.TestCase):
    def setUp(self):
        os.environ["SIMS_THINK_TOKEN"] = TOKEN
        self.addCleanup(lambda: os.environ.pop("SIMS_THINK_TOKEN", None))
        sims._rate_hits.clear()
        self.client = TestClient(api.app, raise_server_exceptions=False)

    def _post(self, **kw):
        kw.setdefault("json", _payload())
        return self.client.post("/api/sims/think", **kw)

    # -- auth ------------------------------------------------------------
    def test_missing_token_is_401(self):
        r = self._post()
        self.assertEqual(r.status_code, 401)

    def test_wrong_token_is_401(self):
        r = self._post(headers={"X-Sims-Token": "nope"})
        self.assertEqual(r.status_code, 401)

    # -- happy path (model mocked) ---------------------------------------
    def test_ok_shape_with_mocked_model(self):
        with mock.patch.object(
                gemini_keys, "post_json",
                return_value=_gemini_ok('  "Lovely day for a chat."  ')) as pj:
            r = self._post(headers={"X-Sims-Token": TOKEN})
        self.assertEqual(r.status_code, 200)
        # Surrounding quotes from the model are stripped.
        self.assertEqual(r.json(), {"line": "Lovely day for a chat."})
        self.assertEqual(pj.call_count, 1)
        # Same key-manager path + lite model brain.py uses.
        self.assertIn("gemini-3.1-flash-lite", pj.call_args[0][0])
        self.assertEqual(pj.call_args[1].get("source"), "sims-think")

    def test_response_carries_open_cors_header(self):
        with mock.patch.object(gemini_keys, "post_json",
                               return_value=_gemini_ok("Hey there.")):
            r = self._post(headers={"X-Sims-Token": TOKEN,
                                    "Origin": GAME_ORIGIN})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "*")

    # -- fallback posture --------------------------------------------------
    def test_fallback_when_model_returns_none(self):
        with mock.patch.object(gemini_keys, "post_json", return_value=None):
            r = self._post(headers={"X-Sims-Token": TOKEN})
        self.assertEqual(r.status_code, 200)
        line = r.json().get("line")
        self.assertIsInstance(line, str)
        self.assertTrue(line.strip())

    def test_fallback_when_model_raises(self):
        with mock.patch.object(gemini_keys, "post_json",
                               side_effect=RuntimeError("pool down")):
            r = self._post(headers={"X-Sims-Token": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("line", "").strip())

    # -- origin gate -------------------------------------------------------
    def test_foreign_origin_is_403(self):
        with mock.patch.object(gemini_keys, "post_json",
                               return_value=_gemini_ok("Hi.")):
            r = self._post(headers={"X-Sims-Token": TOKEN,
                                    "Origin": "https://evil.example.com"})
        self.assertEqual(r.status_code, 403)

    def test_game_origin_and_referer_pass(self):
        with mock.patch.object(gemini_keys, "post_json",
                               return_value=_gemini_ok("Hi.")):
            r1 = self._post(headers={"X-Sims-Token": TOKEN,
                                     "Origin": GAME_ORIGIN})
            r2 = self._post(headers={"X-Sims-Token": TOKEN,
                                     "Referer": GAME_ORIGIN + "/play"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

    def test_lookalike_origin_is_403(self):
        with mock.patch.object(gemini_keys, "post_json",
                               return_value=_gemini_ok("Hi.")):
            r = self._post(headers={
                "X-Sims-Token": TOKEN,
                "Origin": "https://godstown.lockeddoor.ai.evil.com"})
        self.assertEqual(r.status_code, 403)

    # -- CORS preflight ----------------------------------------------------
    def test_preflight_answered_for_this_route(self):
        r = self.client.options(
            "/api/sims/think",
            headers={"Origin": GAME_ORIGIN,
                     "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "x-sims-token"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "*")
        self.assertIn("x-sims-token",
                      r.headers.get("access-control-allow-headers", "").lower())

    # -- size + rate limits --------------------------------------------------
    def test_oversized_body_is_413(self):
        big = json.dumps({"situation": "x" * 5000}).encode()
        r = self.client.post("/api/sims/think", content=big,
                             headers={"X-Sims-Token": TOKEN,
                                      "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_rate_limit_kicks_in(self):
        with mock.patch.object(gemini_keys, "post_json",
                               return_value=_gemini_ok("Hi.")):
            for _ in range(sims._RATE_LIMIT):
                r = self._post(headers={"X-Sims-Token": TOKEN})
                self.assertEqual(r.status_code, 200)
            r = self._post(headers={"X-Sims-Token": TOKEN})
        self.assertEqual(r.status_code, 429)


if __name__ == "__main__":
    unittest.main()
