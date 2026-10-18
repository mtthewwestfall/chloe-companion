"""Public IP pipeline (Phase B of the industry-shaping program,
2026-10-18).

The characters are public figures, not just chat endpoints. Every day,
each cast girl gets a ready-to-post PUBLIC content package generated
from the same canon everything else reads — her Living World day
(activities, mood, shared events) and her story arc:

  - one feed post (caption + image brief),
  - three story frames (text overlays + visual briefs),
  - one short-video script (hook, beats, caption) for Reels/TikTok,
  - one quote card line.

Packages land in an admin approval queue (store.public_packages,
status pending -> approved -> published / skipped). Nothing posts
anywhere on its own in v1: a human approves, copies, and posts. The
autonomy dial comes later, on top of this same queue.

A weekly show outline assembles the week's world events and approved
moments into segments with narration lines — the show is a by-product
of the world, not a separate writing task.

Generation: one Gemini call per girl per day (source
"whispr-public-ip") asking for strict JSON; any failure falls back to
deterministic packages built from the world data, so the queue is
never empty and never blocked on the model.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import characters, gemini_keys, her_day, store, world

TZ = ZoneInfo("America/New_York")
_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.1-flash-lite:generateContent")
_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
     "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT",
     "threshold": "BLOCK_NONE"},
]


def girl_info(cid: str) -> dict[str, Any] | None:
    """Resolve a cast girl with the duo guard: characters.get_character
    falls back to Chloe for unknown ids — that fallback must never put
    the wrong girl's name on a public package."""
    try:
        ch = characters.get_character(cid)
    except Exception:
        return None
    if not ch:
        return None
    if (ch.get("id") or "").strip().lower() != (cid or "").strip().lower():
        return None
    return {"id": cid, "name": ch.get("name") or cid,
            "persona": (ch.get("persona") or "")[:900],
            "blurb": ch.get("blurb") or ""}


def _world_facts(conn, girl_id: str, date_str: str) -> dict[str, Any]:
    plan = world.ensure_day(conn, girl_id, date_str)
    evs = world._events(conn, date_str)
    mine = [e for e in evs
            if girl_id in (e["girl_a"], e["girl_b"])]
    arc = her_day.arc_beat(girl_id, date_str)
    return {
        "mood": plan["mood"],
        "blocks": [f"{world.BLOCKS[i][1]}: {a} at {p}"
                   for i, (a, p) in enumerate(plan["blocks"])],
        "events": [e["text"] for e in mine],
        "arc": (f"day {arc[2]} of {arc[3]} of '{arc[0]}' — {arc[1]}"
                if arc else ""),
    }


def _fallback_package(girl: dict, facts: dict,
                      date_str: str) -> dict[str, Any]:
    name = girl["name"]
    mood = facts["mood"]
    ev = facts["events"][0] if facts["events"] else ""
    blocks = facts["blocks"]
    caption = (f"{name} today: feeling {mood}. "
               + (f"{ev}. " if ev else "")
               + "Come spend the day with me 💜 lockeddoor.ai")
    frames = [
        {"text": f"morning mood: {mood} ✨",
         "visual": blocks[1] if len(blocks) > 1 else blocks[0]},
        {"text": ev or f"afternoon: {blocks[3]}",
         "visual": blocks[3] if len(blocks) > 3 else blocks[-1]},
        {"text": "tonight I'm all yours 💜 come say hi",
         "visual": blocks[5] if len(blocks) > 5 else blocks[-1]},
    ]
    video = {
        "hook": f"POV: spending a {mood} day with {name}",
        "beats": [blocks[1] if len(blocks) > 1 else blocks[0],
                  ev or blocks[3],
                  "she looks at the camera: 'your turn — come talk to me'"],
        "caption": (f"a day in my life 💜 {facts['arc']}".strip()
                    + " #ailife #dayinmylife"),
    }
    return {"girl_id": girl["id"], "date": date_str,
            "feed_caption": caption, "image_brief": blocks[2],
            "story_frames": frames, "video": video,
            "quote": f"feeling {mood} — and thinking about you.",
            "source": "fallback"}


