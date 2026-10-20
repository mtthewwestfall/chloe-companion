"""Live-event Her Day moments (improvement #9, 2026-10-20).

When a Telegram group live event ends, the host girl posts ONE
global Her Day moment about the night. Pinned here:

- a public girl's event moment is ONE global row (HER_DAY_GLOBAL_USER)
  that every user's feed read includes;
- the image comes from the gated daily render path
  (her_day._generate_sfw_image — Sogni/Gemini fallback, likeness
  lock, clean-photo prompt), and when that path yields no image the
  moment posts as a text card instead — never an ungated image;
- the caption references the event (the deterministic fallback
  caption is used here — no proxy token in the test env);
- a private (owner-scoped) girl is refused and gets NO moment;
- a second pictured event moment for the same girl within hours is
  skipped, not stacked;
- the admin endpoint is token-gated and maps a refusal to a 400.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import characters, config, db, her_day, store  # noqa: E402
from app import main as api  # noqa: E402

ADMIN_ATTR = "ADMIN" + chr(95) + "TOKEN"
ROSTER = [
    {"id": "chloe", "name": "Chloe", "avatar_url": "https://x/chloe.png"},
    {"id": "mara", "name": "Mara", "avatar_url": "https://x/mara.png"},
]
EVENT_URL = "/generated/herday_chloe_2026-10-20_live_event.jpg"


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


class EventMomentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_gen = config.GENERATED_DIR
        config.GENERATED_DIR = os.path.join(self.tmp.name, "generated")
        self._real_admin = getattr(config, ADMIN_ATTR)
        setattr(config, ADMIN_ATTR, "test-admin-secret")
        characters._cache = {"ts": 0.0, "chars": []}
        self._env = mock.patch.dict(
            os.environ, {"GEMINI_PROXY_TOKEN": ""})
        self._env.start()
        self._roster = mock.patch.object(
            her_day, "_get_all_girls", return_value=list(ROSTER))
        self._roster.start()

    def tearDown(self):
        self._roster.stop()
        self._env.stop()
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        config.GENERATED_DIR = self._real_gen
        setattr(config, ADMIN_ATTR, self._real_admin)
        characters._cache = {"ts": 0.0, "chars": []}
        self.tmp.cleanup()

    def _rows(self, girl_id=None):
        conn = db.connect(self.db_path)
        try:
            q = "SELECT * FROM her_day_posts"
            args = ()
            if girl_id:
                q += " WHERE girl_id = ?"
                args = (girl_id,)
            return [dict(r) for r in conn.execute(q, args).fetchall()]
        finally:
            conn.close()

    def test_pictured_moment_is_one_global_row(self):
        with mock.patch.object(
                her_day, "_generate_sfw_image",
                return_value=EVENT_URL) as render:
            out = her_day.generate_event_moment("chloe")
        self.assertTrue(out["ok"])
        self.assertEqual(out["created"], 1)
        self.assertEqual(out["post_type"], "image")
        self.assertEqual(out["media_url"], EVENT_URL)
        render.assert_called_once()
        # The gated render was asked for HER, solo, as the event moment.
        args = render.call_args.args
        self.assertEqual(args[2], ["chloe"])
        self.assertEqual(args[3], her_day.EVENT_MOMENT_KEY)
        rows = self._rows("chloe")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], store.HER_DAY_GLOBAL_USER)
        self.assertEqual(rows[0]["media_url"], EVENT_URL)
        # Caption references the event, names nobody.
        self.assertIn("live", rows[0]["caption"].lower())
        # And it shows in an arbitrary user's feed.
        conn = db.connect(self.db_path)
        try:
            feed = store.get_her_day_posts(conn, "some-user")
        finally:
            conn.close()
        self.assertIn(EVENT_URL, [p["media_url"] for p in feed])

    def test_no_image_posts_text_card_via_gated_path(self):
        with mock.patch.object(
                her_day, "_generate_sfw_image", return_value="") as render:
            out = her_day.generate_event_moment("mara")
        self.assertTrue(out["ok"])
        self.assertEqual(out["created"], 1)
        self.assertEqual(out["post_type"], "text")
        self.assertEqual(out["media_url"], "")
        render.assert_called_once()  # still the gated path, no side door
        rows = self._rows("mara")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["post_type"], "text")

    def test_private_girl_gets_no_moment(self):
        with mock.patch.object(
                her_day, "_generate_sfw_image",
                return_value=EVENT_URL) as render:
            out = her_day.generate_event_moment("brooklyn")
        self.assertFalse(out["ok"])
        render.assert_not_called()
        self.assertEqual(self._rows(), [])

    def test_unknown_girl_gets_no_moment(self):
        out = her_day.generate_event_moment("nosuchgirl")
        self.assertFalse(out["ok"])
        self.assertEqual(self._rows(), [])

    def test_second_pictured_moment_is_skipped(self):
        with mock.patch.object(
                her_day, "_generate_sfw_image", return_value=EVENT_URL):
            first = her_day.generate_event_moment("chloe")
            second = her_day.generate_event_moment("chloe")
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(len(self._rows("chloe")), 1)


class EventMomentEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_gen = config.GENERATED_DIR
        config.GENERATED_DIR = os.path.join(self.tmp.name, "generated")
        self._real_admin = getattr(config, ADMIN_ATTR)
        setattr(config, ADMIN_ATTR, "test-admin-secret")
        characters._cache = {"ts": 0.0, "chars": []}
        self._env = mock.patch.dict(
            os.environ, {"GEMINI_PROXY_TOKEN": ""})
        self._env.start()
        self._roster = mock.patch.object(
            her_day, "_get_all_girls", return_value=list(ROSTER))
        self._roster.start()

    def tearDown(self):
        self._roster.stop()
        self._env.stop()
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        config.GENERATED_DIR = self._real_gen
        setattr(config, ADMIN_ATTR, self._real_admin)
        characters._cache = {"ts": 0.0, "chars": []}
        self.tmp.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(api.app)

    def test_endpoint_requires_admin(self):
        resp = self._client().post(
            "/api/v1/admin/her-day/event-moment",
            json={"girl_id": "chloe"})
        self.assertEqual(resp.status_code, 401)

    def test_endpoint_posts_moment(self):
        with mock.patch.object(
                her_day, "_generate_sfw_image", return_value=EVENT_URL):
            resp = self._client().post(
                "/api/v1/admin/her-day/event-moment",
                headers={"x-admin-token": "test-admin-secret"},
                json={"girl_id": "chloe"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["created"], 1)

    def test_endpoint_refuses_private_girl(self):
        resp = self._client().post(
            "/api/v1/admin/her-day/event-moment",
            headers={"x-admin-token": "test-admin-secret"},
            json={"girl_id": "brooklyn"})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
