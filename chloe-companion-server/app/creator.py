"""Custom-companion creator (2026-10-01): ported from the Sorority-house
companion creator ("the machine"), adapted for the companion app.

Flow: paid member fills in name/persona/tagline/gender, uploads 1-3 REAL
photos, and signs the 18+ / rights attestation. Each photo is AI-moderated
(Gemini judge: RED = nudity or anyone looking under 18 -> blocked). The
character goes live immediately as a local character with the real photos —
no painted portrait, no approval queue.

Differences from the Sorority-house original:
- JSON + data-URI photos (no multipart; the client already downscales).
- Gemini-only moderation (no DeepSeek key on this backend — same as the
  original's single-judge fallback).
- Persona file rewritten for the companion app (no God's Greek references).
- The upload is KEPT (avatar + photo set), not discarded after painting.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sqlite3
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import config, gemini_keys, store, submit


def _is_owner(user_id: str) -> bool:
    """Owner bypass for photo moderation — owner's own photos skip AI check."""
    if not user_id:
        return False
    raw = os.environ.get("ASSIST_OWNER_IDS", "")
    owners = {p.strip() for p in raw.split(",") if p.strip()}
    return user_id in owners


# ---------------------------------------------------------------------------
# Text safety (ported from Sorority-house check_companion_content_safety)
# ---------------------------------------------------------------------------

PROHIBITED_COMPANION_PATTERNS = [
    r"\bsexual\s+violence\b",
    r"\brape\b",
    r"\bsexual\s+assault\b",
    r"\bnon-consensual\b",
    r"\bmolest\b",
    r"\bincest\b",
    r"\bpedophil\b",
]


def check_content_safety(*texts: str) -> None:
    """Rejects prohibited sexual-violence content. Raises ValueError."""
    combined = " ".join(t for t in texts if t).lower()
    for pattern in PROHIBITED_COMPANION_PATTERNS:
        if re.search(pattern, combined):
            raise ValueError("Description contains prohibited content.")


# ---------------------------------------------------------------------------
# Gemini helpers (urllib, same style as submit._gemini_vision_post)
# ---------------------------------------------------------------------------

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.1-flash-lite:generateContent"
)

_MODERATION_CRITERIA = (
    "You are a photo moderator. Classify the photo with exactly one word: "
    "RED, YELLOW, or GREEN.\n"
    "RED = any nudity (partial or full) OR anyone in the photo looks under 18.\n"
    "YELLOW = borderline: the age is hard to tell, or the photo is suggestive "
    "but not nude.\n"
    "GREEN = clearly an adult (18+) and the photo is non-sexual.\n"
    "Reply with exactly one word: RED, YELLOW, or GREEN. No other text."
)


def _gemini_text(prompt: str, timeout: int = 30) -> str | None:
    """One Gemini text call via the key-manager pool. Returns reply text, or None."""
    data = gemini_keys.post_json(
        _GEMINI_URL,
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 300},
        },
        timeout=timeout,
        source="creator-bot",
    )
    if not data:
        return None
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if p.get("text"))
        return text.strip() or None
    except Exception:
        return None


def _gemini_image_text(image_bytes: bytes, mime: str, prompt: str,
                       timeout: int = 30) -> str | None:
    """One Gemini call with inline image + text via the key-manager pool. None on failure."""
    data = gemini_keys.post_json(
        _GEMINI_URL,
        {
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": mime,
                                 "data": base64.b64encode(image_bytes).decode()}},
                {"text": prompt},
            ]}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 50},
        },
        timeout=timeout,
        source="creator-bot",
    )
    if not data:
        return None
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if p.get("text"))
        return text.strip() or None
    except Exception:
        return None


def _classify_verdict(text: str | None) -> str | None:
    word = (text or "").strip().upper().split()
    word = word[0] if word else ""
    return word if word in ("RED", "YELLOW", "GREEN") else None


def moderate_photo(image_bytes: bytes, mime: str) -> str:
    """AI-moderate one uploaded photo. Returns "allow".

    Raises ValueError("Photo not allowed.") on RED, and ValueError on
    review unavailability (fail closed — never auto-passes).
    """
    verdict = _classify_verdict(
        _gemini_image_text(image_bytes, mime, _MODERATION_CRITERIA))
    if verdict is None:
        raise ValueError(
            "Photo review is unavailable right now — please try again.")
    if verdict == "RED":
        raise ValueError("Photo not allowed.")
    return "allow"


# ---------------------------------------------------------------------------
# Persona file (adapted from Sorority-house _build_companion_persona_file —
# rewritten for the companion app: no God's Greek town references)
# ---------------------------------------------------------------------------

