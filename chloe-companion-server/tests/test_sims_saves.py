"""God's Tank cloud-save endpoint: POST/GET /api/sims/save (2026-10-13).

The browser game (godstank.lockeddoor.ai) stores one save object per
player, keyed by a client-generated UUID — no accounts. The endpoints
mirror the /api/sims/think gates (app/sims.py): X-Sims-Token vs env
SIMS_THINK_TOKEN (fails closed when unset), game-origin restriction
when an Origin/Referer is present, per-IP rate limit, and path-scoped
open CORS. Saves land in a sims_saves table in the app's SQLite DB.

Run:  python -m unittest discover -s tests -v   (from the project root,
      with the venv's python)
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, sims_saves  # noqa: E402
from app import main as api  # noqa: E402

TOKEN = "test-token-123"
GAME_ORIGIN = "https://godstank.lockeddoor.ai"
PID = "123e4567-e89b-12d3-a456-426614174000"
PID2 = "223e4567-e89b-12d3-a456-426614174001"
PATH = "/api/sims/save"


def _save() -> dict:
    return {
        "day": 3,
        "coins": 145,
        "her": {"name": "Ivy", "traits": ["Playful", "Friendly", "Neat"]},
        "skills": {"guitar": 42.5, "gardening": 7},
        "flags": {"met_cat": True, "note": None},
        "journal": ["sowed carrots", "café shift — ünïcode ok"],
    }


class SimsSavesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        os.environ["SIMS_THINK_TOKEN"] = TOKEN
        sims_saves._rate_hits.clear()
        self.client = TestClient(api.app, raise_server_exceptions=False)

    def tearDown(self):
        config.DB_PATH = self._real_db_path
        os.environ.pop("SIMS_THINK_TOKEN", None)
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    _UNSET = object()

    def _post(self, pid=PID, save=_UNSET, **kw):
        kw.setdefault("json", {"player_id": pid,
                               "save": _save() if save is self._UNSET else save})
        kw.setdefault("headers", {"X-Sims-Token": TOKEN})
        return self.client.post(PATH, **kw)

    def _get(self, pid=PID, **kw):
        kw.setdefault("params", {"player_id": pid})
        kw.setdefault("headers", {"X-Sims-Token": TOKEN})
        return self.client.get(PATH, **kw)

    # -- round trip ---------------------------------------------------------
    def test_save_then_load_round_trip_identical(self):
        r = self._post()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["updated_at"])
        r = self._get()
        self.assertEqual(r.status_code, 200)
        got = r.json()
        self.assertEqual(got["save"], _save())
        self.assertEqual(got["updated_at"], body["updated_at"])

    def test_saves_are_per_player(self):
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self._post(pid=PID2, save={"day": 9}).status_code, 200)
        self.assertEqual(self._get().json()["save"], _save())
        self.assertEqual(self._get(pid=PID2).json()["save"], {"day": 9})

    def test_unknown_player_is_404_no_save(self):
        r = self._get()
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"detail": "no_save"})

    def test_overwrite_updates_content_and_timestamp(self):
        r1 = self._post(save={"day": 1})
        self.assertEqual(r1.status_code, 200)
        time.sleep(0.02)
        r2 = self._post(save={"day": 2, "extra": [1, 2]})
        self.assertEqual(r2.status_code, 200)
        self.assertGreater(r2.json()["updated_at"], r1.json()["updated_at"])
        got = self._get().json()
        self.assertEqual(got["save"], {"day": 2, "extra": [1, 2]})
        self.assertEqual(got["updated_at"], r2.json()["updated_at"])

    # -- token gate -----------------------------------------------------------
    def test_missing_token_is_401(self):
        r = self.client.post(PATH, json={"player_id": PID, "save": _save()})
        self.assertEqual(r.status_code, 401)
        r = self.client.get(PATH, params={"player_id": PID})
        self.assertEqual(r.status_code, 401)

    def test_wrong_token_is_401(self):
        r = self._post(headers={"X-Sims-Token": "nope"})
        self.assertEqual(r.status_code, 401)
        r = self._get(headers={"X-Sims-Token": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_token_unset_fails_closed_401(self):
        os.environ.pop("SIMS_THINK_TOKEN", None)
        try:
            r = self._post()
            self.assertEqual(r.status_code, 401)
            r = self._get()
            self.assertEqual(r.status_code, 401)
        finally:
            os.environ["SIMS_THINK_TOKEN"] = TOKEN

    # -- origin gate -----------------------------------------------------------
    def test_headerless_with_token_passes(self):
        r = self._post()
        self.assertEqual(r.status_code, 200)
        r = self._get()
        self.assertEqual(r.status_code, 200)

    def test_game_origin_and_referer_pass(self):
        r = self._post(headers={"X-Sims-Token": TOKEN, "Origin": GAME_ORIGIN})
        self.assertEqual(r.status_code, 200)
        r = self._get(headers={"X-Sims-Token": TOKEN,
                               "Referer": GAME_ORIGIN + "/play"})
        self.assertEqual(r.status_code, 200)

    def test_wrong_origin_is_403(self):
        r = self._post(headers={"X-Sims-Token": TOKEN,
                                "Origin": "https://evil.example.com"})
        self.assertEqual(r.status_code, 403)
        r = self._get(headers={"X-Sims-Token": TOKEN,
                               "Origin": "https://evil.example.com"})
        self.assertEqual(r.status_code, 403)

    def test_lookalike_origin_is_403(self):
        r = self._post(headers={
            "X-Sims-Token": TOKEN,
            "Origin": "https://godstank.lockeddoor.ai.evil.com"})
        self.assertEqual(r.status_code, 403)

    # -- CORS ---------------------------------------------------------------
    def test_response_carries_open_cors_header(self):
        r = self._post(headers={"X-Sims-Token": TOKEN, "Origin": GAME_ORIGIN})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "*")

    def test_preflight_answered_for_this_route(self):
        r = self.client.options(
            PATH,
            headers={"Origin": GAME_ORIGIN,
                     "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "x-sims-token"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("access-control-allow-origin"), "*")
        self.assertIn("x-sims-token",
                      r.headers.get("access-control-allow-headers", "").lower())

    # -- validation -----------------------------------------------------------
    def test_malformed_player_id_is_400(self):
        for bad in ("not-a-uuid",
                    "123e4567-e89b-12d3-a456-42661417400",    # 35 chars
                    "123e4567e89b12d3a456426614174000",        # no hyphens
                    "zzze4567-e89b-12d3-a456-426614174000",   # non-hex
                    ""):
            r = self._post(pid=bad)
            self.assertEqual(r.status_code, 400, bad)
            r = self._get(pid=bad)
            self.assertEqual(r.status_code, 400, bad)

    def test_missing_player_id_param_is_400(self):
        r = self.client.get(PATH, headers={"X-Sims-Token": TOKEN})
        self.assertEqual(r.status_code, 400)

    def test_non_object_save_is_400(self):
        for bad_save in ([1, 2, 3], "text", 42, None):
            r = self._post(save=bad_save)
            self.assertEqual(r.status_code, 400, repr(bad_save))

    def test_oversized_save_is_413(self):
        big = {"blob": "x" * (280 * 1024)}
        r = self._post(save=big)
        self.assertEqual(r.status_code, 413)

    # -- rate limit -----------------------------------------------------------
    def test_rate_limit_trips_at_cap(self):
        for _ in range(sims_saves._RATE_LIMIT):
            r = self._post()
            self.assertEqual(r.status_code, 200)
        r = self._post()
        self.assertEqual(r.status_code, 429)


if __name__ == "__main__":
    unittest.main()
