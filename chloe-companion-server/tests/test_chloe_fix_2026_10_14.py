"""Chloe fixes (2026-10-14): in-scene fallback fencing for Chloe's own
mouth, photo-drop placeholder/duplicate-card fix, and voice fail-fast
in the Gemini key manager.

Production evidence (deployment 8cf31152, pool fully down):
1. Chloe, ACTIVE stored scene, model failing: her turns were answered
   from her own Mouth fallback bank — "you always get interesting
   after midnight." / "keep talking. i like this — us, late." The
   scene fence's prompt-back shape check was tuned on the GENERIC
   bank (all 8 lines match) and catches only 2 of Chloe's 12 fallback
   lines. Fix: in an active scene, a draft that did not come from
   the model (post-retry) is always replaced with an in-scene line —
   origin beats shape, same _scene_fallback_line machinery.
2. Every 7th message, photos.maybe_drop_photo served a PLACEHOLDER
   manifest entry (no real photo exists in the library), the chat
   response carried "PLACEHOLDER:<id>", and the client rendered its
   photo-drop card — a built-in night-skyline SVG — captioned with
   her reply text, printing the reply twice. Fix: placeholders are
   never served; a real drop carries its own caption; the client
   renders the real photo URL.
3. Rover voice (X-Source: roof-rover-voice) calls the key-manager
   proxy per utterance; with the pool down, each post_json ran
   phase 3's backoff-ignoring sweep (up to the 8-attempt cap of
   guaranteed 429s, each re-stamping the key's backoff). Fix: voice
   sources fail fast — no believed-healthy and no eligible key means
   one decision, zero attempts. Chat behavior unchanged.

Run:  python -m pytest tests/test_chloe_fix_2026_10_14.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import brain, config, gemini_keys, photos  # noqa: E402

CHLOE = {"id": "chloe", "name": "Chloe", "persona": "Chloe is his girlfriend."}
BRIEF = ("You are my teacher and I am your student, alone after class "
         "in your classroom.")


def _ctx(char, **kw):
    ctx = {
        "name": "Matty",
        "character": char,
        "character_name": char.get("name"),
        "recent": [],
    }
    ctx.update(kw)
    return ctx


def _dead_model(calls):
    def dead(*a):
        calls["n"] += 1
        return None
    return dead


class _StubMouth:
    """Stands in for brain.Mouth, serving one forced bank line."""
    line = ""

    def generate(self, user_text, context):
        return type(self).line


class ChloeSceneFenceTest(unittest.TestCase):
    def _compose(self, user_text, ctx, model, cid="chloe"):
        with mock.patch.object(brain, "gemini_generate", model):
            return brain.compose_reply(user_text, ctx, cid)

    def test_model_down_active_scene_retries_once_then_scene_line(self):
        calls = {"n": 0}
        ctx = _ctx(CHLOE, scene_state="active", scene_brief=BRIEF)
        out = self._compose("you want to?", ctx, _dead_model(calls))
        self.assertEqual(calls["n"], 2)  # one retry, then the fence
        self.assertIn(out, brain._SCENE_FALLBACK_LINES)

    def test_every_chloe_fallback_bank_line_is_fenced_in_scene(self):
        # The exact production failure class: whatever line her own
        # bank picks mid-scene with the model down, the answer must
        # be an in-scene descriptive line — never the bank line.
        ctx = _ctx(CHLOE, scene_state="active", scene_brief=BRIEF)
        for raw in brain._LINES["fallback"]:
            line = raw.replace("{n}", "Matty")
            _StubMouth.line = line
            with mock.patch.object(brain, "Mouth", _StubMouth):
                out = self._compose("yeah", ctx, _dead_model({"n": 0}))
            self.assertIn(out, brain._SCENE_FALLBACK_LINES, raw)
            self.assertNotEqual(out, line)

    def test_two_production_lines_specifically(self):
        ctx = _ctx(CHLOE, scene_state="active", scene_brief=BRIEF)
        for line in ("you always get interesting after midnight.",
                     "keep talking. i like this — us, late."):
            _StubMouth.line = line
            with mock.patch.object(brain, "Mouth", _StubMouth):
                out = self._compose("you want to?", ctx,
                                    _dead_model({"n": 0}))
            self.assertIn(out, brain._SCENE_FALLBACK_LINES, line)

    def test_boundary_lines_survive_signal_only_heat(self):
        # Regression guard for the scope line: boundary traffic trips
        # the scene SIGNAL ("take it off") without any started scene.
        # Her boundary bank (hard no / quiet regroup) must still land —
        # the origin fence applies to started scenes only.
        ctx = _ctx(CHLOE)  # no scene_state, no lock
        _StubMouth.line = "no. i need a minute of quiet."
        with mock.patch.object(brain, "Mouth", _StubMouth):
            out = self._compose("take it off for an extra $50", ctx,
                                _dead_model({"n": 0}))
        self.assertEqual(out, "no. i need a minute of quiet.")

    def test_model_prompt_back_replaced_on_chloe_path(self):
        ctx = _ctx(CHLOE, scene_state="active", scene_brief=BRIEF)
        out = self._compose(
            "describe it", ctx,
            lambda *a: "keep talking. i like this — us, late.")
        self.assertIn(out, brain._SCENE_FALLBACK_LINES)

    def test_model_down_no_scene_chloe_unchanged(self):
        calls = {"n": 0}
        ctx = _ctx(CHLOE)
        out = self._compose("how was your day", ctx, _dead_model(calls))
        self.assertEqual(calls["n"], 1)  # no scene retry outside scenes
        self.assertNotIn(out, brain._SCENE_FALLBACK_LINES)
        self.assertTrue(out)

    def test_narration_rule_reaches_chloe_model_path(self):
        ctx = _ctx(CHLOE)
        text = brain._gemini_system_text(ctx, "chloe")
        self.assertIn(brain.SCENE_NARRATION_RULE, text)


class PhotoDropTest(unittest.TestCase):
    PLACEHOLDERS = [
        {"id": "placeholder-01", "file": "PLACEHOLDER_no_file_yet.jpg",
         "caption": "PLACEHOLDER — user curates real photo later",
         "placeholder": True, "tier_min": "plus"},
        {"id": "placeholder-02", "file": "PLACEHOLDER_no_file_yet.jpg",
         "caption": "PLACEHOLDER — user curates real photo later",
         "placeholder": True, "tier_min": "vip"},
    ]
    REAL = {"id": "drop-01", "file": "chloe_01.jpg",
            "caption": "thinking of you", "tier_min": "plus"}

    def test_placeholder_only_library_never_drops(self):
        with mock.patch.object(photos, "load_manifest",
                               return_value=list(self.PLACEHOLDERS)):
            for n in (7, 14, 21, 700):
                self.assertIsNone(photos.maybe_drop_photo("basic", n))

    def test_real_drop_served_with_its_own_caption(self):
        with mock.patch.object(
                photos, "load_manifest",
                return_value=list(self.PLACEHOLDERS) + [self.REAL]):
            drop = photos.maybe_drop_photo("basic", 7)
            self.assertIsNotNone(drop)
            self.assertEqual(drop["file"], "chloe_01.jpg")
            self.assertEqual(drop["caption"], "thinking of you")
            self.assertFalse(drop.get("placeholder"))

    def test_client_renders_real_photo_not_skyline_art(self):
        # The bug lived in the client renderer: it ignored the served
        # URL, painted a built-in night-skyline SVG, and captioned the
        # card with the reply text. Pin the renderer's contract.
        app_js = os.path.join(os.path.dirname(__file__), "..", "..",
                              "chloe-companion-app", "js", "app.js")
        with open(app_js) as f:
            src = f.read()
        start = src.index("function addPhotoDrop")
        body = src[start:src.index("\n  }", start)]
        self.assertIn('<img src="${url}"', body)
        self.assertNotIn("240 90", body)  # the skyline SVG is gone
        self.assertIn("addPhotoDrop(res.photo_url, res.photo_caption",
                      src)


class VoiceFailFastTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self._orig_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._orig_legacy = config.GEMINI_API_KEY
        config.GEMINI_API_KEY = ""
        gemini_keys._belief.clear()
        gemini_keys._belief_seeded = False
        gemini_keys._dispatch_times.clear()
        gemini_keys._recent_fail_ts.clear()
        gemini_keys._latencies.clear()
        gemini_keys._last_probe_mono.clear()
        gemini_keys._last_reserve_tap = None
        self._env = {}
        for name in ("GEMINI_MAX_ATTEMPTS", "GEMINI_FAST_TIMEOUT_S",
                     "GEMINI_ATTEMPT_BUDGET_S", "GEMINI_HEDGE_AFTER_S",
                     "GEMINI_PER_KEY_RPM", "GEMINI_RESERVE_COUNT",
                     "GEMINI_PROBER"):
            self._env[name] = os.environ.pop(name, None)

    def tearDown(self):
        config.DB_PATH = self._orig_db_path
        config.GEMINI_API_KEY = self._orig_legacy
        for name, val in self._env.items():
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _pool_down(self, n=3):
        keys = [gemini_keys.add_key(f"down{i}", f"d{i}", False)
                for i in range(n)]
        for k in keys:
            gemini_keys.report_failure(k["id"], "quota")
        return keys

    def _patch_http(self, behavior):
        calls = []

        def fake(url, payload, key_value, timeout):
            calls.append(key_value)
            return behavior(key_value)

        patcher = mock.patch.object(gemini_keys, "_http_post",
                                    side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_voice_pool_down_fails_fast_zero_attempts(self):
        self._pool_down()
        calls = self._patch_http(lambda kv: {"ok": True})
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="roof-rover-voice")
        self.assertIsNone(result)
        self.assertEqual(calls, [])  # one decision, no per-key sweep

    def test_chat_pool_down_behavior_unchanged(self):
        self._pool_down()

        def behavior(key_value):
            raise urllib.error.HTTPError(
                "https://example.test/gen", 429, "err", None, None)

        calls = self._patch_http(behavior)
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="whispr-chat")
        self.assertIsNone(result)
        # Chat still runs the last-resort sweep (legacy behavior).
        self.assertGreater(len(calls), 0)

    def test_voice_healthy_key_still_serves(self):
        key = gemini_keys.add_key("voice-ok", "v", False)
        gemini_keys.report_success(key["id"])
        calls = self._patch_http(lambda kv: {"ok": True})
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="roof-rover-voice")
        self.assertIsNotNone(result)
        self.assertEqual(calls, ["voice-ok"])

    def test_explicit_fail_fast_param_for_other_sources(self):
        self._pool_down()
        calls = self._patch_http(lambda kv: {"ok": True})
        result = gemini_keys.post_json(
            "https://example.com/gen", {"x": 1}, source="batch-job",
            fail_fast=True)
        self.assertIsNone(result)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
