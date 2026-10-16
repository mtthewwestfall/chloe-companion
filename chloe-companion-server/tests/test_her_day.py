"""Whispr Feed (Her Day) media generation — plan-session wiring.

Regression coverage for the 2026-10-11/13 breakage: her_day images
were gated on config.SOGNI_API_KEY (blanked in production), so every
image post silently fell back to a text card. Images must render
through the shared Sogni plan session (assist.stored_sogni_session,
subscription billing — the same path as give/chat renders), never
the dashboard API key.

Run:  python -m unittest tests.test_her_day -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import config, db, her_day, store  # noqa: E402


def _db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    return conn


class GenerateSfwImageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = config.GENERATED_DIR
        config.GENERATED_DIR = self._tmp.name

    def tearDown(self):
        config.GENERATED_DIR = self._old_dir
        self._tmp.cleanup()

    def _call(self):
        return her_day._generate_sfw_image(
            ["Regan"], "", ["regan"], "gym",
            "selfie at the gym", "2026-10-13")

    def test_no_plan_session_falls_back_to_text_card(self):
        with mock.patch.object(her_day, "_plan_session",
                               return_value=None), \
                mock.patch.object(her_day, "_render_image_plan") as render:
            self.assertEqual(self._call(), "")
            render.assert_not_called()

    def test_api_key_session_is_not_usable(self):
        # stored_sogni_session's API-key fallback must not render:
        # plan renders never burn dashboard API credits.
        conn = _db()
        with mock.patch("app.assist.stored_sogni_session",
                        return_value={"token": "k", "refreshToken": "k",
                                      "api_key": True}), \
                mock.patch.object(her_day.db, "connect",
                                  return_value=conn):
            self.assertIsNone(her_day._plan_session())

    def test_plan_session_render_fills_cache(self):
        dest = her_day._herday_image_path(["regan"], "gym", "2026-10-13")
        src = os.path.join(self._tmp.name, "sogni-img-src.jpg")
        with open(src, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"x" * 2048)
        outcome = {"status": "done", "path": src, "detail": "ok"}
        with mock.patch.object(her_day, "_plan_session",
                               return_value={"token": "t",
                                             "refreshToken": "r"}), \
                mock.patch.object(her_day, "_render_image_plan",
                                  return_value=outcome) as render:
            url = self._call()
            self.assertEqual(url, "/generated/" + os.path.basename(dest))
            self.assertTrue(os.path.exists(dest))
            render.assert_called_once()
            # Second call is a cache hit — no second render.
            self.assertEqual(self._call(), url)
            render.assert_called_once()

    def test_failed_render_falls_back(self):
        outcome = {"status": "failed", "path": None, "detail": "boom"}
        with mock.patch.object(her_day, "_plan_session",
                               return_value={"token": "t",
                                             "refreshToken": "r"}), \
                mock.patch.object(her_day, "_render_image_plan",
                                  return_value=outcome):
            self.assertEqual(self._call(), "")

    def test_render_params_are_plan_billed_images(self):
        from app import assist
        params = assist._sogni_plan_params(
            "prompt", None, config.SOGNI_IMAGE_MODEL, kind="image")
        self.assertEqual(params["billingMode"], "subscription")
        self.assertEqual(params["type"], "image")

    def test_image_reference_travels_as_context_image(self):
        # Regression (2026-10-16): image jobs used to carry the
        # reference as "referenceImage", a video-only asset the SDK's
        # image pipeline never reads — the edit model received no
        # context image and Sogni refused every render (4007).
        from app import assist
        params = assist._sogni_plan_params(
            "prompt", "/tmp/her.jpg", config.SOGNI_IMAGE_MODEL,
            kind="image")
        self.assertEqual(params["contextImages"], ["/tmp/her.jpg"])
        self.assertNotIn("referenceImage", params)
        self.assertEqual(params["modelId"], config.SOGNI_IMAGE_MODEL)

    def test_image_without_reference_uses_non_edit_model(self):
        # A prompt-only render can never satisfy an edit model
        # (4007 again), so it must go to the non-edit sibling.
        from app import assist
        params = assist._sogni_plan_params(
            "prompt", None, config.SOGNI_IMAGE_MODEL, kind="image")
        self.assertEqual(params["modelId"], config.SOGNI_IMAGE_TEXT_MODEL)
        self.assertNotIn("contextImages", params)
        self.assertNotIn("referenceImage", params)


class BackfillTest(unittest.TestCase):
    def setUp(self):
        # File-backed DB: the backfill opens/closes its own
        # connections (as in production), so tests must not share one.
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test.db")
        self.conn = db.connect(self.db_path)
        self.conn.executescript(db.SCHEMA)
        self.conn.commit()
        self._old_connect = her_day.db.connect
        real_connect = db.connect
        her_day.db.connect = lambda *a, **k: real_connect(self.db_path)
        self.date_str = datetime.now().strftime("%Y-%m-%d")
        self.girls = [{"id": "regan", "name": "Regan", "avatar_url": ""},
                      {"id": "mara", "name": "Mara", "avatar_url": ""}]

    def tearDown(self):
        her_day.db.connect = self._old_connect
        self.conn.close()
        self._tmp.cleanup()

    def _seed_today(self, user_id="user1"):
        plans = her_day._build_all_plans(self.girls, self.date_str)
        for g in self.girls:
            for (mkey, _g, _p, _pn) in plans[g["id"]]:
                ptype, _scene, _d = her_day._moment_def(mkey)
                store.save_her_day_post(
                    self.conn, user_id, g["id"], post_type=ptype,
                    media_url="", voice_url="", caption=f"cap {mkey}")
        return plans

    def test_backfill_fills_images_counts_voice(self):
        plans = self._seed_today()
        filled = {}

        def fake_gen(names, avatar, participants, moment_key, scene, ds):
            url = f"/generated/herday_{'_'.join(participants)}_{moment_key}.jpg"
            filled[moment_key] = url
            return url

        with mock.patch.object(her_day, "_get_all_girls",
                               return_value=self.girls), \
                mock.patch.object(her_day, "_generate_sfw_image",
                                  side_effect=fake_gen):
            report = her_day.backfill_today_media(self.date_str)

        rows = self.conn.execute(
            "SELECT post_type, media_url, voice_url FROM her_day_posts"
        ).fetchall()
        images = [r for r in rows if r["post_type"] == "image"]
        voices = [r for r in rows if r["post_type"] == "voice"]
        self.assertTrue(images)
        self.assertTrue(all(r["media_url"] for r in images))
        self.assertEqual(report["image_posts_filled"], len(images))
        self.assertEqual(report["image_posts_still_empty"], 0)
        # Voice posts are owner-gated (paid TTS): counted, never filled.
        self.assertEqual(report["voice_posts_without_audio"], len(voices))
        self.assertTrue(all(r["voice_url"] == "" for r in voices))
        total_plan = sum(len(plans[g["id"]]) for g in self.girls)
        self.assertEqual(report["pairs_processed"], 2)
        self.assertEqual(len(rows), total_plan)

        # Idempotent: a second run fills nothing new.
        with mock.patch.object(her_day, "_get_all_girls",
                               return_value=self.girls), \
                mock.patch.object(her_day, "_generate_sfw_image",
                                  side_effect=fake_gen):
            report2 = her_day.backfill_today_media(self.date_str)
        self.assertEqual(report2["image_posts_filled"], 0)

    def test_backfill_skips_unpairable_pair(self):
        # One stray row that doesn't match the plan length: skipped,
        # never guessed at.
        store.save_her_day_post(self.conn, "user9", "regan",
                                post_type="image", media_url="",
                                caption="stray")
        with mock.patch.object(her_day, "_get_all_girls",
                               return_value=self.girls):
            report = her_day.backfill_today_media(self.date_str)
        self.assertEqual(report["pairs_skipped"], 1)
        self.assertEqual(report["image_posts_filled"], 0)


if __name__ == "__main__":
    unittest.main()
