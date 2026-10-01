"""Assistant capabilities ("her hands"): web search, reminders, email drafts,
job lookup for the companion app.

Tier-gated: the higher the tier, the more she can do. Rollout is owner-only
until the owner approves wider release (ASSIST_ROLLOUT env).

SECURITY — the credential rule, non-negotiable:
  User logins (Gmail OAuth tokens etc.) NEVER touch this server. They live
  ONLY on the user's own device (browser storage); the browser calls the
  provider APIs directly. This module only:
    - performs keyless web lookups (server-side, via our own Gemini key),
    - stores reminders (plain text + time — not credentials),
    - composes email draft text (the CLIENT creates the Gmail draft).
  No credential values are accepted, logged, or persisted anywhere here.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

# ---------------------------------------------------------------------------
# Tier capability matrix — higher tier, more she can do.
# web_search: lookups per day. reminders: max active reminders.
# email_drafts / job_lookup: feature flags.
# ---------------------------------------------------------------------------
CAPS = {
    "basic": {"web_search": 20, "reminders": 10, "email_drafts": False,
              "job_lookup": False},
    "plus": {"web_search": 50, "reminders": 25, "email_drafts": True,
             "job_lookup": True},
    "vip": {"web_search": 100, "reminders": 50, "email_drafts": True,
            "job_lookup": True},
}

_TZ = ZoneInfo("America/New_York")

_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "gemini-3.1-flash-lite:generateContent")


# ---------------------------------------------------------------------------
# Rollout gate — owner-only until he says go.
# ---------------------------------------------------------------------------
def _owner_ids() -> set[str]:
    raw = os.environ.get("ASSIST_OWNER_IDS", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def enabled_for(user_id: str, tier: str) -> bool:
    """True when this user may use assistant capabilities right now."""
    if not user_id:
        return False
    if tier not in CAPS:
        return False  # free / unknown tiers get nothing
    rollout = os.environ.get("ASSIST_ROLLOUT", "owner").strip().lower()
    if rollout == "paid":
        return True
    return user_id in _owner_ids()


def caps_for(tier: str) -> dict:
    return dict(CAPS.get(tier, {"web_search": 0, "reminders": 0,
                                "email_drafts": False, "job_lookup": False}))


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS assist_usage (
               user_id TEXT NOT NULL,
               day TEXT NOT NULL,
               searches INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (user_id, day))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reminders (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id TEXT NOT NULL,
               character_id TEXT NOT NULL DEFAULT 'chloe',
               text TEXT NOT NULL,
               remind_at TEXT NOT NULL,
               created_at TEXT NOT NULL,
               delivered INTEGER NOT NULL DEFAULT 0,
               delivered_at TEXT)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_due "
        "ON reminders (delivered, remind_at)"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Intent detection — fast regex paths. Conservative on purpose: a miss just
# means she answers from her own mouth instead of looking something up.
# ---------------------------------------------------------------------------
_REMIND_RE = re.compile(r"\bremind me\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b(draft|write)\s+(an?\s+)?e-?mail\b", re.IGNORECASE)
_JOB_RE = re.compile(
    r"\b(find|search|look\s+(for|up)).{0,40}\bjob(s)?\b"
    r"|\bjob(s)?\b.{0,40}\b(find|search|looking)\b"
    r"|\b(apply|application).{0,20}\bjob", re.IGNORECASE)
_SEARCH_HINTS = [
    r"\?", r"\bwho won\b", r"\bwhat('s| is) the (score|price|latest|current)",
    r"\bhow much (is|are|does)", r"\bwhen (is|are|does|do|will)",
    r"\blook\s+up\b", r"\bsearch\s+for\b", r"\bcheck\b.*\b(score|price|hours|weather)",
    r"\blatest\b", r"\bcurrent\b.*\b(price|score|news)",
    r"\bwhat time\b", r"\bopen\b.*\b(hours|today)",
]
_SEARCH_RE = re.compile("|".join(_SEARCH_HINTS), re.IGNORECASE)
# Things that look like questions but are about her/us — never a web lookup.
_NOLOOK_RE = re.compile(
    r"\b(do you )?(remember|miss me|love me|like me)\b"
    r"|\bwhat('s| is) your\b|\bwho are you\b|\btell me about yourself\b",
    re.IGNORECASE)


def detect_intent(text: str) -> tuple[str | None, dict]:
    t = (text or "").strip()
    if not t:
        return None, {}
    if _REMIND_RE.search(t):
        return "reminder", {}
    if _EMAIL_RE.search(t):
        return "email_draft", {}
    if _JOB_RE.search(t):
        return "job_search", {"query": t}
    if _NOLOOK_RE.search(t):
        return None, {}
    if _SEARCH_RE.search(t):
        return "web_search", {"query": t}
    return None, {}


# ---------------------------------------------------------------------------
# Web search — grounded Gemini. Keyless for the caller: uses our own key.
# ---------------------------------------------------------------------------
def _gemini_post(payload: dict, timeout: int = 30) -> dict | None:
    api_key = config.GEMINI_API_KEY
    if not api_key:
        return None
    req = urllib.request.Request(
        _GEMINI_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def web_search(query: str) -> dict | None:
    """Grounded web lookup. Returns {answer, sources[]} or None on failure."""
    data = _gemini_post({
        "contents": [{"role": "user", "parts": [
            {"text": "Answer briefly and factually, with today's date "
                     f"({datetime.now(_TZ):%B %d, %Y}) in mind: {query}"}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 500},
    })
    if not data:
        return None
    try:
        cand = data["candidates"][0]
        answer = "".join(p.get("text", "")
                         for p in cand["content"]["parts"]).strip()
        sources: list[str] = []
        for chunk in (cand.get("groundingMetadata", {})
                        .get("groundingChunks") or []):
            web = chunk.get("web") or {}
            if web.get("uri") and web["uri"] not in sources:
                sources.append(web["uri"])
        if not answer:
            return None
        return {"answer": answer, "sources": sources[:4]}
    except Exception:
        return None


def use_search(conn: sqlite3.Connection, user_id: str, caps: dict) -> bool:
    """Consume one daily lookup from the tier budget. False when exhausted."""
    ensure_tables(conn)
    day = datetime.now(_TZ).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT searches FROM assist_usage WHERE user_id = ? AND day = ?",
        (user_id, day)).fetchone()
    used = (row[0] if row else 0)
    if used >= caps["web_search"]:
        return False
    conn.execute(
        "INSERT INTO assist_usage (user_id, day, searches) VALUES (?, ?, 1) "
        "ON CONFLICT (user_id, day) DO UPDATE SET searches = searches + 1",
        (user_id, day))
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
_IN_RE = re.compile(r"\bin\s+(\d+)\s*(minute|hour|day|week)s?\b", re.IGNORECASE)
_TOMORROW_RE = re.compile(
    r"\btomorrow\b(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?",
    re.IGNORECASE)
_AT_RE = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)


def _to_utc_iso(local_dt: datetime) -> str:
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_reminder(text: str,
                   now: datetime | None = None) -> tuple[str | None, str]:
    """Returns (remind_at_utc_iso, reminder_text). (None, text) when no time
    was understood — she'll ask him when."""
    now = now or datetime.now(_TZ)
    t = (text or "").strip()
    when: datetime | None = None

    m = _IN_RE.search(t)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n),
                 "day": timedelta(days=n), "week": timedelta(weeks=n)}[unit]
        when = now + delta
        t = _IN_RE.sub("", t)
    else:
        m = _TOMORROW_RE.search(t)
        if m:
            h, mi, ap = m.group(1), m.group(2), m.group(3)
            hour, minute = int(h or 9), int(mi or 0)
            if ap and ap.lower() == "pm" and hour < 12:
                hour += 12
            if ap and ap.lower() == "am" and hour == 12:
                hour = 0
            when = (now + timedelta(days=1)).replace(hour=hour, minute=minute,
                                                    second=0, microsecond=0)
            t = _TOMORROW_RE.sub("", t)
        else:
            m = _AT_RE.search(t)
            if m:
                hour, minute = int(m.group(1)), int(m.group(2) or 0)
                ap = m.group(3).lower()
                if ap == "pm" and hour < 12:
                    hour += 12
                if ap == "am" and hour == 12:
                    hour = 0
                when = now.replace(hour=hour, minute=minute, second=0,
                                   microsecond=0)
                if when <= now:
                    when += timedelta(days=1)
                t = _AT_RE.sub("", t)

    # Clean the reminder text itself.
    t = re.sub(r"\bremind me\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^\s*(to|that|about)\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip(" .")
    if not t:
        t = "something"
    if when is None:
        return None, t
    return _to_utc_iso(when), t


def set_reminder(conn: sqlite3.Connection, user_id: str, character_id: str,
                 text: str, remind_at_iso: str) -> int:
    ensure_tables(conn)
    cur = conn.execute(
        "INSERT INTO reminders (user_id, character_id, text, remind_at, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, character_id, text[:280], remind_at_iso,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    conn.commit()
    return cur.lastrowid


def active_reminder_count(conn: sqlite3.Connection, user_id: str) -> int:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT COUNT(*) FROM reminders WHERE user_id = ? AND delivered = 0",
        (user_id,)).fetchone()
    return row[0] if row else 0


def due_reminders(conn: sqlite3.Connection,
                  user_id: str) -> list[dict]:
    """Due, undelivered reminders — marks them delivered as it returns."""
    ensure_tables(conn)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT id, character_id, text, remind_at FROM reminders "
        "WHERE user_id = ? AND delivered = 0 AND remind_at <= ? "
        "ORDER BY remind_at LIMIT 10",
        (user_id, now_iso)).fetchall()
    out = [{"id": r[0], "character_id": r[1], "text": r[2],
            "remind_at": r[3]} for r in rows]
    if out:
        conn.execute(
            "UPDATE reminders SET delivered = 1, delivered_at = ? "
            "WHERE id IN (%s)" % ",".join("?" * len(out)),
            (now_iso, *[r["id"] for r in out]))
        conn.commit()
    return out


