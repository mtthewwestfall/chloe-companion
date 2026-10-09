"""Assistant capabilities tests: intent detection, reminder parsing, tier
gating, rollout gate, and the credential rule (nothing credential-shaped is
ever accepted or persisted here).

Run:  python -m unittest discover -s tests -v   (from the project root,
      with the venv's python)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist  # noqa: E402


class IntentTest(unittest.TestCase):
    def test_reminder(self):
        intent, _ = assist.detect_intent("remind me to call mom in 20 minutes")
        self.assertEqual(intent, "reminder")

    def test_email_draft(self):
        intent, _ = assist.detect_intent("draft an email to my boss about friday")
        self.assertEqual(intent, "email_draft")
        intent, _ = assist.detect_intent("write an email to sarah")
        self.assertEqual(intent, "email_draft")

    def test_job_search(self):
        intent, _ = assist.detect_intent("find me game designer jobs in austin")
        self.assertEqual(intent, "job_search")

    def test_web_search_question(self):
        intent, _ = assist.detect_intent("who won the game last night?")
        self.assertEqual(intent, "web_search")
        intent, _ = assist.detect_intent("what's the price of bitcoin right now")
        self.assertEqual(intent, "web_search")

    def test_no_lookup_about_her(self):
        self.assertIsNone(assist.detect_intent("do you remember my name?")[0])
        self.assertIsNone(assist.detect_intent("do you love me?")[0])
        self.assertIsNone(assist.detect_intent("tell me about yourself")[0])

    def test_plain_chat_no_intent(self):
        self.assertIsNone(assist.detect_intent("hey beautiful")[0])
        self.assertIsNone(assist.detect_intent("what did you do today")[0])


class ReminderParseTest(unittest.TestCase):
    TZ = ZoneInfo("America/New_York")

    def test_in_minutes(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=self.TZ)
        iso, text = assist.parse_reminder("remind me to stretch in 20 minutes",
                                          now=now)
        self.assertEqual(iso, "2026-10-01T16:20:00Z")
        self.assertEqual(text, "stretch")

    def test_tomorrow_at(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=self.TZ)
        iso, text = assist.parse_reminder("remind me tomorrow at 9am to call dad",
                                          now=now)
        self.assertEqual(iso, "2026-10-02T13:00:00Z")
        self.assertEqual(text, "call dad")

    def test_at_pm_rolls_to_tomorrow_when_passed(self):
        now = datetime(2026, 10, 1, 18, 0, tzinfo=self.TZ)
        iso, _ = assist.parse_reminder("remind me at 5pm to eat", now=now)
        self.assertEqual(iso, "2026-10-02T21:00:00Z")

    def test_no_time_understood(self):
        iso, text = assist.parse_reminder("remind me to buy milk")
        self.assertIsNone(iso)
        self.assertEqual(text, "buy milk")


class GateTest(unittest.TestCase):
    def setUp(self):
        self.old_rollout = os.environ.get("ASSIST_ROLLOUT")
        self.old_owners = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"

    def tearDown(self):
        for k, v in (("ASSIST_ROLLOUT", self.old_rollout),
                     ("ASSIST_OWNER_IDS", self.old_owners)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_owner_only_by_default(self):
        self.assertTrue(assist.enabled_for("owner_1", "basic"))
        self.assertFalse(assist.enabled_for("stranger", "basic"))

    def test_free_tier_gets_nothing(self):
        self.assertFalse(assist.enabled_for("owner_1", "free"))

    def test_paid_rollout_opens_paid_tiers(self):
        os.environ["ASSIST_ROLLOUT"] = "paid"
        self.assertTrue(assist.enabled_for("anyone", "plus"))
        self.assertFalse(assist.enabled_for("anyone", "free"))

    def test_tier_matrix_shape(self):
        self.assertEqual(assist.caps_for("basic")["web_search"], 20)
        self.assertFalse(assist.caps_for("basic")["email_drafts"])
        self.assertTrue(assist.caps_for("plus")["email_drafts"])
        self.assertTrue(assist.caps_for("vip")["job_lookup"])
        self.assertGreater(assist.caps_for("vip")["web_search"],
                           assist.caps_for("plus")["web_search"])


class ReminderStoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        assist.ensure_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_set_and_due(self):
        rid = assist.set_reminder(self.conn, "u1", "chloe", "stretch",
                                  "2020-01-01T00:00:00Z")
        self.assertGreater(rid, 0)
        due = assist.due_reminders(self.conn, "u1")
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["text"], "stretch")
        # Delivered ones don't come back.
        self.assertEqual(assist.due_reminders(self.conn, "u1"), [])

    def test_cancel(self):
        rid = assist.set_reminder(self.conn, "u1", "chloe", "x",
                                  "2030-01-01T00:00:00Z")
        self.assertTrue(assist.cancel_reminder(self.conn, "u1", rid))
        self.assertEqual(assist.list_reminders(self.conn, "u1"), [])

    def test_user_isolation(self):
        assist.set_reminder(self.conn, "u1", "chloe", "x",
                            "2020-01-01T00:00:00Z")
        self.assertEqual(assist.due_reminders(self.conn, "u2"), [])
        self.assertEqual(len(assist.due_reminders(self.conn, "u1")), 1)


class CredentialHygieneTest(unittest.TestCase):
    """The module must never accept or persist credential material."""

    def test_no_credential_params(self):
        import inspect
        bad = ("password", "passwd", "secret", "oauth", "refresh_token",
               "access_token", "bearer")
        for name, fn in inspect.getmembers(assist, inspect.isfunction):
            for param in inspect.signature(fn).parameters:
                pl = param.lower()
                self.assertFalse(
                    any(b in pl for b in bad),
                    f"assist.{name} takes credential-shaped param '{param}'")

    def test_no_credential_tables(self):
        conn = sqlite3.connect(":memory:")
        assist.ensure_tables(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in tables:
            cols = " ".join(
                r[1] for r in conn.execute(f"PRAGMA table_info({t})")).lower()
            self.assertNotIn("token", cols)
            self.assertNotIn("password", cols)
        conn.close()


if __name__ == "__main__":
    unittest.main()


class AppStatusTest(unittest.TestCase):
    def test_detect_status_phrases(self):
        for t in ("how's onefetch doing?", "what's the app status",
                  "how many founder spots left?", "check my apps",
                  "how is the gemini pool", "status of my apps"):
            self.assertTrue(assist.detect_app_status(t), t)

    def test_no_status_in_plain_chat(self):
        for t in ("hey beautiful", "how's your day", "i love you"):
            self.assertFalse(assist.detect_app_status(t), t)

    def test_detect_brief_phrases(self):
        self.assertTrue(assist.detect_owner_brief("catch me up"))
        self.assertTrue(assist.detect_owner_brief("what are my prices?"))
        self.assertFalse(assist.detect_owner_brief("hey beautiful"))

    def test_status_lines_real_numbers(self):
        s = {"onefetch": {"founder_count": 3, "founder_spots_left": 197,
                          "founder_open": True, "ai_available": True,
                          "site_status": 200},
             "roof_rover": {"shelf_leads": 24, "site_status": 200},
             "gemini": {"believed_healthy": 2,
                        "pool": {"healthy": 2, "cooling": 14},
                        "degraded": False}}
        txt = "\n".join(assist._status_lines(s))
        self.assertIn("3 founders", txt)
        self.assertIn("197 of 200", txt)
        self.assertIn("24 leads", txt)
        self.assertIn("2 keys believed healthy", txt)

    def test_status_lines_unreachable_honest(self):
        txt = "\n".join(assist._status_lines(
            {"onefetch": {}, "roof_rover": {}, "gemini": {}}))
        self.assertIn("OneFetch: UNREACHABLE", txt)
        self.assertIn("Roof Rover: UNREACHABLE", txt)

    def test_fetch_app_status_wires_sources(self):
        def fake_get(url, timeout=6):
            if url.endswith("/api/pricing"):
                return 200, {"founder_count": 1,
                             "founder_spots_left": 199,
                             "founder_open": True}
            if url.endswith("/api/ai/status"):
                return 200, {"available": True}
            if url.endswith("/api/store/leads"):
                return 200, [{"id": 1}, {"id": 2}]
            return None, None
        orig_get, orig_status = assist._http_get_json, assist._http_status
        orig_info = assist.gemini_keys.degradation_info
        try:
            assist._http_get_json = fake_get
            assist._http_status = lambda url, timeout=6: 200
            assist.gemini_keys.degradation_info = lambda: {
                "believed_healthy_key_ids": [8, 17],
                "pool": {"healthy": 2}, "degraded": False}
            s = assist.fetch_app_status()
        finally:
            assist._http_get_json, assist._http_status = orig_get, orig_status
            assist.gemini_keys.degradation_info = orig_info
        self.assertEqual(s["onefetch"]["founder_count"], 1)
        self.assertEqual(s["roof_rover"]["shelf_leads"], 2)
        self.assertEqual(s["gemini"]["believed_healthy"], 2)
