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

import http.client
import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, gemini_keys, store

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


def _extract_repo(text: str) -> str | None:
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
        return {"repo": m.group(2)}
    return None


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
        token = (os.environ.get("GITHUB_TOKEN") or "").strip()
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


def _handle_github_review(user_id: str, character_id: str,
                          gh: dict) -> dict:
    repo = gh.get("repo")
    if not repo:
        return {"brief":
                "He asked you to review his code but didn't say which "
                "repo (his GitHub is mtthewwestfall). Ask him which one "
                "to dig into. Don't guess a repo and don't invent "
                "findings."}
    if not (os.environ.get("GITHUB_TOKEN") or "").strip():
        return {"brief":
                f"He asked you to really review his repo '{repo}', but "
                "your GitHub access isn't wired up yet — you can't "
                "actually see his code right now. Tell him straight, "
                "warm and short: the repo link isn't connected on your "
                "end yet, so no, you haven't looked, and you won't make "
                "anything up. No invented files, findings or issues."}
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
    caps = effective_caps(user_id, tier)
    # Owner-only power tools: explicit owner check, independent of the
    # general rollout above — these never open to customers and are
    # never mentioned to them (a non-owner just gets normal chat).
    if user_id in _owner_ids():
        gh = detect_github_review(text)
        if gh is not None:
            return _handle_github_review(user_id, character_id, gh)
        mk = detect_marketing(text)
        if mk is not None:
            return {"brief": _marketing_brief(mk["product"])}
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
