"""Proactive morning-priority tests (2026-10-19).

The 2026-10-19 incident: two nudges fired at 00:00 local and ate the
whole 2/day cap, so the owner's 8am morning message never fired; a
Gemini storm covered the rest of the window. These tests pin the fix,
all enforced in proactive.deliver_proactive_message:

  1. Morning has priority: it is exempt from the ambient-class daily
     ceiling (its own per-user ceiling applies), and ambient-class
     sends wait while today's morning window is open and no morning
     message has been delivered to that user yet.
  2. No midnight ambient nudges: nudge/ambient sends only land
     09:00-21:00 in the user's local timezone.
  3. In-window retry: a failed morning generation or send never
     records the day as sent, so later ticks inside the window retry;
     the generic morning line is used only in the window's last
     minutes so a storm can delay but never silence the message.
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from app import proactive

NY = ZoneInfo("America/New_York")
DAY = "2026-10-20"  # a Tuesday, EDT


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
        attachments TEXT DEFAULT '[]',
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


def _local(hour, minute=0):
    return datetime(2026, 10, 20, hour, minute, tzinfo=NY)


class TestMorningPriority(unittest.TestCase):
    def setUp(self):
        self.conn = _memdb()
        _add_user(self.conn, "u1")
        proactive._ensure_tables(self.conn)
        self._orig_local = proactive._user_local_now
        self._orig_live = proactive._generate_proactive_message_live
        self._orig_gen = proactive.generate_proactive_message
        self._orig_tg = proactive.send_telegram_message
        proactive._generate_proactive_message_live = \
            lambda *a, **k: "live line"
        proactive.generate_proactive_message = lambda *a, **k: "gen line"

    def tearDown(self):
        proactive._user_local_now = self._orig_local
        proactive._generate_proactive_message_live = self._orig_live
        proactive.generate_proactive_message = self._orig_gen
        proactive.send_telegram_message = self._orig_tg
        self.conn.close()

    # -- helpers ------------------------------------------------------
    def _at(self, hour, minute=0):
        fixed = _local(hour, minute)
        proactive._user_local_now = lambda tz=None, now=None: fixed

    def _sent_today(self, user_id="u1"):
        row = self.conn.execute(
            "SELECT proactive_sent_today FROM users WHERE user_id = ?",
            (user_id,)).fetchone()
        return row["proactive_sent_today"]

    def _outbox(self, user_id="u1"):
        return self.conn.execute(
            "SELECT * FROM proactive_outbox WHERE user_id = ? ORDER BY id",
            (user_id,)).fetchall()

    def _morning_state(self, user_id="u1", character_id="chloe"):
        row = self.conn.execute(
            "SELECT last_morning_date FROM proactive_state "
            "WHERE user_id = ? AND character_id = ?",
            (user_id, character_id)).fetchone()
        return row["last_morning_date"] if row else ""

    def _deliver(self, mtype, user_id="u1"):
        return proactive.deliver_proactive_message(
            self.conn, user_id, "chloe", mtype)

    # -- part 2: reasonable hours --------------------------------------
    def test_midnight_nudge_blocked_and_consumes_nothing(self):
        self._at(0, 30)
        self.assertFalse(self._deliver("nudge"))
        self.assertEqual(self._sent_today(), 0)
        self.assertEqual(self._outbox(), [])
        # ...and the morning message still fires in its window.
        self._at(8, 30)
        self.assertTrue(self._deliver("morning"))
        self.assertEqual(self._morning_state(), DAY)

    def test_late_night_sends_blocked(self):
        self._at(22, 30)
        self.assertFalse(self._deliver("ambient"))
        self._at(5, 0)
        self.assertFalse(self._deliver("nudge"))
        self.assertEqual(self._sent_today(), 0)

    # -- part 1: morning priority over the cap -------------------------
    def test_morning_exempt_from_full_ambient_cap(self):
        # The incident shape: the day's counter is already at the cap
        # before the window opens. The morning message must still go.
        self.conn.execute(
            "UPDATE users SET proactive_day = ?, proactive_sent_today = 2 "
            "WHERE user_id = 'u1'", (DAY,))
        self.conn.commit()
        self._at(8, 30)
        self.assertTrue(self._deliver("morning"))
        self.assertEqual(self._morning_state(), DAY)

    def test_nudge_held_until_morning_delivered(self):
        self._at(9, 30)  # inside the window, morning not yet sent
        self.assertFalse(self._deliver("nudge"))
        self.assertEqual(self._sent_today(), 0)
        self.assertTrue(self._deliver("morning"))
        # Morning delivered — ambient-class slots unlock.
        self.assertTrue(self._deliver("nudge"))
        self.assertEqual(self._sent_today(), 2)

    def test_ambient_ceiling_still_two_per_day(self):
        self._at(12, 0)  # window long past
        self.assertTrue(self._deliver("nudge"))
        self.assertTrue(self._deliver("ambient"))
        self.assertFalse(self._deliver("nudge"))
        self.assertEqual(self._sent_today(), 2)

    def test_morning_sends_do_not_consume_ambient_ceiling(self):
        self._at(8, 30)
        self.assertTrue(self._deliver("morning"))
        self._at(12, 0)
        self.assertTrue(self._deliver("nudge"))
        self.assertTrue(self._deliver("ambient"))
        self.assertFalse(self._deliver("nudge"))
        self.assertEqual(self._sent_today(), 3)

    # -- part 3: in-window retry ---------------------------------------
    def test_telegram_morning_failure_retries(self):
        _add_user(self.conn, "tg_4242")
        self._at(8, 30)
        proactive.send_telegram_message = lambda *a, **k: False
        self.assertFalse(self._deliver("morning", user_id="tg_4242"))
        # The failed send burned nothing: no sent state, no counter.
        self.assertEqual(self._morning_state("tg_4242"), "")
        self.assertEqual(self._sent_today("tg_4242"), 0)
        self.assertEqual(self._outbox("tg_4242")[0]["status"], "failed")
        proactive.send_telegram_message = lambda *a, **k: True
        self.assertTrue(self._deliver("morning", user_id="tg_4242"))
        self.assertEqual(self._morning_state("tg_4242"), DAY)

    def test_morning_generation_failure_retries_in_window(self):
        proactive._generate_proactive_message_live = lambda *a, **k: None
        self._at(8, 30)
        self.assertFalse(self._deliver("morning"))
        self.assertEqual(self._outbox(), [])
        self.assertEqual(self._sent_today(), 0)
        self.assertEqual(self._morning_state(), "")
        proactive._generate_proactive_message_live = \
            lambda *a, **k: "a real good morning"
        self.assertTrue(self._deliver("morning"))
        self.assertEqual(self._morning_state(), DAY)

    def test_morning_generation_fallback_at_window_end(self):
        proactive._generate_proactive_message_live = lambda *a, **k: None
        self._at(9, 57)  # last minutes of the 8-10am window
        self.assertTrue(self._deliver("morning"))
        rows = self._outbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"],
                         "good morning. hope today treats you right.")

    def test_generate_wrapper_fallback_unchanged(self):
        proactive.generate_proactive_message = self._orig_gen
        proactive._generate_proactive_message_live = lambda *a, **k: None
        self.assertEqual(
            proactive.generate_proactive_message(
                self.conn, "u1", "chloe", "nudge"),
            "hey — it's been a while. you good?")
        self.assertEqual(
            proactive.generate_proactive_message(
                self.conn, "u1", "chloe", "morning"),
            "good morning. hope today treats you right.")


if __name__ == "__main__":
    unittest.main()
