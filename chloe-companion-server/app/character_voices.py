"""Per-character ElevenLabs voice IDs (2026-10-16).

Character depth: every character gets her/his OWN distinct ElevenLabs voice
for voice notes, so Mara never speaks with Chloe's voice.

How it ties into PR #24 ("Switch voice TTS to ElevenLabs"): that PR resolves
a character's voice from her `companion_voice` NAME via
ELEVENLABS_VOICE_ID_<NAME> env vars with stock-voice defaults. The gap it
leaves: the admin roster defaults every unset companion_voice to "liora", so
without this module Mara, Isabella, Regan and the rest would all share
Chloe's voice ID.

Resolution order in elevenlabs_voice_id_for():
  1. ELEVENLABS_VOICE_ID_<CHARACTER_ID> env var (per-character override --
     the operator's explicit casting choice wins).
  2. The built-in per-character stock defaults below (each character a
     distinct stock voice).
  3. PR #24's voice-name resolution (voice.elevenlabs_voice_id), once that
     PR is merged -- honors a deliberately chosen companion_voice name.
  4. The "liora" default (Rachel stock voice) -- the flagship never breaks.

Tie-in for PR #24: voice.elevenlabs_voice_id(character) can delegate here
with a one-line change:

    from . import character_voices
    def elevenlabs_voice_id(character=None):
        return character_voices.elevenlabs_voice_id_for(character or {})

The stock IDs below are ElevenLabs' public shared-voice IDs (the same pool
PR #24 draws its defaults from).
"""
from __future__ import annotations

import os
from typing import Any

# Distinct stock ElevenLabs voice per character. Chloe keeps the flagship
# "liora" default (Rachel's stock voice) so her existing voice notes don't
# change; everyone else gets their own voice.
CHARACTER_VOICE_IDS: dict[str, str] = {
    # The six deepened characters.
    "chloe": "21m00Tcm4TlvDq8ikWAM",  # Rachel -- warm flagship
    "mara": "EXAVITQu4vr4xnSDxMaL",  # Bella
    "isabella": "XB0fDUnXU5powFXDhCwa",  # Charlotte
    "regan": "ThT5KcBeYPX3keUQqHPh",  # Dorothy
    "brooklyn": "pFZP5JQG7iQjIQuC4Bku",  # Lily
    "hannah": "cgSgspJ2msm6clMCkdW9O",  # Jessica
    # Rest of the roster -- distinct voices too, so no two characters share.
    "mia": "LcfcDJNUP1GQjkzn1xUU",  # Emily
    "jordan": "9BWtsMINIsypzbvi2QgB",  # Aria
    "jake": "ErXwobaYiN019PkySvjV",  # Antoni
    "eli": "TxGEqnHWrfWFTfGW9XjX",  # Josh
    "rocky": "pNInz6obpgDQGcFmaJgB",  # Adam
}

DEFAULT_VOICE_ID = CHARACTER_VOICE_IDS["chloe"]


def elevenlabs_voice_id_for(character: dict[str, Any] | None) -> str:
    """Resolve the ElevenLabs voice ID for one character.

    Accepts the characters.py character dict (or a bare {"id": ...}).
    Never raises -- the voice path must survive a misconfigured roster.
    """
    cid = ""
    try:
        cid = str((character or {}).get("id") or "").strip().lower()
    except Exception:  # noqa: BLE001 -- voice resolution never breaks chat
        cid = ""
    if cid:
        env_id = (os.environ.get(f"ELEVENLABS_VOICE_ID_{cid.upper()}") or "").strip()
        if env_id:
            return env_id
        if cid in CHARACTER_VOICE_IDS:
            return CHARACTER_VOICE_IDS[cid]
    # Fall through to PR #24's voice-name resolution when it's available
    # (merged): a deliberately chosen companion_voice name is honored for
    # characters without an explicit per-character ID.
    try:
        from . import voice as _voice
        resolve = getattr(_voice, "elevenlabs_voice_id", None)
        if callable(resolve):
            return resolve(character or {}) or DEFAULT_VOICE_ID
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_VOICE_ID


def voice_id_report() -> list[dict[str, str]]:
    """Distinctness audit: one row per known character id.

    Reflects env overrides, so this shows the EFFECTIVE mapping. Used by
    ops/tests to verify no two characters share a voice ID.
    """
    return [
        {"character_id": cid,
         "elevenlabs_voice_id": elevenlabs_voice_id_for({"id": cid})}
        for cid in sorted(CHARACTER_VOICE_IDS)
    ]
