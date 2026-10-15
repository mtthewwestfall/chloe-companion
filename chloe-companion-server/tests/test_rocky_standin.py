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


if __name__ == "__main__":
    unittest.main()
