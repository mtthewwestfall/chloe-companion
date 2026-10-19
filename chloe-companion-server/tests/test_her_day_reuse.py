"""Her Day reuse rotation tests (2026-10-19).

Owner direction: feed posts should be reusable every couple of
weeks. When a (user, girl) pair ends the daily flow with no pictured
post for today, her_day.reuse_fill_for_pair re-issues one of the
girl's eligible past pictured moments as a fresh post. A moment is
eligible when its most recent showing was at least
store.REUSE_MIN_AGE_DAYS ago and it is a clean source: an
admin-injected file (any date) or a moment whose latest showing is
on/after store.REUSE_CLEAN_CUTOFF (the clean-photo prompt fix,
PR #79 — older auto renders may carry fabricated story frames and
are never recycled). Longest-overdue moment comes back first.

store.cleanup_expired_her_day preserves the newest pictured row of
each reusable moment past expiry — that row is the rotation's
memory; without it no source could ever reach 14 days old.

Run:  python -m unittest tests.test_her_day_reuse -v
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import db, her_day, store  # noqa: E402

NOW = datetime(2026, 11, 10, 12, 0, 0)
FMT = "%Y-%m-%d %H:%M:%S"


def _ts(dt):
    return dt.strftime(FMT)


class ReuseTest(unittest.TestCase):
    def setUp(self):
        # File-backed DB: the fill opens/closes its own connections
        # (as in production), so tests must not share one.
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test.db")
        db.init_db(self.db_path)
        self._old_connect = her_day.db.connect
        real_connect = db.connect
        her_day.db.connect = lambda *a, **k: real_connect(self.db_path)

    def tearDown(self):
        her_day.db.connect = self._old_connect
        self._tmp.cleanup()

    def _seed(self, user_id, girl_id, media_url, created, caption="cap",
              post_type="image", expires=None):
        conn = db.connect(self.db_path)
        try:
            row = store.save_her_day_post(
                conn, user_id, girl_id, post_type=post_type,
                media_url=media_url, caption=caption)
            store.set_her_day_post_times(
                conn, row["id"], _ts(created),
                _ts(expires or (created + timedelta(days=1))))
            return row["id"]
        finally:
            conn.close()

    def _rows(self, user_id=None, girl_id="regan"):
        conn = db.connect(self.db_path)
        try:
            q = "SELECT * FROM her_day_posts WHERE girl_id = ?"
            params = [girl_id]
            if user_id is not None:
                q += " AND user_id = ?"
                params.append(user_id)
            return [dict(r) for r in conn.execute(q, params).fetchall()]
        finally:
            conn.close()

    def _fill(self, user_id="user1", girl_id="regan", now=NOW):
        buf = io.StringIO()
        with redirect_stdout(buf):
            post = her_day.reuse_fill_for_pair(user_id, girl_id, now=now)
        return post, buf.getvalue()

    # -- eligibility + re-issue -------------------------------------

    def test_inject_moment_reissued_for_empty_pair(self):
        media = "/generated/herday_inject_regan_2026-10-20_boardwalk.webp"
        self._seed(store.HER_DAY_GLOBAL_USER, "regan", media,
                   datetime(2026, 10, 20, 15, 0, 0),
                   caption="boardwalk miles")
        post, out = self._fill()
        self.assertIsNotNone(post)
        self.assertEqual(post["user_id"], "user1")
        self.assertEqual(post["girl_id"], "regan")
        self.assertEqual(post["media_url"], media)
        self.assertEqual(post["caption"], "boardwalk miles")
        # Fresh row: created at the fill's clock, expires +1 day.
        self.assertEqual(post["created_at"], _ts(NOW))
        self.assertEqual(post["expires_at"], _ts(NOW + timedelta(days=1)))
        # The fill is logged with the [her_day] reuse marker.
        self.assertIn("[her_day] reuse", out)
        self.assertIn("regan", out)
        self.assertIn(media, out)

    def test_shown_five_days_ago_not_eligible(self):
        media = "/generated/herday_inject_regan_2026-11-05_recent.webp"
        self._seed(store.HER_DAY_GLOBAL_USER, "regan", media,
                   NOW - timedelta(days=5), caption="too soon")
        conn = db.connect(self.db_path)
        try:
            self.assertIsNone(
                store.find_reusable_her_day_post(conn, "regan", now=NOW))
        finally:
            conn.close()
        post, _out = self._fill()
        self.assertIsNone(post)
        self.assertEqual(self._rows(user_id="user1"), [])

    def test_pre_cleanup_auto_render_not_eligible(self):
        # An auto render from before the 2026-10-19 clean-photo fix,
        # well over 14 days old: still never recycled.
        media = "/generated/herday_regan_2026-10-01_gym.jpg"
        self._seed("someone", "regan", media,
                   datetime(2026, 10, 1, 9, 0, 0), caption="old auto")
        post, _out = self._fill()
        self.assertIsNone(post)
        self.assertEqual(self._rows(user_id="user1"), [])

    def test_post_cutoff_auto_render_is_eligible(self):
        # A clean-era auto render whose latest showing is >=14 days
        # old qualifies without being an inject.
        media = "/generated/herday_regan_2026-10-25_gym.jpg"
        self._seed("someone", "regan", media,
                   datetime(2026, 10, 25, 9, 0, 0), caption="clean auto")
        post, _out = self._fill()
        self.assertIsNotNone(post)
        self.assertEqual(post["media_url"], media)
        self.assertEqual(post["caption"], "clean auto")

    # -- never displaces a fresh post -------------------------------

    def test_pair_with_fresh_pictured_post_gets_no_reuse(self):
        self._seed(store.HER_DAY_GLOBAL_USER, "regan",
                   "/generated/herday_inject_regan_2026-10-20_old.webp",
                   datetime(2026, 10, 20, 15, 0, 0), caption="old")
        self._seed("user1", "regan", "/generated/fresh_today.jpg",
                   NOW - timedelta(hours=1), caption="fresh",
                   expires=NOW + timedelta(days=1))
        post, _out = self._fill()
        self.assertIsNone(post)
        self.assertEqual(len(self._rows(user_id="user1")), 1)

    def test_global_pictured_today_blocks_fill(self):
        self._seed(store.HER_DAY_GLOBAL_USER, "regan",
                   "/generated/herday_inject_regan_2026-10-20_old.webp",
                   datetime(2026, 10, 20, 15, 0, 0), caption="old")
        self._seed(store.HER_DAY_GLOBAL_USER, "regan",
                   "/generated/herday_inject_regan_2026-11-10_new.webp",
                   NOW - timedelta(hours=2), caption="global today",
                   expires=NOW + timedelta(days=1))
        post, _out = self._fill()
        self.assertIsNone(post)
        self.assertEqual(self._rows(user_id="user1"), [])

    # -- rotation order ----------------------------------------------

    def test_least_recently_shown_picked_first(self):
        older = "/generated/herday_inject_regan_2026-10-11_older.webp"
        newer = "/generated/herday_inject_regan_2026-10-21_newer.webp"
        self._seed(store.HER_DAY_GLOBAL_USER, "regan", newer,
                   datetime(2026, 10, 21, 10, 0, 0), caption="newer")
        self._seed(store.HER_DAY_GLOBAL_USER, "regan", older,
                   datetime(2026, 10, 11, 10, 0, 0), caption="older")
        post1, _ = self._fill("user1")
        self.assertEqual(post1["media_url"], older)
        # 'older' was shown today now, so the next pair rotates to
        # the next-longest-overdue moment instead of repeating it.
        post2, _ = self._fill("user2")
        self.assertEqual(post2["media_url"], newer)
        # Both moments shown today: nothing left for a third pair.
        post3, _ = self._fill("user3")
        self.assertIsNone(post3)

    # -- cleanup keeps the rotation's memory --------------------------

    def test_cleanup_preserves_latest_source_row(self):
        # All seeds are expired relative to the real clock; cleanup
        # must keep exactly the newest pictured row per reusable
        # moment and delete everything else.
        keep_inject = self._seed(
            "u1", "regan",
            "/generated/herday_inject_regan_2026-10-05_walk.webp",
            datetime(2026, 10, 5, 10, 0, 0))
        drop_pre_cutoff = self._seed(
            "u1", "regan", "/generated/herday_regan_2026-10-05_gym.jpg",
            datetime(2026, 10, 5, 10, 0, 0))
        keep_clean_auto = self._seed(
            "u1", "regan", "/generated/herday_regan_2026-10-19_gym.jpg",
            datetime(2026, 10, 19, 8, 0, 0),
            expires=datetime(2026, 10, 19, 9, 0, 0))
        drop_superseded = self._seed(
            "u1", "regan",
            "/generated/herday_inject_regan_2026-10-06_duo.webp",
            datetime(2026, 10, 6, 10, 0, 0))
        keep_latest_duo = self._seed(
            "u2", "regan",
            "/generated/herday_inject_regan_2026-10-06_duo.webp",
            datetime(2026, 10, 18, 10, 0, 0),
            expires=datetime(2026, 10, 19, 9, 0, 0))
        drop_text = self._seed(
            "u1", "regan", "", datetime(2026, 10, 5, 10, 0, 0),
            post_type="text")
        conn = db.connect(self.db_path)
        try:
            store.cleanup_expired_her_day(conn)
            alive = {r["id"] for r in conn.execute(
                "SELECT id FROM her_day_posts").fetchall()}
        finally:
            conn.close()
        self.assertIn(keep_inject, alive)
        self.assertIn(keep_clean_auto, alive)
        self.assertIn(keep_latest_duo, alive)
        self.assertNotIn(drop_pre_cutoff, alive)
        self.assertNotIn(drop_superseded, alive)
        self.assertNotIn(drop_text, alive)


if __name__ == "__main__":
    unittest.main()
