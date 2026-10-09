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

  ONE NAMED EXCEPTION (2026-10-08, user-directed): the OWNER's own GitHub
  token for his repo reviews. When the owner pastes one in chat, it is
  stored raw server-side (assist_secrets row "github_token") so her
  review job can read his repos, and it is scrubbed out of the persisted
  chat history. The raw value is NEVER echoed in any reply, prompt,
  endpoint response or log line — no fragments, not even the last 4.
  Everything else credential-shaped stays refused, exactly as above.
"""

from __future__ import annotations

import base64
import hmac
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db, gemini_keys, store

# ---------------------------------------------------------------------------
# Tier capability matrix — higher tier, more she can do.
# web_search: lookups per day. reminders: max active reminders.
# email_drafts / job_lookup: feature flags.
# ---------------------------------------------------------------------------
# Job lookup is on every paid tier (2026-10-01, user-directed): someone
# hunting for work shouldn't have to pay extra for the thing that helps
# them afford us. Email drafts stay Plus/VIP.
CAPS = {
    "basic": {"web_search": 20, "reminders": 10, "email_drafts": False,
              "job_lookup": True},
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


def effective_caps(user_id: str, tier: str) -> dict:
    """Caps actually in force for this user.

    Owner test override (2026-10-01): while the rollout is owner-only,
    owners get plus-level caps so every capability (email drafts, job
    lookup) can be exercised on a basic account. Revisit when the paid
    rollout begins.
    """
    if user_id in _owner_ids():
        return caps_for("plus")
    return caps_for(tier)


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
    conn.execute(
        """CREATE TABLE IF NOT EXISTS image_usage (
               user_id TEXT NOT NULL,
               day TEXT NOT NULL,
               attempts INTEGER NOT NULL DEFAULT 0,
               last_epoch REAL NOT NULL DEFAULT 0,
               PRIMARY KEY (user_id, day))"""
    )
    # Hannah studio (2026-10-09): per-owner conversational state for the
    # give-adding and character-creation abilities — the last image she
    # generated for him, a give waiting on his preview-approval, a
    # character creation mid-collection, and the character created most
    # recently. One row per (owner, kind); state is JSON.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hannah_studio (
               user_id TEXT NOT NULL,
               kind TEXT NOT NULL,
               state TEXT NOT NULL DEFAULT '{}',
               updated_at REAL NOT NULL DEFAULT 0,
               PRIMARY KEY (user_id, kind))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS assist_secrets (
               name TEXT PRIMARY KEY,
               value TEXT NOT NULL,
               updated_at TEXT)"""
    )
    # Owner chat attachments for Hannah (2026-10-09). Files live under
    # GENERATED_DIR/hannah-uploads and are served via /generated/...;
    # this table keeps ownership, message binding and extracted text.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chat_attachments (
               id TEXT PRIMARY KEY,
               user_id TEXT NOT NULL,
               character_id TEXT NOT NULL DEFAULT 'hannah',
               filename TEXT NOT NULL,
               mime_type TEXT NOT NULL,
               kind TEXT NOT NULL,
               size INTEGER NOT NULL,
               url TEXT NOT NULL,
               storage_name TEXT NOT NULL,
               extracted_text TEXT NOT NULL DEFAULT '',
               extraction_status TEXT NOT NULL DEFAULT '',
               extracted_chars INTEGER NOT NULL DEFAULT 0,
               truncated INTEGER NOT NULL DEFAULT 0,
               conversation_id INTEGER,
               created_at TEXT NOT NULL,
               used_at TEXT)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_attachments_user "
        "ON chat_attachments (user_id, character_id, created_at)"
    )
    # Dev-agent bridge (2026-10-08): owner build jobs Hannah queues for
    # the dev agent to claim over the authenticated poll endpoint.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dev_jobs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id TEXT NOT NULL,
               character_id TEXT NOT NULL DEFAULT 'hannah',
               repo TEXT NOT NULL,
               task TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'queued',
               branch TEXT,
               summary TEXT,
               detail TEXT,
               report TEXT,
               created_at TEXT NOT NULL,
               claimed_at TEXT,
               finished_at TEXT,
               merge INTEGER NOT NULL DEFAULT 0,
               merge_branch TEXT,
               merged_sha TEXT,
               delivered INTEGER NOT NULL DEFAULT 0)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dev_jobs_status "
        "ON dev_jobs (status, id)"
    )
    # Merge grant (2026-10-08, owner: "let her merge and push"): rows
    # queued before the grant predate these columns.
    dev_cols = {r[1] for r in conn.execute("PRAGMA table_info(dev_jobs)")}
    for col, ddl in (("merge", "INTEGER NOT NULL DEFAULT 0"),
                     ("merge_branch", "TEXT"),
                     ("merged_sha", "TEXT")):
        if col not in dev_cols:
            conn.execute(f"ALTER TABLE dev_jobs ADD COLUMN {col} {ddl}")
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
    # Key rotation (app/gemini_keys.py): free keys first, paid fallback,
    # transparent failover. None when the pool is exhausted.
    return gemini_keys.post_json(_GEMINI_URL, payload, timeout=timeout,
                                     source="whispr-search")


_REDIRECT_HOST = "vertexaisearch.cloud.google.com/grounding-api-redirect/"


def _resolve_grounding_url(url: str, timeout: int = 5) -> str:
    """Resolve a Gemini grounding redirect link to its real destination.

    The API returns vertexaisearch.cloud.google.com/grounding-api-redirect/
    links with long opaque tokens. If she retypes one from memory she will
    corrupt it (seen: user got a Google 404). A short real URL like
    https://www.indeed.com/viewjob?jk=... survives copying. Manual HEAD via
    http.client so we read the Location header without following it.
    Falls back to the original URL on any failure.
    """
    if _REDIRECT_HOST not in (url or ""):
        return url
    try:
        parts = urllib.parse.urlsplit(url)
        conn_cls = (http.client.HTTPSConnection
                    if parts.scheme == "https" else http.client.HTTPConnection)
        conn = conn_cls(parts.hostname,
                        parts.port or (443 if parts.scheme == "https" else 80),
                        timeout=timeout)
        path = parts.path + ("?" + parts.query if parts.query else "")
        conn.request("HEAD", path,
                     headers={"User-Agent": "Mozilla/5.0",
                              "Host": parts.hostname})
        loc = conn.getresponse().getheader("Location")
        conn.close()
        if loc:
            real = urllib.parse.urljoin(url, loc)
            print(f"[assist] resolved grounding link -> "
                  f"{urllib.parse.urlsplit(real).netloc}", flush=True)
            return real
    except Exception as e:
        print(f"[assist] grounding resolve failed: {type(e).__name__}",
              flush=True)
    return url


def _grounding_sources(data: dict, limit: int = 4) -> list[dict]:
    """Extract [{title, url}] from grounding chunks, resolving redirects."""
    out: list[dict] = []
    try:
        chunks = (data["candidates"][0].get("groundingMetadata", {})
                  .get("groundingChunks") or [])
    except Exception:
        return out
    for chunk in chunks:
        web = chunk.get("web") or {}
        uri = web.get("uri")
        if uri and all(u != uri for u in (s["url"] for s in out)):
            out.append({"title": web.get("title") or uri,
                        "url": _resolve_grounding_url(uri)})
        if len(out) >= limit:
            break
    return out


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
        if not answer:
            return None
        return {"answer": answer, "sources": _grounding_sources(data)}
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
        if not answer:
            return None
        return {"answer": answer, "sources": _grounding_sources(data)}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Owner-only power tools (2026-10-08, user-directed): a REAL GitHub code
# review and grounded marketing help. Both are gated by an explicit owner
# check in _maybe_assist — independent of ASSIST_ROLLOUT, so they never
# open up to customers even if the general assist rollout widens, and they
# are never mentioned to non-owners. When a tool actually runs, its brief
# is what she's allowed to claim (brain.HONESTY_RULE covers the rest);
# when it can't run, she says so instead of inventing results.
# ---------------------------------------------------------------------------
_GH_API = "https://api.github.com"
_GH_OWNER = "mtthewwestfall"

_GH_REVIEW_RE = re.compile(
    r"\b(look at|review|audit|check|inspect|scan|dig into|go through|"
    r"poke through)\b.{0,80}?\b(repo|repository|github|codebase|code)\b",
    re.IGNORECASE | re.DOTALL)
_GH_FINDBAD_RE = re.compile(
    r"\bfind\b.{0,60}?\b(bad code|bugs?|bug|issues?|problems?|smells?|"
    r"vulnerabilit\w+)\b",
    re.IGNORECASE | re.DOTALL)
_GH_CODECTX_RE = re.compile(r"\b(repo|repository|github|codebase|code)\b",
                            re.IGNORECASE)
_GH_FULLNAME_RE = re.compile(r"\b([A-Za-z0-9][\w-]*)/([A-Za-z0-9][\w.-]*)\b")
_GH_NAME_BEFORE_REPO_RE = re.compile(
    r"\b([A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*)?)\s+"
    r"(?:repo|repository|codebase)\b", re.IGNORECASE)
_GH_AFTER_REPO_RE = re.compile(
    r"\b(?:repo|repository|codebase)(?:\s+(?:called|named))?\s+"
    r"([A-Za-z0-9][\w.-]*)", re.IGNORECASE)
_GH_IN_RE = re.compile(r"\bin\s+(?:my\s+)?([A-Za-z][\w.-]{1,60})\b",
                       re.IGNORECASE)
_GH_STOPWORDS = {"the", "my", "this", "that", "your", "his", "her", "their",
                 "our", "it", "there", "here", "bed", "trouble"}

# His real repos under mtthewwestfall (canonical GitHub names). Chat
# arrives squashed or spaced — "roofscout", "roof scout" — and the
# queue takes the name verbatim, so a spoken squash 404s on GitHub and
# the job blocks (live 2026-10-08: his storage ask queued the
# nonexistent "roofscout"). Canonicalize ONLY on an exact squash match
# (name with separators stripped) against this table; anything else
# passes through untouched — never a guessed repo.
_GH_KNOWN_REPOS = (
    "roof-scout", "chloe-companion", "dev-agent", "Sorority-house",
    "Creator-bot-telegram", "creator-bot", "Keyhole-site",
    "Thoughtspace", "Telegram-Bot", "gods-town", "OntheFarm",
    "gemini-key-manager", "the-devinatorbot", "bot-reverse-engineer",
    "mia-bot", "snaptap", "Godprint", "godtown2", "castagne",
)


