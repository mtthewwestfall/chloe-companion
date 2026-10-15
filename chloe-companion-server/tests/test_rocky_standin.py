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


class RockyPoolImageDeskTest(unittest.TestCase):
    """Stage 5: Rocky can send an image job to the owner's Gemini
    pool by naming it, and the file comes back in chat. The girls'
    routing is untouched."""

    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        self.conn = _conn()

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def test_detect_needs_hint_and_cleans_prompt(self):
        out = assist.detect_pool_image_request(
            "make an image on my gemini of a red barn at dusk")
        self.assertIsNotNone(out)
        self.assertIn("red barn", out)
        self.assertNotIn("gemini", out.lower())
        self.assertIsNone(assist.detect_pool_image_request(
            "make an image of a red barn at dusk"))
        self.assertIsNone(assist.detect_pool_image_request(
            "check the gemini pool status"))
        out2 = assist.detect_pool_image_request(
            "run an image job on the runner: a lighthouse in fog")
        self.assertIsNotNone(out2)
        self.assertIn("lighthouse", out2)

    def test_rocky_dispatch_returns_image_in_chat(self):
        fake = {"status": "ok", "image_url": "/generated/barn.png",
                "model": "gemini-2.5-flash-image", "latency_s": 0.1,
                "bytes": b"", "mime_type": "image/png"}
        with mock.patch.object(assist, "_generate_gemini_image",
                               return_value=fake):
            hit = assist.maybe_assist(
                self.conn, OWNER,
                "make an image on my gemini of a red barn", "rocky")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.get("image_url"), "/generated/barn.png")
        self.assertNotIn("gemini", (hit.get("image_prompt") or ""))

    def test_hannah_never_reaches_the_pool_desk(self):
        with mock.patch.object(assist, "_handle_sogni_lane",
                               return_value={"brief": "sogni lane"}) as lane, \
                mock.patch.object(assist, "_handle_image_request") as desk:
            hit = assist.maybe_assist(
                self.conn, OWNER,
                "make an image on my gemini of a red barn", "hannah")
        self.assertEqual(hit, {"brief": "sogni lane"})
        lane.assert_called_once()
        desk.assert_not_called()


class RockyBrowseLaneTest(unittest.TestCase):
    """Stage 6: browse jobs carry a lane — 'local' for the worker on
    his main machine, 'ovm' for the Oracle VM browser with his
    seeded sessions. Each worker claims only its own lane."""

    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        self.conn = _conn()

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def test_detect_routes_ovm_phrasing(self):
        det = assist.detect_browse_request(
            "open https://example.com in the browser on the oracle vm")
        self.assertIsNotNone(det)
        self.assertEqual(det["lane"], "ovm")
        det2 = assist.detect_browse_request(
            "screenshot https://example.com in the browser")
        self.assertIsNotNone(det2)
        self.assertEqual(det2["lane"], "local")

    def test_claims_are_lane_isolated(self):
        local_id = assist.enqueue_browse_job(
            self.conn, "owner_1", "rocky", "https://a.example",
            "read", None)
        ovm_id = assist.enqueue_browse_job(
            self.conn, "owner_1", "rocky", "https://b.example",
            "read", None, lane="ovm")
        job = assist.claim_browse_job(self.conn)
        self.assertEqual(job["id"], local_id)
        self.assertEqual(job["lane"], "local")
        job = assist.claim_browse_job(self.conn, "ovm")
        self.assertEqual(job["id"], ovm_id)
        self.assertEqual(job["lane"], "ovm")
        self.assertIsNone(assist.claim_browse_job(self.conn))
        self.assertIsNone(assist.claim_browse_job(self.conn, "ovm"))
        self.assertIsNone(assist.claim_browse_job(self.conn, "bogus"))

    def test_lane_column_migrates_onto_old_tables(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        conn.execute(
            "CREATE TABLE browse_jobs ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id TEXT NOT NULL,"
            " character_id TEXT NOT NULL DEFAULT 'rocky',"
            " url TEXT NOT NULL,"
            " action TEXT NOT NULL DEFAULT 'read',"
            " question TEXT,"
            " status TEXT NOT NULL DEFAULT 'queued',"
            " title TEXT, final_url TEXT, result_text TEXT,"
            " image_url TEXT, image_name TEXT, error TEXT,"
            " created_at TEXT NOT NULL, claimed_at TEXT,"
            " finished_at TEXT,"
            " delivered INTEGER NOT NULL DEFAULT 0)")
        conn.execute(
            "INSERT INTO browse_jobs (user_id, url, created_at)"
            " VALUES ('owner_1', 'https://old.example',"
            " '2026-10-01T00:00:00Z')")
        conn.commit()
        assist.ensure_tables(conn)
        job = assist.claim_browse_job(conn)
        self.assertIsNotNone(job)
        self.assertEqual(job["url"], "https://old.example")
        self.assertIsNone(assist.claim_browse_job(conn, "ovm"))
        conn.close()

    def test_rocky_dispatch_queues_ovm_lane(self):
        hit = assist.maybe_assist(
            self.conn, OWNER,
            "read https://example.com in the browser on the oracle vm",
            "rocky")
        self.assertIsNotNone(hit)
        self.assertIn("Oracle VM", hit["brief"])
        self.assertIsNone(assist.claim_browse_job(self.conn))
        job = assist.claim_browse_job(self.conn, "ovm")
        self.assertIsNotNone(job)
        self.assertEqual(job["url"], "https://example.com")


if __name__ == "__main__":
    unittest.main()
