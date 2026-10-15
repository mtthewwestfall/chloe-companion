"""Give-queue drain order (2026-10-14): a trigger's fresh batch
must not jump the pending backlog, and a park stand-down must not
strand the rest of a batch in the in-memory hand-in deque.

Production failure (2026-10-14): the 20:41Z batch (#3990-#4016)
was queued into the processor's hand-in deque; its third row
(#3990) hit the Sogni reconnection throttle, parked, and the
processor stood down — leaving the batch's remaining ids in the
in-memory deque only. Two deploys wiped the deque, and from then
on those rows were reachable solely by the DB sweep, which the
deque-first selection only consults when the deque is empty —
so every fresh trigger batch (boot, 12h loop, admin) cut in
line ahead of them and the backlog never drained.

The fix: the processor selects work in global id order from the
DB (oldest unrendered pending row first) for all trigger / boot /
sweep work; only the single-id hand-off spawn_give_generation_job
uses (assist's chat-driven studio renders, which poll for their
row) keeps a priority lane.
"""
import sqlite3
import threading
import time
import unittest
from unittest import mock

from app import assist, db, give_gen


def _db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db._migrate_character_columns(conn)
    db._migrate_auth_columns(conn)
    db._migrate_submission_columns(conn)
    db._migrate_creator_columns(conn)
    db._migrate_owner_column(conn)
    db._migrate_referral_columns(conn)
    db._migrate_conversation_attachments(conn)
    db._migrate_girl_media_stage(conn)
    conn.executescript(db.SCHEMA)
    return conn


class DrainOrderTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        self._nc = mock.MagicMock(wraps=self.conn)
        self._nc.close = mock.MagicMock()
        give_gen._PENDING_GIVE_IDS.clear()
        if hasattr(give_gen, "_PRIORITY_GIVE_IDS"):
            give_gen._PRIORITY_GIVE_IDS.clear()
        with give_gen._PROCESSOR_GUARD:
            give_gen._GIVE_PROCESSOR_ACTIVE = False
        while assist.live_render_active():
            assist.note_live_render_end()
        self.ran: list[int] = []
        self.park_ids: set[int] = set()

    def tearDown(self):
        while assist.live_render_active():
            assist.note_live_render_end()
        give_gen._PENDING_GIVE_IDS.clear()
        if hasattr(give_gen, "_PRIORITY_GIVE_IDS"):
            give_gen._PRIORITY_GIVE_IDS.clear()
        with give_gen._PROCESSOR_GUARD:
            give_gen._GIVE_PROCESSOR_ACTIVE = False
        self.conn.close()

    # -- helpers ------------------------------------------------------
    def _row(self, girl="mara", stage=1, day="2026-10-13"):
        return give_gen.create_give_generation(
            self.conn, girl, stage, "p", day, status="pending")

    def _job(self, gid):
        """Recorder stand-in for _run_give_generation_job: records
        the attempt, renders (url set, like a success) unless the
        id is in park_ids, in which case it parks exactly like the
        real job (row stays pending, no url) and returns 'parked'."""
        self.ran.append(gid)
        if gid in self.park_ids:
            return "parked"
        give_gen.update_give_generation_status(
            self.conn, gid, "pending", url="/generated/x.mp4")
        return None

    def _run_processor(self, gen_ids):
        with mock.patch("app.give_gen.db.connect",
                        return_value=self._nc), \
                mock.patch.object(give_gen, "_run_give_generation_job",
                                  side_effect=self._job):
            give_gen._process_queued_give_generations(list(gen_ids))

    # -- the repro ----------------------------------------------------
    def test_fresh_batch_does_not_jump_the_backlog(self):
        b1 = self._row(stage=1)
        b2 = self._row(stage=2)
        fresh = self._row(stage=3, day="2026-10-14")
        self._run_processor([fresh])
        # Global id order: the older backlog rows go first even
        # though the trigger handed in only the fresh id.
        self.assertEqual(self.ran, [b1, b2, fresh])

    def test_park_stand_down_then_next_wake_drains_in_order(self):
        """The 20:41Z shape: a batch hands in its ids, an early row
        parks, the processor stands down — and the NEXT wake must
        pick the backlog up in id order, not the newest batch."""
        b1 = self._row(stage=1)
        b2 = self._row(stage=2)
        b3 = self._row(stage=3)
        f1 = self._row(stage=4, day="2026-10-14")
        # First drain: the oldest row hits the throttle and parks.
        self.park_ids = {b1}
        self._run_processor([b1, b2, b3, f1])
        self.assertEqual(self.ran, [b1])
        row = give_gen.get_give_generation(self.conn, b1)
        self.assertEqual(row["status"], "pending")
        self.assertFalse(row["url"])
        # Next wake (a later trigger handing in a fresh row): the
        # parked row is retried first, then the backlog in order.
        self.park_ids = set()
        self.ran = []
        f2 = self._row(stage=5, day="2026-10-14")
        self._run_processor([f2])
        self.assertEqual(self.ran, [b1, b2, b3, f1, f2])
        for gid in (b1, b2, b3, f1, f2):
            row = give_gen.get_give_generation(self.conn, gid)
            self.assertEqual(row["status"], "pending")
            self.assertTrue(row["url"])

    # -- pins ---------------------------------------------------------
    def test_spawned_single_job_keeps_priority(self):
        """spawn_give_generation_job (assist's chat-driven render,
        polled by a waiting caller) must still go first."""
        b1 = self._row(stage=1)
        b2 = self._row(stage=2)
        prio = self._row(stage=9, day="2026-10-14")
        with mock.patch("app.give_gen.db.connect",
                        return_value=self._nc), \
                mock.patch.object(give_gen, "_run_give_generation_job",
                                  side_effect=self._job):
            give_gen.spawn_give_generation_job(prio)
            deadline = time.time() + 10
            while time.time() < deadline:
                alive = [t for t in threading.enumerate()
                         if t.name == f"give-gen-{prio}" and t.is_alive()]
                if not alive:
                    break
                for t in alive:
                    t.join(0.5)
        self.assertEqual(self.ran[0], prio)
        self.assertEqual(sorted(self.ran), sorted([b1, b2, prio]))

    def test_each_row_once_in_order_and_processor_stands_down(self):
        b1 = self._row(stage=1)
        b2 = self._row(stage=2)
        # Handed in out of order, with a duplicate: selection is
        # still global id order, each row exactly once.
        self._run_processor([b2, b1, b2])
        self.assertEqual(self.ran, [b1, b2])
        with give_gen._PROCESSOR_GUARD:
            self.assertFalse(give_gen._GIVE_PROCESSOR_ACTIVE)
        # A bare sweep wake with nothing left does nothing.
        self.ran = []
        self._run_processor([])
        self.assertEqual(self.ran, [])


if __name__ == "__main__":
    unittest.main()
