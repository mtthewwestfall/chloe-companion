"""Character roster for multi-character companion support.

The roster is admin-managed on the Keyhole backend and fetched from its
public endpoint:

    GET {KEYHOLE_BACKEND_URL}/keyhole/characters
        -> {"characters": [{girl, name, door_title, blurb, avatar_url,
                            persona, companion_voice, sort_order,
                            kh_site, kh_telegram, kh_companion}]}

Only characters with kh_companion=true are exposed here. The list is cached
~5 minutes (CACHE_TTL_S); on fetch failure the last good list is reused, and
Chloe is ALWAYS present via a built-in fallback so the companion never
breaks when the Keyhole backend is down.

Companion-local girls (2026-09-30): the admin panel's Girls tab stores extra
characters in the companion DB (local_characters). They are merged into the
roster on every refresh — a local girl with the same id as a Keyhole girl
overrides it. The 'chloe' id is reserved and can never be added locally.

Character dicts (internal): {id, name, avatar_url, persona,
companion_voice, blurb, door_title, sort_order}.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

from . import config

CACHE_TTL_S = 300

_cache: dict[str, Any] = {"ts": 0.0, "chars": []}

# Built-in fallback: Chloe must always resolve, even when the Keyhole
# backend is unreachable. Her full personality engine lives in brain.py /
# trust.py — this persona text is only a descriptor for the fallback path.
CHLOE_FALLBACK: dict[str, Any] = {
    "id": "chloe",
    "name": "Chloe",
    "avatar_url": "",
    "persona": "Warm, intimate, girlfriend-like. She stays. She remembers.",
    "companion_voice": "liora",
    "blurb": "",
    "door_title": "",
    "sort_order": 0,
}


def _local_characters() -> list[dict[str, Any]]:
    """Companion-local girls from the admin panel (enabled only). Never
    raises — the roster must survive a missing table or DB hiccup."""
    try:
        from . import db as _db
        conn = _db.connect()
        try:
            rows = conn.execute(
                "SELECT id, name, avatar_url, persona, companion_voice, blurb,"
                " door_title, sort_order FROM local_characters "
                "WHERE enabled = 1 ORDER BY sort_order, name"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — roster must never break on this
        print(f"[characters] local read failed: {e}", flush=True)
        return []


def bust_cache() -> None:
    """Force the next get_characters() call to refresh (after admin edits)."""
    _cache["ts"] = 0.0


def _fetch() -> list[dict[str, Any]]:
    base = (config.KEYHOLE_BACKEND_URL or "").rstrip("/")
    if not base:
        raise RuntimeError("KEYHOLE_BACKEND_URL not configured")
    req = urllib.request.Request(
        base + "/keyhole/characters",
        headers={"User-Agent": "chloe-companion/characters"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    out: list[dict[str, Any]] = []
    for c in data.get("characters") or []:
        if not isinstance(c, dict) or not c.get("kh_companion"):
            continue
        girl = str(c.get("girl") or "").strip().lower()
        out.append({
            "id": girl or "chloe",
            "name": str(c.get("name") or "Chloe"),
            "avatar_url": str(c.get("avatar_url") or ""),
            "persona": str(c.get("persona") or ""),
            "companion_voice": str(c.get("companion_voice") or "liora") or "liora",
            "blurb": str(c.get("blurb") or ""),
            "door_title": str(c.get("door_title") or ""),
            "sort_order": c.get("sort_order") or 0,
        })
    return out


def get_characters(refresh: bool = False) -> list[dict[str, Any]]:
    """Companion-enabled characters, cached ~5 minutes. Chloe first, then
    admin sort_order."""
    now = time.time()
    if refresh or now - _cache["ts"] > CACHE_TTL_S:
        try:
            chars = _fetch()
        except Exception as e:  # noqa: BLE001 — never break chat on a roster fetch
            print(f"[characters] fetch failed, keeping last list: {e}", flush=True)
            chars = _cache["chars"] or []
        else:
            _cache["chars"] = chars
        _cache["local"] = _local_characters()
        _cache["ts"] = now
    chars = [dict(c) for c in _cache["chars"]]
    for lc in _cache.get("local") or []:
        chars = [c for c in chars if c["id"] != lc["id"]]
        chars.append(dict(lc))
    if not any(c["id"] == "chloe" for c in chars):
        chars.append(dict(CHLOE_FALLBACK))
    chars.sort(key=lambda c: (0 if c["id"] == "chloe" else 1, c.get("sort_order") or 0))
    return chars


def get_character(character_id: str | None) -> dict[str, Any]:
    """Resolve a character id; unknown ids fall back to Chloe (old clients
    never 400)."""
    cid = (character_id or "").strip().lower() or "chloe"
    for c in get_characters():
        if c["id"] == cid:
            return c
    return next(c for c in get_characters() if c["id"] == "chloe")


def public_list() -> list[dict[str, Any]]:
    """The shape served by GET /api/v1/characters."""
    return [
        {"id": c["id"], "name": c["name"], "avatar_url": c["avatar_url"]}
        for c in get_characters()
    ]