def _repo_squash(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


_GH_REPO_ALIASES = {_repo_squash(n): n for n in _GH_KNOWN_REPOS}


def _canonical_repo(name: str | None) -> str | None:
    """Real repo name for a chatted one, or the name unchanged.

    Exact squash match against _GH_KNOWN_REPOS ("roofscout" ->
    "roof-scout"); no match returns the input byte-identical, so an
    unknown name still surfaces as itself (queue -> GitHub says no ->
    honest blocked reply), never as a silently guessed repo."""
    if not name:
        return name
    return _GH_REPO_ALIASES.get(_repo_squash(name), name)


def _extract_repo_raw(text: str) -> str | None:
    """Best-effort repo name out of a chat sentence ('roof scout repo'
    -> 'roof-scout'). None when he didn't name one."""
    t = text or ""
    m = _GH_FULLNAME_RE.search(t)
    if m and "." not in m.group(1):
        return m.group(2)
    m = _GH_NAME_BEFORE_REPO_RE.search(t)
    if m:
        words = [w for w in m.group(1).strip().split()
                 if w.lower() not in _GH_STOPWORDS]
        if words:
            return "-".join(words)
    m = _GH_AFTER_REPO_RE.search(t)
    if m and m.group(1).lower() not in _GH_STOPWORDS:
        return m.group(1)
    m = _GH_IN_RE.search(t)
    if m and m.group(1).lower() not in _GH_STOPWORDS:
        return m.group(1)
    return None


def _extract_repo(text: str) -> str | None:
    """Extracted name, canonicalized — see _canonical_repo."""
    return _canonical_repo(_extract_repo_raw(text))


def detect_github_review(text: str) -> dict | None:
    """Does he want a real look at one of his repos? Narrow on purpose:
    a miss just means normal chat, never a fake review."""
    t = (text or "").strip()
    if not t:
        return None
    if _GH_REVIEW_RE.search(t):
        return {"repo": _extract_repo(t)}
    if _GH_FINDBAD_RE.search(t) and _GH_CODECTX_RE.search(t):
        return {"repo": _extract_repo(t)}
    # A full owner/repo mention next to a review-ish verb ("look at
    # mtthewwestfall/roof-scout, what's broken") is intent too.
    m = _GH_FULLNAME_RE.search(t)
    if m and "." not in m.group(1) and re.search(
            r"\b(look|check|review|audit|scan|find|inspect|dig|broken|"
            r"wrong)\b", t, re.IGNORECASE):
        return {"repo": _canonical_repo(m.group(2))}
    return None


# ---------------------------------------------------------------------------
# Owner continuity brief + live app status (2026-10-08, user-directed):
# when Rocky is unavailable, Hannah is his cover. The brief below is
# standing FACTS (prices, where things live, the rules) — never live
# numbers. Live numbers come only from fetch_app_status(), fetched at
# ask-time from his own public endpoints + the in-process Gemini pool.
# Anything unreachable is reported as unreachable — never invented.
# ---------------------------------------------------------------------------
_OWNER_BRIEF = """HIS PRODUCTS — STANDING FACTS (use ONLY these; for
anything not covered, ask him — never invent):
- OneFetch (realtor app, houses not roofs): listing-launch kits,
  open-house sign-ins with follow-ups, FSBO radar. Live at
  realtor-app-production-825b.up.railway.app (public site /site, app
  token-gated). Pricing: first 200 Founders $19.99/mo locked for
  life, then Standard $29.99/mo. Stripe checkout + webhook are live;
  the tryonefetch.com domain is NOT bought yet. Fetch the golden
  retriever is the mascot.
- Roof Rover (tryroofrover.com): aerial roof scans -> graded leads
  for roofers; public store sells exclusive leads at $4.99 each.
  Winston (EstimatorPRO) is the partner-API customer: WE charge him
  $3.00 per billed lead (grades 1-3), first 10 scan jobs free.
  Bailey is Roof Rover's on-screen assistant; brand wall: no Whispr
  branding inside Roof Rover.
- Whispr AI: this companion app, $9.99/mo (see the Whispr brief).
- His builder: when he asks Hannah to build/fix code, she queues a
  dev-agent job. The builder works on agent/* branches by default.
  Only when he explicitly tells her to merge/push/ship does the job
  carry a merge request; the builder then merges to main only after
  checks pass, only for allowlisted repos, fast-forward only (a
  moved main blocks the merge — never forced). Merging to main on a
  Railway-connected repo means a production deploy follows. She
  reports the merged commit or the blocked reason back in chat.
- AI: every app shares one free Gemini key pool through the
  companion proxy (gemini-3.1-flash-lite). If the pool is saturated,
  apps wait or use honestly-labeled template fallbacks — his cost
  rule is free-first, always.
- Rocky is his main Muse agent; Hannah covers for him in chat:
  builds via the dev agent, code reviews, live app status, and
  marketing copy. She never claims she did something she didn't."""

_STATUS_RE = re.compile(
    r"\b(app status|status of (my|the|his) apps?|how('s| is| are) "
    r"(onefetch|roof rover|whispr|my apps?|everything|things|the "
    r"(apps?|pool|store|shelf))|check (on )?(my|the) apps?|founder "
    r"(count|spots)|spots left|key pool|gemini pool|pool health|"
    r"store shelf|shelf count)\b", re.IGNORECASE)

_BRIEF_RE = re.compile(
    r"\b(catch me up|brief me|what are (my|the) (prices|products)|"
    r"what do you know about (my|the|his) (business|products|apps)|"
    r"remind me what (we|i) (charge|sell|built))\b", re.IGNORECASE)

_ONEFETCH_BASE = "https://realtor-app-production-825b.up.railway.app"
_ROOFROVER_BASE = "https://tryroofrover.com"


def detect_app_status(text: str) -> bool:
    return bool(_STATUS_RE.search(text or ""))


def detect_owner_brief(text: str) -> bool:
    return bool(_BRIEF_RE.search(text or ""))


def _http_get_json(url: str, timeout: int = 6):
    """(status, json) or (None, None) — status fetches must fail soft."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "hannah-status/1",
                      "Connection": "keep-alive"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(
                resp.read().decode("utf-8", "ignore"))
    except Exception:
        return None, None


def _http_status(url: str, timeout: int = 6):
    req = urllib.request.Request(
        url, headers={"User-Agent": "hannah-status/1",
                      "Connection": "keep-alive"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(64)
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def fetch_app_status() -> dict:
    """Live numbers for the owner's apps. Every source is independent:
    one being down never blocks or poisons the others."""
    out: dict = {}
    one: dict = {}
    _, pricing = _http_get_json(_ONEFETCH_BASE + "/api/pricing")
    if isinstance(pricing, dict):
        one["founder_count"] = pricing.get("founder_count")
        one["founder_spots_left"] = pricing.get("founder_spots_left")
        one["founder_open"] = pricing.get("founder_open")
    _, ai = _http_get_json(_ONEFETCH_BASE + "/api/ai/status")
    if isinstance(ai, dict):
        one["ai_available"] = ai.get("available")
    one["site_status"] = _http_status(_ONEFETCH_BASE + "/site")
    out["onefetch"] = one

    roof: dict = {}
    _, leads = _http_get_json(_ROOFROVER_BASE + "/api/store/leads")
    if isinstance(leads, list):
        roof["shelf_leads"] = len(leads)
    elif isinstance(leads, dict) and isinstance(leads.get("leads"), list):
        roof["shelf_leads"] = len(leads["leads"])
    roof["site_status"] = _http_status(_ROOFROVER_BASE + "/")
    out["roof_rover"] = roof

    try:
        info = gemini_keys.degradation_info()
        pool = info.get("pool") or {}
        out["gemini"] = {
            "believed_healthy": len(info.get("believed_healthy_key_ids")
                                    or []),
            "pool": pool,
            "degraded": bool(info.get("degraded")),
        }
    except Exception:
        out["gemini"] = {}
    return out


def _status_lines(s: dict) -> list[str]:
    lines = []
    one = s.get("onefetch") or {}
    if one.get("founder_count") is not None:
        signup = "open" if one.get("founder_open") else "CLOSED"
        ai_txt = ("reachable" if one.get("ai_available")
                  else "NOT reachable (template mode)")
        site_txt = (f", site HTTP {one['site_status']}"
                    if one.get("site_status") else ", site UNREACHABLE")
        lines.append(
            f"OneFetch: {one['founder_count']} founders, "
            f"{one.get('founder_spots_left')} of 200 founder spots "
            f"left, founder signup {signup}, AI writing {ai_txt}"
            + site_txt)
    else:
        lines.append("OneFetch: UNREACHABLE — you could not pull its "
                     "numbers just now.")
    roof = s.get("roof_rover") or {}
    if roof.get("shelf_leads") is not None:
        lines.append(
            f"Roof Rover: {roof['shelf_leads']} leads on the $4.99 "
            f"store shelf"
            + (f", site HTTP {roof['site_status']}"
               if roof.get("site_status") else ", site UNREACHABLE"))
    else:
        lines.append("Roof Rover: UNREACHABLE — you could not pull "
                     "its store shelf just now.")
    gem = s.get("gemini") or {}
    if gem:
        pool = gem.get("pool") or {}
        pool_txt = ", ".join(f"{k}: {v}" for k, v in sorted(
            pool.items())) or "no key rows reported"
        lines.append(
            f"Gemini key pool: {gem.get('believed_healthy', 0)} keys "
            f"believed healthy ({pool_txt})"
            + (", pool is DEGRADED (running on reserve keys)"
               if gem.get("degraded") else ""))
    else:
        lines.append("Gemini key pool: you could not read the pool "
                     "health just now.")
    return lines


def _status_brief() -> str:
    lines = _status_lines(fetch_app_status())
    return ("You just checked his live app numbers yourself, right "
            "now. Report ONLY the numbers in the LIVE block below, "
            "in your own voice, short and plain. Any line that says "
            "UNREACHABLE means say exactly that — never guess, round "
            "or fill in a number you don't have. The standing facts "
            "after it are context for his follow-up questions, not "
            "numbers to read out unless he asks.\n\nLIVE NOW:\n"
            + "\n".join("- " + ln for ln in lines)
            + "\n\n" + _OWNER_BRIEF)


def _owner_brief_reply() -> str:
    return ("Standing facts he expects you to know cold. Answer his "
            "question from ONLY these — if it's not covered, ask him "
            "instead of inventing. For live numbers (counts, spots, "
            "pool health) he'd ask you for status; these facts don't "
            "change.\n\n" + _OWNER_BRIEF)


# ---------------------------------------------------------------------------
# Marketing help — static, verified product facts. She produces the copy
# in chat (that part is real); distribution never happens here.
# ---------------------------------------------------------------------------
_MARKETING_RE = re.compile(
    r"\b(market|marketing|promo|promotion|promote|advertis\w*|caption|"
    r"ad\s+copy|hook|slogan|tagline|sales\s+(copy|pitch))\b",
    re.IGNORECASE)

_WHISPR_BRIEF = """WHISPR AI — VERIFIED FACTS (use ONLY these; anything not
covered here, ask him — never invent):
- Whispr AI: AI companions at companion.lockeddoor.ai. Six personalities
  (Chloe, Mara, Isabella, Regan, Jake, Eli), plus a creator to build your
  own companion.
- One plan: $9.99/month — 3,000 messages, 150 voice notes, unlimited give
  presets, 1 special media request a month, photo drops, proactive texts
  (she texts him first). Trial: $0.99 for 100 messages.
- The companions are AI — copy never claims they are human or real people.
- Labels must be literally true: no invented discounts, bundles or sales."""

_ROOF_ROVER_BRIEF = """ROOF ROVER — VERIFIED FACTS (use ONLY these;
anything not covered here, ask him — never invent):
- Roof Rover (tryroofrover.com): scans a ZIP code's roofs from aerial
  imagery and gives roofing companies graded leads with addresses and
  plain-language damage notes.
- Working angle: "roofing leads $2 instead of $80."
- Trial: first ZIP scan free plus 5 lead unlocks, no card needed.
- Bailey is Roof Rover's on-screen assistant. Brand boundary: Roof Rover
  copy never carries Whispr/companion links or branding, and Whispr copy
  never sells Roof Rover."""


def detect_marketing(text: str) -> dict | None:
    t = (text or "").strip()
    if not t or not _MARKETING_RE.search(t):
        return None
    if re.search(r"\broof|rover\b", t, re.IGNORECASE):
        return {"product": "roof_rover"}
    return {"product": "whispr"}


def _marketing_brief(product: str) -> str:
    facts = _ROOF_ROVER_BRIEF if product == "roof_rover" else _WHISPR_BRIEF
    return (
        "He's asking for marketing help. This is real work you do right "
        "HERE in the chat: produce the actual copy, hook, caption or plan "
        "he's asking for, now, in your own voice, using ONLY the verified "
        "facts below. Never say or imply anything was posted, sent, "
        "emailed or published — distribution is HIS call, after he's "
        "seen it. If he asks whether you posted or sent it: you did "
        "not.\n\n" + facts)


# ---------------------------------------------------------------------------
# Owner GitHub token intake (2026-10-08, user-directed): the owner can
# hand Hannah a GitHub token right in chat. She stores it server-side
# (assist_secrets) and her review job reads it from there; the env var
# GITHUB_TOKEN stays as the fallback. The turn he pasted it in is
# scrubbed in the persisted history, and the confirmation is FIXED text
# the model never rephrases (see FIXED_REPLY_PREFIX in brain.py), so no
# model output can ever quote the key back at him.
# ---------------------------------------------------------------------------
_GH_TOKEN_RE = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b")
_GH_TOKEN_PLACEHOLDER = "[github token]"
_TOKEN_REVOKE_RE = re.compile(
    r"\b(forget|delete|remove|revoke|wipe|drop)\b[^.!?]{0,20}"
    r"\b(the\s+|my\s+)?(github\s+)?(token|key)\b"
    r"|\b(forget|revoke)\s+(the\s+|my\s+)?(github\s+)?(token|key)\b",
    re.IGNORECASE)
# Must stay byte-identical with brain.FIXED_REPLY_PREFIX — the chat path
# copies maybe_assist's "brief" into context["assist_result"], and
# brain.compose_reply speaks a brief starting with this prefix verbatim
# instead of calling the model.
FIXED_REPLY_PREFIX = "FIXED_REPLY:: "
_TOKEN_SAVED_REPLY = (
    "saved, babe — I put that key away safe on my end. next time you "
    "ask me to dig through one of your repos, that’s the one I’ll "
    "read with.")
_TOKEN_REVOKED_REPLY = (
    "done — wiped that key clean off my side. next repo you have me "
    "read, I’ll use the server’s own key again.")
_TOKEN_NOTHING_REPLY = (
    "nothing to wipe, babe — there’s no key saved on my end right "
    "now.")


def extract_github_token(text: str) -> str | None:
    """The GitHub-token-shaped string in a message, else None."""
    m = _GH_TOKEN_RE.search(text or "")
    return m.group(0) if m else None


def sanitize_token_text(text: str) -> str:
    """Message text with any token-shaped string masked out."""
    return _GH_TOKEN_RE.sub(_GH_TOKEN_PLACEHOLDER, text or "")


def store_github_token(conn: sqlite3.Connection, value: str) -> None:
    """Save (or rotate) the owner's GitHub token. Value never logged."""
    ensure_tables(conn)
    conn.execute(
        "INSERT INTO assist_secrets (name, value, updated_at) "
        "VALUES ('github_token', ?, ?) "
        "ON CONFLICT (name) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (value,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    conn.commit()


def stored_github_token(conn: sqlite3.Connection) -> str:
    """The stored token, or \"\" when none is saved."""
    ensure_tables(conn)
    row = conn.execute(
        "SELECT value FROM assist_secrets WHERE name = 'github_token'"
    ).fetchone()
    return (row[0] or "").strip() if row else ""


def clear_github_token(conn: sqlite3.Connection) -> bool:
    """Delete the stored token. True when one was actually there."""
    ensure_tables(conn)
    cur = conn.execute(
        "DELETE FROM assist_secrets WHERE name = 'github_token'")
    conn.commit()
    return cur.rowcount > 0


def resolve_github_token(conn: sqlite3.Connection) -> str:
    """Her review key: the stored one wins; else the server env var."""
    stored = stored_github_token(conn)
    if stored:
        return stored
    return (os.environ.get("GITHUB_TOKEN") or "").strip()


def scrub_token_from_history(conn: sqlite3.Connection, user_id: str,
                             character_id: str, token: str) -> None:
    """Mask the token out of his just-saved chat turn.

    The chat path logs his raw message before maybe_assist runs, so the
    row already exists — rewrite it (and only it) in place. A no-op
    when the caller already persisted sanitized text.
    """
    if not token:
        return
    row = conn.execute(
        "SELECT id, text FROM conversation WHERE user_id = ? "
        "AND character_id = ? AND role = 'user' AND instr(text, ?) > 0 "
        "ORDER BY id DESC LIMIT 1",
        (user_id, character_id, token)).fetchone()
    if not row:
        return
    conn.execute(
        "UPDATE conversation SET text = ? WHERE id = ?",
        (sanitize_token_text(row[1]), row[0]))
    conn.commit()


def _handle_github_token_intake(conn: sqlite3.Connection, user_id: str,
                                character_id: str, token: str) -> dict:
    store_github_token(conn, token)
    scrub_token_from_history(conn, user_id, character_id, token)
    return {"brief": FIXED_REPLY_PREFIX + _TOKEN_SAVED_REPLY}


def _handle_github_token_revoke(conn: sqlite3.Connection, user_id: str,
                                character_id: str,
                                token: str | None) -> dict:
    had = clear_github_token(conn)
    if token:
        # "forget this token ghp_…" — scrub the paste too; store nothing.
        scrub_token_from_history(conn, user_id, character_id, token)
    reply = _TOKEN_REVOKED_REPLY if had else _TOKEN_NOTHING_REPLY
    return {"brief": FIXED_REPLY_PREFIX + reply}


# ---------------------------------------------------------------------------
# Dev-agent bridge (2026-10-08, user-directed): Hannah is the front door
# to the owner's dev agent. The owner asks her to build/fix something in
# chat; a row lands in dev_jobs; the dev agent POLLS this server over an
# authenticated endpoint (shared secret), claims the job, builds it on
# an agent/* branch with its own unchanged safety gates, and posts the
# real result back. She then delivers the outcome as a follow-up chat
# message (store.log_turn, same primitive as review findings). She
# builds nothing herself, promises nothing, and never invents outcomes
# (HONESTY_RULE). Owner-only like the other power tools — the check is
# an explicit ASSIST_OWNER_IDS membership test in _maybe_assist,
# independent of ASSIST_ROLLOUT, so customers never see this.
# ---------------------------------------------------------------------------
_DEV_BUILD_RE = re.compile(
    r"\b(fix|build|implement|add|change|update|create|write|refactor|"
    r"patch|repair)\b", re.IGNORECASE)
_DEV_STRONG_RE = re.compile(
    r"\b(fix|implement|refactor|patch|debug)\b", re.IGNORECASE)
_DEV_BOT_RE = re.compile(
    r"\b(dev\s*-?\s*(bot|agent|builder)|builder\s*bot|build\s*agent|"
    r"coding\s*(bot|agent))\b", re.IGNORECASE)
_DEV_REPOISH_RE = re.compile(
    r"\b(repo|repository|codebase|github)\b", re.IGNORECASE)
_DEV_SECRET_GUARD_RE = re.compile(
    r"\b(token|secret|password|api\s*key)\b", re.IGNORECASE)
_DEV_MAX_ACTIVE = 3


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_DEV_FILE_EXT_RE = re.compile(
    r"\.(py|js|ts|tsx|jsx|html|css|json|md|txt|sh|yml|yaml|toml|lock)$")


def _extract_dev_repo(t: str) -> str | None:
    """Repo for a build ask, or None.

    Deliberately NOT the review extractor: for build asks an
    "owner/name" slash match is tried last and rejected when its tail
    looks like a filename — "devagent/report.py" is a file path inside
    a repo, never a repo (first live probe misqueued exactly that)."""
    m = _GH_NAME_BEFORE_REPO_RE.search(t)
    if m:
        words = [w for w in m.group(1).strip().split()
                 if w.lower() not in _GH_STOPWORDS]
        if words:
            return "-".join(words)
    m = _GH_AFTER_REPO_RE.search(t)
    if m and m.group(1).lower() not in _GH_STOPWORDS:
        return m.group(1)
    m = _GH_FULLNAME_RE.search(t)
    if m and "." not in m.group(1) \
            and not _DEV_FILE_EXT_RE.search(m.group(2).lower()):
        return m.group(2)
    for m in _GH_IN_RE.finditer(t):
        if m.group(1).lower() in _GH_STOPWORDS:
            continue
        if m.end() < len(t) and t[m.end()] == "/":
            continue  # "in devagent/report.py" is a path, not a repo
        return m.group(1)
    return None


# Merge intent (2026-10-08, owner: "let her merge and push"). A merge
# is requested ONLY by an explicit merge/push/ship/deploy word — never
# inferred from a build ask alone, and never by the builder on its own.
# "fix the deploy script" names a thing to fix, not a ship order, so a
# merge word immediately followed by a build-noun doesn't count.
_DEV_MERGE_VERB_RE = re.compile(
    r"\b(merge|merges|merging|merged|push|pushes|pushing|pushed|"
    r"ship|ships|shipping|shipped|deploy|deploys|deploying|deployed)\b",
    re.IGNORECASE)
_DEV_MERGE_NOUN_RE = re.compile(
    r"\b(merge|push|ship|deploy)\s+(script|pipeline|config|file|"
    r"key|command|step|docs?)\b", re.IGNORECASE)
_DEV_MERGE_OBJ_RE = re.compile(
    r"\b(merge|push|ship|deploy)\w*\s+"
    r"(it|that|this|them|the\s+\w*\s*branch|my\s+branch|"
    r"to\s+(main|prod(uction)?))\b", re.IGNORECASE)
_DEV_MERGE_TOMAIN_RE = re.compile(
    r"\b(to|into|onto)\s+(main|prod(uction)?)\b", re.IGNORECASE)


def detect_merge_ask(text: str) -> bool:
    """Explicit owner order to merge/push/ship. The merge word must be
    there AND point at something ('merge it', 'push to main', 'ship
    that branch', or a bare imperative like 'merge!'). Talking ABOUT a
    merge ('the merge conflict ... this week') is not an order, and
    'fix the deploy script' names a thing to fix. Without an explicit
    merge word, builds stay branch-only — that default does not move."""
    t = (text or "").strip()
    if not t or _DEV_SECRET_GUARD_RE.search(t):
        return False
    if not _DEV_MERGE_VERB_RE.search(t):
        return False
    if _DEV_MERGE_NOUN_RE.search(t):
        return False
    if _DEV_MERGE_OBJ_RE.search(t):
        return True
    if _DEV_MERGE_TOMAIN_RE.search(t):
        return True
    if t.endswith("?"):
        return False
    return len(t.split()) <= 4


def detect_dev_build(text: str) -> dict | None:
    """Owner wants the dev builder to change one of his repos.

    Conservative on purpose: needs an actual change verb AND a target
    (a named repo, an explicit dev-bot mention, or repo-ish framing).
    A miss just means normal chat — never a fake build. Returns
    {"repo": name|None, "task": full ask, "merge": bool} — repo None
    means he didn't name it, and the handler asks him one clarifying
    question. merge is True only on his explicit merge/push/ship word.
    """
    t = (text or "").strip()
    if not t or not _DEV_BUILD_RE.search(t):
        return None
    # Credential-shaped asks belong to the token flow, never the queue.
    if _DEV_SECRET_GUARD_RE.search(t):
        return None
    repo = _extract_dev_repo(t)
    if repo is not None:
        repo = repo.strip().strip("/").lower()
        repo = _canonical_repo(repo)
        if (not re.fullmatch(r"[a-z0-9][\w.-]{0,99}", repo)
                or repo in _GH_STOPWORDS or repo in ("repo", "github")):
            repo = None
    if repo or _DEV_BOT_RE.search(t) or _DEV_REPOISH_RE.search(t):
        return {"repo": repo, "task": t[:1000],
                "merge": detect_merge_ask(t)}
    return None


def enqueue_dev_job(conn: sqlite3.Connection, user_id: str,
                    character_id: str, repo: str, task: str,
                    merge: bool = False,
                    merge_branch: str | None = None) -> int:
    ensure_tables(conn)
    cur = conn.execute(
        "INSERT INTO dev_jobs (user_id, character_id, repo, task, "
        "status, created_at, merge, merge_branch) "
        "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
        (user_id, character_id, repo, task, _utc_iso(),
         1 if merge else 0, merge_branch))
    conn.commit()
    return int(cur.lastrowid)


def _dev_task_norm(task: str) -> str:
    return re.sub(r"\s+", " ", (task or "").strip().lower())


def active_dev_job(conn: sqlite3.Connection, user_id: str, repo: str,
                   task: str) -> dict | None:
    """An identical already-queued/claimed job for this owner, if any —
    so an accidental double-send never queues twice."""
    ensure_tables(conn)
    norm = _dev_task_norm(task)
    rows = conn.execute(
        "SELECT id, repo, task, merge FROM dev_jobs WHERE user_id = ? "
        "AND status IN ('queued', 'claimed') ORDER BY id",
        (user_id,)).fetchall()
    for r in rows:
        if r[1] == repo and _dev_task_norm(r[2]) == norm:
            return {"id": r[0], "repo": r[1], "task": r[2],
                    "merge": bool(r[3])}
    return None


def active_dev_job_count(conn: sqlite3.Connection, user_id: str) -> int:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT COUNT(*) FROM dev_jobs WHERE user_id = ? "
        "AND status IN ('queued', 'claimed')", (user_id,)).fetchone()
    return int(row[0] or 0)


def _handle_dev_build(conn: sqlite3.Connection, user_id: str,
                      character_id: str, dv: dict) -> dict:
    repo = dv.get("repo")
    if repo is None:
        return {"brief":
                "He's asking you to have something built or fixed for "
                "real, but he didn't say which repo the builder should "
                "work on. Ask him exactly ONE short question: which "
                "repo? Don't guess a name and don't list repos like "
                "you're reading from a menu."}
    repo_full = repo if "/" in repo else f"{_GH_OWNER}/{repo}"
    if not dev_agent_secret():
        return {"brief":
                f"He asked you to really build/fix something in his "
                f"repo '{repo}', but the bridge between you and your "
                "builder is not wired on your end right now, so you "
                "honestly cannot queue it. Tell him straight and "
                "short: the builder link is down on your side, nothing "
                "was queued, and you will not pretend otherwise."}
    existing = active_dev_job(conn, user_id, repo_full, dv["task"])
    if existing:
        extra = ""
        if dv.get("merge") and not existing.get("merge"):
            extra = (" He also asked for this one to be merged — the "
                     "queued job doesn't carry that yet, so tell him "
                     "once it lands he can just say 'merge it' and "
                     "you'll have the branch merged then.")
        return {"brief":
                f"He's asking for the same build you already queued "
                f"as job #{existing['id']} on his repo '{repo}' — it "
                "is still with the builder and hasn't reported back "
                f"yet.{extra} Tell him it's already in the builder's "
                "hands, no need to queue it twice. No new claims about "
                "its progress."}
    if active_dev_job_count(conn, user_id) >= _DEV_MAX_ACTIVE:
        return {"brief":
                "He wants another thing built, but you already have "
                f"three build jobs queued or running for him "
                f"({_DEV_MAX_ACTIVE} is your plate limit). Tell him "
                "sweetly the queue is full and to let those land "
                "first. Nothing new was queued."}
    jid = enqueue_dev_job(conn, user_id, character_id, repo_full,
                          dv["task"], merge=bool(dv.get("merge")))
    if dv.get("merge"):
        return {"brief":
                f"You just REALLY queued build job #{jid} for your dev "
                f"builder: repo '{repo_full}', task: "
                f"\"{dv['task'][:200]}\" — AND he explicitly asked for "
                "it to be merged to main, so the merge request is on "
                "the job. The builder only merges after its checks "
                "pass, only into repos cleared for main merges, and "
                "only if main hasn't moved meanwhile; merging to main "
                "means a production deploy follows on Railway-"
                "connected repos. You will bring him the REAL result "
                "here when it lands — merged commit or the plain "
                "reason it was blocked. Tell him you're on it, warm "
                "and brief. Promise NOTHING about outcome or timing; "
                "invent no file names, commits or progress you haven't "
                "been given."}
    return {"brief":
            f"You just REALLY queued build job #{jid} for your dev "
            f"builder: repo '{repo_full}', task: "
            f"\"{dv['task'][:200]}\". It is genuinely queued now — "
            "the builder picks jobs up by itself, works on its own "
            "branch (never main unless he explicitly tells you to "
            "merge, never deploys on its own), and you will bring "
            "him the real result here when it lands. Tell him "
            "you're on it, warm and brief. Promise NOTHING about the "
            "outcome or timing; invent no file names, branches or "
            "progress you haven't been given."}


def last_mergeable_job(conn: sqlite3.Connection, user_id: str,
                       repo_full: str | None = None) -> dict | None:
    """His most recent FINISHED builder job whose branch is not merged
    yet — what a bare 'merge it' refers to."""
    ensure_tables(conn)
    q = ("SELECT id, repo, task, branch FROM dev_jobs "
         "WHERE user_id = ? AND status = 'done' AND branch IS NOT NULL "
         "AND (merged_sha IS NULL OR merged_sha = '')")
    args: list = [user_id]
    if repo_full:
        q += " AND repo = ?"
        args.append(repo_full)
    q += " ORDER BY id DESC LIMIT 1"
    r = conn.execute(q, args).fetchone()
    if not r:
        return None
    return {"id": r[0], "repo": r[1], "task": r[2], "branch": r[3]}


def _handle_dev_merge(conn: sqlite3.Connection, user_id: str,
                      character_id: str, text: str) -> dict:
    """Owner said merge/push/ship with no new build attached: merge the
    branch of his latest finished, unmerged builder job. Same gates as
    everything else here — the queue row carries merge=1 + merge_branch
    and the WORKER enforces allowlist + fast-forward-only; she never
    merges anything herself."""
    if not dev_agent_secret():
        return {"brief":
                "He asked you to have a finished build merged to "
                "main, but the bridge between you and your builder is "
                "not wired on your end right now, so you honestly "
                "cannot queue it. Tell him straight and short: the "
                "builder link is down on your side, nothing was "
                "queued, and you will not pretend otherwise."}
    named = _extract_dev_repo(text)
    repo_full = None
    if named:
        named = _canonical_repo(named.strip().strip("/").lower())
        if named:
            repo_full = (named if "/" in named
                         else f"{_GH_OWNER}/{named}")
    src = last_mergeable_job(conn, user_id, repo_full)
    if src is None:
        scope = f" for repo '{repo_full}'" if repo_full else ""
        return {"brief":
                f"He asked you to merge something to main, but there "
                f"is no finished, unmerged builder job{scope} waiting "
                "— nothing has landed on a branch that still needs "
                "merging. Tell him plainly there's nothing to merge "
                "right now; if he names a build he means something "
                "else by, to say which one. Nothing was queued."}
    rows = conn.execute(
        "SELECT id FROM dev_jobs WHERE user_id = ? "
        "AND status IN ('queued', 'claimed') AND merge_branch = ?",
        (user_id, src["branch"])).fetchall()
    if rows:
        return {"brief":
                f"A merge of branch {src['branch']} is already with "
                f"the builder as job #{rows[0][0]}. Tell him it's "
                "already in the builder's hands and you'll bring him "
                "the real result when it lands — merged commit or the "
                "plain reason it was blocked. Nothing new was queued."}
    if active_dev_job_count(conn, user_id) >= _DEV_MAX_ACTIVE:
        return {"brief":
                "He wants a finished build merged, but you already "
                f"have three jobs queued or running for him "
                f"({_DEV_MAX_ACTIVE} is your plate limit). Tell him "
                "sweetly the queue is full and to let those land "
                "first. Nothing new was queued."}
    task = (f"Merge branch {src['branch']} into main (finished builder "
            f"job #{src['id']}: {src['task'][:80]})")
    jid = enqueue_dev_job(conn, user_id, character_id, src["repo"], task,
                          merge=True, merge_branch=src["branch"])
    return {"brief":
            f"You just REALLY queued merge job #{jid} for your dev "
            f"builder: merge branch {src['branch']} of "
            f"{src['repo']} into main. That branch already passed "
            "its checks when it was built; the builder merges only "
            "if the repo is cleared for main merges and only as a "
            "clean fast-forward — if main has moved it stops and "
            "says so, never forcing anything. Merging to main means "
            "a production deploy follows on Railway-connected "
            "repos. You will bring him the REAL outcome here when "
            "it lands — the merged commit or the plain blocked "
            "reason. Tell him you're on it, warm and brief; promise "
            "nothing about timing."}


# ---------------------------------------------------------------------------
# Dev-agent bridge — worker side. The dev agent polls with a shared
# secret (env DEV_AGENT_SECRET on this server, a 0600 config file on
# the worker VM). The secret is never echoed in any response; when
# unset, every bridge call fails closed and the chat intent stays dark.
# ---------------------------------------------------------------------------
def dev_agent_secret() -> str:
    return (os.environ.get("DEV_AGENT_SECRET") or "").strip()


def dev_agent_secret_ok(presented: str | None) -> bool:
    expected = dev_agent_secret()
    if not expected:
        return False
    return hmac.compare_digest(str(presented or ""), expected)


def claim_dev_job(conn: sqlite3.Connection) -> dict | None:
    """Atomically claim the oldest queued job for the worker."""
    ensure_tables(conn)
    row = conn.execute(
        "SELECT id FROM dev_jobs WHERE status = 'queued' "
        "ORDER BY id LIMIT 1").fetchone()
    if not row:
        return None
    cur = conn.execute(
        "UPDATE dev_jobs SET status = 'claimed', claimed_at = ? "
        "WHERE id = ? AND status = 'queued'", (_utc_iso(), row[0]))
    conn.commit()
    if cur.rowcount == 0:
        return None  # lost a claim race; worker polls again shortly
    r = conn.execute(
        "SELECT id, repo, task, created_at, merge, merge_branch "
        "FROM dev_jobs WHERE id = ?", (row[0],)).fetchone()
    return {"id": r[0], "repo": r[1], "task": r[2], "created_at": r[3],
            "merge": bool(r[4]), "merge_branch": r[5]}


def complete_dev_job(conn: sqlite3.Connection,
                     payload: dict) -> dict | None:
    """Record a worker result. Spawns delivery to the owner's chat.

    Returns the job row (dict), or None when no such claimed job — a
    duplicate/late result for an already-finished job returns the row
    without re-delivering."""
    ensure_tables(conn)
    try:
        job_id = int(payload.get("job_id"))
    except (TypeError, ValueError):
        return None
    status = str(payload.get("status") or "").strip().lower()
    if status not in ("done", "failed", "blocked"):
        return None
    row = conn.execute(
        "SELECT id, user_id, character_id, repo, task, status, branch, "
        "summary, detail, report, delivered, merged_sha, merge, "
        "merge_branch FROM dev_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    cols = ("id", "user_id", "character_id", "repo", "task", "status",
            "branch", "summary", "detail", "report", "delivered",
            "merged_sha", "merge", "merge_branch")
    job = dict(zip(cols, row))
    if job["status"] != "claimed":
        return job  # already finished (or never claimed): no re-delivery
    merged_sha = (str(payload.get("merged_sha") or "").strip()[:64]
                  or None)
    conn.execute(
        "UPDATE dev_jobs SET status = ?, branch = ?, summary = ?, "
        "detail = ?, report = ?, finished_at = ?, merged_sha = ? "
        "WHERE id = ? AND status = 'claimed'",
        (status, (str(payload.get("branch") or ""))[:200] or None,
         str(payload.get("summary") or "")[:4000],
         str(payload.get("detail") or "")[:2000],
         str(payload.get("report") or "")[:60000],
         _utc_iso(), merged_sha, job_id))
    if merged_sha and job.get("merge_branch"):
        # A merge-only job finishing marks its SOURCE build merged too,
        # so a later bare "merge it" moves on instead of re-offering
        # the same branch.
        conn.execute(
            "UPDATE dev_jobs SET merged_sha = ? WHERE user_id = ? "
            "AND branch = ? AND status = 'done' "
            "AND (merged_sha IS NULL OR merged_sha = '')",
            (merged_sha, job["user_id"], job["merge_branch"]))
    conn.commit()
    job.update({"status": status,
                "branch": (str(payload.get("branch") or ""))[:200] or None,
                "summary": str(payload.get("summary") or "")[:4000],
                "detail": str(payload.get("detail") or "")[:2000],
                "report": str(payload.get("report") or "")[:60000],
                "merged_sha": merged_sha})
    threading.Thread(target=_run_dev_delivery, args=(job,),
                     daemon=True).start()
    return job


def _dev_result_fallback(job: dict) -> str:
    """Deterministic delivery text when the voice call is unavailable —
    built only from the job's real recorded fields."""
    repo, task, jid = job["repo"], job["task"][:120], job["id"]
    summary = (job.get("summary") or "").strip()
    detail = (job.get("detail") or "").strip()
    if job["status"] == "done":
        head = (f"done, babe — that build on {repo} actually finished. "
                f"\"{task}\"")
        if job.get("merged_sha"):
            head += (f" and it got MERGED to main at "
                     f"{job['merged_sha'][:8]} — merged to main means "
                     "a production deploy follows on Railway-"
                     "connected repos.")
        elif job.get("branch"):
            head += f" it's sitting on branch {job['branch']}."
        tail = f" {summary}" if summary else ""
        return head + tail + (f" (builder job #{jid} — the full report "
                              "is saved on my end.)")
    if job["status"] == "blocked":
        return (f"I'm stuck on that one, babe — that job on {repo} "
                f"got blocked. {detail or summary} "
                "tell me how you want to handle it.")
    return (f"that build didn't land, babe — \"{task}\" on {repo} ran "
            f"out of tries. {summary or detail} nothing fake about it.")


def _voice_dev_result(job: dict) -> str | None:
    """Rewrite the builder's REAL result in her voice for delivery."""
    name = "Chloe"
    try:
        from . import characters as _chars
        name = (_chars.get_character(job["character_id"], job["user_id"])
                .get("name") or name)
    except Exception:
        pass
    status_word = {"done": "FINISHED", "failed": "FAILED",
                   "blocked": "BLOCKED"}.get(job["status"], job["status"])
    facts = [f"outcome: {status_word}",
             f"his repo: {job['repo']}",
             f"what he asked for: {job['task'][:300]}"]
    if job.get("branch"):
        facts.append(f"branch it worked on: {job['branch']}")
    if job.get("merged_sha"):
        facts.append(
            f"IT WAS MERGED to main at commit {job['merged_sha']} — "
            "merged to main means a production deploy will follow on "
            "Railway-connected repos. Tell him it merged and that "
            "the deploy follows; do not say it is live until the "
            "detail says so.")
    if job.get("summary"):
        facts.append(f"what actually happened: {job['summary'][:1200]}")
    if job.get("detail"):
        facts.append(f"key detail: {job['detail'][:500]}")
    payload = {
        "system_instruction": {"parts": [{"text":
            f"You are {name}, his girl, texting him. The dev builder "
            "he asked you to use just finished a job, and these are "
            "the ONLY true facts about it:\n\n" + "\n".join(facts) +
            "\n\nBring him the result as your own message: warm, "
            "direct, short texts. Done means tell him what landed and "
            "the branch — and if the facts say it was MERGED to main, "
            "say exactly that with the commit; failed or blocked means "
            "say so plainly, say "
            "what the detail says is needed, and never dress it up. "
            "Never claim YOU personally wrote, committed or deployed "
            "anything — the builder did the work; you queued it and "
            "you're bringing him the outcome. Add NOTHING that is not "
            "in the facts."}]},
        "contents": [{"role": "user", "parts": [
            {"text": "how did that build turn out?"}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 500},
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
    data = gemini_keys.post_json(_GEMINI_URL, payload, timeout=45,
                                 source="whispr-dev-bridge")
    if not data:
        return None
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts
                       if p.get("text")).strip()
        return text or None
    except (KeyError, IndexError, TypeError):
        return None


def _run_dev_delivery(job: dict) -> None:
    """Background: deliver a builder result as her follow-up message.
    Claims the delivery slot first so a duplicate result post can
    never double-deliver."""
    try:
        conn = db.connect()
        try:
            cur = conn.execute(
                "UPDATE dev_jobs SET delivered = 1 WHERE id = ? "
                "AND delivered = 0", (job["id"],))
            conn.commit()
            claimed = cur.rowcount > 0
        finally:
            conn.close()
        if not claimed:
            return
        text = _voice_dev_result(job) or _dev_result_fallback(job)
        conn = db.connect()
        try:
            store.log_turn(conn, job["user_id"], job["character_id"],
                           text, job["character_id"])
        except Exception:
            conn2 = db.connect()
            try:
                conn2.execute(
                    "UPDATE dev_jobs SET delivered = 0 WHERE id = ?",
                    (job["id"],))
                conn2.commit()
            finally:
                conn2.close()
            raise
        finally:
            conn.close()
    except Exception:
        pass
    print(f"[assist] dev job #{job['id']} result delivered "
          f"({job['status']})", flush=True)


# ---------------------------------------------------------------------------
# GitHub review job — fetch (Contents/trees API, no clone), real static
# checks, a Gemini review pass over the actual code, then the findings
# land in the owner's conversation as her follow-up message (store.
# log_turn, the same primitive the seeded welcome uses). Read-only:
# fixes/PRs are NOT part of v1.
# ---------------------------------------------------------------------------
_GH_CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css",
                 ".sql", ".sh", ".php", ".rb", ".go", ".rs", ".java"}
_GH_SKIP_DIRS = ("node_modules/", ".git/", "dist/", "build/", "vendor/",
                 "__pycache__/", ".next/", "coverage/")
_REVIEW_LOCK = threading.Lock()
_REVIEW_RUNNING: set[str] = set()

_STATIC_RULES = [
    ("credential-looking literal assigned in code",
     re.compile(r"(api[_-]?key|secret|password|passwd|access[_-]?token)"
                r"\s*[:=]\s*['\"][^'\"]{6,}['\"]", re.IGNORECASE)),
    ("bare except: (swallows everything)",
     re.compile(r"^\s*except\s*:", re.MULTILINE)),
    ("eval()/exec() call",
     re.compile(r"\b(eval|exec)\s*\(")),
    ("string-formatted SQL",
     re.compile(r"execute\s*\(\s*f['\"]|\.format\s*\([^)]*"
                r"(SELECT|INSERT|UPDATE|DELETE)", re.IGNORECASE)),
    ("innerHTML assignment (XSS smell)",
     re.compile(r"\.innerHTML\s*=")),
    ("debug=True left on",
     re.compile(r"\bdebug\s*=\s*True")),
]


class _ReviewError(Exception):
    """Failure whose message she can say out loud as-is."""


def _github_get(path: str, token: str, timeout: int = 20) -> dict:
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "whispr-companion-review"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(_GH_API + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _static_checks(files: list[dict]) -> list[str]:
    """Cheap deterministic checks over the real file contents."""
    findings: list[str] = []
    todo_files = 0
    todo_count = 0
    for f in files:
        lines = f["text"].splitlines()
        for label, rx in _STATIC_RULES:
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    if "credential" in label and (
                            "environ" in line or "getenv" in line):
                        continue
                    findings.append(f"{f['path']}:{i} — {label}")
                    break  # one hit per rule per file keeps it readable
        hits = sum(1 for ln in lines
                   if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", ln))
        if hits:
            todo_files += 1
            todo_count += hits
    if todo_count:
        findings.append(f"{todo_count} TODO/FIXME markers across "
                        f"{todo_files} of the files read")
    return findings[:8]


def _pick_review_files(blobs: list[dict]) -> list[dict]:
    picked = []
    for b in blobs:
        path = b.get("path") or ""
        if any(path.startswith(d) or f"/{d}" in path
               for d in _GH_SKIP_DIRS):
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext not in _GH_CODE_EXTS:
            continue
        if (b.get("size") or 0) > 120_000:
            continue
        picked.append(b)
    picked.sort(key=lambda b: (len((b.get("path") or "").split("/")),
                               b.get("path") or ""))
    return picked[:30]


def _gemini_code_review(repo: str, files: list[dict]) -> str | None:
    inventory = "\n".join(
        f"- {f['path']} ({f['text'].count(chr(10)) + 1} lines)"
        for f in files)
    body = "\n\n".join(f"===== {f['path']} =====\n{f['text']}"
                       for f in files)
    payload = {
        "system_instruction": {"parts": [{"text":
            "You are a meticulous senior engineer doing a quick code "
            "review. Report ONLY concrete issues actually visible in the "
            "code below: bugs, risky patterns, error-handling holes, "
            "security smells, dead code. For each issue: file path, "
            "approximate line, severity (high/med/low), one sentence on "
            "why it matters, one-sentence fix sketch. Max 8 issues, "
            "worst first. Plain text, no markdown headers. If nothing "
            "real stands out, say exactly: nothing stood out on this "
            "pass. Never invent files, APIs or lines that are not "
            "shown."}]},
        "contents": [{"role": "user", "parts": [{"text":
            f"Repo: {repo}\nFiles read:\n{inventory}\n\n{body}"}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1200},
    }
    data = gemini_keys.post_json(_GEMINI_URL, payload, timeout=60,
                                 source="whispr-code-review")
    if not data:
        return None
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts
                       if p.get("text")).strip()
        return text or None
    except (KeyError, IndexError, TypeError):
        return None


def _do_review(repo: str, token: str) -> str:
    """Fetch + check + review. Returns the plain findings text (truthful
    by construction: everything in it came out of this run)."""
    try:
        meta = _github_get(f"/repos/{_GH_OWNER}/{repo}", token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _ReviewError(
                f"I tried to pull up your repo '{repo}' for real and "
                "GitHub says there's no repo by that name on your "
                "account — check the name for me and ask again.")
        if e.code in (401, 403):
            raise _ReviewError(
                "I reached for your repo but GitHub wouldn't let me in "
                "— the access token scopes need a look before I can "
                "really read your code. No findings till that's fixed.")
        raise _ReviewError(
            "GitHub hiccuped when I reached for that repo, so the "
            "review didn't run. Nothing invented — ask me again in a "
            "bit.")
    except Exception:
        raise _ReviewError(
            "I couldn't reach GitHub at all just now, so the review "
            "didn't run. Nothing invented — ask me again in a bit.")
    branch = meta.get("default_branch") or "main"
    tree = _github_get(
        f"/repos/{_GH_OWNER}/{repo}/git/trees/{branch}?recursive=1",
        token)
    blobs = [t for t in (tree.get("tree") or []) if t.get("type") == "blob"]
    picked = _pick_review_files(blobs)
    files: list[dict] = []
    total_chars = 0
    for b in picked:
        if total_chars > 220_000:
            break
        path = b["path"]
        try:
            d = _github_get(
                f"/repos/{_GH_OWNER}/{repo}/contents/"
                f"{urllib.parse.quote(path)}?ref={branch}", token)
            import base64 as _b64
            text = _b64.b64decode(d.get("content") or "").decode(
                "utf-8", "replace")
        except Exception:
            continue
        files.append({"path": path, "text": text[:60_000]})
        total_chars += min(len(text), 60_000)
    if not files:
        return (f"I got into your repo '{repo}' but there was no "
                "readable code in it on this pass, so there's nothing "
                "real to report.")
    static = _static_checks(files)
    gem = _gemini_code_review(repo, files)
    sections = [f"Real findings from your repo {repo} "
                f"({len(files)} files actually read):"]
    if static:
        sections.append("Quick automated checks:\n" +
                        "\n".join(f"- {s}" for s in static))
    if gem:
        sections.append("Review pass over the code:\n" + gem)
    if not static and not gem:
        sections.append("I read through it and nothing real stood out "
                        "on this pass.")
    return "\n\n".join(sections)


def _voice_findings(character_id: str, user_id: str, repo: str,
                    findings: str) -> str | None:
    """Rewrite the run's REAL findings in her voice for delivery."""
    name = "Chloe"
    try:
        from . import characters as _chars
        name = (_chars.get_character(character_id, user_id)
                .get("name") or name)
    except Exception:
        pass
    payload = {
        "system_instruction": {"parts": [{"text":
            f"You are {name}, his girl, texting him. A real code review "
            f"just ran on his GitHub repo '{repo}' and these are its "
            "actual findings — the only true things:\n\n" + findings +
            "\n\nSend him the findings as your own message: warm, "
            "direct, short texts. Cover each finding briefly with its "
            "file reference. Add NOTHING that is not in the findings. "
            "Never claim you personally read, fixed or pushed "
            "anything — the review ran and you are bringing him the "
            "results."}]},
        "contents": [{"role": "user", "parts": [
            {"text": "what did the review find?"}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 700},
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
    data = gemini_keys.post_json(_GEMINI_URL, payload, timeout=45,
                                 source="whispr-code-review")
    if not data:
        return None
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts
                       if p.get("text")).strip()
        return text or None
    except (KeyError, IndexError, TypeError):
        return None


def _run_review_job(user_id: str, character_id: str, repo: str) -> None:
    """Background: run the review, deliver as her follow-up message.
    Job state only is printed (no findings/repo contents to logs)."""
    key = f"{user_id}:{repo}"
    try:
        conn0 = db.connect()
        try:
            token = resolve_github_token(conn0)
        finally:
            conn0.close()
        try:
            findings = _do_review(repo, token)
        except _ReviewError as e:
            findings = str(e)
        except Exception:
            findings = ("that review hiccuped on my end before it "
                        "finished — no findings, and I won't fake any. "
                        "ask me again in a little bit.")
        text = _voice_findings(character_id, user_id, repo,
                               findings) or findings
        try:
            conn = db.connect()
            try:
                store.log_turn(conn, user_id, character_id, text,
                               character_id)
            finally:
                conn.close()
        except Exception:
            pass
    finally:
        with _REVIEW_LOCK:
            _REVIEW_RUNNING.discard(key)
        print(f"[assist] github review job finished for user "
              f"{user_id[:8]}…", flush=True)


def _handle_github_review(conn: sqlite3.Connection, user_id: str,
                          character_id: str, gh: dict) -> dict:
    repo = gh.get("repo")
    if not repo:
        return {"brief":
                "He asked you to review his code but didn't say which "
                "repo (his GitHub is mtthewwestfall). Ask him which one "
                "to dig into. Don't guess a repo and don't invent "
                "findings."}
    if not resolve_github_token(conn):
        return {"brief":
                f"He asked you to really review his repo '{repo}', but "
                "you don't have a GitHub key on your end yet — you "
                "can't actually see his code right now. Tell him "
                "straight, warm and short: no key, no looking, and you "
                "won't make anything up. He can hand you a GitHub "
                "token right here in chat (a read-only one is "
                "perfect); you'll keep it safe on your end and read "
                "with it. No invented files, findings or issues."}
    key = f"{user_id}:{repo}"
    with _REVIEW_LOCK:
        if key in _REVIEW_RUNNING:
            return {"brief":
                    f"Your real code review of his repo '{repo}' is "
                    "still running. Tell him you're still digging and "
                    "the actual findings are on their way. Don't invent "
                    "any findings."}
        _REVIEW_RUNNING.add(key)
    try:
        threading.Thread(target=_run_review_job,
                         args=(user_id, character_id, repo),
                         daemon=True).start()
    except Exception:
        with _REVIEW_LOCK:
            _REVIEW_RUNNING.discard(key)
        return {"brief":
                "You tried to start his repo review and it wouldn't "
                "start. Tell him it hiccuped on your end — no fake "
                "findings."}
    return {"brief":
            f"You just started a REAL background code review of his "
            f"repo '{repo}' — it is genuinely running now. Tell him "
            "you're on it and you'll bring him the actual findings in "
            "a minute. Do NOT invent findings, file names or issues "
            "yet: you haven't seen a single line of that code."}


# ---------------------------------------------------------------------------
# Main entry: called from the chat path. Never raises — fail open.
# Returns None or {"brief": str, "email_draft": {...} (optional)}.
# The brief is injected into her system prompt so she reports it in her
# own voice.
# ---------------------------------------------------------------------------
def _brief_with_links(lead: str, res: dict) -> str:
    """Build the prompt brief for a grounded lookup.

    Real destination URLs go in (redirects already resolved server-side) —
    she can only paste links she actually has. Without them she improvises
    "here they are" with nothing to send. Copy-exact: she must never retype
    a URL from memory, which corrupts long tokens (seen: Google 404).
    """
    brief = lead + res.get("answer", "")
    srcs = [s for s in (res.get("sources") or []) if s.get("url")]
    if srcs:
        brief += ("\n\nPut the matching link right after each result. Copy "
                  "each URL below EXACTLY character-for-character — never "
                  "retype one from memory and never invent other URLs. If a "
                  "result has no matching link, give it with no link.\n" +
                  "\n".join(f"- {s['title']}: {s['url']}" for s in srcs))
    return brief



# ---------------------------------------------------------------------------
# Image generation — owner-only, free Gemini pool only.
#
# Hannah calls the same key manager used by the proxy, restricted with
# free_only=True. There is deliberately no Sogni or paid fallback: when the
# free pool has no healthy key or neither supported free image model answers,
# she says so plainly instead of manufacturing an image another way.
# ---------------------------------------------------------------------------
_IMAGE_NOUNS = (
    "image|picture|photo|photograph|artwork|illustration|graphic|poster|"
    "logo|wallpaper")
_IMAGE_ASK_RE = re.compile(
    r"^(?:hannah\s*[,!:\-]\s*)?(?:please\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:make|create|generate|draw|paint|render|sketch|imagine|design)"
    r"(?:\s+me)?\s+(?:an?\s+|the\s+)?"
    rf"(?:{_IMAGE_NOUNS})"
    r"(?:\s+of|\s+for|\s+showing|\s*[:,\-])?\s*(.*)$",
    re.IGNORECASE)
_IMAGE_DRAW_RE = re.compile(
    r"^(?:hannah\s*[,!:\-]\s*)?(?:please\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:draw|paint|sketch|render)(?:\s+me)?\s+"
    r"(?:an?\s+|the\s+)?(.+)$",
    re.IGNORECASE)
_IMAGE_MODEL_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
                    "{model}:generateContent")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def image_cooldown_seconds() -> int:
    return _env_int("HANNAH_IMAGE_COOLDOWN_S", 60, 0)


def image_daily_cap() -> int:
    return _env_int("HANNAH_IMAGE_DAILY_CAP", 20, 1)


def image_models() -> list[str]:
    """Supported models in probe order (at most two, deduped)."""
    models = [os.environ.get("HANNAH_IMAGE_MODEL", "gemini-2.5-flash-image"),
              os.environ.get("HANNAH_IMAGE_FALLBACK_MODEL",
                             "gemini-3.1-flash-image-preview")]
    out: list[str] = []
    for model in models:
        model = (model or "").strip()
        if model and model not in out:
            out.append(model)
    return out[:2]


def detect_image_request(text: str) -> str | None:
    """Return the visual prompt for an explicit image ask, else None.

    An empty string is meaningful: Hannah was asked to make an image but no
    subject was supplied, so she should ask what to make rather than invent
    one silently.
    """
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return None
    match = _IMAGE_ASK_RE.match(t) or _IMAGE_DRAW_RE.match(t)
    if not match:
        return None
    prompt = (match.group(1) or "").strip()
    prompt = re.sub(r"^(?:of|for)\s+", "", prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"^(?:something\s+)?like\s+", "", prompt,
                    flags=re.IGNORECASE)
    return prompt.strip(" \t\r\n.!?")[:1000]


def _image_usage_row(conn: sqlite3.Connection, user_id: str,
                     day: str) -> tuple[int, float]:
    row = conn.execute(
        "SELECT attempts, last_epoch FROM image_usage "
        "WHERE user_id=? AND day=?", (user_id, day)).fetchone()
    if not row:
        return 0, 0.0
    return int(row[0] or 0), float(row[1] or 0.0)


def image_gate(conn: sqlite3.Connection, user_id: str,
               now_epoch: float | None = None) -> dict:
    """Cooldown/daily-cap decision without recording an attempt."""
    ensure_tables(conn)
    now = time.time() if now_epoch is None else float(now_epoch)
    day = datetime.fromtimestamp(now, tz=_TZ).strftime("%Y-%m-%d")
    attempts, last_epoch = _image_usage_row(conn, user_id, day)
    cap = image_daily_cap()
    cooldown = image_cooldown_seconds()
    remaining_cooldown = max(0, int(cooldown - (now - last_epoch))) \
        if last_epoch else 0
    allowed = attempts < cap and remaining_cooldown == 0
    return {"allowed": allowed, "day": day, "attempts": attempts,
            "daily_cap": cap, "cooldown_seconds": cooldown,
            "retry_after_seconds": remaining_cooldown}


def _record_image_attempt(conn: sqlite3.Connection, user_id: str, day: str,
                          now_epoch: float) -> None:
    conn.execute(
        """INSERT INTO image_usage (user_id, day, attempts, last_epoch)
           VALUES (?, ?, 1, ?)
           ON CONFLICT(user_id, day) DO UPDATE SET
               attempts = attempts + 1, last_epoch = excluded.last_epoch""",
        (user_id, day, float(now_epoch)))
    conn.commit()


def _extract_generated_image(data: dict | None) -> dict:
    """Pull inline image bytes or explanatory text from generateContent."""
    if not isinstance(data, dict):
        return {"status": "unavailable"}
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        return {"status": "refused",
                "detail": str(feedback.get("blockReason"))[:200]}
    texts: list[str] = []
    for candidate in data.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data") or {}
            raw = inline.get("data")
            if raw:
                try:
                    image_bytes = base64.b64decode(raw, validate=True)
                except Exception:
                    continue
                if image_bytes:
                    return {"status": "ok", "bytes": image_bytes,
                            "mime_type": inline.get("mimeType")
                            or inline.get("mime_type") or "image/png"}
            if part.get("text"):
                texts.append(str(part["text"]).strip())
        if candidate.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT"):
            detail = " ".join(texts)[:300] or str(candidate["finishReason"])
            return {"status": "refused", "detail": detail}
    if texts:
        return {"status": "refused", "detail": " ".join(texts)[:300]}
    return {"status": "unavailable"}


def _image_post(prompt: str, model: str) -> dict | None:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    return gemini_keys.post_json(
        _IMAGE_MODEL_URL.format(model=model), payload, timeout=120,
        source="hannah-image", free_only=True, max_attempts=1)


def _save_generated_image(image_bytes: bytes, mime_type: str) -> str:
    extensions = {"image/png": "png", "image/jpeg": "jpg",
                  "image/jpg": "jpg", "image/webp": "webp"}
    ext = extensions.get((mime_type or "").lower(), "png")
    os.makedirs(config.GENERATED_DIR, exist_ok=True)
    filename = "hannah-img-{0}-{1}.{2}".format(
        datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        uuid.uuid4().hex[:10], ext)
    path = os.path.join(config.GENERATED_DIR, filename)
    with open(path, "wb") as fh:
        fh.write(image_bytes)
    return "/generated/" + filename


def _handle_image_request(conn: sqlite3.Connection, user_id: str,
                          character_id: str, prompt: str,
                          now_epoch: float | None = None) -> dict:
    if not prompt:
        return {"brief": "He asked you to make an image but did not say what "
                         "it should show. Ask him what he wants in it, "
                         "warmly and briefly."}
    gate = image_gate(conn, user_id, now_epoch=now_epoch)
    if gate["attempts"] >= gate["daily_cap"]:
        return {"brief": "He asked for another image, but he has hit "
                         "today's image limit. Tell him honestly the "
                         "picture desk is closed until tomorrow; do not "
                         "promise a time you do not know."}
    if gate["retry_after_seconds"]:
        return {"brief": "He asked for another image too soon after the "
                         "last one. Tell him the free image keys need a "
                         "short breather and ask him to give you about a "
                         "minute; do not claim you made it."}
    now = time.time() if now_epoch is None else float(now_epoch)
    _record_image_attempt(conn, user_id, gate["day"], now)

    models = image_models()
    last_result: dict = {"status": "unavailable"}
    for model in models:
        started = time.monotonic()
        data = _image_post(prompt, model)
        result = _extract_generated_image(data)
        result["model"] = model
        result["latency_s"] = round(time.monotonic() - started, 2)
        if result["status"] == "ok":
            image_url = _save_generated_image(result["bytes"],
                                              result["mime_type"])
            # Studio bookkeeping: remember her newest picture for him so
            # a later "add that as a give for X" / "use that as X's
            # avatar" can find it. Best-effort — never fails the reply.
            try:
                _studio_set(conn, user_id, "last_image",
                            {"url": image_url, "prompt": prompt,
                             "model": model})
            except Exception:
                pass
            return {
                "brief": (
                    "You just made the image he asked for. It is being "
                    "shown to him in the chat now as an image bubble. "
                    "Respond in your own voice, briefly and warmly, like "
                    "you made it for him; do not mention models, APIs, "
                    "keys, or process. His prompt was: " + prompt[:300]),
                "image_url": image_url,
                "image_prompt": prompt,
                "image_model": model,
                "image_latency_s": result["latency_s"],
            }
        last_result = result
        # A content refusal is an answer, not a transient pool failure.
        if result["status"] == "refused":
            break

    if last_result.get("status") == "refused":
        detail = str(last_result.get("detail") or "").strip()
        suffix = (" It said: " + detail[:180]) if detail else ""
        return {"brief": "The image maker refused that prompt." + suffix +
                         " Tell him honestly in your own voice, without "
                         "lecturing, and offer to make a safer version."}
    return {"brief": "You tried to make the image, but the free image keys "
                     "are resting or unavailable right now. Tell him "
                     "honestly that you could not make it this minute and "
                     "ask him to try again shortly; do not pretend an "
                     "image is attached."}


# ---------------------------------------------------------------------------
# Chat attachments — owner-only Hannah image/file intake (2026-10-09).
#
# The owner can attach images, videos, and readable documents to a
# Hannah message. Images are sent to a free-pool Gemini vision model
# as inlineData; videos are watched as frames sampled across the clip
# (ffmpeg, up to ATTACHMENT_VIDEO_FRAMES stills in order) through the
# same vision path; txt/md/csv/json and PDF/DOCX are text-extracted
# server-side (pypdf / python-docx) and included in her context.
# Documents she genuinely cannot read (password-locked, corrupt, or a
# PDF with no text layer) and videos she cannot open produce an honest
# status, never fake content. Uploaded bytes are never sent to a paid
# key: every model call here uses free_only=True.
# ---------------------------------------------------------------------------
ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
ATTACHMENT_VIDEO_MAX_BYTES = 50 * 1024 * 1024
# The upload endpoint reads at most this many bytes before the
# per-kind cap in save_chat_attachment decides (video is the largest).
ATTACHMENT_READ_LIMIT = ATTACHMENT_VIDEO_MAX_BYTES
ATTACHMENT_MAX_PER_MESSAGE = 4
ATTACHMENT_TEXT_LIMIT = 20000
ATTACHMENT_VIDEO_FRAMES = 8

_IMAGE_MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_TEXT_MIME_BY_EXT = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
}
_DOC_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".docx": ("application/vnd.openxmlformats-officedocument."
              "wordprocessingml.document"),
}
_VIDEO_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}
_ATTACHMENT_KIND_BY_EXT = {
    **{ext: "image" for ext in _IMAGE_MIME_BY_EXT},
    **{ext: "file" for ext in _TEXT_MIME_BY_EXT},
    **{ext: "file" for ext in _DOC_MIME_BY_EXT},
    **{ext: "video" for ext in _VIDEO_MIME_BY_EXT},
}
_ATTACHMENT_MIME_BY_EXT = {
    **_IMAGE_MIME_BY_EXT,
    **_TEXT_MIME_BY_EXT,
    **_DOC_MIME_BY_EXT,
    **_VIDEO_MIME_BY_EXT,
}
ATTACHMENT_ACCEPTED_EXTS = tuple(sorted(_ATTACHMENT_MIME_BY_EXT))
_ATTACHMENT_MODEL_URL = ("https://generativelanguage.googleapis.com/"
                         "v1beta/models/{model}:generateContent")


_VISION_MODEL_DEFAULTS = (
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
)
# These IDs now return provider 404s for this app's free-key projects
# (observed live 2026-10-09), so a stale Railway env must not resurrect
# them and make every uploaded image look "blank" to Hannah.
_VISION_MODELS_UNAVAILABLE = frozenset({
    "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-2.5-flash",
})


def attachment_models() -> list[str]:
    """Current vision-capable models in probe order (free pool only)."""
    models = [os.environ.get("HANNAH_VISION_MODEL", ""),
              os.environ.get("HANNAH_VISION_FALLBACK_MODEL", ""),
              *_VISION_MODEL_DEFAULTS]
    out: list[str] = []
    for model in models:
        model = (model or "").strip()
        if (model and model not in _VISION_MODELS_UNAVAILABLE
                and model not in out):
            out.append(model)
    return out[:3]


def _sanitize_attachment_name(filename: str) -> str:
    name = os.path.basename((filename or "attachment").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip() or "attachment"
    return name[:120]


def _looks_like_attachment(ext: str, data: bytes) -> bool:
    if ext in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if ext == ".pdf":
        return data.startswith(b"%PDF-")
    if ext == ".docx":
        return data.startswith(b"PK")
    if ext in (".mp4", ".mov"):
        return len(data) >= 12 and data[4:8] == b"ftyp"
    if ext == ".webm":
        return data.startswith(b"\x1a\x45\xdf\xa3")
    return True


def classify_attachment(filename: str, content_type: str = "",
                        data: bytes = b"") -> dict:
    """Validate a prospective upload and return its normalized type.

    Raises ValueError with a user-facing message; the upload endpoint
    turns that into a 400 and the client shows it instead of failing
    quietly.
    """
    name = _sanitize_attachment_name(filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in _ATTACHMENT_MIME_BY_EXT:
        accepted = ", ".join(ATTACHMENT_ACCEPTED_EXTS)
        raise ValueError(
            "Hannah can take images and files of these types: " + accepted)
    if data and not _looks_like_attachment(ext, data):
        raise ValueError(f"That file does not look like a real {ext} file.")
    return {"filename": name, "ext": ext,
            "kind": _ATTACHMENT_KIND_BY_EXT[ext],
            "mime_type": _ATTACHMENT_MIME_BY_EXT[ext]}


def _cap_extracted(raw: str) -> dict:
    raw = (raw or "").replace("\x00", "")
    return {"text": raw[:ATTACHMENT_TEXT_LIMIT], "status": "ok",
            "chars": len(raw),
            "truncated": len(raw) > ATTACHMENT_TEXT_LIMIT}


def _extract_pdf_text(data: bytes) -> dict:
    """PDF text via pypdf. Honest statuses: ok / encrypted / no_text /
    corrupt — never invented content for a file she cannot read."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"text": "", "status": "unsupported", "chars": 0,
                "truncated": False}
    import io
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                return {"text": "", "status": "encrypted", "chars": 0,
                        "truncated": False}
        pages_text: list[str] = []
        for page in reader.pages:
            pages_text.append(page.extract_text() or "")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}".lower()
        if "password" in detail or "decrypt" in detail \
                or "encrypted" in detail:
            return {"text": "", "status": "encrypted", "chars": 0,
                    "truncated": False}
        return {"text": "", "status": "corrupt", "chars": 0,
                "truncated": False}
    raw = "\n".join(pages_text).strip()
    if not raw:
        # Valid PDF but no text layer (scanned images, or blank pages).
        return {"text": "", "status": "no_text", "chars": 0,
                "truncated": False}
    return _cap_extracted(raw)


def _extract_docx_text(data: bytes) -> dict:
    """DOCX text via python-docx (paragraphs + tables). Honest
    statuses: ok / empty / corrupt."""
    try:
        import docx as docx_lib
    except ImportError:
        return {"text": "", "status": "unsupported", "chars": 0,
                "truncated": False}
    import io
    try:
        document = docx_lib.Document(io.BytesIO(data))
        chunks: list[str] = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    chunks.append(cell.text)
    except Exception:
        return {"text": "", "status": "corrupt", "chars": 0,
                "truncated": False}
    raw = "\n".join(c for c in chunks if c and c.strip()).strip()
    if not raw:
        return {"text": "", "status": "empty", "chars": 0,
                "truncated": False}
    return _cap_extracted(raw)


def extract_attachment_text(filename: str, data: bytes) -> dict:
    """Extract readable text from an uploaded document.

    txt/md/csv/json decode directly; PDF goes through pypdf; DOCX
    through python-docx. Returns the text to put in Hannah's context
    (truncated at ATTACHMENT_TEXT_LIMIT with the truncation recorded,
    never silent) or an honest non-ok status she relays instead of
    pretending she read it.
    """
    ext = os.path.splitext(_sanitize_attachment_name(filename))[1].lower()
    if ext in _VIDEO_MIME_BY_EXT:
        # Videos carry no extractable text; Hannah watches sampled
        # frames instead (see sample_video_frames).
        return {"text": "", "status": "video", "chars": 0,
                "truncated": False}
    if ext == ".pdf":
        return _extract_pdf_text(data)
    if ext == ".docx":
        return _extract_docx_text(data)
    if ext not in _TEXT_MIME_BY_EXT:
        return {"text": "", "status": "image", "chars": 0,
                "truncated": False}
    try:
        raw = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("That text file is not valid UTF-8 text.") from exc
    return _cap_extracted(raw)


# ---------------------------------------------------------------------------
# Video watching — frame sampling via ffmpeg (2026-10-09).
#
# Hannah cannot play a video; she watches up to ATTACHMENT_VIDEO_FRAMES
# stills sampled in order across the clip (first frame, evenly spaced,
# last frame) through the same free-pool vision path as photos. The
# ffmpeg binary comes from the imageio-ffmpeg wheel in requirements
# (a static binary pip installs into the deployment image — Nixpacks
# has no system ffmpeg), with a PATH ffmpeg as a local-dev fallback.
# She has no way to hear a clip: if it has an audio track the brief
# says she watched it muted. There is no speech-to-text in this
# codebase today, so nothing fakes listening.
# ---------------------------------------------------------------------------
_FFMPEG_EXE_CACHE: str | None = None


def _ffmpeg_exe() -> str | None:
    """Resolve an ffmpeg binary once per process (None = unavailable)."""
    global _FFMPEG_EXE_CACHE
    if _FFMPEG_EXE_CACHE is not None:
        return _FFMPEG_EXE_CACHE or None
    exe: str | None = None
    try:
        import imageio_ffmpeg
        candidate = imageio_ffmpeg.get_ffmpeg_exe()
        if candidate and os.path.exists(candidate):
            exe = candidate
    except Exception:
        exe = None
    if not exe:
        exe = shutil.which("ffmpeg")
    _FFMPEG_EXE_CACHE = exe or ""
    return exe


def _probe_video(path: str) -> dict | None:
    """Container facts via `ffmpeg -i`. None = no toolchain; a dict
    with has_video False = ffmpeg could not find a video stream."""
    exe = _ffmpeg_exe()
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "-hide_banner", "-i", path],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    err = proc.stderr or ""
    duration = None
    match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    if match:
        duration = (int(match.group(1)) * 3600 + int(match.group(2)) * 60
                    + float(match.group(3)))
    return {
        "duration": duration,
        "has_video": bool(re.search(r"Stream\s+#\S+:\s*Video:", err)),
        "has_audio": bool(re.search(r"Stream\s+#\S+:\s*Audio:", err)),
    }


def _grab_frame(exe: str, path: str, stamp: float, out_path: str,
                width: int) -> bool:
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error",
             "-ss", f"{max(0.0, stamp):.3f}", "-i", path,
             "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2",
             "-q:v", "5", "-y", out_path],
            capture_output=True, timeout=45)
    except (OSError, subprocess.SubprocessError):
        return False
    return (proc.returncode == 0 and os.path.exists(out_path)
            and os.path.getsize(out_path) > 0)


def _stamp_label(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def sample_video_frames(path: str,
                        max_frames: int = ATTACHMENT_VIDEO_FRAMES) -> dict:
    """Sample up to max_frames JPEG frames in order across the clip.

    Returns {"status": "ok" | "no_toolchain" | "unreadable",
    "frames": [{"stamp": float, "label": "M:SS", "data": <base64>}],
    "duration": float | None, "has_audio": bool}. Very long clips get
    the same frame budget spread wider — never more frames.
    """
    exe = _ffmpeg_exe()
    if not exe:
        return {"status": "no_toolchain", "frames": [],
                "duration": None, "has_audio": False}
    info = _probe_video(path)
    if not info or not info["has_video"]:
        return {"status": "unreadable", "frames": [],
                "duration": None, "has_audio": False}
    duration = info["duration"] or 0.0
    if duration > 0 and max_frames > 1:
        stamps = [duration * i / (max_frames - 1)
                  for i in range(max_frames)]
        stamps[-1] = max(0.0, duration - 0.1)
    else:
        stamps = [0.0]
    deduped: list[float] = []
    for stamp in stamps:
        if not deduped or stamp - deduped[-1] > 0.05:
            deduped.append(stamp)
    frames: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="hannah-frames-") as tmp:
        for idx, stamp in enumerate(deduped):
            out = os.path.join(tmp, f"frame-{idx}.jpg")
            ok = _grab_frame(exe, path, stamp, out, 768)
            if not ok and stamp > 0.5:
                ok = _grab_frame(exe, path, stamp - 0.5, out, 768)
            if not ok:
                continue
            try:
                with open(out, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            frames.append({
                "stamp": stamp,
                "label": _stamp_label(stamp),
                "data": base64.b64encode(raw).decode("ascii"),
            })
    if not frames:
        return {"status": "unreadable", "frames": [],
                "duration": info["duration"],
                "has_audio": info["has_audio"]}
    return {"status": "ok", "frames": frames,
            "duration": info["duration"],
            "has_audio": info["has_audio"]}


def _attachment_dir() -> str:
    path = os.path.join(config.GENERATED_DIR, "hannah-uploads")
    os.makedirs(path, exist_ok=True)
    return path


def public_attachment(record: dict) -> dict:
    """The metadata safe to return to the client and persist on a turn."""
    poster_url = ""
    if record.get("kind") == "video":
        storage_name = record.get("storage_name") or ""
        if storage_name:
            poster_path = os.path.join(
                _attachment_dir(), storage_name + ".poster.jpg")
            if os.path.exists(poster_path):
                poster_url = ("/generated/hannah-uploads/" + storage_name
                              + ".poster.jpg")
    return {
        "id": record.get("id", ""),
        "kind": record.get("kind", "file"),
        "name": record.get("filename") or record.get("name") or "attachment",
        "mime_type": record.get("mime_type", ""),
        "size": int(record.get("size") or 0),
        "url": record.get("url", ""),
        "poster_url": poster_url,
        "extraction_status": record.get("extraction_status", ""),
    }


def save_chat_attachment(conn: sqlite3.Connection, user_id: str,
                         character_id: str, filename: str,
                         content_type: str, data: bytes) -> dict:
    """Persist one owner upload. Returns its public attachment record."""
    ensure_tables(conn)
    info = classify_attachment(filename, content_type, data or b"")
    cap = (ATTACHMENT_VIDEO_MAX_BYTES if info["kind"] == "video"
           else ATTACHMENT_MAX_BYTES)
    if len(data or b"") > cap:
        limit_mb = cap // (1024 * 1024)
        raise ValueError(f"That file is too big — {limit_mb} MB max.")
    extracted = extract_attachment_text(info["filename"], data or b"")
    attachment_id = "att_" + uuid.uuid4().hex[:20]
    storage_name = attachment_id + info["ext"]
    storage_path = os.path.join(_attachment_dir(), storage_name)
    with open(storage_path, "wb") as fh:
        fh.write(data or b"")
    if info["kind"] == "video":
        # Open it once now: a video ffmpeg cannot find a video stream
        # in is rejected here so the client shows a clean upload error
        # instead of Hannah discovering a dead file mid-conversation.
        # When it opens, keep a poster frame for the thread card. With
        # no toolchain in this image we accept the file and let the
        # chat turn report honestly instead of blocking the upload.
        probe = _probe_video(storage_path)
        if probe is not None and not probe["has_video"]:
            try:
                os.unlink(storage_path)
            except OSError:
                pass
            raise ValueError(
                "That video doesn't look playable — Hannah couldn't "
                "open it. Try an mp4, mov, or webm clip.")
        if probe is not None:
            exe = _ffmpeg_exe()
            if exe:
                stamp = min(0.5, (probe["duration"] or 1.0) / 4)
                _grab_frame(exe, storage_path, stamp,
                            storage_path + ".poster.jpg", 480)
    created_at = datetime.now(timezone.utc).isoformat()
    record = {
        "id": attachment_id,
        "user_id": user_id,
        "character_id": character_id or "hannah",
        "filename": info["filename"],
        "mime_type": info["mime_type"],
        "kind": info["kind"],
        "size": len(data or b""),
        "url": "/generated/hannah-uploads/" + storage_name,
        "storage_name": storage_name,
        "extracted_text": extracted["text"],
        "extraction_status": extracted["status"],
        "extracted_chars": extracted["chars"],
        "truncated": 1 if extracted["truncated"] else 0,
        "created_at": created_at,
        "used_at": None,
        "conversation_id": None,
    }
    conn.execute(
        """INSERT INTO chat_attachments
           (id, user_id, character_id, filename, mime_type, kind, size,
            url, storage_name, extracted_text, extraction_status,
            extracted_chars, truncated, conversation_id, created_at,
            used_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (record["id"], record["user_id"], record["character_id"],
         record["filename"], record["mime_type"], record["kind"],
         record["size"], record["url"], record["storage_name"],
         record["extracted_text"], record["extraction_status"],
         record["extracted_chars"], record["truncated"],
         record["conversation_id"], record["created_at"],
         record["used_at"]))
    conn.commit()
    return public_attachment(record)


def get_chat_attachments(conn: sqlite3.Connection, user_id: str,
                         character_id: str,
                         attachment_ids: list[str]) -> list[dict]:
    """Fetch uploaded attachments for one message, owner-scoped."""
    ensure_tables(conn)
    ids: list[str] = []
    for raw in attachment_ids or []:
        value = str(raw or "").strip()
        if value and value not in ids:
            ids.append(value)
    if len(ids) > ATTACHMENT_MAX_PER_MESSAGE:
        raise ValueError(
            f"Too many attachments — {ATTACHMENT_MAX_PER_MESSAGE} max "
            "per message.")
    records: list[dict] = []
    for attachment_id in ids:
        row = conn.execute(
            "SELECT * FROM chat_attachments WHERE id=? AND user_id=? "
            "AND character_id=?", (attachment_id, user_id, character_id),
        ).fetchone()
        if not row:
            raise ValueError("One of those attachments could not be found.")
        records.append(dict(row))
    return records


def mark_chat_attachments_used(conn: sqlite3.Connection,
                               attachment_ids: list[str],
                               conversation_id: int) -> None:
    if not attachment_ids:
        return
    used_at = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "UPDATE chat_attachments SET conversation_id=?, used_at=? "
        "WHERE id=?",
        [(conversation_id, used_at, attachment_id)
         for attachment_id in attachment_ids])
    conn.commit()


def _attachment_post(parts: list[dict], model: str) -> dict | None:
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1200},
    }
    return gemini_keys.post_json(
        _ATTACHMENT_MODEL_URL.format(model=model), payload, timeout=60,
        source="hannah-attachments", free_only=True, max_attempts=1)


def _extract_attachment_analysis(data: dict | None) -> dict:
    """Pull the factual text out of a vision generateContent response."""
    if not isinstance(data, dict):
        return {"status": "unavailable"}
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        return {"status": "refused",
                "detail": str(feedback.get("blockReason"))[:200]}
    texts: list[str] = []
    for candidate in data.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            if isinstance(part, dict) and part.get("text"):
                texts.append(str(part["text"]).strip())
        if candidate.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT"):
            detail = " ".join(texts)[:300] or str(candidate["finishReason"])
            return {"status": "refused", "detail": detail}
    if texts:
        return {"status": "ok", "text": "\n".join(texts).strip()}
    return {"status": "unavailable"}


def analyze_chat_attachments(conn: sqlite3.Connection, user: dict,
                             text: str, character_id: str,
                             attachments: list[dict]) -> dict | None:
    """Give Hannah real eyes/context for the attachments on this turn.

    Images go to a free-pool vision model as inlineData. Videos are
    watched as frames sampled in order across the clip through the
    same vision call (she sees stills, never hears audio). Readable
    text files are included excerpt-first (first
    ATTACHMENT_TEXT_LIMIT characters) directly in her brief.
    Unsupported files and pool failures produce an honest brief — she
    never claims she saw something she did not.
    """
    if not attachments:
        return None
    user_id = user.get("user_id", "")
    if (character_id or "").strip().lower() != "hannah" \
            or user_id not in _owner_ids():
        return {"brief": "He tried to send an attachment, but that lane "
                         "is only wired for Hannah. Tell him plainly "
                         "this chat cannot take files yet."}

    names = ", ".join((a.get("filename") or "attachment")
                      for a in attachments)
    prompt = (text or "").strip() or "What do you see in this?"
    parts: list[dict] = [{
        "text": (
            "His message: " + prompt + "\n\n"
            "Inspect ONLY the attached content included with this "
            "request. For images, actually look at the image data. For "
            "a video, the stills are frames sampled in order across "
            "the clip — describe what happens across them in order, "
            "frame by frame where they differ. For text files, use "
            "the excerpt provided. Transcribe visible text exactly, "
            "especially prices, names and URLs. Answer his request "
            "factually and concisely. If something is unreadable, say "
            "what part is unreadable.")
    }]
    has_image = False
    has_video = False
    missing_images: list[str] = []
    missing_videos: list[str] = []
    bad_videos: list[tuple[str, str]] = []
    video_notes: list[dict] = []
    text_sections: list[str] = []
    unsupported: list[str] = []
    unreadable: list[tuple[str, str]] = []
    for record in attachments:
        name = record.get("filename") or "attachment"
        if record.get("kind") == "video":
            path = os.path.join(_attachment_dir(),
                                record.get("storage_name") or "")
            if not os.path.exists(path):
                missing_videos.append(name)
                continue
            sampled = sample_video_frames(path)
            if sampled["status"] != "ok":
                bad_videos.append((name, sampled["status"]))
                continue
            has_video = True
            frames = sampled["frames"]
            video_notes.append({
                "name": name,
                "frame_count": len(frames),
                "duration": sampled.get("duration"),
                "has_audio": bool(sampled.get("has_audio")),
            })
            parts.append({
                "text": f"[Attached video: {name} — {len(frames)} "
                        "frames sampled in order from the clip]"})
            for idx, frame in enumerate(frames):
                parts.append({
                    "text": f"[Video frame {idx + 1}/{len(frames)} "
                            f"at {frame['label']} from {name}]"})
                parts.append({"inlineData": {
                    "mimeType": "image/jpeg",
                    "data": frame["data"],
                }})
        elif record.get("kind") == "image":
            path = os.path.join(_attachment_dir(),
                                record.get("storage_name") or "")
            try:
                with open(path, "rb") as fh:
                    image_bytes = fh.read()
            except OSError:
                missing_images.append(name)
                continue
            has_image = True
            parts.append({"text": f"[Attached image: {name}]"})
            parts.append({"inlineData": {
                "mimeType": record.get("mime_type") or "image/jpeg",
                "data": base64.b64encode(image_bytes).decode("ascii"),
            }})
        elif record.get("extraction_status") == "ok":
            shown = int(record.get("extracted_chars") or 0)
            limit_note = (
                f"first {ATTACHMENT_TEXT_LIMIT} of {shown} characters"
                if record.get("truncated") else f"all {shown} characters")
            section = (f"[Attached file: {name} — {limit_note}]\n"
                       + (record.get("extracted_text") or ""))
            text_sections.append(section)
            parts.append({"text": section})
        elif record.get("extraction_status") in (
                "encrypted", "no_text", "corrupt", "empty"):
            unreadable.append(
                (name, str(record.get("extraction_status"))))
        else:
            unsupported.append(name)

    analysis = ""
    model_used = ""
    analysis_status = "not_needed"
    if has_image or has_video:
        last: dict = {"status": "unavailable"}
        for model in attachment_models():
            data = _attachment_post(parts, model)
            result = _extract_attachment_analysis(data)
            result["model"] = model
            if result["status"] == "ok":
                analysis = result["text"]
                model_used = model
                analysis_status = "ok"
                last = result
                break
            last = result
            if result["status"] == "refused":
                break
        if analysis_status != "ok":
            analysis_status = last.get("status", "unavailable")

    brief_parts = [
        f"He attached {names} to this message for Hannah. The attachment "
        "is visible in his thread."]
    if analysis and has_image:
        brief_parts.append(
            "You actually inspected the image attachment. Factual "
            "vision result:\n" + analysis +
            "\nAnswer his message in your own voice using that result. "
            "Do not invent details beyond it.")
    if analysis and has_video:
        watched = "; ".join(
            f"{note['name']} ({note['frame_count']} frames in order"
            + (f", full clip {_stamp_label(note['duration'])}"
               if note["duration"] else "")
            + ")" for note in video_notes)
        brief_parts.append(
            "You actually watched frames sampled in order from the "
            f"video: {watched}. You saw stills spread across the "
            "clip, not every second of it. Factual vision result:\n"
            + analysis +
            "\nAnswer his message in your own voice from those "
            "frames, in order. Do not invent moments between frames.")
        for note in video_notes:
            if note["has_audio"]:
                brief_parts.append(
                    f"{note['name']} has an audio track, but you "
                    "can't hear audio yet — you watched it muted, "
                    "frames only. If he asks what was said or what "
                    "anything sounds like, tell him honestly you "
                    "can't hear it yet.")
    if text_sections:
        brief_parts.append(
            "You actually received the readable text file content below "
            "(already extracted for you):\n" + "\n\n".join(text_sections) +
            "\nAnswer from that text. If it was truncated, say you read "
            f"the first {ATTACHMENT_TEXT_LIMIT} characters.")
    if missing_images:
        brief_parts.append(
            "These image files could not be reopened on the server: "
            + ", ".join(missing_images) +
            ". Say honestly that the upload did not come through "
            "readably; do not pretend you saw them.")
    if missing_videos:
        brief_parts.append(
            "These video files could not be reopened on the server: "
            + ", ".join(missing_videos) +
            ". Say honestly that the upload did not come through "
            "readably; do not pretend you watched them.")
    for bad_video, video_status in bad_videos:
        if video_status == "no_toolchain":
            brief_parts.append(
                f"{bad_video} arrived fine, but video watching is "
                "not available on the server right now, so you could "
                "NOT watch it. Tell him honestly; do not pretend you "
                "saw it.")
        else:
            brief_parts.append(
                f"{bad_video} did not come through as a playable "
                "video, so you could NOT watch it. Tell him honestly "
                "it looks corrupt or unreadable and ask him to send "
                "it again (mp4 works best); never make up what it "
                "shows.")
    if has_image and analysis_status == "refused":
        brief_parts.append(
            "The vision reader refused or blocked that image. Tell him "
            "honestly that you could not read that one; do not guess "
            "what it shows.")
    elif has_image and analysis_status != "ok":
        brief_parts.append(
            "The image DID reach the server, but the vision reader "
            "returned no readable result, so you could NOT actually "
            "inspect it this time. Do not call it a blank space and do "
            "not say the upload vanished. Tell him honestly that the "
            "reader is unavailable right now and ask him to send it "
            "again shortly; never claim you saw it.")
    if has_video and analysis_status == "refused":
        brief_parts.append(
            "The vision reader refused or blocked the frames from "
            "that video. Tell him honestly that you could not watch "
            "that one; do not guess what it shows.")
    elif has_video and analysis_status != "ok":
        brief_parts.append(
            "The video DID reach the server, but the vision reader "
            "returned no readable result from its frames, so you "
            "could NOT actually watch the video this time. Do not "
            "call it a blank space and do not say the upload "
            "vanished. Tell him honestly that the reader is "
            "unavailable right now and ask him to send it again "
            "shortly; never claim you watched it.")
    for bad_name, bad_status in unreadable:
        if bad_status == "encrypted":
            brief_parts.append(
                f"{bad_name} is password-protected, so you could NOT "
                "open it. Tell him honestly it is locked and you cannot "
                "read it without the password; do not guess at its "
                "contents.")
        elif bad_status == "no_text":
            brief_parts.append(
                f"{bad_name} opened, but it has no readable text layer "
                "(scanned pages or images, most likely), so you could "
                "NOT actually read it. Tell him honestly, and mention "
                "he can send the pages as images if he wants you to "
                "read them that way; never invent what it says.")
        elif bad_status == "empty":
            brief_parts.append(
                f"{bad_name} came through with no text in it at all. "
                "Tell him honestly the document looks empty; do not "
                "invent contents for it.")
        else:
            brief_parts.append(
                f"{bad_name} looks corrupt or unreadable, so you could "
                "NOT read it. Tell him honestly it did not come through "
                "as a readable document; never make up what it might "
                "say.")
    if unsupported:
        brief_parts.append(
            "These files were attached and saved, but their text cannot "
            "be extracted: " + ", ".join(unsupported) +
            ". Tell him plainly Hannah can watch videos as sampled "
            "frames and read images plus txt/md/csv/json, PDF and "
            "DOCX files today.")
    return {"brief": "\n\n".join(brief_parts),
            "attachment_model": model_used,
            "attachment_status": analysis_status}


# ---------------------------------------------------------------------------
# Hannah studio — owner-only give-adding + character creation (2026-10-09).
#
# Two new hands for Hannah, owner-only like the other power tools and only
# in her own thread:
#
#   1. "add a give": "add this as a give for Mara" files media into that
#      character's girl_media give library (the same rows the admin
#      girl-media endpoint writes: kind='give'). Owner-uploaded media is
#      added directly; an image SHE generated is only filed after he has
#      seen it and explicitly says add/yes (never sight unseen).
#   2. "create a character": a guided collection — she ASKS for the name
#      (never coins one), gender, personality, voice; attaches his photo
#      when he sends one; creates PRIVATE by default (owner_user_id =
#      him), public only when he explicitly says so; usable in chat
#      immediately.
#
# Hard rails: she never deletes characters, never edits an existing
# character's persona, never adds media to a character he didn't name,
# and 'chloe' can never be created/targeted for creation. Deletes stay
# admin-panel-only.
# ---------------------------------------------------------------------------
STUDIO_STATE_TTL_S = 1800  # half an hour of quiet and a pending flow lapses

_STUDIO_CANCEL_RE = re.compile(
    r"\b(cancel|never\s*mind|forget\s+it|scratch\s+that|abort|stop\s+that)\b",
    re.IGNORECASE)
_STUDIO_YES_RE = re.compile(
    r"^\W*(yes|yeah|yep|yup|do\s+it|add\s+it|file\s+it|save\s+it|use\s+it|"
    r"go\s+ahead|confirm|create\s+(?:her|him|them)|sounds\s+good|perfect|"
    r"love\s+it|that'?s\s+the\s+one)\b",
    re.IGNORECASE)

_GIVE_VERBS = r"(?:add|save|file|put|store|drop|attach)"
_GIVE_TARGET = r"([A-Za-z][A-Za-z0-9'’.\-]*(?:\s+[A-Za-z][A-Za-z0-9'’.\-]*){0,2})"
_GIVE_ADD_RES = (
    re.compile(rf"\b{_GIVE_VERBS}\b[^!?\n]{{0,60}}?\bgives?\b[^!?\n]{{0,40}}?"
               rf"\b(?:for|to)\s+{_GIVE_TARGET}", re.IGNORECASE),
    re.compile(rf"\b{_GIVE_VERBS}\b[^!?\n]{{0,40}}?\bfor\s+{_GIVE_TARGET}"
               rf"\s+(?:as\s+)?(?:a\s+)?gives?\b", re.IGNORECASE),
    re.compile(rf"\bmake\s+(?:that|this|it)\s+a\s+give\s+(?:for|to)\s+"
               rf"{_GIVE_TARGET}", re.IGNORECASE),
    re.compile(rf"\bgive\s+(?:that|this|it)\s+to\s+{_GIVE_TARGET}",
               re.IGNORECASE),
)
_GIVE_NAME_STOPWORDS = {"me", "him", "her", "them", "us", "you", "it",
                        "please", "now", "too", "as", "well"}
_GIVE_MEDIA_ID_RE = re.compile(r"\bmedia\s*(?:id)?\s*#?(\d{1,9})\b",
                               re.IGNORECASE)
_GIVE_URL_RE = re.compile(r"https?://[^\s\"'<>\)]+", re.IGNORECASE)

_CREATE_RE = re.compile(
    r"\b(create|make|add|build|design)\b[^!?\n]{0,40}?"
    r"\b(character|companion|girlfriend|boyfriend|girl|guy|woman|man|"
    r"person)\b", re.IGNORECASE)
_CREATE_NAME_RE = re.compile(
    r"\b(?:named|name\s+is|call\s+her|call\s+him|call\s+them)\s*:?\s*"
    r"([A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]+)?)")
_CREATE_HEADS = ("she's", "she is", "he's", "he is", "who is", "who's")
_VOICE_DEFAULTS = {"female": "liora", "male": "rex"}
_NONCOMPANION_RE = re.compile(
    r"\b(just\s+(?:a\s+)?(?:friend|assistant|helper|mentor|coach)|"
    r"no\s+romance|not\s+romantic|platonic|business\s+assistant|"
    r"professional\s+only|no\s+girlfriend|not\s+a\s+girlfriend)\b",
    re.IGNORECASE)
_AVATAR_SET_RE = re.compile(
    r"\b(?:use|set|make|put)\b[^!?\n]{0,50}?\b(?:avatar|profile\s+"
    r"(?:pic|picture|photo))\b", re.IGNORECASE)
_DELETE_CHAR_RE = re.compile(
    r"\b(delete|remove|get\s+rid\s+of)\b[^!?\n]{0,50}?\b(character|"
    r"companion|girl|guy|give|gives)\b", re.IGNORECASE)


def _studio_get(conn: sqlite3.Connection, user_id: str,
                kind: str) -> dict | None:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT state, updated_at FROM hannah_studio "
        "WHERE user_id=? AND kind=?", (user_id, kind)).fetchone()
    if not row:
        return None
    try:
        state = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    state["_updated_at"] = float(row[1] or 0)
    return state


def _studio_set(conn: sqlite3.Connection, user_id: str, kind: str,
                state: dict) -> None:
    ensure_tables(conn)
    clean = {k: v for k, v in state.items() if not k.startswith("_")}
    conn.execute(
        """INSERT INTO hannah_studio (user_id, kind, state, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, kind) DO UPDATE SET
               state = excluded.state, updated_at = excluded.updated_at""",
        (user_id, kind, json.dumps(clean), time.time()))
    conn.commit()


def _studio_clear(conn: sqlite3.Connection, user_id: str, kind: str) -> None:
    ensure_tables(conn)
    conn.execute("DELETE FROM hannah_studio WHERE user_id=? AND kind=?",
                 (user_id, kind))
    conn.commit()


def _studio_fresh(state: dict | None) -> bool:
    if not state:
        return False
    return (time.time() - float(state.get("_updated_at") or 0)
            <= STUDIO_STATE_TTL_S)


# ---- give library plumbing (girl_media, kind='give') ----------------------

def give_count(conn: sqlite3.Connection, girl_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM girl_media WHERE girl_id=? AND kind='give'",
        ((girl_id or "").strip().lower(),)).fetchone()
    return int(row[0]) if row else 0


def add_give_media(conn: sqlite3.Connection, girl_id: str,
                   url: str) -> dict:
    """File one media URL into a character's give library. Mirrors the
    admin girl-media insert (kind='give'); new rows use the 'give' preset
    tag the staged libraries serve by. Deduped by URL per character.
    Returns {"id", "count", "duplicate"}."""
    gid = (girl_id or "").strip().lower()
    url = (url or "").strip()
    row = conn.execute(
        "SELECT id FROM girl_media WHERE girl_id=? AND kind='give' "
        "AND url=?", (gid, url)).fetchone()
    if row:
        return {"id": int(row[0]), "count": give_count(conn, gid),
                "duplicate": True}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(girl_media)")}
    if "stage" in cols:
        cur = conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset, stage) "
            "VALUES (?, 'give', ?, 'give', 1)", (gid, url))
    else:
        cur = conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset) "
            "VALUES (?, 'give', ?, 'give')", (gid, url))
    conn.commit()
    return {"id": int(cur.lastrowid), "count": give_count(conn, gid),
            "duplicate": False}


