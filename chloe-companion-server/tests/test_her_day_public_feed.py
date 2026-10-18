"""Public Her Day feed display rules (owner direction, 2026-10-17).

The public surfaces (GET /api/v1/her-day/public, GET /today) show only
posts that carry a picture, and at most ONE post per character per
day — her earliest pictured post. Picture-less posts stay in the
table untouched; they just never surface publicly. These tests pin
store.get_public_her_day_posts, the one function both surfaces read.

Run:  python -m unittest tests.test_her_day_public_feed -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import db, store  # noqa: E402


def _db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(db.SCHEMA)
    return conn


def _ts(minutes_ago: int) -> str:
    return (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M:%S")


class PublicHerDayFeedTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def tearDown(self):
        self.conn.close()

    def _post(self, girl_id, caption, minutes_ago, media_url="",
              voice_url="", post_type="image", user_id="test-user"):
        self.conn.execute(
            "INSERT INTO her_day_posts (user_id, girl_id, post_type,"
            " media_url, voice_url, caption, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, girl_id, post_type, media_url, voice_url, caption,
             _ts(minutes_ago), _ts(-60 * 24)))
        self.conn.commit()

    def _feed(self, limit=24):
        return store.get_public_her_day_posts(self.conn, limit=limit)

    def test_pictureless_posts_never_appear(self):
        self._post("mara", "text thought", 10, post_type="text")
        self._post("mara", "image post, render failed", 20)  # empty media
        self._post("chloe", "voice only", 5, post_type="voice",
                   voice_url="/generated/voice/note.mp3")
        self.assertEqual(self._feed(), [])

    def test_girl_with_only_pictureless_posts_disappears(self):
        self._post("sabrina-2", "night out", 30, post_type="text")
        self._post("mara", "gym pic", 10,
                   media_url="/generated/herday_mara_gym.jpg")
        feed = self._feed()
        self.assertEqual([p["girl_id"] for p in feed], ["mara"])

    def test_one_per_girl_per_day_keeps_earliest_pictured(self):
        self._post("mara", "later pic", 10,
                   media_url="/generated/herday_mara_late.jpg")
        self._post("mara", "earliest pic", 90,
                   media_url="/generated/herday_mara_early.jpg")
        self._post("mara", "middle pic", 45,
                   media_url="/generated/herday_mara_mid.jpg")
        feed = self._feed()
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["caption"], "earliest pic")

    def test_pictureless_earlier_post_does_not_claim_the_day(self):
        # A picture-less post earlier in her day must not block her
        # pictured post from being the day's one shown post.
        self._post("mara", "early text", 120, post_type="text")
        self._post("mara", "her pic", 60,
                   media_url="/generated/herday_mara.jpg")
        feed = self._feed()
        self.assertEqual([p["caption"] for p in feed], ["her pic"])

    def test_same_girl_two_days_shows_one_each(self):
        self._post("mara", "today pic", 30,
                   media_url="/generated/herday_mara_today.jpg")
        self._post("mara", "yesterday pic", 60 * 25,
                   media_url="/generated/herday_mara_yday.jpg")
        feed = self._feed()
        self.assertEqual([p["caption"] for p in feed],
                         ["today pic", "yesterday pic"])

    def test_newest_first_across_girls_and_limit_applies(self):
        self._post("mara", "mara pic", 50,
                   media_url="/generated/herday_mara.jpg")
        self._post("chloe", "chloe pic", 5,
                   media_url="/generated/herday_chloe.jpg")
        self._post("regan", "regan pic", 20,
                   media_url="/generated/herday_regan.jpg")
        feed = self._feed()
        self.assertEqual([p["girl_id"] for p in feed],
                         ["chloe", "regan", "mara"])
        self.assertEqual([p["girl_id"] for p in self._feed(limit=2)],
                         ["chloe", "regan"])

    def test_fanout_duplicate_rows_collapse_to_one(self):
        # The same moment is stored once per user; the public feed
        # must still show it once.
        for uid in ("user-a", "user-b", "user-c"):
            self._post("mara", "same moment", 30,
                       media_url="/generated/herday_mara.jpg",
                       user_id=uid)
        feed = self._feed()
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["caption"], "same moment")

    def test_expired_pictured_post_does_not_appear(self):
        self.conn.execute(
            "INSERT INTO her_day_posts (user_id, girl_id, post_type,"
            " media_url, voice_url, caption, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("test-user", "mara", "image", "/generated/herday_mara.jpg",
             "", "expired pic", _ts(60 * 30), _ts(60)))
        self.conn.commit()
        self.assertEqual(self._feed(), [])

    def test_video_post_with_media_counts_as_pictured(self):
        self._post("mara", "clip", 10, post_type="video",
                   media_url="/generated/herday_mara.mp4")
        feed = self._feed()
        self.assertEqual([p["caption"] for p in feed], ["clip"])


if __name__ == "__main__":
    unittest.main()
