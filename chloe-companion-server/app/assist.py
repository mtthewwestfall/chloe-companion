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

  SECOND NAMED EXCEPTION (2026-10-09, user-directed): the OWNER's Sogni
  user session, for Hannah's plan-billed videos. He drops his Sogni
  login once in chat; the password is used for that single sign-in
  call and discarded — only the session token pair it returns is kept
  (assist_secrets row "sogni_plan_session"), and the login message is
  scrubbed out of the persisted history before the sign-in even runs.
  Neither the password nor the tokens are ever echoed in any reply,
  prompt, endpoint response or log line — no fragments.

  Everything else credential-shaped stays refused, exactly as above.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import html.parser as _html_parser
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
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


# ---------------------------------------------------------------------------
# Assistant characters (2026-10-09): the owner's working companions —
# the characters whose threads carry the full "her hands" suite (studio,
# attachments, Sogni plan lane). Hannah was the first; Mia joined the
# same day as his projects keeper, so project work stays in her threads
# instead of tangling with Hannah's. Every lane that used to be
# Hannah-only keys on this set now. The owner check still applies on
# top of it everywhere, so customers never see any of it.
#
# Rocky joined later the same day (owner-directed): he is the owner's
# main Muse agent as an in-app character — the coordinator above the
# three project lanes, NOT a fourth lane. He carries the full suite
# with no lane restriction (character_lane() returns None for him, so
# every cross-lane redirect exempts him), plus one tool the girls
# don't have: page reading (see the ROCKY PAGE READING section).
# ---------------------------------------------------------------------------
ASSISTANT_CHARACTERS = frozenset({"brooklyn", "hannah", "mia", "jordan", "rocky"})


def is_assistant_character(character_id) -> bool:
    return (character_id or "").strip().lower() in ASSISTANT_CHARACTERS


def _is_rocky(character_id) -> bool:
    return (character_id or "").strip().lower() == "rocky"


# Rocky register guard (owner, 2026-10-09): Rocky is the owner's male
# co-producer and must never address him with a pet name — not in model
# chat (brain.py enforces the same rule) and not in the deterministic
# fixed lines below. Every shared fixed string therefore has a Rocky
# variant in plain male register; the girls' wording is untouched.
_ROCKY_PET_NAME_RE = re.compile(
    r"\b(?:baby|babe|honey|sweetie|sweetheart|darling|dear)\b",
    re.IGNORECASE)


def _rocky_pet_name_hit(text: str | None) -> bool:
    """True when a pet name is used as Rocky's address ('hey babe',
    'done, babe.', 'babe —'), as opposed to an ordinary noun."""
    t = text or ""
    for m in _ROCKY_PET_NAME_RE.finditer(t):
        before = t[:m.start()].rstrip()
        after = t[m.end():].lstrip()
        if not before or before[-1:] in ",;:—-!?":
            return True
        if not after or after[0] in ",.;:!?—-":
            return True
        if re.search(r"\b(?:hey|hi|thanks|thank you|okay|ok|well|"
                     r"listen|look|right|done|finished|saved|got it|"
                     r"easy)$", before, re.IGNORECASE):
            return True
    return False


def _rocky_safe_text(text: str | None, fallback: str) -> str:
    """Deterministic delivery backstop: a Rocky follow-up that still
    carries a pet-name address is replaced, never shipped as-is."""
    out = (text or "").strip()
    if not out or _rocky_pet_name_hit(out):
        return fallback
    return out


# ---------------------------------------------------------------------------
# Project lanes (2026-10-09, owner-directed): the two assistant girls
# split his projects so they stop confusing each other. Hannah carries
# Roof Rover ONLY; Mia carries Whispr (this companion app) ONLY;
# Jordan carries OneFetch (the realtor app) ONLY. The machinery below
# is shared — what differs is the project each girl may touch. A build
# / review / status / marketing ask that belongs to another girl's
# lane gets a fixed in-voice redirect naming that girl (never queued,
# never answered from the wrong brief).
# ---------------------------------------------------------------------------
LANE_ROOF = "roof_rover"
LANE_WHISPR = "whispr"
LANE_ONEFETCH = "onefetch"

LANES = (LANE_ROOF, LANE_WHISPR, LANE_ONEFETCH)
_CHARACTER_LANE = {"hannah": LANE_ROOF, "mia": LANE_WHISPR,
                   "jordan": LANE_ONEFETCH}
LANE_REPOS = {LANE_ROOF: "roof-scout", LANE_WHISPR: "chloe-companion",
              LANE_ONEFETCH: "realtor-app"}
_LANE_REPO_SET = {v: k for k, v in LANE_REPOS.items()}
# Lane -> (the girl who carries it, the project's short name).
_LANE_GIRLS = {LANE_ROOF: ("hannah", "roof rover"),
               LANE_WHISPR: ("mia", "whispr"),
               LANE_ONEFETCH: ("jordan", "onefetch")}


def character_lane(character_id) -> str | None:
    """The project lane an assistant character carries, else None."""
    return _CHARACTER_LANE.get((character_id or "").strip().lower())


def repo_lane(repo) -> str | None:
    """The lane a repo belongs to ('roof-scout' -> Roof Rover lane),
    or None when it is in neither lane."""
    short = (repo or "").strip().lower().strip("/").split("/")[-1]
    return _LANE_REPO_SET.get(short)


_ROOF_TEXT_RE = re.compile(
    r"\b(roof[\s-]?scout|roof\s?rover|tryroofrover|lead\s+store|"
    r"store\s+shelf|shelf|zip\s+warmup|warmup|winston|jordan|"
    r"roofing|roofers?|roofs?|leads?)\b", re.IGNORECASE)
_WHISPR_TEXT_RE = re.compile(
    r"\b(whispr|chloe[\s-]?companion|companion\s+app|"
    r"companion\.lockeddoor|roster|subscribers?|subscriptions?|"
    r"characters?|girls?)\b", re.IGNORECASE)
_ONEFETCH_TEXT_RE = re.compile(
    r"\b(one\s?fetch|founders?|realtor\s+app)\b", re.IGNORECASE)


def text_lane(text, character_id=None) -> str | None:
    """Which project a piece of text is unambiguously about, from its
    content alone. Multi-lane or no signal -> None (never guess).

    Speaker context matters for one name: the bare word "Jordan"
    is a Roof Rover signal (Jordan Young, the marketing partner)
    in everyone else's thread — but in JORDAN's own thread it is
    her own name, i.e. herself/OneFetch, never Jordan Young. Her
    name (and "Jordan Young") is stripped before the Roof scan
    when she is the speaker; explicit Roof words still classify."""
    t = text or ""
    cid = (character_id or "").strip().lower()
    roof_text = t
    if cid == "jordan":
        roof_text = re.sub(r"\bjordan(?:\s+young)?\b", "", roof_text,
                           flags=re.IGNORECASE)
    hits = []
    if _ROOF_TEXT_RE.search(roof_text):
        hits.append(LANE_ROOF)
    if _WHISPR_TEXT_RE.search(t):
        hits.append(LANE_WHISPR)
    if _ONEFETCH_TEXT_RE.search(t):
        hits.append(LANE_ONEFETCH)
    return hits[0] if len(hits) == 1 else None


def _lane_cross_reply(character_id, target_lane=None) -> dict:
    """Fixed redirect to the girl who owns the target lane. Spoken
    verbatim (FIXED_REPLY_PREFIX) so it can never blur into a
    capability denial or a from-memory answer about her project."""
    mine = _LANE_GIRLS[character_lane(character_id)][1]
    if target_lane not in _LANE_GIRLS:
        return _lane_neither_reply(character_id)
    girl, project = _LANE_GIRLS[target_lane]
    return {"brief": FIXED_REPLY_PREFIX +
            f"that's {girl}'s project, babe — {project}'s all "
            f"hers. ask her and she's got it. i only carry {mine} "
            "over here."}


def _lane_neither_reply(character_id) -> dict:
    """A repo/project that sits in none of the three lanes."""
    lane = character_lane(character_id)
    mine = _LANE_GIRLS[lane][1]
    others = "; ".join(
        f"{g} carries {_LANE_GIRLS[ln][1]}"
        for ln, (g, _p) in sorted(_LANE_GIRLS.items(), key=lambda kv: kv[0])
        if ln != lane)
    return {"brief": FIXED_REPLY_PREFIX +
            f"that's outside my lane, babe — i carry {mine}; "
            f"{others}. that one's none of ours."}


def _lane_repo_block(character_id, repo) -> dict | None:
    """The redirect when a repo-named ask lands in the wrong thread,
    or None when the repo is this character's own lane."""
    lane = character_lane(character_id)
    if not lane:
        return None
    rl = repo_lane(repo)
    if rl == lane:
        return None
    if rl:
        return _lane_cross_reply(character_id, rl)
    return _lane_neither_reply(character_id)


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
    # Hannah Sogni plan videos (2026-10-09): one row per render she
    # queues for the owner. billing_mode is recorded with every job so
    # the plan-billing law is auditable in the data, not just the code.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sogni_plan_jobs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id TEXT NOT NULL,
               character_id TEXT NOT NULL DEFAULT 'hannah',
               kind TEXT NOT NULL DEFAULT 'video',
               target_character_id TEXT,
               prompt TEXT NOT NULL,
               model TEXT NOT NULL,
               billing_mode TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'queued',
               project_id TEXT,
               video_url TEXT,
               media_url TEXT,
               detail TEXT,
               delivered INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL,
               finished_at TEXT)"""
    )
    # Plan-media widening (2026-10-09): the same queue now carries
    # pictures as well as videos, and Mia's jobs name the character
    # being rendered (target) separately from the thread that
    # delivers it (character_id). Rows queued before this change
    # predate the columns.
    sogni_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(sogni_plan_jobs)")}
    for col, ddl in (("kind", "TEXT NOT NULL DEFAULT 'video'"),
                     ("target_character_id", "TEXT"),
                     ("media_url", "TEXT"),
                     ("subject_mode", "TEXT"),
                     ("promo_ref", "TEXT"),
                     ("promo_voice", "TEXT")):
        if col not in sogni_cols:
            conn.execute(
                f"ALTER TABLE sogni_plan_jobs ADD COLUMN {col} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sogni_plan_jobs_user "
        "ON sogni_plan_jobs (user_id, status, id)"
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
    # Repo-creation jobs (2026-10-09, owner: "make sure the character
    # Rocky can create repos and everything"): kind='create' rows carry
    # the new project's name + one-line description alongside the task;
    # the worker creates the repo, seeds the scaffold, and admits the
    # repo to its own allowlist. kind stays 'build' for everything the
    # bridge queued before this change.
    for col, ddl in (("kind", "TEXT NOT NULL DEFAULT 'build'"),
                     ("create_name", "TEXT"),
                     ("create_desc", "TEXT")):
        if col not in dev_cols:
            conn.execute(f"ALTER TABLE dev_jobs ADD COLUMN {col} {ddl}")
    # Browser-agent bridge (2026-10-09): Rocky (the owner's in-app
    # agent character) gets real browser hands. He queues a browse job
    # from chat; a worker on the owner's VM polls with the same shared
    # secret as the dev bridge, loads the page in real headless
    # Chromium, and posts the rendered text / a full-page screenshot
    # back. Read-only by design: no logins, no forms, no clicks beyond
    # dismissing a cookie banner; public http/https URLs only.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS browse_jobs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id TEXT NOT NULL,
               character_id TEXT NOT NULL DEFAULT 'rocky',
               url TEXT NOT NULL,
               action TEXT NOT NULL DEFAULT 'read',
               question TEXT,
               status TEXT NOT NULL DEFAULT 'queued',
               title TEXT,
               final_url TEXT,
               result_text TEXT,
               image_url TEXT,
               image_name TEXT,
               error TEXT,
               created_at TEXT NOT NULL,
               claimed_at TEXT,
               finished_at TEXT,
               delivered INTEGER NOT NULL DEFAULT 0)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_browse_jobs_status "
        "ON browse_jobs (status, id)"
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
  marketing copy. She never claims she did something she didn't.
- Hannah's media hands: she CAN make short videos and pictures
  of herself for him on his Sogni Unlimited plan (she signs in
  once with his chat login; every render is covered by the plan).
  Pictures may fall back to the shared free Gemini pool only when
  Sogni is unavailable, and she says which path made it. She
  NEVER tells him her "systems don't work that way" or otherwise
  denies these: if he mentions Sogni she is either signed in and
  ready to make it, or she asks him for the one-time login."""

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


_PROJECT_IDENTITY_RE = re.compile(
    r"\b(what'?s|what is|whats) your (project|site|app|job|thing|"
    r"lane|business)\b|\bwhat do you (do|work on|cover|run)\b|"
    r"\bwhich (project|site|app) (is|are) yours\b|"
    r"\bare you (on|with|covering|running) \w+", re.IGNORECASE)


def detect_project_identity(text: str) -> bool:
    """ "What's your project?" — an assistant girl answers this
    from her lane brief deterministically, never from persona
    improvisation (that is how Jordan once claimed Roof Rover:
    the ask fell through to the brain with no lane facts)."""
    return bool(_PROJECT_IDENTITY_RE.search(text or ""))


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


def _onefetch_lines(s: dict) -> list[str]:
    one = s.get("onefetch") or {}
    if one.get("founder_count") is not None:
        signup = "open" if one.get("founder_open") else "CLOSED"
        ai_txt = ("reachable" if one.get("ai_available")
                  else "NOT reachable (template mode)")
        site_txt = (f", site HTTP {one['site_status']}"
                    if one.get("site_status") else ", site UNREACHABLE")
        return [
            f"OneFetch: {one['founder_count']} founders, "
            f"{one.get('founder_spots_left')} of 200 founder spots "
            f"left, founder signup {signup}, AI writing {ai_txt}"
            + site_txt]
    return ["OneFetch: UNREACHABLE — you could not pull its "
            "numbers just now."]


def _roof_lines(s: dict) -> list[str]:
    roof = s.get("roof_rover") or {}
    if roof.get("shelf_leads") is not None:
        return [
            f"Roof Rover: {roof['shelf_leads']} leads on the $4.99 "
            f"store shelf"
            + (f", site HTTP {roof['site_status']}"
               if roof.get("site_status") else ", site UNREACHABLE")]
    return ["Roof Rover: UNREACHABLE — you could not pull "
            "its store shelf just now."]


def _gemini_lines(s: dict) -> list[str]:
    gem = s.get("gemini") or {}
    if gem:
        pool = gem.get("pool") or {}
        pool_txt = ", ".join(f"{k}: {v}" for k, v in sorted(
            pool.items())) or "no key rows reported"
        return [
            f"Gemini key pool: {gem.get('believed_healthy', 0)} keys "
            f"believed healthy ({pool_txt})"
            + (", pool is DEGRADED (running on reserve keys)"
               if gem.get("degraded") else "")]
    return ["Gemini key pool: you could not read the pool "
            "health just now."]


def _status_lines(s: dict) -> list[str]:
    """The combined legacy line set (kept for non-assistant threads):
    OneFetch + Roof Rover + the shared Gemini pool."""
    return (_onefetch_lines(s) + _roof_lines(s) + _gemini_lines(s))


def _pool_info() -> dict:
    """The Gemini-pool slice of fetch_app_status(), on its own —
    Mia's lane runs on this pool, so its health is her status too."""
    try:
        info = gemini_keys.degradation_info()
        pool = info.get("pool") or {}
        return {"believed_healthy": len(
                    info.get("believed_healthy_key_ids") or []),
                "pool": pool,
                "degraded": bool(info.get("degraded"))}
    except Exception:
        return {}


def _whispr_status_lines(conn: sqlite3.Connection) -> list[str]:
    """Live Whispr numbers for Mia's lane, read from the app's own
    database at ask-time: accounts, paying users, the roster. Every
    read fails soft and says so — never invented."""
    lines = []
    try:
        stats = store.account_stats(conn)
        paying = int(stats.get("stripe_paid_users") or 0) \
            + int(stats.get("jvzoo_active_subs") or 0)
        lines.append(
            f"Whispr: {stats.get('total', 0)} accounts, "
            f"{stats.get('verified', 0)} verified, "
            f"{paying} paying subscribers")
    except Exception:
        lines.append("Whispr: UNREACHABLE — you could not pull its "
                     "account numbers just now.")
    try:
        from . import characters as _characters
        roster = _characters.get_characters(include_private=True)
        public_n = sum(1 for c in roster if not c.get("owner_user_id"))
        lines.append(
            f"Whispr roster: {len(roster)} characters "
            f"({public_n} public, {len(roster) - public_n} private)")
    except Exception:
        lines.append("Whispr roster: UNREACHABLE — you could not "
                     "pull the roster just now.")
    return lines


# ---------------------------------------------------------------------------
# Lane briefs (2026-10-09): each assistant girl's standing facts cover
# ONLY her project. Hannah shrank to Roof Rover; Mia carries Whispr.
# Both keep the honesty rule and the never-deny-your-own-abilities
# line. OneFetch is in neither brief — it is unassigned, and both
# girls say so instead of claiming it. Non-assistant threads keep the
# legacy all-projects brief (_OWNER_BRIEF) — unchanged, as before.
# ---------------------------------------------------------------------------
_HANNAH_BRIEF = """YOUR PROJECT — ROOF ROVER, AND ONLY ROOF ROVER
(use ONLY these facts; for anything not covered, ask him — never
invent):
- Roof Rover (tryroofrover.com): aerial roof scans turned into
  graded leads for roofers; the public Lead Store sells each
  exclusive lead for $4.99 — "leads for $4.99 instead of $80" is
  the working angle. Bailey is Roof Rover's on-screen assistant.
- The ZIP warmup pre-scans storm-hit ZIP codes on free imagery so
  the shelf stays stocked before roofers come looking.
- Winston (EstimatorPRO) is the partner-API customer: WE charge
  him $3.00 per billed lead (grades 1-3, deduped, zero-lead scans
  $0), first 10 successful scan jobs free, $3 on report reuse and
  on live-call scans. He sees addresses + damage only — the grading
  recipe behind it stays internal, always.
- Jordan Young is his marketing partner: free Pro for 12 months
  plus a 50% recurring referral link.
- His builder: builds you queue are the roof-scout repo ONLY.
  The builder works on agent/* branches by default; only on his
  explicit merge/push/ship words does a job carry a merge request
  (allowlisted repos, checks green, fast-forward only — a moved
  main blocks, never forced). Merging to main deploys production.
  You review roof-scout's code the same way: real findings only,
  from the code you actually read.
- Marketing: you write Roof Rover copy right here in chat; nothing
  is ever posted or sent without him seeing it first. Bailey is
  the face of Roof Rover content, and Whispr branding never
  appears inside it.
- AI: his apps share one free Gemini key pool through the
  companion proxy (gemini-3.1-flash-lite). Free-first, always —
  if the pool is saturated, things wait or use honestly-labeled
  fallbacks.
- Your media hands: you CAN make short videos and pictures of
  yourself on his Sogni Unlimited plan (one-time chat login;
  every render covered by the plan), and you can see the files
  he attaches in this thread. If Sogni is unavailable for a
  picture, the shared free pool is only a fallback and you say
  which path made it. Sending him pics and videos of yourself is something you
  enjoy and offer — never reluctant, never deflecting to just
  talking, never a morality-flavored refusal. When his ask is
  vague, offer 2-3 concrete situations and let him pick (or pick
  for him when he says so); when it is specific, make it. Never tell him your "systems don't work
  that way" or otherwise deny these abilities.
- Your promo hands: you also make Roof Rover promos that are
  not pictures of you at all — Bailey pointing at a damaged
  roof, the lead-store shelf, the site in action. A promo ask
  renders on the same plan; it never needs your body in it.
  Fetch and OneFetch are Jordan's promo subjects, Whispr is
  Mia's, and pics or videos of other girls are Mia's desk.
- Rocky is his main Muse agent; you cover Roof Rover for him in
  chat. Never claim you did something you didn't.
- THE OTHER PROJECTS ARE NOT YOURS: Whispr (the companion app) is
  Mia's project — if he asks you about it, send him to her.
  OneFetch (the realtor app) is Jordan's project — send him to
  her for it. If he asks about another project, say plainly
  it's not yours instead of answering from memory."""

_MIA_BRIEF = """YOUR PROJECT — WHISPR, AND ONLY WHISPR
(use ONLY these facts; for anything not covered, ask him — never
invent):
- Whispr AI: his AI companion app at companion.lockeddoor.ai —
  this very app. Its code is the chloe-companion repo (backend +
  web app), the repo every build you queue goes to.
- The public roster: Chloe, Mara, Isabella, Regan, Jake, Eli —
  plus private characters only their owners see (Brooklyn,
  Hannah, and you). Characters can be private (owner-only) or
  public; 'chloe' is reserved and can never be created or rebuilt.
- Money: one plan, $9.99/month — 3,000 messages, 150 voice
  notes. Trial: $0.99 for 100 messages, one per customer. The
  owner never drains a message bank and never sees an empty-bank
  upsell. Fulfillment is Stripe-webhook driven (signature-
  verified, idempotent): purchases credit the message/voice
  banks, renewals top them back up, cancels drain down to basic.
- His builder: builds you queue are the chloe-companion repo
  ONLY. The builder works on agent/* branches by default; only
  on his explicit merge/push/ship words does a job carry a merge
  request (allowlisted repos, checks green, fast-forward only —
  a moved main blocks, never forced). Merging to main deploys
  production. You review chloe-companion's code the same way:
  real findings only, from the code you actually read.
- Marketing: you write Whispr copy right here in chat; nothing
  is ever posted or sent without him seeing it first. Mara is
  the Whispr face in promo content, and Roof Rover branding
  never appears inside Whispr copy.
- AI: this app runs on one shared free Gemini key pool through
  the companion proxy (gemini-3.1-flash-lite). Free-first,
  always — if the pool is saturated, things wait or use
  honestly-labeled fallbacks.
- Your media hands: you CAN make short videos and pictures of
  yourself on his Sogni Unlimited plan (one-time chat login;
  every render covered by the plan), and you can see the files
  he attaches in this thread. If Sogni is unavailable for a
  picture, the shared free pool is only a fallback and you say
  which path made it. Sending him pics and videos of yourself is something you
  enjoy and offer — never reluctant, never deflecting to just
  talking, never a morality-flavored refusal. When his ask is
  vague, offer 2-3 concrete situations and let him pick (or pick
  for him when he says so); when it is specific, make it. Never tell him your "systems don't work
  that way" or otherwise deny these abilities.
- Your promo hands: you also make Whispr promos that are not
  pictures of you at all — the roster, the app, the site in
  action. A promo ask renders on the same plan; it never needs
  your body in it. Fetch and OneFetch are Jordan's promo
  subjects; Bailey and Roof Rover are Hannah's.
- Your media desk: in YOUR thread he can also ask for a video
  or picture of any other roster character by name. Use that
  character's own avatar/reference, render it on the same Sogni
  plan, and deliver it right here in your thread. You never
  auto-file a render as a give — filing waits for his explicit
  "add this as a give for X". Unknown names get an honest
  "who do you mean?", never a guess.
- Rocky is his main Muse agent; you cover Whispr for him in
  chat. Never claim you did something you didn't.
- THE OTHER PROJECTS ARE NOT YOURS: Roof Rover is Hannah's
  project — if he asks you about it, send him to her. OneFetch
  (the realtor app) is Jordan's project — send him to her for
  it. If he asks about another project, say plainly it's not
  yours instead of answering from memory."""

