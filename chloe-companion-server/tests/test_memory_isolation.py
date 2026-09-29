"""Memory isolation test: user A's memories must NEVER surface for user B.

Run:  python -m unittest discover -s tests -v   (from the project root,
      with the venv's python)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import config, db, store  # noqa: E402


class MemoryIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)
        self.a = "user_A"
        self.b = "user_B"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_recall_never_leaks_across_users(self):
        store.write_memory(self.conn, self.a, "core", "A's secret name is Pineapple")
        store.write_memory(self.conn, self.b, "core", "B's secret name is Mango")

        a_mems = store.recall_memories(self.conn, self.a)
        b_mems = store.recall_memories(self.conn, self.b)

        self.assertEqual(len(a_mems), 1)
        self.assertEqual(len(b_mems), 1)
        self.assertIn("Pineapple", a_mems[0]["text"])
        self.assertIn("Mango", b_mems[0]["text"])
        # The critical assertions: no cross-contamination, by keyword too.
        self.assertEqual(store.recall_memories(self.conn, self.a, keyword="Mango"), [])
        self.assertEqual(store.recall_memories(self.conn, self.b, keyword="Pineapple"), [])
        for m in store.recall_memories(self.conn, self.a, limit=1000):
            self.assertNotIn("Mango", m["text"])

    def test_export_is_per_user(self):
        store.write_memory(self.conn, self.a, "preference", "A likes tea")
        store.write_memory(self.conn, self.b, "preference", "B likes coffee")
        exported_a = store.export_memories(self.conn, self.a)
        self.assertEqual(len(exported_a), 1)
        self.assertIn("tea", exported_a[0]["text"])

    def test_delete_only_deletes_that_user(self):
        store.write_memory(self.conn, self.a, "fact", "A fact")
        store.write_memory(self.conn, self.b, "fact", "B fact")
        deleted = store.delete_all_memories(self.conn, self.a)
        self.assertEqual(deleted, 1)
        self.assertEqual(store.export_memories(self.conn, self.a), [])
        self.assertEqual(len(store.export_memories(self.conn, self.b)), 1)

    def test_conversation_log_is_per_user(self):
        store.log_turn(self.conn, self.a, "user", "hello from A")
        store.log_turn(self.conn, self.b, "user", "hello from B")
        turns_a = store.recent_turns(self.conn, self.a)
        self.assertEqual(len(turns_a), 1)
        self.assertIn("A", turns_a[0]["text"])

    def test_unknown_user_gets_nothing(self):
        self.assertEqual(store.recall_memories(self.conn, "ghost_user"), [])
        self.assertEqual(store.export_memories(self.conn, "ghost_user"), [])


class BankAccountingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_bank_decrements_and_never_goes_negative(self):
        uid = "bank_user"
        store.get_or_create_user(self.conn, uid)
        self.conn.execute("UPDATE users SET messages_remaining = 1 WHERE user_id = ?", (uid,))
        self.conn.commit()
        self.assertTrue(store.consume_message(self.conn, uid))
        self.assertFalse(store.consume_message(self.conn, uid))  # empty -> False, no crash
        self.assertFalse(store.consume_message(self.conn, uid))
        row = self.conn.execute(
            "SELECT messages_remaining FROM users WHERE user_id = ?", (uid,)).fetchone()
        self.assertEqual(row["messages_remaining"], 0)

    def test_banks_are_per_user(self):
        store.get_or_create_user(self.conn, "u1")
        store.get_or_create_user(self.conn, "u2")
        self.conn.execute("UPDATE users SET messages_remaining = 5 WHERE user_id = 'u1'")
        self.conn.commit()
        store.consume_message(self.conn, "u1")
        r1 = self.conn.execute("SELECT messages_remaining FROM users WHERE user_id='u1'").fetchone()
        r2 = self.conn.execute("SELECT messages_remaining FROM users WHERE user_id='u2'").fetchone()
        self.assertEqual(r1["messages_remaining"], 4)
        self.assertEqual(r2["messages_remaining"], config.TIERS["basic"]["messages_per_month"])

    def test_tier_topup_does_not_stack(self):
        uid = "topup_user"
        store.get_or_create_user(self.conn, uid)
        self.conn.execute("UPDATE users SET messages_remaining = 12 WHERE user_id = ?", (uid,))
        self.conn.commit()
        store.set_tier(self.conn, uid, "plus")  # renewal
        row = self.conn.execute(
            "SELECT messages_remaining, tier FROM users WHERE user_id = ?", (uid,)).fetchone()
        self.assertEqual(row["messages_remaining"], config.TIERS["plus"]["messages_per_month"])
        self.assertEqual(row["tier"], "plus")


class SchedulerGuardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_proactive_tier_gated_and_capped(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        from app import scheduler
        # basic user: never eligible
        store.get_or_create_user(self.conn, "basic_u")
        # plus user at cap: never eligible
        store.set_tier(self.conn, "plus_capped", "plus")
        # Use the user's own timezone day so the daily counter isn't reset.
        today = dt.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        self.conn.execute(
            "UPDATE users SET proactive_day=?, proactive_sent_today=2 WHERE user_id='plus_capped'",
            (today,))
        self.conn.commit()
        eligible = scheduler.eligible_users(self.conn)
        uids = [u["user_id"] for u in eligible]
        self.assertNotIn("basic_u", uids)
        self.assertNotIn("plus_capped", uids)


class TrustIsolationTest(unittest.TestCase):
    """Trust posture: starts at M5, advances slowly, strictly per-user."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)
        from app import trust  # noqa: E402
        self.trust = trust
        self.a = "trust_A"
        self.b = "trust_B"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_new_user_starts_at_m5_never_lower(self):
        state = self.trust.get_state(self.conn, self.a)
        self.assertEqual(state["milestone"], self.trust.START_MILESTONE)
        self.assertEqual(state["milestone"], 5)
        self.assertGreaterEqual(state["milestone"], 5)

    def test_no_advance_before_stage_days_elapse(self):
        state, advanced = self.trust.maybe_advance(self.conn, self.a)
        self.assertFalse(advanced)
        self.assertEqual(state["milestone"], 5)

    def test_advance_after_stage_days_at_most_one_per_check(self):
        self.trust.get_state(self.conn, self.a)  # create the row first
        # 3 days at M5 (needs 2) -> advances to M6 only.
        self.conn.execute(
            "UPDATE trust_state SET stage_entered_at = datetime('now', '-3 days') "
            "WHERE user_id = ?", (self.a,))
        self.conn.commit()
        state, advanced = self.trust.maybe_advance(self.conn, self.a)
        self.assertTrue(advanced)
        self.assertEqual(state["milestone"], 6)
        # Even with 30 days banked, only one stage per check (M6 needs 3).
        self.conn.execute(
            "UPDATE trust_state SET stage_entered_at = datetime('now', '-30 days') "
            "WHERE user_id = ?", (self.a,))
        self.conn.commit()
        state, advanced = self.trust.maybe_advance(self.conn, self.a)
        self.assertTrue(advanced)
        self.assertEqual(state["milestone"], 7)

    def test_m8_is_terminal(self):
        self.trust.get_state(self.conn, "m8_user")
        self.conn.execute(
            "UPDATE trust_state SET milestone = 8, "
            "stage_entered_at = datetime('now', '-365 days') WHERE user_id = 'm8_user'")
        self.conn.commit()
        state, advanced = self.trust.maybe_advance(self.conn, "m8_user")
        self.assertFalse(advanced)
        self.assertEqual(state["milestone"], 8)

    def test_stage_days_dials_are_short(self):
        self.assertEqual(self.trust.STAGE_DAYS, [2, 3, 5])

    def test_revealed_facts_grow_with_milestone(self):
        f5 = self.trust.revealed_pinned_facts(5)
        f8 = self.trust.revealed_pinned_facts(8)
        self.assertEqual(len(f5), 6)
        self.assertEqual(len(f8), 21)
        self.assertTrue(any("necklace" in f for f in f5))  # surface set at M5
        # Cumulative: everything at M5 is still there at M8.
        for f in f5:
            self.assertIn(f, f8)

    def test_trust_state_is_per_user(self):
        self.conn.execute(
            "UPDATE trust_state SET stage_entered_at = datetime('now', '-3 days') "
            "WHERE user_id = ?", (self.a,))
        self.conn.commit()
        # Create both rows, advance only A.
        self.trust.get_state(self.conn, self.a)
        self.trust.get_state(self.conn, self.b)
        self.conn.execute(
            "UPDATE trust_state SET stage_entered_at = datetime('now', '-3 days') "
            "WHERE user_id = ?", (self.a,))
        self.conn.commit()
        state_a, _ = self.trust.maybe_advance(self.conn, self.a)
        state_b = self.trust.get_state(self.conn, self.b)
        self.assertEqual(state_a["milestone"], 6)
        self.assertEqual(state_b["milestone"], 5)