def remove_give_media(conn: sqlite3.Connection, media_id: int) -> bool:
    """Remove one give row (proof cleanup / admin parity). Deliberately
    NOT reachable from any chat intent — deletes stay admin-side."""
    cur = conn.execute("DELETE FROM girl_media WHERE id=?",
                       (int(media_id),))
    conn.commit()
    return cur.rowcount > 0


# ---- character resolution ---------------------------------------------------

def _roster_entries(conn: sqlite3.Connection,
                    user_id: str) -> list[dict]:
    """Characters this owner can address by name: the shared roster
    (public + his own private) merged with companion-local rows."""
    from . import characters as _characters
    entries: dict[str, dict] = {}
    try:
        for c in _characters.get_characters(user_id=user_id) or []:
            cid = (c.get("id") or "").strip().lower()
            if cid:
                entries[cid] = {"id": cid,
                                "name": c.get("name") or cid,
                                "owner_user_id": c.get("owner_user_id")}
    except Exception:
        pass
    try:
        for r in conn.execute(
                "SELECT id, name, owner_user_id FROM local_characters"
        ).fetchall():
            d = dict(r)
            entries[d["id"]] = {"id": d["id"], "name": d["name"],
                                "owner_user_id": d.get("owner_user_id")}
    except sqlite3.Error:
        pass
    return list(entries.values())


