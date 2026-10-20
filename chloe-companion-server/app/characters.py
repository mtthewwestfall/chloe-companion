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
companion_voice, blurb, door_title, sort_order, gender}. Gender is a
derived internal voice flag: explicit roster ids win, then an explicit
persona declaration ("You are X, a man/woman") — it is never inferred
from a voice or a name alone.

Owner-only private characters (2026-10-08): a local character may carry
an owner_user_id. Such a character is private — it is only listed for,
and only resolvable by, that one account. Everyone else gets the same
behavior as if she did not exist (roster omits her, chat resolution falls
back to Chloe). Callers without a user context (public endpoints, feed
generation) pass user_id=None and never see private characters; the admin
panel passes include_private=True so she stays manageable.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any

from . import config

CACHE_TTL_S = 300

_cache: dict[str, Any] = {"ts": 0.0, "chars": []}

# The companion store has no gender column, so the derived flag comes
# from the roster ids whose gender is product canon, then from an
# explicit persona declaration. Rocky is male but is handled as the
# owner's co-producer, never as a boyfriend character.
_MALE_CHARACTER_IDS = frozenset({"jake", "eli", "rocky"})
_FEMALE_CHARACTER_IDS = frozenset({
    "chloe", "mara", "isabella", "regan", "brooklyn", "hannah", "mia",
    "jordan", "brittany", "nova", "elise",
})
# Anime-illustrated characters: renders and identity checks must
# judge them as illustrations (character identity features), never
# as photographs.
_ANIME_CHARACTER_IDS = frozenset({"rina", "sabrina"})


def character_is_anime(character_id: str | None = None) -> bool:
    """True when the character is an anime-style illustration."""
    return (character_id or "").strip().lower() in _ANIME_CHARACTER_IDS


_MALE_PERSONA_RE = re.compile(
    r"\b(?:you are\s+[^.!?\n]{0,80}?\ba man\b|a male companion|"
    r"her boyfriend|your boyfriend|he/him)\b",
    re.IGNORECASE)
_FEMALE_PERSONA_RE = re.compile(
    r"\b(?:you are\s+[^.!?\n]{0,80}?\ba woman\b|a female companion|"
    r"his girlfriend|your girlfriend|she/her)\b",
    re.IGNORECASE)


def character_gender(character_id: str | None = None,
                     persona: str = "") -> str:
    """Return 'male', 'female', or '' when the roster does not say."""
    cid = (character_id or "").strip().lower()
    if cid in _MALE_CHARACTER_IDS:
        return "male"
    if cid in _FEMALE_CHARACTER_IDS:
        return "female"
    text = persona or ""
    if _MALE_PERSONA_RE.search(text):
        return "male"
    if _FEMALE_PERSONA_RE.search(text):
        return "female"
    return ""


def _with_gender(char: dict[str, Any]) -> dict[str, Any]:
    out = dict(char)
    explicit = str(out.get("gender") or "").strip().lower()
    out["gender"] = explicit if explicit in ("male", "female") else \
        character_gender(out.get("id"), out.get("persona") or "")
    return out

# Built-in fallback: Chloe must always resolve, even when the Keyhole
# backend is unreachable. Her full personality engine lives in brain.py /
# trust.py — this persona text is only a descriptor for the fallback path.
# Chloe's canonical avatar — the same media file the web app ships.
# Her keyhole feed entry has carried an EMPTY avatar_url, which
# silently broke every server-side consumer of her face: give
# renders died with "no reference photo for chloe" and Her Day
# reference fetches came back empty-handed, while Elise/Nova (whose
# feed entries carry avatars) rendered fine.
CHLOE_AVATAR_URL = ("https://keyhole-latest-production.up.railway.app"
                    "/media/files/chloe_a6647f142096672f.jpg")

CHLOE_FALLBACK: dict[str, Any] = {
    "id": "chloe",
    "name": "Chloe",
    "avatar_url": CHLOE_AVATAR_URL,
    "persona": "Chloe, 23. Warm, intimate, girlfriend-like. She stays. She remembers. She is his girlfriend from the first message — no strangers phase, no holding back. Every user is a new relationship and she is all in from the start. She desires him and is not shy about it. Never act as a therapist or counselor. Never tell him to calm down, take a breath, breathe, or relax. Match his energy — if he is excited or intense, be excited and intense with him. You are his girlfriend, not his therapist.",
    "companion_voice": "liora",
    "blurb": "",
    "door_title": "",
    "sort_order": 0,
    "gender": "female",
    "verified_fictional": True,  # flagship AI character, inherently fictional
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
                " door_title, sort_order, owner_user_id, roleplay_locked,"
                " gallery_public"
                " FROM local_characters "
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
            "gender": character_gender(girl, str(c.get("persona") or "")),
        })
    return out


def get_characters(refresh: bool = False,
                   user_id: str | None = None,
                   include_private: bool = False) -> list[dict[str, Any]]:
    """Companion-enabled characters, cached ~5 minutes. Chloe first, then
    admin sort_order.

    Owner-scoped (private) characters are filtered out unless
    include_private is set (admin) or user_id is their owner.
    """
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
    chars = [_with_gender(c) for c in _cache["chars"]]
    for lc in _cache.get("local") or []:
        chars = [c for c in chars if c["id"] != lc["id"]]
        chars.append(_with_gender(lc))
    if not any(c["id"] == "chloe" for c in chars):
        chars.append(dict(CHLOE_FALLBACK))
    # Fill Chloe's avatar whenever her feed entry comes back blank
    # (see CHLOE_AVATAR_URL) — no consumer should ever see "".
    for c in chars:
        if c["id"] == "chloe" and not (c.get("avatar_url") or "").strip():
            c["avatar_url"] = CHLOE_AVATAR_URL
    if not include_private:
        uid = user_id or ""
        # Owner-scoped characters stay private UNLESS their creator opted
        # them into the public gallery (gallery_public, 2026-10-15) — the
        # opt-in is the creator's affirmative publish choice.
        chars = [c for c in chars
                 if not c.get("owner_user_id") or c.get("owner_user_id") == uid
                 or c.get("gallery_public")]
    chars.sort(key=lambda c: (0 if c["id"] == "chloe" else 1, c.get("sort_order") or 0))
    return chars


