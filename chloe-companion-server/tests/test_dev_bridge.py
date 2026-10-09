"""Dev-agent bridge tests (2026-10-08): owner-only build dispatch,
queue lifecycle, and the fail-closed shared-secret worker endpoints.

She queues real jobs; she never builds, promises, or invents.
Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist  # noqa: E402

OWNER = "tg_owner_test"
ASK = ("fix the report formatter in the dev-agent repo so it "
       "passes its tests")


class EnvMixin(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ["ASSIST_OWNER_IDS"] = OWNER
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["DEV_AGENT_SECRET"] = "test-bridge-secret"
        self.conn = sqlite3.connect(":memory:")
        assist.ensure_tables(self.conn)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.conn.close()


class DetectDevBuildTest(EnvMixin):
    def test_named_repo(self):
        dv = assist.detect_dev_build(ASK)
        self.assertIsNotNone(dv)
        self.assertEqual(dv["repo"], "dev-agent")
        self.assertIn("report formatter", dv["task"])

    def test_owner_slash_repo(self):
        dv = assist.detect_dev_build(
            "please update mtthewwestfall/roof-scout to cache imagery")
        self.assertIsNotNone(dv)
        self.assertEqual(dv["repo"], "roof-scout")

    def test_file_path_does_not_shadow_repo_name(self):
        # First live probe: "devagent/report.py" was grabbed as
        # owner/repo and misqueued "mtthewwestfall/report.py".
        dv = assist.detect_dev_build(
            "please fix the report formatter in the dev-agent repo — "
            "devagent/report.py's format_job_report needs to pass the "
            "tests in tests/test_report.py")
        self.assertIsNotNone(dv)
        self.assertEqual(dv["repo"], "dev-agent")

    def test_file_path_alone_names_no_repo(self):
        # No repo named: the intent fires with repo None so the
        # handler asks its one clarifying question — it never invents
        # "devagent" or "report.py" as the target repo.
        dv = assist.detect_dev_build(
            "update devagent/report.py so it passes its tests")
        self.assertIsNotNone(dv)
        self.assertIsNone(dv["repo"])

    def test_bot_mention_no_repo(self):
        dv = assist.detect_dev_build("have your dev bot fix the thing")
        self.assertIsNotNone(dv)
        self.assertIsNone(dv["repo"])

    def test_plain_chat_is_not_a_build(self):
        self.assertIsNone(assist.detect_dev_build("fix me a sandwich"))
        self.assertIsNone(assist.detect_dev_build(
            "how do I fix a leaky roof"))
        self.assertIsNone(assist.detect_dev_build("hey beautiful"))

    def test_token_talk_is_never_a_build(self):
        self.assertIsNone(assist.detect_dev_build(
            "change my github token please"))


class QueueLifecycleTest(EnvMixin):
    def test_enqueue_claim_complete(self):
        jid = assist.enqueue_dev_job(self.conn, OWNER, "hannah",
                                     "mtthewwestfall/dev-agent", ASK)
        job = assist.claim_dev_job(self.conn)
        self.assertEqual(job["id"], jid)
        self.assertEqual(job["repo"], "mtthewwestfall/dev-agent")
        self.assertIsNone(assist.claim_dev_job(self.conn))

        delivered = []
        orig = assist._run_dev_delivery
        assist._run_dev_delivery = delivered.append
        try:
            done = assist.complete_dev_job(self.conn, {
                "job_id": jid, "status": "done",
                "branch": "agent/fix-report-1", "summary": "tests pass",
                "detail": "", "report": "# Job report"})
        finally:
            assist._run_dev_delivery = orig
        self.assertEqual(done["status"], "done")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["branch"], "agent/fix-report-1")
        # A duplicate result post returns the row, no second delivery.
        again = assist.complete_dev_job(self.conn, {
            "job_id": jid, "status": "done", "branch": "agent/other"})
        self.assertEqual(again["status"], "done")
        self.assertEqual(len(delivered), 1)
        self.assertIsNone(assist.complete_dev_job(
            self.conn, {"job_id": 999999, "status": "done"}))

    def test_dedupe_same_task(self):
        assist.enqueue_dev_job(self.conn, OWNER, "hannah",
                               "mtthewwestfall/dev-agent", ASK)
        dup = assist.active_dev_job(self.conn, OWNER,
                                    "mtthewwestfall/dev-agent",
                                    ASK.upper())
        self.assertIsNotNone(dup)
        self.assertIsNone(assist.active_dev_job(
            self.conn, OWNER, "mtthewwestfall/dev-agent",
            "a totally different task"))
        self.assertEqual(
            assist.active_dev_job_count(self.conn, OWNER), 1)


class SecretGateTest(EnvMixin):
    def test_secret_ok(self):
        self.assertTrue(assist.dev_agent_secret_ok("test-bridge-secret"))
        self.assertFalse(assist.dev_agent_secret_ok("wrong"))
        self.assertFalse(assist.dev_agent_secret_ok(None))
        self.assertFalse(assist.dev_agent_secret_ok(""))

    def test_unset_secret_fails_closed(self):
        del os.environ["DEV_AGENT_SECRET"]
        self.assertFalse(assist.dev_agent_secret_ok("test-bridge-secret"))


class MaybeAssistGateTest(EnvMixin):
    def _user(self, uid):
        return {"user_id": uid, "tier": "basic"}

    def test_owner_ask_queues_real_job(self):
        hit = assist.maybe_assist(self.conn, self._user(OWNER), ASK,
                                  "hannah")
        self.assertIsNotNone(hit)
        self.assertIn("queued build job", hit["brief"])
        job = assist.claim_dev_job(self.conn)
        self.assertIsNotNone(job)
        self.assertEqual(job["repo"], "mtthewwestfall/dev-agent")

    def test_owner_ask_no_repo_asks_one_question(self):
        hit = assist.maybe_assist(self.conn, self._user(OWNER),
                                  "have your dev bot fix the thing",
                                  "hannah")
        self.assertIsNotNone(hit)
        self.assertIn("which repo", hit["brief"])
        self.assertIsNone(assist.claim_dev_job(self.conn))

    def test_owner_ask_bridge_dark_without_secret(self):
        del os.environ["DEV_AGENT_SECRET"]
        hit = assist.maybe_assist(self.conn, self._user(OWNER), ASK,
                                  "hannah")
        self.assertIsNotNone(hit)
        self.assertIn("not wired", hit["brief"])
        self.assertIsNone(assist.claim_dev_job(self.conn))

    def test_non_owner_never_queues_even_with_rollout_paid(self):
        # The owner gate is independent of ASSIST_ROLLOUT: even fully
        # rolled-out assist leaves dispatch to owners only.
        os.environ["ASSIST_ROLLOUT"] = "paid"
        hit = assist.maybe_assist(self.conn, self._user("tg_customer_1"),
                                  ASK, "hannah")
        if hit is not None:
            self.assertNotIn("queued build job", hit.get("brief", ""))
        self.assertIsNone(assist.claim_dev_job(self.conn))

    def test_marketing_still_wins_over_weak_build_verbs(self):
        hit = assist.maybe_assist(
            self.conn, self._user(OWNER),
            "write me a promo hook for roof rover", "hannah")
        self.assertIsNotNone(hit)
        self.assertIn("VERIFIED FACTS", hit["brief"])
        self.assertIsNone(assist.claim_dev_job(self.conn))


class FallbackTextTest(EnvMixin):
    def test_done_fallback_uses_real_fields(self):
        job = {"id": 7, "repo": "mtthewwestfall/dev-agent",
               "task": "fix the thing", "status": "done",
               "branch": "agent/fix-7", "summary": "2 files changed.",
               "detail": ""}
        out = assist._dev_result_fallback(job)
        self.assertIn("agent/fix-7", out)
        self.assertIn("actually finished", out)

    def test_failed_fallback_is_plain(self):
        job = {"id": 8, "repo": "mtthewwestfall/dev-agent",
               "task": "fix the thing", "status": "failed",
               "branch": "agent/fix-8", "summary": "",
               "detail": "tests still red"}
        out = assist._dev_result_fallback(job)
        self.assertIn("didn't land", out)
        self.assertIn("tests still red", out)


class RepoNameCanonicalTest(EnvMixin):
    """2026-10-08 live block: his storage ask said "roofscout" and the
    queue took it verbatim -> GitHub 404 -> instant block. Squashed or
    spaced spoken forms of his real repos canonicalize; hyphenated
    input survives byte-identical; unknown names pass through as
    themselves, never as a guessed repo."""

    def test_job5_squashed_text_now_maps(self):
        dv = assist.detect_dev_build(
            "Create a storage system for all the scans that railways "
            "can transfer to the empty it’s bin and get more for "
            "transfer for my roofscout repo")
        self.assertIsNotNone(dv)
        self.assertEqual(dv["repo"], "roof-scout")

    def test_hyphenated_preserved(self):
        self.assertEqual(assist.detect_dev_build(
            "build it in my roof-scout repo")["repo"], "roof-scout")
        self.assertEqual(assist.detect_dev_build(
            "fix the thing in roof-scout")["repo"], "roof-scout")
        self.assertEqual(assist.detect_dev_build(
            "patch the parser in chloe-companion")["repo"],
            "chloe-companion")

    def test_spaced_and_caps_forms(self):
        self.assertEqual(assist.detect_dev_build(
            "fix the thing in my roof scout repo")["repo"], "roof-scout")
        self.assertEqual(assist.detect_dev_build(
            "update my RoofScout repo please")["repo"], "roof-scout")

    def test_squashed_dev_agent_and_queue(self):
        hit = assist.maybe_assist(
            self.conn, {"user_id": OWNER, "tier": "basic"},
            "fix the formatter in my devagent repo", "hannah")
        self.assertIsNotNone(hit)
        self.assertIn("queued build job", hit["brief"])
        job = assist.claim_dev_job(self.conn)
        self.assertIsNotNone(job)
        self.assertEqual(job["repo"], "mtthewwestfall/dev-agent")

    def test_unknown_name_passes_through(self):
        dv = assist.detect_dev_build(
            "fix the thing in my brand-new-repo repo")
        self.assertIsNotNone(dv)
        self.assertEqual(dv["repo"], "brand-new-repo")

    def test_canonical_helper(self):
        self.assertEqual(assist._canonical_repo("roofscout"),
                         "roof-scout")
        self.assertEqual(assist._canonical_repo("ROOFSCOUT"),
                         "roof-scout")
        self.assertEqual(assist._canonical_repo("roof-scout"),
                         "roof-scout")
        self.assertEqual(assist._canonical_repo("not-a-repo"),
                         "not-a-repo")
        self.assertIsNone(assist._canonical_repo(None))
        self.assertEqual(assist._canonical_repo(""), "")

    def test_review_path_canonicalizes(self):
        gh = assist.detect_github_review("review my roof-scout repo")
        self.assertEqual(gh, {"repo": "roof-scout"})
        gh = assist.detect_github_review("review the code in roofscout")
        self.assertEqual(gh, {"repo": "roof-scout"})
        gh = assist.detect_github_review(
            "look at mtthewwestfall/roofscout, what's broken")
        self.assertEqual(gh, {"repo": "roof-scout"})

    def test_owner_slash_squashed(self):
        dv = assist.detect_dev_build(
            "please update mtthewwestfall/roofscout to cache imagery")
        self.assertIsNotNone(dv)
        self.assertEqual(dv["repo"], "roof-scout")


if __name__ == "__main__":
    unittest.main()