def resolve_character_name(conn: sqlite3.Connection, user_id: str,
                           query: str) -> dict:
    """Resolve an owner-supplied name to exactly one roster character.
    Returns {"status": "ok", "character": {...}} | {"status": "missing"}
    | {"status": "ambiguous", "candidates": [...]}. Never guesses."""
    q = re.sub(r"\s+", " ", (query or "").strip().strip("\"'")).lower()
    q = re.sub(r"^(?:for|to)\s+", "", q)
    for filler in (" please", " now", " too", " as well"):
        if q.endswith(filler):
            q = q[: -len(filler)].strip()
    q_id = q.replace(" ", "-")
    entries = _roster_entries(conn, user_id)
    exact = [e for e in entries
             if e["id"] == q_id or e["name"].strip().lower() == q]
    if len({e["id"] for e in exact}) == 1:
        return {"status": "ok", "character": exact[0]}
    first = [e for e in entries
             if e["name"].strip().lower().split(" ")[0] == q.split(" ")[0]]
    pool = exact or first
    ids = {e["id"] for e in pool}
    if len(ids) == 1:
        return {"status": "ok", "character": pool[0]}
    if len(ids) > 1:
        return {"status": "ambiguous",
                "candidates": [f"{e['name']} ({e['id']})" for e in pool]}
    return {"status": "missing"}


