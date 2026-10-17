"""Liora voice notes via ElevenLabs TTS.

Gate: requires ELEVENLABS_API_KEY AND CHLOE_VOICE_ENABLED=1 (see
app/config.py: config.ELEVENLABS_API_KEY / config.VOICE_NOTES_ENABLED).
When the gate is closed the function returns None gracefully — it never
raises and never makes a network call.

On success the MP3 is saved under config.GENERATED_DIR (served by the
app at /generated/) and the /generated/<file> URL is returned, so the
URL passes her_day.validate_her_day_media_url and plays in the app.

API (ElevenLabs docs, "Text to speech"):
    POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
    Headers: xi-api-key: <key>, Content-Type: application/json
    {"text": ..., "model_id": "eleven_multilingual_v2"}
    -> raw audio bytes (audio/mpeg)

Cost reference: ElevenLabs bills per character (multilingual v2 =
1 credit/char); ~$0.05 per 280-char voice note at list credit rates.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import time
import urllib.error
import urllib.request
import uuid

from . import config

# ElevenLabs TTS endpoint (docs: "Text to speech"). Override with
# ELEVENLABS_TTS_ENDPOINT if the deployment ever targets a different
# host (e.g. a proxy).
ELEVENLABS_TTS_ENDPOINT = os.environ.get(
    "ELEVENLABS_TTS_ENDPOINT", "https://api.elevenlabs.io/v1/text-to-speech")
# Default synthesis model. Override with ELEVENLABS_MODEL_ID.
ELEVENLABS_MODEL_ID = os.environ.get(
    "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")

LIORA_VOICE = "liora"  # voice name per user's live setup

# Character voice name -> ElevenLabs voice ID. A character's
# companion_voice ("liora", "rex", ...) selects the entry. The values
# below are ElevenLabs' public stock voices — used until the owner maps
# each character to its real voice ID via ELEVENLABS_VOICE_ID_<NAME>
# (e.g. ELEVENLABS_VOICE_ID_LIORA) or the ELEVENLABS_VOICE_ID default.
_BUILTIN_VOICE_IDS = {
    "liora": "21m00Tcm4TlvDq8ikWAM",  # Rachel (female)
    "rex": "pNInz6obpgDQGcFmaJgB",  # Adam (male)
}

EST_COST_PER_NOTE_USD = 0.05

# Billing/latency guard: chat only asks for <= 280 chars, but other
# callers can pass longer text — clamp so one note can't balloon the bill.
MAX_TTS_CHARS = 1000
_TTS_TIMEOUT_S = 60


def _log(msg: str) -> None:
    print(f"[voice] {msg}", flush=True)


def _voice_id_for(voice_name: str) -> str:
    """Resolve a character voice name to an ElevenLabs voice ID.

    Precedence: ELEVENLABS_VOICE_ID_<NAME> (e.g.
    ELEVENLABS_VOICE_ID_LIORA), then ELEVENLABS_VOICE_ID, then the
    built-in stock-voice mapping above.
    """
    name = (voice_name or "").strip().lower() or LIORA_VOICE
    per_name = os.environ.get(
        f"ELEVENLABS_VOICE_ID_{name.upper()}", "").strip()
    if per_name:
        return per_name
    default = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    if default:
        return default
    return _BUILTIN_VOICE_IDS.get(name, _BUILTIN_VOICE_IDS[LIORA_VOICE])


def _extract_audio(body: bytes) -> bytes:
    """Decode an ElevenLabs TTS response into raw audio bytes.

    Success returns the raw audio bytes (audio/mpeg). A JSON body on a
    200 is unexpected (error details normally ride on a non-200 status)
    but handled defensively.
    """
    if body[:1] in (b"{", b"["):
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("audio"):
            try:
                audio = base64.b64decode(payload["audio"])
            except (binascii.Error, ValueError, TypeError) as e:
                raise RuntimeError(
                    f"TTS audio field is not valid base64: {e}")
            if not audio:
                raise RuntimeError("TTS returned an empty audio field")
            return audio
        raise RuntimeError(
            f"TTS returned unexpected JSON: {body[:120]!r}")
    if not body:
        raise RuntimeError("TTS returned an empty response body")
    return body


def _synthesize(text: str, voice_id: str, api_key: str) -> bytes:
    """POST the text to ElevenLabs TTS. Returns raw MP3 bytes; raises."""
    payload = json.dumps({
        "text": text,
        "model_id": ELEVENLABS_MODEL_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{ELEVENLABS_TTS_ENDPOINT}/{voice_id}",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
            "xi-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TTS_TIMEOUT_S) as resp:
            status = getattr(resp, "status", 200)
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"ElevenLabs TTS HTTP {e.code}: {detail}")
    if status != 200:
        raise RuntimeError(f"ElevenLabs TTS HTTP {status}")
    return _extract_audio(body)


def _save_mp3(audio: bytes, user_id: str) -> str:
    """Store the MP3 under GENERATED_DIR; return its /generated/ URL."""
    os.makedirs(config.GENERATED_DIR, exist_ok=True)
    safe = ("".join(c if c.isalnum() else "_" for c in (user_id or "anon"))
            [:16] or "anon")
    fname = f"voice_{safe}_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp3"
    dest = os.path.join(config.GENERATED_DIR, fname)
    with open(dest, "wb") as fh:
        fh.write(audio)
    return "/generated/" + fname


def maybe_generate_voice_note(text: str, user_id: str, voice_name: str = "liora",
                              quota: bool = True, consume=None) -> str | None:
    """Generate a voice note with ElevenLabs TTS, returning its URL.

    Returns None (never raises) when voice is unavailable: quota=False,
    the feature flag is off, the API key is missing, the request fails,
    is empty.
    `consume` runs only after a note is generated and saved.

    `voice_name` is the character's voice (companion_voice from the
    roster; Chloe is "liora") — resolved to an ElevenLabs voice ID via
    _voice_id_for.
    """
    if not quota or not config.VOICE_NOTES_ENABLED:
        return None
    if not config.ELEVENLABS_API_KEY:
        _log("ELEVENLABS_API_KEY is not set; voice note unavailable "
             "(returning None)")
        return None
    clean = (text or "").strip()
    if not clean:
        return None
    if len(clean) > MAX_TTS_CHARS:
        clean = clean[:MAX_TTS_CHARS]
    vid = _voice_id_for(voice_name)
    try:
        audio = _synthesize(clean, vid, config.ELEVENLABS_API_KEY)
    except Exception as e:  # noqa: BLE001 — any TTS failure is a soft None
        _log(f"ElevenLabs TTS request failed ({type(e).__name__}: {e}); "
             "returning None")
        return None
    try:
        url = _save_mp3(audio, user_id)
    except OSError as e:
        _log(f"voice note save failed: {e}; returning None")
        return None
    _log(f"generated voice note for user={user_id} voice={voice_name} "
         f"({len(audio)} bytes)")
    return (consume(), url)[1] if consume is not None else url
