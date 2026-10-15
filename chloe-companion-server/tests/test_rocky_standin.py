"""Rocky the stand-in (2026-10-15): the operator context pack,
Rocky's live status board, the Gemini-pool picture desk route,
and the browse-lane split (local worker vs the Oracle VM browser).

Stage 1 pins live here; later stages append their own classes.
All network seams are mocked — zero real calls.

Run:  python -m unittest tests.test_rocky_standin -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, brain, db  # noqa: E402

OWNER = {"user_id": "owner_1", "tier": "basic"}


def _ctx(cid: str) -> dict:
    return {"character": {"id": cid, "name": cid.title(), "persona": ""},
            "name": "Matthew", "recent": []}


class RockyContextPackTest(unittest.TestCase):
    def setUp(self):
        brain._ROCKY_CONTEXT_CACHE["text"] = None

    def test_pack_file_loads_with_key_facts(self):
        pack = brain._rocky_context_pack()
        self.assertTrue(pack)
        for marker in ("OPERATOR CONTEXT PACK", "$49.99", "$99.99",
                       "Winston", "credits", "The Unknown Girl",
                       "The Known Girl", "Ashen Oath"):
            self.assertIn(marker, pack)

    def test_pack_injected_for_rocky_only(self):
        rocky_text = brain._gemini_system_text(_ctx("rocky"), "rocky")
        self.assertIn("OPERATOR CONTEXT PACK", rocky_text)
        self.assertIn("OPERATOR CONTEXT PACK", rocky_text)
        for other in ("hannah", "chloe", "mia", "jordan", "jake"):
            other_text = brain._gemini_system_text(_ctx(other), other)
            self.assertNotIn("OPERATOR CONTEXT PACK", other_text,
                             f"pack leaked into {other}'s system text")

    def test_pack_missing_fails_soft(self):
        with mock.patch("builtins.open", side_effect=OSError("gone")):
            brain._ROCKY_CONTEXT_CACHE["text"] = None
            self.assertEqual(brain._rocky_context_pack(), "")
            brain._ROCKY_CONTEXT_CACHE["text"] = None
            text = brain._gemini_system_text(_ctx("rocky"), "rocky")
            self.assertIn("You are Rocky", text)
            self.assertNotIn("OPERATOR CONTEXT PACK", text)


FAKE_STATUS = {"onefetch": {"founder_count": 3, "founder_spots_left": 197,
                             "founder_open": True, "ai_available": True,
                             "site_status": 200},
               "roof_rover": {"shelf_leads": 5, "site_status": 200},
               "gemini": {"believed_healthy": 2,
                          "pool": {"healthy": 2}, "degraded": False}}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    assist.ensure_tables(conn)
    return conn


class RockyStatusBoardTest(unittest.TestCase):
    """Stage 4: Rocky's status answer is the full board — project
    lines + Whispr numbers + the Sogni render queue — read live."""

    def setUp(self):
        self.conn = _conn()
        patcher = mock.patch.object(assist, "fetch_app_status",
                                    return_value=FAKE_STATUS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rocky_status_includes_whispr_and_render_queue(self):
        brief = assist._status_brief(self.conn, "rocky")
        self.assertIn("Gemini key pool", brief)
        self.assertIn("Whispr:", brief)
        self.assertIn("Sogni render queue: empty", brief)

    def test_render_queue_counts_waiting_jobs(self):
        for status in ("pending", "pending", "generating", "approved"):
            self.conn.execute(
                "INSERT INTO give_generations (girl_id, stage, prompt,"
                " day, status) VALUES ('mia', 2, 'p', '2026-10-15', ?)",
                (status,))
        self.conn.commit()
        lines = assist._render_queue_lines(self.conn)
        self.assertEqual(len(lines), 1)
        self.assertIn("3 give render(s)", lines[0])
        self.assertIn("pending: 2", lines[0])
        self.assertIn("generating: 1", lines[0])
        brief = assist._status_brief(self.conn, "rocky")
        self.assertIn("Sogni render queue: 3", brief)

    def test_render_queue_fails_soft(self):
        self.conn.execute("DROP TABLE give_generations")
        lines = assist._render_queue_lines(self.conn)
        self.assertIn("UNREACHABLE", lines[0])

    def test_girls_status_unchanged_by_the_board(self):
        hannah = assist._status_brief(self.conn, "hannah")
        self.assertNotIn("Sogni render queue", hannah)
        mia = assist._status_brief(self.conn, "mia")
        self.assertIn("Whispr:", mia)
        self.assertNotIn("Sogni render queue", mia)


if __name__ == "__main__":
    unittest.main()
