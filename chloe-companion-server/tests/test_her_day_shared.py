"""Her Day shared day-set tests (2026-10-09).

The daily set is generated ONCE per girl and shared by every user (stored
under store.HER_DAY_SHARED_USER); feeds lazy-attach to it and per-user
state is only lightweight her_day_seen rows. Covers: single shared
generation, no per-user/probe writes, per-user seen isolation, legacy
per-user row fallback, public feed preference, seen cleanup, on-demand
generate contract, and that voice is never invented (stub stays silent).

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import config, db, her_day, store  # noqa: E402

GIRLS = [
    {"id": g, "name": g.title(), "avatar_url": ""}
    for g in ("chloe", "mara", "isabella", "regan", "jake", "eli")
]
SHARED = store.HER_DAY_SHARED_USER


class SharedHerDayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_get_girls = her_day._get_all_girls
        her_day._get_all_girls = lambda: [dict(g) for g in GIRLS]
        self._real_image = her_day._generate_sfw_image
        self.image_news = []
        self._seen_imgs = set()

        def fake_image(names, avatar_url, participant_ids, moment_key,
                       scene, date_str):
            key = ("_".join(sorted(participant_ids)), moment_key, date_str)
            if key in self._seen_imgs:  # mirrors the on-disk cache
                return "/generated/cached.jpg"
            self._seen_imgs.add(key)
            self.image_news.append(key)
            return "/generated/new.jpg"

        her_day._generate_sfw_image = fake_image
        her_day._caption_cache.clear()

    def tearDown(self):
        config.DB_PATH = self._real_db_path
        her_day._get_all_girls = self._real_get_girls
        her_day._generate_sfw_image = self._real_image
        her_day._caption_cache.clear()
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _mk_users(self, *uids):
        conn = db.connect(self.db_path)
        try:
            for uid in uids:
                conn.execute(
                    "INSERT INTO users (user_id, name, email)"
                    " VALUES (?, ?, ?)",
                    (uid, uid, f"{uid}@x.test"))
            conn.commit()
        finally:
            conn.close()

    def _rows(self):
        conn = db.connect(self.db_path)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM her_day_posts").fetchall()]
        finally:
            conn.close()

    def _plan_total(self):
        plans = her_day._build_all_plans(
            GIRLS, datetime.now().strftime("%Y-%m-%d"))
        return sum(len(p) for p in plans.values())

    def _seen_count(self):
        conn = db.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM her_day_seen").fetchone()[0]
        finally:
            conn.close()

    # -- generation ------------------------------------------------------
    def test_daily_generates_one_shared_set(self):
        self._mk_users("user-a", "user-b", "probe-1")
        total = self._plan_total()
        report = her_day.generate_daily()
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["generated"], total)
        rows = self._rows()
        self.assertEqual(len(rows), total)
        self.assertEqual({r["user_id"] for r in rows}, {SHARED})
        # Second run of the day: everything already there.
        report2 = her_day.generate_daily()
        self.assertEqual(report2["generated"], 0)
        self.assertEqual(len(self._rows()), total)

    def test_no_user_is_ever_written_for(self):
        self._mk_users("user-a", "probe-1", "probe-2")
        her_day.generate_daily()
        owners = {r["user_id"] for r in self._rows()}
        self.assertEqual(owners, {SHARED})

    def test_images_generate_once_regardless_of_users(self):
        self._mk_users(*[f"user-{i}" for i in range(8)])
        her_day.generate_daily()
        plans = her_day._build_all_plans(
            GIRLS, datetime.now().strftime("%Y-%m-%d"))
        unique_visual = set()
        for gid, plan in plans.items():
            for (mkey, is_group, pids, _pn) in plan:
                ptype, scene, _d = her_day._moment_def(mkey)
                if ptype == "image" and scene:
                    unique_visual.add(
                        ("_".join(sorted([gid] + pids)), mkey))
        self.assertEqual(len(self.image_news), len(unique_visual))

    def test_on_demand_generate_creates_shared_once(self):
        self._mk_users("user-a", "user-b")
        total = self._plan_total()
        self.assertEqual(her_day.generate_for_all_girls("user-a"), total)
        self.assertEqual(her_day.generate_for_all_girls("user-b"), 0)
        self.assertEqual({r["user_id"] for r in self._rows()}, {SHARED})

    def test_voice_is_never_invented(self):
        self._mk_users("user-a")
        her_day.generate_daily()
        for r in self._rows():
            self.assertFalse((r["voice_url"] or "").startswith("STUB"))

    # -- lazy attach / seen ----------------------------------------------
    def test_two_users_read_the_same_posts_without_writes(self):
        self._mk_users("user-a", "user-b")
        her_day.generate_daily()
        before = len(self._rows())
        conn = db.connect(self.db_path)
        try:
            a = store.get_her_day_posts(conn, "user-a")
            b = store.get_her_day_posts(conn, "user-b")
        finally:
            conn.close()
        self.assertEqual([p["id"] for p in a], [p["id"] for p in b])
        self.assertEqual(len(a), self._plan_total())
        self.assertEqual(len(self._rows()), before)
        self.assertEqual(self._seen_count(), 0)

    def test_probe_reads_feed_without_any_write(self):
        self._mk_users("probe-1")
        her_day.generate_daily()
        before = len(self._rows())
        conn = db.connect(self.db_path)
        try:
            posts = store.get_her_day_posts(conn, "probe-1")
        finally:
            conn.close()
        self.assertEqual(len(posts), self._plan_total())
        self.assertEqual(len(self._rows()), before)

    def test_seen_is_per_user_and_idempotent(self):
        self._mk_users("user-a", "user-b")
        her_day.generate_daily()
        conn = db.connect(self.db_path)
        try:
            post_id = store.get_her_day_posts(conn, "user-a")[0]["id"]
            self.assertTrue(store.mark_her_day_viewed(conn, post_id,
                                                      "user-a"))
            self.assertTrue(store.mark_her_day_viewed(conn, post_id,
                                                      "user-a"))
            a = {p["id"]: p for p in store.get_her_day_posts(conn, "user-a")}
            b = {p["id"]: p for p in store.get_her_day_posts(conn, "user-b")}
            self.assertEqual(a[post_id]["viewed"], 1)
            self.assertEqual(b[post_id]["viewed"], 0)
            self.assertEqual(self._seen_count(), 1)
            self.assertFalse(store.mark_her_day_viewed(conn, 999999,
                                                       "user-a"))
        finally:
            conn.close()

    def test_foreign_legacy_post_cannot_be_marked(self):
        self._mk_users("user-a", "user-b")
        conn = db.connect(self.db_path)
        try:
            post = store.save_her_day_post(conn, "user-a", "chloe",
                                           post_type="text", caption="x")
            self.assertFalse(store.mark_her_day_viewed(conn, post["id"],
                                                       "user-b"))
            self.assertTrue(store.mark_her_day_viewed(conn, post["id"],
                                                      "user-a"))
        finally:
            conn.close()

    # -- legacy fallback ---------------------------------------------------
    def test_legacy_rows_read_until_shared_set_wins(self):
        self._mk_users("user-a")
        conn = db.connect(self.db_path)
        try:
            legacy = store.save_her_day_post(
                conn, "user-a", "chloe", post_type="text",
                caption="legacy chloe")
            extra = store.save_her_day_post(
                conn, "user-a", "offroster", post_type="text",
                caption="girl with no shared set")
            posts = store.get_her_day_posts(conn, "user-a")
            self.assertEqual({p["id"] for p in posts},
                             {legacy["id"], extra["id"]})
        finally:
            conn.close()
        her_day.generate_daily()
        conn = db.connect(self.db_path)
        try:
            posts = store.get_her_day_posts(conn, "user-a")
            ids = [p["id"] for p in posts]
            captions = [p["caption"] for p in posts]
            # Shared Chloe set replaces the legacy Chloe row (no dupes);
            # the off-roster legacy row still shows.
            self.assertNotIn(legacy["id"], ids)
            self.assertIn(extra["id"], ids)
            self.assertNotIn("legacy chloe", captions)
            self.assertIn("girl with no shared set", captions)
            self.assertEqual(len(ids), self._plan_total() + 1)
        finally:
            conn.close()

    def test_girl_filter_applies_to_shared_reads(self):
        self._mk_users("user-a")
        her_day.generate_daily()
        conn = db.connect(self.db_path)
        try:
            chloe = store.get_her_day_posts(conn, "user-a",
                                            girl_id="chloe")
            self.assertTrue(chloe)
            self.assertEqual({p["girl_id"] for p in chloe}, {"chloe"})
        finally:
            conn.close()

    # -- public feed / cleanup ---------------------------------------------
    def test_public_feed_prefers_shared_without_duplicates(self):
        self._mk_users("user-a", "user-b", "user-c")
        her_day.generate_daily()
        conn = db.connect(self.db_path)
        try:
            posts = store.get_public_her_day_posts(conn, limit=100)
        finally:
            conn.close()
        # Exactly one shared copy of the day-set — not one per user.
        self.assertEqual(len(posts), self._plan_total())

    def test_cleanup_expires_posts_and_prunes_seen(self):
        self._mk_users("user-a")
        her_day.generate_daily()
        conn = db.connect(self.db_path)
        try:
            post_id = store.get_her_day_posts(conn, "user-a")[0]["id"]
            store.mark_her_day_viewed(conn, post_id, "user-a")
            self.assertEqual(self._seen_count(), 1)
            conn.execute(
                "UPDATE her_day_posts SET expires_at = datetime('now',"
                " '-1 hour') WHERE id = ?", (post_id,))
            conn.commit()
            deleted = store.cleanup_expired_her_day(conn)
            self.assertEqual(deleted, 1)
            self.assertEqual(self._seen_count(), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
