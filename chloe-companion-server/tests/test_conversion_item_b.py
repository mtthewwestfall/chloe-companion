"""Conversion fix Item B tests (2026-10-18): the new-user win-back gate.

Eligibility, exactly as specced: a genuine free user (not owner, not
test/QA, not the probe, never paid, basic tier) with 1-49 lifetime
messages, signed up within the last 14 days, whose last user turn was
>= 24h ago, who has never been win-backed, and who has no retention
send already today. Boundary cases pinned one by one.

Run:  python -m pytest tests/test_conversion_item_b.py -q
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from app import config, proactive

NOW = datetime(2026, 10, 18, 17, 0, 0)  # naive UTC, like stored stamps


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE users (
        user_id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        email TEXT,
        tier TEXT DEFAULT 'basic',
        messages_remaining INTEGER DEFAULT 0,
        messages_used_total INTEGER DEFAULT 0,
        proactive_enabled INTEGER DEFAULT 0,
        timezone TEXT DEFAULT 'America/New_York',
        proactive_day TEXT DEFAULT '',
        proactive_sent_today INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
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


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _add_user(conn, user_id="u_new", total=5, created_days_ago=2,
              last_turn_hours_ago=30, **kw):
    d = dict(user_id=user_id, name="Sam", email="sam@realmail.org",
             tier="basic", messages_remaining=45,
             messages_used_total=total, proactive_enabled=1,
             timezone="America/New_York", proactive_day="",
             proactive_sent_today=0,
             created_at=_fmt(NOW - timedelta(days=created_days_ago)))
    d.update(kw)
    cols = ", ".join(d.keys())
    vals = ", ".join(["?"] * len(d))
    conn.execute(f"INSERT INTO users ({cols}) VALUES ({vals})",
                 tuple(d.values()))
    if last_turn_hours_ago is not None:
        conn.execute(
            "INSERT INTO conversation (user_id, character_id, role, "
            "text, created_at) VALUES (?, 'chloe', 'user', 'hi', ?)",
            (user_id, _fmt(NOW - timedelta(hours=last_turn_hours_ago))))
        conn.execute(
            "INSERT INTO conversation (user_id, character_id, role, "
            "text, created_at) VALUES (?, 'chloe', 'chloe', 'hey', ?)",
            (user_id, _fmt(NOW - timedelta(hours=last_turn_hours_ago))))
    conn.commit()
    return user_id


class WinbackGate(unittest.TestCase):
    def setUp(self):
        self.conn = _memdb()
        self._old_owners = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"

    def tearDown(self):
        if self._old_owners is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._old_owners
        self.conn.close()

    def _due(self, user_id="u_new", **kw):
        uid = _add_user(self.conn, user_id, **kw)
        return proactive.should_send_winback(self.conn, uid, "chloe",
                                             now=NOW)

    def test_base_case_is_due(self):
        self.assertTrue(self._due())

    def test_zero_messages_is_not_due(self):
        self.assertFalse(self._due(total=0))

    def test_one_message_is_due(self):
        self.assertTrue(self._due(total=1))

    def test_49_messages_is_due(self):
        self.assertTrue(self._due(total=49))

    def test_50_messages_is_not_due(self):
        self.assertFalse(self._due(total=50))

    def test_signup_14_days_ago_is_due(self):
        self.assertTrue(self._due(created_days_ago=14))

    def test_signup_15_days_ago_is_not_due(self):
        self.assertFalse(self._due(created_days_ago=15))

    def test_idle_24h_is_due(self):
        self.assertTrue(self._due(last_turn_hours_ago=24))

    def test_idle_23h_is_not_due(self):
        self.assertFalse(self._due(last_turn_hours_ago=23))

    def test_no_turns_is_not_due(self):
        self.assertFalse(self._due(last_turn_hours_ago=None))

    def test_owner_is_not_due(self):
        self.assertFalse(self._due(user_id="owner_1"))

    def test_probe_is_not_due(self):
        self.assertFalse(self._due(user_id="probe_wh_1"))

    def test_test_email_is_not_due(self):
        self.assertFalse(self._due(email="qa@mailinator.com"))

    def test_paid_user_is_not_due(self):
        uid = _add_user(self.conn)
        self.conn.execute("INSERT INTO trial_grants (user_id) VALUES (?)",
                          (uid,))
        self.conn.commit()
        self.assertFalse(proactive.should_send_winback(
            self.conn, uid, "chloe", now=NOW))

    def test_non_basic_tier_is_not_due(self):
        self.assertFalse(self._due(tier="plus"))

    def test_already_winbacked_is_not_due(self):
        uid = _add_user(self.conn)
        proactive._record_retention(self.conn, uid, "chloe", "winback",
                                    "2026-10-10")
        self.assertFalse(proactive.should_send_winback(
            self.conn, uid, "chloe", now=NOW))

    def test_retention_send_today_blocks(self):
        uid = _add_user(self.conn)
        local_today = proactive._user_local_now(
            "America/New_York", NOW).date().isoformat()
        proactive._record_retention(self.conn, uid, "chloe", "warmhour",
                                    local_today)
        self.assertFalse(proactive.should_send_winback(
            self.conn, uid, "chloe", now=NOW))

    def test_flag_off_is_not_due(self):
        uid = _add_user(self.conn)
        old = config.CONV_WINBACK
        config.CONV_WINBACK = False
        try:
            self.assertFalse(proactive.should_send_winback(
                self.conn, uid, "chloe", now=NOW))
        finally:
            config.CONV_WINBACK = old

    def test_record_sent_marks_winback_once(self):
        uid = _add_user(self.conn)
        self.assertTrue(proactive.should_send_winback(
            self.conn, uid, "chloe", now=NOW))
        proactive._record_sent(self.conn, uid, "chloe", "winback",
                               "2026-10-18")
        self.assertFalse(proactive.should_send_winback(
            self.conn, uid, "chloe", now=NOW))
        row = proactive._retention_row(self.conn, uid, "winback")
        self.assertIsNotNone(row)
        self.assertEqual(row["sent_local_date"], "2026-10-18")


if __name__ == "__main__":
    unittest.main()
