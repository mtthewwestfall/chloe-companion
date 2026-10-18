"""Conversion fix Item C tests (2026-10-18): record the moments.

Pins: events write + daily counts roundtrip; checkout_abandoned is
computed at read time (24h window, same user); the client intake
endpoint accepts only the whitelisted kinds; the admin read is
token-gated; and the chat empty-bank wall now leaves a wall_hit row.

Run:  python -m pytest tests/test_conversion_item_c.py -q
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException  # noqa: E402

from app import characters, config, db, events, store  # noqa: E402
from app import main as api  # noqa: E402


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _insert(conn, user_id, kind, hours_ago, meta=""):
    ts = (datetime.utcnow() - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO conversion_events (user_id, kind, meta, created_at) "
        "VALUES (?, ?, ?, ?)", (user_id, kind, meta, ts))
    conn.commit()


class EventsModule(unittest.TestCase):
    def setUp(self):
        self.conn = _memdb()
        events._ensure_table(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_record_and_daily_counts(self):
        events.record_event(self.conn, "u1", "wall_hit",
                            {"wall": "out_of_messages"})
        events.record_event(self.conn, "u1", "plans_view")
        events.record_event(self.conn, "u2", "wall_hit",
                            {"wall": "voice_cap"})
        out = events.daily_counts(self.conn, days=1)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        days = {d["date"]: d["counts"] for d in out["days"]}
        self.assertEqual(days[today]["wall_hit"], 2)
        self.assertEqual(days[today]["plans_view"], 1)

    def test_flag_off_records_nothing(self):
        old = config.CONV_EVENTS
        config.CONV_EVENTS = False
        try:
            events.record_event(self.conn, "u1", "plans_view")
        finally:
            config.CONV_EVENTS = old
        out = events.daily_counts(self.conn, days=1)
        self.assertEqual(out["days"], [])

    def test_abandoned_after_24h_without_completion(self):
        _insert(self.conn, "u1", "checkout_started", hours_ago=30)
        out = events.daily_counts(self.conn, days=7)
        counts = [d["counts"] for d in out["days"]]
        abandoned = sum(c.get("checkout_abandoned", 0) for c in counts)
        self.assertEqual(abandoned, 1)

    def test_not_abandoned_when_completed_within_24h(self):
        _insert(self.conn, "u1", "checkout_started", hours_ago=30)
        _insert(self.conn, "u1", "checkout_completed", hours_ago=29)
        out = events.daily_counts(self.conn, days=7)
        abandoned = sum(d["counts"].get("checkout_abandoned", 0)
                        for d in out["days"])
        self.assertEqual(abandoned, 0)

    def test_not_abandoned_inside_the_window(self):
        _insert(self.conn, "u1", "checkout_started", hours_ago=2)
        out = events.daily_counts(self.conn, days=7)
        abandoned = sum(d["counts"].get("checkout_abandoned", 0)
                        for d in out["days"])
        self.assertEqual(abandoned, 0)

    def test_completion_for_another_user_does_not_rescue(self):
        _insert(self.conn, "u1", "checkout_started", hours_ago=30)
        _insert(self.conn, "u2", "checkout_completed", hours_ago=29)
        out = events.daily_counts(self.conn, days=7)
        abandoned = sum(d["counts"].get("checkout_abandoned", 0)
                        for d in out["days"])
        self.assertEqual(abandoned, 1)


class _Client:
    host = "127.0.0.1"


class _Req:
    def __init__(self, token=None, admin=None):
        self.headers = {}
        if token:
            self.headers["authorization"] = f"Bearer {token}"
        if admin:
            self.headers["x-admin-token"] = admin
        self.client = _Client()


class Endpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_admin = config.ADMIN_TOKEN
        config.ADMIN_TOKEN = "test-admin-token"
        self._real_cache = characters._cache
        characters._cache = {"ts": time.time(), "chars": [], "local": []}
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, "u_wall")
            conn.execute("UPDATE users SET messages_remaining = 0 "
                         "WHERE user_id = 'u_wall'")
            conn.commit()
            store.create_session(conn, "u_wall", "tok_wall")
        finally:
            conn.close()

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        config.ADMIN_TOKEN = self._real_admin
        characters._cache = self._real_cache
        self.tmp.cleanup()

    def test_client_event_accepted_and_counted(self):
        body = api.ConversionEventIn(kind="plans_view")
        out = api.conversion_event(body, _Req("tok_wall"))
        self.assertTrue(out["ok"])
        res = api.admin_conversion_events(
            _Req(admin="test-admin-token"), days=1)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        days = {d["date"]: d["counts"] for d in res["days"]}
        self.assertEqual(days[today]["plans_view"], 1)

    def test_client_event_rejects_unknown_kind(self):
        body = api.ConversionEventIn(kind="wall_hit")  # server-side only
        with self.assertRaises(HTTPException) as ctx:
            api.conversion_event(body, _Req("tok_wall"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_admin_read_requires_token(self):
        with self.assertRaises(HTTPException):
            api.admin_conversion_events(_Req(), days=1)

    def test_empty_bank_chat_records_wall_hit(self):
        body = api.ChatIn(message="hello?", character_id="chloe")
        resp = api.chat(body, _Req("tok_wall"))
        self.assertIn("$0.99", resp["reply_text"])  # the upsell wall
        conn = db.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT kind, meta FROM conversion_events "
                "WHERE user_id = 'u_wall'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "wall_hit")
        self.assertIn("out_of_messages", row["meta"])


if __name__ == "__main__":
    unittest.main()
