"""Proactive ambient master-switch tests (2026-10-22).

Roof ordered the nudge system OFF after it kept burning Gemini keys.
PROACTIVE_AMBIENT=off must refuse every non-morning proactive send
(nudge / ambient / winback / warmhour) inside
proactive.deliver_proactive_message BEFORE any generation happens —
a refused send queues nothing, records nothing, and costs zero API
calls. The morning message is a separate class and keeps working.
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


class TestAmbientOff(unittest.TestCase):
    def setUp(self):
        self.conn = _memdb()
        _add_user(self.conn, "u1")
        proactive._ensure_tables(self.conn)
        self._orig_local = proactive._user_local_now
        self._orig_live = proactive._generate_proactive_message_live
        self._orig_gen = proactive.generate_proactive_message
        self._orig_env = os.environ.get("PROACTIVE_AMBIENT")
        proactive._generate_proactive_message_live = \
            lambda *a, **k: "live line"
        proactive.generate_proactive_message = lambda *a, **k: "gen line"
        self.gen_calls = []
        orig_gen = proactive.generate_proactive_message
        def counting_gen(*a, **k):
            self.gen_calls.append((a, k))
            return orig_gen(*a, **k)
        proactive.generate_proactive_message = counting_gen

    def tearDown(self):
        proactive._user_local_now = self._orig_local
        proactive._generate_proactive_message_live = self._orig_live
        proactive.generate_proactive_message = self._orig_gen
        if self._orig_env is None:
            os.environ.pop("PROACTIVE_AMBIENT", None)
        else:
            os.environ["PROACTIVE_AMBIENT"] = self._orig_env
        self.conn.close()

    def _at(self, hour, minute=0):
        fixed = datetime(2026, 10, 22, hour, minute, tzinfo=NY)
        proactive._user_local_now = lambda tz=None, now=None: fixed

    def _deliver(self, mtype, user_id="u1"):
        return proactive.deliver_proactive_message(
            self.conn, user_id, "chloe", mtype)

    def _outbox(self, user_id="u1"):
        return self.conn.execute(
            "SELECT * FROM proactive_outbox WHERE user_id = ? ORDER BY id",
            (user_id,)).fetchall()

    def test_switch_parsing(self):
        for val, expected in (("off", False), ("OFF", False),
                              ("0", False), ("false", False),
                              ("on", True), ("1", True)):
            os.environ["PROACTIVE_AMBIENT"] = val
            self.assertEqual(proactive._ambient_enabled(), expected, val)
        os.environ.pop("PROACTIVE_AMBIENT", None)
        self.assertTrue(proactive._ambient_enabled())

    def test_off_refuses_all_ambient_class_and_generates_nothing(self):
        os.environ["PROACTIVE_AMBIENT"] = "off"
        self._at(10, 30)  # inside reasonable hours, past morning window
        for mtype in ("nudge", "ambient", "winback", "warmhour"):
            self.assertFalse(self._deliver(mtype), mtype)
        self.assertEqual(self._outbox(), [])
        self.assertEqual(self.gen_calls, [])

    def test_off_leaves_morning_alone(self):
        os.environ["PROACTIVE_AMBIENT"] = "off"
        self._at(8, 30)  # inside the morning window
        self.assertTrue(self._deliver("morning"))
        self.assertEqual(len(self._outbox()), 1)

    def test_on_by_default_nudge_still_sends(self):
        os.environ.pop("PROACTIVE_AMBIENT", None)
        self._at(8, 30)
        self.assertTrue(self._deliver("morning"))
        self._at(10, 30)
        self.assertTrue(self._deliver("nudge"))


if __name__ == "__main__":
    unittest.main()
