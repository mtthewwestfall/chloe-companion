"""Live proactive messaging tests (2026-10-16, feat/proactive-live).

Covers: pairs from conversation history, morning timing (due at 8:15am,
not twice, not at 3pm), nudge timing (due after 24h, not when active,
not twice in 24h), and paid-tier gating.
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from app import proactive


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE users (
        user_id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        tier TEXT DEFAULT 'basic',
        messages_remaining INTEGER DEFAULT 0,
        proactive_enabled INTEGER DEFAULT 0,
        timezone TEXT DEFAULT 'America/New_York',
        proactive_day TEXT DEFAULT '',
        proactive_sent_today INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE conversation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        character_id TEXT DEFAULT 'chloe',
        role TEXT NOT NULL,
        text TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE TABLE stripe_customers (user_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE trial_grants (user_id TEXT PRIMARY KEY)")
    conn.execute("""CREATE TABLE jvzoo_subscriptions (
        user_id TEXT PRIMARY KEY, status TEXT DEFAULT 'active')""")
    conn.commit()
    return conn


def _add_user(conn, user_id, **kw):
    d = dict(user_id=user_id, name="Test", tier="basic",
             messages_remaining=10, proactive_enabled=1,
             timezone="America/New_York", proactive_day="",
             proactive_sent_today=0)
    d.update(kw)
    cols = ", ".join(d.keys())
    vals = ", ".join(["?"] * len(d))
    conn.execute(f"INSERT INTO users ({cols}) VALUES ({vals})",
                 tuple(d.values()))
    conn.commit()


def _add_turn(conn, user_id, character_id="chloe", role="user",
              created_at=None):
    ts = created_at or datetime.now().isoformat()
    conn.execute(
        "INSERT INTO conversation (user_id, character_id, role, text,"
        " created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, character_id, role, "hi", ts))
    conn.commit()


class TestPairs(unittest.TestCase):
    def test_pairs_from_history(self):
        conn = _memdb()
        _add_user(conn, "u1")
        _add_turn(conn, "u1", "chloe")
        _add_turn(conn, "u1", "rocky")
        pairs = proactive.get_all_user_character_pairs(conn)
        ids = {(p["user_id"], p["character_id"]) for p in pairs}
        self.assertIn(("u1", "chloe"), ids)
        self.assertIn(("u1", "rocky"), ids)
        conn.close()

    def test_no_pairs_without_user_turns(self):
        conn = _memdb()
        _add_user(conn, "u1")
        _add_turn(conn, "u1", "chloe", role="chloe")
        pairs = proactive.get_all_user_character_pairs(conn)
        self.assertEqual(pairs, [])
        conn.close()


class TestMorning(unittest.TestCase):
    def _morning_conn(self, last_turn_hours_ago=2):
        conn = _memdb()
        _add_user(conn, "u1")
        ts = (datetime.now() - timedelta(hours=last_turn_hours_ago))
        _add_turn(conn, "u1", "chloe", created_at=ts.isoformat())
        return conn

    def test_morning_due_at_815(self):
        conn = self._morning_conn(2)
        now = datetime.now(ZoneInfo("America/New_York")).replace(
            hour=8, minute=15, second=0, microsecond=0)
        naive = now.replace(tzinfo=None)
        self.assertTrue(proactive.should_send_morning_message(
            conn, "u1", "chloe", now=naive))
        conn.close()

    def test_morning_not_at_3pm(self):
        conn = self._morning_conn(2)
        now = datetime.now(ZoneInfo("America/New_York")).replace(
            hour=15, minute=0, second=0, microsecond=0)
        naive = now.replace(tzinfo=None)
        self.assertFalse(proactive.should_send_morning_message(
            conn, "u1", "chloe", now=naive))
        conn.close()

    def test_morning_not_twice(self):
        conn = self._morning_conn(2)
        now = datetime.now(ZoneInfo("America/New_York")).replace(
            hour=8, minute=15, second=0, microsecond=0)
        naive = now.replace(tzinfo=None)
        proactive._ensure_tables(conn)
        today = now.strftime("%Y-%m-%d")
        proactive._record_sent(conn, "u1", "chloe", "morning", today)
        self.assertFalse(proactive.should_send_morning_message(
            conn, "u1", "chloe", now=naive))
        conn.close()


class TestNudge(unittest.TestCase):
    def test_nudge_after_24h(self):
        conn = _memdb()
        _add_user(conn, "u1")
        ts = (datetime.now() - timedelta(hours=25)).isoformat()
        _add_turn(conn, "u1", "chloe", created_at=ts)
        self.assertTrue(proactive.should_send_nudge(conn, "u1", "chloe"))
        conn.close()

    def test_nudge_not_when_active(self):
        conn = _memdb()
        _add_user(conn, "u1")
        ts = (datetime.now() - timedelta(hours=1)).isoformat()
        _add_turn(conn, "u1", "chloe", created_at=ts)
        self.assertFalse(proactive.should_send_nudge(conn, "u1", "chloe"))
        conn.close()

    def test_nudge_not_twice_in_24h(self):
        conn = _memdb()
        _add_user(conn, "u1")
        ts = (datetime.now() - timedelta(hours=25)).isoformat()
        _add_turn(conn, "u1", "chloe", created_at=ts)
        proactive._ensure_tables(conn)
        conn.execute(
            "INSERT INTO proactive_state (user_id, character_id,"
            " last_nudge_at) VALUES (?, ?, datetime('now', '-1 hour'))",
            ("u1", "chloe"))
        conn.commit()
        self.assertFalse(proactive.should_send_nudge(conn, "u1", "chloe"))
        conn.close()


class TestGating(unittest.TestCase):
    def test_unpaid_no_messages_cannot_receive(self):
        conn = _memdb()
        _add_user(conn, "u1", messages_remaining=0, proactive_enabled=1)
        self.assertFalse(proactive.user_can_receive(conn, "u1"))
        conn.close()

    def test_messages_remaining_can_receive(self):
        conn = _memdb()
        _add_user(conn, "u1", messages_remaining=5, proactive_enabled=1)
        self.assertTrue(proactive.user_can_receive(conn, "u1"))
        conn.close()

    def test_opt_out_cannot_receive(self):
        conn = _memdb()
        _add_user(conn, "u1", messages_remaining=10, proactive_enabled=0)
        self.assertFalse(proactive.user_can_receive(conn, "u1"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
