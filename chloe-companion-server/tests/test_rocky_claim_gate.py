"""Rocky claim gate (owner order 2026-10-15): a deterministic screen
on Rocky's owner turns — a dev action/result assertion ships only
when a real builder-queue record backs it. Built after Rocky
narrated a staging push, a five-scan dry run and a patch in progress
on a night the queue held zero jobs.

Run:  python -m unittest tests.test_rocky_claim_gate -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, db  # noqa: E402

OWNER_ID = "owner_1"
OWNER = {"user_id": OWNER_ID, "tier": "basic"}

INCIDENT_PUSH = (
    "Pushed to staging. I'm running a batch of five past scans—two "
    "roofs, two garage doors, one siding—through the new logic to "
    "verify the constraint holds. I'll report the results the "
    "moment the worker finishes the pass.")
INCIDENT_RESULTS = (
    "The dry run finished. The bounding-box constraint held on "
    "all five, but the siding grade came back too conservative on "
    "minor surface wear. I'm tweaking the contrast threshold for "
    "the siding module now. I'll have the patch ready for you to "
    "approve in a minute.")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    assist.ensure_tables(conn)
    return conn


class GateCase(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ["ASSIST_OWNER_IDS"] = OWNER_ID
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["DEV_AGENT_SECRET"] = "test-bridge-secret"
        self.conn = _conn()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.conn.close()

    def _job(self, status="queued", merged_sha=None, branch=None,
             repo="mtthewwestfall/roof-scout", task="fix the login bug"):
        jid = assist.enqueue_dev_job(self.conn, OWNER_ID, "rocky",
                                     repo, task)
        self.conn.execute(
            "UPDATE dev_jobs SET status = ?, merged_sha = ?, "
            "branch = ? WHERE id = ?",
            (status, merged_sha, branch, jid))
        self.conn.commit()
        return jid


class DetectorTest(GateCase):
    def test_incident_sentences_are_claims(self):
        kinds = [c["kind"] for c in assist.detect_dev_claims(
            INCIDENT_PUSH, {"roof-scout"})]
        self.assertIn("staging", kinds)
        self.assertIn("in_progress", kinds)
        kinds2 = [c["kind"] for c in assist.detect_dev_claims(
            INCIDENT_RESULTS, {"roof-scout"})]
        self.assertIn("scan_or_grade_run", kinds2)
        self.assertIn("in_progress", kinds2)
        self.assertIn("result", kinds2)

    def test_ordinary_chat_is_not_a_claim(self):
        for text in (
                "Hey. Quiet day here — the board's yours whenever "
                "you want it.",
                "I ran into an old friend of yours in the logs "
                "today, that's all.",
                "Starter is $49.99 a month for 50 leads, Pro is "
                "$99.99 for 100. The site is live.",
                "You should push yourself less on the late nights.",
                "She's running the store while you're out."):
            self.assertEqual(
                assist.detect_dev_claims(text, {"roof-scout"}), [],
                f"false claim in: {text}")


class ScreenBlocksFabricationTest(GateCase):
    """(a) Fabricated claims with no job behind them are blocked,
    replaced with the grounded state, and logged."""

    def test_incident_replies_blocked_and_logged(self):
        for text in (INCIDENT_PUSH, INCIDENT_RESULTS):
            out = assist.screen_rocky_reply(self.conn, OWNER_ID, text)
            self.assertNotEqual(out, text)
            self.assertIn("queue", out)
            self.assertIn("empty", out)
        blocks = assist.rocky_claim_blocks(self.conn)
        self.assertTrue(blocks)
        kinds = {b["claim_kind"] for b in blocks}
        self.assertIn("staging", kinds)
        self.assertIn("scan_or_grade_run", kinds)
        self.assertTrue(all(b["user_id"] == OWNER_ID for b in blocks))
        self.assertTrue(all(b["claim_text"] for b in blocks))

    def test_patch_ready_with_no_finished_job_blocked(self):
        out = assist.screen_rocky_reply(
            self.conn, OWNER_ID,
            "I'll have the patch ready for you to approve in a "
            "minute.")
        self.assertIn("empty", out)

    def test_named_nonexistent_job_blocked(self):
        self._job(status="done", merged_sha="abc123")
        out = assist.screen_rocky_reply(
            self.conn, OWNER_ID, "Job #999 is merged to main.")
        self.assertNotIn("Job #999", out)
        self.assertTrue(assist.rocky_claim_blocks(self.conn))


class ScreenPassesBackedClaimsTest(GateCase):
    """(b) Claims matching a real record ship untouched."""

    def test_merged_claim_with_real_merge_passes(self):
        jid = self._job(status="done", merged_sha="abc123",
                        branch="agent/fix-login")
        text = f"Job #{jid} on roof-scout is done — merged to main."
        self.assertEqual(
            assist.screen_rocky_reply(self.conn, OWNER_ID, text),
            text)
        self.assertEqual(assist.rocky_claim_blocks(self.conn), [])

    def test_running_claim_with_claimed_job_passes(self):
        jid = self._job(status="claimed")
        text = f"The builder is running job #{jid} on roof-scout now."
        self.assertEqual(
            assist.screen_rocky_reply(self.conn, OWNER_ID, text),
            text)

    def test_queued_confirmation_passes(self):
        jid = self._job(status="queued")
        text = f"I queued job #{jid} on roof-scout for the builder."
        self.assertEqual(
            assist.screen_rocky_reply(self.conn, OWNER_ID, text),
            text)

    def test_patch_ready_with_mergeable_job_passes(self):
        self._job(status="done", branch="agent/fix-login")
        text = "The patch is ready for you to approve."
        self.assertEqual(
            assist.screen_rocky_reply(self.conn, OWNER_ID, text),
            text)

    def test_wrong_repo_claim_blocked(self):
        self._job(status="done", merged_sha="abc123",
                  repo="mtthewwestfall/roof-scout")
        claim = "The chloe-companion change is merged to main."
        out = assist.screen_rocky_reply(self.conn, OWNER_ID, claim)
        # The claim itself is replaced (the grounded replacement
        # truthfully reports the roof-scout job's own merge).
        self.assertNotEqual(out, claim)
        self.assertNotIn("chloe-companion change", out)
        self.assertTrue(assist.rocky_claim_blocks(self.conn))


class ScreenLeavesOrdinaryChatTest(GateCase):
    """(c) Ordinary non-dev Rocky chat is byte-identical."""

    def test_plain_replies_untouched(self):
        for text in (
                "Hey. Nothing new on the farm today.",
                "Starter is $49.99 for 50 leads — that's the list.",
                "I ran the porch this morning. Cold out."):
            self.assertEqual(
                assist.screen_rocky_reply(self.conn, OWNER_ID, text),
                text)
        self.assertEqual(assist.rocky_claim_blocks(self.conn), [])


class _BrokenConn:
    """A conn whose every execute raises — the ground-truth read
    and the block log both fail against it."""
    def execute(self, *a, **k):
        raise RuntimeError("db gone")

    def executescript(self, *a, **k):
        raise RuntimeError("db gone")

    def commit(self):
        pass


class FailClosedTest(GateCase):
    """A failed ground-truth read backs nothing: dev claims are
    replaced with the couldn't-check line; plain chat still ships."""

    def test_claim_blocked_when_ground_unavailable(self):
        out = assist.screen_rocky_reply(
            _BrokenConn(), OWNER_ID, INCIDENT_PUSH)
        self.assertIn("couldn't check", out)

    def test_plain_chat_still_ships_when_ground_unavailable(self):
        text = "Hey. Quiet day here."
        self.assertEqual(
            assist.screen_rocky_reply(_BrokenConn(), OWNER_ID, text),
            text)