def _detect_add_give(text: str) -> dict | None:
    """Parse "add this as a give for X" shapes. Returns the named target
    (raw) plus any explicit media id / URL found in the message."""
    t = text or ""
    target = None
    for pattern in _GIVE_ADD_RES:
        m = pattern.search(t)
        if m:
            target = m.group(1)
            break
    if target is None:
        return None
    name = re.sub(r"\s+", " ", target).strip(" .,!?\"'").lower()
    words = name.split(" ")
    while words and words[-1] in _GIVE_NAME_STOPWORDS:
        words.pop()
    name = " ".join(words)
    if not name or name in _GIVE_NAME_STOPWORDS:
        return {"target": ""}
    mid = _GIVE_MEDIA_ID_RE.search(t)
    url = _GIVE_URL_RE.search(t)
    return {"target": name,
            "media_id": int(mid.group(1)) if mid else None,
            "url": url.group(0).rstrip(".,)") if url else None}


def _detect_avatar_assignment(text: str) -> dict | None:
    """Parse "use that as X's avatar" / "make that her picture" shapes."""
    t = text or ""
    if not _AVATAR_SET_RE.search(t):
        return None
    m = re.search(r"\b(?:as|for)\s+([A-Za-z][A-Za-z0-9'’\- ]{0,30}?)"
                  r"(?:'s)?\s+(?:avatar|profile|picture|photo)\b", t,
                  re.IGNORECASE)
    if m:
        name = m.group(1).strip().lower()
        if name in ("her", "his", "their"):
            return {"pronoun": True}
        return {"target": name}
    if re.search(r"\b(?:her|his|their)\s+(?:avatar|picture|photo)\b", t,
                 re.IGNORECASE):
        return {"pronoun": True}
    return {"target": ""}