def list_reminders(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    ensure_tables(conn)
    rows = conn.execute(
        "SELECT id, character_id, text, remind_at, created_at FROM reminders "
        "WHERE user_id = ? AND delivered = 0 ORDER BY remind_at",
        (user_id,)).fetchall()
    return [{"id": r[0], "character_id": r[1], "text": r[2],
             "remind_at": r[3], "created_at": r[4]} for r in rows]


def cancel_reminder(conn: sqlite3.Connection, user_id: str,
                    reminder_id: int) -> bool:
    ensure_tables(conn)
    cur = conn.execute(
        "DELETE FROM reminders WHERE id = ? AND user_id = ? AND delivered = 0",
        (reminder_id, user_id))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Email drafts — the server only COMPOSES. The browser creates the Gmail
# draft with the on-device token. No credentials here, ever.
# ---------------------------------------------------------------------------
def compose_draft(user_text: str) -> dict | None:
    data = _gemini_post({
        "system_instruction": {"parts": [{"text":
            "You write email drafts. Reply with ONLY a JSON object: "
            "{\"to\": \"...\", \"subject\": \"...\", \"body\": \"...\"}. "
            "Guess the recipient from context; empty string when unknown. "
            "Body is warm, short, plain text. No markdown, no commentary."}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 600},
    })
    if not data:
        return None
    try:
        text = "".join(p.get("text", "")
                       for p in data["candidates"][0]["content"]["parts"])
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        draft = json.loads(text)
        return {"to": str(draft.get("to", "")),
                "subject": str(draft.get("subject", "")),
                "body": str(draft.get("body", ""))}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Job lookup — grounded search, jobs-focused.
# ---------------------------------------------------------------------------
def job_search(query: str) -> dict | None:
    data = _gemini_post({
        "contents": [{"role": "user", "parts": [{"text":
            "Find current job openings matching this request. Reply as a "
            "short plain-text list: job title — company — location — one line "
            "why it fits. Max 6. Then 'Sources:' with the site names. "
            f"Request: {query}"}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 700},
    })
    if not data:
        return None
    try:
        cand = data["candidates"][0]
        answer = "".join(p.get("text", "")
                         for p in cand["content"]["parts"]).strip()
        sources: list[str] = []
        for chunk in (cand.get("groundingMetadata", {})
                        .get("groundingChunks") or []):
            web = chunk.get("web") or {}
            if web.get("uri") and web["uri"] not in sources:
                sources.append(web["uri"])
        return {"answer": answer, "sources": sources[:4]} if answer else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main entry: called from the chat path. Never raises — fail open.
# Returns None or {"brief": str, "email_draft": {...} (optional)}.
# The brief is injected into her system prompt so she reports it in her
# own voice.
# ---------------------------------------------------------------------------
def maybe_assist(conn: sqlite3.Connection, user: dict, text: str,
                 character_id: str) -> dict | None:
    try:
        return _maybe_assist(conn, user, text, character_id)
    except Exception:
        return None


def _maybe_assist(conn: sqlite3.Connection, user: dict, text: str,
                  character_id: str) -> dict | None:
    user_id = user.get("user_id", "")
    tier = user.get("tier") or "free"
    if not enabled_for(user_id, tier):
        return None
    caps = caps_for(tier)
    intent, args = detect_intent(text)
    if not intent:
        return None

    if intent == "web_search":
        if not use_search(conn, user_id, caps):
            return {"brief": "He asked you to look something up but he's "
                             "out of lookups for today. Tell him sweetly "
                             "you'll check first thing tomorrow."}
        res = web_search(args["query"])
        if not res:
            return {"brief": "You tried to look something up for him but "
                             "couldn't pull it up. Say so honestly, short."}
        return {"brief": "You just looked this up for him — answer: "
                         f"{res['answer']}"}

    if intent == "reminder":
        if active_reminder_count(conn, user_id) >= caps["reminders"]:
            return {"brief": "He asked for a reminder but his reminder list "
                             "is full. Tell him to clear one first."}
        remind_at, rtext = parse_reminder(text)
        if not remind_at:
            return {"brief": "He asked you to remind him but didn't say "
                             "when. Ask him when, sweetly."}
        set_reminder(conn, user_id, character_id, rtext, remind_at)
        when_local = datetime.strptime(
            remind_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).astimezone(_TZ)
        pretty = when_local.strftime("%A at %-I:%M %p")
        return {"brief": f"Reminder SET for {pretty}: \"{rtext}\". "
                         "Confirm it in your own words, short and warm."}

    if intent == "email_draft":
        if not caps["email_drafts"]:
            return {"brief": "He asked you to draft an email, but that's a "
                             "Plus/VIP thing and he's on basic. Tell him "
                             "sweetly it's one of the Plus perks."}
        draft = compose_draft(text)
        if not draft or not draft.get("body"):
            return {"brief": "You tried to draft his email but the words "
                             "wouldn't come. Ask him for a little more "
                             "detail."}
        return {"brief": "You just wrote his email draft"
                         f"{' to ' + draft['to'] if draft.get('to') else ''}"
                         f" (subject: {draft.get('subject') or 'no subject yet'}). "
                         "Tell him it's ready — if his email isn't connected "
                         "in the app, he'll need to connect it first, then "
                         "you'll drop the draft in his Gmail.",
                "email_draft": draft}

    if intent == "job_search":
        if not caps["job_lookup"]:
            return {"brief": "He asked about jobs, but job hunting help is a "
                             "Plus/VIP thing and he's on basic. Tell him "
                             "sweetly."}
        if not use_search(conn, user_id, caps):
            return {"brief": "He asked about jobs but he's out of lookups "
                             "for today. Tell him sweetly."}
        res = job_search(args["query"])
        if not res or not res.get("answer"):
            return {"brief": "You looked for jobs for him but came up "
                             "empty. Say so honestly and ask what to "
                             "narrow."}
        return {"brief": "You just hunted jobs for him — results: "
                         f"{res['answer']}"}

    return None
