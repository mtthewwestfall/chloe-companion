"""Public Today page (GET /today) — server-rendered Her Day feed.

Covers the 2026-10-15 feature as amended by the owner's display rules
(2026-10-17): the page lists today's shared Her Day posts newest-first,
but only pictured posts appear — at most one per character per day —
and picture-less posts never render (no text cards, no placeholders).
Captions are escaped, and the page carries one CTA into the signup
flow (the app root, whose auth sheet opens on arrival for logged-out
visitors).

Run:  python -m unittest tests.test_today_page -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import db, store  # noqa: E402
from app import main as api  # noqa: E402

GIRLS = [
    {"id": "regan", "name": "Regan", "avatar_url": ""},
    {"id": "mara", "name": "Mara", "avatar_url": "https://x.test/mara.jpg"},
]


def _ts(minutes_ago: int) -> str:
    return (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M:%S")


class TodayPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._pub = mock.patch.object(
            api.characters, "public_list", return_value=GIRLS)
        self._pub.start()

    def tearDown(self):
        self._pub.stop()
        api._conn = self._real_conn
        self.tmp.cleanup()

    def _post(self, girl_id, post_type, caption, minutes_ago,
              media_url="", voice_url=""):
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO her_day_posts (user_id, girl_id, post_type,"
                " media_url, voice_url, caption, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("test-user", girl_id, post_type, media_url,
                 voice_url, caption, _ts(minutes_ago), _ts(-60 * 24)))
            conn.commit()
        finally:
            conn.close()

    def _body(self) -> str:
        resp = api.today_page()
        self.assertEqual(resp.status_code, 200)
        return resp.body.decode("utf-8")

    def test_lists_posts_newest_first(self):
        self._post("regan", "image", "older post about coffee", 90,
                   media_url="/generated/herday_regan.jpg")
        self._post("mara", "image", "newer post about the gym", 5,
                   media_url="/generated/herday_mara.jpg")
        body = self._body()
        self.assertLess(body.index("newer post about the gym"),
                        body.index("older post about coffee"))
        self.assertIn("Regan", body)
        self.assertIn("Mara", body)

    def test_pictureless_posts_do_not_render(self):
        # Owner display rules (2026-10-17): no picture, no public post —
        # text posts and failed renders (empty media_url) stay hidden,
        # with no text card or placeholder in their place.
        self._post("regan", "text", "just a thought today", 10)
        self._post("regan", "image", "bedtime soon", 20)  # empty media_url
        body = self._body()
        self.assertNotIn("just a thought today", body)
        self.assertNotIn("bedtime soon", body)
        self.assertNotIn('<article class="today-card today-textcard">', body)
        self.assertIn("Nothing yet today", body)

    def test_media_renders_and_voice_only_post_is_hidden(self):
        self._post("mara", "image", "gym done", 5,
                   media_url="/generated/herday_mara_gym.jpg")
        self._post("regan", "voice", "rant incoming", 15,
                   voice_url="/generated/voice/rant.mp3")
        body = self._body()
        self.assertIn('<img class="today-media" src="/generated/herday_mara_gym.jpg"', body)
        # A voice-only post has no picture, so it never appears publicly.
        self.assertNotIn("rant incoming", body)
        self.assertNotIn("<audio", body)
        # Mara has an avatar url -> avatar img renders too.
        self.assertIn('<img class="today-avatar" src="https://x.test/mara.jpg"', body)

    def test_one_post_per_girl_per_day(self):
        self._post("mara", "image", "mara later pic", 10,
                   media_url="/generated/herday_mara_late.jpg")
        self._post("mara", "image", "mara earliest pic", 90,
                   media_url="/generated/herday_mara_early.jpg")
        body = self._body()
        self.assertIn("mara earliest pic", body)
        self.assertNotIn("mara later pic", body)

    def test_cta_targets_signup_root(self):
        self._post("regan", "text", "hi", 5)
        body = self._body()
        self.assertIn('<a class="today-cta" href="/">', body)
        self.assertIn("50 free messages", body)

    def test_caption_is_escaped(self):
        self._post("regan", "image", "<script>alert(1)</script>", 5,
                   media_url="/generated/herday_regan.jpg")
        body = self._body()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>alert(1)</script>", body)

    def test_empty_feed_renders_empty_state(self):
        body = self._body()
        self.assertIn("Nothing yet today", body)
        self.assertIn('<a class="today-cta" href="/">', body)


if __name__ == "__main__":
    unittest.main()