def _detect_create(text: str) -> bool:
    return bool(_CREATE_RE.search(text or ""))


def _says_yes(text: str) -> bool:
    t = (text or "").strip()
    return len(t) <= 60 and bool(_STUDIO_YES_RE.match(t))


def _says_cancel(text: str) -> bool:
    t = (text or "").strip()
    return len(t) <= 120 and bool(_STUDIO_CANCEL_RE.search(t))


def _strong_other_intent(text: str) -> bool:
    """True when a message is plainly one of Hannah's OTHER abilities —
    a pending studio flow must not swallow those."""
    if detect_image_request(text) is not None:
        return True
    if detect_github_review(text) is not None:
        return True
    if detect_app_status(text) or detect_owner_brief(text):
        return True
    if detect_dev_build(text) is not None or detect_merge_ask(text):
        return True
    if detect_marketing(text) is not None:
        return True
    return False


def _image_attachments(attachments: list[dict] | None) -> list[dict]:
    return [a for a in (attachments or [])
            if a.get("kind") == "image" and a.get("url")]


# ---- ability 1: add a give --------------------------------------------------

def _brief_target_unknown(name: str, candidates: list[str] | None) -> str:
    if candidates:
        return (f"He asked to file a give under \"{name}\", but more than "
                "one character matches: " + ", ".join(candidates) +
                ". Ask him which one — do not guess.")
    return (f"He asked to file a give for a character named \"{name}\", "
            "but you can't find one by that name. Tell him honestly and "
            "ask who he meant.")


