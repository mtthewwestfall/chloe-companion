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


class ImageGenerationTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        assist.ensure_tables(self.conn)
        self.old_env = {k: os.environ.get(k) for k in (
            "HANNAH_IMAGE_COOLDOWN_S", "HANNAH_IMAGE_DAILY_CAP",
            "HANNAH_IMAGE_MODEL", "HANNAH_IMAGE_FALLBACK_MODEL")}
        os.environ["HANNAH_IMAGE_COOLDOWN_S"] = "60"
        os.environ["HANNAH_IMAGE_DAILY_CAP"] = "3"
        self.tmp = tempfile.TemporaryDirectory()
        self.old_generated = assist.config.GENERATED_DIR
        assist.config.GENERATED_DIR = self.tmp.name
        self.old_post = assist._image_post

    def tearDown(self):
        assist._image_post = self.old_post
        assist.config.GENERATED_DIR = self.old_generated
        self.tmp.cleanup()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def _fake_image_data(self):
        import base64
        raw = b"\x89PNG\r\n\x1a\nfake-image-bytes"
        return {"candidates": [{"content": {"parts": [{
            "inlineData": {
                "mimeType": "image/png",
                "data": base64.b64encode(raw).decode("ascii"),
            }}]}}]}

    def test_detect_image_prompt(self):
        self.assertEqual(
            assist.detect_image_request(
                "Hannah, make an image of a red barn at sunset"),
            "a red barn at sunset")
        self.assertEqual(
            assist.detect_image_request(
                "create a picture: neon sign for a coffee shop"),
            "neon sign for a coffee shop")
        self.assertEqual(
            assist.detect_image_request("draw me a cat wearing a hat"),
            "cat wearing a hat")
        self.assertEqual(assist.detect_image_request("make an image"), "")

    def test_non_image_ask_not_hijacked(self):
        self.assertIsNone(assist.detect_image_request("make me a coffee"))
        self.assertIsNone(assist.detect_image_request("what did you do today"))

    def test_extract_inline_image(self):
        result = assist._extract_generated_image(self._fake_image_data())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mime_type"], "image/png")
        self.assertTrue(result["bytes"].startswith(b"\x89PNG"))

    def test_post_is_free_only_single_attempt(self):
        calls = []
        old = assist.gemini_keys.post_json
        try:
            assist.gemini_keys.post_json = lambda *a, **kw: (
                calls.append((a, kw)) or {"candidates": []})
            assist._image_post("a lighthouse", "gemini-test-image")
        finally:
            assist.gemini_keys.post_json = old
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1]["free_only"])
        self.assertEqual(calls[0][1]["max_attempts"], 1)
        self.assertEqual(calls[0][1]["source"], "hannah-image")
        self.assertIn("gemini-test-image:generateContent", calls[0][0][0])

    def test_handle_success_cooldown_and_cap(self):
        assist._image_post = lambda prompt, model: self._fake_image_data()
        first = assist._handle_image_request(
            self.conn, "owner_1", "hannah", "a red barn", now_epoch=1000)
        self.assertIn("image_url", first)
        self.assertTrue(first["image_url"].startswith("/generated/hannah-img-"))
        saved = os.path.join(self.tmp.name,
                             os.path.basename(first["image_url"]))
        self.assertTrue(os.path.exists(saved))

        second = assist._handle_image_request(
            self.conn, "owner_1", "hannah", "a blue barn", now_epoch=1030)
        self.assertNotIn("image_url", second)
        self.assertIn("too soon", second["brief"])

        os.environ["HANNAH_IMAGE_DAILY_CAP"] = "1"
        third = assist._handle_image_request(
            self.conn, "owner_1", "hannah", "a green barn", now_epoch=1061)
        self.assertNotIn("image_url", third)
        self.assertIn("today's image limit", third["brief"])

    def test_pool_unavailable_is_honest_and_recorded(self):
        assist._image_post = lambda prompt, model: None
        result = assist._handle_image_request(
            self.conn, "owner_1", "hannah", "a lighthouse", now_epoch=2000)
        self.assertNotIn("image_url", result)
        self.assertIn("free image keys are resting", result["brief"])
        gate = assist.image_gate(self.conn, "owner_1", now_epoch=2001)
        self.assertEqual(gate["attempts"], 1)


class AttachmentTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        assist.ensure_tables(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.old_generated = assist.config.GENERATED_DIR
        assist.config.GENERATED_DIR = self.tmp.name
        self.old_post = assist._attachment_post
        self.old_env = {k: os.environ.get(k) for k in (
            "ASSIST_OWNER_IDS", "HANNAH_VISION_MODEL",
            "HANNAH_VISION_FALLBACK_MODEL")}
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        os.environ["HANNAH_VISION_MODEL"] = "gemini-test-vision"
        os.environ["HANNAH_VISION_FALLBACK_MODEL"] = "gemini-test-vision"
        self.owner = {"user_id": "owner_1", "tier": "basic"}
        self.png = b"\x89PNG\r\n\x1a\nfake-image-bytes"

    def tearDown(self):
        assist._attachment_post = self.old_post
        assist.config.GENERATED_DIR = self.old_generated
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()
        self.tmp.cleanup()

    def test_save_and_scope_attachments(self):
        image = assist.save_chat_attachment(
            self.conn, "owner_1", "hannah", "promo.png", "image/png",
            self.png)
        self.assertEqual(image["kind"], "image")
        self.assertTrue(image["url"].startswith(
            "/generated/hannah-uploads/att_"))
        rows = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [image["id"]])
        self.assertEqual(rows[0]["filename"], "promo.png")
        with self.assertRaises(ValueError):
            assist.get_chat_attachments(
                self.conn, "someone_else", "hannah", [image["id"]])
        with self.assertRaises(ValueError):
            assist.get_chat_attachments(
                self.conn, "owner_1", "chloe", [image["id"]])

    def test_upload_validation(self):
        with self.assertRaises(ValueError):
            assist.save_chat_attachment(
                self.conn, "owner_1", "hannah", "payload.exe",
                "application/octet-stream", b"MZ")
        with self.assertRaises(ValueError):
            assist.save_chat_attachment(
                self.conn, "owner_1", "hannah", "fake.png",
                "image/png", b"not-a-png")
        old_max = assist.ATTACHMENT_MAX_BYTES
        assist.ATTACHMENT_MAX_BYTES = 4
        try:
            with self.assertRaises(ValueError):
                assist.save_chat_attachment(
                    self.conn, "owner_1", "hannah", "notes.txt",
                    "text/plain", b"12345")
        finally:
            assist.ATTACHMENT_MAX_BYTES = old_max

    def test_text_extraction_and_truncation(self):
        saved = assist.save_chat_attachment(
            self.conn, "owner_1", "hannah", "notes.md", "text/markdown",
            ("alpha " * 10).encode())
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]
        self.assertEqual(row["extraction_status"], "ok")
        self.assertIn("alpha", row["extracted_text"])

        old_limit = assist.ATTACHMENT_TEXT_LIMIT
        assist.ATTACHMENT_TEXT_LIMIT = 5
        try:
            extracted = assist.extract_attachment_text(
                "long.txt", b"123456789")
        finally:
            assist.ATTACHMENT_TEXT_LIMIT = old_limit
        self.assertEqual(extracted["text"], "12345")
        self.assertEqual(extracted["chars"], 9)
        self.assertTrue(extracted["truncated"])

    def test_text_file_brief_includes_content_without_vision_call(self):
        saved = assist.save_chat_attachment(
            self.conn, "owner_1", "hannah", "brief.txt", "text/plain",
            b"Alpha bravo. The secret word is pineapple.")
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]

        def forbidden(parts, model):
            raise AssertionError("text-only files must not call vision")

        assist._attachment_post = forbidden
        result = assist.analyze_chat_attachments(
            self.conn, self.owner, "summarize this", "hannah", [row])
        self.assertIn("pineapple", result["brief"])
        self.assertEqual(result["attachment_status"], "not_needed")

    def test_image_brief_uses_inline_vision(self):
        saved = assist.save_chat_attachment(
            self.conn, "owner_1", "hannah", "promo.png", "image/png",
            self.png)
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]
        seen = {}

        def fake_post(parts, model):
            seen["parts"] = parts
            seen["model"] = model
            return {"candidates": [{"content": {"parts": [{
                "text": "It says $4.99 PER LEAD and tryroofrover.com/store."
            }]}}]}

        assist._attachment_post = fake_post
        result = assist.analyze_chat_attachments(
            self.conn, self.owner, "what does this say?", "hannah", [row])
        self.assertIn("$4.99 PER LEAD", result["brief"])
        self.assertEqual(result["attachment_model"], "gemini-test-vision")
        inline = [p.get("inlineData") for p in seen["parts"]
                  if p.get("inlineData")]
        self.assertEqual(len(inline), 1)
        self.assertEqual(inline[0]["mimeType"], "image/png")

    def test_mixed_image_and_text_parts(self):
        image = assist.save_chat_attachment(
            self.conn, "owner_1", "hannah", "promo.png", "image/png",
            self.png)
        note = assist.save_chat_attachment(
            self.conn, "owner_1", "hannah", "notes.txt", "text/plain",
            b"Alpha bravo.")
        rows = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [image["id"], note["id"]])
        seen = {}

        def fake_post(parts, model):
            seen["parts"] = parts
            return {"candidates": [{"content": {"parts": [{
                "text": "A promo image."}]}}]}

        assist._attachment_post = fake_post
        result = assist.analyze_chat_attachments(
            self.conn, self.owner, "use both", "hannah", rows)
        all_text = "\n".join(p.get("text", "") for p in seen["parts"])
        self.assertIn("Alpha bravo", all_text)
        self.assertIn("A promo image", result["brief"])

    def test_vision_unavailable_is_honest(self):
        saved = assist.save_chat_attachment(
            self.conn, "owner_1", "hannah", "promo.png", "image/png",
            self.png)
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]
        assist._attachment_post = lambda parts, model: None
        result = assist.analyze_chat_attachments(
            self.conn, self.owner, "what is this?", "hannah", [row])
        self.assertIn("could NOT actually inspect", result["brief"])
        self.assertIn("image DID reach the server", result["brief"])
        self.assertIn("Do not call it a blank space", result["brief"])
        self.assertEqual(result["attachment_status"], "unavailable")

    def test_attachment_models_skip_retired_provider_ids(self):
        os.environ["HANNAH_VISION_MODEL"] = "gemini-2.5-flash"
        os.environ["HANNAH_VISION_FALLBACK_MODEL"] = "gemini-2.0-flash"
        self.assertEqual(
            assist.attachment_models(),
            ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite",
             "gemini-3.8-flash"])

    def test_attachment_post_stays_free_only_single_attempt(self):
        calls = []
        old = assist.gemini_keys.post_json
        try:
            assist.gemini_keys.post_json = lambda *a, **kw: (
                calls.append((a, kw)) or {"candidates": []})
            assist._attachment_post(
                [{"text": "look"}], "gemini-3.1-flash-lite")
        finally:
            assist.gemini_keys.post_json = old
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1]["free_only"])
        self.assertEqual(calls[0][1]["max_attempts"], 1)
        self.assertEqual(calls[0][1]["source"], "hannah-attachments")
        self.assertIn(
            "gemini-3.1-flash-lite:generateContent", calls[0][0][0])

    @staticmethod
    def _pdf_bytes(text=None):
        """Build a tiny real PDF (with a text layer when text is given)."""
        if text is None:
            content = b""
        else:
            safe = (text.replace("\\", " ").replace("(", " ")
                    .replace(")", " ").replace("\n", " "))
            content = (b"BT /F1 12 Tf 72 720 Td (" + safe.encode()
                       + b") Tj ET")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
             b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
            (b"<< /Length " + str(len(content)).encode()
             + b" >>\nstream\n" + content + b"\nendstream"),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        out = [b"%PDF-1.4\n"]
        offsets = []
        pos = len(out[0])
        for idx, body in enumerate(objects, start=1):
            offsets.append(pos)
            chunk = (str(idx).encode() + b" 0 obj\n" + body + b"\nendobj\n")
            out.append(chunk)
            pos += len(chunk)
        xref = pos
        table = (b"xref\n0 6\n0000000000 65535 f \n" + b"".join(
            (("%010d" % off) + " 00000 n \n").encode() for off in offsets))
        out.append(table + b"trailer\n<< /Size 6 /Root 1 0 R >>\n"
                     + b"startxref\n" + str(xref).encode() + b"\n%%EOF")
        return b"".join(out)

    @staticmethod
    def _docx_bytes(text=""):
        import io
        import docx as docx_lib
        document = docx_lib.Document()
        if text:
            document.add_paragraph(text)
        buf = io.BytesIO()
        document.save(buf)
        return buf.getvalue()

    def test_pdf_text_reaches_brief(self):
        saved = assist.save_chat_attachment(
            self.conn, "owner_1", "hannah", "report.pdf",
            "application/pdf",
            self._pdf_bytes("Invoice total is 499 dollars for roof leads."))
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]
        self.assertEqual(row["extraction_status"], "ok")
        self.assertIn("499 dollars", row["extracted_text"])
        result = assist.analyze_chat_attachments(
            self.conn, self.owner, "what does this say?", "hannah", [row])
        self.assertIn("Invoice total is 499 dollars", result["brief"])
        self.assertEqual(result["attachment_status"], "not_needed")

    def test_docx_text_reaches_brief(self):
        saved = assist.save_chat_attachment(
            self.conn, "owner_1", "hannah", "memo.docx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
            self._docx_bytes("The launch meeting moved to Friday."))
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]
        self.assertEqual(row["extraction_status"], "ok")
        result = assist.analyze_chat_attachments(
            self.conn, self.owner, "summarize this", "hannah", [row])
        self.assertIn("launch meeting moved to Friday", result["brief"])

    def test_long_docx_truncates(self):
        old_limit = assist.ATTACHMENT_TEXT_LIMIT
        assist.ATTACHMENT_TEXT_LIMIT = 10
        try:
            extracted = assist.extract_attachment_text(
                "long.docx", self._docx_bytes("abcdefghij" * 5))
        finally:
            assist.ATTACHMENT_TEXT_LIMIT = old_limit
        self.assertEqual(len(extracted["text"]), 10)
        self.assertTrue(extracted["truncated"])
        self.assertEqual(extracted["status"], "ok")

    def test_unreadable_documents_are_honest(self):
        corrupt = assist.extract_attachment_text(
            "broken.pdf", b"%PDF-1.4 this is not really a pdf at all")
        self.assertEqual(corrupt["status"], "corrupt")
        blank = assist.extract_attachment_text(
            "blank.pdf", self._pdf_bytes(None))
        self.assertEqual(blank["status"], "no_text")
        empty_docx = assist.extract_attachment_text(
            "empty.docx", self._docx_bytes(""))
        self.assertEqual(empty_docx["status"], "empty")

        saved = assist.save_chat_attachment(
            self.conn, "owner_1", "hannah", "broken.pdf",
            "application/pdf", b"%PDF-1.4 this is not really a pdf")
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]
        result = assist.analyze_chat_attachments(
            self.conn, self.owner, "read this", "hannah", [row])
        self.assertIn("corrupt or unreadable", result["brief"])

    def test_encrypted_pdf_is_honest(self):
        import io
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(612, 792)
        writer.encrypt("secret-password")
        buf = io.BytesIO()
        writer.write(buf)
        extracted = assist.extract_attachment_text(
            "locked.pdf", buf.getvalue())
        self.assertEqual(extracted["status"], "encrypted")

    def test_message_attachment_limit(self):
        ids = []
        for idx in range(5):
            saved = assist.save_chat_attachment(
                self.conn, "owner_1", "hannah", f"n{idx}.txt",
                "text/plain", b"x")
            ids.append(saved["id"])
        with self.assertRaises(ValueError):
            assist.get_chat_attachments(
                self.conn, "owner_1", "hannah", ids)

    def test_history_persists_attachment_metadata(self):
        from app import db, store
        path = os.path.join(self.tmp.name, "history.db")
        db.init_db(path)
        conn = db.connect(path)
        try:
            attachment = {"id": "att_test", "kind": "image",
                          "name": "promo.png", "url": "/generated/x.png",
                          "mime_type": "image/png", "size": 12,
                          "extraction_status": ""}
            turn_id = store.log_turn(
                conn, "owner_1", "user", "what does this say?", "hannah",
                attachments=[attachment])
            self.assertGreater(turn_id, 0)
            history = store.conversation_history(
                conn, "owner_1", character_id="hannah")
            self.assertEqual(history[0]["attachments"][0]["id"], "att_test")
        finally:
            conn.close()


