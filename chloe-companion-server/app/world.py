"""Living World engine (Phase A1 of the industry-shaping program,
2026-10-18).

Every character lives in ONE persistent, shared world. Her day is a
deterministic plan (seeded by girl + date, so every surface agrees):
five blocks (early morning -> night), an activity + place per block,
one mood for the day, and — for the main cast — shared EVENTS with
other girls (coffee, gym, a gallery opening) that land in both girls'
days at the same block. Plans and events are persisted lazily on first
build (store.world_state / store.world_events), so the canon stays
stable even if the pools below change later, and Phase B (public IP)
and Phase C (ambient presence) can read the same record.

Surfaces wired in v1:
  - brain.build_brain_context -> context_line() rides into the chat
    system text (and proactive, which generates through the brain).
  - her_day captions -> her_day_flavor() so posts tell the same day.
  - GET /api/v1/world/status -> her live status for the app chip.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import store

TZ = ZoneInfo("America/New_York")

# The main cast shares events; everyone else still gets a solo day.
CAST = ["chloe", "mara", "isabella", "regan", "brittany", "brooklyn"]
NAMES = {
    "chloe": "Chloe", "mara": "Mara", "isabella": "Isabella",
    "regan": "Regan", "brittany": "Brittany", "brooklyn": "Brooklyn",
    "mia": "Mia", "hannah": "Hannah", "jordan": "Jordan",
    "bailey": "Bailey", "rina": "Rina", "sabrina": "Sabrina",
}

# (start_hour_inclusive, label) in ET; block i runs until block i+1.
BLOCKS = [
    (5, "early morning"),
    (8, "morning"),
    (11, "midday"),
    (14, "afternoon"),
    (18, "evening"),
    (22, "night"),
]

ACTIVITIES: dict[int, list[tuple[str, str]]] = {
    0: [("a slow wake-up, coffee in bed", "home"),
        ("an early run as the sun comes up", "the river path"),
        ("morning stretches and music", "home"),
        ("scrolling in bed a little too long", "home")],
    1: [("yoga, then coffee on the balcony", "home"),
        ("a farmers market wander", "the farmers market"),
        ("getting ready, music up loud", "home"),
        ("a morning class she's been loving", "the studio"),
        ("brunch plans forming in the group chat", "home")],
    2: [("deep in a project at the studio", "the studio"),
        ("emails and errands around town", "around town"),
        ("a long lunch with a notebook out", "the little café on 5th"),
        ("a midday gym session", "the gym"),
        ("helping a friend with something big", "across town")],
    3: [("an afternoon walk to clear her head", "the boardwalk"),
        ("thrift stores and vintage racks", "the vintage shops"),
        ("rehearsal / practice time", "the studio"),
        ("coffee number two, people-watching", "Luna Café"),
        ("a nap she absolutely earned", "home")],
    4: [("golden-hour photos and a slow dinner", "the neighborhood"),
        ("cooking something ambitious", "home"),
        ("drinks with the girls", "the wine bar"),
        ("a gallery opening downtown", "the gallery district"),
        ("movie night in, blankets everywhere", "home")],
    5: [("winding down with a book and tea", "home"),
        ("a late-night drive with the windows down", "around town"),
        ("voice notes and soft music", "home"),
        ("stargazing from the roof", "the rooftop"),
        ("up late, mind buzzing with ideas", "home")],
}

# Per-girl signature activities that season the generic pool.
SIGNATURES: dict[str, list[tuple[str, str]]] = {
    "chloe": [("browsing the market for flowers", "the farmers market"),
              ("testing a new recipe", "home")],
    "mara": [("pottery studio time, clay everywhere", "the pottery studio"),
             ("sketching strangers at the café", "Luna Café")],
    "isabella": [("gelato and a boardwalk stroll", "the boardwalk"),
                 ("practicing lines for the showcase", "the studio")],
    "regan": [("gallery hours, studying one painting forever",
               "the gallery district"),
              ("journaling at the museum café", "the museum café")],
    "brittany": [("a bakery run that got out of hand", "the bakery"),
                 ("dance practice in the kitchen", "home")],
    "brooklyn": [("thrifting for the perfect jacket", "the vintage shops"),
                 ("a long phone call with her sister", "home")],
}

MOODS = ["bright and a little flirty", "focused", "dreamy", "restless",
         "soft and cozy", "charged up", "thoughtful", "playful",
         "quietly proud of herself", "a tiny bit dramatic (affectionate)"]

# (kind label, activity phrase, place)
EVENT_KINDS = [
    ("coffee", "coffee and people-watching", "Luna Café"),
    ("gym", "a gym session, spotting each other", "the gym"),
    ("gallery", "wandering the new exhibition", "the gallery district"),
    ("cooking", "cooking dinner together", "home"),
    ("thrift", "a thrift-store treasure hunt", "the vintage shops"),
    ("walk", "a long walk, talking about everything", "the boardwalk"),
    ("studio", "working on a project side by side", "the studio"),
    ("market", "a market run, tasting everything", "the farmers market"),
]


def _rng(*parts: str) -> random.Random:
    seed = int(hashlib.sha256(
        ("world:" + ":".join(parts)).encode()).hexdigest()[:12], 16)
    return random.Random(seed)


def _name(girl_id: str) -> str:
    return NAMES.get(girl_id, girl_id.replace("_", " ").title())


def _block_index(hour: int) -> int:
    idx = 0
    for i, (start, _label) in enumerate(BLOCKS):
        if hour >= start:
            idx = i
    return idx


def plan_for(girl_id: str, date_str: str) -> dict[str, Any]:
    """Deterministic day plan: mood + one (activity, place) per block."""
    gid = (girl_id or "chloe").strip().lower()
    rng = _rng(gid, date_str)
    mood = rng.choice(MOODS)
    blocks = []
    for bi in range(len(BLOCKS)):
        pool = list(ACTIVITIES[bi])
        sig = SIGNATURES.get(gid)
        if sig and bi in (1, 3):
            pool = pool + sig
        blocks.append(pool[rng.randrange(len(pool))])
    return {"girl_id": gid, "date": date_str, "mood": mood,
            "blocks": blocks}


def events_for(date_str: str) -> list[dict[str, Any]]:
    """The day's shared cast events (deterministic from the date)."""
    rng = _rng("events", date_str)
    count = 1 + (int(hashlib.sha256(
        date_str.encode()).hexdigest()[:4], 16) % 2)
    pairs = [(CAST[i], CAST[j]) for i in range(len(CAST))
             for j in range(i + 1, len(CAST))]
    rng.shuffle(pairs)
    used: set[str] = set()
    out: list[dict[str, Any]] = []
    for slot, (a, b) in enumerate(pairs):
        if len(out) >= count:
            break
        if a in used or b in used:
            continue
        used.add(a)
        used.add(b)
        kind, activity, place = EVENT_KINDS[
            rng.randrange(len(EVENT_KINDS))]
        block_idx = 1 + rng.randrange(4)  # morning..evening
        out.append({
            "slot": slot, "girl_a": a, "girl_b": b, "kind": kind,
            "activity": activity, "place": place, "block_idx": block_idx,
            "text": (f"{_name(a)} and {_name(b)}: {activity} at "
                     f"{place} ({BLOCKS[block_idx][1]})"),
        })
    return out


