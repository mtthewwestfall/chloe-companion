"""Gemini image fallback tests (2026-10-19).

Covers assist.gemini_image_render (the Sogni-outcome-shaped Gemini
image render) and the Her Day wiring: Sogni down/no session -> Gemini
fallback renders; Sogni success -> fallback never called; both down
-> "" (text card) exactly as before.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, config, her_day  # noqa: E402
from app import media_gen  # noqa: E402

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _ok_response():
    return {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png",
                        "data": base64.b64encode(_PNG).decode("ascii")}}]},
        "finishReason": "STOP"}]}


class GeminiImageRenderTest(unittest.TestCase):
    def setUp(self):
        self._orig_post = assist.gemini_keys.post_json
        self._orig_env = os.environ.get("IMAGE_FALLBACK")
        os.environ.pop("IMAGE_FALLBACK", None)
        self.captured = {}

    def tearDown(self):
        assist.gemini_keys.post_json = self._orig_post
        if self._orig_env is None:
            os.environ.pop("IMAGE_FALLBACK", None)
        else:
            os.environ["IMAGE_FALLBACK"] = self._orig_env

    def _fake_post(self, response):
        def _post(url, payload, timeout=30, source="unknown", **kw):
            self.captured = {"url": url, "payload": payload,
                             "source": source, "timeout": timeout}
            return response
        return _post

    def test_success_writes_file_and_shapes_payload(self):
        assist.gemini_keys.post_json = self._fake_post(_ok_response())
        out = assist.gemini_image_render("a girl at a studio")
        self.assertEqual(out["status"], "done")
        with open(out["path"], "rb") as f:
            self.assertEqual(f.read(), _PNG)
        os.unlink(out["path"])
        self.assertIn("gemini-2.5-flash-image", self.captured["url"])
        self.assertEqual(self.captured["source"], "image-fallback")
        cfg = self.captured["payload"]["generationConfig"]
        self.assertEqual(cfg["responseModalities"], ["TEXT", "IMAGE"])
        self.assertEqual(len(self.captured["payload"]["safetySettings"]), 4)

    def test_reference_image_attached(self):
        assist.gemini_keys.post_json = self._fake_post(_ok_response())
        with tempfile.NamedTemporaryFile(suffix=".jpg",
                                         delete=False) as f:
            f.write(_PNG)
            ref = f.name
        try:
            out = assist.gemini_image_render("edit her", ref)
            self.assertEqual(out["status"], "done")
            os.unlink(out["path"])
            parts = self.captured["payload"]["contents"][0]["parts"]
            self.assertEqual(len(parts), 2)
            self.assertEqual(parts[1]["inlineData"]["mimeType"],
                             "image/jpeg")
        finally:
            os.unlink(ref)

    def test_block_reason_is_refused(self):
        assist.gemini_keys.post_json = self._fake_post(
            {"promptFeedback": {"blockReason": "SAFETY"}})
        out = assist.gemini_image_render("x")
        self.assertEqual(out["status"], "refused")

    def test_pool_none_is_pool_exhausted(self):
        assist.gemini_keys.post_json = self._fake_post(None)
        out = assist.gemini_image_render("x")
        self.assertEqual(out["status"], "pool_exhausted")

    def test_text_only_is_no_image(self):
        assist.gemini_keys.post_json = self._fake_post(
            {"candidates": [{"content": {"parts": [{"text": "no"}]},
                             "finishReason": "STOP"}]})
        out = assist.gemini_image_render("x")
        self.assertEqual(out["status"], "no_image")

    def test_env_off_disables(self):
        os.environ["IMAGE_FALLBACK"] = "off"
        assist.gemini_keys.post_json = self._fake_post(_ok_response())
        out = assist.gemini_image_render("x")
        self.assertEqual(out["status"], "fallback_off")
        self.assertEqual(self.captured, {})


class HerDayFallbackWiringTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_gen_dir = config.GENERATED_DIR
        config.GENERATED_DIR = self._tmp
        self._orig_session = her_day._plan_session
        self._orig_render_plan = her_day._render_image_plan
        self._orig_fetch_ref = media_gen._fetch_reference
        self._orig_fb = assist.gemini_image_render
        self.fb_calls = []
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False,
                                         dir=self._tmp) as f:
            f.write(_PNG)
            self._img = f.name
        media_gen._fetch_reference = lambda url: None

    def tearDown(self):
        config.GENERATED_DIR = self._orig_gen_dir
        her_day._plan_session = self._orig_session
        her_day._render_image_plan = self._orig_render_plan
        media_gen._fetch_reference = self._orig_fetch_ref
        assist.gemini_image_render = self._orig_fb

    def _fb(self, outcome):
        def _render(prompt, ref_path=None):
            self.fb_calls.append(prompt)
            return outcome
        return _render

    def _call(self):
        return her_day._generate_sfw_image(
            ["Chloe"], "", ["chloe"], "morning_coffee",
            "a morning scene", "2026-10-19")

    def test_no_session_uses_fallback(self):
        her_day._plan_session = lambda: None
        assist.gemini_image_render = self._fb(
            {"status": "done", "path": self._img})
        url = self._call()
        self.assertTrue(url.startswith("/generated/"))
        self.assertEqual(len(self.fb_calls), 1)
        self.assertTrue(os.path.exists(
            os.path.join(self._tmp, os.path.basename(url))))

    def test_sogni_failure_uses_fallback(self):
        her_day._plan_session = lambda: {"pair": 1}
        her_day._render_image_plan = lambda s, p, r: {
            "status": "failed", "detail": "4089 capacity"}
        assist.gemini_image_render = self._fb(
            {"status": "done", "path": self._img})
        url = self._call()
        self.assertTrue(url.startswith("/generated/"))
        self.assertEqual(len(self.fb_calls), 1)

    def test_sogni_success_skips_fallback(self):
        her_day._plan_session = lambda: {"pair": 1}
        her_day._render_image_plan = lambda s, p, r: {
            "status": "done", "path": self._img}
        assist.gemini_image_render = self._fb(
            {"status": "done", "path": self._img})
        url = self._call()
        self.assertTrue(url.startswith("/generated/"))
        self.assertEqual(self.fb_calls, [])

    def test_both_down_returns_empty(self):
        her_day._plan_session = lambda: None
        assist.gemini_image_render = self._fb(
            {"status": "pool_exhausted", "detail": "none"})
        self.assertEqual(self._call(), "")


class SfwPromptCleanPhotoTest(unittest.TestCase):
    """The SFW image prompt must demand a clean photograph, never a
    social-media screenshot (2026-10-19: an "Instagram story" tail
    made the renderer fabricate story UI and invented handles like
    "regan_lom" on Her Day moments)."""

    def test_prompt_demands_clean_photograph(self):
        p = her_day._sfw_prompt(["Regan"], "having coffee at a cafe").lower()
        self.assertNotIn("instagram", p)
        for phrase in ("clean standalone photograph",
                       "not a social media post",
                       "no story frame",
                       "no text overlays",
                       "no usernames",
                       "no watermarks"):
            self.assertIn(phrase, p)

    def test_prompt_stays_sfw(self):
        p = her_day._sfw_prompt(["Regan"], "at the gym").lower()
        self.assertIn("fully clothed", p)
        self.assertIn("non-sexual", p)


if __name__ == "__main__":
    unittest.main()
