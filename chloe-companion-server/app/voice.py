"""Liora voice notes via xAI TTS.

Gate: requires CHLOE_XAI_API_KEY AND CHLOE_VOICE_ENABLED=1 (see
app/config.py: config.XAI_API_KEY / config.VOICE_NOTES_ENABLED). When
the gate is closed the function returns None gracefully — it never
raises and never makes a network call.

On success the MP3 is saved under config.GENERATED_DIR (served by the
app at /generated/) and the /generated/<file> URL is returned, so the
URL passes her_day.validate_her_day_media_url and plays in the app.

API (xAI docs, "Text to speech - REST"):
    POST https://api.x.ai/v1/tts
    {"text": ..., "voice_id": "liora", "language": "en"}
    -> JSON {"audio": "<base64>", "content_type": "audio/mpeg", ...}
The older code audit referenced the /v1/audio/speech endpoint; the
endpoint is overridable with CHLOE_XAI_TTS_ENDPOINT if the team still
uses it.

Cost reference (from the existing Keyhole backend): $15 / 1M chars,
~$0.0045 per voice note.
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

# Current xAI TTS endpoint (docs: "Text to speech - REST"). Override with
# CHLOE_XAI_TTS_ENDPOINT if the deployment still targets the legacy
# /v1/audio/speech endpoint.
XAI_TTS_ENDPOINT = os.environ.get(
    "CHLOE_XAI_TTS_ENDPOINT", "https://api.x.ai/v1/tts")
LIORA_VOICE = "liora"  # voice name per user's live setup
EST_COST_PER_NOTE_USD = 0.0045

# Billing/latency guard: chat only asks for <= 280 chars, but other
# callers can pass longer text — clamp so one note can't balloon the bill.
MAX_TTS_CHARS = 1000
_TTS_TIMEOUT_S = 60


def _log(msg: str) -> None:
    print(f"[voice] {msg}", flush=True)


def _extract_audio(body: bytes) -> bytes:
    """Decode an xAI /v1/tts response into raw audio bytes.

    The documented shape is JSON with a base64 "audio" field; some
    builds return the raw audio bytes instead — handle both.
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
    if not body:
        raise RuntimeError("TTS returned an empty response body")
    return body


def _synthesize(text: str, voice_id: str, api_key: str) -> bytes:
    """POST the text to xAI TTS. Returns raw MP3 bytes; raises on failure."""
    payload = json.dumps({
        "text": text,
        "voice_id": voice_id,
        "language": "en",
    }).encode("utf-8")
    req = urllib.request.Request(
        XAI_TTS_ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
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
        raise RuntimeError(f"xAI TTS HTTP {e.code}: {detail}")
    if status != 200:
        raise RuntimeError(f"xAI TTS HTTP {status}")
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


def maybe_generate_voice_note(text: str, user_id: str,
                              voice_name: str = "liora") -> str | None:
    """Generate a voice note with xAI TTS, returning its /generated/ URL.

    Returns None (never raises) when voice is unavailable: the feature
    flag is off, the API key is missing, the request fails, or the text
    is empty. Callers must only consume a voice-note credit when this
    returns a URL.

    `voice_name` is the character's xAI voice (companion_voice from the
    Keyhole admin roster; Chloe is "liora").
    """
    if not config.VOICE_NOTES_ENABLED:
        return None
    if not config.XAI_API_KEY:
        _log("CHLOE_XAI_API_KEY is not set; voice note unavailable "
             "(returning None)")
        return None
    clean = (text or "").strip()
    if not clean:
        return None
    if len(clean) > MAX_TTS_CHARS:
        clean = clean[:MAX_TTS_CHARS]
    vid = (voice_name or "liora").strip() or "liora"
    try:
        audio = _synthesize(clean, vid, config.XAI_API_KEY)
    except Exception as e:  # noqa: BLE001 — any TTS failure is a soft None
        _log(f"xAI TTS request failed ({type(e).__name__}: {e}); "
             "returning None")
        return None
    try:
        url = _save_mp3(audio, user_id)
    except OSError as e:
        _log(f"voice note save failed: {e}; returning None")
        return None
    _log(f"generated voice note for user={user_id} voice={vid} "
         f"({len(audio)} bytes)")
    return url
