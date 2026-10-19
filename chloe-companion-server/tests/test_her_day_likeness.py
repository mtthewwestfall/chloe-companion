"""Her Day likeness lock tests (2026-10-19, improvement #1).

Every Her Day image — Sogni render, Gemini fallback render, today's
cached file, or a pooled prior render — must pass a vision identity
check against the character's avatar before it can be attached to a
post. A clear mismatch regenerates once, then the pictured post is
skipped; a checker failure is fail-closed (skip, never post
unchecked). Anime characters are judged style-aware.

The render seam (her_day._render_image_plan) and the verdict seam
(media_gen.likeness_verdict) are mocked; media_gen-level tests mock
the key pool (gemini_keys.post_json) and assert prompt selection.

Run:  python -m unittest tests.test_her_day_likeness -v
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import config, her_day, media_gen  # noqa: E402

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
_PNG_URI = "data:image/png;base64," + base64.b64encode(_PNG).decode()


class HerDayLikenessGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = config.GENERATED_DIR
        config.GENERATED_DIR = self._tmp.name
        self._old_env = os.environ.pop("HER_DAY_LIKENESS", None)
        her_day._LIKENESS_MEMO.clear()
        self.render_calls = 0
        self.verdict_calls = []
        self._src = os.path.join(self._tmp.name, "render-src.jpg")
        with open(self._src, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"r" * 2048)
        self._patches = [
            mock.patch.object(her_day, "_plan_session",
                              return_value={"token": "t",
                                            "refreshToken": "r"}),
            mock.patch.object(her_day, "_render_image_plan",
                              side_effect=self._render),
            mock.patch.object(media_gen, "_fetch_reference",
                              return_value=None),
            mock.patch.object(media_gen, "likeness_verdict",
                              side_effect=self._verdict),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        config.GENERATED_DIR = self._old_dir
        if self._old_env is not None:
            os.environ["HER_DAY_LIKENESS"] = self._old_env
        her_day._LIKENESS_MEMO.clear()
        self._tmp.cleanup()

    def _render(self, session, prompt, ref_path):
        self.render_calls += 1
        return {"status": "done", "path": self._src}

    def _verdict(self, girl, url):
        """Scripted per-test via self.verdict_script: a list consumed
        in call order, or a {girl_id: verdict} dict."""
        self.verdict_calls.append(girl.get("id"))
        script = self.verdict_script
        if isinstance(script, dict):
            return script[girl.get("id")]
        return script.pop(0)

    verdict_script: list | dict = ["yes"]

    def _call(self, names=None, avatar="https://x/avatar.jpg",
              participants=None, moment="gym"):
        return her_day._generate_sfw_image(
            names or ["Chloe"], avatar, participants or ["chloe"],
            moment, "selfie at the gym", "2026-10-19")

    def _dest(self, participants=None, moment="gym"):
        return her_day._herday_image_path(
            participants or ["chloe"], moment, "2026-10-19")

    def test_match_passes(self):
        self.verdict_script = ["yes"]
        url = self._call()
        self.assertTrue(url.startswith("/generated/"))
        self.assertEqual(self.render_calls, 1)
        self.assertTrue(os.path.exists(self._dest()))

    def test_mismatch_regenerates_then_passes(self):
        self.verdict_script = ["no", "yes"]
        url = self._call()
        self.assertTrue(url.startswith("/generated/"))
        self.assertEqual(self.render_calls, 2)

    def test_double_mismatch_skips_posting(self):
        self.verdict_script = ["no", "no"]
        self.assertEqual(self._call(), "")
        self.assertEqual(self.render_calls, 2)
        self.assertFalse(os.path.exists(self._dest()))

    def test_uncertain_twice_skips_posting(self):
        self.verdict_script = ["uncertain", "uncertain"]
        self.assertEqual(self._call(), "")
        self.assertEqual(self.render_calls, 2)
        self.assertFalse(os.path.exists(self._dest()))

    def test_checker_error_skips_without_regen(self):
        self.verdict_script = ["error"]
        self.assertEqual(self._call(), "")
        self.assertEqual(self.render_calls, 1)
        self.assertFalse(os.path.exists(self._dest()))

    def test_no_reference_posts_unchecked(self):
        self.verdict_script = ["no_reference"]
        url = self._call(avatar="")
        self.assertTrue(url.startswith("/generated/"))
        self.assertEqual(self.render_calls, 1)

    def test_group_partner_mismatch_rejects(self):
        self.verdict_script = {"chloe": "yes", "mara": "no"}
        girls = [{"id": "chloe", "name": "Chloe", "avatar_url": "a"},
                 {"id": "mara", "name": "Mara", "avatar_url": "b"}]
        with mock.patch.object(her_day, "_get_all_girls",
                               return_value=girls):
            url = self._call(names=["Chloe", "Mara"],
                             participants=["chloe", "mara"],
                             moment="gym_buddies")
        self.assertEqual(url, "")
        self.assertIn("mara", self.verdict_calls)
        self.assertEqual(self.render_calls, 2)

    def test_cached_mismatch_is_dropped_and_rerendered(self):
        dest = self._dest()
        with open(dest, "wb") as f:
            f.write(b"OLD-BAD-RENDER")
        self.verdict_script = ["no", "yes"]
        url = self._call()
        self.assertTrue(url.startswith("/generated/"))
        self.assertEqual(self.render_calls, 1)
        with open(dest, "rb") as f:
            self.assertNotEqual(f.read(), b"OLD-BAD-RENDER")

    def test_cached_file_not_served_when_checker_down(self):
        dest = self._dest()
        with open(dest, "wb") as f:
            f.write(b"UNVERIFIED")
        self.verdict_script = ["error"]
        self.assertEqual(self._call(), "")
        self.assertEqual(self.render_calls, 0)
        # Fail-closed, not destructive: the file survives for a later
        # verification once the checker recovers.
        self.assertTrue(os.path.exists(dest))

    def test_cached_match_serves_without_render(self):
        dest = self._dest()
        with open(dest, "wb") as f:
            f.write(b"GOOD")
        self.verdict_script = ["yes"]
        url = self._call()
        self.assertTrue(url.startswith("/generated/"))
        self.assertEqual(self.render_calls, 0)


class LikenessVerdictStyleTest(unittest.TestCase):
    """media_gen.likeness_verdict: style-aware prompt selection and
    the no_reference / error split the Her Day gate relies on."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = config.GENERATED_DIR
        config.GENERATED_DIR = self._tmp.name
        self.gen_path = os.path.join(self._tmp.name, "cand.jpg")
        with open(self.gen_path, "wb") as f:
            f.write(_PNG)
        self.captured = {}
        self._orig_post = media_gen.gemini_keys.post_json
        media_gen.gemini_keys.post_json = self._post

    def tearDown(self):
        media_gen.gemini_keys.post_json = self._orig_post
        config.GENERATED_DIR = self._old_dir
        self._tmp.cleanup()

    def _post(self, url, payload, timeout=30, source="unknown", **kw):
        self.captured = {"url": url, "payload": payload, "source": source}
        return {"candidates": [{"content": {"parts": [
            {"text": '{"same_person": "yes"}'}]}}]}

    def _prompt(self):
        return self.captured["payload"]["contents"][0]["parts"][0]["text"]

    def test_anime_character_gets_illustration_prompt(self):
        for gid in ("rina", "sabrina"):
            self.captured = {}
            girl = {"id": gid, "avatar_url": _PNG_URI}
            verdict = media_gen.likeness_verdict(girl, "/generated/cand.jpg")
            self.assertEqual(verdict, "yes")
            prompt = self._prompt().lower()
            self.assertIn("anime", prompt)
            self.assertIn("illustration", prompt)

    def test_photo_character_gets_photo_prompt(self):
        girl = {"id": "chloe", "avatar_url": _PNG_URI}
        verdict = media_gen.likeness_verdict(girl, "/generated/cand.jpg")
        self.assertEqual(verdict, "yes")
        prompt = self._prompt().lower()
        self.assertIn("two photos", prompt)
        self.assertNotIn("anime", prompt)

    def test_male_character_gets_neutral_prompt(self):
        girl = {"id": "jake", "avatar_url": _PNG_URI}
        verdict = media_gen.likeness_verdict(girl, "/generated/cand.jpg")
        self.assertEqual(verdict, "yes")
        prompt = self._prompt().lower()
        self.assertIn("person", prompt)
        self.assertNotIn("woman", prompt)

    def test_no_reference_is_distinct_from_error(self):
        girl = {"id": "chloe", "avatar_url": ""}
        self.assertEqual(
            media_gen.likeness_verdict(girl, "/generated/cand.jpg"),
            "no_reference")

    def test_pool_down_is_error_and_face_verdict_stays_unavailable(self):
        media_gen.gemini_keys.post_json = lambda *a, **k: None
        girl = {"id": "chloe", "avatar_url": _PNG_URI}
        self.assertEqual(
            media_gen.likeness_verdict(girl, "/generated/cand.jpg"),
            "error")
        # PR #52 gate contract unchanged: checker failure is
        # 'unavailable' there (fail-open).
        self.assertEqual(
            media_gen._face_verdict(girl, "/generated/cand.jpg"),
            "unavailable")


class CharacterAnimeTest(unittest.TestCase):
    def test_anime_roster(self):
        from app import characters
        self.assertTrue(characters.character_is_anime("rina"))
        self.assertTrue(characters.character_is_anime("Sabrina"))
        self.assertFalse(characters.character_is_anime("chloe"))
        self.assertFalse(characters.character_is_anime(""))
        self.assertFalse(characters.character_is_anime(None))


if __name__ == "__main__":
    unittest.main()