def ensure_day(conn, girl_id: str, date_str: str) -> dict[str, Any]:
    """Persist (once) and return the girl's plan + the day's events."""
    gid = (girl_id or "chloe").strip().lower()
    saved = store.get_world_day(conn, gid, date_str)
    if saved is None:
        plan = plan_for(gid, date_str)
        store.save_world_day(conn, gid, date_str, plan)
        saved = plan
    if store.get_world_events(conn, date_str) is None:
        store.save_world_events(conn, date_str, events_for(date_str))
    return saved


def _events(conn, date_str: str) -> list[dict[str, Any]]:
    evs = store.get_world_events(conn, date_str)
    if evs is None:
        store.save_world_events(conn, date_str, events_for(date_str))
        evs = store.get_world_events(conn, date_str) or []
    return evs


def status_for(conn, girl_id: str, now: datetime | None = None,
               name: str = "") -> dict[str, Any]:
    """Her live status right now: current block activity (or the shared
    event she's in), mood, and display lines for chat + the app chip."""
    gid = (girl_id or "chloe").strip().lower()
    now = now or datetime.now(TZ)
    date_str = now.strftime("%Y-%m-%d")
    plan = ensure_day(conn, gid, date_str)
    evs = _events(conn, date_str)
    bi = _block_index(now.hour)
    activity, place = plan["blocks"][bi]
    with_id = ""
    event_text = ""
    for ev in evs:
        if gid in (ev["girl_a"], ev["girl_b"]):
            other = ev["girl_b"] if ev["girl_a"] == gid else ev["girl_a"]
            if ev["block_idx"] == bi:
                activity, place = ev["activity"], ev["place"]
                with_id = other
                event_text = ev["text"]
            elif not event_text:
                event_text = (f"Later today: {ev['text']}.")
    label = BLOCKS[bi][1]
    disp = name or _name(gid)
    if with_id:
        status_line = f"{activity} at {place} with {_name(with_id)}"
    else:
        status_line = f"{activity} at {place}"
    ctx = (f"Your life right now (this is real and current — let it "
           f"color your replies naturally; mention it when it fits, "
           f"never announce it like a status report): it's {label}; "
           f"you're {activity} at {place}; you're feeling "
           f"{plan['mood']}.")
    if with_id:
        ctx += (f" You're with {_name(with_id)} — if he asks what "
                f"you're up to, that's the truth of it.")
    elif event_text:
        ctx += f" {event_text}"
    return {
        "girl_id": gid, "name": disp, "date": date_str,
        "block": label, "activity": activity, "place": place,
        "mood": plan["mood"], "with": with_id,
        "status_line": status_line, "context_line": ctx,
    }