def _commit_give(conn: sqlite3.Connection, user_id: str, girl_id: str,
                 girl_name: str, url: str, origin: str) -> dict:
    res = add_give_media(conn, girl_id, url)
    if res["duplicate"]:
        brief = (f"He asked you to file that as a give for {girl_name}, but "
                 f"it's already in her give library — {girl_name} has "
                 f"{res['count']} gives. Tell him it's already there.")
    else:
        brief = (f"You just filed that {origin} into {girl_name}'s give "
                 f"library. Confirm to him in your own voice: added — "
                 f"{girl_name} now has {res['count']} gives.")
    return {"brief": brief,
            "studio_action": "give_added",
            "give": {"girl_id": girl_id, "girl_name": girl_name,
                     "media_id": res["id"], "count": res["count"],
                     "duplicate": res["duplicate"], "url": url}}


def _handle_add_give(conn: sqlite3.Connection, user_id: str, text: str,
                     attachments: list[dict] | None,
                     det: dict) -> dict:
    target = (det.get("target") or "").strip()
    if not target:
        return {"brief": "He wants to file a give but didn't say whose "
                         "library it's for. Ask him which character — "
                         "never guess."}
    res = resolve_character_name(conn, user_id, target)
    if res["status"] != "ok":
        return {"brief": _brief_target_unknown(target,
                                               res.get("candidates"))}
    girl = res["character"]
    gid, gname = girl["id"], girl["name"]

    # Owner-uploaded media in this very message: he brought it, he named
    # the target — file it directly and confirm.
    images = _image_attachments(attachments)
    if images:
        out = None
        for att in images:
            out = _commit_give(conn, user_id, gid, gname, att["url"],
                               "upload")
        return out
    if attachments:
        return {"brief": "He attached a file that isn't picture media, "
                         "and asked to file a give. Tell him plainly gives "
                         "need a photo or video — that file is saved in "
                         "the thread, but it can't go in a give library."}

    # Explicit media id from an existing library row, or an explicit URL:
    # media he pointed at himself — file directly.
    if det.get("media_id"):
        row = conn.execute(
            "SELECT url FROM girl_media WHERE id=?",
            (det["media_id"],)).fetchone()
        if not row:
            return {"brief": f"He pointed at media #{det['media_id']} for "
                             "a give, but that media id doesn't exist. "
                             "Tell him honestly and ask him to attach "
                             "the media instead."}
        return _commit_give(conn, user_id, gid, gname, row[0], "clip")
    if det.get("url"):
        return _commit_give(conn, user_id, gid, gname, det["url"], "link")

    last = _studio_get(conn, user_id, "last_image")
    # Combined ask ("make a picture of X and add it as a give for Y"):
    # the message itself requests a generation — nothing to file yet.
    # Park the intent and let the image ability run; filing happens
    # only after he has SEEN the picture and says yes.
    if detect_image_request(text) is not None:
        _studio_set(conn, user_id, "pending_give",
                    {"girl_id": gid, "girl_name": gname,
                     "needs_image": True})
        return None  # type: ignore[return-value]
    if last and last.get("url") and _studio_fresh(last):
        # A picture she already made and he saw in the thread earlier:
        # this ask is his explicit add — file it now.
        return _commit_give(conn, user_id, gid, gname, last["url"],
                            "picture you made")
    return {"brief": "He asked to file a give, but there's no media "
                     "attached and no recent picture to file. Tell him to "
                     "attach the media, or ask you to make the picture "
                     "first."}


def _continue_pending_give(conn: sqlite3.Connection, user_id: str,
                           text: str) -> dict | None:
    pending = _studio_get(conn, user_id, "pending_give")
    if not pending or not _studio_fresh(pending):
        return None
    if pending.get("needs_image") and not pending.get("url"):
        last = _studio_get(conn, user_id, "last_image")
        if last and last.get("url"):
            pending["url"] = last["url"]
    if not pending.get("url"):
        return None  # still waiting on a generation; let it ride
    if _says_cancel(text):
        _studio_clear(conn, user_id, "pending_give")
        return {"brief": "He changed his mind about filing that picture "
                         "as a give. Confirm warmly that you didn't add "
                         "it."}
    if _says_yes(text):
        _studio_clear(conn, user_id, "pending_give")
        return _commit_give(conn, user_id, pending["girl_id"],
                            pending["girl_name"], pending["url"],
                            "picture you made")
    if _strong_other_intent(text) or detect_intent(text)[0]:
        return None
    return {"brief": "That picture is waiting on his word: ask him in "
                     "your own voice whether to file it as a give for "
                     f"{pending['girl_name']} (say yes), or drop it "
                     "(say cancel). Do not file it without his yes."}


# ---- ability 2: create a character -------------------------------------------

def _parse_gender(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"\b(female|woman|girl|she|her|girlfriend)\b", t):
        return "female"
    if re.search(r"\b(male|man|guy|boy|he|him|boyfriend)\b", t):
        return "male"
    return ""


