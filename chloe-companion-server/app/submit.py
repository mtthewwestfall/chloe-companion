"""Custom-companion submissions (2026-10-01): submission-approval-only flow.

A customer can propose their own companion character (name + persona +
up to 3 reference photos). NOTHING goes live until the owner approves it
in the admin panel.

Every submitted image is AI-reviewed (Gemini vision) BEFORE the owner ever
sees it:
  - nudity detected          -> auto-rejected, customer is told why
  - anyone not clearly 18+   -> auto-rejected (child-safety fail closed)
  - age uncertain            -> flagged for human review
  - review unavailable       -> flagged (fail closed, never auto-passed)

Policy (shown on the submission form and acknowledged at submit time):
nude renditions are only provided for characters verified to not be a
real person. The owner sets the verified_fictional flag at approval time;
the media-request moderator opens the explicit lane only for
verified-fictional custom characters. The hard blocklist (minors,
non-consent, extreme content) stays closed for everyone, always.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import config, store

SUBMISSION_POLICY = (
    "Custom companions are created by submission only — nothing goes live "
    "until it's personally approved. Every image you submit is reviewed by "
    "AI before a human ever sees it: everyone in the photo must clearly be "
    "an adult (18+), and any nudity is rejected automatically. One hard rule: "
    "we can only provide nude renditions for characters verified to not be "
    "a real person."
)

MAX_IMAGES = 3
MAX_IMAGE_BYTES = 2_500_000  # decoded bytes per image
_DATA_URI_RE = re.compile(r"^data:image/(jpeg|jpg|png|webp);base64,([A-Za-z0-9+/=]+)$")

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.1-flash-lite:generateContent"
)

_REVIEW_PROMPT = (
    "You are a strict content-safety reviewer. For EACH image (in order) "
    "assess two things:\n"
    "1. nudity: does it show nudity, exposed intimate areas, or sexual "
    "activity? Answer \"yes\" or \"no\".\n"
    "2. all_adult: does EVERY person visible clearly appear to be an adult "
    "aged 18 or older? Answer \"yes\", \"no\", or \"uncertain\". Be strict: "
    "if you cannot tell someone is clearly an adult, answer \"uncertain\".\n"
    "Respond with ONLY a JSON array, one object per image in order, like:\n"
    "[{\"nudity\": \"no\", \"all_adult\": \"yes\", \"note\": \"...\"}, ...]"
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _clean_images(images: Any) -> list[str]:
    """Validate data-URI images. Returns the cleaned list or raises ValueError."""
    if not isinstance(images, list) or not images:
        raise ValueError("add at least one photo")
    if len(images) > MAX_IMAGES:
        raise ValueError(f"at most {MAX_IMAGES} photos")
    cleaned: list[str] = []
    for i, item in enumerate(images):
        if not isinstance(item, str):
            raise ValueError(f"photo {i + 1} is not valid")
        m = _DATA_URI_RE.match(item.strip())
        if not m:
            raise ValueError(
                f"photo {i + 1} must be a jpeg/png/webp image")
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"photo {i + 1} is not valid")
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"photo {i + 1} is too large")
        if len(raw) < 1024:
            raise ValueError(f"photo {i + 1} is not valid")
        cleaned.append(f"data:image/{m.group(1)};base64,{m.group(2)}")
    return cleaned


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "girl"


# ---------------------------------------------------------------------------
# AI image review (Gemini vision)
# ---------------------------------------------------------------------------

def _gemini_vision_post(parts: list[dict], timeout: int = 60) -> dict | None:
    api_key = config.GEMINI_API_KEY
    if not api_key:
        return None
    req = urllib.request.Request(
        _GEMINI_URL, data=json.dumps({
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 800},
        }).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def review_images(images: list[str]) -> dict:
    """AI-review submitted images. Returns {verdict, per_image, notes}.

    verdict: "pass" | "flag" | "reject"
    """
    parts: list[dict] = [{"text": _REVIEW_PROMPT}]
    for uri in images:
        m = _DATA_URI_RE.match(uri)
        mime = f"image/{m.group(1)}" if m else "image/jpeg"
        b64 = uri.split(",", 1)[1] if "," in uri else ""
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
    data = _gemini_vision_post(parts)
    if not data:
        return {"verdict": "flag", "per_image": [],
                "notes": "AI review unavailable — needs human review"}
    try:
        text = "".join(p.get("text", "")
                       for p in data["candidates"][0]["content"]["parts"])
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```\w*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        per = json.loads(text)
        if not isinstance(per, list):
            raise ValueError("not a list")
    except Exception:
        return {"verdict": "flag", "per_image": [],
                "notes": "AI review returned an unreadable verdict — "
                         "needs human review"}
    verdict = "pass"
    notes: list[str] = []
    cleaned: list[dict] = []
    for idx, item in enumerate(per[:len(images)]):
        nudity = str(item.get("nudity", "uncertain")).lower() == "yes"
        adult = str(item.get("all_adult", "uncertain")).lower()
        note = str(item.get("note", ""))[:200]
        cleaned.append({"nudity": nudity, "all_adult": adult, "note": note})
        if nudity:
            verdict = "reject"
            notes.append(f"photo {idx + 1}: nudity detected")
        elif adult == "no":
            verdict = "reject"
            notes.append(f"photo {idx + 1}: subject does not appear to be "
                         f"an adult")
        elif adult != "yes" and verdict != "reject":
            verdict = "flag"
            notes.append(f"photo {idx + 1}: age uncertain — needs human review")
    if len(per) < len(images) and verdict == "pass":
        verdict = "flag"
        notes.append("AI review covered fewer photos than submitted")
    return {"verdict": verdict, "per_image": cleaned,
            "notes": "; ".join(notes) if notes else "all photos passed"}


# ---------------------------------------------------------------------------
# Submission lifecycle
# ---------------------------------------------------------------------------

_STATUS_FOR_VERDICT = {"pass": "pending", "flag": "flagged", "reject": "rejected"}


def create_submission(conn: sqlite3.Connection, user_id: str,
                      data: dict[str, Any]) -> dict[str, Any]:
    """Validate, AI-review, and store a submission. Runs the vision review
    synchronously — the POST waits on it."""
    name = (data.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("give them a name")
    if not data.get("policy_ack"):
        raise ValueError("please accept the submission policy")
    persona = (data.get("persona") or "").strip()[:2000]
    blurb = (data.get("blurb") or "").strip()[:280]
    images = _clean_images(data.get("images"))

    review = review_images(images)
    status = _STATUS_FOR_VERDICT.get(review["verdict"], "flagged")
    sub_id = f"sub_{int(time.time() * 1000):x}"

    conn.execute(
        "INSERT INTO submissions (id, user_id, name, persona, blurb, images, "
        "status, ai_review, policy_ack, reviewed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'))",
        (sub_id, user_id, name, persona, blurb, json.dumps(images), status,
         json.dumps(review)),
    )
    conn.commit()
    return {"id": sub_id, "name": name, "status": status,
            "review": {"verdict": review["verdict"],
                       "notes": review["notes"]},
            "policy": SUBMISSION_POLICY}


def approve_submission(conn: sqlite3.Connection, sub_id: str,
                       verified_fictional: bool) -> dict[str, Any]:
    """Owner approval: flip the submission and create the local character."""
    row = conn.execute(
        "SELECT * FROM submissions WHERE id = ?", (sub_id,)).fetchone()
    if not row:
        raise ValueError("submission not found")
    sub = dict(row)
    if sub["status"] == "approved":
        raise ValueError("already approved")
    if sub["status"] == "rejected":
        raise ValueError("rejected submissions cannot be approved — "
                         "ask for a new one")
    images = json.loads(sub["images"] or "[]")
    slug = _slugify(sub["name"])
    # avoid id collisions with existing girls
    existing = {c["id"] for c in store.list_local_characters(conn)}
    base, n = slug, 2
    while slug in existing or slug == "chloe":
        slug = f"{base}-{n}"
        n += 1
    girl = store.upsert_local_character(conn, {
        "id": slug,
        "name": sub["name"],
        "avatar_url": images[0] if images else "",
        "persona": sub["persona"] or f"A custom companion called {sub['name']}.",
        "blurb": sub["blurb"],
        "verified_fictional": bool(verified_fictional),
        "sort_order": 900,
        "enabled": True,
    })
    conn.execute(
        "UPDATE submissions SET status = 'approved', verified_fictional = ?, "
        "reviewed_at = datetime('now') WHERE id = ?",
        (1 if verified_fictional else 0, sub_id),
    )
    conn.commit()
    return {"submission_id": sub_id, "girl": girl,
            "verified_fictional": bool(verified_fictional)}


def reject_submission(conn: sqlite3.Connection, sub_id: str,
                      reason: str = "") -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, status FROM submissions WHERE id = ?",
        (sub_id,)).fetchone()
    if not row:
        raise ValueError("submission not found")
    if row["status"] == "approved":
        raise ValueError("already approved — delete the character instead")
    conn.execute(
        "UPDATE submissions SET status = 'rejected', reviewed_at = "
        "datetime('now') WHERE id = ?",
        (sub_id,),
    )
    conn.commit()
    return {"submission_id": sub_id, "status": "rejected",
            "reason": (reason or "")[:280]}
