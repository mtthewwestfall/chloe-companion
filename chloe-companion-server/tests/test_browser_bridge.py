"""Browser-agent bridge tests (2026-10-09): Rocky-only deterministic
browse dispatch, queue lifecycle, SSRF wall, screenshot landing, and
the fail-closed shared-secret worker endpoints.

He queues real jobs; the VM worker loads the page; he never invents
what a page says.
Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import base64
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist  # noqa: E402

OWNER = "tg_owner_test"
OTHER = "tg_someone_else"


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

    def _user(self, uid):
        return {"user_id": uid, "tier": "basic"}


class DetectBrowseTest(EnvMixin):
    def test_screenshot_phrase(self):
        det = assist.detect_browse_request("screenshot tryroofrover.com")
        self.assertIsNotNone(det)
        self.assertEqual(det["action"], "screenshot")
        self.assertEqual(det["url"], "tryroofrover.com")

    def test_look_in_browser_is_read(self):
        det = assist.detect_browse_request(
            "look at https://example.com/pricing in the browser")
        self.assertIsNotNone(det)
        self.assertEqual(det["action"], "read")
        self.assertEqual(det["url"], "https://example.com/pricing")

    def test_question_is_kept(self):
        det = assist.detect_browse_request(
            "screenshot of https://example.com — what's the headline?")
        self.assertIsNotNone(det)
        self.assertEqual(det["action"], "screenshot")
        self.assertIn("headline", det["question"] or "")

    def test_bare_url_is_not_a_browse(self):
        self.assertIsNone(assist.detect_browse_request(
            "check out https://example.com sometime"))
        self.assertIsNone(assist.detect_browse_request(
            "https://example.com is a neat site"))

    def test_plain_chat_is_not_a_browse(self):
        self.assertIsNone(assist.detect_browse_request(
            "take a screenshot of my heart, it's yours"))
        self.assertIsNone(assist.detect_browse_request("hey rocky"))

    def test_browser_word_without_url_asks_for_link(self):
        det = assist.detect_browse_request("open that in the browser")
        self.assertIsNotNone(det)
        self.assertIsNone(det["url"])


class RoutingTest(EnvMixin):
    def test_owner_rocky_queues_job(self):
        hit = assist.maybe_assist(self.conn, self._user(OWNER),
                                  "screenshot tryroofrover.com", "rocky")
        self.assertIsNotNone(hit)
        job = assist.claim_browse_job(self.conn)
        self.assertIsNotNone(job)
        self.assertEqual(job["action"], "screenshot")
        self.assertEqual(job["url"], "https://tryroofrover.com")

    def test_owner_rocky_ssrf_refused(self):
        hit = assist.maybe_assist(self.conn, self._user(OWNER),
                                  "screenshot http://127.0.0.1:8765/x",
                                  "rocky")
        self.assertIsNotNone(hit)
        self.assertIn("off limits", hit["brief"])
        self.assertIsNone(assist.claim_browse_job(self.conn))

    def test_non_owner_rocky_queues_nothing(self):
        hit = assist.maybe_assist(self.conn, self._user(OTHER),
                                  "screenshot tryroofrover.com", "rocky")
        self.assertIsNone(assist.claim_browse_job(self.conn))
        self.assertTrue(hit is None or "browser job" not in
                        str(hit.get("brief", "")))

    def test_owner_other_character_queues_nothing(self):
        # v1 is Rocky's hands only — even the owner asking Mia gets
        # no browser queue.
        assist.maybe_assist(self.conn, self._user(OWNER),
                            "screenshot tryroofrover.com", "mia")
        self.assertIsNone(assist.claim_browse_job(self.conn))


class QueueLifecycleTest(EnvMixin):
    def _no_deliver(self):
        orig = assist._run_browse_delivery
        assist._run_browse_delivery = lambda job: None
        self.addCleanup(setattr, assist, "_run_browse_delivery", orig)

    def test_enqueue_claim_complete_read(self):
        self._no_deliver()
        jid = assist.enqueue_browse_job(self.conn, OWNER, "rocky",
                                        "https://example.com", "read",
                                        "what's the headline?")
        job = assist.claim_browse_job(self.conn)
        self.assertEqual(job["id"], jid)
        self.assertEqual(job["question"], "what's the headline?")
        self.assertIsNone(assist.claim_browse_job(self.conn))
        done = assist.complete_browse_job(self.conn, {
            "job_id": jid, "status": "done", "title": "Example Domain",
            "final_url": "https://example.com/",
            "text": "This domain is for use in documentation examples."})
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["title"], "Example Domain")
        # Duplicate result: returns the row, no error, no re-delivery.
        again = assist.complete_browse_job(self.conn, {
            "job_id": jid, "status": "failed", "error": "late"})
        self.assertEqual(again["status"], "done")

    def test_complete_unknown_or_unclaimed(self):
        self._no_deliver()
        self.assertIsNone(assist.complete_browse_job(
            self.conn, {"job_id": 9999, "status": "done"}))
        jid = assist.enqueue_browse_job(self.conn, OWNER, "rocky",
                                        "https://example.com", "read",
                                        None)
        # Never claimed -> result ignored (row returned untouched,
        # still queued, nothing delivered) — same rule as dev bridge.
        row = assist.complete_browse_job(
            self.conn, {"job_id": jid, "status": "done"})
        self.assertEqual(row["status"], "queued")

    def test_failed_result_keeps_error(self):
        self._no_deliver()
        jid = assist.enqueue_browse_job(self.conn, OWNER, "rocky",
                                        "https://example.com", "read",
                                        None)
        assist.claim_browse_job(self.conn)
        done = assist.complete_browse_job(self.conn, {
            "job_id": jid, "status": "failed",
            "error": "that page wants a sign-in first."})
        self.assertEqual(done["status"], "failed")
        self.assertIn("sign-in", done["error"])

    def test_screenshot_result_saves_image(self):
        self._no_deliver()
        tmp = tempfile.mkdtemp()
        orig_dir = assist.config.GENERATED_DIR
        assist.config.GENERATED_DIR = tmp
        self.addCleanup(setattr, assist.config, "GENERATED_DIR", orig_dir)
        jid = assist.enqueue_browse_job(self.conn, OWNER, "rocky",
                                        "https://example.com",
                                        "screenshot", None)
        assist.claim_browse_job(self.conn)
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
        done = assist.complete_browse_job(self.conn, {
            "job_id": jid, "status": "done", "title": "Example Domain",
            "final_url": "https://example.com/", "image_b64": png,
            "image_format": "png"})
        self.assertTrue(done["image_url"].startswith("/generated/browse-"))
        saved = os.path.join(tmp, os.path.basename(done["image_url"]))
        self.assertTrue(os.path.exists(saved))
        text, attachments = assist._browse_result_fallback(done)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["kind"], "image")
        self.assertEqual(attachments[0]["url"], done["image_url"])

    def test_text_is_capped(self):
        self._no_deliver()
        jid = assist.enqueue_browse_job(self.conn, OWNER, "rocky",
                                        "https://example.com", "read",
                                        None)
        assist.claim_browse_job(self.conn)
        done = assist.complete_browse_job(self.conn, {
            "job_id": jid, "status": "done",
            "text": "x" * (assist._BROWSE_TEXT_CAP + 5000)})
        self.assertEqual(len(done["result_text"]),
                         assist._BROWSE_TEXT_CAP)


class UrlWallTest(EnvMixin):
    def test_public_ok(self):
        self.assertTrue(assist._browse_url_ok("https://example.com/x"))
        self.assertTrue(assist._browse_url_ok("http://example.com"))

    def test_private_and_local_refused(self):
        for bad in ("http://127.0.0.1/x", "http://10.0.0.5/",
                    "https://192.168.1.1/admin", "http://localhost/x",
                    "https://foo.local/x", "https://[::1]/",
                    "ftp://example.com/x", "file:///etc/passwd"):
            self.assertFalse(assist._browse_url_ok(bad), bad)


class SecretGateTest(EnvMixin):
    def test_secret_fails_closed_when_unset(self):
        os.environ.pop("DEV_AGENT_SECRET", None)
        self.assertFalse(assist.dev_agent_secret_ok("anything"))

    def test_secret_match(self):
        self.assertTrue(assist.dev_agent_secret_ok("test-bridge-secret"))
        self.assertFalse(assist.dev_agent_secret_ok("wrong"))


if __name__ == "__main__":
    unittest.main()