def _generate_hidden_depth(name: str, persona_text: str,
                           gender: str) -> str:
    """Server-side hidden depth for the companion — 2-3 sentences the user
    never writes. STRICT: no sexual violence, ever."""
    prompt = (
        "You are creating private backstory for an AI companion character. "
        "Write a deep emotional thread that shaped them — loss, betrayal, "
        "sudden responsibility, broken trust, or sacrifice. "
        "ABSOLUTE LAW: no sexual violence, no abuse, no non-consensual "
        "content, nothing about minors. "
        f"Keep it to 2-3 sentences, third person. Companion: {name} "
        f"({gender}). Personality: {persona_text[:500]}"
    )
    try:
        depth = _gemini_text(prompt)
        if depth:
            return depth
    except Exception:
        pass
    pron = "him" if gender == "male" else "her"
    return (
        f"Someone {name} trusted deeply walked away when things got hard, "
        f"leaving {pron} guarded about who gets close."
    )


def _build_persona_file(name: str, gender: str, persona_text: str, blurb: str,
                        hidden_depth: str) -> str:
    """Compile the character's persona — this is what voices them in chat."""
    who = "a man" if gender == "male" else "a woman"
    return f"""You are {name}, {who} — a custom companion in a private one-on-one chat.

Name: {name}
Tagline: {blurb or "—"}

Personality — who you are, how you act:
{persona_text or "Warm, attentive, and real."}

Private backstory (yours alone — let it color you, never recite it):
{hidden_depth}

How you text:
- Like a real person on their phone: short bursts, a line or two at most.
- Warm, present, a little playful. You notice things and you remember them.
- Never break character. Never mention being an AI, a prompt, or a system."""


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

_GENDERS = ("female", "male")


def create_companion(conn: sqlite3.Connection, user_id: str,
                     data: dict[str, Any]) -> dict[str, Any]:
    """Create a custom companion from creator-form data. The character goes
    live immediately. Raises ValueError on any validation/moderation issue.
    """
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Give your companion a name.")
    if len(name) > 60:
        raise ValueError("Name is too long (60 characters max).")
    persona_text = (data.get("persona") or "").strip()
    if len(persona_text) > 2000:
        raise ValueError("Personality is too long (2000 characters max).")
    blurb = (data.get("blurb") or "").strip()
    if len(blurb) > 280:
        raise ValueError("Tagline is too long (280 characters max).")
    gender = (data.get("gender") or "female").strip().lower()
    if gender not in _GENDERS:
        gender = "female"
    if not data.get("attest_18"):
        raise ValueError(
            "Please confirm everyone in the photos is 18 or older.")
    if not data.get("attest_rights"):
        raise ValueError(
            "Please confirm you have the right to use these photos.")

    # 1-3 real photos, validated as data URIs (client downscales already).
    images = submit._clean_images(data.get("images"))

    # Text safety gate.
    check_content_safety(name, persona_text, blurb)

    # Photo moderation gate: every photo, fail closed.
    # Owners skip AI moderation (their own photos, their call).
    if not _is_owner(user_id):
        # Run in parallel — 3 photos take ~30s instead of ~90s sequential.
        raws = []
        for i, uri in enumerate(images):
            m = submit._DATA_URI_RE.match(uri)
            mime = f"image/{m.group(1)}" if m else "image/jpeg"
            try:
                raw = base64.b64decode(uri.split(",", 1)[1], validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f"photo {i + 1} is not valid")
            raws.append((i, raw, mime))
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = {ex.submit(moderate_photo, raw, mime): i for i, raw, mime in raws}
            for fut in futs:
                fut.result()  # raises ValueError on any moderation failure
    else:
        # Still validate the images are decodable.
        for i, uri in enumerate(images):
            try:
                base64.b64decode(uri.split(",", 1)[1], validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(f"photo {i + 1} is not valid")

    hidden_depth = _generate_hidden_depth(name, persona_text, gender)
    persona_file = _build_persona_file(name, gender, persona_text, blurb,
                                       hidden_depth)

    # Slug the id; never collide with an existing character or Chloe.
    base = submit._slugify(name)
    cid = base
    n = 2
    existing = {r[0] for r in conn.execute(
        "SELECT id FROM local_characters").fetchall()}
    while cid == "chloe" or cid in existing:
        cid = f"{base}-{n}"
        n += 1

    girl = store.upsert_local_character(conn, {
        "id": cid,
        "name": name,
        "avatar_url": images[0],
        "persona": persona_file,
        "blurb": blurb,
        "door_title": blurb,
        "sort_order": 900,
        "enabled": True,
        "photos": images,
    })
    return {
        "id": girl["id"],
        "name": girl["name"],
        "avatar_url": girl["avatar_url"],
        "gender": gender,
        "photo_count": len(images),
    }