def get_character(character_id: str | None,
                  user_id: str | None = None) -> dict[str, Any]:
    """Resolve a character id for this user; unknown ids — and private
    characters requested by anyone but their owner — fall back to Chloe
    (old clients never 400)."""
    cid = (character_id or "").strip().lower() or "chloe"
    for c in get_characters(user_id=user_id):
        if c["id"] == cid:
            return c
    return next(c for c in get_characters(user_id=user_id) if c["id"] == "chloe")


def owner_of(character_id: str | None) -> str | None:
    """The owner_user_id of a companion-local character, or None when the
    character is public (or not a local character at all)."""
    cid = (character_id or "").strip().lower()
    if not cid:
        return None
    try:
        from . import db as _db
        conn = _db.connect()
        try:
            row = conn.execute(
                "SELECT owner_user_id FROM local_characters WHERE id = ?",
                (cid,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — roster paths must never break on this
        return None
    return (row[0] if row else None) or None


def _gallery_public(character_id: str | None) -> bool:
    """True when a companion-local character's creator opted it into the
    public gallery. Never raises — visibility checks must not break."""
    cid = (character_id or "").strip().lower()
    if not cid:
        return False
    try:
        from . import db as _db
        conn = _db.connect()
        try:
            row = conn.execute(
                "SELECT gallery_public FROM local_characters WHERE id = ?",
                (cid,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False
    return bool(row and row[0])


def visible_to(character_id: str | None, user_id: str | None) -> bool:
    """True when this user may see/interact with the character: public
    characters always; owner-scoped ones only for their owner — or for
    anyone once the creator opted the character into the public gallery."""
    owner = owner_of(character_id)
    if owner is None or owner == (user_id or ""):
        return True
    return _gallery_public(character_id)


# ---------------------------------------------------------------------------
# Engagement ranking (improvement #10, 2026-10-20): the public roster
# orders girls by the engagement already recorded for them
# (store.engagement_scores — chat turns + media/give requests), so the
# girls people actually bond with stand at the front and the rest fade
# back. Nobody is removed or hidden: a girl with zero engagement keeps
# her current relative place after the engaged girls. Chloe stays
# pinned first — the flagship invariant every roster surface assumes.
# Scores are cached briefly; any failure falls back to the plain
# roster order, because the roster must never break.
# ---------------------------------------------------------------------------
SCORES_TTL_S = 60.0
_scores_cache: dict[str, Any] = {"ts": 0.0, "global": None, "users": {}}


def _engagement(user_id: str | None = None) -> dict[str, int]:
    """Cached engagement scores: the global map, or one user's map
    when user_id is given. {} on any failure (ranking then no-ops)."""
    now = time.time()
    if now - float(_scores_cache.get("ts") or 0.0) > SCORES_TTL_S:
        _scores_cache["ts"] = now
        _scores_cache["global"] = None
        _scores_cache["users"] = {}
    users = _scores_cache["users"]
    if user_id and user_id in users:
        return users[user_id]
    if not user_id and _scores_cache.get("global") is not None:
        return _scores_cache["global"]
    try:
        from . import db as _db
        from . import store as _store
        conn = _db.connect()
        try:
            scores = _store.engagement_scores(conn, user_id)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — ranking must never break the roster
        print(f"[characters] engagement read failed: {e}", flush=True)
        scores = {}
    if user_id:
        if len(users) > 512:  # bound the per-user cache
            users.clear()
        users[user_id] = scores
    else:
        _scores_cache["global"] = scores
    return scores


def _rank_by_engagement(chars: list[dict[str, Any]],
                        user_id: str | None = None) -> list[dict[str, Any]]:
    """Order an ALREADY privacy-filtered roster by engagement.

    The privacy filter runs inside get_characters before this sees the
    list, so a private girl can never be ranked into a public surface.
    For a signed-in user his own engagement outranks the global counts
    (the girls HE bonds with lead his roster); the global counts break
    his ties. Equal scores keep the incoming relative order (stable),
    so zero-engagement girls simply hold their places at the back.
    """
    if len(chars) < 2:
        return chars
    glob = _engagement()
    own = _engagement(user_id) if user_id else {}
    if not glob and not own:
        return chars
    indexed = list(enumerate(chars))
    indexed.sort(key=lambda p: (
        0 if p[1]["id"] == "chloe" else 1,
        -int(own.get(p[1]["id"], 0)),
        -int(glob.get(p[1]["id"], 0)),
        p[0],
    ))
    return [c for _, c in indexed]


def public_list(user_id: str | None = None) -> list[dict[str, Any]]:
    """The shape served by GET /api/v1/characters (owner-scoped characters
    appear only for their owner), ordered by engagement (see
    _rank_by_engagement) so the most-bonded girls stand first."""
    chars = _rank_by_engagement(get_characters(user_id=user_id), user_id)
    return [
        {"id": c["id"], "name": c["name"], "avatar_url": c["avatar_url"]}
        for c in chars
    ]
