"""Duo mode (2026-10-17): two companions in one thread.

The main brain is deeply single-character, so duo runs its own light
generation: both personas + the pair's transcript (stored under a
synthetic duo:<a>:<b> character key in the normal conversation table)
go to Gemini, which writes the next messages with [Name] speaker
prefixes. One message charge per user send, same as normal chat.

Speaker integrity (2026-10-17 fix): the model once wrote lines for a
girl who was not in the chat (it produced [Chloe] lines in a Brooklyn +
Isabella thread, and they were stored and labeled as hers). Two holes
made that possible: characters.get_character() silently falls back to
Chloe for unresolvable ids, so girl_info could hand back Chloe's name
and persona under another girl's id; and the line parser passed
foreign [Name] prefixes through as text and defaulted unprefixed
lines to the first girl. Now girl_info only accepts a character whose
resolved id matches the request, and the parser drops any line spoken
by someone outside the chosen speaker set — a girl who is not in the
chat can never appear as a speaker.

Participation (2026-10-17): the girls do NOT both reply to every
message. A deterministic chooser picks who speaks before the model
call: a question directed at one girl is answered by her alone; a
named girl leads (the other joins only occasionally); an open message
varies between one reply and both. At least one girl always replies.
"""
from __future__ import annotations

import random
import re
from typing import Any, Callable

from . import characters, gemini_keys

_DUO_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.1-flash-lite:generateContent")
_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z .'-]{0,30})\s*[:\-–]\s*(.+)$")
_BRACKET_RE = re.compile(r"^\[\s*([A-Za-z][A-Za-z .'-]{0,30})\s*\]\s*(.*)$")
_QUESTION_WORDS = {
    "what", "why", "how", "when", "where", "who", "whom", "which",
    "whose", "do", "does", "did", "can", "could", "would", "will",
    "is", "are", "was", "were", "have", "has", "should", "may", "might",
}
# Canned lines if the model call fails — one per girl, used only for
# girls the chooser actually picked to speak.
_CANNED = (
    "okay he's here 💜 hi you",
    "took you long enough 😌 what are we getting into?",
)


def duo_key(a: str, b: str) -> str:
    return "duo:" + ":".join(sorted([a, b]))


def girl_info(cid: str, user_id: str | None = None) -> dict[str, Any] | None:
    """Resolve a duo participant. characters.get_character() falls back
    to Chloe for unknown ids and for other users' private characters —
    that fallback must never leak into a duo (it is how a third girl's
    name and persona once entered a two-girl thread), so accept only a
    character whose resolved id is exactly the requested one."""
    try:
        ch = characters.get_character(cid, user_id)
    except Exception:
        return None
    if not ch:
        return None
    if (ch.get("id") or "").strip().lower() != (cid or "").strip().lower():
        return None
    return {"id": cid, "name": ch.get("name") or cid,
            "persona": (ch.get("persona") or "")[:1100]}


def _speaker_maps(girls: list[dict]) -> tuple[dict, dict]:
    by_name: dict[str, dict] = {}
    first: dict[str, dict] = {}
    for g in girls:
        by_name[g["name"].lower()] = g
        first.setdefault(g["name"].split()[0].lower(), g)
    return by_name, first


def _lookup(name: str, by_name: dict, first: dict) -> dict | None:
    key = (name or "").strip().lower()
    return by_name.get(key) or first.get(key)


def _parse_lines(text: str, allowed: list[dict],
                 pair: list[dict] | None = None) -> list[dict[str, str]]:
    """Parse model output into messages from `allowed` speakers only.

    Bracketed prefixes are the speaker-marker contract: a bracketed
    name that is not an allowed speaker marks a line spoken by someone
    outside the chat — the line is dropped whole, never relabeled. A
    "Name:" prefix naming the non-speaking member of the pair is also
    dropped (she was chosen to stay silent this turn); any other colon
    line is ordinary prose under the current speaker. Returned text
    never carries a speaker prefix, so the "[Name] text" the endpoint
    stores always names the girl who actually said it.
    """
    pair = pair or allowed
    by_name, first = _speaker_maps(allowed)
    silent_maps = _speaker_maps([g for g in pair if g not in allowed])
    out: list[dict[str, str]] = []
    current: dict | None = None
    for raw in (text or "").splitlines():
        line = raw.strip().strip("*").strip()
        if not line:
            continue
        dropped = False
        while True:
            bm = _BRACKET_RE.match(line)
            if not bm:
                break
            g = _lookup(bm.group(1), by_name, first)
            if g is None:
                dropped = True
                break
            current = g
            line = bm.group(2).strip()
        if dropped or not line:
            continue
        m = _LINE_RE.match(line)
        if m:
            g = _lookup(m.group(1), by_name, first)
            if g is not None:
                current = g
                line = m.group(2).strip()
            elif _lookup(m.group(1), *silent_maps) is not None:
                continue
        if current is None:
            current = allowed[0]
        if not line:
            continue
        out.append({"girl_id": current["id"], "name": current["name"],
                    "text": line[:400]})
        if len(out) >= 4:
            break
    return out


