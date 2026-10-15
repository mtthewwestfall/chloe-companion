"""Her Day media persistence fix (2026-10-15).

Three parts under test:
  1. config.GENERATED_DIR defaults to a "generated" folder NEXT TO the
     SQLite DB, so in production (CHLOE_DB_PATH on the Railway volume)
     renders land on the persistent volume; CHLOE_GENERATED_DIR still
     wins when set.
  2. POST /api/v1/admin/her-day/set-media attaches an existing
     /generated/ file to an existing post — admin-gated, 404 on an
     unknown post, and it refuses anything that is not a real pool
     file (no /media/ URLs, no absolute URLs, no missing files, and
     NEVER a file registered in girl_media — Her Day stays SFW).
  3. backfill_today_media is pool-first: a render of the same
     participants+moment from ANY earlier day in the pool is reused
     (copied to today's cache name) and the renderer is never called
     for pooled moments.

Run:  python -m unittest tests.test_her_day_media_persistence -v
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException  # noqa: E402

from app import config, db, her_day, store  # noqa: E402
from app import main as api  # noqa: E402

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ADMIN_ATTR = "ADMIN" + chr(95) + "TOKEN"


# ---------------------------------------------------------------------------
# 1. GENERATED_DIR resolution
# ---------------------------------------------------------------------------

class GeneratedDirConfigTest(unittest.TestCase):
    def _import_config(self, extra_env):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CHLOE_")}
        env.update(extra_env)
        code = (
            "import sys; sys.path.insert(0, %r);"
            "from app import config;"
            "print(config.DB_PATH); print(config.GENERATED_DIR)"
        ) % SERVER_ROOT
        out = subprocess.run([sys.executable, "-c", code], env=env,
                             capture_output=True, text=True,
                             cwd=SERVER_ROOT, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        lines = out.stdout.strip().splitlines()
        return lines[0], lines[1]

    def test_generated_dir_sits_next_to_db(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "chloe.db")
            got_db, got_gen = self._import_config(
                {"CHLOE_DB_PATH": db_path})
            self.assertEqual(got_db, db_path)
            self.assertEqual(got_gen, os.path.join(td, "generated"))

    def test_generated_dir_env_override_still_wins(self):
        with tempfile.TemporaryDirectory() as td:
            custom = os.path.join(td, "elsewhere")
            _db, got_gen = self._import_config({
                "CHLOE_DB_PATH": os.path.join(td, "chloe.db"),
                "CHLOE_GENERATED_DIR": custom})
            self.assertEqual(got_gen, custom)


# ---------------------------------------------------------------------------
# 2. set-media endpoint
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


class SetMediaEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_admin = getattr(config, ADMIN_ATTR)
        setattr(config, ADMIN_ATTR, "test-admin-secret")
        self._old_gen = config.GENERATED_DIR
        self.gen_dir = os.path.join(self.tmp.name, "generated")
        os.makedirs(self.gen_dir)
        config.GENERATED_DIR = self.gen_dir

        conn = db.connect(self.db_path)
        try:
            self.post = store.save_her_day_post(
                conn, "user1", "chloe", post_type="image",
                media_url="", voice_url="", caption="cap")
        finally:
            conn.close()

    def tearDown(self):
        api._conn = self._real_conn
        setattr(config, ADMIN_ATTR, self._real_admin)
        config.GENERATED_DIR = self._old_gen
        self.tmp.cleanup()

    def _req(self, tok="test-admin-secret"):
        return _Req({"x-admin-token": tok} if tok else {})

    def _body(self, **kw):
        return api.HerDaySetMediaIn(**kw)

    def _write_pool_file(self, name, data=b"\xff\xd8\xffx"):
        with open(os.path.join(self.gen_dir, name), "wb") as f:
            f.write(data)
        return "/generated/" + name

    def _call(self, body, req=None):
        return api.admin_her_day_set_media(body, req or self._req())

    def test_requires_admin(self):
        with self.assertRaises(HTTPException) as cm:
            self._call(self._body(post_id=self.post["id"],
                                  media_url="/generated/x.jpg"),
                       req=self._req(tok=None))
        self.assertIn(cm.exception.status_code, (401, 403, 503))

    def test_unknown_post_is_404(self):
        url = self._write_pool_file("herday_chloe_2026-01-01_gym.jpg")
        with self.assertRaises(HTTPException) as cm:
            self._call(self._body(post_id=999999, media_url=url))
        self.assertEqual(cm.exception.status_code, 404)

    def test_nothing_to_set_is_400(self):
        with self.assertRaises(HTTPException) as cm:
            self._call(self._body(post_id=self.post["id"]))
        self.assertEqual(cm.exception.status_code, 400)

    def test_rejects_media_dir_url(self):
        with self.assertRaises(HTTPException) as cm:
            self._call(self._body(post_id=self.post["id"],
                                  media_url="/media/some_clip.mp4"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_rejects_absolute_url(self):
        with self.assertRaises(HTTPException) as cm:
            self._call(self._body(
                post_id=self.post["id"],
                media_url="https://example.com/generated/x.jpg"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_rejects_missing_file(self):
        with self.assertRaises(HTTPException) as cm:
            self._call(self._body(
                post_id=self.post["id"],
                media_url="/generated/herday_chloe_2026-01-01_gym.jpg"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_rejects_path_escape(self):
        with self.assertRaises(HTTPException) as cm:
            self._call(self._body(post_id=self.post["id"],
                                  media_url="/generated/../chloe.db"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_rejects_give_library_file(self):
        # A real file in the pool that is registered in girl_media is
        # give content — it must never land on a Her Day post.
        url = self._write_pool_file("req_123.mp4", b"\x00" * 16)
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO girl_media (girl_id, kind, url, preset)"
                " VALUES ('chloe', 'give', ?, 0)", (url,))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(HTTPException) as cm:
            self._call(self._body(post_id=self.post["id"], media_url=url))
        self.assertEqual(cm.exception.status_code, 400)

    def test_rejects_give_upload_name(self):
        url = self._write_pool_file("give-upload-chloe-stage3-ab12cd34.mp4",
                                    b"\x00" * 16)
        with self.assertRaises(HTTPException) as cm:
            self._call(self._body(post_id=self.post["id"], media_url=url))
        self.assertEqual(cm.exception.status_code, 400)

    def test_sets_media_and_voice_and_clears(self):
        img = self._write_pool_file("herday_chloe_2026-01-01_gym.jpg")
        aud = self._write_pool_file("voice_chloe_2026-01-01.mp3",
                                    b"\x00" * 16)
        out = self._call(self._body(post_id=self.post["id"],
                                    media_url=img, voice_url=aud))
        self.assertTrue(out["ok"])
        self.assertEqual(out["post"]["media_url"], img)
        self.assertEqual(out["post"]["voice_url"], aud)
        # Clearing with "" works and leaves the other field alone.
        out = self._call(self._body(post_id=self.post["id"], media_url=""))
        self.assertEqual(out["post"]["media_url"], "")
        self.assertEqual(out["post"]["voice_url"], aud)


# ---------------------------------------------------------------------------
# 3. Pool-first backfill
# ---------------------------------------------------------------------------

class PoolFirstBackfillTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test.db")
        self.conn = db.connect(self.db_path)
        self.conn.executescript(db.SCHEMA)
        self.conn.commit()
        self._old_connect = her_day.db.connect
        real_connect = db.connect
        her_day.db.connect = lambda *a, **k: real_connect(self.db_path)
        self._old_gen = config.GENERATED_DIR
        self.gen_dir = os.path.join(self._tmp.name, "generated")
        os.makedirs(self.gen_dir)
        config.GENERATED_DIR = self.gen_dir
        self.date_str = datetime.now().strftime("%Y-%m-%d")
        self.girls = [{"id": "regan", "name": "Regan", "avatar_url": ""},
                      {"id": "mara", "name": "Mara", "avatar_url": ""}]

    def tearDown(self):
        her_day.db.connect = self._old_connect
        config.GENERATED_DIR = self._old_gen
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

    def _pool_file(self, participants, moment_key, date="2020-01-01",
                   data=b"POOL"):
        dest = her_day._herday_image_path(participants, moment_key, date)
        with open(dest, "wb") as f:
            f.write(data)
        return dest

    def test_pool_file_for_finds_any_date_newest_wins(self):
        older = self._pool_file(["regan"], "gym", "2020-01-01", b"OLD")
        newer = self._pool_file(["regan"], "gym", "2021-06-01", b"NEW")
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))
        self.assertEqual(her_day._pool_file_for(["regan"], "gym"), newer)
        # Different moment / different girl: no match.
        self.assertIsNone(her_day._pool_file_for(["regan"], "cooking"))
        self.assertIsNone(her_day._pool_file_for(["mara"], "gym"))
        # A non-dated lookalike is never pooled.
        with open(os.path.join(self.gen_dir,
                               "herday_regan_xxx_gym.jpg"), "wb") as f:
            f.write(b"FAKE")
        self.assertEqual(her_day._pool_file_for(["regan"], "gym"), newer)

    def test_backfill_prefers_pool_and_skips_renderer(self):
        plans = self._seed_today()
        # Pool EVERY image moment from an earlier day.
        pooled = 0
        for g in self.girls:
            for (mkey, _is_group, pids, _pn) in plans[g["id"]]:
                ptype, scene, _d = her_day._moment_def(mkey)
                if ptype != "image" or not scene:
                    continue
                self._pool_file([g["id"]] + list(pids), mkey)
                pooled += 1
        self.assertTrue(pooled)

        def _boom(*_a, **_k):
            raise AssertionError("renderer must not run for pooled media")

        with mock.patch.object(her_day, "_get_all_girls",
                               return_value=self.girls), \
                mock.patch.object(her_day, "_generate_sfw_image",
                                  side_effect=_boom):
            report = her_day.backfill_today_media(self.date_str)

        image_rows = self.conn.execute(
            "SELECT media_url FROM her_day_posts WHERE post_type = 'image'"
        ).fetchall()
        self.assertTrue(image_rows)
        self.assertTrue(all(r["media_url"] for r in image_rows))
        # Every pooled fill is counted, and URLs point at TODAY'S dated
        # cache files (copied from the pool), which exist on disk.
        self.assertEqual(report["image_posts_filled_from_pool"],
                         report["image_posts_filled"])
        self.assertEqual(report["image_posts_filled"], len(image_rows))
        for r in image_rows:
            name = os.path.basename(r["media_url"])
            self.assertIn(self.date_str, name)
            with open(os.path.join(self.gen_dir, name), "rb") as f:
                self.assertEqual(f.read(), b"POOL")

    def test_backfill_renders_only_what_pool_lacks(self):
        plans = self._seed_today()
        # Pool exactly one distinct moment; everything else renders.
        first = None
        for g in self.girls:
            for (mkey, _is_group, pids, _pn) in plans[g["id"]]:
                ptype, scene, _d = her_day._moment_def(mkey)
                if ptype == "image" and scene:
                    first = ([g["id"]] + list(pids), mkey)
                    break
            if first:
                break
        self.assertIsNotNone(first)
        self._pool_file(*first)
        rendered = []

        def fake_gen(names, avatar, participants, moment_key, scene, ds):
            rendered.append((tuple(sorted(participants)), moment_key))
            return f"/generated/rendered_{moment_key}.jpg"

        with mock.patch.object(her_day, "_get_all_girls",
                               return_value=self.girls), \
                mock.patch.object(her_day, "_generate_sfw_image",
                                  side_effect=fake_gen):
            report = her_day.backfill_today_media(self.date_str)
        self.assertGreaterEqual(report["image_posts_filled_from_pool"], 1)
        self.assertGreater(report["image_posts_filled"],
                           report["image_posts_filled_from_pool"])
        self.assertNotIn((tuple(sorted(first[0])), first[1]), rendered)


if __name__ == "__main__":
    unittest.main()
