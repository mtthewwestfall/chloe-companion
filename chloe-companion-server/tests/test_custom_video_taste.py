"""Improvement #6 (2026-10-19): custom videos never dead-end.

A custom render can park for hours behind Sogni's session / throttle
/ capacity states, and the user used to stare at an empty chat the
whole time. Now submit delivers an INSTANT taste clip — the
ask-closest preset from the girl's OWN give library, stage read from
the request description and clamped to store.give_stage_cap — into
the chat with her taste line, recorded on the row (`taste_url`).

Pinned here:
- submit (POST /api/v1/custom-videos handler) delivers the taste at
  once, matched to the ask's stage, records taste_url, still spawns
  the render exactly once, and touches NO media-request pool /
  entitlement (custom requests stay free);
- a synthetic group/event account's stage-9 ask is served at the
  stage-3 cap, never above;
- a Sogni-stalled (parked) request still leaves him the taste clip;
- render success afterwards delivers the real custom video as the
  ONE follow-up (taste + fulfilled = exactly two turns; a re-run of
  the runner is skipped and delivers nothing more);
- a definitive refusal after a taste closes as `fallback` WITHOUT a
  second preset — the taste stands as the fallback on the row;
- taste delivery is idempotent per request and a no-op when she has
  no library of her own;
- update_custom_request records taste_url; the migration adds the
  column to a pre-#6 table idempotently.
The render seam is mocked throughout — no real renders.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, characters, config, custom_video, db, store  # noqa: E402
from app import main as api  # noqa: E402

USER = "taste_user"
EVENT_USER = "tg_900000000123"  # synthetic group/event account (cap 3)
SESSION = {"token": "tok-aaa", "refreshToken": "ref-bbb"}
REFUSAL = "5000: Sensitive content detected."
STAGE1_URL = "/media/taste-brooklyn-s1.mp4"
STAGE2_URL = "/media/taste-brooklyn-s2.mp4"


def _db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db._migrate_character_columns(conn)
    db._migrate_auth_columns(conn)
    db._migrate_submission_columns(conn)
    db._migrate_creator_columns(conn)
    db._migrate_owner_column(conn)
    db._migrate_referral_columns(conn)
    db._migrate_conversation_attachments(conn)
    db._migrate_girl_media_stage(conn)
    db._migrate_give_gen_source(conn)
    db._migrate_custom_video_columns(conn)
    conn.executescript(db.SCHEMA)
    return conn


class _NoClose:
    def __init__(self, conn):
        self._c = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._c, name)


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self._nc = _NoClose(self.conn)
        self._connect_patch = mock.patch.object(
            db, "connect", return_value=self._nc)
        self._connect_patch.start()
        store.get_or_create_user(self.conn, USER)
        store.get_or_create_user(self.conn, EVENT_USER)
        self.conn.commit()

    def tearDown(self):
        self._connect_patch.stop()
        self.conn.close()

    # -- helpers ------------------------------------------------------
    def _req(self, girl, desc, user=USER):
        return store.save_custom_request(self.conn, user, girl, desc)

    def _row(self, rid):
        return dict(self.conn.execute(
            "SELECT * FROM custom_video_requests WHERE id = ?",
            (rid,)).fetchone())

    def _seed_preset(self, girl, url, stage=1):
        self.conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset, stage) "
            "VALUES (?, 'give', ?, 'give', ?)", (girl, url, stage))
        self.conn.commit()

    def _turns(self, girl, user=USER):
        return [dict(r) for r in self.conn.execute(
            "SELECT role, text, attachments FROM conversation "
            "WHERE user_id = ? AND character_id = ?",
            (user, girl)).fetchall()]

    def _login_run(self, req_id, outcome, extra_patches=()):
        patches = [
            mock.patch.object(assist, "stored_sogni_session",
                              return_value=SESSION),
            mock.patch.object(assist, "_fetch_first_frame",
                              return_value=None),
            mock.patch.object(assist, "_sogni_plan_run",
                              return_value=(outcome, None)),
            *extra_patches,
        ]
        for p in patches:
            p.start()
        try:
            return custom_video.run_custom_generation_with_login(req_id)
        finally:
            for p in patches:
                p.stop()


class TasteDeliveryTest(_Base):
    def test_taste_matches_ask_stage_and_records_row(self):
        self._seed_preset("brooklyn", STAGE1_URL, stage=1)
        self._seed_preset("brooklyn", STAGE2_URL, stage=2)
        req = self._req("brooklyn", "take off your shirt and smile")
        url = custom_video.deliver_taste_clip(self.conn, req)
        self.assertEqual(url, STAGE2_URL)
        row = self._row(req["id"])
        self.assertEqual(row["taste_url"], STAGE2_URL)
        self.assertEqual(row["status"], "pending")  # render still owed
        turns = self._turns("brooklyn")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], custom_video._TASTE_LINE)
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["kind"], "video")
        self.assertEqual(atts[0]["url"], STAGE2_URL)

    def test_taste_line_promises_no_delivery_time(self):
        line = custom_video._TASTE_LINE.lower()
        for word in ("minute", "hour", "soon", "shortly", "in a bit"):
            self.assertNotIn(word, line)

    def test_taste_idempotent_per_request(self):
        self._seed_preset("brooklyn", STAGE1_URL)
        req = self._req("brooklyn", "dancing slowly")
        first = custom_video.deliver_taste_clip(self.conn, req)
        second = custom_video.deliver_taste_clip(
            self.conn, self._row(req["id"]))
        self.assertEqual(first, STAGE1_URL)
        self.assertEqual(second, STAGE1_URL)
        self.assertEqual(len(self._turns("brooklyn")), 1)

    def test_taste_no_library_is_a_noop(self):
        req = self._req("brooklyn", "dancing slowly")
        self.assertEqual(
            custom_video.deliver_taste_clip(self.conn, req), "")
        self.assertEqual(self._row(req["id"])["taste_url"], "")
        self.assertEqual(self._turns("brooklyn"), [])

    def test_taste_never_crosses_girls(self):
        self._seed_preset("mia", "/media/taste-mia-s1.mp4")
        req = self._req("brooklyn", "dancing slowly")
        self.assertEqual(
            custom_video.deliver_taste_clip(self.conn, req), "")
        self.assertEqual(self._turns("brooklyn"), [])
        self.assertEqual(self._turns("mia"), [])

    def test_taste_respects_group_stage_cap(self):
        for st in range(1, 10):
            self._seed_preset("chloe", f"/media/taste-chloe-s{st}.mp4",
                              stage=st)
        req = self._req("chloe", "I want you to fuck me",
                        user=EVENT_USER)
        url = custom_video.deliver_taste_clip(self.conn, req)
        self.assertEqual(url, "/media/taste-chloe-s3.mp4")
        turns = self._turns("chloe", user=EVENT_USER)
        self.assertEqual(len(turns), 1)


class TasteThenOutcomeTest(_Base):
    def _tasted_req(self):
        self._seed_preset("brooklyn", STAGE1_URL)
        req = self._req("brooklyn", "dancing in a red dress")
        custom_video.deliver_taste_clip(self.conn, req)
        return self._row(req["id"])

    def test_stalled_render_still_leaves_the_taste(self):
        req = self._tasted_req()
        token = self._login_run(req["id"], {
            "status": "throttled",
            "detail": "4056: Connection temporarily blocked due to "
                      "excessive reconnections",
            "throttle_seconds": 1800, "path": None,
            "project_id": None})
        self.assertEqual(token, "parked")
        row = self._row(req["id"])
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["taste_url"], STAGE1_URL)
        turns = self._turns("brooklyn")
        self.assertEqual(len(turns), 1)  # just the taste, nothing more
        self.assertEqual(turns[0]["text"], custom_video._TASTE_LINE)

    def test_success_is_the_one_followup(self):
        req = self._tasted_req()
        with mock.patch("os.path.getsize", return_value=10):
            token = self._login_run(req["id"], {
                "status": "done", "path": "/tmp/taste_done.mp4",
                "project_id": "p1", "detail": ""})
        self.assertEqual(token, "fulfilled")
        row = self._row(req["id"])
        self.assertEqual(row["status"], "fulfilled")
        self.assertEqual(row["video_url"], "/generated/taste_done.mp4")
        turns = self._turns("brooklyn")
        self.assertEqual(len(turns), 2)  # taste + the real one, no more
        self.assertEqual(turns[0]["text"], custom_video._TASTE_LINE)
        self.assertIn("I made that special video for you",
                      turns[1]["text"])
        # A re-run (admin retry / sweep overlap) delivers nothing more:
        # the atomic claim skips a non-pending row.
        token = self._login_run(req["id"], {
            "status": "done", "path": "/tmp/taste_done2.mp4",
            "project_id": "p2", "detail": ""})
        self.assertEqual(token, "skipped")
        self.assertEqual(len(self._turns("brooklyn")), 2)

    def test_refusal_after_taste_sends_no_second_preset(self):
        req = self._tasted_req()
        token = self._login_run(req["id"], {
            "status": "failed", "detail": REFUSAL, "path": None,
            "project_id": None})
        self.assertEqual(token, "fallback")
        row = self._row(req["id"])
        self.assertEqual(row["status"], "fallback")
        self.assertEqual(row["error"], REFUSAL)
        # The taste he already holds stands as the recorded fallback.
        self.assertEqual(row["fallback_url"], STAGE1_URL)
        turns = self._turns("brooklyn")
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], custom_video._TASTE_LINE)


class StoreAndMigrationTest(_Base):
    def test_update_custom_request_records_taste_url(self):
        req = self._req("brooklyn", "dancing slowly")
        store.update_custom_request(
            self.conn, req["id"], "pending", taste_url=STAGE1_URL)
        self.assertEqual(self._row(req["id"])["taste_url"], STAGE1_URL)

    def test_migration_adds_taste_url_to_pre_fix_table(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE custom_video_requests ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id TEXT NOT NULL, girl_id TEXT NOT NULL,"
            " description TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " video_url TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        db._migrate_custom_video_columns(conn)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(custom_video_requests)").fetchall()}
        self.assertIn("taste_url", cols)
        db._migrate_custom_video_columns(conn)  # idempotent
        conn.close()


class _Req:
    def __init__(self, token):
        self.headers = {"authorization": f"Bearer {token}"}
        self.base_url = "https://example.test/"


def _char(cid, name, owner=None):
    return {"id": cid, "name": name, "avatar_url": "", "persona": "",
            "companion_voice": "", "blurb": "", "door_title": "",
            "sort_order": 1, "owner_user_id": owner}


class SubmitEndpointTest(unittest.TestCase):
    """End to end through POST /api/v1/custom-videos' handler: the
    taste lands in the chat DURING the submit call, the render spawn
    fires exactly once, and nothing is charged anywhere."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_key = config.SOGNI_API_KEY
        config.SOGNI_API_KEY = "test-key"
        self._real_cache = characters._cache
        characters._cache = {"ts": time.time(), "chars": [], "local": [
            _char("chloe", "Chloe")]}
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, USER, "Matty")
            store.get_or_create_user(conn, EVENT_USER)
            self.token = "tok-user"
            store.create_session(conn, USER, self.token)
            self.event_token = "tok-event"
            store.create_session(conn, EVENT_USER, self.event_token)
            for st in (1, 2, 3, 9):
                conn.execute(
                    "INSERT INTO girl_media (girl_id, kind, url,"
                    " preset, stage) VALUES ('chloe', 'give', ?,"
                    " 'give', ?)",
                    (f"https://cdn.test/chloe-stage{st}.mp4", st))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        config.SOGNI_API_KEY = self._real_key
        characters._cache = self._real_cache
        self.tmp.cleanup()

    def _submit(self, token, desc):
        with mock.patch.object(assist, "stored_sogni_session",
                               return_value=None), \
                mock.patch.object(custom_video,
                                  "spawn_custom_generation") as spawn:
            resp = api.create_custom_video(
                api.CustomVideoIn(girl_id="chloe", description=desc),
                _Req(token))
        return resp, spawn

    def _turns(self, user):
        conn = db.connect(self.db_path)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT role, text, attachments FROM conversation "
                "WHERE user_id = ? AND character_id = 'chloe'",
                (user,)).fetchall()]
        finally:
            conn.close()

    def test_submit_delivers_taste_and_spawns_render_once(self):
        resp, spawn = self._submit(
            self.token, "take off your shirt and smile")
        self.assertEqual(resp["status"], "pending")
        # Ask named stage 2 -> the taste is her stage-2 clip.
        self.assertEqual(resp["taste_url"],
                         "https://cdn.test/chloe-stage2.mp4")
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(spawn.call_args[0][0], resp["id"])
        turns = self._turns(USER)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], custom_video._TASTE_LINE)
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["url"],
                         "https://cdn.test/chloe-stage2.mp4")
        conn = db.connect(self.db_path)
        try:
            row = dict(conn.execute(
                "SELECT * FROM custom_video_requests WHERE id = ?",
                (resp["id"],)).fetchone())
            self.assertEqual(row["taste_url"],
                             "https://cdn.test/chloe-stage2.mp4")
            self.assertEqual(row["status"], "pending")
            # No charge anywhere: no media-request rows, no pool use.
            n = conn.execute(
                "SELECT COUNT(*) FROM media_requests").fetchone()[0]
            self.assertEqual(n, 0)
            self.assertEqual(
                store.regular_requests_used_this_month(conn, USER), 0)
        finally:
            conn.close()

    def test_submit_stage9_ask_on_event_account_capped_at_3(self):
        resp, spawn = self._submit(
            self.event_token, "I want you to fuck me")
        self.assertEqual(resp["taste_url"],
                         "https://cdn.test/chloe-stage3.mp4")
        self.assertEqual(spawn.call_count, 1)
        turns = self._turns(EVENT_USER)
        self.assertEqual(len(turns), 1)
        atts = json.loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["url"],
                         "https://cdn.test/chloe-stage3.mp4")


if __name__ == "__main__":
    unittest.main()
