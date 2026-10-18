"""Voice notes via Gemini TTS (through the shared Gemini key manager).

Engine history: voice notes launched on a stub, moved to ElevenLabs
(PR #24, per-character stock voices in PR #26), and on 2026-10-17 the
owner ordered the engine switched to Gemini ("Forget ElevenLabs, just
use Gemini"). Synthesis now runs through app/gemini_keys.post_json —
the same key-manager path as chat (free keys first, paid fallback,
transparent failover) — so voice routing is identical to the rest of
the app. The ElevenLabs code/env below is retained for reference and
rollback only; it is never called for synthesis anymore.

Gate: requires CHLOE_VOICE_ENABLED=1 (see app/config.py:
config.VOICE_NOTES_ENABLED). When the gate is closed the function
returns None gracefully — it never raises and never makes a network
call.

On success the audio is saved under config.GENERATED_DIR (served by
the app at /generated/) and the /generated/<file> URL is returned, so
the URL passes her_day.validate_her_day_media_url and plays in the
app (the client plays voice_note_url / audio_url with a plain
HTMLAudio element, which is format-agnostic).

API (Gemini TTS, native generateContent):
    POST https://generativelanguage.googleapis.com/v1beta/models/
         {model}:generateContent        (via gemini_keys.post_json)
    {"contents": [{"parts": [{"text": ...}]}],
     "generationConfig": {"responseModalities": ["AUDIO"],
                          "speechConfig": {"voiceConfig":
                              {"prebuiltVoiceConfig":
                                  {"voiceName": <name>}}}}}
    -> candidates[0].content.parts[].inlineData.data = base64 raw PCM
       (mimeType "audio/L16;codec=pcm;rate=24000")

Gemini TTS returns headerless PCM, so the bytes are wrapped in a WAV
container before saving (the same Gemini-TTS->WAV treatment the Rover
call stack uses) — a .wav the app's audio element plays directly.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
import wave

from . import config, gemini_keys

# ---------------------------------------------------------------------------
# Gemini TTS (the live synthesis engine, 2026-10-17)
# ---------------------------------------------------------------------------
# Model override with GEMINI_TTS_MODEL (e.g. the pro TTS tier).
GEMINI_TTS_MODEL = os.environ.get(
    "GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
_GEMINI_TTS_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_TTS_MODEL}:generateContent")
# Usage-log source for the key manager, matching the whispr-* family
# (whispr-chat, whispr-duo, whispr-search).
_TTS_SOURCE = "whispr-voice"
# Gemini TTS speaks raw 16-bit mono PCM; the rate rides in the
# response mimeType ("audio/L16;codec=pcm;rate=24000").
_TTS_DEFAULT_SAMPLE_RATE = 24000

# Default Gemini voice for a character with no specific assignment:
# Kore — a clear, firm female voice, Gemini's canonical default.
DEFAULT_GEMINI_VOICE = "Kore"

# Character voice name -> Gemini prebuilt voice name. A character's
# companion_voice ("liora", "rex", ...) selects the entry when no
# per-character assignment applies.
_BUILTIN_GEMINI_VOICES = {
    "liora": "Sulafat",  # Chloe's voice name -> her Gemini voice below
    "rex": "Charon",     # male default (was ElevenLabs Adam)
}

# Per-character Gemini voices (2026-10-17, replacing the ElevenLabs
# stock map below): each character keeps a DISTINCT voice matched to
# the persona of the ElevenLabs stock voice she had, so a voice note
# still sounds like the character who sent it. Env overrides win:
# GEMINI_TTS_VOICE_<VOICE_NAME> first, then
# GEMINI_TTS_VOICE_<CHARACTER_ID> (e.g. GEMINI_TTS_VOICE_MARA), then
# this map, then GEMINI_TTS_VOICE, then the built-in name map, then
# DEFAULT_GEMINI_VOICE.
_CHARACTER_GEMINI_VOICES = {
    "chloe": "Sulafat",       # warm (was Rachel)
    "mara": "Leda",           # youthful (was Bella)
    "isabella": "Despina",    # smooth (was Charlotte)
    "regan": "Erinome",       # clear (was Alice)
    "brittany": "Vindemiatrix",  # gentle (was Lily)
    "brooklyn": "Pulcherrima",   # forward (was Grace)
    "mia": "Zephyr",          # bright (was Elli)
    "hannah": "Autonoe",      # bright (was Dorothy)
    "jordan": "Aoede",        # breezy (was Aria)
    "bailey": "Laomedeia",    # upbeat (was Bella; distinct from Mara)
    "rina": "Callirrhoe",     # easy-going (was Gigi)
    "sabrina": "Kore",        # firm (was Matilda)
    "nova": "Gacrux",         # mature (new 2026-10-18)
    "elise": "Achernar",      # soft (new 2026-10-18)
    "jake": "Alnilam",        # firm (was Josh)
    "eli": "Schedar",         # even (was Arnold)
    "rocky": "Charon",        # informative (was Adam/rex)
}


def _gemini_voice_for(voice_name: str, character_id: str = "") -> str:
    """Resolve a character's voice to a Gemini prebuilt voice name.

    Precedence mirrors the retired ElevenLabs resolver: per-name env,
    per-character env, the per-character map, the global env default,
    the built-in name map, then DEFAULT_GEMINI_VOICE.
    """
    name = (voice_name or "").strip().lower() or LIORA_VOICE
    cid = (character_id or "").strip().lower()
    per_name = os.environ.get(
        f"GEMINI_TTS_VOICE_{name.upper()}", "").strip()
    if per_name:
        return per_name
    if cid:
        per_char = os.environ.get(
            f"GEMINI_TTS_VOICE_{cid.upper()}", "").strip()
        if per_char:
            return per_char
        if cid in _CHARACTER_GEMINI_VOICES:
            return _CHARACTER_GEMINI_VOICES[cid]
    default = os.environ.get("GEMINI_TTS_VOICE", "").strip()
    if default:
        return default
    return _BUILTIN_GEMINI_VOICES.get(name, DEFAULT_GEMINI_VOICE)


def _tts_payload(text: str, gemini_voice: str) -> dict:
    """Build the Gemini TTS generateContent payload."""
    return {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": gemini_voice}}},
        },
        # Same posture as the chat brain: she's a willing adult
        # girlfriend; default filters must not deflect her lines.
        "safetySettings": [
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
             "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HARASSMENT",
             "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",
             "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT",
             "threshold": "BLOCK_ONLY_HIGH"},
        ],
    }


def _extract_pcm(data: dict) -> tuple[bytes, int]:
    """Pull raw PCM + sample rate out of a Gemini TTS response.

    Raises RuntimeError on any shape that carries no audio (blocked
    prompt, empty candidates, malformed inline data).
    """
    candidates = (data or {}).get("candidates") or []
    pcm = bytearray()
    rate = _TTS_DEFAULT_SAMPLE_RATE
    for cand in candidates:
        parts = ((cand or {}).get("content") or {}).get("parts") or []
        for part in parts:
            inline = (part or {}).get("inlineData") or {}
            b64 = inline.get("data")
            if not b64:
                continue
            try:
                pcm.extend(base64.b64decode(b64))
            except (binascii.Error, ValueError, TypeError) as e:
                raise RuntimeError(
                    f"TTS audio field is not valid base64: {e}")
            m = re.search(r"rate=(\d+)", inline.get("mimeType") or "")
            if m:
                rate = int(m.group(1))
    if not pcm:
        raise RuntimeError(
            f"TTS returned no audio parts: {str(data)[:200]!r}")
    return bytes(pcm), rate


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate or _TTS_DEFAULT_SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def _synthesize(text: str, gemini_voice: str) -> bytes:
    """Synthesize via Gemini TTS through the key manager.

    Returns WAV bytes; raises RuntimeError when the pool cannot serve
    or the response carries no audio.
    """
    data = gemini_keys.post_json(
        _GEMINI_TTS_URL, _tts_payload(text, gemini_voice),
        timeout=_TTS_TIMEOUT_S, source=_TTS_SOURCE)
    if not data:
        raise RuntimeError("Gemini TTS: key pool returned no result")
    pcm, rate = _extract_pcm(data)
    return _pcm_to_wav(pcm, rate)


def _save_wav(audio: bytes, user_id: str) -> str:
    """Store the WAV under GENERATED_DIR; return its /generated/ URL."""
    os.makedirs(config.GENERATED_DIR, exist_ok=True)
    safe = ("".join(c if c.isalnum() else "_" for c in (user_id or "anon"))
            [:16] or "anon")
    fname = f"voice_{safe}_{int(time.time())}_{uuid.uuid4().hex[:8]}.wav"
    dest = os.path.join(config.GENERATED_DIR, fname)
    with open(dest, "wb") as fh:
        fh.write(audio)
    return "/generated/" + fname


# ---------------------------------------------------------------------------
# ElevenLabs (RETIRED engine — retained for reference/rollback only.
# Nothing below this line is called for synthesis anymore.)
# ---------------------------------------------------------------------------
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

# Per-character stock voices (2026-10-17): until now every girl resolved
# to liora/Rachel, so all voice notes sounded like Chloe. Each roster
# character gets her OWN ElevenLabs stock voice so a voice note sounds
# like the character who sent it. Env overrides still win:
# ELEVENLABS_VOICE_ID_<VOICE_NAME> first, then
# ELEVENLABS_VOICE_ID_<CHARACTER_ID> (e.g. ELEVENLABS_VOICE_ID_MARA).
_CHARACTER_VOICE_IDS = {
    "chloe": "21m00Tcm4TlvDq8ikWAM",     # Rachel - Chloe's established voice
    "mara": "EXAVITQu4vr4xnSDxMaL",      # Bella
    "isabella": "XB0fDUnXU5powFXDhCwa",  # Charlotte
    "regan": "Xb7hH8MSUJpSbSDYk0k2",     # Alice
    "brittany": "pFZP5JQG7iQjIQuC4Bku",  # Lily
    "brooklyn": "GBv7mTt0atIp3Br8iCZE",  # Grace
    "mia": "MF3mGyEYCl7XYWbV9V6O",       # Elli
    "hannah": "ThT5KcBeYPX3keUQqHPh",    # Dorothy
    "jordan": "9BWtsMINqrJLrRacOk9x",    # Aria
    "bailey": "EXAVITQu4vr4xnSDxMaL",    # Bella (minor roster member)
    "rina": "jBpfuIE2acCO8z3wKNLl",      # Gigi
    "sabrina": "XrExE9yKIg1WjnnlVkGX",   # Matilda
    "jake": "TxGEqnHWrfWFTfGW9XjX",      # Josh
    "eli": "VR6AewLTigWG4xSOukaG",       # Arnold
    "rocky": "pNInz6obpgDQGcFmaJgB",     # Adam (rex)
}

EST_COST_PER_NOTE_USD = 0.05

# Billing/latency guard: chat only asks for <= 280 chars, but other
# callers can pass longer text — clamp so one note can't balloon the bill.
MAX_TTS_CHARS = 1000
_TTS_TIMEOUT_S = 60


def _log(msg: str) -> None:
    print(f"[voice] {msg}", flush=True)


def _voice_id_for(voice_name: str, character_id: str = "") -> str:
    """Resolve a character's voice to an ElevenLabs voice ID.

    LEGACY (retired engine): kept so existing imports/env tooling keep
    working; synthesis resolves Gemini voices via _gemini_voice_for.

    Precedence: ELEVENLABS_VOICE_ID_<NAME> (e.g.
    ELEVENLABS_VOICE_ID_LIORA), then ELEVENLABS_VOICE_ID_<CHARACTER_ID>,
    then the per-character stock map, then ELEVENLABS_VOICE_ID, then the
    built-in name map. The character map sits above the global default so
    a single ELEVENLABS_VOICE_ID can no longer flatten every girl back
    to one voice.
    """
    name = (voice_name or "").strip().lower() or LIORA_VOICE
    cid = (character_id or "").strip().lower()
    per_name = os.environ.get(
        f"ELEVENLABS_VOICE_ID_{name.upper()}", "").strip()
    if per_name:
        return per_name
    if cid:
        per_char = os.environ.get(
            f"ELEVENLABS_VOICE_ID_{cid.upper()}", "").strip()
        if per_char:
            return per_char
        if cid in _CHARACTER_VOICE_IDS:
            return _CHARACTER_VOICE_IDS[cid]
    default = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    if default:
        return default
    return _BUILTIN_VOICE_IDS.get(name, _BUILTIN_VOICE_IDS[LIORA_VOICE])


def _extract_audio(body: bytes) -> bytes:
    """Decode an ElevenLabs TTS response into raw audio bytes.

    LEGACY (retired engine). Success returns the raw audio bytes
    (audio/mpeg). A JSON body on a 200 is unexpected (error details
    normally ride on a non-200 status) but handled defensively.
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


