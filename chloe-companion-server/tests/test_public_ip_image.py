"""Public IP package-image tests (2026-10-19).

The Public IP approval queue used to be text-only. Now a package's
image brief renders in the tab (POST /api/v1/admin/public-ip/
generate-image, via public_ip.generate_package_image ->
assist.gemini_image_render seeded with her avatar) and the render is
stored on the package record; an APPROVED package with an image can
post to the feed (POST /api/v1/admin/public-ip/post-to-feed) through
the existing Her Day inject endpoint as one global post.

Covered: admin auth gating on both endpoints, no-package and
no-brief clean 400s, successful render stores media_url + file,
render failure is a 502 that preserves the previous image, feed
posting is gated on approved + imaged (inject never fires otherwise),
and a real inject lands one global Her Day row with the package
caption and flips the package to published.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException  # noqa: E402

from app import assist, characters, config, db, media_gen, store  # noqa: E402
from app import main as api  # noqa: E402

ADMIN_ATTR = "ADMIN" + chr(95) + "TOKEN"
DATE = "2026-10-19"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


class PublicIpImageTest(unittest.TestCase):
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
        os.makedirs(config.GENERATED_DIR, exist_ok=True)
        self._real_admin = getattr(config, ADMIN_ATTR)
        setattr(config, ADMIN_ATTR, "test-admin-secret")
        # Hermetic roster: one Chloe with a (mocked-away) avatar URL.
        characters._cache = {
            "ts": time.time(), "local": [],
            "chars": [{"id": "chloe", "name": "Chloe",
                       "avatar_url": "https://example.invalid/chloe.jpg",
                       "persona": "warm and playful", "blurb": "",
                       "companion_voice": "liora", "sort_order": 0}]}
        # No network in tests: the reference fetch is stubbed out and
        # the render helper is replaced per-test.
        self._real_fetch_ref = media_gen._fetch_reference
        media_gen._fetch_reference = lambda url: None  # noqa: E731
        self._real_render = assist.gemini_image_render
        self._real_inject = api.admin_her_day_inject
        self.inject_calls = []

    def tearDown(self):
        api._conn = self._real_conn
        api.admin_her_day_inject = self._real_inject
        config.DB_PATH = self._real_db_path
        config.GENERATED_DIR = self._real_gen
        setattr(config, ADMIN_ATTR, self._real_admin)
        characters._cache = {"ts": 0.0, "chars": []}
        media_gen._fetch_reference = self._real_fetch_ref
        assist.gemini_image_render = self._real_render
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------
    def _req(self):
        return _Req({"x-admin-token": "test-admin-secret"})

    def _seed_package(self, status="pending", brief="golden-hour "
                      "boardwalk walk, laughing", image_url=""):
        pkg = {"girl_id": "chloe", "date": DATE,
               "feed_caption": "boardwalk miles, zero regrets",
               "image_brief": brief, "story_frames": [],
               "video": {}, "quote": "thinking of you",
               "source": "llm"}
        if image_url:
            pkg["image_url"] = image_url
        conn = db.connect(self.db_path)
        try:
            store.save_public_package(conn, "chloe", DATE, pkg)
            if status != "pending":
                store.set_public_package_status(conn, "chloe", DATE,
                                                status)
        finally:
            conn.close()

    def _package(self):
        conn = db.connect(self.db_path)
        try:
            return store.get_public_package(conn, "chloe", DATE)
        finally:
            conn.close()

    def _render_ok(self, prompt, ref_path=None):
        fd, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(_PNG)
        return {"status": "done", "path": path, "detail": "ok"}

    def _gen(self, req=None):
        body = api.PublicIpImageIn(girl_id="chloe", date=DATE)
        return api.admin_public_ip_generate_image(body, req or self._req())

    def _post(self, req=None):
        body = api.PublicIpImageIn(girl_id="chloe", date=DATE)
        return api.admin_public_ip_post_to_feed(body, req or self._req())

    def _mock_inject(self):
        def fake(body, request):
            self.inject_calls.append(body)
            return {"ok": True, "created": 1,
                    "media_url": "/generated/herday_inject_fake.png",
                    "users": "all"}
        api.admin_her_day_inject = fake

    # -- auth ---------------------------------------------------------
    def test_generate_image_requires_admin(self):
        self._seed_package()
        with self.assertRaises(HTTPException) as cm:
            self._gen(_Req({}))
        self.assertEqual(cm.exception.status_code, 401)

    def test_post_to_feed_requires_admin(self):
        self._seed_package(status="approved", image_url="/generated/x.png")
        with self.assertRaises(HTTPException) as cm:
            self._post(_Req({"x-admin-token": "wrong"}))
        self.assertEqual(cm.exception.status_code, 401)

    # -- generate-image ----------------------------------------------
    def test_generate_image_no_package_is_clean_400(self):
        assist.gemini_image_render = self._render_ok
        with self.assertRaises(HTTPException) as cm:
            self._gen()
        self.assertEqual(cm.exception.status_code, 400)

    def test_generate_image_no_brief_is_clean_400(self):
        self._seed_package(brief="")
        called = []
        assist.gemini_image_render = \
            lambda p, r=None: called.append(p) or self._render_ok(p, r)
        with self.assertRaises(HTTPException) as cm:
            self._gen()
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(called, [])  # renderer never invoked

    def test_generate_image_success_stores_media_url(self):
        self._seed_package(status="approved")
        seen = {}

        def render(prompt, ref_path=None):
            seen["prompt"] = prompt
            return self._render_ok(prompt, ref_path)
        assist.gemini_image_render = render
        out = self._gen()
        self.assertTrue(out["ok"])
        self.assertTrue(out["media_url"].startswith("/generated/publicip_chloe_"))
        rec = self._package()
        self.assertEqual(rec["package"]["image_url"], out["media_url"])
        self.assertEqual(rec["status"], "approved")  # status untouched
        dest = os.path.join(config.GENERATED_DIR,
                            os.path.basename(out["media_url"]))
        self.assertTrue(os.path.isfile(dest))
        # The brief drives the prompt; no photoreal forcing (anime cast).
        self.assertIn("boardwalk walk", seen["prompt"])
        self.assertNotIn("photorealistic", seen["prompt"].lower())

    def test_generate_image_failure_preserves_previous_image(self):
        self._seed_package(image_url="/generated/publicip_old.png")
        assist.gemini_image_render = \
            lambda p, r=None: {"status": "pool_exhausted",
                               "detail": "gemini pool returned nothing"}
        with self.assertRaises(HTTPException) as cm:
            self._gen()
        self.assertEqual(cm.exception.status_code, 502)
        self.assertEqual(self._package()["package"]["image_url"],
                         "/generated/publicip_old.png")

    def test_generate_image_replaces_previous_image(self):
        self._seed_package(image_url="/generated/publicip_old.png")
        assist.gemini_image_render = self._render_ok
        out = self._gen()
        self.assertNotEqual(out["media_url"], "/generated/publicip_old.png")
        self.assertEqual(self._package()["package"]["image_url"],
                         out["media_url"])

    # -- post-to-feed gating -------------------------------------------
    def test_post_blocked_when_not_approved(self):
        self._seed_package(status="pending",
                           image_url="/generated/publicip_chloe_x.png")
        self._mock_inject()
        with self.assertRaises(HTTPException) as cm:
            self._post()
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(self.inject_calls, [])

    def test_post_blocked_without_image(self):
        self._seed_package(status="approved")
        self._mock_inject()
        with self.assertRaises(HTTPException) as cm:
            self._post()
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(self.inject_calls, [])

    def test_post_blocked_when_image_file_missing(self):
        self._seed_package(status="approved",
                           image_url="/generated/publicip_chloe_gone.png")
        self._mock_inject()
        with self.assertRaises(HTTPException) as cm:
            self._post()
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(self.inject_calls, [])

    def test_post_approved_with_image_calls_inject_globally(self):
        self._seed_package(status="approved",
                           image_url="/generated/publicip_chloe_x.png")
        with open(os.path.join(config.GENERATED_DIR,
                               "publicip_chloe_x.png"), "wb") as f:
            f.write(_PNG)
        self._mock_inject()
        out = self._post()
        self.assertTrue(out["ok"])
        self.assertEqual(len(self.inject_calls), 1)
        body = self.inject_calls[0]
        self.assertEqual(body.girl_id, "chloe")
        self.assertEqual(body.user_id, "")  # blank = one global post
        self.assertEqual(body.caption, "boardwalk miles, zero regrets")
        self.assertEqual(base64.b64decode(body.image_b64), _PNG)
        self.assertEqual(self._package()["status"], "published")

    def test_post_end_to_end_lands_one_global_feed_row(self):
        self._seed_package(status="approved",
                           image_url="/generated/publicip_chloe_x.png")
        with open(os.path.join(config.GENERATED_DIR,
                               "publicip_chloe_x.png"), "wb") as f:
            f.write(_PNG)
        out = self._post()  # real inject endpoint
        self.assertTrue(out["ok"])
        conn = db.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT user_id, caption FROM her_day_posts "
                "WHERE girl_id = 'chloe'").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], store.HER_DAY_GLOBAL_USER)
        self.assertEqual(rows[0][1], "boardwalk miles, zero regrets")


if __name__ == "__main__":
    unittest.main()