class AttachmentVideoTest(unittest.TestCase):
    """Videos: accepted types, frame sampling, ordered vision brief,
    audio honesty. Fixtures are real clips built with the same ffmpeg
    the server samples with (assist._ffmpeg_exe())."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        cls.exe = assist._ffmpeg_exe()
        if not cls.exe:
            raise unittest.SkipTest("no ffmpeg toolchain available")
        cls.fixture_dir = tempfile.mkdtemp(prefix="hannah-vidfix-")
        cards = os.path.join(cls.fixture_dir, "cards.mp4")

        def run(args):
            proc = subprocess.run(args, capture_output=True, text=True,
                                  timeout=120)
            if proc.returncode != 0:
                raise unittest.SkipTest(
                    "ffmpeg fixture build failed: " + proc.stderr[-300:])

        # 6-second clip: blue (2s) -> red (2s) -> green (2s).
        run([cls.exe, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i",
             "color=c=0x1d4ed8:size=160x120:duration=2:rate=5",
             "-f", "lavfi", "-i",
             "color=c=0xb91c1c:size=160x120:duration=2:rate=5",
             "-f", "lavfi", "-i",
             "color=c=0x15803d:size=160x120:duration=2:rate=5",
             "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
             "-map", "[out]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             cards])
        run([cls.exe, "-hide_banner", "-loglevel", "error", "-y",
             "-i", cards, "-c", "copy",
             os.path.join(cls.fixture_dir, "cards.mov")])
        run([cls.exe, "-hide_banner", "-loglevel", "error", "-y",
             "-i", cards, "-c:v", "libvpx-vp9", "-b:v", "150k", "-an",
             os.path.join(cls.fixture_dir, "cards.webm")])
        # Same clip with an audible tone track (she still can't hear).
        run([cls.exe, "-hide_banner", "-loglevel", "error", "-y",
             "-i", cards, "-f", "lavfi", "-i",
             "sine=frequency=440:duration=6",
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
             "-shortest", os.path.join(cls.fixture_dir, "cards_audio.mp4")])
        # 30-second clip: the frame budget must not grow with length.
        run([cls.exe, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i",
             "color=c=0x334155:size=160x120:duration=30:rate=5",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             os.path.join(cls.fixture_dir, "long.mp4")])

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.fixture_dir, ignore_errors=True)

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        assist.ensure_tables(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.old_generated = assist.config.GENERATED_DIR
        assist.config.GENERATED_DIR = self.tmp.name
        self.old_post = assist._attachment_post
        self.old_env = {k: os.environ.get(k) for k in (
            "ASSIST_OWNER_IDS", "HANNAH_VISION_MODEL",
            "HANNAH_VISION_FALLBACK_MODEL")}
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        os.environ["HANNAH_VISION_MODEL"] = "gemini-test-vision"
        os.environ["HANNAH_VISION_FALLBACK_MODEL"] = "gemini-test-vision"
        self.owner = {"user_id": "owner_1", "tier": "basic"}

    def tearDown(self):
        assist._attachment_post = self.old_post
        assist.config.GENERATED_DIR = self.old_generated
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()
        self.tmp.cleanup()

    def _fixture(self, name):
        with open(os.path.join(self.fixture_dir, name), "rb") as fh:
            return fh.read()

    def _save(self, name, mime):
        return assist.save_chat_attachment(
            self.conn, "owner_1", "hannah", name, mime,
            self._fixture(name))

    def test_video_types_accepted_and_saved(self):
        saved = self._save("cards.mp4", "video/mp4")
        self.assertEqual(saved["kind"], "video")
        self.assertEqual(saved["extraction_status"], "video")
        self.assertTrue(saved["url"].endswith(".mp4"))
        # A poster frame is captured at upload for the thread card.
        self.assertTrue(saved["poster_url"].endswith(".poster.jpg"))
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]
        self.assertEqual(row["kind"], "video")

        for name, mime in (("cards.mov", "video/quicktime"),
                           ("cards.webm", "video/webm")):
            info = assist.classify_attachment(name, mime,
                                              self._fixture(name))
            self.assertEqual(info["kind"], "video")
            self.assertEqual(info["mime_type"], mime)

    def test_video_validation_and_caps(self):
        with self.assertRaises(ValueError):
            assist.save_chat_attachment(
                self.conn, "owner_1", "hannah", "clip.avi",
                "video/avi", self._fixture("cards.mp4"))
        with self.assertRaises(ValueError):
            assist.save_chat_attachment(
                self.conn, "owner_1", "hannah", "fake.mp4", "video/mp4",
                b"this is not really an mp4 file at all")
        old_cap = assist.ATTACHMENT_VIDEO_MAX_BYTES
        assist.ATTACHMENT_VIDEO_MAX_BYTES = 8
        try:
            with self.assertRaises(ValueError):
                self._save("cards.mp4", "video/mp4")
        finally:
            assist.ATTACHMENT_VIDEO_MAX_BYTES = old_cap
        # The 10 MB document cap is separate and unchanged.
        self.assertEqual(assist.ATTACHMENT_MAX_BYTES, 10 * 1024 * 1024)

    def test_frame_sampling_count_order_budget(self):
        import base64
        sampled = assist.sample_video_frames(
            os.path.join(self.fixture_dir, "cards.mp4"))
        self.assertEqual(sampled["status"], "ok")
        frames = sampled["frames"]
        self.assertGreaterEqual(len(frames), 3)
        self.assertLessEqual(len(frames), assist.ATTACHMENT_VIDEO_FRAMES)
        stamps = [f["stamp"] for f in frames]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(stamps[0], 0.0)
        self.assertLessEqual(stamps[-1], sampled["duration"])
        for frame in frames:
            raw = base64.b64decode(frame["data"])
            self.assertTrue(raw.startswith(b"\xff\xd8"))  # JPEG
        # First and last frames show different cards.
        self.assertNotEqual(frames[0]["data"], frames[-1]["data"])
        self.assertFalse(sampled["has_audio"])

        long_sampled = assist.sample_video_frames(
            os.path.join(self.fixture_dir, "long.mp4"))
        self.assertEqual(long_sampled["status"], "ok")
        self.assertLessEqual(len(long_sampled["frames"]),
                             assist.ATTACHMENT_VIDEO_FRAMES)

    def test_video_brief_feeds_ordered_frames(self):
        saved = self._save("cards.mp4", "video/mp4")
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]
        seen = {}

        def fake_post(parts, model):
            seen["parts"] = parts
            return {"candidates": [{"content": {"parts": [{
                "text": "Three title cards in order: blue, red, green."
            }]}}]}

        assist._attachment_post = fake_post
        result = assist.analyze_chat_attachments(
            self.conn, self.owner, "what happens in this?", "hannah",
            [row])
        self.assertIn("actually watched", result["brief"])
        self.assertIn("blue, red, green", result["brief"])
        self.assertEqual(result["attachment_model"], "gemini-test-vision")
        inline = [p for p in seen["parts"] if p.get("inlineData")]
        labels = [p.get("text", "") for p in seen["parts"]
                  if p.get("text", "").startswith("[Video frame ")]
        self.assertEqual(len(inline), len(labels))
        self.assertGreaterEqual(len(labels), 3)
        numbers = [int(lbl.split("/")[0].rsplit(" ", 1)[1])
                   for lbl in labels]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(numbers[0], 1)

    def test_video_audio_is_honest(self):
        saved = self._save("cards_audio.mp4", "video/mp4")
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]
        assist._attachment_post = lambda parts, model: {
            "candidates": [{"content": {"parts": [{
                "text": "Colored cards, no visible people."}]}}]}
        result = assist.analyze_chat_attachments(
            self.conn, self.owner, "what do you hear?", "hannah", [row])
        self.assertIn("can't hear audio", result["brief"])
        self.assertIn("muted", result["brief"])

    def test_video_vision_unavailable_is_honest(self):
        saved = self._save("cards.mp4", "video/mp4")
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]
        assist._attachment_post = lambda parts, model: None
        result = assist.analyze_chat_attachments(
            self.conn, self.owner, "watch this", "hannah", [row])
        self.assertIn("could NOT actually watch the video",
                      result["brief"])
        self.assertEqual(result["attachment_status"], "unavailable")

    def test_corrupt_video_rejected_at_upload(self):
        # Right container magic, garbage inside: ffmpeg finds no video
        # stream, so the upload fails cleanly instead of mid-chat.
        bogus = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
        with self.assertRaises(ValueError) as ctx:
            assist.save_chat_attachment(
                self.conn, "owner_1", "hannah", "broken.mp4",
                "video/mp4", bogus)
        self.assertIn("playable", str(ctx.exception))

    def test_unreadable_video_is_honest_in_brief(self):
        saved = self._save("cards.mp4", "video/mp4")
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "hannah", [saved["id"]])[0]
        # Trash the stored bytes after upload (kept the ftyp header so
        # it still routes as the same attachment record).
        path = os.path.join(assist._attachment_dir(),
                            row["storage_name"])
        with open(path, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64)
        assist._attachment_post = lambda parts, model: {
            "candidates": [{"content": {"parts": [{"text": "x"}]}}]}
        result = assist.analyze_chat_attachments(
            self.conn, self.owner, "watch this", "hannah", [row])
        self.assertIn("did not come through as a playable video",
                      result["brief"])