def _synthesize_elevenlabs(text: str, voice_id: str, api_key: str) -> bytes:
    """POST the text to ElevenLabs TTS. Returns raw MP3 bytes; raises.

    LEGACY (retired engine): retained for rollback; not called.
    """
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
    """Store the MP3 under GENERATED_DIR; return its /generated/ URL.

    LEGACY (retired engine): Gemini notes save as WAV via _save_wav.
    """
    os.makedirs(config.GENERATED_DIR, exist_ok=True)
    safe = ("".join(c if c.isalnum() else "_" for c in (user_id or "anon"))
            [:16] or "anon")
    fname = f"voice_{safe}_{int(time.time())}_{uuid.uuid4().hex[:8]}.mp3"
    dest = os.path.join(config.GENERATED_DIR, fname)
    with open(dest, "wb") as fh:
        fh.write(audio)
    return "/generated/" + fname


def maybe_generate_voice_note(text: str, user_id: str, voice_name: str = "liora",
                              quota: bool = True, consume=None,
                              character_id: str = "") -> str | None:
    """Generate a voice note with Gemini TTS, returning its URL.

    Returns None (never raises) when voice is unavailable: quota=False,
    the feature flag is off, the key pool cannot serve, the synthesis
    fails, or the text is empty.
    `consume` runs only after a note is generated and saved.

    `voice_name` is the character's voice (companion_voice from the
    roster; Chloe is "liora") and `character_id` her roster id — both
    feed _gemini_voice_for, which picks her Gemini voice.
    """
    if not quota or not config.VOICE_NOTES_ENABLED:
        return None
    clean = (text or "").strip()
    if not clean:
        return None
    if len(clean) > MAX_TTS_CHARS:
        clean = clean[:MAX_TTS_CHARS]
    gvoice = _gemini_voice_for(voice_name, character_id)
    try:
        audio = _synthesize(clean, gvoice)
    except Exception as e:  # noqa: BLE001 — any TTS failure is a soft None
        _log(f"Gemini TTS request failed ({type(e).__name__}: {e}); "
             "returning None")
        return None
    try:
        url = _save_wav(audio, user_id)
    except OSError as e:
        _log(f"voice note save failed: {e}; returning None")
        return None
    _log(f"generated voice note for user={user_id} voice={voice_name} "
         f"gemini_voice={gvoice} ({len(audio)} bytes)")
    return (consume(), url)[1] if consume is not None else url
