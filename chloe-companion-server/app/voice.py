"""Liora voice notes via xAI TTS — STUBBED / DISABLED by default.

Rules:
- No API key configured (CHLOE_XAI_API_KEY) -> returns None. Always.
- Feature flag off (CHLOE_VOICE_ENABLED=1) -> returns None. Always.
- Even when enabled, THIS PHASE never makes a network call: it returns a
  clearly-marked stub URL so the chat contract (voice_note_url) can be
  exercised end-to-end. Paid TTS needs explicit user approval before any
  real use — see README.
"""
from __future__ import annotations

from . import config

# xAI TTS reference (from the existing Keyhole backend): $15 / 1M chars,
# ~$0.0045 per voice note. Fingerprint: 24 kHz mono MP3, no ID3 header.
XAI_TTS_ENDPOINT = "https://api.x.ai/v1/audio/speech"
LIORA_VOICE = "liora"  # voice name per user's live setup
EST_COST_PER_NOTE_USD = 0.0045


def maybe_generate_voice_note(text: str, user_id: str) -> str | None:
    """Return a voice_note_url, or None when voice is unavailable/disabled.

    STUB: no network calls. Returns None unless BOTH the key and the flag
    are set; when both are set it returns a placeholder URL (still no
    network — the real call is a Phase 3 step after user approval).
    """
    if not config.XAI_API_KEY:
        return None
    if not config.VOICE_NOTES_ENABLED:
        return None
    # TODO(phase3, needs user approval): POST to XAI_TTS_ENDPOINT with the
    # key, store the MP3, return its URL. Never call without approval.
    safe = "".join(c if c.isalnum() else "_" for c in user_id)[:16]
    return f"STUB_TTS_DISABLED://voice/{safe}/note.mp3"
