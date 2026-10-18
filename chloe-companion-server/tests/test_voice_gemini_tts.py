"""Voice notes on Gemini TTS (2026-10-17, owner-directed engine swap).

ElevenLabs was the synthesis engine (PR #24/#26) until the owner
ordered "Forget ElevenLabs, just use Gemini". These tests pin the new
path: synthesis goes through app/gemini_keys.post_json (the shared
key manager) with a Gemini TTS generateContent payload, each character
resolves to her own Gemini prebuilt voice, the returned headerless PCM
is wrapped into a WAV the app can play, and the CHLOE_VOICE_ENABLED
gate / quota behavior is unchanged. The ElevenLabs API key is NOT
required anymore.

Synthesis is mocked at the pool boundary (gemini_keys.post_json) —
no network, no keys.

Run:  python -m unittest tests.test_voice_gemini_tts -v
"""
from __future__ import annotations

import base64
import io
import os
import sys
import tempfile
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import character_voices, config, gemini_keys, voice  # noqa: E402

# 0.1s of 24kHz 16-bit mono "audio" (silence is fine — shape is what
# matters here, not sound).
_PCM = b"\x01\x02" * 2400
_PCM_B64 = base64.b64encode(_PCM).decode("ascii")


def _gemini_tts_ok(pcm_b64: str = _PCM_B64, rate: int = 24000) -> dict:
    return {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": f"audio/L16;codec=pcm;rate={rate}",
                        "data": pcm_b64}}]}}]}


class GeminiVoiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_generated = config.GENERATED_DIR
        self._old_enabled = config.VOICE_NOTES_ENABLED
        self._old_el_key = config.ELEVENLABS_API_KEY
        config.GENERATED_DIR = self.tmp.name
        config.VOICE_NOTES_ENABLED = True
        self.addCleanup(self._restore_config)
        # Hermetic voice env: no operator overrides leaking in.
        self._env_stash = {k: v for k, v in os.environ.items()
                           if k.startswith("GEMINI_TTS_VOICE")}
        for k in self._env_stash:
            del os.environ[k]
        self.addCleanup(self._restore_env)
        self.calls = []

        def fake_post_json(url, payload, timeout=30, source="unknown",
                           **kw):
            self.calls.append({"url": url, "payload": payload,
                               "timeout": timeout, "source": source})
            return _gemini_tts_ok()

        self._post_patch = mock.patch.object(
            gemini_keys, "post_json", side_effect=fake_post_json)
        self._post_patch.start()
        self.addCleanup(self._post_patch.stop)

    def _restore_config(self):
        config.GENERATED_DIR = self._old_generated
        config.VOICE_NOTES_ENABLED = self._old_enabled
        config.ELEVENLABS_API_KEY = self._old_el_key

    def _restore_env(self):
        for k in list(os.environ):
            if k.startswith("GEMINI_TTS_VOICE"):
                del os.environ[k]
        os.environ.update(self._env_stash)

    # -- request shape ----------------------------------------------------

    def test_request_shape_through_key_manager(self):
        url = voice.maybe_generate_voice_note(
            "Hi Matty, miss you.", "user1", character_id="chloe")
        self.assertIsNotNone(url)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertTrue(call["url"].endswith(
            "gemini-2.5-flash-preview-tts:generateContent"), call["url"])
        self.assertEqual(call["source"], "whispr-voice")
        payload = call["payload"]
        self.assertEqual(payload["contents"],
                         [{"parts": [{"text": "Hi Matty, miss you."}]}])
        gen = payload["generationConfig"]
        self.assertEqual(gen["responseModalities"], ["AUDIO"])
        vname = (gen["speechConfig"]["voiceConfig"]
                 ["prebuiltVoiceConfig"]["voiceName"])
        self.assertEqual(vname, "Sulafat")  # Chloe's Gemini voice

    def test_per_character_voice_selection(self):
        cases = {"mara": "Leda", "isabella": "Despina",
                 "regan": "Erinome", "mia": "Zephyr",
                 "jordan": "Aoede", "rocky": "Charon",
                 "jake": "Alnilam", "eli": "Schedar"}
        for cid, want in cases.items():
            self.calls.clear()
            url = voice.maybe_generate_voice_note(
                "hey", "user1", character_id=cid)
            self.assertIsNotNone(url, cid)
            gen = self.calls[0]["payload"]["generationConfig"]
            got = (gen["speechConfig"]["voiceConfig"]
                   ["prebuiltVoiceConfig"]["voiceName"])
            self.assertEqual(got, want, cid)

    def test_default_voice_for_unassigned(self):
        # Known voice name, no character: the name map applies.
        self.assertEqual(voice._gemini_voice_for("liora", ""), "Sulafat")
        self.assertEqual(voice._gemini_voice_for("rex", ""), "Charon")
        # Nothing known at all: the sensible female default.
        self.assertEqual(voice._gemini_voice_for("mystery", "nobody"),
                         voice.DEFAULT_GEMINI_VOICE)
        self.assertEqual(voice.DEFAULT_GEMINI_VOICE, "Kore")

    def test_env_override_wins(self):
        os.environ["GEMINI_TTS_VOICE_MARA"] = "Aoede"
        self.assertEqual(voice._gemini_voice_for("liora", "mara"), "Aoede")
        os.environ["GEMINI_TTS_VOICE"] = "Kore"
        self.assertEqual(voice._gemini_voice_for("mystery", ""), "Kore")

    def test_character_map_is_distinct(self):
        names = list(voice._CHARACTER_GEMINI_VOICES.values())
        self.assertEqual(len(names), len(set(names)))

    def test_character_voices_facade(self):
        self.assertEqual(
            character_voices.gemini_voice_name_for(
                {"id": "mara", "companion_voice": "liora"}), "Leda")
        self.assertEqual(
            character_voices.gemini_voice_name_for({"id": "chloe"}),
            "Sulafat")
        report = character_voices.gemini_voice_report()
        self.assertEqual({r["character_id"] for r in report},
                         set(voice._CHARACTER_GEMINI_VOICES))

    # -- audio wrapping -----------------------------------------------------

    def test_wav_wrapping_and_saved_file(self):
        url = voice.maybe_generate_voice_note(
            "wrapping check", "user9", character_id="mia")
        self.assertTrue(url.startswith("/generated/"), url)
        self.assertTrue(url.endswith(".wav"), url)
        path = os.path.join(self.tmp.name, os.path.basename(url))
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as fh:
            blob = fh.read()
        with wave.open(io.BytesIO(blob), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getframerate(), 24000)
            self.assertEqual(wf.readframes(wf.getnframes()), _PCM)

    def test_sample_rate_follows_response_mimetype(self):
        with mock.patch.object(
                gemini_keys, "post_json",
                return_value=_gemini_tts_ok(rate=16000)):
            url = voice.maybe_generate_voice_note(
                "rate check", "user9", character_id="mia")
        path = os.path.join(self.tmp.name, os.path.basename(url))
        with open(path, "rb") as fh:
            blob = fh.read()
        with wave.open(io.BytesIO(blob), "rb") as wf:
            self.assertEqual(wf.getframerate(), 16000)

    # -- gates & failure behavior -------------------------------------------

    def test_gate_off_and_no_quota_make_no_call(self):
        config.VOICE_NOTES_ENABLED = False
        self.assertIsNone(voice.maybe_generate_voice_note(
            "hi", "user1", character_id="chloe"))
        config.VOICE_NOTES_ENABLED = True
        self.assertIsNone(voice.maybe_generate_voice_note(
            "hi", "user1", quota=False, character_id="chloe"))
        self.assertEqual(self.calls, [])

    def test_elevenlabs_key_no_longer_required(self):
        config.ELEVENLABS_API_KEY = ""
        url = voice.maybe_generate_voice_note(
            "no elevenlabs anymore", "user1", character_id="chloe")
        self.assertIsNotNone(url)
        self.assertEqual(len(self.calls), 1)

    def test_pool_failure_is_soft_none(self):
        with mock.patch.object(gemini_keys, "post_json", return_value=None):
            self.assertIsNone(voice.maybe_generate_voice_note(
                "hi", "user1", character_id="chloe"))
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_no_audio_in_response_is_soft_none(self):
        with mock.patch.object(
                gemini_keys, "post_json", return_value={"candidates": []}):
            self.assertIsNone(voice.maybe_generate_voice_note(
                "hi", "user1", character_id="chloe"))

    def test_consume_runs_only_on_success(self):
        seen = []
        url = voice.maybe_generate_voice_note(
            "hi", "user1", character_id="chloe",
            consume=lambda: seen.append(1))
        self.assertIsNotNone(url)
        self.assertEqual(seen, [1])
        with mock.patch.object(gemini_keys, "post_json", return_value=None):
            self.assertIsNone(voice.maybe_generate_voice_note(
                "hi", "user1", character_id="chloe",
                consume=lambda: seen.append(2)))
        self.assertEqual(seen, [1])

    def test_text_clamped_to_max_chars(self):
        voice.maybe_generate_voice_note(
            "x" * 1500, "user1", character_id="chloe")
        text = self.calls[0]["payload"]["contents"][0]["parts"][0]["text"]
        self.assertEqual(len(text), voice.MAX_TTS_CHARS)


if __name__ == "__main__":
    unittest.main()
