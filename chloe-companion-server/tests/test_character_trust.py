"""Per-character trust tests (2026-10-16): each girl's own relationship clock.

Run: python -m unittest discover -s tests -v (from the project root,
     with the venv's python)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import character_trust, db, store, trust  # noqa: E402


class CharacterTrustTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)
        self.u = "ctrust_user"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_new_pair_starts_at_m5(self):
        state = character_trust.get_state(self.conn, self.u, "mara")
        self.assertEqual(state["milestone"], 5)
        self.assertEqual(state["character_id"], "mara")

    def test_trust_is_per_character(self):
        # Chloe advances; Mara stays at M5.
        character_trust.get_state(self.conn, self.u, "chloe")
        character_trust.get_state(self.conn, self.u, "mara")
        self.conn.execute(
            "UPDATE trust_state SET stage_entered_at = datetime('now', '-3 days') "
            "WHERE user_id = ? AND character_id = 'chloe'", (self.u,))
        self.conn.commit()
        state_chloe, advanced = character_trust.maybe_advance(self.conn, self.u, "chloe")
        state_mara = character_trust.get_state(self.conn, self.u, "mara")
        self.assertTrue(advanced)
        self.assertEqual(state_chloe["milestone"], 6)
        self.assertEqual(state_mara["milestone"], 5)

    def test_advance_writes_evolution_beat(self):
        character_trust.get_state(self.conn, self.u, "chloe")
        self.conn.execute(
            "UPDATE trust_state SET stage_entered_at = datetime('now', '-3 days') "
            "WHERE user_id = ? AND character_id = 'chloe'", (self.u,))
        self.conn.commit()
        _, advanced = character_trust.maybe_advance(self.conn, self.u, "chloe")
        self.assertTrue(advanced)
        beats = store.recall_memories(
            self.conn, self.u, mem_type="tone", character_id="chloe")
        self.assertTrue(any("M6" in b["text"] for b in beats))
        # Beat is per-character: Mara has none.
        mara_beats = store.recall_memories(
            self.conn, self.u, mem_type="tone", character_id="mara")
        self.assertEqual(mara_beats, [])

    def test_reset_for_character_only(self):
        character_trust.get_state(self.conn, self.u, "chloe")
        character_trust.get_state(self.conn, self.u, "mara")
        self.conn.execute(
            "UPDATE trust_state SET milestone = 7 "
            "WHERE user_id = ? AND character_id = 'chloe'", (self.u,))
        self.conn.commit()
        character_trust.reset_for_character(self.conn, self.u, "chloe")
        # Chloe reset to M5 on next get_state; Mara untouched.
        chloe = character_trust.get_state(self.conn, self.u, "chloe")
        self.assertEqual(chloe["milestone"], 5)
        row = self.conn.execute(
            "SELECT milestone FROM trust_state WHERE user_id = ? AND character_id = 'mara'",
            (self.u,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["milestone"], 5)

    def test_global_trust_untouched(self):
        # trust.py's global per-user trust still works (backward compat).
        state = trust.get_state(self.conn, self.u)
        self.assertEqual(state["milestone"], 5)


if __name__ == "__main__":
    unittest.main()