def _mentioned(text_low: str, girl: dict) -> bool:
    names = {girl["name"].lower(), girl["name"].split()[0].lower()}
    return any(n and re.search(r"\b" + re.escape(n) + r"\b", text_low)
               for n in names)


def _is_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t:
        return True
    # A question word up front — including right after a leading name
    # ("Brooklyn what did you do today") — marks it as a question.
    tokens = [w.strip(".,!¡¿") for w in re.split(r"\s+", t.lower())[:3]]
    return any(w in _QUESTION_WORDS for w in tokens)


def choose_speakers(ga: dict, gb: dict, user_text: str,
                    last_speaker_id: str | None = None,
                    rand: Callable[[], float] | None = None
                    ) -> list[dict]:
    """Pick who replies to this user message (primary first, >= 1).

    - Both girls named -> both reply.
    - One girl named -> she leads; a question to her is hers alone,
      otherwise the other girl joins only occasionally (~25%).
    - Nobody named -> the lead alternates with whoever spoke last;
      a general question is usually answered by one girl (~80%),
      an open message gets both voices about half the time.
    """
    rand = rand or random.random
    low = (user_text or "").lower()
    a_hit = _mentioned(low, ga)
    b_hit = _mentioned(low, gb)
    question = _is_question(user_text)
    if a_hit and b_hit:
        return [ga, gb]
    if a_hit or b_hit:
        primary = ga if a_hit else gb
        other = gb if a_hit else ga
        if question:
            return [primary]
        return [primary, other] if rand() < 0.25 else [primary]
    primary, other = (gb, ga) if last_speaker_id == ga["id"] else (ga, gb)
    if question:
        return [primary, other] if rand() < 0.2 else [primary]
    return [primary, other] if rand() < 0.5 else [primary]


def _last_speaker_id(rows, ga: dict, gb: dict) -> str | None:
    by_name, first = _speaker_maps([ga, gb])
    for r in rows:  # rows are newest-first
        if r["role"] == "user":
            continue
        m = _BRACKET_RE.match(str(r["text"]).strip())
        if m:
            g = _lookup(m.group(1), by_name, first)
            if g is not None:
                return g["id"]
    return None


def generate_duo_reply(conn, user_id: str, ga: dict, gb: dict,
                       user_text: str, user_name: str,
                       rand: Callable[[], float] | None = None
                       ) -> list[dict]:
    key = duo_key(ga["id"], gb["id"])
    rows = conn.execute(
        "SELECT role, text FROM conversation WHERE user_id = ? "
        "AND character_id = ? ORDER BY id DESC LIMIT 14",
        (user_id, key)).fetchall()
    transcript = []
    for r in reversed(rows):
        if r["role"] == "user":
            transcript.append(f"Him: {r['text']}")
        else:
            transcript.append(str(r["text"]))
    speakers = choose_speakers(ga, gb, user_text,
                               last_speaker_id=_last_speaker_id(
                                   rows, ga, gb),
                               rand=rand)
    if len(speakers) == 1:
        only = speakers[0]
        other = gb if only is ga else ga
        speak_rule = (
            f"Only {only['name']} replies this time — {other['name']} "
            "stays quiet and does NOT speak. Write exactly ONE short "
            f"message, from {only['name']} only, prefixed exactly with "
            f"[{only['name'].split()[0]}]. ")
    else:
        speak_rule = (
            "Write the next 2 to 3 short chat messages total between "
            "the two of them. Each message goes on its own line, "
            "prefixed exactly with the speaker's first name in "
            "brackets, like "
            f"[{ga['name'].split()[0]}] text. They react to him AND to "
            "each other — playful, natural, a little teasing rivalry "
            "is fun. ")
    prompt = (
        f"You are writing a group chat between {ga['name']} and "
        f"{gb['name']} — two companions — and their boyfriend "
        f"{user_name or 'him'}. Only {ga['name']} and {gb['name']} "
        "are in this chat; nobody else ever speaks.\n\n"
        f"{ga['name']}'s personality: {ga['persona']}\n\n"
        f"{gb['name']}'s personality: {gb['persona']}\n\n"
        "Rules: " + speak_rule +
        "Each message under 240 characters. Sound like real texting. "
        "Adults only, consensual. No narration outside the messages.\n\n"
        "Recent conversation:\n"
        + "\n".join(transcript[-12:])
        + f"\nHim: {user_text}\n"
    )
    data = gemini_keys.post_json(
        _DUO_URL,
        {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
         "generationConfig": {"temperature": 0.95,
                              "maxOutputTokens": 600}},
        timeout=60, source="whispr-duo")
    text = ""
    if data:
        try:
            text = "".join(
                p.get("text", "") for p in
                data["candidates"][0]["content"]["parts"]).strip()
        except Exception:
            text = ""
    msgs = _parse_lines(text, speakers, pair=[ga, gb]) if text else []
    if not msgs:
        canned = {ga["id"]: _CANNED[0], gb["id"]: _CANNED[1]}
        msgs = [{"girl_id": g["id"], "name": g["name"],
                 "text": canned[g["id"]]} for g in speakers]
    return msgs