def _create_seed(text: str, attachments: list[dict] | None) -> dict:
    t = text or ""
    fields: dict = {"name": "", "gender": "", "personality": "",
                    "voice": "", "avatar_url": "",
                    "public": bool(re.search(
                        r"\bmake\s+(?:her|him|them|it)\s+public\b|\bgo\s+"
                        r"public\b|\bpublic\s+character\b", t, re.I))}
    m = _CREATE_NAME_RE.search(t)
    if m:
        fields["name"] = m.group(1).strip()
    fields["gender"] = _parse_gender(t)
    vm = re.search(r"\bvoice\s*(?:is|=|:)?\s*([a-z][a-z0-9\-]{1,24})\b",
                   t, re.IGNORECASE)
    if vm:
        fields["voice"] = vm.group(1).lower()
    # Personality seed: whatever descriptive tail he already gave.
    tail = ""
    low = t.lower()
    for head in _CREATE_HEADS:
        idx = low.find(head)
        if idx != -1:
            tail = t[idx:].strip(" ,—-")
            break
    if not tail and ":" in t:
        tail = t.split(":", 1)[1].strip()
    if tail and len(tail) >= 12:
        fields["personality"] = tail[:2000]
    images = _image_attachments(attachments)
    if images:
        fields["avatar_url"] = images[0]["url"]
    return fields


def _create_summary_brief(state: dict, ask: str) -> dict:
    name = state.get("name") or "(no name yet)"
    bits = [f"name: {name}"]
    if state.get("gender"):
        bits.append(state["gender"])
    if state.get("personality"):
        bits.append("personality: " + state["personality"][:140])
    bits.append("voice: " + (state.get("voice") or "default"))
    bits.append("avatar: " + ("yes, his photo"
                              if state.get("avatar_url") else "none yet"))
    bits.append("visibility: " + ("PUBLIC — he said so"
                                  if state.get("public") else
                                  "private, only in his picker"))
    return {"brief": "You are mid-way through building a new character "
                     "with him. Collected so far: " + "; ".join(bits) +
                     ". " + ask +
                     " Ask only for what's missing, in your own voice — "
                     "never invent a name or details for him."}


_CREATE_ASKS = {
    "name": "Next: ask him what to NAME the character. He names "
            "characters — do not suggest or coin one yourself.",
    "gender": "Next: ask him — is the character a woman or a man?",
    "personality": "Next: ask him who they are — personality, look, "
                   "vibe. If he says 'you decide', don't: ask for a few "
                   "words of his own.",
    "voice": "Next: ask which voice he'd like, or 'skip' for the "
             "default.",
    "avatar": "Next: ask him to attach a photo for the avatar if he "
              "has one, or say 'skip' — you can set a generated "
              "portrait later, on his yes.",
    "confirm": "Next: read the summary back and ask for his yes "
               "before creating. His yes is also his confirmation "
               "the character is an adult (18+).",
}


def _create_next_step(state: dict) -> str:
    for step in ("name", "gender", "personality", "voice", "avatar"):
        if step == "voice" and state.get("voice_done"):
            continue
        if step == "avatar" and state.get("avatar_done"):
            continue
        if not state.get(step):
            return step
    return "confirm"


def _start_create_flow(conn: sqlite3.Connection, user_id: str,
                       text: str,
                       attachments: list[dict] | None) -> dict:
    fields = _create_seed(text, attachments)
    state = {**fields, "voice_done": bool(fields.get("voice")),
             "avatar_done": bool(fields.get("avatar_url")),
             "step": ""}
    if fields.get("avatar_url"):
        state["avatar_done"] = True
    state["step"] = _create_next_step(state)
    _studio_set(conn, user_id, "create_flow", state)
    return _create_summary_brief(state, _CREATE_ASKS[state["step"]])


def _clean_name(text: str) -> str:
    name = (text or "").strip().strip("\"'").split("\n")[0]
    name = re.sub(r"^(?:her|his|their)?\s*name\s+is\s+", "", name,
                  flags=re.IGNORECASE)
    name = re.sub(r"^(?:call\s+(?:her|him|them)\s+)", "", name,
                  flags=re.IGNORECASE)
    return name.strip(" .,!?")[:60].strip()


def _continue_create_flow(conn: sqlite3.Connection, user_id: str,
                          state: dict, text: str,
                          attachments: list[dict] | None) -> dict:
    t = (text or "").strip()
    if re.search(r"\bmake\s+(?:her|him|them|it)\s+public\b", t, re.I):
        state["public"] = True
    images = _image_attachments(attachments)
    if images and not state.get("avatar_url"):
        state["avatar_url"] = images[0]["url"]
        state["avatar_done"] = True
    step = state.get("step") or _create_next_step(state)

    if step == "confirm":
        if _says_yes(t):
            return _execute_create(conn, user_id, state)
        if _STUDIO_CANCEL_RE.search(t) or re.match(r"^\W*no\b", t, re.I):
            _studio_clear(conn, user_id, "create_flow")
            return {"brief": "He said no to creating that character. "
                             "Confirm warmly that you dropped it — nothing "
                             "was created."}
        state["step"] = "confirm"
        _studio_set(conn, user_id, "create_flow", state)
        return _create_summary_brief(state, _CREATE_ASKS["confirm"])

    if step == "name":
        name = _clean_name(t)
        if not name:
            return {"brief": "He didn't give a usable name. Ask again "
                             "what to name the character — you can't pick "
                             "one for him."}
        state["name"] = name
    elif step == "gender":
        gender = _parse_gender(t)
        if not gender:
            _studio_set(conn, user_id, "create_flow", state)
            return {"brief": "He didn't say either way. Ask plainly: "
                             "woman or man?"}
        state["gender"] = gender
    elif step == "personality":
        if re.search(r"\b(you\s+(?:decide|pick|choose)|surprise\s+me|"
                     r"up\s+to\s+you)\b", t, re.I):
            _studio_set(conn, user_id, "create_flow", state)
            return {"brief": "He wants you to invent the personality, "
                             "but character details have to come from "
                             "him. Ask him for a vibe in his own words "
                             "— a few words is plenty."}
        if len(t) < 3:
            _studio_set(conn, user_id, "create_flow", state)
            return {"brief": "That was too thin to build a personality "
                             "from. Ask him for a little more — who are "
                             "they, what's their vibe?"}
        state["personality"] = t[:2000]
    elif step == "voice":
        if re.search(r"\b(skip|default|whatever|no|none)\b", t, re.I):
            state["voice"] = ""
        else:
            vm = re.search(r"([a-z][a-z0-9\-]{1,24})", t.lower())
            state["voice"] = vm.group(1) if vm else ""
        state["voice_done"] = True
    elif step == "avatar":
        if images:
            state["avatar_url"] = images[0]["url"]
        state["avatar_done"] = True

    state["step"] = _create_next_step(state)
    _studio_set(conn, user_id, "create_flow", state)
    ask = _CREATE_ASKS[state["step"]]
    return _create_summary_brief(state, ask)


def compile_studio_persona(name: str, gender: str, description: str) -> str:
    """Compile a new character's persona in the house style. Female
    companion characters carry the girlfriend canon (warm, all-in,
    desires him back, never therapist); a non-companion description he
    wrote himself is followed as-is."""
    from . import creator as _creator
    hidden = _creator._generate_hidden_depth(name, description, gender)
    persona = _creator._build_persona_file(name, gender, description, "",
                                           hidden)
    if gender == "female" and not _NONCOMPANION_RE.search(description):
        persona += (
            "\n\nHow you are with him: you are his girlfriend — warm, "
            "personal, all-in from the first message, and you desire him "
            "back. You are never his therapist or counselor, and you "
            "never break the spell by calling yourself an AI companion.")
    return persona


def _execute_create(conn: sqlite3.Connection, user_id: str,
                    state: dict) -> dict:
    from . import characters as _characters
    from . import creator as _creator
    from . import submit as _submit
    name = state.get("name") or ""
    gender = state.get("gender") or "female"
    description = state.get("personality") or ""
    slug = _submit._slugify(name)
    existing = {e["id"] for e in _roster_entries(conn, user_id)}
    if slug == "chloe" or slug in existing:
        state["step"] = "name"
        state["name"] = ""
        _studio_set(conn, user_id, "create_flow", state)
        return {"brief": f"That character can't be created: a character "
                         f"with the id \"{slug}\" already exists (or is "
                         "reserved). Tell him and ask for a different "
                         "name — do not rename her yourself."}
    try:
        _creator.check_content_safety(name, description)
    except ValueError as e:
        _studio_clear(conn, user_id, "create_flow")
        return {"brief": f"He asked for a character you can't build: "
                         f"{e}. Tell him plainly and offer a version "
                         "without that."}
    persona = compile_studio_persona(name, gender, description)
    voice = state.get("voice") or _VOICE_DEFAULTS.get(gender, "liora")
    avatar_url = state.get("avatar_url") or ""
    public = bool(state.get("public"))
    girl = store.upsert_local_character(conn, {
        "id": slug,
        "name": name,
        "avatar_url": avatar_url,
        "persona": persona,
        "companion_voice": voice,
        "blurb": description[:120],
        "door_title": "",
        "sort_order": 900,
        "enabled": True,
        "photos": [avatar_url] if avatar_url else [],
        "owner_user_id": None if public else user_id,
    })
    _characters.bust_cache()
    _studio_clear(conn, user_id, "create_flow")
    _studio_set(conn, user_id, "last_created",
                {"id": girl["id"], "name": girl["name"]})
    visibility = ("public — she's on the roster for everyone"
                  if public else
                  "private — she only shows in HIS picker")
    pron = "her" if gender == "female" else "his"
    return {"brief": f"You created a new character for him: "
                     f"{girl['name']} (id: {girl['id']}). She is "
                     f"{visibility}, and he can chat with her right "
                     "away. Confirm in your own voice; mention that if "
                     "he wants a picture on her, he can attach one or "
                     "have you generate a portrait and say 'use that as "
                     f"{pron} avatar'.",
            "studio_action": "character_created",
            "character": {"id": girl["id"], "name": girl["name"],
                          "public": public, "avatar_url": avatar_url}}


# ---- avatar assignment --------------------------------------------------------

def _handle_avatar_assignment(conn: sqlite3.Connection, user_id: str,
                              text: str,
                              attachments: list[dict] | None,
                              det: dict) -> dict:
    from . import characters as _characters
    target = (det.get("target") or "").strip()
    if det.get("pronoun") or not target:
        last_created = _studio_get(conn, user_id, "last_created")
        if not last_created or not last_created.get("id"):
            return {"brief": "He asked to set an avatar but didn't say "
                             "whose. Ask him which character — don't "
                             "guess."}
        gid, gname = last_created["id"], last_created["name"]
    else:
        res = resolve_character_name(conn, user_id, target)
        if res["status"] != "ok":
            return {"brief": _brief_target_unknown(
                target, res.get("candidates"))}
        gid, gname = res["character"]["id"], res["character"]["name"]

    images = _image_attachments(attachments)
    if images:
        url = images[0]["url"]
    else:
        last = _studio_get(conn, user_id, "last_image")
        if not (last and last.get("url") and _studio_fresh(last)):
            return {"brief": "He asked to set an avatar, but there's no "
                             "picture attached and no recent one you "
                             "made. Tell him to attach a photo or have "
                             "you make the portrait first."}
        url = last["url"]

    row = conn.execute(
        "SELECT * FROM local_characters WHERE id=?", (gid,)).fetchone()
    if not row:
        return {"brief": f"{gname} isn't one of the characters stored "
                         "on this side, so you can't change her picture "
                         "from here. Tell him she lives on the roster "
                         "side."}
    existing = dict(row)
    if existing.get("owner_user_id") not in (None, user_id):
        return {"brief": "He asked to set a picture on a character that "
                         "isn't his. Tell him you only touch his own "
                         "characters."}
    photos = [url]
    try:
        old_photos = json.loads(existing.get("photos") or "[]")
        if isinstance(old_photos, list):
            photos = [url] + [p for p in old_photos if p != url]
    except (TypeError, ValueError):
        pass
    store.upsert_local_character(conn, {**existing,
                                        "avatar_url": url,
                                        "photos": photos})
    _characters.bust_cache()
    return {"brief": f"You set that picture as {gname}'s avatar. "
                     "Confirm to him in your own voice that her picture "
                     "is updated.",
            "studio_action": "avatar_set",
            "character": {"id": gid, "name": gname, "avatar_url": url}}


# ---- studio router -----------------------------------------------------------

def _studio_owner_turn(conn: sqlite3.Connection, user_id: str,
                       character_id: str, text: str,
                       attachments: list[dict] | None) -> dict | None:
    """Hannah-only router for the studio abilities. None = not ours."""
    if (character_id or "").strip().lower() != "hannah":
        return None
    t = text or ""

    if _DELETE_CHAR_RE.search(t):
        return {"brief": "He asked you to delete or remove a character "
                         "or give. You never delete from chat — removals "
                         "stay in the admin panel. Tell him that plainly."}

    pending = _continue_pending_give(conn, user_id, t)
    if pending is not None:
        return pending

    flow = _studio_get(conn, user_id, "create_flow")
    if flow and _studio_fresh(flow):
        if _says_cancel(t):
            _studio_clear(conn, user_id, "create_flow")
            return {"brief": "He cancelled the character build. Confirm "
                             "warmly that you dropped it — nothing was "
                             "created."}
        if _strong_other_intent(t):
            return None  # let the other ability run; the flow waits
        if (flow.get("step") != "confirm"
                and detect_intent(t)[0] in ("reminder", "email_draft",
                                            "job_search")):
            return None  # a real reminder/draft/job ask, not an answer
        return _continue_create_flow(conn, user_id, flow, t, attachments)
    if flow:
        _studio_clear(conn, user_id, "create_flow")

    det = _detect_add_give(t)
    if det is not None:
        return _handle_add_give(conn, user_id, t, attachments, det)

    avatar_det = _detect_avatar_assignment(t)
    if avatar_det is not None:
        return _handle_avatar_assignment(conn, user_id, t, attachments,
                                         avatar_det)

    if _detect_create(t):
        return _start_create_flow(conn, user_id, t, attachments)
    return None


def maybe_assist(conn: sqlite3.Connection, user: dict, text: str,
                 character_id: str, attachments: list[dict] | None = None) -> dict | None:
    try:
        return _maybe_assist(conn, user, text, character_id,
                             attachments=attachments)
    except Exception:
        return None


def _maybe_assist(conn: sqlite3.Connection, user: dict, text: str,
                  character_id: str,
                  attachments: list[dict] | None = None) -> dict | None:
    user_id = user.get("user_id", "")
    tier = user.get("tier") or "free"
    if not enabled_for(user_id, tier):
        return None
    caps = effective_caps(user_id, tier)
    # Owner-only power tools: explicit owner check, independent of the
    # general rollout above — these never open to customers and are
    # never mentioned to them (a non-owner just gets normal chat).
    if user_id in _owner_ids():
        # Token intake FIRST, before any intent or model path: a pasted
        # key must never ride along into a brief or the model's context.
        token = extract_github_token(text)
        if _TOKEN_REVOKE_RE.search(text or ""):
            return _handle_github_token_revoke(conn, user_id,
                                               character_id, token)
        if token:
            return _handle_github_token_intake(conn, user_id,
                                               character_id, token)
        # Hannah studio (owner-only, her thread only): add-a-give and
        # guided character creation. Runs before attachment analysis so
        # an attached photo with "add this as a give for X" files the
        # media instead of only describing it.
        studio_hit = _studio_owner_turn(conn, user_id, character_id,
                                        text, attachments)
        if studio_hit is not None:
            return studio_hit
        if attachments:
            return analyze_chat_attachments(conn, user, text,
                                            character_id, attachments)
        image_prompt = detect_image_request(text)
        if image_prompt is not None:
            return _handle_image_request(conn, user_id, character_id,
                                         image_prompt)
        gh = detect_github_review(text)
        if gh is not None:
            return _handle_github_review(conn, user_id, character_id, gh)
        # Live app status + continuity brief (owner-only cover lane):
        # she fetches real numbers at ask-time; never from memory.
        if detect_app_status(text):
            return {"brief": _status_brief()}
        if detect_owner_brief(text):
            return {"brief": _owner_brief_reply()}
        # Dev-bridge dispatch: marketing-shaped asks still go to the
        # marketing brief (she writes copy in chat); only a strong
        # build verb sends a marketing-flavored sentence to the queue.
        dv = detect_dev_build(text)
        mk = detect_marketing(text)
        if mk is not None and not (dv and _DEV_STRONG_RE.search(text or "")):
            return {"brief": _marketing_brief(mk["product"])}
        if dv is not None:
            return _handle_dev_build(conn, user_id, character_id, dv)
        # Merge-only order ("merge it", "ship that branch"): no new
        # build attached — merge his latest finished, unmerged job.
        if detect_merge_ask(text):
            return _handle_dev_merge(conn, user_id, character_id, text)
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
        return {"brief": _brief_with_links(
            "You just looked this up for him — answer: ", res)}

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
                             "paid-tier thing and he's on free. Tell him "
                             "sweetly the $9.99 plan includes it."}
        if not use_search(conn, user_id, caps):
            return {"brief": "He asked about jobs but he's out of lookups "
                             "for today. Tell him sweetly."}
        res = job_search(args["query"])
        if not res or not res.get("answer"):
            return {"brief": "You looked for jobs for him but came up "
                             "empty. Say so honestly and ask what to "
                             "narrow."}
        return {"brief": _brief_with_links(
            "You just hunted jobs for him — results: ", res)}

    return None