class BrainExtractionTest(unittest.TestCase):
    """Rule-based memory extraction: typed, conservative, deduped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)
        from app import brain  # noqa: E402
        self.brain = brain

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_extracts_name_core(self):
        self.assertEqual(
            self.brain.extract_memories("hey, my name is Theo"),
            [("core", "His name is Theo.")],
        )
        self.assertEqual(
            self.brain.extract_memories("call me Marcus"),
            [("core", "His name is Marcus.")],
        )

    def test_name_guards(self):
        # Negated "call me" is not a name; lowercase "i am X" is not a name.
        self.assertEqual(self.brain.extract_memories("i am tired"), [])
        mems = self.brain.extract_memories("don't call me buddy")
        self.assertFalse(any(m[0] == "core" for m in mems))

    def test_extracts_preference_and_boundary(self):
        mems = self.brain.extract_memories("i love late night drives")
        self.assertIn(("preference", "He likes late night drives."), mems)
        mems = self.brain.extract_memories("don't call me buddy")
        self.assertTrue(any(m[0] == "boundary" for m in mems))

    def test_learn_from_turn_writes_and_dedupes(self):
        n1 = self.brain.learn_from_turn(self.conn, "u1", "my name is Theo")
        n2 = self.brain.learn_from_turn(self.conn, "u1", "my name is Theo")
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)  # duplicate not written twice
        cores = store.recall_memories(self.conn, "u1", mem_type="core")
        self.assertEqual(len(cores), 1)

    def test_learn_is_per_user(self):
        self.brain.learn_from_turn(self.conn, "uA", "my name is Theo")
        self.assertEqual(store.recall_memories(self.conn, "uB"), [])

    def test_digest_refreshes(self):
        self.brain.learn_from_turn(self.conn, "u1", "my name is Theo")
        digest = self.brain.get_digest(self.conn, "u1")
        self.assertIn("Theo", digest)

    def test_brain_context_has_all_layers(self):
        self.brain.learn_from_turn(self.conn, "u1", "my name is Theo")
        user = store.get_or_create_user(self.conn, "u1")
        ctx = self.brain.build_brain_context(self.conn, "u1", user, "hey chloe")
        for key in ("trust_line", "trust_stage", "conduct", "pinned_facts",
                    "rolling_summary", "relevant_memories", "recent", "name", "tier"):
            self.assertIn(key, ctx)
        self.assertEqual(len(ctx["pinned_facts"]), 6)  # M5 surface set
        self.assertIn("M5", ctx["trust_stage"])


if __name__ == "__main__":
    unittest.main()