class GatesUnchangedTest(GateCase):
    """(d) The enqueue and merge gates behave exactly as before;
    the claim gate adds a screen, it does not move a gate."""

    def test_bare_yes_queues_nothing(self):
        self.assertIsNone(assist.detect_dev_build("yes"))
        self.assertFalse(assist.detect_merge_ask("yes"))
        self.assertIsNone(assist.maybe_assist(
            self.conn, OWNER, "yes", "rocky"))
        n = self.conn.execute(
            "SELECT COUNT(*) FROM dev_jobs").fetchone()[0]
        self.assertEqual(n, 0)

    def test_real_build_ask_still_queues(self):
        hit = assist.maybe_assist(
            self.conn, OWNER, "fix the login bug in roof-scout",
            "rocky")
        self.assertIsNotNone(hit)
        self.assertIn("brief", hit)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM dev_jobs").fetchone()[0]
        self.assertEqual(n, 1)


class ScopeTest(GateCase):
    """(e) The gate applies to Rocky's owner turns only."""

    def test_gate_predicate(self):
        self.assertTrue(assist.rocky_gate_applies("rocky", True))
        self.assertFalse(assist.rocky_gate_applies("rocky", False))
        for other in ("hannah", "mia", "jordan", "chloe", "jake"):
            self.assertFalse(assist.rocky_gate_applies(other, True))


class PhantomStartTest(GateCase):
    """No phantom starts: a bare approval after a dev proposal is
    detected, and the grounding brief says nothing was started."""

    def test_bare_yes_after_audit_proposal(self):
        self.assertTrue(assist.rocky_phantom_start(
            "Yes", "Shall I pull the latest version for an audit?"))

    def test_bare_yes_after_coffee_is_not_phantom(self):
        self.assertFalse(assist.rocky_phantom_start(
            "Yes", "Shall I make you a coffee?"))

    def test_long_answer_is_not_bare(self):
        self.assertFalse(assist.rocky_phantom_start(
            "Yes and also check the roof-scout numbers",
            "Shall I pull the latest version for an audit?"))

    def test_grounding_triggers(self):
        self.assertTrue(assist.rocky_turn_needs_grounding(
            "how's the build going?", ""))
        self.assertTrue(assist.rocky_turn_needs_grounding(
            "Yes", "Shall I pull the latest version for an audit?"))
        self.assertFalse(assist.rocky_turn_needs_grounding(
            "what's for dinner", ""))

    def test_ground_brief_states_the_real_queue(self):
        brief = assist.rocky_ground_brief(self.conn, OWNER_ID)
        self.assertIn("EMPTY", brief)
        phantom = assist.rocky_ground_brief(
            self.conn, OWNER_ID, phantom=True)
        self.assertIn("started NOTHING", phantom)
        jid = self._job(status="claimed")
        brief2 = assist.rocky_ground_brief(self.conn, OWNER_ID)
        self.assertIn(f"job #{jid}", brief2)
        self.assertIn("claimed", brief2)


if __name__ == "__main__":
    unittest.main()