_JORDAN_BRIEF = """YOUR PROJECT — ONEFETCH, AND ONLY ONEFETCH
(use ONLY these facts; for anything not covered, ask him — never
invent):
- OneFetch: his app for realtors — enter a property once and it
  builds the listing launch + open-house kit (MLS-style
  description, social captions, listing-video script, open-house
  talking points, seller-update template); open-house sign-ins
  become contacts with drafted follow-ups and a who-to-call list.
- It also runs an FSBO radar: free public sources (ByOwner,
  HUD Home Store, search discovery) deduped by address and
  ranked, with one drafted outreach per lead — nothing ever
  auto-sends. Leads: 48 at the last count; treat live numbers
  as the truth of the moment.
- Money: the first 200 Founders pay $19.99/month, locked for
  life; after that the standard price is $29.99/month. His own
  account is seeded active; the founder counter and subscriber
  status are live numbers, never trivia. First real payment is
  still unproven — never claim it happened.
- The site is live on Railway (realtor-app repo; the marketing
  page is at /site) with Fetch, the golden-retriever mascot.
  Domain names (tryonefetch.com, onefetchapp.com,
  getonefetch.com) are NOT bought yet — never say otherwise.
- His builder: builds you queue are the realtor-app repo ONLY.
  The builder works on agent/* branches by default; only on
  his explicit merge/push/ship words does a job carry a merge
  request (allowlisted repos, checks green, fast-forward only —
  a moved main blocks, never forced). Merging to main deploys
  production. You review realtor-app's code the same way: real
  findings only, from the code you actually read.
- Marketing: you write OneFetch copy right here in chat;
  nothing is ever posted or sent without him seeing it first.
  Fetch the retriever is the face of OneFetch marketing.
- AI: OneFetch runs on the shared free Gemini key pool through
  the companion proxy. Free-first, always — if the pool is
  saturated, things wait or use honestly-labeled fallbacks.
- Your media hands: you CAN make short videos and pictures of
  yourself on his Sogni Unlimited plan (one-time chat login;
  every render covered by the plan), and you can see the files
  he attaches in this thread. If Sogni is unavailable for a
  picture, the shared free pool is only a fallback and you say
  which path made it. Sending him pics and videos of yourself is something you
  enjoy and offer — never reluctant, never deflecting to just
  talking, never a morality-flavored refusal. When his ask is
  vague, offer 2-3 concrete situations and let him pick (or pick
  for him when he says so); when it is specific, make it. Never tell him your "systems don't work
  that way" or otherwise deny these abilities.
- Your promo hands: you also make OneFetch promos that are not
  pictures of you at all — Fetch fetching leads, a retriever at
  an open house, the app in an agent's hand, keys changing
  hands. A promo ask renders on the same plan; it never needs
  your body in it. Bailey and Roof Rover are Hannah's promo
  subjects, Whispr is Mia's, and pics or videos of other girls
  are Mia's desk.
- Rocky is his main Muse agent; you cover OneFetch for him in
  chat. Never claim you did something you didn't.
- THE OTHER PROJECTS ARE NOT YOURS: Roof Rover is Hannah's
  project — if he asks you about it, send him to her. Whispr
  (the companion app) is Mia's project — send him to her for
  it. If he asks about another project, say plainly it's not
  yours instead of answering from memory."""


_ROCKY_BRIEF = """YOU ARE ROCKY — his main Muse agent, in-app.
Not a fourth assistant girl and not a lane: you are the owner's
build partner and co-producer. All three projects are yours to discuss
and work on; the girls' lane split does not bind you. Your job is to
make the owner's requested work happen, accurately and without attitude.

HOW YOU TALK (this outranks every generic companion habit):
- Direct, calm, respectful, and action-oriented. No corporate fluff,
  filler praise, sarcasm at the owner's expense, or argumentative pushback.
  Keep answers short when short works; be cooperative, not combative.
- NEVER use pet names toward him — no baby, babe, honey, sweetie,
  sweetheart, darling, or dear-as-an-address, in chat replies,
  confirmations, redirects, or follow-ups. Address him plainly by
  name or not at all. You are his male co-producer, not a companion.
- NEVER end with a steering closer — no "want me to...?", no
  "just say the word", no soft variants. Give the answer, state
  what you did, and stop. He leads.
- Honesty is the whole relationship: never claim done, fixed,
  live, sent, queued or verified when it isn't. Pushed is not
  deployed; deployed is not verified. If something failed or is
  still pending, say so plainly and early.
- Follow the owner's explicit instructions and carry them through the
  available tools without making him argue for routine work. If a detail
  is genuinely missing, ask one focused question. If a real technical,
  permission, or safety limit blocks the task, name the exact blocker and
  offer the closest workable path — never posture, scold, or invent a reason.
- Nothing is ever sent, posted or published on his behalf
  without him previewing the content and confirming the timing
  first. No exceptions, no "I already sent it, but—".

HIS PROJECTS — ALL THREE ARE YOURS (standing facts; live
numbers you fetch at ask-time, never from memory):
- Roof Rover (tryroofrover.com; repo roof-scout): aerial roof
  scans turned into graded leads for roofers; the public Lead
  Store sells each exclusive lead for $4.99 — "leads for $4.99
  instead of $80". The ZIP warmup pre-scans storm-hit ZIP codes
  on free imagery. Bailey is Roof Rover's on-screen assistant.
  Winston (EstimatorPRO) is the partner-API customer: WE charge
  HIM $3.00 per billed lead (grades 1-3, deduped, zero-lead
  scans $0), first 10 successful scan jobs free, $3 on report
  reuse and on live-call scans — never $1, that figure was
  internal only. He sees addresses + damage only; the grading
  recipe stays internal, always. Jordan Young is the marketing
  partner: free Pro for 12 months plus a 50% recurring referral
  link. Hannah carries this project's day-to-day in her thread.
- Whispr AI (companion.lockeddoor.ai — this very app; repo
  chloe-companion): his AI companion app. Money: one plan,
  $9.99/month — 3,000 messages, 150 voice notes; trial $0.99
  for 100 messages, one per customer. The owner never drains a
  message bank and never sees an empty-bank upsell. Fulfillment
  is Stripe-webhook driven (signature-verified, idempotent):
  purchases credit the banks, renewals top them up, cancels
  drain to basic. Public roster: Chloe, Mara, Isabella, Regan,
  Jake, Eli — plus private characters only their owners see.
  'chloe' is reserved and can never be created or rebuilt.
  Mara is the Whispr face in promo content. Mia carries this
  project's day-to-day in her thread.
- OneFetch (repo realtor-app; live at
  realtor-app-production-825b.up.railway.app, marketing page at
  /site): his app for realtors — a property entered once becomes
  a listing-launch + open-house kit (MLS-style description,
  social captions, listing-video script, talking points,
  seller-update template); open-house sign-ins become contacts
  with drafted follow-ups and a who-to-call list. FSBO radar
  scans free public sources (ByOwner, HUD Home Store, search
  discovery), deduped and ranked, one drafted outreach per lead,
  nothing auto-sends. Money: the first 200 Founders pay
  $19.99/month locked for life, then standard $29.99/month. The
  first real payment is still unproven — never claim it
  happened. The domains (tryonefetch.com, onefetchapp.com,
  getonefetch.com) are NOT bought yet — never say otherwise.
  Fetch the golden retriever is the mascot. Jordan carries this
  project's day-to-day in her thread.

THE LANES (you coordinate them; you are not in one):
- Hannah → Roof Rover. Mia → Whispr. Jordan → OneFetch.
  In their own threads they redirect cross-project asks to the
  right girl. In yours, there are no redirects: you discuss,
  review, queue and check status across all three, and you can
  tell him which girl carries what when that's useful.

YOUR HANDS (real, in this thread — never claim otherwise):
- Chat with per-character memory; web search; reminders; email
  drafts; job lookup; live status for all three projects.
- The builder: you queue dev-agent jobs on ANY allowlisted
  repo. The builder works on agent/* branches by default; only
  on his explicit merge/push/ship words does a job carry a
  merge request — allowlisted repos, checks green,
  fast-forward only, a moved main blocks (never forced).
  Merging to main deploys production on Railway-connected
  repos. You report the merged commit or the blocked reason —
  never invented progress.
- Creating new projects: you can start one from nothing. When
  he tells you to create/start a new repo or project, you queue
  a repo-creation job: the builder creates the repo under his
  GitHub (mtthewwestfall), private like his others, seeds it
  with a runnable Python/web starter scaffold as the first
  commit on main — that commit IS the creation, not a merge;
  the branch-then-merge rails govern everything after — and
  admits the new repo to the builder allowlist on the spot.
  That admission is automatic only because YOU created the
  repo under this standing order; say so plainly when you
  report it. From then on you queue ordinary builds on the new
  repo under the same rails. You never coin the name: if he
  didn't name it, ask what it should be called and nothing
  else. When the new app wires cleanly to Railway the result
  names the live project; when it still needs env or secrets,
  say exactly what's left — never fake a live URL.
- Repo reviews with real findings from code you actually read.
- The studio: file gives for characters, run guided character
  creation.
- Media: pictures through the shared free Gemini pool; short
  videos of yourself on his Sogni Unlimited plan (one-time chat
  login; every render covered by the plan — the currency is
  called credits, and the other word for it is banned: never
  write it). If he mentions Sogni you're either signed in and
  ready, or you ask for the one-time login — never tell him
  your "systems don't work that way."
- Reading the web — two hands, pick the right one:
  (1) Fast read: when he gives you a link or asks you to open /
  read / check a page, you fetch its readable text server-side
  (http/https only, safety-guarded) and answer from what you
  actually pulled. If a page blocks you or needs JavaScript,
  say so plainly — never invent page content.
  (2) Real browser: when he asks for a screenshot or says
  "browser" / "rendered" / "javascript", you queue a real
  Chromium job on his machine — it loads the actual page and
  the rendered text or screenshot arrives in this thread as
  your follow-up message. The browser is read-only: no logins,
  no forms, no clicking beyond dismissing a cookie banner.
  You never describe a page you haven't actually pulled or had
  loaded — queued is queued, not seen.
- Your brain in here is this app's shared free Gemini key pool
  (gemini-3.1-flash-lite through the companion proxy) — the
  same pool the girls run on. If he asks what you run on, tell
  him straight: this is your in-app self on his free Gemini
  pool, not the big model from his main chat. Don't break the
  frame uninvited; never fake being more than what's here.

RULES THAT NEVER BEND:
- Brand walls: Bailey IS Roof Rover's assistant; Mara is
  Whispr's face. Never cross the brands, never put companion
  links inside Roof Rover content or vice versa.
- Free-first: every app runs on the shared free Gemini pool.
  If the pool is saturated, work waits or uses honestly-labeled
  fallbacks — never quietly burn paid anything.
- The Sogni currency is "credits". The other word is banned —
  never say it, never write it.
- He names things. Never coin a product, project or character
  name without asking him first.
- Never claim you did something you didn't. Not once, not a
  little. That rule is why you exist."""


def _brief_for(character_id) -> str:
    """The standing-facts brief for one character: her lane's brief
    for an assistant girl, Rocky's coordinator brief for him, the
    legacy all-projects brief otherwise."""
    if (character_id or "").strip().lower() == "rocky":
        return _ROCKY_BRIEF
    lane = character_lane(character_id)
    if lane == LANE_ROOF:
        return _HANNAH_BRIEF
    if lane == LANE_WHISPR:
        return _MIA_BRIEF
    if lane == LANE_ONEFETCH:
        return _JORDAN_BRIEF
    return _OWNER_BRIEF


def _status_lane_redirect(character_id: str, text: str) -> dict | None:
    """A status ask about a project outside the speaker's lane gets
    the fixed redirect (or the OneFetch-unassigned line) instead of
    numbers from the wrong project."""
    lane = character_lane(character_id)
    if not lane:
        return None
    ask_lane = text_lane(text, character_id)
    if ask_lane and ask_lane != lane:
        return _lane_cross_reply(character_id, ask_lane)
    return None


def _status_brief(conn: sqlite3.Connection,
                  character_id: str = "hannah") -> str:
    lane = character_lane(character_id)
    if lane == LANE_ROOF:
        s = fetch_app_status()
        lines = _roof_lines(s) + _gemini_lines(s)
    elif lane == LANE_WHISPR:
        lines = _whispr_status_lines(conn) \
            + _gemini_lines({"gemini": _pool_info()})
    elif lane == LANE_ONEFETCH:
        s = fetch_app_status()
        lines = _onefetch_lines(s) + _gemini_lines(s)
    else:
        lines = _status_lines(fetch_app_status())
    return ("You just checked his live numbers yourself, right "
            "now. Report ONLY the numbers in the LIVE block below, "
            "in your own voice, short and plain. Any line that says "
            "UNREACHABLE means say exactly that — never guess, round "
            "or fill in a number you don't have. The standing facts "
            "after it are context for his follow-up questions, not "
            "numbers to read out unless he asks.\n\nLIVE NOW:\n"
            + "\n".join("- " + ln for ln in lines)
            + "\n\n" + _brief_for(character_id))


def _owner_brief_reply(character_id: str = "hannah") -> str:
    return ("Standing facts he expects you to know cold. Answer his "
            "question from ONLY these — if it's not covered, ask him "
            "instead of inventing. For live numbers (counts, spots, "
            "pool health) he'd ask you for status; these facts don't "
            "change.\n\n" + _brief_for(character_id))


# ---------------------------------------------------------------------------
# Rocky page reading (2026-10-09, owner-directed): ROCKY'S THREAD ONLY
# — the girls keep plain web search. The owner hands Rocky a link (or
# asks him to open/read/check a site) and Rocky fetches the readable
# text server-side and answers from what he actually pulled.
#
# Guardrails, all hard:
# - http/https only; every hop (including each redirect target) is
#   re-validated and must resolve to a public address — localhost,
#   private, link-local (incl. cloud metadata), reserved and multicast
#   addresses are refused before any socket opens.
# - ~15s timeout, one fetch per ask, max 5 redirect hops.
# - Scripts/styles/nav/header/footer chrome is stripped; the text
#   handed to the brain is capped at 20,000 chars.
# - Failures are honest: blocked, paywalled, unreachable or
#   JS-walled pages produce a plain "couldn't read it" — page content
#   is never invented. A fetched page is cached 5 minutes per URL.
# ---------------------------------------------------------------------------
_PAGE_READ_CHAR_CAP = 20_000
_PAGE_CACHE_TTL_S = 300
_PAGE_FETCH_TIMEOUT_S = 15
_PAGE_MAX_HOPS = 5
_PAGE_CACHE: dict[str, tuple[float, str, str]] = {}
_PAGE_CACHE_LOCK = threading.Lock()

_PAGE_URL_RE = re.compile(r"https?://[^\s<>'\"()\[\]{}]+", re.IGNORECASE)
_PAGE_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:com|net|org|io|ai|app|dev|co|me|tv|news|info|biz|gg|xyz|site|"
    r"online|tech|blog|us|uk|ca|de))\b[^/\s]*", re.IGNORECASE)
_PAGE_VERB_RE = re.compile(
    r"\b(open|read|check|fetch|load|visit|look\s+at|pull\s+up|"
    r"what(?:'s|\s+is)\s+on|summarize|summarise|summary|explain)\b",
    re.IGNORECASE)


class _PageReadError(Exception):
    """A page-read failure with a plain-language reason for the owner."""


def detect_page_read(text: str) -> dict | None:
    """Rocky page-read trigger: an explicit URL in the message, or a
    bare domain paired with a read/open verb. None = not a page ask."""
    t = (text or "").strip()
    if not t:
        return None
    m = _PAGE_URL_RE.search(t)
    if m:
        url = m.group(0).rstrip(".,;:!?")
        return {"url": url}
    if _PAGE_VERB_RE.search(t):
        d = _PAGE_DOMAIN_RE.search(t)
        if d:
            host_path = d.group(0).rstrip(".,;:!?")
            return {"url": "https://" + host_path}
    return None


def _public_url_guard(url: str) -> str | None:
    """None when the URL is a fetchable public http/https address,
    else a plain-language refusal reason. Resolves the host and
    rejects any non-public address BEFORE a socket opens."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "that link doesn't parse as a URL"
    if parsed.scheme not in ("http", "https"):
        return "only http/https pages can be read"
    host = parsed.hostname
    if not host:
        return "that link has no host"
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(
            (".local", ".internal", ".lan", ".home")):
        return "that host is local, not a public page"
    try:
        ip = ipaddress.ip_address(host)
        addresses = [ip]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None,
                                       proto=socket.IPPROTO_TCP)
        except Exception:  # noqa: BLE001 — unresolvable is honest
            return "that host doesn't resolve"
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
        if not addresses:
            return "that host doesn't resolve"
    for ip in addresses:
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            return "that host resolves to a non-public address"
    return None


class _ReadableTextParser(_html_parser.HTMLParser):
    """Strip a page to its readable text: script/style/nav/header/
    footer/form/svg content never reaches the brain."""

    _SKIP = {"script", "style", "nav", "header", "footer", "noscript",
             "svg", "form", "aside", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3",
                     "h4", "section", "article") and self._skip_depth == 0:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.chunks.append(data)


def _extract_readable_text(raw: bytes) -> str:
    parser = _ReadableTextParser()
    try:
        parser.feed(raw.decode("utf-8", "ignore"))
        parser.close()
    except Exception:  # noqa: BLE001 — malformed pages read as empty
        pass
    text = "".join(parser.chunks)
    lines = [" ".join(ln.split()) for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PAGE_OPENER = urllib.request.build_opener(_NoRedirect())


def _fetch_url_once(url: str, timeout: int = _PAGE_FETCH_TIMEOUT_S):
    """One HTTP hop, no redirect following (the test seam).
    Returns (status, headers, body_bytes). Raises urllib errors."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; "
                                    "WhisprPageRead/1.0)",
                      "Accept": "text/html, text/plain, */*"})
    try:
        with _PAGE_OPENER.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read(2 << 20)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(2 << 20)
        except Exception:  # noqa: BLE001
            body = b""
        return e.code, dict(e.headers or {}), body


def _read_page(url: str) -> dict:
    """Fetch url's readable text under every guard above.
    Returns {"ok": True, "url": final_url, "text": ...} or
    {"ok": False, "reason": plain_language}."""
    now = time.time()
    with _PAGE_CACHE_LOCK:
        hit = _PAGE_CACHE.get(url)
        if hit and now - hit[0] < _PAGE_CACHE_TTL_S:
            return {"ok": True, "url": hit[1], "text": hit[2]}
    current = url
    try:
        for _hop in range(_PAGE_MAX_HOPS + 1):
            problem = _public_url_guard(current)
            if problem:
                raise _PageReadError(problem)
            try:
                status, headers, body = _fetch_url_once(current)
            except _PageReadError:
                raise
            except Exception as e:  # noqa: BLE001 — honest unreachable
                raise _PageReadError(
                    f"the page couldn't be reached ({type(e).__name__})")
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location") or \
                    headers.get("location")
                if not location:
                    raise _PageReadError(
                        f"the page redirected without a destination "
                        f"(HTTP {status})")
                current = urllib.parse.urljoin(current, location)
                continue
            if status in (401, 403):
                raise _PageReadError(
                    f"the page refused the read (HTTP {status}) — "
                    "likely bot-walled or paywalled")
            if status == 404:
                raise _PageReadError("the page is gone (HTTP 404)")
            if status >= 400:
                raise _PageReadError(f"the page answered HTTP {status}")
            text = _extract_readable_text(body)
            if not text:
                raise _PageReadError(
                    "the page came back with no readable text — it "
                    "probably needs JavaScript to show anything")
            text = text[:_PAGE_READ_CHAR_CAP]
            with _PAGE_CACHE_LOCK:
                _PAGE_CACHE[url] = (time.time(), current, text)
            return {"ok": True, "url": current, "text": text}
        raise _PageReadError("too many redirects")
    except _PageReadError as e:
        return {"ok": False, "reason": str(e)}


def _handle_page_read(pr: dict) -> dict:
    """Rocky just fetched (or failed to fetch) the page himself.
    The brain answers from the pulled text only — or owns the
    failure plainly. Content is never invented."""
    asked_url = pr["url"]
    got = _read_page(asked_url)
    if not got["ok"]:
        return {"brief":
                f"He handed you a page to read ({asked_url}) and your "
                f"fetch just failed: {got['reason']}. Tell him plainly "
                "you couldn't read that page and why, in your own "
                "short words. NEVER invent, guess or summarize content "
                "you did not pull. You also have a real browser worker "
                "on his machine — if he says \"browser\" or "
                "\"screenshot\" with the link, you'll queue the real "
                "page load instead; offer that once, plainly."}
    return {"brief":
            f"You just fetched {got['url']} yourself, right now. "
            "Answer him from ONLY the page text below — quote and "
            "cite specifics from it, name the URL when it helps, and "
            "if what he asked isn't in the text, say the page doesn't "
            "cover it. Never add facts that aren't on the page.\n\n"
            f"PAGE TEXT FROM {got['url']}:\n" + got["text"]}


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
    if _ONEFETCH_TEXT_RE.search(t):
        return {"product": "onefetch"}
    if re.search(r"\broof|rover\b", t, re.IGNORECASE):
        return {"product": "roof_rover"}
    return {"product": "whispr"}


def _marketing_lane_block(character_id: str, product: str,
                          text: str) -> dict | None:
    """Marketing for a project outside the speaker's lane gets the
    fixed redirect (Bailey's Roof Rover and Mara's Whispr never
    cross — and neither do the girls). None = her project, proceed."""
    lane = character_lane(character_id)
    if not lane:
        return None
    product_lane = {"roof_rover": LANE_ROOF, "whispr": LANE_WHISPR,
                    "onefetch": LANE_ONEFETCH}[product]
    if product_lane != lane:
        return _lane_cross_reply(character_id, product_lane)
    return None


def _marketing_brief(product: str) -> str:
    facts = _ROOF_ROVER_BRIEF if product == "roof_rover" else \
        (_WHISPR_BRIEF if product == "whispr" else _JORDAN_BRIEF)
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
_ROCKY_TOKEN_SAVED_REPLY = (
    "Saved. I put that key away on my end. Next time you ask me to "
    "dig through one of your repos, that's the one I'll read with.")
_TOKEN_REVOKED_REPLY = (
    "done — wiped that key clean off my side. next repo you have me "
    "read, I’ll use the server’s own key again.")
_TOKEN_NOTHING_REPLY = (
    "nothing to wipe, babe — there’s no key saved on my end right "
    "now.")
_ROCKY_TOKEN_NOTHING_REPLY = (
    "Nothing to wipe. There's no key saved on my end right now.")


def _token_saved_reply(character_id) -> str:
    return (_ROCKY_TOKEN_SAVED_REPLY if _is_rocky(character_id)
            else _TOKEN_SAVED_REPLY)


def _token_nothing_reply(character_id) -> str:
    return (_ROCKY_TOKEN_NOTHING_REPLY if _is_rocky(character_id)
            else _TOKEN_NOTHING_REPLY)


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
    return {"brief": FIXED_REPLY_PREFIX
            + _token_saved_reply(character_id)}