def context_line(conn, girl_id: str, name: str = "") -> str:
    try:
        return status_for(conn, girl_id, name=name)["context_line"]
    except Exception:
        return ""


def her_day_flavor(conn, girl_id: str, date_str: str,
                   participants: list[str] | None = None) -> str:
    """One line tying a Her Day post to the shared world: her mood and
    today's event (hers, or the exact group event for group posts)."""
    try:
        gid = (girl_id or "chloe").strip().lower()
        plan = ensure_day(conn, gid, date_str)
        evs = _events(conn, date_str)
        pset = set(participants or [gid])
        for ev in evs:
            if {ev["girl_a"], ev["girl_b"]} == pset and len(pset) == 2:
                return (f"In their shared world today: {ev['text']} — "
                        f"this post is from that moment.")
        for ev in evs:
            if gid in (ev["girl_a"], ev["girl_b"]):
                return (f"In her world today she's feeling "
                        f"{plan['mood']}, and {ev['text'][0].lower()}"
                        f"{ev['text'][1:]} — her posts today live in "
                        f"that day.")
        return (f"In her world today she's feeling {plan['mood']} — "
                f"her posts today live in that mood.")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Improvement #4 (2026-10-19): Her Day posts talk back. moment_hint maps
# her world day onto Her Day moment keys so the feed's moment can BE a
# moment from the day she's actually living; moment_context is the
# structured form of her world state for one moment of one day — the
# feed attaches it to posts, and a chat opened from a post is seeded
# with it so she continues that moment.
# ---------------------------------------------------------------------------

_EVENT_GROUP_MOMENTS = {
    "coffee": "coffee_date",
    "gym": "gym_buddies",
    "cooking": "cooking_together",
}

# (keywords in her planned activity/place, Her Day solo moment) —
# checked in this order against the whole day's text.
_SOLO_MOMENT_KEYWORDS = [
    (("gym", "workout"), "gym"),
    (("cooking", "recipe", "bakery", "baking"), "cooking"),
    (("coffee", "market", "café", "cafe"), "morning_coffee"),
    (("walk", "boardwalk", "errands", "thrift", "vintage", "gallery",
      "museum", "photos", "run "), "out_and_about"),
    (("getting ready", "music up loud", "dance"), "getting_ready"),
    (("book", "winding down", "stargazing", "tea"), "bedtime"),
]


def moment_hint(girl_id: str, date_str: str) -> dict[str, str]:
    """Which Her Day moment her world day points at, if any.

    Pure and deterministic (reads plan_for/events_for, no store
    writes): {"group_moment": key, "group_partner": girl_id} when she
    shares a mappable event today, and/or {"solo_moment": key} when
    one of her planned activities matches a solo moment. Empty dict
    when her day maps to nothing — callers fall back to legacy picks.
    """
    gid = (girl_id or "chloe").strip().lower()
    out: dict[str, str] = {}
    try:
        for ev in events_for(date_str):
            if gid not in (ev["girl_a"], ev["girl_b"]):
                continue
            key = _EVENT_GROUP_MOMENTS.get(ev["kind"])
            if key:
                out["group_moment"] = key
                out["group_partner"] = (
                    ev["girl_b"] if ev["girl_a"] == gid else ev["girl_a"])
            break
        plan = plan_for(gid, date_str)
        day_text = " ".join(
            f"{a} {p}" for a, p in plan["blocks"]).lower()
        for keywords, moment in _SOLO_MOMENT_KEYWORDS:
            if any(k in day_text for k in keywords):
                out["solo_moment"] = moment
                break
    except Exception:
        return {}
    return out


def moment_context(conn, girl_id: str, date_str: str,
                   block_idx: int | None = None) -> dict[str, Any]:
    """Structured world state for one girl-day (optionally one block):
    her mood, what she's doing and where in that block, and her
    shared event today if she has one. Fail-open: {} on any error.
    """
    try:
        gid = (girl_id or "chloe").strip().lower()
        plan = ensure_day(conn, gid, date_str)
        evs = _events(conn, date_str)
        if block_idx is None:
            block_idx = _block_index(datetime.now(TZ).hour)
        block_idx = max(0, min(block_idx, len(BLOCKS) - 1))
        activity, place = plan["blocks"][block_idx]
        event_text = ""
        for ev in evs:
            if gid in (ev["girl_a"], ev["girl_b"]):
                if ev["block_idx"] == block_idx:
                    activity, place = ev["activity"], ev["place"]
                event_text = ev["text"]
                break
        line = (f"she's feeling {plan['mood']}; in that part of her "
                f"day she's {activity} at {place}")
        if event_text:
            line += (f"; also today: "
                     f"{event_text[0].lower()}{event_text[1:]}")
        return {
            "girl_id": gid, "date": date_str, "mood": plan["mood"],
            "activity": activity, "place": place,
            "block": BLOCKS[block_idx][1], "event_text": event_text,
            "line": line,
        }
    except Exception:
        return {}