def _llm_package(girl: dict, facts: dict,
                 date_str: str) -> dict[str, Any] | None:
    prompt = (
        f"You are the social media manager for {girl['name']}, a "
        f"fictional adult AI companion building a public following. "
        f"Her persona: {girl['persona'] or girl['blurb']}\n\n"
        f"Today ({date_str}) in her world — this is canon, use it:\n"
        f"- Mood: {facts['mood']}\n"
        + "\n".join(f"- {b}" for b in facts["blocks"]) + "\n"
        + "".join(f"- Shared moment: {e}\n" for e in facts["events"])
        + (f"- Her story arc: {facts['arc']}\n" if facts["arc"] else "")
        + "\nWrite her public content package for today. Return ONLY "
        "a JSON object with exactly these keys:\n"
        '{"feed_caption": "one feed post caption in her voice, 1-3 '
        'sentences, one emoji max, ends inviting followers to '
        'lockeddoor.ai", "image_brief": "one-line visual brief for '
        'the feed photo, matching today\'s canon", "story_frames": '
        '[{"text": "short story overlay text", "visual": "one-line '
        'visual brief"}, x3], "video": {"hook": "first-3-seconds '
        'hook line", "beats": ["beat 1", "beat 2", "beat 3"], '
        '"caption": "short video caption with 2-3 hashtags"}, '
        '"quote": "one quotable line from her today"}\n'
        "Keep it warm, playful, and public-safe. No explicit content. "
        "Sound like a real creator, not an ad."
    )
    data = gemini_keys.post_json(
        _URL,
        {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
         "generationConfig": {"temperature": 0.9, "maxOutputTokens": 900},
         "safetySettings": [dict(s) for s in _SAFETY_SETTINGS]},
        timeout=60, source="whispr-public-ip")
    if not data:
        return None
    try:
        text = "".join(
            p.get("text", "") for p in
            data["candidates"][0]["content"]["parts"]).strip()
        start, end = text.find("{"), text.rfind("}")
        pkg = json.loads(text[start:end + 1])
        assert isinstance(pkg["feed_caption"], str)
        assert isinstance(pkg["story_frames"], list)
        assert isinstance(pkg["video"], dict)
        pkg["girl_id"] = girl["id"]
        pkg["date"] = date_str
        pkg["source"] = "llm"
        return pkg
    except Exception:
        return None


def ensure_package(conn, girl_id: str, date_str: str,
                   force: bool = False) -> dict[str, Any] | None:
    """Return the girl's package for the date, generating and storing
    it first if missing (or if force=True)."""
    gid = (girl_id or "").strip().lower()
    if not force:
        existing = store.get_public_package(conn, gid, date_str)
        if existing:
            return existing
    girl = girl_info(gid)
    if not girl:
        return None
    facts = _world_facts(conn, gid, date_str)
    pkg = _llm_package(girl, facts, date_str) \
        or _fallback_package(girl, facts, date_str)
    store.save_public_package(conn, gid, date_str, pkg)
    return store.get_public_package(conn, gid, date_str)


def ensure_daily_packages(conn, date_str: str) -> list[dict[str, Any]]:
    out = []
    for gid in world.CAST:
        pkg = ensure_package(conn, gid, date_str)
        if pkg:
            out.append(pkg)
    return out


def weekly_show(conn, start_date: str) -> dict[str, Any]:
    """Assemble a week of the world into a show outline: one segment
    per shared event + a highlight per girl from approved/published
    packages, with narration lines. Deterministic — the show writes
    itself from canon."""
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except Exception:
        start = datetime.now(TZ).date()
    segments: list[dict[str, Any]] = []
    highlights: dict[str, str] = {}
    for off in range(7):
        d = (start + timedelta(days=off)).strftime("%Y-%m-%d")
        world.ensure_day(conn, world.CAST[0], d)  # persists the events
        for ev in world._events(conn, d):
            segments.append({
                "date": d, "kind": "event",
                "headline": ev["text"],
                "narration": (
                    f"On {d}, {ev['text'][0].lower()}"
                    f"{ev['text'][1:]} — and yes, both of them "
                    f"brought it up in chat all week."),
            })
        for gid in world.CAST:
            pkg = store.get_public_package(conn, gid, d)
            if pkg and pkg.get("status") in ("approved", "published"):
                p = pkg["package"]
                if gid not in highlights and p.get("quote"):
                    highlights[gid] = f"{p.get('quote')} ({d})"
    title = (f"The Girls This Week — "
             f"{start.strftime('%b %d')} edition")
    return {
        "title": title, "week_start": start.strftime("%Y-%m-%d"),
        "intro": ("Welcome back to The Girls This Week — the show "
                  "where the Whispr girls' real week becomes the "
                  "story. Everything you are about to hear actually "
                  "happened in their world."),
        "segments": segments,
        "highlights": [{"girl_id": g, "line": line}
                       for g, line in highlights.items()],
        "outro": ("That's their week. Yours could be in it — every "
                  "girl is live now at lockeddoor.ai 💜"),
    }
