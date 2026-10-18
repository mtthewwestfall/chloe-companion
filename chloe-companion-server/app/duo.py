"""Duo mode (2026-10-17): two companions in one thread.

The main brain is deeply single-character, so duo runs its own light
generation: both personas + the pair's transcript (stored under a
synthetic duo:<a>:<b> character key in the normal conversation table)
go to Gemini, which writes the next 1-3 messages with [Name] speaker
prefixes. One message charge per user send, same as normal chat.
"""
from __future__ import annotations

import re
from typing import Any

from . import characters, gemini_keys

_DUO_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.1-flash-lite:generateContent")
_LINE_RE = re.compile(r"^\[?([A-Za-z][A-Za-z .'-]{0,30})\]?\s*[:\-–]\s*(.+)$")


def duo_key(a: str, b: str) -> str:
    return "duo:" + ":".join(sorted([a, b]))


def girl_info(cid: str) -> dict[str, Any] | None:
    try:
        ch = characters.get_character(cid)
    except Exception:
        return None
    if not ch:
        return None
    return {"id": cid, "name": ch.get("name") or cid,
            "persona": (ch.get("persona") or "")[:1100]}


def _parse_lines(text: str, ga: dict, gb: dict) -> list[dict[str, str]]:
    by_name = {ga["name"].lower(): ga, gb["name"].lower(): gb}
    first = {ga["name"].split()[0].lower(): ga,
             gb["name"].split()[0].lower(): gb}
    out: list[dict[str, str]] = []
    current = ga
    for raw in (text or "").splitlines():
        line = raw.strip().strip("*").strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if m and (m.group(1).strip().lower() in by_name
                  or m.group(1).strip().lower() in first):
            key = m.group(1).strip().lower()
            current = by_name.get(key) or first.get(key) or current
            line = m.group(2).strip()
        if not line:
            continue
        out.append({"girl_id": current["id"], "name": current["name"],
                    "text": line[:400]})
        if len(out) >= 4:
            break
    return out


def generate_duo_reply(conn, user_id: str, ga: dict, gb: dict,
                       user_text: str, user_name: str) -> list[dict]:
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
    prompt = (
        f"You are writing a group chat between {ga['name']} and "
        f"{gb['name']} — two companions — and their boyfriend "
        f"{user_name or 'him'}.\n\n"
        f"{ga['name']}'s personality: {ga['persona']}\n\n"
        f"{gb['name']}'s personality: {gb['persona']}\n\n"
        "Rules: write the next 1 to 3 short chat messages total. Each "
        "message goes on its own line, prefixed exactly with the "
        "speaker's first name in brackets, like "
        f"[{ga['name'].split()[0]}] text. They react to him AND to each "
        "other — playful, natural, a little teasing rivalry is fun. "
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
    msgs = _parse_lines(text, ga, gb) if text else []
    if not msgs:
        msgs = [
            {"girl_id": ga["id"], "name": ga["name"],
             "text": "okay he's here 💜 hi you"},
            {"girl_id": gb["id"], "name": gb["name"],
             "text": "took you long enough 😌 what are we getting into?"},
        ]
    return msgs