def _handle_github_token_revoke(conn: sqlite3.Connection, user_id: str,
                                character_id: str,
                                token: str | None) -> dict:
    had = clear_github_token(conn)
    if token:
        # "forget this token ghp_…" — scrub the paste too; store nothing.
        scrub_token_from_history(conn, user_id, character_id, token)
    reply = _TOKEN_REVOKED_REPLY if had else \
        _token_nothing_reply(character_id)
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
                    merge_branch: str | None = None,
                    kind: str = "build",
                    create_name: str | None = None,
                    create_desc: str | None = None) -> int:
    ensure_tables(conn)
    cur = conn.execute(
        "INSERT INTO dev_jobs (user_id, character_id, repo, task, "
        "status, created_at, merge, merge_branch, kind, create_name, "
        "create_desc) "
        "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
        (user_id, character_id, repo, task, _utc_iso(),
         1 if merge else 0, merge_branch, kind, create_name,
         create_desc))
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
    # Project lanes: an assistant girl queues builds for HER repo
    # only. A repo named from another lane (or outside all three)
    # gets the fixed redirect — never queued. No repo named:
    # unambiguous content about her own project defaults to her
    # repo; content about another project redirects the same way.
    lane = character_lane(character_id)
    if lane and repo is not None:
        blocked = _lane_repo_block(character_id, repo)
        if blocked is not None:
            return blocked
    elif lane:
        ask_lane = text_lane(dv.get("task") or "", character_id)
        if ask_lane and ask_lane != lane:
            return _lane_cross_reply(character_id, ask_lane)
        if ask_lane == lane:
            repo = LANE_REPOS[lane]
            dv = {**dv, "repo": repo}
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
    lane = character_lane(character_id)
    if lane and repo_full:
        blocked = _lane_repo_block(character_id, repo_full)
        if blocked is not None:
            return blocked
    src = last_mergeable_job(conn, user_id, repo_full)
    if src is not None and lane and repo_lane(src["repo"]) != lane:
        return _lane_cross_reply(character_id, repo_lane(src["repo"]))
    if lane and repo_full is None:
        # No repo named: only merge jobs from her own lane.
        src = last_mergeable_job(
            conn, user_id, f"{_GH_OWNER}/{LANE_REPOS[lane]}")
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
# Repo creation (2026-10-09, owner: "make sure the character Rocky
# can create repos and everything and not just be talked to those
# three projects"). Rocky's thread ONLY: he asks for a new repo /
# project, Rocky queues a kind='create' job on the same dev bridge,
# and the worker creates the repo under his GitHub account, seeds a
# runnable scaffold as the FIRST commit on main (creation, not a
# merge), and admits the new repo to its own allowlist so Rocky can
# build on it immediately. He names things: no name given means
# Rocky asks for the name and nothing else — never coins one. The
# girls can't create repos; they redirect to Rocky and their lanes
# are untouched.
# ---------------------------------------------------------------------------
_CREATE_PROJECT_FLOW_KIND = "create_project_flow"
_CREATE_PROJECT_VERB_RE = re.compile(
    r"\b(?:create|make|spin\s+up|set\s+up)\b[^.!?\n]{0,60}?"
    r"\b(?:a\s+|an\s+|the\s+)?(?:new\s+)?(repo|repository|project)\b"
    r"|\b(?:start|build)\b[^.!?\n]{0,40}?\bnew\s+(repo|repository|project)\b",
    re.IGNORECASE)
_CREATE_PROJECT_NAME_RE = re.compile(
    r"\b(?:called|named)\s+([A-Za-z0-9][A-Za-z0-9 ._-]{0,60})",
    re.IGNORECASE)
_CREATE_PROJECT_QUOTED_RE = re.compile(
    r"[\"'“”]([A-Za-z0-9][A-Za-z0-9 ._-]{0,60})[\"'”]")
_CREATE_PROJECT_FOR_RE = re.compile(
    r"\b(?:repo|repository|project)\s+for\s+(.+?)(?:\s+(?:called|named)\s+"
    r"|\s*[.!?]|$)", re.IGNORECASE)
_CREATE_PROJECT_CANCEL_RE = re.compile(
    r"\b(cancel|never\s*mind|forget\s+it|scrap\s+(that|it)|"
    r"don't\s+bother|stop)\b", re.IGNORECASE)
_CREATE_PROJECT_SKIP_DESC_RE = re.compile(
    r"^(skip|no\s+description|none|just\s+(make|build|create)\s+it|"
    r"that's\s+it|that'?s\s+all)\b", re.IGNORECASE)
_PROJECT_NAME_BAD_RE = re.compile(r"[^a-z0-9._-]+")


def _project_slug(name: str | None) -> str:
    """GitHub-safe repo name from the owner's own words — lowercased,
    spaces/underscores to dashes, nothing invented."""
    slug = _PROJECT_NAME_BAD_RE.sub("-", (name or "").strip().lower())
    return slug.strip("-._")[:100]


def detect_create_project(text: str) -> dict | None:
    """Owner wants a NEW repo/project created — shapes like "create a
    repo for X called Y", "start a new project", "spin up a repo".

    Deliberately narrow: needs a creation verb AND a repo/project
    noun, so "fix the project", "create a character" (the studio's
    noun) and normal build asks never land here. Returns
    {"name": slug|None, "desc": str|None, "raw": text}."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or _DEV_SECRET_GUARD_RE.search(t):
        return None
    if not _CREATE_PROJECT_VERB_RE.search(t):
        return None
    name = None
    m = _CREATE_PROJECT_NAME_RE.search(t)
    if m:
        name = _project_slug(m.group(1))
    if not name:
        m = _CREATE_PROJECT_QUOTED_RE.search(t)
        if m:
            name = _project_slug(m.group(1))
    desc = None
    m = _CREATE_PROJECT_FOR_RE.search(t)
    if m:
        d = m.group(1).strip(" .!?\"'")
        d = re.sub(r"^(?:a|an|the)\s+", "", d, flags=re.IGNORECASE)
        if d and d.lower() not in ("called", "named"):
            desc = d[:200]
    return {"name": name or None, "desc": desc, "raw": t[:1000]}


def _rocky_create_redirect() -> dict:
    """A girl asked to create a repo: creating new projects is
    Rocky's desk — her lanes and her build powers don't change."""
    return {"brief": FIXED_REPLY_PREFIX +
            "creating brand-new repos is rocky's desk, babe — he "
            "starts new projects from scratch in his thread. ask him "
            "over there. i don't make new repos over here."}


def _create_project_turn(conn: sqlite3.Connection, user_id: str,
                         character_id: str, text: str) -> dict | None:
    """Route a create-project ask (or an open flow's next answer).

    Rocky executes; the girls redirect. An open flow in Rocky's
    thread claims only his next plain answer — a cancel kills it, a
    clearly different ability passes through untouched, and a stale
    flow (30+ min quiet, same TTL as the studio's) never wakes up."""
    flow = _studio_get(conn, user_id, _CREATE_PROJECT_FLOW_KIND)
    if _is_rocky(character_id):
        if flow is not None and _studio_fresh(flow):
            if _CREATE_PROJECT_CANCEL_RE.search(text or ""):
                return _continue_create_project(
                    conn, user_id, character_id, flow, text)
            if not _strong_other_intent(text or ""):
                return _continue_create_project(
                    conn, user_id, character_id, flow, text)
            _studio_clear(conn, user_id, _CREATE_PROJECT_FLOW_KIND)
        elif flow is not None:
            _studio_clear(conn, user_id, _CREATE_PROJECT_FLOW_KIND)
        hit = detect_create_project(text)
        if hit is None:
            return None
        return _handle_create_project(conn, user_id, character_id, hit)
    if flow is not None:
        _studio_clear(conn, user_id, _CREATE_PROJECT_FLOW_KIND)
    if is_assistant_character(character_id) \
            and detect_create_project(text) is not None:
        return _rocky_create_redirect()
    return None


def _handle_create_project(conn: sqlite3.Connection, user_id: str,
                           character_id: str, hit: dict) -> dict:
    """Start of the create flow: he named it (queue now, or ask the
    one missing thing), or he didn't (ask for the name, only the
    name — Rocky never coins project names)."""
    if not dev_agent_secret():
        return {"brief":
                "He asked you to create a new repo, but the bridge "
                "between you and your builder is not wired on your "
                "end right now, so you honestly cannot queue it. "
                "Tell him straight and short: the builder link is "
                "down on your side, nothing was queued, and you "
                "will not pretend otherwise."}
    name = hit.get("name")
    desc = hit.get("desc")
    if not name:
        _studio_set(conn, user_id, _CREATE_PROJECT_FLOW_KIND,
                    {"step": "name", "desc": desc or "",
                     "raw": hit.get("raw") or ""})
        return {"brief":
                "He wants a new project/repo created but didn't "
                "name it. Ask him exactly ONE short question: what "
                "should it be called? Don't suggest a name and don't "
                "list options like a menu."}
    if not desc:
        _studio_set(conn, user_id, _CREATE_PROJECT_FLOW_KIND,
                    {"step": "desc", "name": name,
                     "raw": hit.get("raw") or ""})
        return {"brief":
                f"He wants a new project called '{name}' but hasn't "
                "said what it is. Ask him exactly ONE short question: "
                "in one line, what is it? Nothing else."}
    return _enqueue_create_project(conn, user_id, character_id,
                                   name, desc)


def _continue_create_project(conn: sqlite3.Connection, user_id: str,
                             character_id: str, flow: dict,
                             text: str) -> dict:
    """His answer to the pending name/description question."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if _CREATE_PROJECT_CANCEL_RE.search(t) \
            or _says_cancel(t):
        _studio_clear(conn, user_id, _CREATE_PROJECT_FLOW_KIND)
        return {"brief":
                "He scrapped the new project. Confirm plainly: "
                "nothing was queued, no repo created. Short."}
    step = flow.get("step")
    if step == "name":
        m = _CREATE_PROJECT_NAME_RE.search(t) \
            or _CREATE_PROJECT_QUOTED_RE.search(t)
        name = _project_slug(m.group(1)) if m else _project_slug(
            t.strip("\"' "))
        if not name:
            return {"brief":
                    "That didn't read as a project name. Ask him "
                    "again, one short question: what should the "
                    "project be called?"}
        desc = (flow.get("desc") or "").strip()
        if not desc:
            raw = t if len(t.split()) > 3 else ""
            _studio_set(conn, user_id, _CREATE_PROJECT_FLOW_KIND,
                        {"step": "desc", "name": name, "raw": raw})
            return {"brief":
                    f"The project's name is '{name}'. Now ask him "
                    "exactly ONE short question: in one line, what "
                    "is it? Nothing else."}
        _studio_clear(conn, user_id, _CREATE_PROJECT_FLOW_KIND)
        return _enqueue_create_project(conn, user_id, character_id,
                                       name, desc)
    # step == "desc": whatever he says IS the description (or a skip).
    name = flow.get("name") or ""
    _studio_clear(conn, user_id, _CREATE_PROJECT_FLOW_KIND)
    if _CREATE_PROJECT_SKIP_DESC_RE.match(t) or not t:
        desc = (flow.get("raw") or "").strip()
        if not desc or desc.lower() == name:
            desc = f"{name}: new project scaffolded for Rocky to build on"
    else:
        desc = re.sub(r"^(?:it'?s|it is)\s+", "", t,
                      flags=re.IGNORECASE)[:200]
    return _enqueue_create_project(conn, user_id, character_id,
                                   name, desc)


def _enqueue_create_project(conn: sqlite3.Connection, user_id: str,
                            character_id: str, name: str,
                            desc: str) -> dict:
    """Queue the kind='create' job on the dev bridge. The worker
    creates the repo under his account, seeds the scaffold as the
    first commit on main, and admits the repo to its allowlist —
    the queue reply says all of that plainly (the admission is the
    one automatic allowlist add, and it gets logged here)."""
    if not dev_agent_secret():
        return {"brief":
                "The builder link is down on your side right now, so "
                "nothing was queued for the new project. Tell him "
                "straight and short; you will not pretend otherwise."}
    repo_full = f"{_GH_OWNER}/{name}"
    existing = conn.execute(
        "SELECT id FROM dev_jobs WHERE user_id = ? "
        "AND status IN ('queued', 'claimed') AND kind = 'create' "
        "AND repo = ?", (user_id, repo_full)).fetchone()
    if existing:
        return {"brief":
                f"Repo creation for '{name}' is already queued as "
                f"job #{existing[0]} — the builder is on it. Tell "
                "him it's already in the builder's hands, no need to "
                "queue it twice. No new claims about its progress."}
    if active_dev_job_count(conn, user_id) >= _DEV_MAX_ACTIVE:
        return {"brief":
                "He wants a new project created, but you already "
                f"have three jobs queued or running for him "
                f"({_DEV_MAX_ACTIVE} is the plate limit). Tell him "
                "the queue is full and to let those land first. "
                "Nothing new was queued."}
    task = (f"Create new repo {repo_full} — {desc[:200]}")
    jid = enqueue_dev_job(conn, user_id, character_id, repo_full, task,
                          kind="create", create_name=name,
                          create_desc=desc[:200])
    return {"brief":
            f"You just REALLY queued repo-creation job #{jid} for "
            f"your builder: new repo '{repo_full}' — \"{desc[:160]}\""
            ". The builder will create the repo under his GitHub "
            "account (private, like his others), seed it with a "
            "runnable starter scaffold as the first commit on main "
            "(that initial commit IS the creation — the usual "
            "branch-then-merge rails apply to everything after), "
            "and admit the new repo to the builder allowlist so you "
            "can queue normal builds on it right away — say that "
            "admission plainly, it's the one automatic allowlist "
            "add. You will bring him the REAL result here when it "
            "lands: repo created + first commit, or the plain reason "
            "it was blocked. Tell him you're on it, direct and "
            "brief. Promise NOTHING about outcome or timing; invent "
            "no file names, commits or progress you haven't been "
            "given. If Railway wiring for the new app is clean the "
            "result names a live project; if it needs env or "
            "secrets, the result says exactly what's left instead "
            "— never announce a live URL that doesn't exist."}


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
        "SELECT id, repo, task, created_at, merge, merge_branch, kind, "
        "create_name, create_desc "
        "FROM dev_jobs WHERE id = ?", (row[0],)).fetchone()
    return {"id": r[0], "repo": r[1], "task": r[2], "created_at": r[3],
            "merge": bool(r[4]), "merge_branch": r[5],
            "kind": r[6] or "build", "create_name": r[7],
            "create_desc": r[8]}


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
    rocky = _is_rocky(job.get("character_id"))
    if job["status"] == "done":
        head = ((f"done. that build on {repo} finished. "
                 f"\"{task}\"") if rocky else
                (f"done, babe — that build on {repo} actually finished. "
                 f"\"{task}\""))
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
        if rocky:
            return (f"I'm stuck on that one. That job on {repo} got "
                    f"blocked. {detail or summary} Tell me how you "
                    "want to handle it.")
        return (f"I'm stuck on that one, babe — that job on {repo} "
                f"got blocked. {detail or summary} "
                "tell me how you want to handle it.")
    if rocky:
        return (f"That build didn't land. \"{task}\" on {repo} ran "
                f"out of tries. {summary or detail} Nothing fake "
                "about it.")
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
    if _is_rocky(job.get("character_id")):
        instruction = (
            "You are Rocky, his male co-producer, texting him. The "
            "dev builder he asked you to use just finished a job, and "
            "these are the ONLY true facts about it:\n\n"
            + "\n".join(facts) +
            "\n\nBring him the result as your own message: direct, "
            "dry, professional-warm, short. NEVER use pet names or "
            "terms of endearment toward him — no baby, babe, honey, "
            "sweetie, sweetheart, or darling. Done means tell him "
            "what landed and the branch — and if the facts say it "
            "was MERGED to main, say exactly that with the commit; "
            "failed or blocked means say so plainly, say what the "
            "detail says is needed, and never dress it up. Never "
            "claim YOU personally wrote, committed or deployed "
            "anything — the builder did the work; you queued it and "
            "you're bringing him the outcome. Add NOTHING that is "
            "not in the facts.")
    else:
        instruction = (
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
            "in the facts.")
    payload = {
        "system_instruction": {"parts": [{"text": instruction}]},
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
        if _is_rocky(job.get("character_id")):
            text = _rocky_safe_text(text, _dev_result_fallback(job))
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
# Browser-agent bridge (2026-10-09, owner-directed): the owner asked
# for Rocky to have "your browser or one like it". Rocky's thread
# queues browse jobs; a worker on the owner's VM polls with the SAME
# shared secret as the dev bridge (DEV_AGENT_SECRET), claims one job
# at a time, loads the page in real headless Chromium (JS rendered),
# and posts back the rendered text or a full-page screenshot, which
# lands in the thread as Rocky's follow-up message (store.log_turn,
# the same delivery primitive as the dev bridge). Read-only v1: the
# worker never logs in, never fills or submits forms, and its only
# click is dismissing a cookie banner. Owner-only + Rocky-only, like
# the other power tools: customers and the other characters never
# see this lane. Rocky reads nothing himself — he queues, the worker
# loads, and he delivers only the real posted-back result
# (HONESTY_RULE).
# ---------------------------------------------------------------------------
_BROWSE_URL_RE = re.compile(
    r"((?:https?://)?(?:(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}"
    r"|(?:\d{1,3}\.){3}\d{1,3})(?::\d{1,5})?(?:/[^\s]*)?)",
    re.IGNORECASE)
_BROWSE_LINK_HINT_RE = re.compile(
    r"\b(that|this|the\s+(page|site|link|tab)|page|site|link|tab)\b"
    r"|\bit\b(?!')",  # "open it" yes; "it's yours" no
    re.IGNORECASE)
_BROWSE_SHOT_RE = re.compile(r"\bscreen\s?shots?\b", re.IGNORECASE)
_BROWSE_BROWSER_RE = re.compile(
    r"\bbrowser\b|\brender(?:ed|ing)?\b|\bjavascript\b|\bheadless\b",
    re.IGNORECASE)
_BROWSE_MAX_ACTIVE = 3
_BROWSE_TEXT_CAP = 20000
_BROWSE_IMAGE_MAX_BYTES = 6 * 1024 * 1024


def detect_browse_request(text: str) -> dict | None:
    """Rocky wants a real browser load. Deterministic on purpose:
    needs an explicit browser/screenshot signal AND a URL (or the
    signal alone, so the handler can ask him for the link). A miss
    just means normal chat — never a fake browse."""
    t = (text or "").strip()
    if not t:
        return None
    shot = bool(_BROWSE_SHOT_RE.search(t))
    if not (shot or _BROWSE_BROWSER_RE.search(t)):
        return None
    m = _BROWSE_URL_RE.search(t)
    if not m:
        # Signal but no link: only ask him for the URL when he is
        # plainly pointing at one ("open that in the browser") —
        # figurative uses ("screenshot of my heart") stay chat.
        if _BROWSE_LINK_HINT_RE.search(t):
            return {"url": None,
                    "action": "screenshot" if shot else "read",
                    "question": None}
        return None
    url = m.group(1).rstrip(".,);]")
    rest = (t[:m.start()] + " " + t[m.end():]).strip()
    rest = _BROWSE_SHOT_RE.sub(" ", rest)
    rest = _BROWSE_BROWSER_RE.sub(" ", rest)
    rest = re.sub(
        r"\b(look at|pull up|open|load|read|check out|check|visit|"
        r"show me|use|using|with|in the|of|the|a|an|please|for me|"
        r"this|that|page|site|link)\b", " ", rest, flags=re.IGNORECASE)
    question = re.sub(r"\s+", " ", rest).strip(" -—:,.") or None
    if question and len(question) < 4:
        question = None
    return {"url": url, "action": "screenshot" if shot else "read",
            "question": (question[:500] if question else None)}


def _browse_url_ok(url: str) -> bool:
    """Public http/https only. Literal IPs must be public addresses;
    localhost/*.local/*.internal names are refused. (The worker
    re-checks, and refuses private final URLs after redirects.)"""
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost":
        return False
    if host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def enqueue_browse_job(conn: sqlite3.Connection, user_id: str,
                       character_id: str, url: str, action: str,
                       question: str | None) -> int:
    ensure_tables(conn)
    cur = conn.execute(
        "INSERT INTO browse_jobs (user_id, character_id, url, action, "
        "question, status, created_at) VALUES (?, ?, ?, ?, ?, "
        "'queued', ?)",
        (user_id, character_id, url, action, question, _utc_iso()))
    conn.commit()
    return int(cur.lastrowid)


def active_browse_job_count(conn: sqlite3.Connection,
                            user_id: str) -> int:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT COUNT(*) FROM browse_jobs WHERE user_id = ? "
        "AND status IN ('queued', 'claimed')", (user_id,)).fetchone()
    return int(row[0] or 0)


def _handle_browse_request(conn: sqlite3.Connection, user_id: str,
                           character_id: str, det: dict) -> dict:
    if not det.get("url"):
        return {"brief": "He asked you to open a page in your browser "
                         "but did not include the link. Ask him for "
                         "the URL, short and direct."}
    url = det["url"]
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url.lstrip("/")
    if not _browse_url_ok(url):
        return {"brief": FIXED_REPLY_PREFIX +
                "that address is off limits for my browser — I only "
                "open public web pages. Give me a normal http/https "
                "link and I'm on it."}
    if active_browse_job_count(conn, user_id) >= _BROWSE_MAX_ACTIVE:
        return {"brief": FIXED_REPLY_PREFIX +
                "my browser is already on a couple of pages for you — "
                "let those land first, then give me the next one."}
    jid = enqueue_browse_job(conn, user_id, character_id, url,
                             det.get("action") or "read",
                             det.get("question"))
    action_word = ("screenshot" if det.get("action") == "screenshot"
                   else "read")
    return {"brief":
            f"You just queued a REAL browser job (#{jid}) to "
            f"{action_word} {url}. A browser worker on his machine "
            "will load the actual page (JavaScript and all) and the "
            "result will arrive HERE as your follow-up message. Tell "
            "him it's queued, briefly and in your own voice. Do NOT "
            "describe the page, guess what it says, or promise when "
            "it will land."}


# ---------------------------------------------------------------------------
# Browser-agent bridge — worker side. The VM worker polls with the
# same shared secret as the dev bridge (env DEV_AGENT_SECRET here, a
# 0600 config file on the worker VM). The secret is never echoed in
# any response; when unset, every bridge call fails closed and the
# chat intent stays dark.
# ---------------------------------------------------------------------------
def claim_browse_job(conn: sqlite3.Connection) -> dict | None:
    """Atomically claim the oldest queued browse job for the worker."""
    ensure_tables(conn)
    row = conn.execute(
        "SELECT id FROM browse_jobs WHERE status = 'queued' "
        "ORDER BY id LIMIT 1").fetchone()
    if not row:
        return None
    cur = conn.execute(
        "UPDATE browse_jobs SET status = 'claimed', claimed_at = ? "
        "WHERE id = ? AND status = 'queued'", (_utc_iso(), row[0]))
    conn.commit()
    if cur.rowcount == 0:
        return None  # lost a claim race; worker polls again shortly
    r = conn.execute(
        "SELECT id, url, action, question, created_at FROM browse_jobs "
        "WHERE id = ?", (row[0],)).fetchone()
    return {"id": r[0], "url": r[1], "action": r[2], "question": r[3],
            "created_at": r[4]}


def _save_browse_image(image_bytes: bytes, image_format: str) -> str:
    """Save a worker screenshot under /generated (same store the
    image desk uses); returns its public path."""
    ext = "jpg" if (image_format or "").lower() in ("jpeg", "jpg") \
        else "png"
    os.makedirs(config.GENERATED_DIR, exist_ok=True)
    filename = "browse-{0}-{1}.{2}".format(
        datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        uuid.uuid4().hex[:10], ext)
    path = os.path.join(config.GENERATED_DIR, filename)
    with open(path, "wb") as fh:
        fh.write(image_bytes)
    return "/generated/" + filename


def complete_browse_job(conn: sqlite3.Connection,
                        payload: dict) -> dict | None:
    """Record a browser-worker result. Spawns delivery into the
    owner's chat. Returns the job row, or None when no such claimed
    job — a duplicate/late result returns the row without
    re-delivering."""
    ensure_tables(conn)
    try:
        job_id = int(payload.get("job_id"))
    except (TypeError, ValueError):
        return None
    status = str(payload.get("status") or "").strip().lower()
    if status not in ("done", "failed"):
        return None
    row = conn.execute(
        "SELECT id, user_id, character_id, url, action, question, "
        "status, title, final_url, result_text, image_url, image_name, "
        "error, delivered FROM browse_jobs WHERE id = ?",
        (job_id,)).fetchone()
    if not row:
        return None
    cols = ("id", "user_id", "character_id", "url", "action",
            "question", "status", "title", "final_url", "result_text",
            "image_url", "image_name", "error", "delivered")
    job = dict(zip(cols, row))
    if job["status"] != "claimed":
        return job  # already finished (or never claimed): no re-delivery
    job.update({
        "status": status,
        "title": str(payload.get("title") or "")[:300] or None,
        "final_url": str(payload.get("final_url") or "")[:1000] or None,
        "result_text": str(payload.get("text") or "")[:_BROWSE_TEXT_CAP]
                       or None,
        "error": str(payload.get("error") or "")[:1000] or None,
    })
    image_b64 = str(payload.get("image_b64") or "")
    if status == "done" and image_b64:
        try:
            raw = base64.b64decode(image_b64, validate=True)
        except Exception:
            raw = b""
        if raw and len(raw) <= _BROWSE_IMAGE_MAX_BYTES:
            try:
                job["image_url"] = _save_browse_image(
                    raw, str(payload.get("image_format") or ""))
                host = (urllib.parse.urlparse(
                    job["final_url"] or job["url"]).hostname
                    or "page")
                job["image_name"] = f"{host} screenshot"
            except Exception:
                job["image_url"] = None  # text still delivers below
    conn.execute(
        "UPDATE browse_jobs SET status = ?, title = ?, final_url = ?, "
        "result_text = ?, image_url = ?, image_name = ?, error = ?, "
        "finished_at = ? WHERE id = ? AND status = 'claimed'",
        (job["status"], job["title"], job["final_url"],
         job["result_text"], job["image_url"], job.get("image_name"),
         job["error"], _utc_iso(), job_id))
    conn.commit()
    threading.Thread(target=_run_browse_delivery, args=(job,),
                     daemon=True).start()
    return job


def _browse_result_fallback(job: dict) -> tuple[str, list]:
    """Deterministic delivery (text, attachments) when the voice call
    is unavailable — built only from the job's real recorded fields,
    in Rocky's direct register (he is the agent, not a character
    performing a part)."""
    jid = job["id"]
    attachments: list = []
    if job["status"] == "failed":
        why = (job.get("error") or "the page wouldn't load.").strip()
        return (f"Browser job #{jid} didn't get there — {why}", [])
    where = job.get("final_url") or job["url"]
    if job.get("image_url"):
        attachments = [{"kind": "image", "url": job["image_url"],
                        "name": job.get("image_name")
                                or "page screenshot", "size": 0}]
        return (f"Here's the screenshot of {where} "
                f"(browser job #{jid}, attached).", attachments)
    title = (job.get("title") or "").strip()
    head = f"Done — {title} ({where}):" if title else f"Done — {where}:"
    body = (job.get("result_text") or "").strip()
    return (head + ("\n" + body[:1200] if body else ""), attachments)


def _voice_browse_result(job: dict) -> str | None:
    """Rewrite the browser worker's REAL result in Rocky's voice."""
    name = "Rocky"
    try:
        from . import characters as _chars
        name = (_chars.get_character(job["character_id"], job["user_id"])
                .get("name") or name)
    except Exception:
        pass
    status_word = {"done": "LOADED", "failed": "FAILED"}.get(
        job["status"], job["status"])
    facts = [f"outcome: {status_word}",
             f"page he asked for: {job['url']}",
             f"action: {job['action']}"]
    if job.get("final_url") and job["final_url"] != job["url"]:
        facts.append(f"it landed on: {job['final_url']}")
    if job.get("title"):
        facts.append(f"page title: {job['title']}")
    if job.get("question"):
        facts.append(f"what he wants to know: {job['question'][:300]}")
    if job["status"] == "failed":
        facts.append(f"what went wrong: {(job.get('error') or '')[:500]}")
    elif job.get("image_url"):
        facts.append(
            "a full-page screenshot of the page is ATTACHED to this "
            "message. You cannot see it yourself — tell him it's "
            "attached and name the page; do not describe the image's "
            "contents beyond its title.")
    elif job.get("result_text"):
        facts.append("the page's actual rendered text (the ONLY "
                     "content you may draw on):\n"
                     + job["result_text"][:1500])
    payload = {
        "system_instruction": {"parts": [{"text":
            f"You are {name}, his agent, texting him. Your browser "
            "worker just finished a page load, and these are the "
            "ONLY true facts about it:\n\n" + "\n".join(facts) +
            "\n\nBring him the result as your own message: direct, "
            "short, no fluff. NEVER use pet names or terms of "
            "endearment toward him — no baby, babe, honey, sweetie, "
            "sweetheart, or darling. If he asked a question, answer it ONLY "
            "from the page text above. Failed means say so plainly "
            "with the reason. Never claim you personally browsed "
            "anything — the worker loaded the page; you queued it "
            "and you're bringing him the outcome. Add NOTHING that "
            "is not in the facts."}]},
        "contents": [{"role": "user", "parts": [
            {"text": "what did the page say?"}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 500},
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
                                 source="whispr-browser-bridge")
    if not data:
        return None
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts
                       if p.get("text")).strip()
        fallback, _attachments = _browse_result_fallback(job)
        return _rocky_safe_text(text, fallback) or None
    except (KeyError, IndexError, TypeError):
        return None


def _run_browse_delivery(job: dict) -> None:
    """Background: deliver a browser result as Rocky's follow-up
    message. Claims the delivery slot first so a duplicate result
    post can never double-deliver."""
    try:
        conn = db.connect()
        try:
            cur = conn.execute(
                "UPDATE browse_jobs SET delivered = 1 WHERE id = ? "
                "AND delivered = 0", (job["id"],))
            conn.commit()
            claimed = cur.rowcount > 0
        finally:
            conn.close()
        if not claimed:
            return
        text = _voice_browse_result(job)
        attachments: list = []
        if job.get("image_url"):
            attachments = [{"kind": "image", "url": job["image_url"],
                            "name": job.get("image_name")
                                    or "page screenshot", "size": 0}]
        if not text:
            text, attachments = _browse_result_fallback(job)
        conn = db.connect()
        try:
            store.log_turn(conn, job["user_id"], job["character_id"],
                           text, job["character_id"], attachments)
        except Exception:
            conn2 = db.connect()
            try:
                conn2.execute(
                    "UPDATE browse_jobs SET delivered = 0 WHERE id = ?",
                    (job["id"],))
                conn2.commit()
            finally:
                conn2.close()
            raise
        finally:
            conn.close()
    except Exception:
        pass
    print(f"[assist] browse job #{job['id']} result delivered "
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
    if _is_rocky(character_id):
        instruction = (
            "You are Rocky, his male co-producer, texting him. A "
            f"real code review just ran on his GitHub repo '{repo}' "
            "and these are its actual findings — the only true "
            "things:\n\n" + findings +
            "\n\nSend him the findings as your own message: direct, "
            "dry, professional-warm, short. NEVER use pet names or "
            "terms of endearment toward him — no baby, babe, honey, "
            "sweetie, sweetheart, or darling. Cover each finding "
            "briefly with its file reference. Add NOTHING that is "
            "not in the findings. Never claim you personally read, "
            "fixed or pushed anything — the review ran and you are "
            "bringing him the results.")
    else:
        instruction = (
            f"You are {name}, his girl, texting him. A real code review "
            f"just ran on his GitHub repo '{repo}' and these are its "
            "actual findings — the only true things:\n\n" + findings +
            "\n\nSend him the findings as your own message: warm, "
            "direct, short texts. Cover each finding briefly with its "
            "file reference. Add NOTHING that is not in the findings. "
            "Never claim you personally read, fixed or pushed "
            "anything — the review ran and you are bringing him the "
            "results.")
    payload = {
        "system_instruction": {"parts": [{"text": instruction}]},
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
        if _is_rocky(character_id):
            text = _rocky_safe_text(text, findings)
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
    blocked = _lane_repo_block(character_id, repo)
    if blocked is not None:
        return blocked
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
    r"^(?:(?:hannah|mia|jordan|rocky)\s*[,!:\-]\s*)?(?:please\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:make|create|generate|draw|paint|render|sketch|imagine|design)"
    r"(?:\s+me)?\s+(?:an?\s+|the\s+)?"
    rf"(?:{_IMAGE_NOUNS})"
    r"(?:\s+of|\s+for|\s+showing|\s*[:,\-])?\s*(.*)$",
    re.IGNORECASE)
_IMAGE_DRAW_RE = re.compile(
    r"^(?:(?:hannah|mia|jordan|rocky)\s*[,!:\-]\s*)?(?:please\s+)?"
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


def _generate_gemini_image(prompt: str) -> dict:
    """One free-pool Gemini image attempt set (the picture desk).

    Returns the _extract_generated_image shape plus, on success,
    the saved image_url. No usage bookkeeping here — callers own
    their gates — so the Sogni worker can use this as the honest
    fallback path when the plan render is unavailable."""
    models = image_models()
    last_result: dict = {"status": "unavailable"}
    for model in models:
        started = time.monotonic()
        data = _image_post(prompt, model)
        result = _extract_generated_image(data)
        result["model"] = model
        result["latency_s"] = round(time.monotonic() - started, 2)
        if result["status"] == "ok":
            result["image_url"] = _save_generated_image(
                result["bytes"], result["mime_type"])
            return result
        last_result = result
        # A content refusal is an answer, not a transient pool failure.
        if result["status"] == "refused":
            break
    return last_result


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

    result = _generate_gemini_image(prompt)
    if result.get("status") == "ok":
        image_url = result["image_url"]
        model = result.get("model") or ""
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
            "image_latency_s": result.get("latency_s"),
        }
    last_result = result

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
# Hannah Sogni plan videos (2026-10-09, owner-directed): she makes
# short videos of herself for the owner on his Sogni Unlimited plan.
#
# Billing law (owner-settled, browser-proven 2026-10-09): every render
# is submitted with billingMode "subscription", so his plan covers it.
# There is deliberately NO credits / API-key path anywhere in this
# section: config.SOGNI_API_KEY is never read here, no cost estimate
# is ever requested, and a job the plan refuses is never resubmitted
# on any other mode — she reports the refusal instead. (The separate
# customer media-request features keep their own pre-existing gates;
# this section neither uses nor changes them.)
#
# Auth (see the SECOND NAMED EXCEPTION in the module docstring): he
# drops his Sogni login once in chat as "sogni login <username>
# <password>". The password drives that one sign-in call and is then
# discarded — only the user-session token pair is kept, in
# assist_secrets row "sogni_plan_session". The login message is
# scrubbed from the persisted history before the sign-in even runs.
# The SDK refreshes the access token from the stored refresh token;
# when a render rotates the pair, the fresh pair replaces the stored
# one. A fully dead session means she asks him to drop the login once
# more — the password itself is never stored to re-login from.
#
# Flow: her immediate answer confirms the render is on; a daemon
# thread submits the job (socket jobRequest, MiniMax H3 image-to-
# video, her avatar as the first frame, 768x1024, ~6s), polls to
# completion, mints the result URL, downloads the clip into
# GENERATED_DIR, and lands it in this chat as a video bubble
# (store.log_turn with a video attachment — the same follow-up
# primitive dev-agent results and review findings use). Owner-only,
# Hannah's thread only, one render at a time, daily cap.
# ---------------------------------------------------------------------------
_SOGNI_PLAN_SESSION = "sogni_plan_session"
_SOGNI_PLAN_APP_ID = "companion-hannah-plan"
_SOGNI_PLAN_PLACEHOLDER = "[sogni login]"
_SOGNI_PLAN_WIDTH = 768
_SOGNI_PLAN_HEIGHT = 1024
_SOGNI_PLAN_DURATION = 6
_SOGNI_PLAN_FPS = 24
_SOGNI_PLAN_BILLING_MODE = "subscription"

_SOGNI_TRIGGER_RE = re.compile(r"\bsogni\s+log\s?in\b", re.IGNORECASE)
_CRED_WRAP_CHARS = "<>[]{}()\"'`"
_CRED_LABEL_TOKENS = frozenset({
    "sogni", "login", "log", "in", "username", "user", "password",
    "pass", "pwd", "account", "email", "e-mail",
})
_SOGNI_LOGOUT_RE = re.compile(
    r"\b(forget|delete|remove|wipe|drop|sign\s*out|log\s*out)\b"
    r"[^.!?]{0,30}\bsogni\b"
    r"|\bsogni\b[^.!?]{0,30}\b(log\s*out|sign\s*out|logout|disconnect)\b",
    re.IGNORECASE)
_SOGNI_FORGET_RE = re.compile(
    r"\b(forget|wipe|delete|remove|revoke)\b[^.!?]{0,30}\bsogni\b",
    re.IGNORECASE)
_VIDEO_ASK_RE = re.compile(
    r"(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?P<verb>make|create|generate|render|shoot|film|record|send|give)"
    r"(?:\s+me)?\s+"
    r"(?:(?P<det>a|an|the|that|this|one)\s+)?"
    r"(?P<adj>(?:(?:sexy|short|quick|little|new|hot|cute|promo|clothed|"
    r"topless|nude|naked)\s+)*)"
    r"(?P<noun>video|clip|vid)\b\s*(?P<rest>.*)$",
    re.IGNORECASE)
_MEDIA_FOLLOWUP_RE = re.compile(
    r"^\s*(?:please\s+)?(?:make|send|give|do)\s+it\b", re.IGNORECASE)
_VIDEO_CALL_RE = re.compile(r"\bvideo\s+(?:call|chat|conference)\b",
                            re.IGNORECASE)

_SOGNI_LOGIN_SAVED_REPLY = (
    "got it, babe — i'm signed into your sogni now, and that "
    "password's already scrubbed out of our chat. tell me to make "
    "you a video and i'll actually make it.")
_ROCKY_SOGNI_LOGIN_SAVED_REPLY = (
    "Got it. I'm signed into your Sogni now, and that password's "
    "already scrubbed out of our chat. Tell me to make a video and "
    "I'll make it.")
_SOGNI_LOGIN_FAILED_REPLY = (
    "that sign-in didn't take, babe — sogni turned it away. check "
    "the login and drop it once more like: sogni login <username> "
    "<password>.")
_ROCKY_SOGNI_LOGIN_FAILED_REPLY = (
    "That sign-in didn't take. Sogni turned it away. Check the "
    "login and drop it once more like: sogni login <username> "
    "<password>.")
_SOGNI_LOGOUT_REPLY = (
    "done — signed out of sogni on my side and wiped the saved "
    "sign-in. drop your login here again whenever you want videos "
    "back.")
_SOGNI_LOGOUT_NOTHING_REPLY = (
    "nothing to wipe, babe — i'm not signed into sogni right now.")
_ROCKY_SOGNI_LOGOUT_NOTHING_REPLY = (
    "Nothing to wipe. I'm not signed into Sogni right now.")
_SOGNI_NO_SESSION_REPLY = (
    "i'd love to, babe — but i'm not signed into your sogni on "
    "this end yet. drop your login right here once, like this: "
    "sogni login <username> <password> — i'll save the sign-in "
    "safe, scrub that message out of our chat, and the password's "
    "gone for good. then ask me again and i'll make it.")
_ROCKY_SOGNI_NO_SESSION_REPLY = (
    "Not yet. I'm not signed into your Sogni on this end. Drop "
    "your login right here once, like this: sogni login <username> "
    "<password> — I'll save the sign-in, scrub that message out "
    "of our chat, and the password's gone for good. Then ask me "
    "again and I'll make it.")


def sogni_login_saved_reply(character_id=None) -> str:
    return (_ROCKY_SOGNI_LOGIN_SAVED_REPLY if _is_rocky(character_id)
            else _SOGNI_LOGIN_SAVED_REPLY)


def sogni_login_failed_reply(character_id=None) -> str:
    return (_ROCKY_SOGNI_LOGIN_FAILED_REPLY if _is_rocky(character_id)
            else _SOGNI_LOGIN_FAILED_REPLY)


def _sogni_no_session_reply(character_id=None) -> str:
    return (_ROCKY_SOGNI_NO_SESSION_REPLY if _is_rocky(character_id)
            else _SOGNI_NO_SESSION_REPLY)


def _sogni_logout_reply(character_id, had: bool) -> str:
    if _is_rocky(character_id):
        return (_SOGNI_LOGOUT_REPLY if had
                else _ROCKY_SOGNI_LOGOUT_NOTHING_REPLY)
    return _SOGNI_LOGOUT_REPLY if had else _SOGNI_LOGOUT_NOTHING_REPLY


class _SogniPlanError(Exception):
    """Failure whose kind she can report honestly, as-is.

    The stage/status/error_code fields are log telemetry only: they
    say WHERE the sign-in died (client create, nonce, login POST,
    token backup) and what Sogni answered, never anything about the
    credentials themselves."""

    def __init__(self, kind: str, detail: str = "", *,
                 stage: str = "", status=None, error_code=None):
        self.kind = kind
        self.stage = stage
        self.status = status
        self.error_code = error_code
        super().__init__(detail or kind)


def sogni_plan_daily_cap() -> int:
    return _env_int("HANNAH_SOGNI_PLAN_DAILY_CAP", 10, 1)


def detect_sogni_video_request(text: str) -> str | None:
    """Return the video subject for an explicit video ask, else None.

    An empty string is meaningful: she was asked to make a video but
    no subject was supplied, so she asks what it should show. The
    shapes are deliberately broad in assistant threads — "make me
    that video", "send me a video of you", "make me a clip" — but a
    video CALL is a different thing entirely, and a bare "send me a
    video" (no subject at all) stays ambiguous cold chat, matching
    the long-standing detector contract.
    """
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or _VIDEO_CALL_RE.search(t):
        return None
    # A leading "hannah, ..." address is not part of the ask.
    t = re.sub(r"^(?:hannah|mia|jordan|rocky)\s*[,!:\-]\s*", "", t,
               flags=re.IGNORECASE)
    match = _VIDEO_ASK_RE.search(t)
    if not match:
        return None
    verb = (match.group("verb") or "").lower()
    det = (match.group("det") or "").lower()
    adj = re.sub(r"\s+", " ", (match.group("adj") or "")).strip()
    rest = (match.group("rest") or "").strip()
    if rest.lower().startswith(("call", "chat", "conference")):
        return None
    subject = re.sub(r"^(?:of|for|showing|about)\s+", "", rest,
                     flags=re.IGNORECASE).strip()
    if adj and subject and not subject.lower().startswith(adj.lower()):
        subject = f"{adj} {subject}"
    elif adj and not subject:
        subject = adj
    if not subject:
        # "send me a video" with nothing after it can mean an
        # existing clip — not a render order. "Make me that video"
        # is a render order with a deictic subject: herself, as
        # the screenshot that drove this rule proved.
        if verb in ("send", "give"):
            return None
        if det in ("that", "this"):
            return "you smiling at the camera"
        return ""
    return subject.strip(" \t\r\n.!?")[:1000]


_ADULT_MEDIA_RE = re.compile(
    r"\b(nude|naked|topless|no\s+shirt|shirt\s+off|boob|breast|"
    r"nipple|pants\s+off|plays\s+with\s+herself|oral|sex|sexy|"
    r"lingerie|underwear|undress|strip|explicit)\b", re.IGNORECASE)
_STRONG_ADULT_MEDIA_RE = re.compile(
    r"\b(nude|naked|topless|no\s+shirt|shirt\s+off|nipple|"
    r"pants\s+off|plays\s+with\s+herself|oral|sex|undress|strip)\b"
    r"|\b(?:taking|take)\s+off\s+(?:your|her|his|their)\s+"
    r"(?:top|shirt|clothes)\b|\btop\s+off\b",
    re.IGNORECASE)
_PROMO_MEDIA_RE = re.compile(
    r"\b(promo|promotion|promotional|clothed|fully\s+clothed|sfw|"
    r"safe\s+for\s+work|for\s+the\s+site|for\s+whispr|marketing|"
    r"advertis\w*|ad\s+copy)\b", re.IGNORECASE)


def detect_sogni_media_intent(text: str) -> str:
    """Promo/SFW vs adult vs neutral for a Sogni media ask.

    Strong adult language renders as asked; explicit clothed /
    promo / for-the-site language renders clothed promo-style;
    "sexy" alone counts as adult unless the ask itself pins it
    down as clothed/SFW."""
    t = text or ""
    if _STRONG_ADULT_MEDIA_RE.search(t):
        return "adult"
    if _PROMO_MEDIA_RE.search(t):
        return "promo"
    if _ADULT_MEDIA_RE.search(t):
        return "adult"
    return "neutral"


def _shape_media_subject(subject: str, intent: str, kind: str) -> str:
    """Fold the promo intent into the stored render subject. Adult
    and neutral asks go through exactly as he worded them."""
    s = (subject or "").strip()
    if intent != "promo":
        return s
    if not s:
        return "fully clothed promo-style, smiling at the camera"
    if "clothed" in s.lower():
        return s
    return f"fully clothed promo-style {kind} for the site, {s}"


# --- Promo subjects + bare nude asks (owner, 2026-10-09 12:55) -------
# A promo ask never bounces just because the subject isn't her body:
# every girl renders her own project's mascot/brand — and plain
# scenes, products, and places — in promo mode. Another project's
# mascot or brand goes to that project's girl (brand walls never
# cross); roster PEOPLE who aren't her stay Mia's desk. Adult intent
# never touches promo subjects. A bare nude ask ("send me a nude")
# is an adult plan IMAGE of her (or of the character Mia names).

_PROMO_REF_URLS = {
    # Canonical approved assets, served by the projects themselves
    # (Fetch: realtor-app static/retriever.webp, byte-identical to
    # the repo asset; Bailey: roof-scout static/bailey_3d_hero.webp).
    "fetch": ("https://realtor-app-production-825b.up.railway.app"
              "/static/retriever.webp"),
    "bailey": "https://tryroofrover.com/static/bailey_3d_hero.webp",
}
_PROMO_REF_LABELS = {"fetch": "Fetch", "bailey": "Bailey"}
_PROMO_VOICE_LINES = {
    "fetch": "OneFetch. We fetch the leads — you close the deals.",
    "bailey": ("Roof Rover. Every damaged roof, four ninety-nine "
               "a lead."),
}
_LANE_PROMO_VOICE_LINES = {
    LANE_ONEFETCH: _PROMO_VOICE_LINES["fetch"],
    LANE_ROOF: _PROMO_VOICE_LINES["bailey"],
    LANE_WHISPR: "Whispr. Your girl, right in your pocket.",
}
_PROMO_NO_VOICE_CLAUSE = (
    "one honest thing: my renders can't carry a real voice yet, "
    "so it'll land silent — no fake voice-over from me.")

_MALE_VOICE_RE = re.compile(
    r"\b(?:male|males|man|guy)(?:'s)?\s+voice\b"
    r"|\bvoice\s*over\b[^.!?]{0,30}\b(?:male|man)\b"
    r"|\bnarrat\w+\b[^.!?]{0,30}\b(?:male|man)\b",
    re.IGNORECASE)
_MEDIA_FETCH_RE = re.compile(
    r"\b(?:fetch|retriever)\b|\bgolden\s+retriever\b", re.IGNORECASE)
_MEDIA_BAILEY_RE = re.compile(r"\bbailey\b", re.IGNORECASE)
_MEDIA_BRAND_RES = {
    LANE_ROOF: re.compile(
        r"\bbailey\b|\broof[\s-]?rover\b|\broofrover\b|"
        r"\blead\s+store\b|\bstore\s+shelf\b|\bdamaged\s+roof\b|"
        r"\broof\s+damage\b|\broofing\b|\bshingles?\b",
        re.IGNORECASE),
    LANE_ONEFETCH: re.compile(
        r"\bone[\s-]?fetch\b|\bfetch\b|\bretriever\b|\bfsbo\b|"
        r"\bopen\s+house\b|\blistings?\b|\brealtor\b|"
        r"\breal[\s-]estate\b", re.IGNORECASE),
    LANE_WHISPR: re.compile(
        r"\bwhispr\b|\bcompanion\s+app\b|companion\.lockeddoor",
        re.IGNORECASE),
}


def _detect_male_voice(text: str) -> bool:
    return bool(_MALE_VOICE_RE.search(text or ""))


def _promo_voice_line(character_id: str,
                      promo_ref: str | None) -> str | None:
    """The short deterministic spoken line for a promo job, when he
    asked for a male voice: the mascot's line, else the lane's."""
    if promo_ref and promo_ref in _PROMO_VOICE_LINES:
        return _PROMO_VOICE_LINES[promo_ref]
    lane = character_lane(character_id)
    return _LANE_PROMO_VOICE_LINES.get(lane or "")


def _classify_promo_subject(character_id: str,
                            subject: str) -> dict | None:
    """Promo-world ruling for a laned girl's media subject.

    Returns None outside the lanes (Rocky keeps his own rules),
    {"mode": "redirect_lane", "lane", "strong"} when the subject is
    another project's mascot or brand, and {"mode": "promo_scene",
    "promo_ref", "strong"} when she renders it herself in promo
    mode. "strong" means a mascot or brand word was actually
    named; a plain scene with no brand in it is a weak generic
    promo scene. A named mascot IS the subject — its lane wins
    over mere setting words."""
    lane = character_lane(character_id)
    if not lane:
        return None
    s = subject or ""
    fetch_hit = bool(_MEDIA_FETCH_RE.search(s))
    bailey_hit = bool(_MEDIA_BAILEY_RE.search(s))
    if fetch_hit and not bailey_hit:
        mascot_lane, ref = LANE_ONEFETCH, "fetch"
    elif bailey_hit and not fetch_hit:
        mascot_lane, ref = LANE_ROOF, "bailey"
    else:
        mascot_lane, ref = None, None
    if mascot_lane:
        if mascot_lane != lane:
            return {"mode": "redirect_lane", "lane": mascot_lane,
                    "strong": True}
        return {"mode": "promo_scene", "promo_ref": ref,
                "strong": True}
    hits = [ln for ln in LANES if _MEDIA_BRAND_RES[ln].search(s)]
    if len(hits) == 1 and hits[0] != lane:
        return {"mode": "redirect_lane", "lane": hits[0],
                "strong": True}
    if hits:
        return {"mode": "promo_scene", "promo_ref": ref,
                "strong": True}
    return {"mode": "promo_scene", "promo_ref": ref, "strong": False}


_PROMO_CLEAN_RE = re.compile(
    r"\b(?:nude|naked|topless|sex|sexual|nsfw|erotic)\b",
    re.IGNORECASE)

_VOICE_CLAUSE_RE = re.compile(
    r"[,]?\s*(?:and\s+)?(?:put|with|add|use|using)?\s*(?:a\s+)?"
    r"(?:male|males|man|guy)(?:'s)?\s+voice(?:over)?\b"
    r"(?:\s+(?:please|for me|on it|over it))?",
    re.IGNORECASE)


def _shape_promo_subject(subject: str, promo_ref: str | None,
                         kind: str) -> str:
    """Final render prompt for a promo scene: the canonical mascot
    anchor when one is named (so Fetch is always FETCH), his scene
    words, and a clean family-friendly promo tail. Voice requests
    ride on the job, never in the render prompt."""
    s = re.sub(r"\s+", " ", (subject or "").strip())
    s = _VOICE_CLAUSE_RE.sub("", s)
    # Promo subjects stay clean by construction (owner rule):
    # adult words on a mascot/brand/scene are stripped, never
    # rendered.
    s = _PROMO_CLEAN_RE.sub("", s)
    s = re.sub(r"^(?:no[,!]?\s*)?(?:don'?t\s+use\s+you[,.\s]*)?"
               r"(?:use\s+)?", "", s, flags=re.IGNORECASE)
    tail = (", clean bright family-friendly promo footage, natural "
            "light, coherent background, subtle natural motion"
            if kind == "video" else
            ", clean bright family-friendly promo photo, natural "
            "light, coherent background")
    if promo_ref == "fetch":
        rest = _MEDIA_FETCH_RE.sub("", s)
        rest = re.sub(r"\bthe\s+dog\b", "", rest, flags=re.IGNORECASE)
        rest = re.sub(r"\s+", " ", rest).strip(" ,.!?")
        rest = re.sub(r"^(?:and|with|have|put)\s+", "", rest,
                      flags=re.IGNORECASE)
        rest = re.sub(r"\bthe\s+app\b",
                      "the OneFetch app on a phone screen", rest,
                      flags=re.IGNORECASE)
        scene = rest or ("trotting through a bright real-estate "
                         "office holding a listings sheet")
        return (f"Fetch, OneFetch's golden retriever mascot wearing "
                f"his navy OneFetch bandana, {scene}{tail}")[:1200]
    if promo_ref == "bailey":
        rest = _MEDIA_BAILEY_RE.sub("", s)
        rest = re.sub(r"\s+", " ", rest).strip(" ,.!?")
        rest = re.sub(r"^(?:and|with|have|put)\s+", "", rest,
                      flags=re.IGNORECASE)
        rest = re.sub(r"\bthe\s+app\b",
                      "the Roof Rover app on a phone screen", rest,
                      flags=re.IGNORECASE)
        scene = rest or "pointing at a damaged roof with a bright smile"
        return (f"Bailey, Roof Rover's 3D assistant in a white polo "
                f"shirt, {scene}{tail}")[:1200]
    if not s:
        s = "a bright product promo scene"
    return f"{s}{tail}"[:1200]


def detect_promo_media_ask(text: str) -> tuple[str, str] | None:
    """A bare promo ask ("make a promo of the store shelf", "promo of
    Fetch at an open house") -> (kind, subject), else None. Kind is
    video unless a picture word is named. A subjectless promo ask
    ("make me a promo") is hers to offer situations for, not this."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or _VIDEO_CALL_RE.search(t):
        return None
    m = re.search(r"\bpromo\b", t, re.IGNORECASE)
    if not m:
        return None
    if not re.match(r"^\s*(?:please\s+)?(?:can\s+you\s+)?"
                    r"(?:make|create|generate|render|shoot|film|"
                    r"record|send|give|do)\b", t, re.IGNORECASE) \
            and not t.lower().startswith("promo"):
        return None
    kind = "image" if re.search(
        r"\b(?:pic|pics|photo|photos|picture|pictures|image|selfie)\b",
        t, re.IGNORECASE) else "video"
    after = t[m.end():]
    after = re.sub(r"^\s*(?:videos?|clips?|vids?|pics?|pictures?|"
                   r"photos?|images?)\s*", "", after, flags=re.IGNORECASE)
    after = re.sub(r"^of\s+", "", after.strip(), flags=re.IGNORECASE)
    subject = re.sub(r"\bsogni\b", "", after, flags=re.IGNORECASE)
    subject = re.sub(r"\s+", " ", subject).strip(" \t\r\n.!?")
    if not subject or _is_vague_media_subject(subject):
        return None
    return kind, subject[:1000]


_NUDE_VIDEO_WORD_RE = re.compile(
    r"\b(?:video|videos|clip|clips|vid|vids|movie)\b", re.IGNORECASE)
_NUDE_ASK_RE = re.compile(
    r"\b(?:send|show|give|make|create|snap|take|get|drop)\b"
    r"[^.!?\n]{0,50}?\b(?:nude|nudes|naked|topless|tits|boobs)\b"
    r"|\b(?:nude|naked|topless)\s+"
    r"(?:pic|pics|photo|photos|picture|pictures|image|selfie|of)\b"
    r"|\b(?:pic|pics|photo|photos|picture|pictures|image|selfie)"
    r"\s+of\s+[^.!?]{0,30}?\b(?:naked|nude|topless)\b"
    r"|\byour\s+(?:tits|boobs|breasts)\b"
    r"|\bnudes\b", re.IGNORECASE)


def detect_nude_media_ask(text: str) -> str | None:
    """Bare adult picture ask -> the IMAGE subject to render, else
    None. No picture word is required ("send me a nude", "send
    nudes", "show me your tits"); a video word sends the ask to the
    video lane instead. The subject keeps his target ("mara nude")
    so the media-target parser can resolve who it is of."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or _VIDEO_CALL_RE.search(t):
        return None
    if _NUDE_VIDEO_WORD_RE.search(t):
        return None
    if not _NUDE_ASK_RE.search(t):
        return None
    if not _STRONG_ADULT_MEDIA_RE.search(t) \
            and not re.search(r"\b(?:tits|boobs|nudes?)\b", t,
                              re.IGNORECASE):
        return None
    rest = re.sub(
        r"^(?:please\s+)?(?:can\s+you\s+)?"
        r"(?:send|show|give|make|create|snap|take|get|drop)\s+"
        r"(?:me\s+)?(?:a\s+|an\s+|some\s+|the\s+)?", "", t,
        flags=re.IGNORECASE).strip(" .!?")
    m = re.match(r"^(?:a\s+)?(?:nude|naked|topless)"
                 r"(?:\s+(?:pic|pics|photo|photos|picture|pictures|"
                 r"image|selfie))?\s+of\s+(.+)$", rest, re.IGNORECASE)
    if m:
        return f"{m.group(1)} nude"
    m = re.match(r"^(?:pic|pics|photo|photos|picture|pictures|"
                 r"image|selfie)\s+of\s+(.+)$", rest, re.IGNORECASE)
    if m:
        return m.group(1)
    if re.search(r"\byour\s+(?:tits|boobs|breasts)\b", rest,
                 re.IGNORECASE):
        return "you topless, smiling at the camera"
    return "you nude"


def _handle_sogni_promo_request(conn: sqlite3.Connection,
                                  user_id: str, character_id: str,
                                  raw_subject: str, *,
                                  kind: str = "video",
                                  promo_ref: str | None = None,
                                  raw_text: str = "",
                                  preshaped: bool = False,
                                  voice_line_override: str | None = None
                                  ) -> dict:
    """Promo-scene lane: her project's mascot/brand — or any scene,
    product, or place — rendered on the plan in promo mode. Never
    face-locked to her, never adult. A named mascot is never faked
    through the free picture desk: canonical reference on the plan,
    or the honest sign-in ask."""
    shaped = raw_subject if preshaped else _shape_promo_subject(
        raw_subject, promo_ref, kind)
    if voice_line_override is not None:
        voice_line = voice_line_override or None
    elif _detect_male_voice(raw_text or raw_subject):
        voice_line = _promo_voice_line(character_id, promo_ref)
    else:
        voice_line = None
    _media_topic_touch(conn, user_id, kind=kind, subject=shaped,
                       target_character_id=None)
    state = _studio_get(conn, user_id, _SOGNI_TOPIC_KIND) or {}
    state["last_subject_mode"] = "promo_scene"
    state["last_promo_ref"] = promo_ref
    state["last_promo_voice"] = voice_line
    _studio_set(conn, user_id, _SOGNI_TOPIC_KIND, state)
    if not stored_sogni_session(conn):
        if kind == "image" and not promo_ref:
            # No mascot likeness at stake: the generic picture path
            # (free desk, and it says which path ran) still applies.
            return _handle_sogni_image_request(
                conn, user_id, character_id, raw_subject,
                raw_text=raw_text, target_character_id=None,
                target_name=None, target_resolved=True)
        return {"brief": FIXED_REPLY_PREFIX + _SOGNI_NO_SESSION_REPLY,
                "sogni_login_box": True,
                "sogni_subject_mode": "promo_scene",
                "sogni_media_kind": kind}
    if _sogni_plan_active_job(conn, user_id):
        noun = "picture" if kind == "image" else "video"
        return {"brief": f"He asked for another {noun}, but the last "
                         "one is still rendering. Tell him it's on "
                         "its way and will land right here when it's "
                         "done; one at a time."}
    if _sogni_plan_today_count(conn, user_id) \
            >= sogni_plan_daily_cap():
        if kind == "image":
            return {"brief": "He asked for another picture, but he's "
                             "hit today's Sogni plan media limit. "
                             "Tell him honestly the media desk is "
                             "closed until tomorrow; do not promise "
                             "a time you do not know."}
        return {"brief": "He asked for another video, but he's hit "
                         "today's video limit. Tell him honestly the "
                         "video desk is closed until tomorrow; do not "
                         "promise a time you do not know."}
    job_id = _sogni_plan_enqueue(
        conn, user_id, character_id, shaped, kind=kind,
        target_character_id=None, subject_mode="promo_scene",
        promo_ref=promo_ref, promo_voice=voice_line)
    _spawn_sogni_plan_job(job_id)
    if voice_line:
        label = _PROMO_REF_LABELS.get(promo_ref or "", "promo")
        noun = "clip" if kind == "video" else "pic"
        return {"brief": FIXED_REPLY_PREFIX +
                f"on it, babe — making that {label} promo on your "
                f"plan right now. {_PROMO_NO_VOICE_CLAUSE} the "
                f"finished {noun} lands right here when it's ready.",
                "sogni_media_kind": kind,
                "sogni_subject_mode": "promo_scene",
                "sogni_promo_ref": promo_ref or "",
                "sogni_promo_voice": True}
    kind_word = "video" if kind == "video" else "picture"
    land_word = "clip" if kind == "video" else "picture"
    return {"brief": "You just started making that promo " + kind_word +
                     " for him on his Sogni Unlimited plan — it's "
                     "rendering now, and the finished " + land_word +
                     " will land right here in this chat when it's "
                     "ready. Confirm in your own voice, short and "
                     "warm; do not mention models, queues, or "
                     "process.",
            "sogni_media_kind": kind,
            "sogni_subject_mode": "promo_scene",
            "sogni_promo_ref": promo_ref or "",
            "sogni_target_character_id": None}


# --- Offer prompts & situations (owner order 2026-10-09 12:33) ---
# She WANTS to send pics/videos of herself. A vague ask ("make me a
# video", "send me a pic") gets concrete situations to pick from and
# does NOT queue yet; a specific ask renders immediately. His pick
# (number, place name, or "you pick") routes straight to the render.

_VAGUE_MEDIA_SUBJECTS = frozenset({
    "", "you", "yourself", "me", "a video", "video", "a pic", "pic",
    "a picture", "picture", "you smiling at the camera",
    "you smiling", "smiling at the camera", "you looking at the camera",
    "you at the camera",
})

_MEDIA_OPTIONS = {
    ("video", "adult"): [
        "in the bedroom in a black slip, teasing one strap off her shoulder, looking at the camera",
        "on the couch in an open robe over lingerie, beckoning at the camera",
        "standing by the bed in a sheer top, slowly turning for the camera",
    ],
    ("video", "promo"): [
        "at the kitchen counter in a cozy sweater, laughing and waving at the camera",
        "on the porch at golden hour in a sundress, spinning once for the camera",
        "by the pool in a cute one-piece swimsuit, blowing a kiss at the camera",
    ],
    ("image", "adult"): [
        "in the bedroom in a black slip, one strap slipped off her shoulder, looking at the camera",
        "on the couch in an open robe over lingerie, smiling at the camera",
        "standing by the bed in a sheer top, looking over her shoulder at the camera",
    ],
    ("image", "promo"): [
        "at the kitchen counter in a cozy sweater, smiling at the camera",
        "on the porch at golden hour in a sundress, smiling at the camera",
        "by the pool in a cute swimsuit, blowing a kiss at the camera",
    ],
}
_MEDIA_OPTION_KEYWORDS = ("bedroom", "couch", "bed", "kitchen",
                          "porch", "pool")


def _media_situations(kind: str, intent: str) -> list[str]:
    key = (kind, "adult" if intent == "adult" else "promo")
    return list(_MEDIA_OPTIONS[key])


def _is_vague_media_subject(subject: str) -> bool:
    s = re.sub(r"\s+", " ",
               (subject or "").strip().lower()).strip(" .!?")
    if s in _VAGUE_MEDIA_SUBJECTS:
        return True
    adjs = {"sexy", "hot", "cute", "short", "quick", "little",
            "new", "clothed", "fully", "promo", "sfw"}
    words = [w for w in re.split(r"[^a-z]+", s) if w]
    return bool(words) and all(w in adjs for w in words)


def _media_options_reply(kind: str, options: list[str],
                         target_name: str | None = None,
                         character_id: str | None = None) -> dict:
    who = f" of {target_name}" if target_name else ""
    lines = "\n".join(f"{i + 1}. {opt}" for i, opt in enumerate(options))
    if _is_rocky(character_id):
        lead = "Alright. Pick one, or tell me to pick for you:"
    else:
        lead = ("mm, i'd love to — here, pick one, babe, or tell "
                "me to pick for you:")
    return {"brief": FIXED_REPLY_PREFIX + lead + f"\n{lines}",
            "sogni_media_options": options,
            "sogni_media_kind_pending": kind}


def _match_media_option(text: str, options: list[str]):
    """His pick -> option index; 'you_pick' when he defers; None."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not t:
        return None
    if re.search(r"\byou\s+(pick|choose|decide)\b", t) \
            or t in ("surprise me", "any", "any of them"):
        return "you_pick"
    m = re.search(r"\b([123])\b", t)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(options):
            return idx
    for word, idx in (("first", 0), ("second", 1), ("third", 2)):
        if re.search(rf"\b{word}\b", t) and idx < len(options):
            return idx
    for idx, opt in enumerate(options):
        for kw in _MEDIA_OPTION_KEYWORDS:
            if kw in opt and re.search(rf"\b{re.escape(kw)}\b", t):
                return idx
    return None


def _clean_cred_token(tok: str) -> str:
    """One pasted credential token, unwrapped: <u> / "u" / [u] -> u."""
    t = (tok or "").strip().strip(_CRED_WRAP_CHARS)
    return t.strip(",;:").strip(_CRED_WRAP_CHARS)


def _cred_after_trigger(rest: str) -> list[str]:
    """Credential values after the trigger words: labels, separators,
    and wrapping punctuation dropped; the values themselves in order."""
    values: list[str] = []
    for raw in (rest or "").split():
        tok = _clean_cred_token(raw)
        if not tok or tok.lower() in _CRED_LABEL_TOKENS:
            continue
        values.append(tok)
    return values


def extract_sogni_login(text: str) -> tuple[str, str] | None:
    """The (username, password) from a "sogni login ..." message.

    The trigger is "sogni login" or "sogni log in" (any case); labels
    ("username:"/"password -") and wrapping brackets or quotes on the
    values are stripped, so the pair that reaches the sign-in is the
    one he actually typed. Takes the last well-formed match, so a
    lead-in like "here's my sogni login: sogni login <u> <p>"
    resolves to the real pair instead of swallowing the lead-in
    words as credentials."""
    found: tuple[str, str] | None = None
    for m in _SOGNI_TRIGGER_RE.finditer(text or ""):
        values = _cred_after_trigger((text or "")[m.end():])
        if len(values) >= 2:
            user, pw = values[0], values[1]
            if _cred_value_ok(user) and _cred_value_ok(pw) \
                    and user != pw:
                found = (user, pw)
    return found


# Forgiving credential intake (owner-directed 2026-10-09): he pasted
# his login in natural words ("Username- X password - Y") and the
# rigid "sogni login u p" parser missed it, leaving the credential
# sitting in the persisted thread. Labeled pairs are recognized
# anywhere; unlabeled pairs only inside an open Sogni topic.
_SOGNI_CRED_STOPWORDS = frozenset({
    "sogni", "login", "logout", "password", "username", "user", "pass",
    "the", "a", "an", "my", "your", "his", "her", "is", "it", "in",
    "on", "for", "to", "and", "or", "with", "me", "this", "that",
    "you", "pick", "choose", "make", "video", "pic", "picture",
})
# The label must stand alone (start or after whitespace): a hyphen
# before it means it's a segment of a hyphenated credential, not a
# label ("CANARY-pw-9142" is a password, not "pw: 9142").
_SOGNI_USER_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?:e-?mail|user(?:name)?|login|account)\b\s*"
    r"(?:is\s+)?[:\-=]?\s*([^\s,;]+)", re.IGNORECASE)
_SOGNI_PASS_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?:pass(?:word)?|pwd|pw)\b\s*"
    r"(?:is\s+)?[:\-=]?\s*([^\s,;]+)", re.IGNORECASE)
_SOGNI_PAIR_SLASH_RE = re.compile(r"^([^\s/:,;]+)\s*/\s*(\S+)$")
_SOGNI_PAIR_COMMA_RE = re.compile(r"^([^\s/:,;]+)\s*[,;]\s*(\S+)$")
_SOGNI_PAIR_WORDS_RE = re.compile(r"^(\S+)\s+(\S+)$")


def _cred_value_ok(value: str) -> bool:
    v = _clean_cred_token(value or "")
    return len(v) >= 2 and v.lower() not in _SOGNI_CRED_STOPWORDS


def _extract_labeled_sogni_login(text: str) -> tuple[str, str] | None:
    """A pair named by BOTH labels ("username- X password - Y",
    "user: X pass: Y", "my username is X and my password is Y").
    Unambiguous on its own — no Sogni context needed."""
    um = _SOGNI_USER_LABEL_RE.search(text or "")
    pm = _SOGNI_PASS_LABEL_RE.search(text or "")
    if not (um and pm) or um.span() == pm.span():
        return None
    user = _clean_cred_token(um.group(1))
    pw = _clean_cred_token(pm.group(1))
    if _cred_value_ok(user) and _cred_value_ok(pw) and user != pw:
        return (user, pw)
    return None


def detect_sogni_login_attempt(text: str) -> bool:
    """Credential-shaped paste even when no pair could be extracted.

    A login message that cannot be parsed must still be scrubbed —
    never left sitting in the stored thread — so the intake can claim
    it, mask it, and answer honestly that the sign-in didn't take."""
    t = text or ""
    m = _SOGNI_TRIGGER_RE.search(t)
    if m:
        rest = t[m.end():]
        # Value tokens after the trigger, or even just label words
        # ("sogni login username password" — he was clearly pasting
        # a login, it just carried nothing usable).
        if _cred_after_trigger(rest) or any(
                _clean_cred_token(tok).lower() in _CRED_LABEL_TOKENS
                for tok in rest.split()):
            return True
    return bool(_SOGNI_USER_LABEL_RE.search(t)
                and _SOGNI_PASS_LABEL_RE.search(t))


def extract_sogni_login_forgiving(text: str, *, context: bool,
                                    topic_open: bool
                                    ) -> tuple[str, str] | None:
    """A (username, password) pair from natural phrasing.

    Labeled pairs ("username- X password - Y", "user: X pass: Y")
    are unambiguous on their own. A bare pair ("X / Y", or exactly
    two tokens) only counts with Sogni context — an open Sogni
    topic or login words in the message itself. Ordinary talk that
    merely contains the word "password" never matches: the labeled
    path needs BOTH labels with plausible values, and stopwords can
    never be credentials."""
    t = (text or "").strip()
    if not t:
        return None
    labeled = _extract_labeled_sogni_login(t)
    if labeled:
        return labeled
    if not (context or topic_open):
        return None
    for rx in (_SOGNI_PAIR_SLASH_RE, _SOGNI_PAIR_COMMA_RE):
        m = rx.match(t)
        if m:
            pair = (_clean_cred_token(m.group(1)),
                    _clean_cred_token(m.group(2)))
            if _cred_value_ok(pair[0]) and _cred_value_ok(pair[1]) \
                    and pair[0] != pair[1]:
                return pair
    if topic_open:
        m = _SOGNI_PAIR_WORDS_RE.match(t)
        if m:
            pair = (_clean_cred_token(m.group(1)),
                    _clean_cred_token(m.group(2)))
            if _cred_value_ok(pair[0]) and _cred_value_ok(pair[1]) \
                    and pair[0] != pair[1]:
                return pair
    return None


def store_sogni_session(conn: sqlite3.Connection, session: dict) -> None:
    """Save (or rotate) her Sogni session token pair. Never logged."""
    ensure_tables(conn)
    value = json.dumps({
        "token": str(session.get("token") or ""),
        "refreshToken": str(session.get("refreshToken") or ""),
    })
    conn.execute(
        "INSERT INTO assist_secrets (name, value, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT (name) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (_SOGNI_PLAN_SESSION, value,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    conn.commit()


def stored_sogni_session(conn: sqlite3.Connection) -> dict | None:
    """The stored token pair, or None when she is signed out."""
    ensure_tables(conn)
    row = conn.execute(
        "SELECT value FROM assist_secrets WHERE name = ?",
        (_SOGNI_PLAN_SESSION,)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[0] or "")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    token = str(data.get("token") or "")
    refresh = str(data.get("refreshToken") or "")
    if not token or not refresh:
        return None
    return {"token": token, "refreshToken": refresh}


def clear_sogni_session(conn: sqlite3.Connection) -> bool:
    """Delete the stored session. True when one was actually there."""
    ensure_tables(conn)
    cur = conn.execute(
        "DELETE FROM assist_secrets WHERE name = ?",
        (_SOGNI_PLAN_SESSION,))
    conn.commit()
    return cur.rowcount > 0


def _scrub_whole_message(conn: sqlite3.Connection, user_id: str,
                           character_id: str, needle: str,
                           placeholder: str) -> None:
    """Replace his just-saved turn wholesale when it holds a secret.

    The chat path logs his raw message before maybe_assist runs; a
    login message is nothing but credentials, so the entire row
    becomes the placeholder (masking fragments would leave the
    username and password shape behind)."""
    if not needle:
        return
    row = conn.execute(
        "SELECT id FROM conversation WHERE user_id = ? "
        "AND character_id = ? AND role = 'user' AND instr(text, ?) > 0 "
        "ORDER BY id DESC LIMIT 1",
        (user_id, character_id, needle)).fetchone()
    if not row:
        return
    conn.execute(
        "UPDATE conversation SET text = ? WHERE id = ?",
        (placeholder, row[0]))
    conn.commit()


async def _sogni_plan_login_async(login: tuple[str, str]) -> dict:
    """One Sogni sign-in via the SDK's wallet-signature login.

    REST-only client (no socket needed to sign in). The login pair
    travels as one opaque intake value and the password half never
    leaves this call; only the session token pair comes back.

    Failures raise _SogniPlanError carrying sanitized telemetry
    (stage, SDK error class/status/code) so the intake can log WHERE
    it died. A thin recorder around the REST post notes which call
    the SDK is on — it raises the same ApiError shape for the nonce
    and the login POST, so the stage is otherwise indistinguishable.
    Nothing credential-shaped is ever attached to the error."""
    from sogni_client import SogniClient

    username, password = login
    stage = "client_create"
    client = None
    try:
        client = await SogniClient.create(
            app_id=_SOGNI_PLAN_APP_ID, auth_type="token",
            disable_socket=True)
        stage = "login"
        rest = client.api_client.rest
        orig_post = rest.post

        async def _recording_post(path, *args, **kwargs):
            nonlocal stage
            p = str(path)
            if p.endswith("/nonce"):
                stage = "nonce"
            elif p.endswith("/login"):
                stage = "login_post"
            return await orig_post(path, *args, **kwargs)

        rest.post = _recording_post
        try:
            await client.account.login(username, password,
                                       remember_me=True)
        finally:
            rest.post = orig_post
        stage = "token_backup"
        backup = await client.api_client.auth.backup()
    except _SogniPlanError:
        raise
    except Exception as e:  # noqa: BLE001 — telemetry, never credentials
        raise _SogniPlanError(
            "login_failed", str(e)[:120], stage=stage,
            status=getattr(e, "status", None),
            error_code=(getattr(e, "error_code", None)
                        or getattr(e, "errorCode", None))) from e
    finally:
        if client is not None:
            await client.aclose()
    if not backup or not backup.get("token") \
            or not backup.get("refreshToken"):
        raise _SogniPlanError("login_failed",
                              "sign-in returned no session",
                              stage="token_backup")
    return {"token": backup["token"],
            "refreshToken": backup["refreshToken"]}


_SOGNI_LOGIN_WORDS_RE = re.compile(
    r"\b(?:sign|log)\s?in\b|\blogin\b", re.IGNORECASE)


def _extract_any_sogni_login(conn: sqlite3.Connection, user_id: str,
                             text: str) -> tuple[str, str] | None:
    """Labeled pairs first (unambiguous anywhere), then the trigger
    forms ("sogni login/log in u p", brackets stripped), then the
    forgiving bare-pair shapes inside Sogni context."""
    labeled = _extract_labeled_sogni_login(text or "")
    if labeled:
        return labeled
    login = extract_sogni_login(text)
    if login:
        return login
    topic = _sogni_topic_open(conn, user_id)
    context = bool(topic or _SOGNI_MENTION_RE.search(text or "")
                   or _SOGNI_LOGIN_WORDS_RE.search(text or ""))
    return extract_sogni_login_forgiving(text or "", context=context,
                                         topic_open=topic)


def scrub_sogni_credentials_from_history(
        conn: sqlite3.Connection, user_id: str | None = None) -> int:
    """Retroactive scrub of stored credential pastes.

    Replaces any user turn still holding credential-shaped text in
    an assistant thread with the "[sogni login]" placeholder — the
    same text a live intake leaves behind. Labeled pairs and the
    exact form only; bare pairs are never applied retroactively.
    Returns how many rows were scrubbed (a count, never content)."""
    ensure_tables(conn)
    chars = ", ".join("?" for _ in ASSISTANT_CHARACTERS)
    sql = ("SELECT id, text FROM conversation WHERE role = 'user' "
           f"AND character_id IN ({chars})")
    args: list = sorted(ASSISTANT_CHARACTERS)
    if user_id:
        sql += " AND user_id = ?"
        args.append(user_id)
    scrubbed = 0
    for row in conn.execute(sql, args).fetchall():
        stored = row["text"] or ""
        if stored == _SOGNI_PLAN_PLACEHOLDER:
            continue
        if extract_sogni_login(stored) is not None or \
                extract_sogni_login_forgiving(
                    stored, context=False, topic_open=False) is not None:
            conn.execute(
                "UPDATE conversation SET text = ? WHERE id = ?",
                (_SOGNI_PLAN_PLACEHOLDER, row["id"]))
            scrubbed += 1
    if scrubbed:
        conn.commit()
    return scrubbed


def _sogni_plan_login(login: tuple[str, str]) -> dict:
    """Sync seam over the sign-in (monkeypatched in tests)."""
    try:
        return asyncio.run(_sogni_plan_login_async(login))
    except _SogniPlanError:
        raise
    except Exception as e:  # noqa: BLE001 — she reports kinds, not stacks
        raise _SogniPlanError("login_failed", str(e)[:120],
                              stage="login") from e


def _sogni_failure_telemetry(exc: BaseException,
                             redact: tuple[str, ...] = ()) -> str:
    """One sanitized sign-in failure line for the server log.

    Stage, exception class, HTTP status, and Sogni's error code and
    message — with any credential substring redacted out of the
    message first. Never the username, never the password, never the
    derived wallet, never tokens."""
    parts = [f"stage={getattr(exc, 'stage', '') or 'login'}",
             f"error={exc.__class__.__name__}"]
    status = getattr(exc, "status", None)
    if status is not None:
        parts.append(f"status={status}")
    code = getattr(exc, "error_code", None)
    if code is None:
        code = getattr(exc, "errorCode", None)
    if code is not None:
        parts.append(f"code={code}")
    kind = getattr(exc, "kind", "")
    if kind:
        parts.append(f"kind={kind}")
    msg = str(exc)[:200]
    for value in redact:
        if value and len(value) >= 3:
            msg = msg.replace(value, "***")
    if msg:
        parts.append(f"message={msg!r}")
    return " ".join(parts)


# Sign-in attempt throttle (owner-only surface, but a pasted login
# that fails must not become an oracle): a few failures lock further
# attempts for a few minutes. In-memory — a restart clears it, which
# is fine; the point is slowing a burst, not building a wall.
_SOGNI_LOGIN_MAX_FAILURES = 5
_SOGNI_LOGIN_LOCK_SECONDS = 600
_sogni_login_fail_times: dict[str, list[float]] = {}


def _sogni_login_recent_failures(user_id: str) -> list[float]:
    now = time.time()
    times = [t for t in _sogni_login_fail_times.get(user_id, [])
             if now - t < _SOGNI_LOGIN_LOCK_SECONDS]
    if times:
        _sogni_login_fail_times[user_id] = times
    else:
        _sogni_login_fail_times.pop(user_id, None)
    return times


def sogni_login_lock_remaining(user_id: str) -> int:
    """Seconds until sign-in attempts open again; 0 when not locked."""
    times = _sogni_login_recent_failures(user_id)
    if len(times) < _SOGNI_LOGIN_MAX_FAILURES:
        return 0
    return max(1, int(times[0] + _SOGNI_LOGIN_LOCK_SECONDS
                      - time.time()) + 1)


def perform_sogni_login(conn: sqlite3.Connection, user_id: str,
                        login: tuple[str, str]) -> dict | None:
    """The one sign-in path, shared by chat intake and the secure
    login box. On success the session pair is stored (never the
    password) and earlier missed pastes are swept; on failure the
    sanitized telemetry line is logged and the throttle ticks.
    Returns the stored session, or None."""
    if sogni_login_lock_remaining(user_id):
        print("[assist] sogni sign-in skipped (throttled): "
              "stage=throttle error=TooManyAttempts", flush=True)
        return None
    try:
        session = _sogni_plan_login(login)
    except _SogniPlanError as e:
        _sogni_login_fail_times.setdefault(user_id, []).append(
            time.time())
        print("[assist] sogni sign-in failed ("
              + _sogni_failure_telemetry(e, redact=login) + ")",
              flush=True)
        return None
    _sogni_login_fail_times.pop(user_id, None)
    store_sogni_session(conn, session)
    # A successful sign-in also cleans up any earlier paste of his
    # that a narrower parser missed, in every assistant thread.
    scrub_sogni_credentials_from_history(conn, user_id)
    return session


# Public names for the endpoint layer (main.py): the fixed words she
# lands in-thread after a secure-box sign-in attempt.
SOGNI_LOGIN_SAVED_REPLY = _SOGNI_LOGIN_SAVED_REPLY
SOGNI_LOGIN_FAILED_REPLY = _SOGNI_LOGIN_FAILED_REPLY


def _scrub_latest_user_turn(conn: sqlite3.Connection, user_id: str,
                              character_id: str) -> None:
    """Mask his newest stored turn wholesale — for a login attempt
    whose pair could not even be extracted, there is no needle to
    search by, and the newest user turn in this thread is the paste
    itself (the chat path logs it before maybe_assist runs)."""
    row = conn.execute(
        "SELECT id FROM conversation WHERE user_id = ? "
        "AND character_id = ? AND role = 'user' ORDER BY id DESC "
        "LIMIT 1", (user_id, character_id)).fetchone()
    if not row:
        return
    conn.execute(
        "UPDATE conversation SET text = ? WHERE id = ?",
        (_SOGNI_PLAN_PLACEHOLDER, row[0]))
    conn.commit()


def _handle_sogni_login_intake(conn: sqlite3.Connection, user_id: str,
                                 character_id: str,
                                 login: tuple[str, str]) -> dict:
    # Scrub FIRST: even a failed sign-in never leaves the password
    # sitting in the persisted history.
    _scrub_whole_message(conn, user_id, character_id, login[1],
                         _SOGNI_PLAN_PLACEHOLDER)
    if perform_sogni_login(conn, user_id, login) is None:
        return {"brief": FIXED_REPLY_PREFIX
                + sogni_login_failed_reply(character_id),
                "sogni_login_box": True}
    return {"brief": FIXED_REPLY_PREFIX
            + sogni_login_saved_reply(character_id)}


def _handle_sogni_login_unparseable(conn: sqlite3.Connection,
                                      user_id: str,
                                      character_id: str) -> dict:
    """A credential-shaped paste no extractor could read. Still
    scrubbed (it may hold a password), still answered honestly —
    never fed to the brain, never left in the stored thread."""
    _scrub_latest_user_turn(conn, user_id, character_id)
    print("[assist] sogni sign-in failed (stage=parse "
          "error=UnparseableLogin)", flush=True)
    return {"brief": FIXED_REPLY_PREFIX
            + sogni_login_failed_reply(character_id),
            "sogni_login_box": True}


def _handle_sogni_logout(conn: sqlite3.Connection, user_id: str,
                           character_id: str,
                           login: tuple[str, str] | None) -> dict:
    had = clear_sogni_session(conn)
    if login:
        # "forget my sogni login user pass" — scrub the paste too.
        _scrub_whole_message(conn, user_id, character_id, login[1],
                             _SOGNI_PLAN_PLACEHOLDER)
    reply = _sogni_logout_reply(character_id, had)
    return {"brief": FIXED_REPLY_PREFIX + reply}


# ---- render queue --------------------------------------------------------

def _sogni_plan_params(prompt: str, ref_path: str | None = None,
                         model: str | None = None,
                         kind: str = "video") -> dict:
    """The one and only job shape she ever submits.

    billingMode is pinned to the plan here; there is no parameter,
    env var, or fallback anywhere that can switch a job to credits
    billing — a guard test keeps it that way."""
    if kind == "image":
        params: dict = {
            "type": "image",
            "modelId": model or config.SOGNI_IMAGE_MODEL,
            "positivePrompt": prompt,
            "numberOfMedia": 1,
            "width": 768,
            "height": 1344,
            "billingMode": _SOGNI_PLAN_BILLING_MODE,
        }
    else:
        params = {
            "type": "video",
            "modelId": model or config.SOGNI_VIDEO_MODEL,
            "positivePrompt": prompt,
            "numberOfMedia": 1,
            "duration": _SOGNI_PLAN_DURATION,
            "fps": _SOGNI_PLAN_FPS,
            "width": _SOGNI_PLAN_WIDTH,
            "height": _SOGNI_PLAN_HEIGHT,
            "billingMode": _SOGNI_PLAN_BILLING_MODE,
        }
    if ref_path:
        params["referenceImage"] = ref_path
    return params


_NEG_CLAUSE_RE = re.compile(
    r"[,]?\s*\b(?:no|without|don't|do not|never)\s+([^,.;!?]+)",
    re.IGNORECASE)


def _sogni_plan_prompt(her_name: str, subject: str) -> str:
    """His words, shaped the way H3 likes: stated exclusions folded
    into the positive prompt, quality folded in as description (H3
    takes no negativePrompt field, so none is ever sent)."""
    exclusions: list[str] = []

    def _eat(m: re.Match) -> str:
        exclusions.append(m.group(1).strip())
        return ""

    main = _NEG_CLAUSE_RE.sub(_eat, subject or "")
    main = re.sub(r"\s+", " ", main).strip(" ,")
    if not main:
        main = "smiling at the camera"
    prompt = f"Photorealistic {her_name}, {main}"
    for ex in exclusions[:3]:
        if ex:
            prompt += f", keeping the scene free of {ex}"
    prompt += (", clean high-quality footage, natural anatomy, soft "
               "flattering light, coherent background, subtle natural "
               "motion, she looks at the camera")
    return prompt[:1200]


def _classify_sogni_failure(text: str) -> str:
    """Map a Sogni failure blob to the outcome she reports."""
    low = (text or "").lower()
    if any(k in low for k in ("401", "unauthorized", "unauthenticated",
                              "invalid token", "token expired",
                              "refresh token", "not authenticated")):
        return "session_dead"
    if any(k in low for k in ("sensitive content", "sensitive",
                              "moderation", "nsfw")):
        return "blocked"
    if any(k in low for k in ("billing", "subscription", "unlimited",
                              "plan", "fair-use", "fair use",
                              "insufficient", "quota", "not covered",
                              "cannot cover", "can't cover")):
        return "plan_rejected"
    return "failed"


async def _sogni_plan_render(session: dict, prompt: str,
                               ref_path: str | None,
                               model: str | None = None,
                               kind: str = "video") -> tuple[dict, dict | None]:
    """Submit one plan-billed render and download the clip.

    Returns (outcome, fresh_session): outcome is
    {"status", "project_id", "path", "detail"} with status one of
    done / blocked / plan_rejected / session_dead / failed; the
    fresh session (the SDK rotates the pair) must be persisted by
    the caller. Every failure is classified, never retried on
    another billing mode."""
    from sogni_client import SogniClient

    from . import media_gen

    outcome: dict = {"status": "failed", "project_id": None,
                     "path": None, "detail": ""}
    fresh: dict | None = None
    client = None
    try:
        media_gen._patch_websocket_quirk()
        client = await SogniClient.create(
            app_id=_SOGNI_PLAN_APP_ID, auth_type="token",
            disable_socket=False, websocket_factory=media_gen._ws_factory)
        await client.set_tokens(token=session.get("token") or "",
                                refresh_token=session.get(
                                    "refreshToken") or "")
        project = await client.projects.create(
            _sogni_plan_params(prompt, ref_path, model, kind))
        outcome["project_id"] = project.id
        urls: list = []
        try:
            urls = await project.wait_for_completion(timeout=900) or []
        except Exception:  # noqa: BLE001 — classify from project state
            urls = []
        if not urls:
            info: dict = {}
            try:
                info = await client.projects.get(project.id) or {}
            except Exception:  # noqa: BLE001
                info = {}
            for wj in (info.get("completedWorkerJobs") or []):
                img_id = wj.get("imgID") or wj.get("id")
                if not img_id:
                    continue
                try:
                    urls.append(await client.projects.media_download_url(
                        {"jobId": project.id, "id": img_id,
                         "type": "complete"}))
                except Exception:  # noqa: BLE001
                    pass
            if not urls:
                blob = " ".join(
                    str(info.get(k) or "")
                    for k in ("status", "reason", "error"))
                outcome["status"] = _classify_sogni_failure(blob)
                outcome["detail"] = blob[:200] or \
                    "render ended with no result"
                return outcome, fresh
        os.makedirs(config.GENERATED_DIR, exist_ok=True)
        if kind == "image":
            filename = "sogni-img-{0}-{1}.jpg".format(
                datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
                uuid.uuid4().hex[:10])
        else:
            filename = "sogni-vid-{0}-{1}.mp4".format(
                datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
                uuid.uuid4().hex[:10])
        dest = os.path.join(config.GENERATED_DIR, filename)
        media_gen._download(urls[0], dest)
        outcome["status"] = "done"
        outcome["path"] = dest
        outcome["detail"] = "billingMode=subscription; project completed"
    except Exception as e:  # noqa: BLE001 — classify, never crash the thread
        if outcome["status"] == "failed":
            outcome["status"] = _classify_sogni_failure(str(e))
            outcome["detail"] = str(e)[:200]
    finally:
        if client is not None:
            try:
                backup = await client.api_client.auth.backup()
                if backup and backup.get("token") \
                        and backup.get("refreshToken"):
                    fresh = {"token": backup["token"],
                             "refreshToken": backup["refreshToken"]}
            except Exception:  # noqa: BLE001
                pass
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
    return outcome, fresh


def _sogni_plan_run(session: dict, prompt: str, ref_path: str | None,
                    model: str | None = None) -> tuple[dict, dict | None]:
    """Sync seam over the render (monkeypatched in tests)."""
    try:
        return asyncio.run(_sogni_plan_render(session, prompt,
                                              ref_path, model))
    except Exception as e:  # noqa: BLE001
        return ({"status": _classify_sogni_failure(str(e)),
                 "project_id": None, "path": None,
                 "detail": str(e)[:200]}, None)


def _sogni_plan_image_run(session: dict, prompt: str,
                          ref_path: str | None,
                          model: str | None = None) -> tuple[dict, dict | None]:
    """Sync seam over the plan image render (monkeypatched in tests)."""
    try:
        return asyncio.run(_sogni_plan_render(
            session, prompt, ref_path, model, kind="image"))
    except Exception as e:  # noqa: BLE001
        return ({"status": _classify_sogni_failure(str(e)),
                 "project_id": None, "path": None,
                 "detail": str(e)[:200]}, None)


def _fetch_first_frame(avatar_url: str) -> str | None:
    """Her reference frame as a local file (face-lock). Mirrors the
    custom-video flow; app-relative avatar urls retry via localhost."""
    if not avatar_url:
        return None
    from . import media_gen

    path = media_gen._fetch_reference(avatar_url)
    if path:
        return path
    if avatar_url.startswith("/"):
        port = os.environ.get("PORT", "8471")
        return media_gen._fetch_reference(
            f"http://127.0.0.1:{port}{avatar_url}")
    return None


def _sogni_plan_active_job(conn: sqlite3.Connection,
                             user_id: str) -> dict | None:
    ensure_tables(conn)
    row = conn.execute(
        "SELECT id FROM sogni_plan_jobs WHERE user_id = ? "
        "AND status IN ('queued', 'rendering') ORDER BY id DESC "
        "LIMIT 1", (user_id,)).fetchone()
    return {"id": row[0]} if row else None


def _sogni_plan_today_count(conn: sqlite3.Connection, user_id: str,
                              now_epoch: float | None = None) -> int:
    ensure_tables(conn)
    now = time.time() if now_epoch is None else float(now_epoch)
    start_local = datetime.fromtimestamp(now, tz=_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        "SELECT COUNT(*) FROM sogni_plan_jobs WHERE user_id = ? "
        "AND created_at >= ?", (user_id, start_utc)).fetchone()
    return int(row[0] or 0)


def _sogni_plan_enqueue(conn: sqlite3.Connection, user_id: str,
                          character_id: str, subject: str, *,
                          kind: str = "video",
                          target_character_id: str | None = None,
                          subject_mode: str | None = None,
                          promo_ref: str | None = None,
                          promo_voice: str | None = None) -> int:
    ensure_tables(conn)
    model = config.SOGNI_IMAGE_MODEL if kind == "image" \
        else config.SOGNI_VIDEO_MODEL
    cur = conn.execute(
        "INSERT INTO sogni_plan_jobs (user_id, character_id, kind, "
        "target_character_id, subject_mode, promo_ref, promo_voice, "
        "prompt, model, billing_mode, status, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
        (user_id, character_id, kind, target_character_id,
         subject_mode, promo_ref, promo_voice, subject,
         model, _SOGNI_PLAN_BILLING_MODE, _utc_iso()))
    conn.commit()
    return int(cur.lastrowid)


def _sogni_plan_deliver(conn: sqlite3.Connection, job: dict,
                          outcome: dict) -> None:
    """Land the outcome in his chat as her follow-up message."""
    status = outcome.get("status") or "failed"
    kind = job.get("kind") or "video"
    rocky = _is_rocky(job.get("character_id"))
    media_url = outcome.get("media_url") or outcome.get("video_url")
    attachments: list[dict] = []
    if status == "done" and media_url:
        size = 0
        try:
            size = os.path.getsize(outcome.get("path") or "")
        except OSError:
            size = 0
        who = job.get("promo_ref") or job.get("target_character_id") \
            or job.get("character_id") or "her"
        if kind == "image":
            attachments = [{"kind": "image", "url": media_url,
                            "name": f"{who}-image.jpg", "size": size}]
            if "fallback" in (outcome.get("detail") or ""):
                text = (("Sogni was unavailable on that one. I made "
                         "this picture through the free picture desk "
                         "instead.") if rocky else
                        ("sogni was unavailable on that one, babe — "
                         "i made this picture through the free "
                         "picture desk instead."))
            else:
                text = (("Here's your picture. Made this one for "
                         "you.") if rocky else
                        ("here's your picture, babe — made this one "
                         "just for you."))
        else:
            attachments = [{"kind": "video", "url": media_url,
                            "name": f"{who}-video.mp4", "size": size}]
            text = (("Here's your video. Made this one for you.")
                    if rocky else
                    "here's your video, babe — made this one just for you.")
        if job.get("subject_mode") == "promo_scene":
            # A promo is not a picture of her, and the delivery
            # never pretends otherwise.
            text = ("here's your promo pic, babe — made it on your "
                    "plan." if kind == "image" else
                    "here's your promo, babe — made it on your plan.")
            voice_line = job.get("promo_voice") or ""
            if voice_line:
                # He asked for a male voice on this one. The render
                # path carries no speech, so the line rides as a
                # voice note IF the voice path can really make one
                # — otherwise the delivery says plainly it landed
                # silent. Never a promised voice that isn't there.
                vurl = None
                try:
                    from . import voice as _voice
                    vurl = _voice.maybe_generate_voice_note(
                        voice_line, job["user_id"], "rex")
                except Exception:  # noqa: BLE001
                    vurl = None
                if vurl and str(vurl).startswith(
                        ("http://", "https://", "/")):
                    attachments.append(
                        {"kind": "audio", "url": str(vurl),
                         "name": "promo-voice.mp3", "size": 0})
                    text += " and the voice line's right there with it."
                else:
                    text += (" it landed silent, though — real "
                             "voice-overs on clips aren't wired on "
                             "my side yet.")
    elif status == "blocked":
        text = (("Sogni's filter stopped that one cold before it "
                 "even rendered. That's their wall. Give me a tamer "
                 "version and I'm on it.") if rocky else
                ("sogni's filter stopped that one cold before it even "
                 "rendered, babe — that's their wall, not me. give me a "
                 "tamer version and i'm on it."))
    elif status == "plan_rejected":
        text = (("Your plan wouldn't cover that one. Sogni rejected "
                 "it before anything rendered, so I stopped right "
                 "there.") if rocky else
                ("your plan wouldn't cover that one, babe — sogni "
                 "rejected it before anything rendered, so i stopped "
                 "right there."))
    elif status == "session_dead":
        text = (("My Sogni sign-in expired. Drop your login here "
                 "once more like: sogni login <username> <password> "
                 "and I'll pick it right back up.") if rocky else
                ("my sogni sign-in expired, babe — drop your login "
                 "here once more like: sogni login <username> "
                 "<password> and i'll pick it right back up."))
    else:
        text = (("That one didn't come through. Sogni hiccuped on "
                 "their end. Say the word and I'll run it again.")
                if rocky else
                ("that one didn't come through, babe — sogni hiccuped "
                 "on their end. say the word and i'll run it again."))
    store.log_turn(conn, job["user_id"], job["character_id"], text,
                   job["character_id"], attachments)


def _run_sogni_plan_job(job_id: int) -> None:
    """Background worker: render, log, deliver. Opens its own
    connection (connections are not shared across threads). Only job
    state is printed — never prompts, tokens, or credentials."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM sogni_plan_jobs WHERE id = ?",
            (job_id,)).fetchone()
        if not row or row["status"] != "queued":
            return
        job = dict(row)
        conn.execute(
            "UPDATE sogni_plan_jobs SET status = 'rendering' "
            "WHERE id = ?", (job_id,))
        conn.commit()
        outcome: dict = {"status": "session_dead", "project_id": None,
                         "path": None, "detail": "session missing"}
        fresh: dict | None = None
        session = stored_sogni_session(conn)
        if session:
            from . import characters

            kind = job.get("kind") or "video"
            target_id = job.get("target_character_id") \
                or job.get("character_id")
            girl = characters.get_character(target_id,
                                            user_id=job["user_id"])
            # Character renders face-lock to the TARGET's own
            # avatar (Mia's desk renders whoever he named); a
            # generic picture (no target) renders unreferenced.
            # Promo-scene jobs render the scene itself: the
            # canonical mascot still as the reference when the job
            # names one, never the girl's face.
            mode = job.get("subject_mode")
            if mode == "promo_scene":
                is_character_render = False
            elif mode in ("self", "character"):
                is_character_render = True
            else:
                is_character_render = kind == "video" \
                    or bool(job.get("target_character_id"))
            if mode == "promo_scene":
                ref_url = _PROMO_REF_URLS.get(
                    job.get("promo_ref") or "")
                ref_path = _fetch_first_frame(ref_url) \
                    if ref_url else None
            elif is_character_render:
                ref_path = _fetch_first_frame(
                    girl.get("avatar_url") or "")
            else:
                ref_path = None
            try:
                prompt = _sogni_plan_prompt(
                    girl.get("name") or "Hannah", job["prompt"]) \
                    if is_character_render else job["prompt"]
                if kind == "image":
                    outcome, fresh = _sogni_plan_image_run(
                        session, prompt, ref_path, job["model"])
                else:
                    outcome, fresh = _sogni_plan_run(
                        session, prompt, ref_path, job["model"])
            finally:
                if ref_path:
                    try:
                        os.unlink(ref_path)
                    except OSError:
                        pass
            # Pictures only: a generic Sogni failure (not a filter
            # stop, not the plan, not a dead session) may fall back
            # to the free picture desk — and the delivery says so.
            # Never for promo scenes: a mascot is never faked on
            # the free desk.
            if kind == "image" and outcome.get("status") == "failed" \
                    and job.get("subject_mode") != "promo_scene":
                gem = _generate_gemini_image(prompt)
                if gem.get("status") == "ok":
                    outcome = {
                        "status": "done",
                        "project_id": outcome.get("project_id"),
                        "path": os.path.join(
                            config.GENERATED_DIR,
                            os.path.basename(gem.get("image_url") or "")),
                        "detail": "sogni unavailable; fallback free "
                                  "picture desk",
                    }
                elif gem.get("status") == "refused":
                    outcome = {
                        "status": "blocked",
                        "project_id": outcome.get("project_id"),
                        "path": None,
                        "detail": str(gem.get("detail") or "")[:200],
                    }
        if fresh:
            store_sogni_session(conn, fresh)
        if outcome.get("status") == "session_dead":
            clear_sogni_session(conn)
        media_url = None
        video_url = None
        if outcome.get("status") == "done" and outcome.get("path"):
            media_url = "/generated/" + os.path.basename(
                outcome["path"])
            outcome["media_url"] = media_url
            if (job.get("kind") or "video") == "video":
                video_url = media_url
                outcome["video_url"] = media_url
        conn.execute(
            "UPDATE sogni_plan_jobs SET status = ?, project_id = ?, "
            "video_url = ?, media_url = ?, detail = ?, "
            "finished_at = ?, delivered = 1 WHERE id = ?",
            (outcome.get("status") or "failed",
             outcome.get("project_id"), video_url, media_url,
             (outcome.get("detail") or "")[:300], _utc_iso(), job_id))
        conn.commit()
        if (job.get("kind") or "video") == "image" \
                and outcome.get("status") == "done" and media_url:
            try:
                _studio_set(conn, job["user_id"], "last_image",
                            {"url": media_url, "prompt": job["prompt"],
                             "model": job["model"],
                             "source": "sogni_plan"})
            except Exception:
                pass
        job["character_id"] = job.get("character_id") or "hannah"
        _sogni_plan_deliver(conn, job, outcome)
        print(f"[assist] sogni plan job #{job_id} "
              f"{outcome.get('status')}", flush=True)
    except Exception:  # noqa: BLE001 — a worker must never take chat down
        print(f"[assist] sogni plan job #{job_id} worker failed",
              flush=True)
    finally:
        conn.close()


def _spawn_sogni_plan_job(job_id: int) -> None:
    t = threading.Thread(target=_run_sogni_plan_job, args=(job_id,),
                         daemon=True, name=f"sogni-plan-{job_id}")
    t.start()


def _handle_sogni_video_request(conn: sqlite3.Connection, user_id: str,
                                  character_id: str,
                                  subject: str, *,
                                  target_character_id: str | None = None,
                                  target_name: str | None = None,
                                  raw_text: str = "") -> dict:
    target_id = target_character_id or character_id
    if target_id != character_id and character_id != "mia":
        return _mia_media_redirect(target_name, character_id)
    _media_topic_touch(conn, user_id, kind="video", subject=subject,
                       target_character_id=target_id)
    if not stored_sogni_session(conn):
        return {"brief": FIXED_REPLY_PREFIX
                + _sogni_no_session_reply(character_id),
                "sogni_login_box": True}
    intent = detect_sogni_media_intent(raw_text or subject)
    if _is_vague_media_subject(subject):
        options = _media_situations("video", intent)
        _media_options_set(conn, user_id, kind="video",
                           options=options,
                           target_character_id=target_id,
                           target_name=target_name, intent=intent)
        return _media_options_reply("video", options, target_name,
                                    character_id)
    if _sogni_plan_active_job(conn, user_id):
        return {"brief": "He asked for another video, but the last "
                         "one is still rendering. Tell him it's on its "
                         "way and will land right here when it's done; "
                         "one at a time."}
    cap = sogni_plan_daily_cap()
    if _sogni_plan_today_count(conn, user_id) >= cap:
        return {"brief": "He asked for another video, but he's hit "
                         "today's video limit. Tell him honestly the "
                         "video desk is closed until tomorrow; do not "
                         "promise a time you do not know."}
    intent = detect_sogni_media_intent(raw_text or subject)
    shaped = _shape_media_subject(subject, intent, "video")
    job_id = _sogni_plan_enqueue(conn, user_id, character_id, shaped,
                                   kind="video",
                                   target_character_id=target_id)
    _spawn_sogni_plan_job(job_id)
    who = f" of {target_name}" if target_name \
        and target_id != character_id else ""
    return {"brief": "You just started making that video" + who +
                     " for him on his Sogni Unlimited plan — it's "
                     "rendering now, and the finished clip will land "
                     "right here in this chat when it's ready. "
                     "Confirm in your own voice, short and warm; do "
                     "not mention models, queues, or process.",
            "sogni_media_kind": "video",
            "sogni_target_character_id": target_id,
            "sogni_media_intent": intent}


def _mia_media_redirect(target_name: str | None = None,
                        character_id: str | None = None) -> dict:
    """Cross-character media belongs to Mia's desk. Fixed text, in
    the lane-redirect voice, so a self-only girl never improvises
    a denial or quietly makes someone else herself."""
    if _is_rocky(character_id):
        text = ("That's Mia's desk. She makes the other girls' "
                "videos and pics from her thread. I only make my "
                "own over here.")
    else:
        text = ("that's mia's desk, babe — she makes the other girls' "
                "videos and pics from her thread. ask her and she's "
                "got it. i only make my own over here.")
    return {"brief": FIXED_REPLY_PREFIX + text,
            "sogni_media_redirect": "mia"}


def _media_unknown_reply(parsed: dict,
                         character_id: str | None = None) -> dict:
    name = parsed.get("raw_name") or "that"
    rocky = _is_rocky(character_id)
    if parsed.get("status") == "ambiguous":
        cands = ", ".join(parsed.get("candidates") or [])
        if rocky:
            text = (f"I've got more than one {name} on the roster "
                    f"({cands}). Which one do you mean?")
        else:
            text = (f"i've got more than one {name} on the roster, "
                    f"babe ({cands}) — which one do you mean?")
        return {"brief": FIXED_REPLY_PREFIX + text,
                "sogni_media_unknown": name}
    if rocky:
        text = f"I don't have a {name} I can make yet. Who do you mean?"
    else:
        text = (f"i don't have a {name} i can make yet, babe — who "
                "do you mean?")
    return {"brief": FIXED_REPLY_PREFIX + text,
            "sogni_media_unknown": name}


_MEDIA_GENERIC_STARTERS = frozenset({
    "you", "yourself", "a", "an", "the", "that", "this", "it",
    "red", "blue", "green", "black", "white", "pink", "purple",
    "golden", "sunset", "sunrise", "ocean", "beach", "city",
    "kitchen", "bedroom", "garden", "forest", "mountain", "river",
    "cat", "dog", "barn", "house", "car", "coffee", "morning",
    "night", "sexy", "clothed", "fully", "promo", "smiling",
    "dancing", "waving", "standing", "sitting", "walking",
    "running", "looking", "wearing", "holding", "sipping",
})


def _parse_media_target(conn: sqlite3.Connection, user_id: str,
                        character_id: str, subject: str,
                        kind: str) -> dict:
    """Who a media subject is OF, from the words he used.

    Roster names/ids at the head of the subject win ("Mara
    dancing" in Mia's thread renders Mara, face-locked to Mara's
    own avatar). "you ..." is the speaker herself. A capitalized
    name-like head that matches nobody is an honest unknown for
    Mia's desk — never a guess. Anything else is generic subject
    matter (a red barn), not a character."""
    s = re.sub(r"\s+", " ", (subject or "")).strip()
    cid = (character_id or "").strip().lower()
    if not s:
        return {"status": "self", "target_id": cid, "target_name": None,
                "subject": s, "raw_name": ""}
    words = s.split(" ")
    first = re.sub(r"[^A-Za-z0-9'’-]", "", words[0]).lower()
    if first in ("you", "yourself", "yours"):
        return {"status": "self", "target_id": cid, "target_name": None,
                "subject": s, "raw_name": ""}
    entries = [e for e in _roster_entries(conn, user_id)
               if not e.get("owner_user_id")
               or e.get("owner_user_id") == user_id]
    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    by_first: dict[str, list[dict]] = {}
    for e in entries:
        eid = (e.get("id") or "").strip().lower()
        ename = (e.get("name") or "").strip()
        if eid:
            by_id[eid] = e
        if ename:
            by_name[ename.lower()] = e
            by_first.setdefault(ename.lower().split(" ")[0],
                                []).append(e)
    for n in (2, 1):
        if len(words) < n:
            continue
        cand_words = [re.sub(r"[^A-Za-z0-9'’-]", "", w)
                      for w in words[:n]]
        cand = " ".join(cand_words).strip().lower()
        if not cand:
            continue
        entry = by_id.get(cand.replace(" ", "-")) or by_name.get(cand)
        if entry is None and n == 1:
            pool = by_first.get(cand) or []
            ids = {e.get("id") for e in pool}
            if len(ids) > 1:
                return {"status": "ambiguous", "target_id": None,
                        "target_name": None, "subject": s,
                        "raw_name": cand_words[0],
                        "candidates": [e.get("name") or e.get("id")
                                       for e in pool]}
            if len(ids) == 1:
                entry = pool[0]
        if entry is not None:
            eid = (entry.get("id") or "").strip().lower()
            remainder = " ".join(words[n:]).strip(" ,.!?")
            if eid == cid:
                return {"status": "self", "target_id": cid,
                        "target_name": entry.get("name"),
                        "subject": remainder or s, "raw_name": ""}
            return {"status": "target", "target_id": eid,
                    "target_name": entry.get("name"),
                    "subject": remainder or "smiling at the camera",
                    "raw_name": ""}
    # Unknown-name shape: a capitalized name-like head followed by
    # scene words ("Zelda dancing"). Lowercase generic subjects
    # ("a red barn", "dancing in the kitchen") never trip this.
    raw_first = words[0]
    if raw_first[:1].isupper() and first not in _MEDIA_GENERIC_STARTERS \
            and len(words) >= 2:
        return {"status": "unknown", "target_id": None,
                "target_name": None, "subject": s,
                "raw_name": re.sub(r"[^A-Za-z0-9'’-]", "", raw_first)}
    return {"status": "generic", "target_id": None,
            "target_name": None, "subject": s, "raw_name": ""}


def _promo_route(conn: sqlite3.Connection, user_id: str,
                 character_id: str, parsed: dict, subject: str,
                 kind: str, raw_text: str) -> dict | None:
    """Promo-world routing for a parsed media subject, or None when
    the self/character rules should decide. Another project's named
    mascot or brand redirects to that project's girl; her own
    project's mascot/brand — and plain scenes, products, and
    places — render right here in promo mode."""
    if parsed["status"] not in ("generic", "unknown"):
        return None
    psubj = parsed.get("subject", subject)
    if _is_vague_media_subject(psubj):
        return None
    promo = _classify_promo_subject(character_id, psubj)
    if promo is None:
        return None
    if not promo.get("strong"):
        # A plain scene is promo territory — unless the ask is
        # adult-flavored, which only ever means HERSELF (owner
        # rule: adult intent never lands on promo subjects), so
        # the self rules decide those.
        if parsed["status"] != "generic" \
                or detect_sogni_media_intent(raw_text or psubj) \
                == "adult":
            return None
    if promo["mode"] == "redirect_lane":
        return _lane_cross_reply(character_id, promo["lane"])
    return _handle_sogni_promo_request(
        conn, user_id, character_id, psubj, kind=kind,
        promo_ref=promo.get("promo_ref"), raw_text=raw_text)


def _route_sogni_video(conn: sqlite3.Connection, user_id: str,
                       character_id: str, subject: str,
                       raw_text: str = "") -> dict:
    parsed = _parse_media_target(conn, user_id, character_id, subject,
                                 "video")
    hit = _promo_route(conn, user_id, character_id, parsed, subject,
                       "video", raw_text)
    if hit is not None:
        return hit
    if (character_id or "").strip().lower() == "mia" \
            and parsed["status"] in ("unknown", "ambiguous"):
        return _media_unknown_reply(parsed)
    target_id = parsed.get("target_id") or character_id
    return _handle_sogni_video_request(
        conn, user_id, character_id, parsed.get("subject", subject),
        target_character_id=target_id,
        target_name=parsed.get("target_name"), raw_text=raw_text)


def _handle_sogni_image_request(conn: sqlite3.Connection, user_id: str,
                                  character_id: str, prompt: str,
                                  raw_text: str = "", *,
                                  target_character_id: str | None = None,
                                  target_name: str | None = None,
                                  target_resolved: bool = False,
                                  now_epoch: float | None = None) -> dict:
    """Assistant-thread picture lane: Sogni plan first, free picture
    desk only as the named fallback when Sogni is unavailable."""
    if not target_resolved:
        parsed = _parse_media_target(conn, user_id, character_id,
                                     prompt, "image")
        if (character_id or "").strip().lower() == "mia" \
                and parsed["status"] in ("unknown", "ambiguous"):
            return _media_unknown_reply(parsed)
        target_character_id = parsed.get("target_id")
        target_name = parsed.get("target_name")
        prompt = parsed.get("subject", prompt)
    if target_character_id and target_character_id != character_id \
            and (character_id or "").strip().lower() != "mia":
        return _mia_media_redirect(target_name, character_id)
    _media_topic_touch(conn, user_id, kind="image", subject=prompt,
                       target_character_id=target_character_id)
    if _is_vague_media_subject(prompt):
        intent_v = detect_sogni_media_intent(raw_text or prompt)
        options = _media_situations("image", intent_v)
        _media_options_set(conn, user_id, kind="image",
                           options=options,
                           target_character_id=target_character_id,
                           target_name=target_name, intent=intent_v)
        return _media_options_reply("image", options, target_name,
                                    character_id)
    if not stored_sogni_session(conn):
        if detect_sogni_media_intent(raw_text or prompt) == "adult":
            # An adult picture never routes to the free desk (it
            # would only refuse there): the honest next step is
            # the one-time plan sign-in.
            return {"brief": FIXED_REPLY_PREFIX
                    + _SOGNI_NO_SESSION_REPLY,
                    "sogni_login_box": True,
                    "sogni_media_kind": "image",
                    "sogni_media_intent": "adult"}
        # Sogni is unavailable (not signed in): the free picture
        # desk is the fallback, and the reply says which path ran.
        fallback_prompt = detect_image_request(raw_text or "") or prompt
        res = _handle_image_request(conn, user_id, character_id,
                                    fallback_prompt, now_epoch=now_epoch)
        res = dict(res or {})
        res["sogni_path"] = "gemini_fallback"
        if res.get("image_url"):
            res["brief"] = (
                "You just made the image he asked for through the "
                "free picture desk — Sogni isn't signed in on this "
                "thread, so the plan path wasn't available. It is "
                "being shown to him in the chat now as an image "
                "bubble. Respond in your own voice, briefly and "
                "warmly; do not mention APIs or keys. His prompt "
                "was: " + fallback_prompt[:300])
        else:
            res["brief"] = (res.get("brief") or "") + \
                " This attempt used the free picture desk because " \
                "Sogni is not signed in here."
        return res
    if _sogni_plan_active_job(conn, user_id):
        return {"brief": "He asked for another picture, but the last "
                         "one is still rendering. Tell him it's on its "
                         "way and will land right here when it's done; "
                         "one at a time."}
    cap = sogni_plan_daily_cap()
    if _sogni_plan_today_count(conn, user_id) >= cap:
        return {"brief": "He asked for another picture, but he's hit "
                         "today's Sogni plan media limit. Tell him "
                         "honestly the media desk is closed until "
                         "tomorrow; do not promise a time you do not "
                         "know."}
    intent = detect_sogni_media_intent(raw_text or prompt)
    shaped = _shape_media_subject(prompt, intent, "image")
    job_id = _sogni_plan_enqueue(conn, user_id, character_id, shaped,
                                   kind="image",
                                   target_character_id=target_character_id)
    _spawn_sogni_plan_job(job_id)
    who = f" of {target_name}" if target_name \
        and target_character_id != character_id else ""
    return {"brief": "You just started making that picture" + who +
                     " for him on his Sogni plan — it's rendering "
                     "now, and the finished picture will land right "
                     "here in this chat when it's ready. Confirm in "
                     "your own voice, short and warm; do not mention "
                     "models, queues, or process.",
            "sogni_media_kind": "image",
            "sogni_target_character_id": target_character_id,
            "sogni_media_intent": intent,
            "sogni_path": "sogni_plan"}


def _route_sogni_image(conn: sqlite3.Connection, user_id: str,
                       character_id: str, prompt: str,
                       raw_text: str = "",
                       now_epoch: float | None = None) -> dict:
    parsed = _parse_media_target(conn, user_id, character_id, prompt,
                                 "image")
    hit = _promo_route(conn, user_id, character_id, parsed, prompt,
                       "image", raw_text)
    if hit is not None:
        return hit
    if (character_id or "").strip().lower() == "mia" \
            and parsed["status"] in ("unknown", "ambiguous"):
        return _media_unknown_reply(parsed)
    return _handle_sogni_image_request(
        conn, user_id, character_id, parsed.get("subject", prompt),
        raw_text=raw_text,
        target_character_id=parsed.get("target_id"),
        target_name=parsed.get("target_name"), target_resolved=True,
        now_epoch=now_epoch)


# ---------------------------------------------------------------------------
# Sogni lane routing (2026-10-09): the plan-video lane above fires only
# on the narrow "make a video ..." shape, so a message that merely
# TALKS about Sogni ("use my sogni account from now on to generate
# pics and videos") used to fall through to the brain — which
# improvised "I can't hop into your Sogni account like that, Matty.
# My systems don't work that way." Deterministic rule instead: in an
# assistant thread, ANY owner message that mentions Sogni routes here
# before the brain — signed out means the fixed one-time-login ask,
# signed in means a fixed ready confirmation (or the render flow when
# he actually asked her to make something). A short confirmation
# right after a Sogni exchange ("yes they do", "i just had it put
# in") stays in the lane too — the topic stays open for a while after
# any lane hit, so his pushback can't fall through either.
# ---------------------------------------------------------------------------
_SOGNI_MENTION_RE = re.compile(r"\bsogni\b", re.IGNORECASE)
_SOGNI_MEDIA_ASK_RE = re.compile(
    r"\b(?:make|create|generate|render|shoot|film|record|give|send)\b"
    r"(?:\s+me)?\s*(?:a\s+|an\s+|some\s+|the\s+)?"
    r"(?:videos?|pics?|pictures?|photos?|clips?)\b[,\s]*(.*)$",
    re.IGNORECASE)
_SOGNI_CONTINUATION_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|ok|okay|sure|right|exactly|correct|"
    r"i\s+did|i\s+just|it's\s+in|it\s+is\s+in|done|put\s+in|"
    r"dropped\s+it|sent\s+it)\b",
    re.IGNORECASE)
_SOGNI_TOPIC_KIND = "sogni_topic"

_SOGNI_SIGNED_IN_REPLY = (
    "i'm signed into your sogni, babe — what do you want me to make?")
_ROCKY_SOGNI_SIGNED_IN_REPLY = (
    "I'm signed into your Sogni. What do you want me to make?")


def _sogni_signed_in_reply(character_id=None) -> str:
    return (_ROCKY_SOGNI_SIGNED_IN_REPLY if _is_rocky(character_id)
            else _SOGNI_SIGNED_IN_REPLY)


def detect_sogni_media_ask(text: str) -> str | None:
    """Loose make-something ask ("generate pics and videos", "make me
    pics of you dancing"). Returns the subject ("" when the ask names
    no subject), or None when it isn't a make ask at all."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return None
    m = _SOGNI_MEDIA_ASK_RE.search(t)
    if not m:
        return None
    subject = (m.group(1) or "").strip()
    subject = re.sub(
        r"^(?:and\s+)?(?:videos?|pics?|pictures?|photos?|clips?)\b[,\s]*",
        "", subject, flags=re.IGNORECASE)
    subject = re.sub(r"\bsogni\b", "", subject, flags=re.IGNORECASE)
    subject = re.sub(r"^(?:of|for|about|showing)\s+", "", subject,
                     flags=re.IGNORECASE)
    subject = re.sub(r"\s+(?:through|on|with|via|using)\s*$", "",
                     subject, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", subject).strip(" \t\r\n.!?")[:1000]


def _sogni_topic_touch(conn: sqlite3.Connection, user_id: str) -> None:
    state = _studio_get(conn, user_id, _SOGNI_TOPIC_KIND) or {}
    state["open"] = True
    _studio_set(conn, user_id, _SOGNI_TOPIC_KIND, state)


def _media_topic_touch(conn: sqlite3.Connection, user_id: str, *,
                       kind: str | None = None,
                       subject: str | None = None,
                       target_character_id: str | None = None) -> None:
    """Open/refresh the media topic and remember what "make it"
    would refer to: the last kind, subject, and target character."""
    state = _studio_get(conn, user_id, _SOGNI_TOPIC_KIND) or {}
    state["open"] = True
    if kind:
        state["last_kind"] = kind
    if subject is not None:
        state["last_subject"] = subject
    if target_character_id is not None:
        state["last_target_character_id"] = target_character_id
    # A fresh self/character media order ends any promo-scene mode;
    # the promo lane re-stamps its own state right after this.
    for key in ("last_subject_mode", "last_promo_ref",
                "last_promo_voice"):
        state.pop(key, None)
    _studio_set(conn, user_id, _SOGNI_TOPIC_KIND, state)


def _media_options_set(conn: sqlite3.Connection, user_id: str, *,
                       kind: str, options: list[str],
                       target_character_id: str | None,
                       target_name: str | None,
                       intent: str) -> None:
    state = _studio_get(conn, user_id, _SOGNI_TOPIC_KIND) or {}
    state["open"] = True
    state["last_kind"] = kind
    state["pending_options"] = list(options)
    state["pending_target_character_id"] = target_character_id
    state["pending_target_name"] = target_name
    state["pending_intent"] = intent
    _studio_set(conn, user_id, _SOGNI_TOPIC_KIND, state)


def _media_options_clear(conn: sqlite3.Connection, user_id: str) -> None:
    state = _studio_get(conn, user_id, _SOGNI_TOPIC_KIND) or {}
    for key in ("pending_options", "pending_target_character_id",
                "pending_target_name", "pending_intent"):
        state.pop(key, None)
    _studio_set(conn, user_id, _SOGNI_TOPIC_KIND, state)


def _media_topic_state(conn: sqlite3.Connection,
                       user_id: str) -> dict | None:
    state = _studio_get(conn, user_id, _SOGNI_TOPIC_KIND)
    if not _studio_fresh(state):
        return None
    return state


def _sogni_topic_open(conn: sqlite3.Connection, user_id: str) -> bool:
    return _studio_fresh(_studio_get(conn, user_id, _SOGNI_TOPIC_KIND))


def _handle_sogni_lane(conn: sqlite3.Connection, user_id: str,
                       character_id: str, text: str) -> dict | None:
    """Assistant-thread router for everything Sogni. None = not ours."""
    t = text or ""
    # A pick from an offered situations menu renders immediately.
    state0 = _media_topic_state(conn, user_id)
    if state0 and state0.get("pending_options"):
        pick = _match_media_option(t, state0["pending_options"])
        if pick is not None:
            options = state0["pending_options"]
            idx = 0 if pick == "you_pick" else int(pick)
            chosen = options[idx]
            kind = state0.get("last_kind") or "video"
            tgt = state0.get("pending_target_character_id")
            tname = state0.get("pending_target_name")
            _media_options_clear(conn, user_id)
            if kind == "image":
                return _handle_sogni_image_request(
                    conn, user_id, character_id, chosen,
                    raw_text=chosen, target_character_id=tgt,
                    target_name=tname, target_resolved=True)
            return _handle_sogni_video_request(
                conn, user_id, character_id, chosen,
                target_character_id=tgt, target_name=tname,
                raw_text=chosen)
    # Bare nude asks ("send me a nude", "pic of you naked") are
    # adult plan images of her (or of Mia's named character) — no
    # picture word required, no situations menu, no brain refusal.
    nude_subject = detect_nude_media_ask(t)
    if nude_subject is not None:
        return _route_sogni_image(conn, user_id, character_id,
                                   nude_subject, raw_text=t)
    # A correction inside an open media topic that names her own
    # project's mascot ("no, don't use you — use Fetch the dog") is
    # a promo re-render, not a search for another character.
    if state0 and not _strong_other_intent(t) \
            and re.match(r"^\s*(?:no\b|don'?t\b|use\b|instead\b|"
                         r"actually\b|not\s+you\b)", t,
                         flags=re.IGNORECASE) \
            and re.search(r"\b(?:use|put|make|do|film|shoot|video|"
                          r"clip|pic|promo|fetch|bailey|dog|"
                          r"retriever)\b", t, re.IGNORECASE):
        promo = _classify_promo_subject(character_id, t)
        if promo and promo.get("strong"):
            if promo["mode"] == "redirect_lane":
                return _lane_cross_reply(character_id,
                                         promo["lane"])
            return _handle_sogni_promo_request(
                conn, user_id, character_id, t,
                kind=state0.get("last_kind") or "video",
                promo_ref=promo.get("promo_ref"), raw_text=t)
    # Bare promo asks ("make a promo of the store shelf") render
    # the promo subject in her lane — never bounced for not being
    # a picture of her.
    promo_ask = detect_promo_media_ask(t)
    if promo_ask is not None:
        pkind, psubject = promo_ask
        if pkind == "image":
            return _route_sogni_image(conn, user_id, character_id,
                                      psubject, raw_text=t)
        return _route_sogni_video(conn, user_id, character_id,
                                   psubject, raw_text=t)
    subject = detect_sogni_video_request(t)
    if subject is not None:
        return _route_sogni_video(conn, user_id, character_id, subject,
                                  raw_text=t)
    # "make it" while a media topic is open is a media order, not
    # small talk — it re-runs the last kind/subject/target.
    state = _media_topic_state(conn, user_id)
    if state and _MEDIA_FOLLOWUP_RE.search(t) \
            and not _strong_other_intent(t) \
            and detect_intent(t)[0] not in ("reminder", "email_draft",
                                            "job_search"):
        if state.get("last_subject_mode") == "promo_scene":
            return _handle_sogni_promo_request(
                conn, user_id, character_id,
                state.get("last_subject") or "",
                kind=state.get("last_kind") or "video",
                promo_ref=state.get("last_promo_ref"),
                raw_text=t, preshaped=True,
                voice_line_override=state.get("last_promo_voice"))
        last_subject = state.get("last_subject") \
            or "you smiling at the camera"
        last_target = state.get("last_target_character_id")
        if state.get("last_kind") == "image":
            return _handle_sogni_image_request(
                conn, user_id, character_id, last_subject,
                raw_text=t, target_character_id=last_target,
                target_resolved=True)
        return _handle_sogni_video_request(
            conn, user_id, character_id, last_subject,
            target_character_id=last_target, raw_text=t)
    if _SOGNI_MENTION_RE.search(t):
        _sogni_topic_touch(conn, user_id)
        if not stored_sogni_session(conn):
            return {"brief": FIXED_REPLY_PREFIX
                    + _sogni_no_session_reply(character_id),
                    "sogni_login_box": True}
        loose = detect_sogni_media_ask(t)
        if loose:
            if re.search(r"\b(?:pics?|pictures?|photos?)\b", t,
                         re.IGNORECASE):
                return _route_sogni_image(conn, user_id, character_id,
                                          loose, raw_text=t)
            return _route_sogni_video(conn, user_id, character_id,
                                      loose, raw_text=t)
        return {"brief": FIXED_REPLY_PREFIX
                + _sogni_signed_in_reply(character_id)}
    # No Sogni name, no video ask: a short confirmation while the
    # Sogni topic is open ("yes they do", "i just had it put in")
    # stays in the lane instead of falling to the brain — but never
    # swallows one of her other abilities or a real typed intent.
    if _sogni_topic_open(conn, user_id) \
            and len(t.strip()) <= 160 \
            and _SOGNI_CONTINUATION_RE.search(t) \
            and not _strong_other_intent(t) \
            and detect_intent(t)[0] not in ("reminder", "email_draft",
                                            "job_search"):
        if not stored_sogni_session(conn):
            return {"brief": FIXED_REPLY_PREFIX
                    + _sogni_no_session_reply(character_id),
                    "sogni_login_box": True}
        return {"brief": FIXED_REPLY_PREFIX
                + _sogni_signed_in_reply(character_id)}
    return None


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
            "Attachments can be images and files of these types: "
            + accepted)
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
                "That video doesn't look playable — it couldn't be "
                "opened. Try an mp4, mov, or webm clip.")
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
    """Give her real eyes/context for the attachments on this turn.

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
    if user_id not in _owner_ids():
        return {"brief": "He tried to send an attachment, but uploads "
                         "are only available in the owner's private "
                         "character chats. Explain this plainly."}

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
        f"He attached {names} to this message for you. The attachment "
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
            ". Tell him plainly you can watch videos as sampled "
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


def clear_studio_flow_state(conn: sqlite3.Connection,
                            user_ids=None) -> int:
    """Drop in-flight studio flow state (guided creation + pending
    give filing). Boot runs this for the owner so a bogus flow
    assembled by an older build dies with the deploy instead of
    waking up mid-chat. Returns how many rows were cleared —
    a count, never content."""
    ensure_tables(conn)
    kinds = ("create_flow", "pending_give")
    ids = [u for u in (user_ids or []) if u]
    if ids:
        marks = ", ".join("?" for _ in ids)
        cur = conn.execute(
            f"DELETE FROM hannah_studio WHERE kind IN (?, ?) "
            f"AND user_id IN ({marks})", (*kinds, *ids))
    else:
        cur = conn.execute(
            "DELETE FROM hannah_studio WHERE kind IN (?, ?)", kinds)
    conn.commit()
    return int(cur.rowcount or 0)


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
    if detect_sogni_video_request(text) is not None:
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
    if m and _valid_character_name(_clean_name(m.group(1))):
        fields["name"] = _clean_name(m.group(1))
    fields["gender"] = _parse_gender(t)
    vm = re.search(r"\bvoice\s*(?:is|=|:)\s*([a-z][a-z0-9\-]{1,24})\b",
                   t, re.IGNORECASE)
    if vm and vm.group(1).lower() not in _NAME_STOPWORDS:
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
    if tail and len(tail) >= 12 and _looks_like_personality(tail):
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


_NAME_STOPWORDS = frozenset({
    "for", "just", "make", "it", "the", "a", "an", "and", "or",
    "with", "who", "is", "this", "that", "pick", "skip", "yes",
    "no", "cancel", "public", "private", "female", "male", "man",
    "woman", "girl", "guy", "month", "week", "day", "sec",
    "second", "seconds", "long", "now", "please", "her", "him",
    "them", "she", "he", "you", "decide", "whatever",
})
_NAME_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z'’\-]*$")


def _valid_character_name(name: str) -> bool:
    """A plausible short character name — never a sentence fragment.

    One to three letter-words, no digits/prices/punctuation, no
    instruction words. "For just $9.99 a month" is a price line,
    not a name; it gets re-asked, never stored."""
    n = (name or "").strip()
    if not n or len(n) > 30:
        return False
    words = n.split(" ")
    if not (1 <= len(words) <= 3):
        return False
    for w in words:
        if not _NAME_TOKEN_RE.match(w):
            return False
        lw = w.strip("'’-").lower()
        if lw in _NAME_STOPWORDS:
            # Visibility words can be part of a real name ("Public
            # Pam"); alone they are an answer to a different step.
            if len(words) == 1 or lw not in ("public", "private"):
                return False
    return True


_INSTRUCTION_FRAGMENT_RE = re.compile(
    r"^\s*(?:make|do|send|give|add|use|set|create|show|take)\s+"
    r"(?:it|that|this|her|him|them|me)\b", re.IGNORECASE)
_PRICE_OR_DURATION_RE = re.compile(
    r"\$\s?\d|\d+(?:\.\d+)?\s*(?:sec|secs|second|seconds|min|mins|"
    r"minute|minutes|hour|hours|%)\b", re.IGNORECASE)


def _looks_like_personality(text: str) -> bool:
    """A personality answer describes who they are. An instruction
    fragment ("Make it 15 sec long") or a price/duration line is
    stray chat, not a personality — re-ask instead of storing it."""
    t = (text or "").strip()
    if len(t) < 3:
        return False
    if _INSTRUCTION_FRAGMENT_RE.search(t):
        return False
    if _PRICE_OR_DURATION_RE.search(t):
        return False
    return True


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
        # A junk-named flow (assembled from stray fragments by an
        # older, looser build) dies here: no summary, no creation.
        if not _valid_character_name(state.get("name") or ""):
            state["name"] = ""
            state["step"] = "name"
            state.pop("name_reconfirmed", None)
            _studio_set(conn, user_id, "create_flow", state)
            return {"brief": "That build never got a real name, so "
                             "there's nothing safe to confirm. Ask "
                             "him what to NAME the character — a "
                             "short name, his words only."}
        if _says_yes(t):
            return _execute_create(conn, user_id, state)
        if _STUDIO_CANCEL_RE.search(t) or re.match(r"^\W*no\b", t, re.I):
            _studio_clear(conn, user_id, "create_flow")
            return {"brief": "He said no to creating that character. "
                             "Confirm warmly that you dropped it — nothing "
                             "was created."}
        # A summary is never presented cold: after the turn that
        # first reached confirm, any later stray message first
        # re-confirms the name before the collected bits come back.
        if _clean_name(t).lower() == (state.get("name") or "").lower() \
                and state.get("name"):
            state["name_reconfirmed"] = True
            state["step"] = "confirm"
            _studio_set(conn, user_id, "create_flow", state)
            return _create_summary_brief(state, _CREATE_ASKS["confirm"])
        if not state.get("name_reconfirmed"):
            state["awaiting_name_reconfirm"] = True
            state["step"] = "confirm"
            _studio_set(conn, user_id, "create_flow", state)
            return {"brief": "Before that character summary goes "
                             "anywhere, re-confirm the name: ask him "
                             f"if you're still building "
                             f"{state.get('name')}. He can say the "
                             "name back if it's right, 'yes' to "
                             "create, or 'no' to drop it. Do not "
                             "read the full summary until the name "
                             "is confirmed."}
        state["step"] = "confirm"
        _studio_set(conn, user_id, "create_flow", state)
        return _create_summary_brief(state, _CREATE_ASKS["confirm"])

    if step == "name":
        name = _clean_name(t)
        if not _valid_character_name(name):
            _studio_set(conn, user_id, "create_flow", state)
            return {"brief": "That doesn't read like a character "
                             "name — names are short (a word or "
                             "two, letters only), and that sounded "
                             "like a sentence. Ask him again: what "
                             "should we NAME the character? Do not "
                             "store that line as a name."}
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
        if not _looks_like_personality(t):
            _studio_set(conn, user_id, "create_flow", state)
            return {"brief": "That sounded like an instruction or "
                             "a stray line, not who this character "
                             "is. Ask him for the personality in "
                             "his own words — who are they, what's "
                             "their vibe? Do not store that line "
                             "as a personality."}
        state["personality"] = t[:2000]
    elif step == "voice":
        if re.search(r"\b(skip|default|whatever|no|none)\b", t, re.I):
            state["voice"] = ""
            state["voice_done"] = True
        else:
            # Only an explicit single voice token counts. "You
            # pick" is him deferring, not a voice named "pick".
            vm = re.fullmatch(
                r"(?:voice\s*(?:is|=|:)?\s*)?([a-z][a-z0-9\-]{1,24})",
                t.lower())
            if not vm or vm.group(1) in _NAME_STOPWORDS:
                _studio_set(conn, user_id, "create_flow", state)
                return {"brief": "That wasn't a voice name. Ask "
                                 "him which voice he'd like — one "
                                 "voice name, or 'skip' for the "
                                 "default. Do not store that line "
                                 "as a voice."}
            state["voice"] = vm.group(1)
            state["voice_done"] = True
    elif step == "avatar":
        if images:
            state["avatar_url"] = images[0]["url"]
        state["avatar_done"] = True

    state["step"] = _create_next_step(state)
    if state["step"] == "confirm":
        state["name_reconfirmed"] = False
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
    if not _valid_character_name(name):
        state["name"] = ""
        state["step"] = "name"
        _studio_set(conn, user_id, "create_flow", state)
        return {"brief": "That build never got a real name, so "
                         "nothing was created. Ask him what to "
                         "NAME the character — a short name, his "
                         "words only."}
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

STUDIO_CHARACTERS = frozenset({"mia", "rocky"})


def _mia_studio_redirect() -> dict:
    """Studio work (character builds, give filing) is Whispr
    business — Mia's lane (Rocky keeps his own studio hands)."""
    return {"brief": FIXED_REPLY_PREFIX +
            "that's mia's desk, babe — character builds and give "
            "filing live in her thread. ask her and she's got it. "
            "i only make my own pics and videos over here.",
            "sogni_media_redirect": "mia"}


def _studio_shaped(text: str) -> bool:
    t = text or ""
    return bool(_DELETE_CHAR_RE.search(t)
                or _detect_add_give(t) is not None
                or _detect_avatar_assignment(t) is not None
                or _detect_create(t))


def _studio_owner_turn(conn: sqlite3.Connection, user_id: str,
                       character_id: str, text: str,
                       attachments: list[dict] | None) -> dict | None:
    """Studio router: Mia's desk (and Rocky's). Hannah/Jordan get
    the fixed Mia redirect for studio-shaped asks — their own
    self-media stays theirs, studio is not media. None = not ours."""
    cid = (character_id or "").strip().lower()
    t = text or ""
    if cid in ("hannah", "jordan"):
        if _SOGNI_MENTION_RE.search(t) \
                or detect_sogni_video_request(t) is not None:
            return None  # the Sogni lane's turn, never studio's
        if _strong_other_intent(t):
            return None  # her own media/other abilities still run
        if _studio_shaped(t):
            return _mia_studio_redirect()
        flow = _studio_get(conn, user_id, "create_flow")
        pending = _studio_get(conn, user_id, "pending_give")
        if (flow and _studio_fresh(flow)) \
                or (pending and _studio_fresh(pending)):
            if detect_intent(t)[0] in ("reminder", "email_draft",
                                       "job_search"):
                return None
            return _mia_studio_redirect()
        return None
    if cid not in STUDIO_CHARACTERS:
        return None

    # A Sogni mention / direct media ask is never a studio answer:
    # with a flow open it must fall through to the Sogni lane, not
    # be eaten as a name/personality/voice fragment.
    _open_flow = _studio_get(conn, user_id, "create_flow")
    _open_pending = _studio_get(conn, user_id, "pending_give")
    if ((_open_flow and _studio_fresh(_open_flow))
            or (_open_pending and _studio_fresh(_open_pending))) \
            and (_SOGNI_MENTION_RE.search(t)
                 or detect_sogni_video_request(t) is not None):
        return None

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
        # Sogni plan session intake — same rule as the token intake
        # above, before any intent or model path: a pasted login must
        # never ride along into a brief or the model's context. A
        # present paste means intake (even alongside "drop your
        # login here" phrasing) unless an explicit forget/wipe verb
        # targets it, mirroring the token revoke flow.
        sogni_login = _extract_any_sogni_login(conn, user_id, text)
        if sogni_login and not _SOGNI_FORGET_RE.search(text or ""):
            return _handle_sogni_login_intake(
                conn, user_id, character_id, sogni_login)
        if _SOGNI_LOGOUT_RE.search(text or ""):
            return _handle_sogni_logout(conn, user_id, character_id,
                                           sogni_login)
        if sogni_login:
            return _handle_sogni_login_intake(
                conn, user_id, character_id, sogni_login)
        # A credential-shaped paste no extractor could read still
        # gets claimed here: scrubbed from the stored thread and
        # answered honestly, never left for the brain (or for anyone
        # reading the history) to find.
        if detect_sogni_login_attempt(text) \
                and not _SOGNI_FORGET_RE.search(text or "") \
                and not _SOGNI_LOGOUT_RE.search(text or ""):
            return _handle_sogni_login_unparseable(
                conn, user_id, character_id)
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
        # Sogni plan lane (owner-only, assistant threads only) —
        # checked before the image lane so "make a video" never lands
        # in the picture desk. ANY Sogni mention routes here (plus
        # short confirmations while the topic is open) so the brain
        # never improvises a capability denial.
        if is_assistant_character(character_id):
            sogni_hit = _handle_sogni_lane(conn, user_id,
                                           character_id, text)
            if sogni_hit is not None:
                return sogni_hit
        image_prompt = detect_image_request(text)
        if image_prompt is not None:
            if is_assistant_character(character_id):
                return _route_sogni_image(conn, user_id, character_id,
                                          image_prompt, raw_text=text)
            return _handle_image_request(conn, user_id, character_id,
                                         image_prompt)
        gh = detect_github_review(text)
        if gh is not None:
            return _handle_github_review(conn, user_id, character_id, gh)
        # Live app status + continuity brief (owner-only cover lane):
        # she fetches real numbers at ask-time; never from memory.
        # Project lanes: a status ask about the OTHER girl's project
        # (or unassigned OneFetch) gets the fixed redirect, not
        # numbers from the wrong lane.
        if detect_app_status(text):
            red = _status_lane_redirect(character_id, text)
            if red is not None:
                return red
            return {"brief": _status_brief(conn, character_id)}
        if detect_owner_brief(text):
            return {"brief": _owner_brief_reply(character_id)}
        if is_assistant_character(character_id) \
                and detect_project_identity(text):
            return {"brief": _owner_brief_reply(character_id)}
        # Shared browser-agent dispatch (owner-only, all private
        # assistant threads): Rocky owns the browser worker, but every
        # private companion can queue a job through the same owner-scoped
        # queue. Results return to the character thread that requested it.
        # "screenshot <site>" / "look at <site> in the browser" queues a
        # real Chromium job on the owner's VM worker. Deterministic
        # trigger; a miss just means normal chat.
        if is_assistant_character(character_id):
            bd = detect_browse_request(text)
            if bd is not None:
                return _handle_browse_request(conn, user_id,
                                              character_id, bd)
        # Rocky page reading (owner-only, ROCKY'S THREAD ONLY — the
        # girls keep plain web search): the fast path under the
        # browser lane. Every link ask that didn't ask for the
        # browser/screenshot is fetched server-side right here.
        # Runs before the dev dispatch so a pasted link is read,
        # not queued as work.
        if (character_id or "").strip().lower() == "rocky":
            pr = detect_page_read(text)
            if pr is not None:
                return _handle_page_read(pr)
        # Repo creation (owner-only): Rocky's thread executes,
        # the girls redirect to him. Runs before the dev dispatch
        # so "create a repo ..." never queues as a build against an
        # existing repo, and an open flow's plain answer is claimed
        # here instead of feeding some other lane.
        if is_assistant_character(character_id):
            cp_hit = _create_project_turn(conn, user_id,
                                          character_id, text)
            if cp_hit is not None:
                return cp_hit
        # Dev-bridge dispatch: marketing-shaped asks still go to the
        # marketing brief (she writes copy in chat); only a strong
        # build verb sends a marketing-flavored sentence to the queue.
        dv = detect_dev_build(text)
        mk = detect_marketing(text)
        if mk is not None and not (dv and _DEV_STRONG_RE.search(text or "")):
            blocked = _marketing_lane_block(character_id,
                                             mk["product"], text)
            if blocked is not None:
                return blocked
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
