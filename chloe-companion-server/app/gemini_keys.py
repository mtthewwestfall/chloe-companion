"""Gemini API key manager: rotation, health tracking, automatic failover.

Two-pool system: keys are split into an active pool (rotated normally)
and a reserve pool (~30% of keys, configurable via GEMINI_RESERVE_COUNT).
Reserve keys are held back and only used when every active key is cooling
down, failing, or disabled — so heavy load can never burn through all
keys at once. Reserve usage is logged.

Free keys are tried first (round-robin by health); paid keys are fallback
only, used when every free key is exhausted, failing, or disabled.

Health model per key:
- consecutive_failures: reset on any success.
- last_used_at: round-robin rotation — least-recently-used eligible key
  is picked first (within free tier, then paid tier).
- 429 / quota errors: exponential backoff via quota_exhausted_until
  (5 min, 15 min, 1 h, 4 h). The key is skipped until the window passes,
  then retried automatically.
- 401 / 402 / 403 (auth/payment class): needs_attention = 1 (surfaced
  in admin); the key is skipped until an admin re-enables or fixes it.
- Error-class separation (efficiency pass, 2026-10-15): failures are
  classified before they touch key health, because a flaky upstream
  minute is not a quota wall.
  - TRANSIENT (5xx, timeouts, connection drops): a brief pause only
    (15 s, 30 s, then 60 s cap, via quota_exhausted_until so selection
    skips the key), and NO health consequences: consecutive_failures is
    untouched, the key never degrades or needs attention over a
    transient streak, and the next success restores it fully.
  - REQUEST (other 4xx — the call itself was bad, e.g. 400/404): no key
    consequence at all (not even a failure count), and post_json stops
    trying further keys for that call — a different key cannot fix a
    bad request.
  - MODEL (2026-10-17): the model itself is overloaded for every key
    (error body names overload — "overloaded"/"UNAVAILABLE", the shape
    of a real Gemini 503). Not the key's fault: no pause, no counters,
    no belief change — only counted in total_requests. Feeds the
    per-model tracker below instead of key health.
  Every usage row records its error class (quota / payment /
  transient / request / model) so failure rates can be reported by class.

Pacing (efficiency pass, 2026-10-15): each key tracks its own daily
request count (day_requests / day_stamp, UTC). Candidate ordering —
both the classic sweep and the believed-healthy fast path — prefers,
among equally healthy keys, the one with the fewest requests today,
then least-recently-used. Daily per-project quotas are therefore
sipped evenly through the day instead of the pool draining in a clump
by midday and recovering in a clump at midnight.

Thread-safe: a module-level lock guards key selection and counter
updates. Network I/O happens outside the lock. Each call opens and closes
its own sqlite connection (same pattern as the rest of the app).

Secret storage: key values live in the server-side sqlite DB — the same
trust boundary as Railway env vars (the repo has no encryption infra, and
every other secret here is an env var). The admin list endpoint only ever
returns masked values ("...abcd"). Never log full key values.

Hardening (bulletproofing) notes:
- Quota errors circuit-break on their own ladder (5, 15, 60, 240
  minutes). Transient errors never circuit-break: their pause ladder
  is seconds-scale (15, 30, 60 s) and carries no health consequences
  (see error-class separation above).
- Per-key rate limiting: dispatches per key are tracked in-memory (per
  process) over a rolling 60s window. Keys at/over GEMINI_PER_KEY_RPM
  (default 12) are skipped so rotation moves on before a 429 hits.
- Healthiest-first ordering: among eligible keys, free keys come first,
  then lowest health score (consecutive failures x10 + failures in the
  last 5 min x5 + latency penalty), then fewest requests today, then
  least-recently-used.
- Degraded mode: when the reserve pool is tapped, is_degraded() is true
  for 10 minutes; the per-key RPM limit is automatically halved and
  /api/v1/admin/gemini-health exposes the signal so callers can slow
  down too.

Efficiency model (fail-fast + background prober):
- A background prober thread keeps an in-memory "believed healthy" view
  of the pool fresh OFF the request path: it re-checks keys whose state
  is unknown or stale with a 1-token generation (never needs_attention
  or disabled keys, never deep inside a backoff window — only at/near
  expiry — and never when real traffic already produced fresher
  evidence). A key that recovers is believed healthy again within ~a
  probe cycle of its backoff expiring, instead of whenever the next
  unlucky request discovers it. The prober is deliberately lean
  (efficiency pass): 30s cadence, at most 4 probes per cycle, a
  believed-healthy key is only re-confirmed after ~7 idle minutes
  (belief itself lasts 10), and a key is never probed more often than
  every 2 minutes — prober rows are a small minority of the usage log.
- post_json() tries believed-healthy keys first (active pool, free-first,
  ranked by today's usage then latency) with a short per-attempt
  timeout (GEMINI_FAST_TIMEOUT_S, default 8s) and fails over
  immediately — no sleeping on the request path; all backoff
  intelligence lives in the health state written by real attempts and
  the prober. If nothing is believed healthy (cold start, all keys
  sick), it falls back to the classic eligible-key sweep. When nothing
  is believed healthy AND nothing is eligible, post_json makes ONE
  no-healthy-key decision and returns None — for every source, not
  just fail-fast ones. There is no all-keys last-resort sweep anymore:
  firing guaranteed-failure attempts at keys inside their backoff
  windows only re-arms those backoffs and pushes recovery out (the
  2026-10-14 pool-sweep lesson). Attempts stay bounded by
  GEMINI_MAX_ATTEMPTS (default 8) and GEMINI_ATTEMPT_BUDGET_S
  (default 50s) per call.
- Optional hedging (GEMINI_HEDGE_AFTER_S > 0): if the first fast attempt
  runs longer than that many seconds, a second believed-healthy key is
  raced in parallel; at most 2 requests are ever in flight. Off by
  default so a thin pool is never double-spent.
- Belief is seeded from the persisted health columns at first use and
  every attempt/probe writes its outcome back through the same
  report_success/report_failure path, so a restart doesn't cold-start
  into probing on live traffic.

Model fallback (2026-10-17): the chat model is an ordered chain, not a
single name. GEMINI_MODEL_CHAIN (comma-separated, default
"gemini-3.1-flash-lite,gemini-3.5-flash-lite") lists the chain primary-
first; model_chain() parses it and current_model() returns the model
currently serving traffic. post_json rewrites the primary model segment
of the caller's URL to current_model() before every attempt phase, so
every existing caller (brain, sims, creator, submit, the proxy) rides
the failover with no signature change.

Overload detection: an attempt whose error body names model overload
("overloaded"/"UNAVAILABLE" — the shape of a real Gemini 503) against a
chain model is classified as the 'model' error class: it is NOT recorded
against the key (no pause, no counters, no belief change — the key did
nothing wrong) and is only counted in total_requests. Each such failure
feeds the per-model tracker; when overload evidence spans multiple keys
(>= 2 distinct keys, or >= 3 failures inside the evidence window) the
model is marked "hot" for GEMINI_MODEL_COOLDOWN_S (default 120 s) and
subsequent post_json calls route to the next chain model. The background
prober checks every chain model (primary key-health logic unchanged) so
fallback readiness is known before live traffic needs it, and a
successful probe lifts a model's cooldown early. degradation_info()
exposes per-model status under "models".

When the primary is healthy the rewrite is a no-op and no tracker state
is touched: key rotation, quotas, and the paid/free reserve logic behave
exactly as before.

Stage 1 — see clearly (key-manager overhaul, 2026-10-18):
(a) Two-quota classification: a 429's error payload names WHICH quota
    was hit. Per-minute (RPM) failures park the key briefly, aligned
    to Google's own retryDelay (capped at ~120 s). Per-day (RPD)
    failures park the key until the next America/Los_Angeles
    midnight — the daily quotas reset on Pacific time — and the key
    receives no calls and no probes until then (selection and the
    prober already honor quota_exhausted_until). Ambiguous 429s keep
    the classic ladder. GEMINI_QUOTA_CLASSIFY=0 restores ladder-only.
(b) Health grid: per-(key, model, capability) cells, persisted in
    gemini_key_cells and learned from observed outcomes. Selection
    skips a key whose cell for the request's capability (text /
    image / tts / grounding) is cooling or dead, without touching the
    key's other cells. A model-not-found 404 for a chain model marks
    the MODEL dead globally (in-memory, with a recheck window) so the
    chain skips it for every key at once instead of rediscovering the
    corpse per key. GEMINI_HEALTH_GRID=0 disables the grid.
(c) Dead-key forensics: permanent-failure signatures in error bodies
    (deleted/disabled service account, invalid key, Gemini API not
    enabled in the project) set needs_attention WITH a plain cause
    label in the attention_reason column — a dead key is never again
    conflated with a key that is merely cooling down.
    GEMINI_KEY_FORENSICS=0 restores unlabeled payment handling.

Stage 2 — protect what matters (key-manager overhaul, 2026-10-18):
(a) Reserved lanes: the K healthiest-eligible free keys by id
    (GEMINI_RESERVE_INTERACTIVE, default 3) are reserved for
    interactive sources (companion chat/voice, live calls). Bulk
    sources (roof-rover scans, warmup, her-day/give pipelines,
    prober-adjacent tooling) never draw them; when the general pool
    empties, bulk gets a typed pool_busy rejection (status_out /
    HTTP 429 at the proxy) with retry_after instead of eating the
    reserve. Interactive traffic sees the whole pool and spills into
    the general keys freely. GEMINI_LANES=0 disables.
(b) Storm breaker: rolling real-traffic failure window in the
    manager. Tripped (>= 20 attempts in 60 s, >= 50% failures) it
    opens for GEMINI_STORM_OPEN_S (default 30 s): bulk fails fast
    with the typed signal, interactive gets a shortened walk
    (max 2 attempts). A successes-proportional attempts ceiling
    keeps retries from self-amplifying. GEMINI_STORM_BREAKER=0 off.
(c) Single-flight: concurrent identical post_json calls (same
    source + URL + payload) coalesce onto one upstream execution;
    waiters share a deep copy of the leader's result, or run the
    call themselves if the leader failed/timed out. Leased calls
    never coalesce (their accounting is per-lease).
    GEMINI_SINGLEFLIGHT=0 off.
(d) Leases: manager-issued bulk budgets in gemini_leases.
    request_lease(source, job_id, calls) -> lease dict or None
    (denied). post_json(lease_id=...) charges one unit per upstream
    attempt; exhaustion aborts the walk with a typed lease_exhausted.
    Bulk sources without an explicit lease are charged against a
    generous standing per-source daily default lease
    (GEMINI_LEASE_DEFAULT_CALLS, default 100000) so accounting is
    real but nothing breaks on day one. GEMINI_LEASES=0 disables
    charging entirely.

Stage 3 — get smart (key-manager overhaul, 2026-10-18):
(a) Burn tracking: every attempt increments gemini_key_model_day
    (Pacific day, per key+model). When an RPD park lands, the day's
    count for that key+model is learned into gemini_key_limits as an
    OBSERVED daily limit (never a published prior). The health
    payload exposes per-key served-today, the Pacific reset time,
    learned limits, and a capacity forecast in hours at the last
    hour's pool-wide burn rate. GEMINI_BURN_TRACKING=0 off.
(b) Prober slimming: passive health from real traffic is the
    default evidence. Under GEMINI_PROBER_SLIM (default on), the
    prober no longer re-confirms idle healthy keys, probes an
    unknown-state key once rather than cycling it, and a key leaving
    cooldown earns exactly ONE validation probe per cooldown exit
    (keyed to the exact backoff value); non-primary model probes
    run only while that model is hot or dead-marked. Flag off
    restores the legacy cycling prober.
(c) Sticky sessions: post_json(session_id=...) (proxy header
    X-Pool-Session) pins the session to the key that served it for
    30 minutes; the pinned key is tried first and the pin re-forms
    on the serving key whenever routing moves. Cached-content
    tokens are counted only when the API actually returns
    usageMetadata.cachedContentTokenCount — observed, never
    estimated. GEMINI_STICKY_SESSIONS=0 off.
(d) Spend ledger: gemini_source_day rolls up calls / failures /
    paid-key calls per source per Pacific day, exposed in the
    health payload with a spike flag when a source's paid calls
    run >= 3x its trailing 7-day average (and >= 10). Paid DOLLARS
    are never reported — no per-call price exists in this codebase,
    so the ledger counts paid calls, not invented money.
    GEMINI_SPEND_LEDGER=0 off.

Key Manager v2 (2026-10-20) — project-aware capacity:
- Project grouping: Gemini quotas are PER PROJECT, not per key. Each
  key row carries a `project` tag (admin-set; empty = the key is its
  own project, effective id "key-<id>"). An RPD (per-day) park lands
  on the key's WHOLE project group until the Pacific reset, and the
  day's summed burn is learned as the project's observed limit in
  gemini_project_limits. GEMINI_KEY_PROJECTS="label=project,..."
  can seed tags by label at ensure-table time.
- Pre-call quota ledger: per-(project, model) daily + per-minute
  usage is checked BEFORE selection against configured limits
  (GEMINI_DAILY_LIMITS / GEMINI_RPM_LIMITS JSON, learned project
  limits, and a built-in free-tier default for flash-lite models:
  500 RPD / 10 RPM). Projects at/over a limit are filtered out of
  every selection phase; when nothing remains, the call gets the
  typed local verdict pool_busy/"ledger" instead of a provider 429.
- Circuit breakers: per-key rolling 60 s outcomes (real traffic).
  >= GEMINI_BREAKER_MIN_ATTEMPTS (5) attempts with > 50% failures
  benches the key for GEMINI_BREAKER_COOLDOWN_S (60 s). A success
  clears the bench and the outcome window — and also clears
  needs_attention: a key that just served (e.g. the funded paid key
  via an admin test) is rehabilitated, not parked forever.
  GEMINI_BREAKER=0 disables.
- Retry discipline: a post_json call makes at most
  1 + GEMINI_CROSS_KEY_RETRIES (default 1) key attempts across the
  fast + sweep phases (an explicit caller max_attempts still wins).
  The reserve-fallback pass keeps its bounded quota-only extension,
  but only when it reaches a project not yet tried — a different
  project's quota is the one retry that carries information.
- Usage-based selection: candidate ordering ranks by the project's
  remaining daily-budget fraction (ledger-aware) ahead of the
  classic health/day-count/LRU signals.
- Priority lanes: post_json(lane=...) — "interactive" (default),
  "proactive_morning", "proactive_ambient", "batch". Morning maps
  to the interactive reserve class (time-critical); ambient/batch
  map to bulk. When the pool's remaining budget fraction (projects
  with known limits, primary model) falls below the lane's floor
  (GEMINI_LANE_MIN_BUDGET_BATCH 0.15 / _AMBIENT 0.08 /
  _MORNING 0.03), the call is refused up front with the typed
  pool_busy/"lane_budget" signal + retry_after — batch yields
  first, chat last (interactive has no floor).
- Paid guardrails: the paid tier is a funded overflow lane with a
  daily ceiling (GEMINI_PAID_DAILY_CEILING, default 1000 attempts
  per Pacific day across paid keys; 0 = no ceiling). At the ceiling
  paid keys leave every candidate list. Crossing
  GEMINI_PAID_TRICKLE_DAILY (25) logs a once-a-day alert; both are
  surfaced in degradation_info()["paid"].
- Admin rollup: degradation_info()["v2"] carries per-project
  remaining budget, keys-by-state, paid usage vs ceiling, and a
  pool-exhaustion projection at the current burn rate; burn gains
  seconds_to_reset + preferred_batch_window (the 3 h after the
  Pacific reset — when the Her Day batch belongs).
  GEMINI_KEY_V2=0 turns the v2 selection/ledger/lane behaviors off
  (schema + reporting stay).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

from . import config, db

_lock = threading.Lock()
logger = logging.getLogger(__name__)

# Backoff ladder for quota/429 failures, indexed by consecutive_failures.
_BACKOFF_MINUTES = (5, 15, 60, 240)

# Pause ladder (SECONDS) for transient errors — 5xx, timeouts, network
# drops — indexed by the key's current transient streak. A transient
# failure is a brief pause only: it never increments
# consecutive_failures and never degrades the key (error-class
# separation, 2026-10-15).
_TRANSIENT_PAUSE_S = (15.0, 30.0, 60.0)
# A transient streak older than this starts over at the ladder's foot.
_TRANSIENT_STREAK_RESET_S = 600.0

# Per-key rate-limiting windows (in-memory, per process).
_DISPATCH_WINDOW_S = 60.0
_FAILURE_WINDOW_S = 300.0
# How long after a reserve-pool tap the pool reports degraded.
_DEGRADED_WINDOW_S = 600.0
# Avg latency above this (seconds) adds a health-score penalty.
_LATENCY_PENALTY_S = 10.0
_MAX_LATENCY_SAMPLES = 20

# --- Fail-fast request path -------------------------------------------------
# Sources that fail fast when the pool is known down (see post_json's
# fail_fast): live-voice traffic (Rover's call brain + TTS on the VM)
# makes many proxy calls per minute mid-call, so a per-request sweep
# of a dead pool multiplies fast. Matched as a source-name prefix.
_FAIL_FAST_SOURCE_PREFIXES = ("roof-rover-voice",)
# How long a single attempt on a believed-healthy key may take before
# failover. Real generations on a healthy key finish well inside this;
# a dead/slow key must not eat the whole caller timeout serially.
_FAST_TIMEOUT_DEFAULT_S = 8.0
# Max attempts (across all phases) for one post_json call.
_MAX_ATTEMPTS_DEFAULT = 8
# Extra attempts granted to the reserve/fallback pass (post_json
# phase 3) beyond the shared cap — only when every attempt so far
# failed with the quota class. A quota wall is per-project: the
# active pool being walled says nothing about held-back keys, whose
# quotas are separate. A transient storm is upstream weather and
# earns no extension (the fallback keys face the same weather).
_RESERVE_FALLBACK_MAX_EXTRA = 5
# Total wall-clock budget for one post_json call across attempts.
_ATTEMPT_BUDGET_DEFAULT_S = 50.0
# Ranking latency assumed for a believed-healthy key with no samples yet.
_UNKNOWN_LATENCY_S = 2.0
# A confirmed success keeps a key "believed healthy" for this long
# (real traffic and the prober refresh it well before it lapses).
_BELIEF_TTL_S = 600.0

# --- Model fallback chain (2026-10-17) --------------------------------------
# The chat model is an ordered chain (primary first), not a single name.
# GEMINI_MODEL_CHAIN overrides the default
# "gemini-3.1-flash-lite,gemini-3.5-flash-lite".
# (gemini-2.5-flash-lite was the original fallback; Google retired it
# for these projects — a 404 "no longer available to new users" — on
# 2026-10-17, which is why the fallback is now 3.5-flash-lite, the
# replacement named in Google's own 404 message.)
_MODEL_CHAIN_DEFAULT = "gemini-3.1-flash-lite,gemini-3.5-flash-lite"
_GEMINI_API_BASE = ("https://generativelanguage.googleapis.com"
                    "/v1beta/models")
_MODEL_IN_URL_RE = re.compile(r"/models/([^/:?]+)")

# Per-model overload evidence: how far back overload failures count,
# how many distinct keys (or total failures) mark a model hot, and the
# default hot-cooldown window (overridable via GEMINI_MODEL_COOLDOWN_S).
_MODEL_OVERLOAD_WINDOW_S = 300.0
_MODEL_OVERLOAD_MIN_KEYS = 2
_MODEL_OVERLOAD_MIN_EVENTS = 3
_MODEL_COOLDOWN_DEFAULT_S = 120.0

# Error-body hints that an upstream failure is a MODEL-level outage
# (the shape of a real Gemini overload 503: "The model is overloaded.
# Please try again later.", status "UNAVAILABLE") rather than a key or
# request problem.
_OVERLOAD_BODY_HINTS = ("overloaded", "unavailable")

# How often the background prober re-checks each non-primary chain
# model (fallback readiness), at most.
_PROBE_MODEL_INTERVAL_S = 120.0

# --- Background prober ------------------------------------------------------
# Lean settings (efficiency pass, 2026-10-15): the prober used to
# generate the overwhelming majority of usage-log rows (455 of the last
# 500 at the 2026-10-15 baseline) by re-confirming idle healthy keys
# every ~2 minutes. It now wakes every 30s, sends at most
# _PROBE_MAX_PER_CYCLE probes (recovery checks first), re-confirms an
# idle healthy key only after _PROBE_CONFIRM_S, and never probes the
# same key more often than _PROBE_MIN_INTERVAL_S.
_PROBE_INTERVAL_DEFAULT_S = 30.0   # prober wake-up cadence
_PROBE_CONFIRM_S = 420.0           # re-confirm an idle healthy key (< TTL)
_PROBE_MIN_INTERVAL_S = 120.0      # never probe the same key more often
_PROBE_QUIET_S = 120.0             # real traffic fresher than this = no probe
_PROBE_MAX_PER_CYCLE = 4           # hard cap on probes sent per cycle
_PROBE_TIMEOUT_S = 12.0

# In-memory per-process state, guarded by _lock. The DB columns remain
# the source of truth for consecutive failures and backoff windows;
# these structures only carry soft signals (rate, recent failures,
# latency) that don't need to survive restarts.
_dispatch_times: dict[int, deque] = {}  # key_id -> dispatch timestamps (60s)
_recent_fail_ts: dict[int, deque] = {}  # key_id -> failure timestamps (5min)
_latencies: dict[int, deque] = {}       # key_id -> recent request latencies (s)
_last_reserve_tap: float | None = None  # monotonic time of last reserve use

# Believed-health view for the fail-fast path, guarded by _lock:
# key_id -> {"ok": bool, "at": wall-clock of last confirmation,
#            "lat": EWMA latency seconds or None}
_belief: dict[int, dict] = {}
_belief_seeded = False

# Prober bookkeeping, guarded by _lock.
_prober_thread: threading.Thread | None = None
_last_probe_mono: dict[int, float] = {}  # key_id -> monotonic time of last probe
_prober_last_cycle_at: float | None = None
_probes_total = 0
_probes_ok = 0

# Transient-failure streaks, guarded by _lock: key_id -> (streak count,
# monotonic time of last transient failure). In-memory only — a
# process restart simply restarts every streak at the ladder's foot,
# which is the forgiving direction.
_transient_streaks: dict[int, tuple[int, float]] = {}

# Per-model transient health (model fallback, 2026-10-17), guarded by
# _lock. In-memory only: a restart forgets hot flags, which is the
# forgiving direction (traffic retries the primary once).
_model_hot_until: dict[str, float] = {}      # model -> monotonic expiry
_model_overload_events: dict[str, list] = {}  # model -> [(mono_ts, key_id)]
_model_probe_at: dict[str, float] = {}        # model -> mono of last probe
_model_belief: dict[str, dict] = {}           # model -> {"ok","at","key_id"}

# --- Stage 1: see clearly (2026-10-18) ---------------------------------------
# Globally-dead models (model-not-found 404 on a chain model), guarded
# by _lock: model -> monotonic expiry of the dead mark. Past the
# recheck window the model is treated as unknown again — one attempt
# may test it; a failure re-marks it, a success clears it. In-memory
# only, like the rest of the model tracker: a restart forgets the
# mark, which is the forgiving direction.
_model_dead_until: dict[str, float] = {}
_DEAD_MODEL_RECHECK_S = 21600.0  # 6 h
# Cap on an RPM park: Google's retryDelay for a per-minute wall is
# seconds-scale; anything longer is treated as daily evidence.
_RPM_PARK_CAP_S = 120.0
# A retryDelay at/above this with no explicit per-day marker is read
# as daily-quota evidence (a per-minute limit never asks for 5+ min).
_RPD_DELAY_HINT_S = 300.0
# How much of an error body is read for classification/forensics (the
# log line still only shows the first 300 chars).
_ERROR_BODY_READ = 8192

_CAPABILITIES = ("text", "image", "tts", "grounding")


def _flag(name: str, default: str = "1") -> bool:
    """Boolean env flag: '0'/'false'/'no' (any case) mean off."""
    return (os.environ.get(name, default) or "").strip().lower() \
        not in ("0", "false", "no", "off")


def _quota_classify_on() -> bool:
    return _flag("GEMINI_QUOTA_CLASSIFY", "1")


def _health_grid_on() -> bool:
    return _flag("GEMINI_HEALTH_GRID", "1")


def _forensics_on() -> bool:
    return _flag("GEMINI_KEY_FORENSICS", "1")


# --- Stage 3 flags -------------------------------------------------------------

def _burn_on() -> bool:
    return _flag("GEMINI_BURN_TRACKING", "1")


def _prober_slim_on() -> bool:
    return _flag("GEMINI_PROBER_SLIM", "1")


def _sticky_on() -> bool:
    return _flag("GEMINI_STICKY_SESSIONS", "1")


def _ledger_on() -> bool:
    return _flag("GEMINI_SPEND_LEDGER", "1")


def _pt_day_str(ts: float | None = None) -> str:
    """The current Pacific (America/Los_Angeles) calendar day,
    YYYY-MM-DD — Gemini daily quotas and all Stage-3 day tables key
    off this day. Same zoneinfo + manual-DST fallback approach as
    _next_pt_midnight_epoch."""
    ts = time.time() if ts is None else ts
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(
            ts, tz=ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        pass
    utc = datetime.fromtimestamp(ts, tz=timezone.utc)

    def _nth_sunday(year: int, month: int, n: int) -> int:
        first = datetime(year, month, 1)
        return 1 + (6 - first.weekday()) % 7 + 7 * (n - 1)

    dst_start = datetime(utc.year, 3, _nth_sunday(utc.year, 3, 2), 10,
                         tzinfo=timezone.utc)
    dst_end = datetime(utc.year, 11, _nth_sunday(utc.year, 11, 1), 9,
                       tzinfo=timezone.utc)
    offset_h = -7 if dst_start <= utc < dst_end else -8
    return (utc + timedelta(hours=offset_h)).strftime("%Y-%m-%d")


def _next_pt_midnight_epoch(now_ts: float | None = None) -> float:
    """Epoch seconds of the next midnight in America/Los_Angeles.

    Gemini daily quotas reset at Pacific midnight. Uses zoneinfo
    when the tz database is present; otherwise falls back to the US
    DST rule (PDT = UTC-7 from the second Sunday of March to the
    first Sunday of November, PST = UTC-8 otherwise) so the park
    never depends on tzdata being installed."""
    ts = time.time() if now_ts is None else now_ts
    try:
        from zoneinfo import ZoneInfo
        pt = ZoneInfo("America/Los_Angeles")
        local = datetime.fromtimestamp(ts, tz=pt)
        midnight = (local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()
    except Exception:
        pass
    # Manual fallback: find the UTC offset in force at ts.
    utc = datetime.fromtimestamp(ts, tz=timezone.utc)

    def _nth_sunday(year: int, month: int, n: int) -> int:
        first = datetime(year, month, 1)
        days = (6 - first.weekday()) % 7  # weekday(): Mon=0 .. Sun=6
        return 1 + days + 7 * (n - 1)

    dst_start = datetime(utc.year, 3, _nth_sunday(utc.year, 3, 2), 10,
                         tzinfo=timezone.utc)
    dst_end = datetime(utc.year, 11, _nth_sunday(utc.year, 11, 1), 9,
                       tzinfo=timezone.utc)
    offset_h = -7 if dst_start <= utc < dst_end else -8
    local = utc + timedelta(hours=offset_h)
    midnight_local = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    return (midnight_local - timedelta(hours=offset_h)).replace(
        tzinfo=timezone.utc).timestamp()


_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def _parse_retry_delay_s(body: str | None) -> float | None:
    """Google's RetryInfo retryDelay ("39s") from an error body."""
    if not body:
        return None
    m = _RETRY_DELAY_RE.search(body)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _classify_quota(body: str | None) -> tuple[str | None, float | None]:
    """Classify a 429 body as (kind, retry_delay_s): kind is 'rpd'
    (per-day), 'rpm' (per-minute), or None when the payload carries
    no usable dimension evidence — the caller then keeps the classic
    backoff ladder. Dimension evidence is read from the quotaId /
    quotaMetric / quota_unit strings Google embeds in the
    QuotaFailure/ErrorInfo details; a very long retryDelay with no
    explicit marker is treated as daily evidence."""
    delay = _parse_retry_delay_s(body)
    text = (body or "").lower()
    if "perday" in text or "requestsperday" in text \
            or '"1/d/' in text or "per_day" in text:
        return "rpd", delay
    if "perminute" in text or "requestsperminute" in text \
            or '"1/min' in text or "per_minute" in text:
        return "rpm", delay
    if delay is not None and delay >= _RPD_DELAY_HINT_S:
        return "rpd", delay
    return None, delay


def _permanent_failure_reason(code: int | None,
                              body: str | None) -> str | None:
    """Plain cause label when an error body carries a PERMANENT
    key/project failure signature — the key will never recover on its
    own, so it belongs in needs_attention with the cause attached,
    not in a cooldown. Returns None for ordinary failures."""
    if code not in (400, 401, 402, 403):
        return None
    text = (body or "").lower()
    if "service account" in text and ("deleted" in text
                                      or "disabled" in text):
        return "Google service account deleted or disabled"
    if "api_key_invalid" in text or "api key not valid" in text:
        return "API key not valid (revoked or mistyped)"
    if "has not been used in project" in text \
            or "service_disabled" in text \
            or "api has not been used" in text:
        return "Gemini API not enabled in this key's project"
    return None


def _request_capability(url: str, payload: dict | None) -> str:
    """The capability a request exercises: 'grounding' when the
    payload asks for Google Search grounding, 'tts' for speech
    models, 'image' for image models/output, else 'text'."""
    payload = payload or {}
    tools = payload.get("tools") or []
    for tool in tools:
        if isinstance(tool, dict) and any(
                "google_search" in str(k).lower() for k in tool):
            return "grounding"
    model = (_model_from_url(url) or "").lower()
    if "tts" in model:
        return "tts"
    gen = payload.get("generationConfig") or {}
    modalities = [str(m).upper() for m in
                  (gen.get("responseModalities") or [])]
    if "image" in model or "IMAGE" in modalities:
        return "image"
    return "text"


def _cell_model(model: str | None, capability: str) -> str:
    """The model a cell is keyed by. Grounding quota is a project
    feature, not a per-model one, so grounding cells are keyed '*'."""
    if capability == "grounding":
        return "*"
    return model or "*"


# --- Stage 2: protect (2026-10-18) -------------------------------------------
# Storm-breaker state, guarded by _lock: rolling real-attempt outcomes
# (mono_ts, ok) and the open-until timestamp.
_storm_events: deque = deque()
_storm_open_until: float = 0.0
_STORM_WINDOW_S = 60.0
_STORM_MIN_EVENTS = 20
_STORM_FAIL_SHARE = 0.5
# Single-flight registry, guarded by _flight_lock.
_flight_lock = threading.Lock()
_flights: dict[str, dict] = {}
_flight_hits = 0
_flight_leader_runs = 0

# Source classes for lanes/leases. Interactive is the safe default:
# only sources explicitly known to be bulk are treated as bulk (plus
# env additions). The live-call voice source starts with the bulk
# "roof-rover" prefix, so interactive membership is checked FIRST.
_BULK_EXACT_SOURCES = {
    "prober", "whispr-facecheck", "whispr-code-review",
    "whispr-dev-bridge", "whispr-browser-bridge", "whispr-public-ip",
    "hannah-image", "hannah-attachments", "prompt-studio",
}
_BULK_SOURCE_PREFIXES = ("roof-rover", "warmup", "her-day", "give-")
_INTERACTIVE_EXACT_SOURCES = {
    "roof-rover-voice", "whispr-chat", "whispr-duo", "whispr-voice",
    "whispr-search", "sims-think", "creator-bot", "whispr-vision",
}


def _env_list(name: str) -> set[str]:
    raw = (os.environ.get(name, "") or "").strip()
    return {p.strip() for p in raw.split(",") if p.strip()}


def _source_class(source: str | None) -> str:
    """'bulk' or 'interactive' for a post_json source tag."""
    s = (source or "unknown").strip()
    if s in _INTERACTIVE_EXACT_SOURCES \
            or s in _env_list("GEMINI_INTERACTIVE_SOURCES"):
        return "interactive"
    if s in _BULK_EXACT_SOURCES \
            or s in _env_list("GEMINI_BULK_SOURCES") \
            or any(s.startswith(p) for p in _BULK_SOURCE_PREFIXES):
        return "bulk"
    return "interactive"


def _lanes_on() -> bool:
    return _flag("GEMINI_LANES", "1")


def _reserve_interactive_count() -> int:
    try:
        return max(0, int(os.environ.get(
            "GEMINI_RESERVE_INTERACTIVE", "3")))
    except (TypeError, ValueError):
        return 3


def _lane_reserve_ids(conn) -> set[int]:
    """The free keys reserved for interactive traffic: the K highest
    ids among active, attention-free free keys (K =
    GEMINI_RESERVE_INTERACTIVE). Chosen from usable keys so dead keys
    (e.g. the rested needs_attention ids at the top of the range)
    never waste reserve slots."""
    if not _lanes_on():
        return set()
    k = _reserve_interactive_count()
    if k <= 0:
        return set()
    try:
        rows = conn.execute(
            """SELECT id FROM gemini_keys
               WHERE is_paid = 0 AND is_active = 1
                 AND needs_attention = 0
               ORDER BY id DESC LIMIT ?""",
            (k,),
        ).fetchall()
        return {r["id"] for r in rows}
    except Exception:
        return set()


def _pool_busy_retry_after(lane_ids: set[int]) -> int:
    """Seconds until the earliest general (non-reserve) key leaves
    its backoff — the honest retry_after for a pool_busy rejection."""
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            rows = conn.execute(
                """SELECT id, quota_exhausted_until FROM gemini_keys
                   WHERE is_active = 1 AND needs_attention = 0
                     AND quota_exhausted_until IS NOT NULL
                     AND quota_exhausted_until > datetime('now')"""
            ).fetchall()
        finally:
            conn.close()
        waits = []
        now = time.time()
        for r in rows:
            if r["id"] in lane_ids:
                continue
            until = _parse_ts(r["quota_exhausted_until"])
            if until:
                waits.append(until - now)
        if waits:
            return int(max(5, min(min(waits), 900)))
    except Exception:
        pass
    return 60


def _storm_breaker_on() -> bool:
    return _flag("GEMINI_STORM_BREAKER", "1")


def _storm_open_s() -> float:
    return max(1.0, _env_float("GEMINI_STORM_OPEN_S", 30.0))


def _note_storm_event_locked(ok: bool) -> None:
    """Record one real attempt outcome and trip the breaker when the
    rolling window goes bad. Caller must hold _lock."""
    global _storm_open_until
    now = time.monotonic()
    _storm_events.append((now, ok))
    cutoff = now - 120.0
    while _storm_events and _storm_events[0][0] <= cutoff:
        _storm_events.popleft()
    if not _storm_breaker_on() or _storm_open_until > now:
        return
    recent = [(ts, o) for ts, o in _storm_events
              if ts > now - _STORM_WINDOW_S]
    if len(recent) >= _STORM_MIN_EVENTS:
        fails = sum(1 for _ts, o in recent if not o)
        if fails / len(recent) >= _STORM_FAIL_SHARE:
            _storm_open_until = now + _storm_open_s()
            logger.warning(
                "gemini_keys: STORM BREAKER open for %.0fs "
                "(%d/%d attempts failed in the last %.0fs)",
                _storm_open_s(), fails, len(recent), _STORM_WINDOW_S)


def _storm_stats() -> dict:
    """(open, seconds_left, events_60s, failures_60s, successes_60s)."""
    now = time.monotonic()
    with _lock:
        recent = [(ts, o) for ts, o in _storm_events
                  if ts > now - _STORM_WINDOW_S]
        fails = sum(1 for _ts, o in recent if not o)
        return {
            "open": _storm_open_until > now,
            "open_s_left": max(0.0, _storm_open_until - now),
            "events_60s": len(recent),
            "failures_60s": fails,
            "successes_60s": len(recent) - fails,
        }


def _singleflight_on() -> bool:
    return _flag("GEMINI_SINGLEFLIGHT", "1")


# --- Stage 3 state -----------------------------------------------------------
# Cooldown exits that already earned their one slim-prober validation
# probe, keyed by the exact backoff epoch — a NEW exit (a different
# backoff value) earns a fresh one.
_probe_validated_until: dict[int, float] = {}
# Sticky sessions: session id -> {"key_id", "expires"} (monotonic).
_sessions: dict[str, dict] = {}
_SESSION_TTL_S = 1800.0
# Cached-content tokens actually reported by the API in
# usageMetadata (observed only; never estimated).
_cached_tokens_total = 0


def _flight_key(source: str, url: str, payload: dict) -> str:
    raw = json.dumps(payload or {}, sort_keys=True, default=str)
    return hashlib.sha256(
        (source + "\n" + url + "\n" + raw).encode("utf-8")).hexdigest()


# --- Leases (Stage 2d) ---------------------------------------------------------

def _leases_on() -> bool:
    return _flag("GEMINI_LEASES", "1")


def _lease_max_calls() -> int:
    try:
        return max(1, int(os.environ.get("GEMINI_LEASE_MAX_CALLS",
                                         "5000")))
    except (TypeError, ValueError):
        return 5000


def _lease_max_active_per_source() -> int:
    try:
        return max(1, int(os.environ.get(
            "GEMINI_LEASE_MAX_ACTIVE_PER_SOURCE", "8")))
    except (TypeError, ValueError):
        return 8


def _lease_default_calls() -> int:
    try:
        return max(1, int(os.environ.get("GEMINI_LEASE_DEFAULT_CALLS",
                                         "100000")))
    except (TypeError, ValueError):
        return 100000


def _lease_truthful_cap_on() -> bool:
    """Pool fix (2026-10-18): the per-source active-lease cap counts
    only leases that can still represent real concurrency. Off (0)
    restores the original Stage-2 accounting (every unexpired
    'active' row counts, however dead its holder is)."""
    return _flag("GEMINI_LEASE_TRUTHFUL_CAP", "1")


def _lease_idle_reap_s() -> float:
    """Seconds of charge silence after which an active lease is
    treated as a dead holder (its scan finished, paused, or was
    interrupted — continuations run under fresh job ids) and reaped
    at the next issuance for the same source."""
    try:
        return max(60.0, float(os.environ.get(
            "GEMINI_LEASE_IDLE_REAP_S", "1800")))
    except (TypeError, ValueError):
        return 1800.0


def _lease_row_to_dict(r) -> dict:
    return {
        "lease_id": r["lease_id"],
        "source": r["source"],
        "job_id": r["job_id"],
        "budget_total": r["budget_total"],
        "budget_used": r["budget_used"],
        "budget_left": max(0, r["budget_total"] - r["budget_used"]),
        "status": r["status"],
        "expires_at": r["expires_at"],
    }


def request_lease(source: str, job_id: str, calls: int,
                  ttl_s: float = 3600.0) -> dict | None:
    """Issue a bulk lease: `calls` upstream attempts for
    (source, job_id). Idempotent per (source, job_id) while the
    lease is active AND still has budget. Returns the lease dict,
    or None when denied (leases off, invalid size, or the source
    already holds the max active leases)."""
    return _issue_lease(source, job_id, calls, ttl_s,
                        enforce_caps=True, roll_when_exhausted=False)


def _issue_lease(source: str, job_id: str, calls: int,
                 ttl_s: float = 3600.0,
                 enforce_caps: bool = True,
                 roll_when_exhausted: bool = False) -> dict | None:
    if not _leases_on():
        return None
    source = (source or "unknown")[:64]
    job_id = (job_id or "")[:128]
    if not job_id:
        return None
    try:
        calls = int(calls)
    except (TypeError, ValueError):
        return None
    if calls < 1 or (enforce_caps and calls > _lease_max_calls()):
        return None
    import secrets as _secrets
    now = time.time()
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            existing = conn.execute(
                """SELECT * FROM gemini_leases
                   WHERE source = ? AND job_id = ? AND status = 'active'
                     AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY created_at DESC LIMIT 1""",
                (source, job_id, now),
            ).fetchone()
            if existing and (not roll_when_exhausted
                             or existing["budget_used"]
                             < existing["budget_total"]):
                return _lease_row_to_dict(existing)
            truthful = enforce_caps and _lease_truthful_cap_on()
            if truthful:
                # Pool fix (2026-10-18): reap this source's dead
                # holders before counting. A lease silent for the
                # idle window belongs to a scan that finished,
                # paused, or died; its continuation runs under a
                # fresh job id, so the row is not concurrency.
                # Leases predating last_charge_at fall back to their
                # created_at; an unparseable row is never reaped.
                reaped = conn.execute(
                    """UPDATE gemini_leases SET status = 'expired'
                       WHERE source = ? AND status = 'active'
                         AND (expires_at IS NULL OR expires_at > ?)
                         AND COALESCE(last_charge_at,
                                      CAST(strftime('%s', created_at)
                                           AS REAL), ?)
                             < ?""",
                    (source, now, now,
                     now - _lease_idle_reap_s()),
                )
                if reaped.rowcount:
                    conn.commit()
                    logger.warning(
                        "gemini_keys: reaped %d idle lease(s) for "
                        "source %s (silent > %ss) before cap check",
                        reaped.rowcount, source,
                        int(_lease_idle_reap_s()))
            if truthful:
                # The manager's own default accounting leases are
                # issued cap-exempt by design and must not eat the
                # source's explicit slots; an exhausted lease can
                # never charge again, so it is not concurrency
                # either. Both still count when the flag is off.
                active = conn.execute(
                    """SELECT COUNT(*) AS n FROM gemini_leases
                       WHERE source = ? AND status = 'active'
                         AND (expires_at IS NULL OR expires_at > ?)
                         AND job_id NOT LIKE 'default:%'
                         AND budget_used < budget_total""",
                    (source, now),
                ).fetchone()["n"]
            else:
                active = conn.execute(
                    """SELECT COUNT(*) AS n FROM gemini_leases
                       WHERE source = ? AND status = 'active'
                         AND (expires_at IS NULL OR expires_at > ?)""",
                    (source, now),
                ).fetchone()["n"]
            if enforce_caps and active >= _lease_max_active_per_source():
                return None
            lease_id = "lease_" + _secrets.token_hex(8)
            conn.execute(
                """INSERT INTO gemini_leases
                       (lease_id, source, job_id, budget_total,
                        budget_used, created_at, expires_at, status,
                        last_charge_at)
                   VALUES (?, ?, ?, ?, 0, datetime('now'), ?, 'active',
                           ?)""",
                (lease_id, source, job_id, calls,
                 now + max(60.0, float(ttl_s)), now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM gemini_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            return _lease_row_to_dict(row)
        finally:
            conn.close()


def lease_status(lease_id: str) -> dict | None:
    """Current state of one lease (for callers/admin)."""
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT * FROM gemini_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            return _lease_row_to_dict(row) if row else None
        finally:
            conn.close()
    except Exception:
        return None


def release_lease(lease_id: str) -> bool:
    """Close a lease early (holder is done)."""
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            cur = conn.execute(
                """UPDATE gemini_leases SET status = 'released'
                   WHERE lease_id = ? AND status = 'active'""",
                (lease_id,),
            )
            conn.commit()
            return (cur.rowcount or 0) > 0
        finally:
            conn.close()


def _lease_charge(lease_id: str) -> bool:
    """Atomically spend one unit of a lease. False when the lease is
    unknown, released, expired, or exhausted."""
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            _now = time.time()
            cur = conn.execute(
                """UPDATE gemini_leases
                   SET budget_used = budget_used + 1,
                       last_charge_at = ?
                   WHERE lease_id = ? AND status = 'active'
                     AND budget_used < budget_total
                     AND (expires_at IS NULL OR expires_at > ?)""",
                (_now, lease_id, _now),
            )
            conn.commit()
            return (cur.rowcount or 0) > 0
        finally:
            conn.close()
    except Exception:
        return False


def _default_source_lease(source: str) -> str | None:
    """The standing daily default lease for a bulk source (Stage 2d):
    generous, persisted, and really charged, so unleased bulk traffic
    is accounted without breaking on day one. None when leases are
    off or issuance is denied (caller then runs unleased)."""
    if not _leases_on():
        return None
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Internal issuance: the public size cap does not apply (the
    # default must stay generous), and an exhausted default rolls
    # into a fresh lease via _issue_lease's has-budget check.
    lease = _issue_lease(source, f"default:{source}:{day}",
                         _lease_default_calls(), ttl_s=26 * 3600.0,
                         enforce_caps=False, roll_when_exhausted=True)
    return lease["lease_id"] if lease else None


def _model_is_dead(model: str | None) -> bool:
    """True while a model's global dead mark is inside its recheck
    window. Caller need not hold _lock (dict read is atomic)."""
    if not model:
        return False
    return _model_dead_until.get(model, 0.0) > time.monotonic()


def _mark_model_dead_locked(model: str, reason: str) -> None:
    """Mark a model dead globally. Caller must hold _lock."""
    _model_dead_until[model] = time.monotonic() + _DEAD_MODEL_RECHECK_S
    _model_hot_until.pop(model, None)
    logger.warning(
        "gemini_keys: model %s marked DEAD globally (%s) - the chain "
        "skips it for every key until the recheck window passes",
        model, reason)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _per_key_rpm() -> int:
    """Max dispatches per key per rolling 60s window. Free Gemini keys
    allow ~15 req/min; stay conservative so we rotate before a 429."""
    try:
        return max(1, int(os.environ.get("GEMINI_PER_KEY_RPM", "12")))
    except ValueError:
        return 12


def _fast_timeout_s() -> float:
    return max(1.0, _env_float("GEMINI_FAST_TIMEOUT_S", _FAST_TIMEOUT_DEFAULT_S))


def _max_attempts() -> int:
    try:
        return max(1, int(os.environ.get("GEMINI_MAX_ATTEMPTS",
                                         _MAX_ATTEMPTS_DEFAULT)))
    except (TypeError, ValueError):
        return _MAX_ATTEMPTS_DEFAULT


def _attempt_budget_s() -> float:
    return max(1.0, _env_float("GEMINI_ATTEMPT_BUDGET_S",
                               _ATTEMPT_BUDGET_DEFAULT_S))


def _hedge_after_s() -> float:
    """Seconds after which a second healthy key is raced (0 = off)."""
    return max(0.0, _env_float("GEMINI_HEDGE_AFTER_S", 0.0))


def _probe_interval_s() -> float:
    return max(5.0, _env_float("GEMINI_PROBE_INTERVAL_S",
                               _PROBE_INTERVAL_DEFAULT_S))


def model_chain() -> list[str]:
    """Ordered model fallback chain, primary first. Configurable via
    the GEMINI_MODEL_CHAIN env var (comma-separated); defaults to
    "gemini-3.1-flash-lite,gemini-3.5-flash-lite"."""
    raw = (os.environ.get("GEMINI_MODEL_CHAIN", "") or "").strip()
    if not raw:
        raw = _MODEL_CHAIN_DEFAULT
    chain: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in chain:
            chain.append(name)
    return chain or _MODEL_CHAIN_DEFAULT.split(",")


def model_url(model: str) -> str:
    """generateContent endpoint URL for a chain model."""
    return f"{_GEMINI_API_BASE}/{model}:generateContent"


def _model_cooldown_s() -> float:
    """How long an overloaded model stays hot (seconds)."""
    return max(1.0, _env_float("GEMINI_MODEL_COOLDOWN_S",
                               _MODEL_COOLDOWN_DEFAULT_S))


def _model_from_url(url: str) -> str | None:
    """The model named by a Gemini generateContent URL, or None for
    non-Gemini URLs."""
    m = _MODEL_IN_URL_RE.search(url or "")
    return m.group(1) if m else None


def _current_model_locked() -> str:
    """Model currently serving traffic: the first chain model neither
    in its overload cooldown nor marked globally dead (Stage 1 grid).
    Caller must hold _lock."""
    chain = model_chain()
    now = time.monotonic()
    grid = _health_grid_on()
    for model in chain:
        if grid and _model_dead_until.get(model, 0.0) > now:
            continue
        if _model_hot_until.get(model, 0.0) <= now:
            return model
    return chain[0]


def current_model() -> str:
    """Model currently serving traffic: the chain primary while it is
    healthy, the next chain model while the primary is hot (model-level
    overload cooldown). Falls back to the primary when every model is
    hot — the key pool's own fail-fast still bounds the attempts."""
    with _lock:
        return _current_model_locked()


def _is_model_overload(body: str | None) -> bool:
    """True when an upstream error body says the MODEL is overloaded or
    unavailable — a model-level outage every key would hit — rather
    than a problem with the key or the request."""
    text = (body or "").lower()
    return any(h in text for h in _OVERLOAD_BODY_HINTS)


def _is_model_not_found(body: str | None, model: str | None) -> bool:
    """True when a 404 body says the MODEL itself does not exist for
    the project — retired or never provisioned ("This model
    models/<name> is no longer available to new users...", status
    NOT_FOUND). Every key would get the same 404, so this is a
    model-level outage like overload, never a bad request: classing
    it 'request' would fatally abort the walk on the first key tried
    and block every fallback, including the paid key (the
    2026-10-17 outage, when the chain's fallback model was retired
    by Google while the primary was overloaded)."""
    if not model:
        return False
    text = (body or "").lower()
    if model.lower() not in text:
        return False
    return ("no longer available" in text or "not found" in text
            or "not_found" in text)


def _note_model_overload_locked(model: str, key_id: int) -> None:
    """Feed the per-model overload tracker. Caller must hold _lock.

    When overload evidence against `model` spans multiple keys (>=
    _MODEL_OVERLOAD_MIN_KEYS distinct keys, or >=
    _MODEL_OVERLOAD_MIN_EVENTS failures inside the evidence window),
    the model is marked hot for the cooldown window and subsequent
    post_json calls route to the next chain model."""
    now = time.monotonic()
    events = _model_overload_events.setdefault(model, [])
    events.append((now, key_id))
    cutoff = now - _MODEL_OVERLOAD_WINDOW_S
    events[:] = [(ts, kid) for ts, kid in events if ts > cutoff]
    if _model_hot_until.get(model, 0.0) > now:
        return  # already hot; the cooldown is not extended
    distinct = {kid for _ts, kid in events}
    if len(distinct) >= _MODEL_OVERLOAD_MIN_KEYS \
            or len(events) >= _MODEL_OVERLOAD_MIN_EVENTS:
        cooldown = _model_cooldown_s()
        _model_hot_until[model] = now + cooldown
        logger.warning(
            "gemini_keys: model %s overloaded (%d failures across %d "
            "keys) - hot for %.0fs, failing over down the chain",
            model, len(events), len(distinct), cooldown)


def _route_url(url: str) -> str:
    """Route a Gemini URL through the model fallback chain.

    When the URL names the chain's primary model, its model segment is
    rewritten to the model currently serving traffic — the primary
    itself while healthy (a no-op), the next chain model while the
    primary is hot. URLs naming any other model (explicit pins, e.g.
    the proxy's {model} path) pass through untouched, as do
    non-Gemini URLs."""
    model = _model_from_url(url)
    chain = model_chain()
    if not model or not chain or model != chain[0]:
        return url
    target = current_model()
    if target == model:
        return url
    return url.replace(f"/models/{model}:", f"/models/{target}:", 1)


def _model_health_info() -> list[dict]:
    """Per-model fallback-chain status, for degradation_info()."""
    chain = model_chain()
    with _lock:
        serving = _current_model_locked()
        now_mono = time.monotonic()
        out = []
        for i, model in enumerate(chain):
            hot_until = _model_hot_until.get(model, 0.0)
            hot = hot_until > now_mono
            belief = _model_belief.get(model)
            out.append({
                "model": model,
                "primary": i == 0,
                "serving": model == serving,
                "hot": hot,
                "hot_until": (
                    datetime.fromtimestamp(
                        time.time() + (hot_until - now_mono),
                        tz=timezone.utc).isoformat()
                    if hot else None),
                "probe_ok": belief.get("ok") if belief else None,
                "probe_at": (
                    datetime.fromtimestamp(
                        belief["at"], tz=timezone.utc).isoformat()
                    if belief and belief.get("at") else None),
            })
    return out


def _pick_model_probe_row_locked(rows) -> dict | None:
    """A key to probe a fallback model with: the believed-healthiest
    eligible row that is NOT inside a backoff window, has no fresh real
    traffic (probes must never starve real traffic), and was not
    probed recently. Caller must hold _lock. None when no row is
    eligible — the model simply waits for the next idle cycle."""
    now_wall = time.time()
    now_mono = time.monotonic()
    best = None
    for r in rows:
        until = _parse_ts(r["quota_exhausted_until"])
        if until is not None and until > now_wall:
            continue  # inside a backoff window: never probe
        if now_mono - _last_probe_mono.get(r["id"], 0.0) \
                < _PROBE_MIN_INTERVAL_S:
            continue
        dq = _dispatch_times.get(r["id"])
        if dq and (now_mono - dq[-1]) < _PROBE_QUIET_S:
            continue
        b = _belief.get(r["id"])
        if b and b.get("ok"):
            return r  # believed healthy beats every other signal
        if best is None:
            best = r
    return best


def _parse_ts(value) -> float | None:
    """Parse a stored UTC timestamp string to epoch seconds."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(
                value, fmt).replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError):
            continue
    return None


def _prune(dq: deque, cutoff: float) -> None:
    while dq and dq[0] <= cutoff:
        dq.popleft()


def _record_dispatch_locked(key_id: int) -> None:
    """Note that key_id was handed out. Caller must hold _lock."""
    now = time.monotonic()
    dq = _dispatch_times.setdefault(key_id, deque())
    _prune(dq, now - _DISPATCH_WINDOW_S)
    dq.append(now)


def _key_over_limit(key_id: int) -> bool:
    """True when key_id hit the per-key RPM cap in the rolling window.
    Caller must hold _lock."""
    dq = _dispatch_times.get(key_id)
    if not dq:
        return False
    _prune(dq, time.monotonic() - _DISPATCH_WINDOW_S)
    return len(dq) >= _effective_per_key_rpm()


def _under_limit(rows):
    """Filter eligible rows to keys under their per-key rate cap."""
    return [r for r in rows if not _key_over_limit(r["id"])]


def _note_failure_locked(key_id: int) -> None:
    """Record a failure timestamp for health scoring. Hold _lock."""
    now = time.monotonic()
    dq = _recent_fail_ts.setdefault(key_id, deque())
    _prune(dq, now - _FAILURE_WINDOW_S)
    dq.append(now)


def _recent_failure_count(key_id: int) -> int:
    dq = _recent_fail_ts.get(key_id)
    if not dq:
        return 0
    _prune(dq, time.monotonic() - _FAILURE_WINDOW_S)
    return len(dq)


def _belief_entry_locked(key_id: int) -> dict:
    """The belief record for key_id, created if needed. Hold _lock."""
    return _belief.setdefault(key_id, {"ok": False, "at": 0.0, "lat": None})


def _record_latency_locked(key_id: int, seconds: float) -> None:
    """Record a request latency sample. Caller must hold _lock."""
    dq = _latencies.setdefault(key_id, deque(maxlen=_MAX_LATENCY_SAMPLES))
    dq.append(max(0.0, seconds))
    entry = _belief_entry_locked(key_id)
    prev = entry.get("lat")
    entry["lat"] = seconds if prev is None else (prev * 0.6 + seconds * 0.4)


def _avg_latency(key_id: int) -> float | None:
    dq = _latencies.get(key_id)
    if not dq:
        return None
    return sum(dq) / len(dq)


def _health_score(key_id: int, consecutive_failures: int) -> float:
    """Lower is healthier. Weighs DB failure streak, recent failure
    bursts, and elevated latency."""
    score = float(consecutive_failures or 0) * 10.0
    score += _recent_failure_count(key_id) * 5.0
    avg = _avg_latency(key_id)
    if avg is not None and avg > _LATENCY_PENALTY_S:
        score += 5.0
    return score


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day_count(row) -> int:
    """Requests a key has served today (UTC), for pacing. Rows written
    before the day-counter migration (or stamped on another day) count
    as zero — every key starts each UTC day even."""
    try:
        stamp = row["day_stamp"]
        count = row["day_requests"]
    except (KeyError, IndexError):
        return 0
    if not stamp or not count:
        return 0
    return int(count) if stamp == _today_utc() else 0


def _next_transient_pause_locked(key_id: int) -> float:
    """Record a transient failure for key_id and return its pause in
    seconds (15 -> 30 -> 60 cap). Caller must hold _lock."""
    now = time.monotonic()
    streak, last = _transient_streaks.get(key_id, (0, 0.0))
    if now - last > _TRANSIENT_STREAK_RESET_S:
        streak = 0
    streak += 1
    _transient_streaks[key_id] = (streak, now)
    return _TRANSIENT_PAUSE_S[min(streak - 1, len(_TRANSIENT_PAUSE_S) - 1)]


def _seed_belief_locked(conn) -> None:
    """Seed the believed-healthy view from persisted health at first use,
    so a restart starts from the last known pool state instead of
    cold-probing on live traffic. Caller must hold _lock."""
    global _belief_seeded
    if _belief_seeded:
        return
    _belief_seeded = True
    try:
        rows = conn.execute(
            """SELECT id, consecutive_failures, last_success_at
               FROM gemini_keys
               WHERE is_active = 1 AND needs_attention = 0"""
        ).fetchall()
    except Exception:
        return
    for r in rows:
        if r["id"] in _belief:
            # Live evidence (an attempt/probe outcome recorded before
            # this lazy first seed ran) is fresher than the persisted
            # columns; never stomp it with older state.
            continue
        ts = _parse_ts(r["last_success_at"])
        ok = ts is not None and (r["consecutive_failures"] or 0) == 0
        _belief[r["id"]] = {"ok": bool(ok), "at": ts or 0.0, "lat": None}


def is_degraded() -> bool:
    """True while the pool is serving from reserve keys (10 min after
    the last reserve tap). Callers should slow down when degraded."""
    if _last_reserve_tap is None:
        return False
    return (time.monotonic() - _last_reserve_tap) < _DEGRADED_WINDOW_S


def _effective_per_key_rpm() -> int:
    """Per-key RPM cap, automatically halved while degraded so reserve
    keys are protected."""
    rpm = _per_key_rpm()
    if is_degraded():
        rpm = max(1, rpm // 2)
    return rpm


def paid_key_ids() -> list[int]:
    """Ids of the paid (fallback-tier) keys, so status consumers can
    count FREE healthy keys out of the believed-healthy set."""
    try:
        with _lock:
            conn = db.connect()
            try:
                _ensure_table(conn)
                return [r["id"] for r in conn.execute(
                    "SELECT id FROM gemini_keys WHERE is_paid = 1"
                ).fetchall()]
            finally:
                conn.close()
    except Exception:
        return []


def degradation_info() -> dict:
    """Machine-readable pool health for adaptive throttling by callers."""
    rpm = _per_key_rpm()
    degraded = is_degraded()
    tapped_at = None
    if _last_reserve_tap is not None:
        tapped_at = datetime.fromtimestamp(
            time.time() - (time.monotonic() - _last_reserve_tap),
            tz=timezone.utc,
        ).isoformat()
    info = {
        "degraded": degraded,
        "reserve_tapped": _last_reserve_tap is not None,
        "reserve_tapped_at": tapped_at,
        "per_key_rpm": rpm,
        "effective_per_key_rpm": _effective_per_key_rpm(),
        "advice": ("pool serving from reserves - halve your request rate"
                   if degraded else "pool healthy"),
    }
    # Live pool view: persisted per-key status counts, the fail-fast
    # belief set, and prober state (refreshed off the request path).
    try:
        with _lock:
            conn = db.connect()
            park_rows, cell_rows = [], []
            try:
                _ensure_table(conn)
                _seed_belief_locked(conn)
                rows = conn.execute("SELECT * FROM gemini_keys").fetchall()
                statuses = [_row_to_public(r)["status"] for r in rows]
                # Stage 1 observability inputs (queried while the
                # connection is open; assembled into info below).
                try:
                    park_rows = conn.execute(
                        """SELECT quota_park_kind, COUNT(*) AS n
                           FROM gemini_keys
                           WHERE quota_exhausted_until IS NOT NULL
                             AND quota_exhausted_until > datetime('now')
                           GROUP BY quota_park_kind""").fetchall()
                    cell_rows = conn.execute(
                        """SELECT status, COUNT(*) AS n
                           FROM gemini_key_cells
                           GROUP BY status""").fetchall()
                except Exception:
                    park_rows, cell_rows = [], []
            finally:
                conn.close()
            now = time.time()
            healthy_ids = sorted(
                kid for kid, b in _belief.items()
                if b.get("ok") and (now - b.get("at", 0.0)) <= _BELIEF_TTL_S)
            info["believed_healthy_key_ids"] = healthy_ids
            info["pool"] = {s: statuses.count(s) for s in sorted(set(statuses))}
            # Stage 1 observability: live quota parks by dimension,
            # forensics cause labels, health-grid cell counts, and
            # globally-dead models.
            info["quota_parks"] = {
                (r["quota_park_kind"] or "ladder"): r["n"]
                for r in park_rows}
            info["attention_reasons"] = {
                str(r["id"]): r["attention_reason"] for r in rows
                if r["needs_attention"]
                and _row_get(r, "attention_reason")}
            info["health_cells"] = {r["status"]: r["n"]
                                    for r in cell_rows}
            info["dead_models"] = [
                {"model": m,
                 "recheck_at": datetime.fromtimestamp(
                     time.time() + (until - time.monotonic()),
                     tz=timezone.utc).isoformat()}
                for m, until in sorted(_model_dead_until.items())
                if until > time.monotonic()]
            # Stage 2 observability: lanes, storm breaker, single
            # flight, leases.
            try:
                lane_conn = db.connect()
                try:
                    _ensure_table(lane_conn)
                    info["lanes"] = {
                        "enabled": _lanes_on(),
                        "reserve_key_ids": sorted(
                            _lane_reserve_ids(lane_conn)),
                        "reserve_count": _reserve_interactive_count(),
                    }
                    lease_rows = lane_conn.execute(
                        """SELECT source, COUNT(*) AS n,
                                  SUM(budget_used) AS used,
                                  SUM(budget_total) AS total
                           FROM gemini_leases WHERE status = 'active'
                             AND (expires_at IS NULL
                                  OR expires_at > ?)
                           GROUP BY source""",
                        (time.time(),),
                    ).fetchall()
                    info["leases"] = {
                        r["source"]: {
                            "active": r["n"], "used": r["used"] or 0,
                            "total": r["total"] or 0}
                        for r in lease_rows}
                finally:
                    lane_conn.close()
            except Exception:
                pass
            info["prober"] = {
                "running": bool(_prober_thread is not None
                                and _prober_thread.is_alive()),
                "last_cycle_at": (
                    datetime.fromtimestamp(_prober_last_cycle_at,
                                           tz=timezone.utc).isoformat()
                    if _prober_last_cycle_at else None),
                "probes_total": _probes_total,
                "probes_ok": _probes_ok,
                "slim": _prober_slim_on(),
            }
        # Storm + single-flight state (outside the lock above:
        # _storm_stats takes it itself).
        info["storm"] = _storm_stats()
        with _flight_lock:
            info["singleflight"] = {
                "enabled": _singleflight_on(),
                "hits": _flight_hits,
                "leader_runs": _flight_leader_runs,
                "in_flight": len(_flights),
            }
        # Stage 3: burn + forecast, spend ledger, sticky sessions.
        try:
            _day = _pt_day_str()
            conn = db.connect()
            try:
                _ensure_table(conn)
                served: dict = {}
                for r in conn.execute(
                        """SELECT key_id, model, calls
                           FROM gemini_key_model_day
                           WHERE pt_day = ?""", (_day,)).fetchall():
                    e = served.setdefault(
                        str(r["key_id"]), {"total": 0, "by_model": {}})
                    e["total"] += r["calls"]
                    e["by_model"][r["model"]] = r["calls"]
                limits = [
                    {"key_id": r["key_id"], "model": r["model"],
                     "daily_limit": r["daily_limit"],
                     "learned_from": r["learned_from"]}
                    for r in conn.execute(
                        "SELECT * FROM gemini_key_limits").fetchall()
                ]
                sources: dict = {}
                for r in conn.execute(
                        """SELECT source, calls, failures, paid_calls
                           FROM gemini_source_day WHERE pt_day = ?""",
                        (_day,)).fetchall():
                    sources[r["source"]] = {
                        "calls": r["calls"],
                        "failures": r["failures"],
                        "paid_calls": r["paid_calls"],
                        "paid_anomaly": False}
                for r in conn.execute(
                        """SELECT source,
                                  AVG(paid_calls) AS avg_paid
                           FROM gemini_source_day
                           WHERE pt_day < ? AND pt_day >= date(?, '-7 days')
                           GROUP BY source""",
                        (_day, _day)).fetchall():
                    s = sources.get(r["source"])
                    if s is None:
                        continue
                    avg = float(r["avg_paid"] or 0.0)
                    s["paid_avg_7d"] = round(avg, 2)
                    s["paid_anomaly"] = bool(
                        s["paid_calls"] >= 10
                        and s["paid_calls"] >= 3.0 * max(avg, 1.0))
                rate_row = conn.execute(
                    """SELECT COUNT(*) AS n FROM gemini_key_usage
                       WHERE created_at >= datetime('now', '-1 hour')"""
                ).fetchone()
                rate = float(rate_row["n"]) if rate_row else 0.0
                free_active = conn.execute(
                    """SELECT COUNT(*) AS n FROM gemini_keys
                       WHERE is_paid = 0 AND is_active = 1"""
                ).fetchone()["n"]
            finally:
                conn.close()
            primary = model_chain()[0]
            lim_by_key = {l["key_id"]: l["daily_limit"]
                          for l in limits if l["model"] == primary}
            remaining = 0
            for kid, lim in lim_by_key.items():
                used = served.get(str(kid), {}).get(
                    "by_model", {}).get(primary, 0)
                remaining += max(0, lim - used)
            if not lim_by_key:
                forecast = {"status": "learning",
                            "reason": "no observed RPD limits yet",
                            "hours": None}
            elif rate <= 0:
                forecast = {"status": "no_recent_burn",
                            "hours": None,
                            "keys_with_limits": len(lim_by_key)}
            else:
                forecast = {
                    "status": "ok",
                    "hours": round(remaining / rate, 1),
                    "model": primary,
                    "remaining_calls": remaining,
                    "burn_rate_per_hour": int(rate),
                    "keys_with_limits": len(lim_by_key),
                    "partial": len(lim_by_key) < int(free_active),
                }
            info["burn"] = {
                "pt_day": _day,
                "pt_reset_epoch": _next_pt_midnight_epoch(),
                "keys_served_today": served,
                "learned_limits": limits,
                "forecast": forecast,
            }
            info["ledger"] = {"pt_day": _day, "sources": sources}
            now_m = time.monotonic()
            with _lock:
                active_sessions = sum(
                    1 for e in _sessions.values()
                    if e["expires"] > now_m)
                cached_total = _cached_tokens_total
            info["sessions"] = {
                "enabled": _sticky_on(),
                "active": active_sessions,
                "cached_tokens_total_observed": cached_total,
            }
        except Exception:
            pass
        # Key Manager v2 (additive): paid guardrail state, reset
        # extras on the burn block, and the per-project rollup.
        try:
            info["paid"] = _paid_state()
            _win = batch_window()
            if isinstance(info.get("burn"), dict):
                info["burn"]["seconds_to_reset"] = \
                    _win["seconds_until_reset"]
                info["burn"]["preferred_batch_window"] = \
                    _win["in_preferred_window"]
            info["v2"] = _v2_rollup()
        except Exception:
            pass
        # Per-model fallback-chain status (outside the lock above:
        # _model_health_info takes it itself).
        info["models"] = _model_health_info()
    except Exception:
        pass  # health reporting must never break the caller
    return info

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS gemini_keys (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash             TEXT NOT NULL UNIQUE,
    key_value            TEXT NOT NULL,
    label                TEXT NOT NULL DEFAULT '',
    is_paid              INTEGER NOT NULL DEFAULT 0,
    is_active            INTEGER NOT NULL DEFAULT 1,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_at      TEXT,
    last_success_at      TEXT,
    total_requests       INTEGER NOT NULL DEFAULT 0,
    total_failures       INTEGER NOT NULL DEFAULT 0,
    quota_exhausted_until TEXT,
    needs_attention      INTEGER NOT NULL DEFAULT 0,
    last_used_at         TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_USAGE_TABLE = """
CREATE TABLE IF NOT EXISTS gemini_key_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    key_id     INTEGER,
    source     TEXT NOT NULL DEFAULT 'unknown',
    success    INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    error_class TEXT
)
"""


def _now_str() -> str:
    # Microsecond precision so back-to-back calls within the same
    # second still rotate correctly (ORDER BY last_used_at ASC).
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _ensure_table(conn) -> None:
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_USAGE_TABLE)
    # Migrations for pre-existing tables
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(gemini_keys)").fetchall()]
    if "last_used_at" not in cols:
        conn.execute("ALTER TABLE gemini_keys ADD COLUMN last_used_at TEXT")
        conn.commit()
    if "day_requests" not in cols:
        # Per-key daily request counter (pacing): how many requests
        # this key served on day_stamp (UTC date). Incremented by the
        # report_* paths via SQL CASE so it resets itself daily.
        conn.execute(
            "ALTER TABLE gemini_keys ADD COLUMN day_requests"
            " INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE gemini_keys ADD COLUMN day_stamp TEXT")
        conn.commit()
    ucols = [r["name"] for r in conn.execute("PRAGMA table_info(gemini_key_usage)").fetchall()]
    if ucols and "latency_ms" not in ucols:
        conn.execute("ALTER TABLE gemini_key_usage ADD COLUMN latency_ms INTEGER")
        conn.commit()
    if ucols and "error_class" not in ucols:
        # Failure classification per usage row (quota / payment /
        # transient / request / model; NULL on success and on pre-migration
        # rows) so failure rates can be reported by class.
        conn.execute("ALTER TABLE gemini_key_usage ADD COLUMN error_class TEXT")
        conn.commit()
    # Stage 1 (2026-10-18): dead-key forensics cause label + which
    # quota dimension parked the key ('rpm' / 'rpd'; NULL = classic
    # ladder or a non-quota park).
    if "attention_reason" not in cols:
        conn.execute(
            "ALTER TABLE gemini_keys ADD COLUMN attention_reason TEXT")
        conn.commit()
    if "quota_park_kind" not in cols:
        conn.execute(
            "ALTER TABLE gemini_keys ADD COLUMN quota_park_kind TEXT")
        conn.commit()
    # Key Manager v2 (2026-10-20): which Google Cloud project a key
    # belongs to. Gemini quotas are per project, so this tag drives
    # project grouping (RPD parks), the quota ledger, and usage-based
    # selection. Empty = unassigned: the key is treated as its own
    # project ("key-<id>") until an admin assigns a real project.
    if "project" not in cols:
        conn.execute(
            "ALTER TABLE gemini_keys ADD COLUMN project"
            " TEXT NOT NULL DEFAULT ''")
        conn.commit()
    _seed_projects_from_env(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_project_limits (
            project     TEXT NOT NULL,
            model       TEXT NOT NULL,
            daily_limit INTEGER NOT NULL,
            learned_from TEXT,
            updated_at  TEXT,
            PRIMARY KEY (project, model)
        )
    """)
    # Stage 1 health grid: one row per (key, model, capability) cell
    # that has ever been observed non-ok (plus ok rows written on
    # success to clear a cell). status: 'ok' / 'cooling' / 'dead';
    # until_ts is an epoch expiry for 'cooling' (NULL = no expiry).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_key_cells (
            key_id     INTEGER NOT NULL,
            model      TEXT NOT NULL,
            capability TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'ok',
            until_ts   REAL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (key_id, model, capability)
        )
    """)
    # Stage 2 leases: manager-issued bulk call budgets.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_leases (
            lease_id     TEXT PRIMARY KEY,
            source       TEXT NOT NULL,
            job_id       TEXT NOT NULL,
            budget_total INTEGER NOT NULL,
            budget_used  INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at   REAL,
            status       TEXT NOT NULL DEFAULT 'active',
            last_charge_at REAL
        )
    """)
    # Pool fix (2026-10-18): per-lease last-charge timestamp so the
    # active-lease cap can tell live concurrency from dead holders.
    lcols = [r["name"] for r in
             conn.execute("PRAGMA table_info(gemini_leases)").fetchall()]
    if lcols and "last_charge_at" not in lcols:
        conn.execute(
            "ALTER TABLE gemini_leases ADD COLUMN last_charge_at REAL")
    # Stage 3: per-key/model daily burn counters (Pacific day),
    # learned per-key/model daily limits (observed exhaustion only),
    # and the per-source daily spend ledger (calls/failures/paid
    # calls — never dollars).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_key_model_day (
            pt_day TEXT NOT NULL,
            key_id INTEGER NOT NULL,
            model  TEXT NOT NULL,
            calls  INTEGER NOT NULL DEFAULT 0,
            ok     INTEGER NOT NULL DEFAULT 0,
            fail   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (pt_day, key_id, model)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_key_limits (
            key_id      INTEGER NOT NULL,
            model       TEXT NOT NULL,
            daily_limit INTEGER NOT NULL,
            learned_from TEXT,
            updated_at  TEXT,
            PRIMARY KEY (key_id, model)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gemini_source_day (
            pt_day     TEXT NOT NULL,
            source     TEXT NOT NULL,
            calls      INTEGER NOT NULL DEFAULT 0,
            failures   INTEGER NOT NULL DEFAULT 0,
            paid_calls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (pt_day, source)
        )
    """)
    conn.commit()


# SQL fragment (used inside the report_* UPDATEs) that bumps a key's
# daily request counter, resetting it when the stored stamp is not
# today (UTC). Keeps pacing state in the same write as the outcome.
_DAY_BUMP_SQL = ("day_requests = CASE WHEN day_stamp = date('now')"
                 " THEN day_requests + 1 ELSE 1 END,"
                 " day_stamp = date('now'),")


def _log_usage(conn, key_id: int | None, source: str, success: bool,
               latency_ms: int | None = None,
               error_class: str | None = None) -> None:
    """Lightweight per-request log: which app used which key, whether
    it worked, how long the attempt took, and — on failure — which
    error class it belonged to. Powers the admin usage endpoint and
    before/after efficiency measurement."""
    try:
        conn.execute(
            "INSERT INTO gemini_key_usage"
            " (key_id, source, success, latency_ms, error_class)"
            " VALUES (?, ?, ?, ?, ?)",
            (key_id, (source or "unknown")[:64], 1 if success else 0,
             latency_ms, error_class),
        )
        # Keep the table small: retain the most recent 5000 rows.
        conn.execute(
            "DELETE FROM gemini_key_usage WHERE id NOT IN"
            " (SELECT id FROM gemini_key_usage ORDER BY id DESC LIMIT 5000)"
        )
        conn.commit()
    except Exception:
        pass  # usage logging must never break the request path


def _learn_cell(conn, key_id: int, model: str | None, capability: str,
                status: str, until_ts: float | None = None) -> None:
    """Persist one health-grid cell outcome (Stage 1). Best-effort:
    learning must never break the request path."""
    try:
        conn.execute(
            """INSERT INTO gemini_key_cells
                   (key_id, model, capability, status, until_ts,
                    updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(key_id, model, capability) DO UPDATE SET
                   status = excluded.status,
                   until_ts = excluded.until_ts,
                   updated_at = excluded.updated_at""",
            (key_id, _cell_model(model, capability), capability, status,
             until_ts),
        )
        conn.commit()
    except Exception:
        pass


def _learn_cell_conn(key_id: int, model: str | None, capability: str,
                     status: str, until_ts: float | None = None) -> None:
    """_learn_cell on a short-lived connection (attempt path)."""
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            _learn_cell(conn, key_id, model, capability, status, until_ts)
        finally:
            conn.close()
    except Exception:
        pass


def _cell_blocked_ids(model: str | None, capability: str) -> set[int]:
    """Key ids whose health-grid cell for (model, capability) is
    currently cooling (unexpired) or dead. Empty set when the grid is
    off or nothing is blocked."""
    if not _health_grid_on():
        return set()
    cmodel = _cell_model(model, capability)
    now = time.time()
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            rows = conn.execute(
                """SELECT key_id, status, until_ts FROM gemini_key_cells
                   WHERE model = ? AND capability = ? AND status != 'ok'""",
                (cmodel, capability),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return set()
    blocked = set()
    for r in rows:
        if r["status"] == "dead":
            blocked.add(r["key_id"])
        elif r["status"] == "cooling" \
                and (r["until_ts"] is None or r["until_ts"] > now):
            blocked.add(r["key_id"])
    return blocked


def _filter_cells(candidates, model: str | None, capability: str):
    """Drop candidates whose health-grid cell blocks this request."""
    blocked = _cell_blocked_ids(model, capability)
    if not blocked:
        return candidates
    return [c for c in candidates if c[0] not in blocked]


def _key_hash(key_value: str) -> str:
    return hashlib.sha256(key_value.encode("utf-8")).hexdigest()


def mask_key(key_value: str) -> str:
    v = key_value or ""
    return ("..." + v[-4:]) if len(v) >= 4 else "..."


def _seed_legacy_key(conn) -> None:
    """First-run migration: adopt the GEMINI_API_KEY env var as the initial
    paid key (label 'legacy-env') so existing deploys keep working."""
    row = conn.execute("SELECT COUNT(*) AS n FROM gemini_keys").fetchone()
    if row["n"]:
        return
    legacy = (config.GEMINI_API_KEY or "").strip()
    if not legacy:
        return
    conn.execute(
        "INSERT INTO gemini_keys (key_hash, key_value, label, is_paid)"
        " VALUES (?, ?, 'legacy-env', 1)",
        (_key_hash(legacy), legacy),
    )
    conn.commit()


def _reserve_count(total_keys: int) -> int:
    """Number of keys to hold in the reserve pool.

    Defaults to ~30% of total keys (at least 1 when there are 2+ keys).
    Override with the GEMINI_RESERVE_COUNT env var. Always keeps at
    least one key in the active pool.
    """
    if total_keys <= 1:
        return 0
    env_val = (os.environ.get("GEMINI_RESERVE_COUNT") or "").strip()
    if env_val:
        try:
            n = int(env_val)
            return max(0, min(n, total_keys - 1))
        except ValueError:
            pass
    return max(1, min(round(total_keys * 0.3), total_keys - 1))


def _pool_ids(conn) -> tuple[set[int], set[int]]:
    """Split all keys into (active_ids, reserve_ids).

    The reserve pool is the N highest key IDs (N from _reserve_count);
    everything else is active. Deterministic and stable across calls —
    no DB migration needed.
    """
    rows = conn.execute("SELECT id FROM gemini_keys ORDER BY id ASC").fetchall()
    ids = [r["id"] for r in rows]
    n_reserve = _reserve_count(len(ids))
    if n_reserve <= 0:
        return set(ids), set()
    reserve_ids = set(ids[-n_reserve:])
    active_ids = set(ids[:-n_reserve])
    return active_ids, reserve_ids


def _eligible_rows(conn, only_ids: set[int] | None = None):
    """Active keys whose backoff window has passed and which don't need
    attention, free keys first, healthiest first.

    When only_ids is given, restricts selection to those key IDs — used
    to implement the active/reserve pool split.
    """
    _ensure_table(conn)
    _seed_legacy_key(conn)
    if only_ids is not None and not only_ids:
        return []
    query = """SELECT id, key_value, is_paid, consecutive_failures, last_used_at,
                    day_requests, day_stamp, project
           FROM gemini_keys
           WHERE is_active = 1
             AND needs_attention = 0
             AND (quota_exhausted_until IS NULL
                  OR quota_exhausted_until <= datetime('now'))"""
    params: list = []
    if only_ids is not None:
        placeholders = ",".join("?" * len(only_ids))
        query += f" AND id IN ({placeholders})"
        params.extend(sorted(only_ids))
    query += """ ORDER BY is_paid ASC,
                    last_used_at ASC,
                    consecutive_failures ASC,
                    id ASC"""
    return conn.execute(query, params).fetchall()


def _order_by_health(rows) -> list[tuple[int, str]]:
    """Order eligible keys: free keys first, then healthiest first,
    then fewest requests served today (pacing: spread the day's load
    so per-project quotas drain evenly instead of in a clump), then
    least-recently-used.

    Key Manager v2: within each tier the LEADING signal is the key's
    project remaining daily-budget fraction for the chain's primary
    model (ledger-aware usage-based selection) — the project with
    the most budget left serves first, so quotas drain evenly across
    projects instead of in id order. Projects with no known limit
    count as fully available (they have never exhausted)."""
    fracs = _remaining_fractions(None) if _v2_on() else {}

    def keyfn(r):
        frac = fracs.get(_project_of(r))
        return (-(1.0 if frac is None else frac),
                _health_score(r["id"], r["consecutive_failures"]),
                _day_count(r),
                r["last_used_at"] or "")

    free = sorted((r for r in rows if not r["is_paid"]), key=keyfn)
    paid = sorted((r for r in rows if r["is_paid"]), key=keyfn)
    return [(r["id"], r["key_value"]) for r in free + paid]


def _v2_tuple_filter(cands):
    """Key Manager v2 candidate hygiene for the tuple pickers:
    circuit-breaker benches and the paid daily ceiling remove keys
    before any caller sees them."""
    if not _v2_on():
        return cands
    cands = _filter_benched(cands, _benched_ids())
    state = _paid_state()
    _note_paid_state_alerts(state)
    return _filter_paid_capped(cands, state["ceiling_hit"])


def get_candidate_keys() -> list[tuple[int, str]]:
    """Ordered (key_id, key_value) candidates. Empty when nothing usable.

    Two-pool system: the active pool rotates normally. The reserve pool
    (~30% of keys, see _reserve_count) is held back and only tapped when
    every active key is cooling down, failed, disabled, or rate-limited.
    Reserve usage is logged so exhaustion of the active pool is visible.

    Hardening: keys at/over their per-key RPM cap are skipped so rotation
    moves on before a 429; candidates are ordered healthiest-first
    (free tier first, then lowest health score, then least-recently-used).
    """
    global _last_reserve_tap
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            active_ids, reserve_ids = _pool_ids(conn)
            # Tier 1: active pool (over-limit keys filtered out)
            active_rows = _eligible_rows(conn, active_ids)
            rows = _under_limit(active_rows)
            if rows:
                return _v2_tuple_filter(_order_by_health(rows))
            # Tier 2: reserve pool — active pool exhausted or rate-limited
            if reserve_ids:
                rows = _under_limit(_eligible_rows(conn, reserve_ids))
                if rows:
                    _last_reserve_tap = time.monotonic()
                    reason = "rate-limited" if active_rows else "exhausted"
                    logger.warning(
                        "gemini_keys: active pool %s, tapping reserve "
                        "pool (%d reserve keys eligible)",
                        reason,
                        len(rows),
                    )
                    return _v2_tuple_filter(_order_by_health(rows))
            return []
        finally:
            conn.close()


def _fast_candidates() -> list[tuple[int, str]]:
    """Keys currently *believed healthy*: confirmed by a successful real
    attempt or prober check recently, with no failure since, and under
    the per-key rate cap. Active pool first; the reserve pool appears
    only when no active key is believed healthy (reserve rules, incl.
    the degraded-mode tap signal, are preserved). Ordered free-first,
    then by fewest requests served today (pacing: never immediately
    re-hit the key that just served while other healthy keys exist),
    then by recent latency, then least-recently-used. Empty when nothing
    is believed healthy — the caller then uses the classic sweep."""
    global _last_reserve_tap
    now = time.time()
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            _seed_belief_locked(conn)
            active_ids, reserve_ids = _pool_ids(conn)
            rows = _eligible_rows(conn)
        finally:
            conn.close()

        def fresh(r) -> bool:
            b = _belief.get(r["id"])
            if not b or not b.get("ok"):
                return False
            if now - b.get("at", 0.0) > _BELIEF_TTL_S:
                return False
            return not _key_over_limit(r["id"])

        fracs = _remaining_fractions(None) if _v2_on() else {}

        def rank(r):
            b = _belief.get(r["id"]) or {}
            lat = b.get("lat")
            frac = fracs.get(_project_of(r))
            return (0 if not r["is_paid"] else 1,
                    -(1.0 if frac is None else frac),
                    _day_count(r),
                    lat if lat is not None else _UNKNOWN_LATENCY_S,
                    r["last_used_at"] or "")

        pool = sorted(
            (r for r in rows if r["id"] in active_ids and fresh(r)), key=rank)
        if not pool:
            pool = sorted(
                (r for r in rows if r["id"] in reserve_ids and fresh(r)),
                key=rank)
            if pool:
                _last_reserve_tap = time.monotonic()
                logger.warning(
                    "gemini_keys: no active key believed healthy, "
                    "fast path tapping reserve pool")
        return _v2_tuple_filter([(r["id"], r["key_value"]) for r in pool])


def get_key(source: str = "unknown") -> tuple[int, str] | None:
    """Next key in round-robin rotation, or None when pool is exhausted.

    Returns the least-recently-used eligible key (free tier first) and
    marks it used so the next call rotates to a different key. This
    distributes load across all keys instead of hammering the first one.

    Two-pool behavior: draws from the active pool first; only when the
    active pool is fully exhausted does it tap the reserve pool (logged).
    A used reserve key stays in the reserve pool — its last_used_at
    update rotates it to the back of the reserve queue.

    `source` names the calling app (e.g. "whispr-web"); it is recorded
    when the key is actually used via post_json().
    """
    cands = get_candidate_keys()
    if not cands:
        return None
    key_id, key_value = cands[0]
    # Mark as used so rotation advances even if caller doesn't use post_json
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            conn.execute(
                "UPDATE gemini_keys SET last_used_at = ? WHERE id = ?",
                (_now_str(), key_id),
            )
            conn.commit()
            _record_dispatch_locked(key_id)
        finally:
            conn.close()
    return (key_id, key_value)


def report_success(key_id: int) -> None:
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            conn.execute(
                """UPDATE gemini_keys
                   SET consecutive_failures = 0,
                       last_success_at = ?,
                       last_used_at = ?,
                       total_requests = total_requests + 1,
                       """ + _DAY_BUMP_SQL + """
                       quota_exhausted_until = NULL,
                       quota_park_kind = NULL,
                       needs_attention = 0,
                       attention_reason = ''
                   WHERE id = ?""",
                (_now_str(), _now_str(), key_id),
            )
            conn.commit()
        finally:
            conn.close()
        # A success fully restores the key: any transient streak is
        # forgiven along with the failure counters.
        _transient_streaks.pop(key_id, None)
        # Key Manager v2: a success also rehabilitates the per-key
        # circuit breaker — bench and rolling outcome window cleared.
        # (The needs_attention clear in the UPDATE above is the same
        # principle for the admin park: a key that just served a real
        # request — e.g. the funded paid key via an admin test — is
        # rehabilitated, not parked forever.)
        _bench_until.pop(key_id, None)
        _outcomes.pop(key_id, None)
        entry = _belief_entry_locked(key_id)
        entry["ok"] = True
        entry["at"] = time.time()


def report_failure(key_id: int, error_type: str,
                   quota_kind: str | None = None,
                   retry_delay_s: float | None = None,
                   attention_reason: str | None = None) -> float | None:
    """Record a failure; returns the park expiry (epoch) it wrote, if
    any, so the caller can mirror it into the health grid.

    Stage 1 kwargs (all optional; today's behavior when absent):
    - quota_kind/retry_delay_s: the 429's classified dimension.
      'rpd' parks until the next Pacific midnight; 'rpm' parks for
      min(retry_delay_s, ~120 s). The dimension is stored in
      quota_park_kind so admin can tell a daily wall from a ladder.
    - attention_reason: plain cause label stored alongside
      needs_attention for permanent (payment-class) failures.

    Error classes (2026-10-15 separation):

    - 'quota' (429): exponential backoff ladder (5, 15, 60, 240 min via
      quota_exhausted_until) and +1 consecutive_failures — a real wall.
    - 'payment' (401/402/403 auth class): needs_attention = 1 and +1
      consecutive_failures — a real wall an admin must clear.
    - 'transient' (5xx, timeout, network drop; 'other' is accepted as
      a legacy alias): a brief pause only — 15/30/60 seconds by streak,
      written to quota_exhausted_until so selection skips the key —
      with NO health consequences: consecutive_failures is untouched,
      the recent-failure ranking signal is untouched, and the key can
      never degrade or need attention over transient errors. The next
      success restores it fully.
    - 'request' (other 4xx — the call itself was bad): the request is
      counted, nothing else. No failure count, no pause, no belief
      change; the key did nothing wrong.
    - 'model' (2026-10-17; the model is overloaded for every key —
      detected from the error body, see _is_model_overload): NOT the
      key's fault, so nothing is recorded against the key at all: no
      pause, no failure counters, no belief change. The request is
      counted and the usage row carries the 'model' class; failover is
      owned by the per-model tracker (fed by _attempt), which marks
      the model hot once overload evidence spans multiple keys.
    """
    if error_type == "other":
        error_type = "transient"
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT consecutive_failures, quota_exhausted_until"
                " FROM gemini_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
            if not row:
                return None
            parked_until = None
            if error_type == "request":
                conn.execute(
                    """UPDATE gemini_keys
                       SET total_requests = total_requests + 1,
                       """ + _DAY_BUMP_SQL + """
                       last_used_at = ?
                       WHERE id = ?""",
                    (_now_str(), key_id),
                )
                conn.commit()
                return
            if error_type == "model":
                # Model-level outage: not the key's fault. Count the
                # request; touch nothing else — no pause, no failure
                # counters, no belief change. Failover is owned by the
                # per-model tracker (see _note_model_overload_locked).
                conn.execute(
                    """UPDATE gemini_keys
                       SET total_requests = total_requests + 1,
                       """ + _DAY_BUMP_SQL + """
                       last_used_at = ?
                       WHERE id = ?""",
                    (_now_str(), key_id),
                )
                conn.commit()
                return
            failures = (row["consecutive_failures"] or 0) + 1
            if error_type == "quota":
                # Stage 1 two-quota parking: when the 429's dimension
                # was classified, park by dimension instead of the
                # ladder — 'rpd' until the next Pacific midnight,
                # 'rpm' for the retryDelay (capped ~120 s). The key
                # keeps its +1 consecutive failure either way (the
                # ranking signal stays honest); only the park differs.
                park_kind = None
                if _quota_classify_on() and quota_kind == "rpd":
                    until_epoch = _next_pt_midnight_epoch()
                    park_kind = "rpd"
                elif _quota_classify_on() and quota_kind == "rpm":
                    delay = retry_delay_s if retry_delay_s else 60.0
                    until_epoch = time.time() + min(
                        max(delay, 1.0), _RPM_PARK_CAP_S)
                    park_kind = "rpm"
                else:
                    wait = _BACKOFF_MINUTES[min(
                        failures - 1, len(_BACKOFF_MINUTES) - 1)]
                    until_epoch = (datetime.now(timezone.utc)
                                   + timedelta(minutes=wait)).timestamp()
                until = datetime.fromtimestamp(
                    until_epoch, tz=timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """UPDATE gemini_keys
                       SET consecutive_failures = ?,
                           last_failure_at = ?,
                           total_requests = total_requests + 1,
                           total_failures = total_failures + 1,
                           """ + _DAY_BUMP_SQL + """
                           quota_exhausted_until = ?,
                           quota_park_kind = ?
                       WHERE id = ?""",
                    (failures, _now_str(), until, park_kind, key_id),
                )
                _note_failure_locked(key_id)
                parked_until = until_epoch
                if park_kind == "rpd" and _v2_on():
                    # Key Manager v2: a daily-quota exhaustion is a
                    # PROJECT event — every key in the project draws
                    # on the same quota, so the whole group cools
                    # until the same Pacific reset. Unassigned keys
                    # are their own project (no-op until tagged).
                    rows = conn.execute(
                        "SELECT id, project FROM gemini_keys").fetchall()
                    kmap = {r["id"]: _project_of(r) for r in rows}
                    mine = kmap.get(key_id)
                    if mine:
                        for other_id, proj in kmap.items():
                            if other_id != key_id and proj == mine:
                                conn.execute(
                                    """UPDATE gemini_keys
                                       SET quota_exhausted_until = ?,
                                           quota_park_kind = 'rpd'
                                       WHERE id = ? AND
                                           (quota_exhausted_until IS NULL
                                            OR quota_exhausted_until < ?)""",
                                    (until, other_id, until))
            elif error_type == "payment":
                conn.execute(
                    """UPDATE gemini_keys
                       SET consecutive_failures = ?,
                           last_failure_at = ?,
                           total_requests = total_requests + 1,
                           total_failures = total_failures + 1,
                           """ + _DAY_BUMP_SQL + """
                           needs_attention = 1,
                           attention_reason = COALESCE(?, attention_reason)
                       WHERE id = ?""",
                    (failures, _now_str(),
                     attention_reason if _forensics_on() else None,
                     key_id),
                )
                _note_failure_locked(key_id)
            else:  # transient
                pause_s = _next_transient_pause_locked(key_id)
                until_dt = datetime.now(timezone.utc) + timedelta(
                    seconds=pause_s)
                until = until_dt.strftime("%Y-%m-%d %H:%M:%S")
                # Never shorten a longer window already in force
                # (e.g. a quota backoff the key is also serving).
                existing = _parse_ts(row["quota_exhausted_until"])
                if existing is not None and existing > until_dt.timestamp():
                    until = row["quota_exhausted_until"]
                    parked_until = existing
                else:
                    parked_until = until_dt.timestamp()
                conn.execute(
                    """UPDATE gemini_keys
                       SET last_failure_at = ?,
                           total_requests = total_requests + 1,
                           total_failures = total_failures + 1,
                           """ + _DAY_BUMP_SQL + """
                           quota_exhausted_until = ?
                       WHERE id = ?""",
                    (_now_str(), until, key_id),
                )
            conn.commit()
        finally:
            conn.close()
        entry = _belief_entry_locked(key_id)
        entry["ok"] = False
        entry["at"] = time.time()
        return parked_until


def retry_exhausted() -> int:
    """Clear backoff windows that have passed so those keys become eligible
    again. Returns the number of keys reopened. (Selection already skips
    keys still inside their window, so this is bookkeeping.)"""
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            cur = conn.execute(
                """UPDATE gemini_keys
                   SET quota_exhausted_until = NULL
                   WHERE quota_exhausted_until <= datetime('now')
                     AND quota_exhausted_until IS NOT NULL"""
            )
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()


def _classify_http_error(code: int | None) -> str:
    """Map an upstream HTTP status to an error class (separation,
    2026-10-15): 429 is a quota wall; 401/402/403 are the auth/payment
    class (real consequences); 5xx is transient (a flaky upstream
    minute, not the key's fault); any other 4xx means the request
    itself was bad ('request') and no key could have served it."""
    if code == 429:
        return "quota"
    if code in (401, 402, 403):
        return "payment"
    if code is not None and 500 <= code <= 599:
        return "transient"
    if code is not None and 400 <= code <= 499:
        return "request"
    return "transient"


def _all_active_keys() -> list[tuple[int, str]]:
    """All active keys ignoring backoff windows, free first.
    Last-resort pool when every key is cooling down."""
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            rows = conn.execute(
                """SELECT id, key_value
                   FROM gemini_keys
                   WHERE is_active = 1 AND needs_attention = 0
                   ORDER BY is_paid ASC, consecutive_failures ASC, id ASC"""
            ).fetchall()
            return [(r["id"], r["key_value"]) for r in rows]
        finally:
            conn.close()


def _http_post(url: str, payload: dict, key_value: str, timeout: float) -> dict:
    """One raw Gemini POST. Raises HTTPError on HTTP failure, and
    URLError/timeout/JSON errors as-is."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": key_value},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _attempt(key_id: int, key_value: str, url: str, payload: dict,
             timeout: float, source: str) -> tuple[bool, dict | None, float, str | None]:
    """One attempt against one key. Records the dispatch, latency, health
    outcome and usage row. Returns (ok, data, seconds_taken,
    error_class) where error_class is None on success and one of
    'quota' / 'payment' / 'transient' / 'request' / 'model' on failure.
    All failure semantics live in report_failure — an attempt never
    sleeps."""
    with _lock:
        _record_dispatch_locked(key_id)
    start = time.monotonic()
    data, error_type = None, None
    quota_kind, retry_delay_s, attention_reason = None, None, None
    err_full = ""
    try:
        data = _http_post(url, payload, key_value, timeout)
    except urllib.error.HTTPError as e:
        error_type = _classify_http_error(e.code)
        try:
            err_full = e.read().decode(
                "utf-8", errors="replace")[:_ERROR_BODY_READ]
        except Exception:
            err_full = ""
        _err_body = err_full[:300]
        model = _model_from_url(url)
        # Stage 1 forensics: a permanent-failure signature (deleted
        # service account, invalid key, API not enabled) outranks the
        # status-code class — the key is dead, not unlucky — and is
        # recorded as the payment class WITH its cause label.
        if _forensics_on():
            reason = _permanent_failure_reason(e.code, err_full)
            if reason:
                error_type = "payment"
                attention_reason = reason
        if error_type == "quota" and _quota_classify_on():
            quota_kind, retry_delay_s = _classify_quota(err_full)
        if model is not None and model in model_chain() \
                and _is_model_overload(_err_body):
            # Model-level outage, not a key problem: the 'model' class
            # carries no key-health consequences, and the failure feeds
            # the per-model tracker (which may mark the model hot).
            error_type = "model"
            with _lock:
                _note_model_overload_locked(model, key_id)
        elif e.code == 404 and model is not None \
                and model in model_chain() \
                and _is_model_not_found(_err_body, model):
            # The chain model itself is gone for these projects
            # (retired / not provisioned): same model-level handling
            # as overload. Without this, the 404 lands in the
            # 'request' class — "the call itself was bad" — which is
            # fatal to the walk, so one dead chain entry takes down
            # every caller even with a healthy pool behind it.
            # Stage 1 grid: ALSO mark the model dead globally so the
            # chain skips it for every key at once (overload tracking
            # above only heats it after multi-key evidence).
            error_type = "model"
            with _lock:
                _note_model_overload_locked(model, key_id)
                if _health_grid_on():
                    _mark_model_dead_locked(
                        model, "model-not-found 404 (retired/removed)")
        logger.warning(
            "gemini_keys attempt failed: source=%s key_id=%s http=%s "
            "class=%s %s",
            source, key_id, e.code, error_type, _err_body)
    except Exception:
        # Timeouts, connection drops, DNS, malformed responses: the
        # transient class — brief pause, no health consequences.
        error_type = "transient"
    seconds = time.monotonic() - start
    with _lock:
        _record_latency_locked(key_id, seconds)
        # Stage 2 storm breaker: real traffic outcomes feed it (the
        # prober's own checks are not caller weather). 'model' and
        # 'request' class outcomes are excluded: model outages have
        # their own tracker, and bad requests say nothing about the
        # pool's weather.
        if source != "prober" and error_type not in ("model", "request"):
            _note_storm_event_locked(error_type is None)
            # Key Manager v2 per-key circuit breaker: same population.
            _note_outcome_locked(key_id, error_type is None)
    model = _model_from_url(url)
    capability = _request_capability(url, payload)
    if error_type is None:
        report_success(key_id)
        _log_usage_conn(key_id, source, True, seconds)
        _record_attempt_stats(key_id, model, source, True)
        if _sticky_on():
            _note_cached_tokens(data)
        if _health_grid_on():
            _learn_cell_conn(key_id, model, capability, "ok")
            if model and _model_is_dead(model):
                with _lock:
                    _model_dead_until.pop(model, None)
        return True, data, seconds, None
    parked_until = report_failure(
        key_id, error_type, quota_kind=quota_kind,
        retry_delay_s=retry_delay_s, attention_reason=attention_reason)
    _log_usage_conn(key_id, source, False, seconds, error_type)
    _record_attempt_stats(key_id, model, source, False)
    if quota_kind == "rpd":
        # The day's burn for this (key, model) at the moment of an
        # RPD exhaustion is an observed daily limit (Stage 3a).
        _learn_limit_from_rpd(key_id, model)
        # Key Manager v2: the PROJECT's summed burn is its observed
        # daily limit (the quota the keys were all drawing on).
        _learn_project_limit_from_rpd(key_id, model)
    if _health_grid_on():
        # Mirror the outcome into this request's health-grid cell.
        if error_type == "quota":
            _learn_cell_conn(key_id, model, capability, "cooling",
                             parked_until)
        elif capability == "grounding" and error_type == "payment":
            # Grounding failed auth-class on this project: the
            # capability is dead for the key even though the key
            # itself may serve other capabilities fine.
            _learn_cell_conn(key_id, model, capability, "dead")
        elif capability == "grounding" and err_full and (
                "grounding" in err_full.lower()
                and ("not enabled" in err_full.lower()
                     or "not available" in err_full.lower()
                     or "permission" in err_full.lower())):
            _learn_cell_conn(key_id, model, capability, "dead")
    return False, None, seconds, error_type


def _charge_lease_or_stop(state) -> bool:
    """Charge the call's lease for one more upstream attempt (Stage
    2d). True when the attempt may proceed. On exhaustion of an
    explicit lease the walk stops fatally with a typed
    lease_exhausted in status_out; a default source lease that ran
    dry simply detaches (day-one safety — the exhaustion stays
    visible on the lease row) and the call continues unleased."""
    lease_id = state.get("lease_id")
    if not lease_id:
        return True
    if _lease_charge(lease_id):
        return True
    if not state.get("lease_strict", True):
        state["lease_id"] = None
        logger.warning(
            "gemini_keys: default source lease %s exhausted; "
            "continuing unleased", lease_id)
        return True
    state["fatal"] = "lease_exhausted"
    out = state.get("status_out")
    if out is not None:
        out.update({"error": "lease_exhausted", "lease_id": lease_id})
    logger.warning("gemini_keys: lease %s exhausted mid-call", lease_id)
    return False


def _try_candidates(candidates, url, payload, timeout, source, tried,
                    state) -> dict | None:
    """Try candidates in order until one succeeds. `tried` (set of key
    ids) and `state` ({"n": attempts made, "deadline": monotonic}) are
    shared across the phases of one post_json call so the attempt cap
    and time budget hold globally. A 'request'-class failure (the call
    itself was bad) sets state["fatal"] and stops the walk: no other
    key can fix a bad request."""
    while True:
        rerouted = None
        for key_id, key_value in candidates:
            if key_id in tried:
                continue
            if state.get("v2_retry_cap") is not None:
                # Key Manager v2 retry discipline: the walk stops
                # once KEY-FAULT attempts (transient/payment — the
                # classes that indict a key) exceed the budget of
                # 1 + GEMINI_CROSS_KEY_RETRIES. Quota and model
                # outcomes are the pool's designed failover signals
                # (per-project quotas; the model chain) — they never
                # consume the retry budget and stay bounded by the
                # overall attempt cap + time budget instead.
                key_faults = (state["n"]
                              - state.get("quota_failures", 0)
                              - state.get("model_failures", 0))
                if key_faults >= state["v2_retry_cap"]:
                    break
            if state["n"] >= state.get("max_n", _max_attempts()) or time.monotonic() >= state["deadline"]:
                break
            if not _charge_lease_or_stop(state):
                break
            tried.add(key_id)
            state["n"] += 1
            ok, data, _seconds, error_type = _attempt(
                key_id, key_value, url, payload, timeout, source)
            if ok:
                state["served_key_id"] = key_id
                _so = state.get("status_out")
                if _so is not None:
                    _so["served_key_id"] = key_id
                return data
            if error_type == "quota":
                state["quota_failures"] = state.get("quota_failures", 0) + 1
            if error_type == "model":
                state["model_failures"] = state.get("model_failures", 0) + 1
                # The model went hot mid-walk: reroute the rest of this
                # walk to the model now serving traffic. The failures so
                # far blamed the model, not the keys, so the tried set is
                # cleared — the same keys may serve fine on the next model.
                candidate_url = _route_url(url)
                if candidate_url != url:
                    rerouted = candidate_url
                    break
            if error_type == "request":
                state["fatal"] = "request"
                break
        if rerouted is None:
            return None
        # Restart the pass over the full candidate list on the new
        # model. (The pre-2026-10-17 code only swapped the URL and
        # cleared `tried`, but the for-loop had already consumed the
        # candidates, so when the hot-marking failure was the last
        # candidate the fallback model was never attempted in this
        # phase.) The shared attempt cap and deadline still bound the
        # total work, and _route_url only rewrites the chain primary,
        # so a second reroute cannot ping-pong between two models.
        logger.info(
            "gemini_keys: model overloaded mid-call, failing "
            "over to %s", _model_from_url(rerouted))
        url = rerouted
        tried.clear()


def _try_hedged(candidates, url, payload, timeout, source, tried,
                state) -> dict | None:
    """Hedged variant of _try_candidates: race a second candidate when
    the first runs past GEMINI_HEDGE_AFTER_S. At most 2 requests are in
    flight at once; the first success wins. Attempt bookkeeping is
    identical to the sequential path."""
    delay = _hedge_after_s()
    executor = ThreadPoolExecutor(max_workers=2,
                                  thread_name_prefix="gemini-hedge")
    try:
        queue = list(candidates)
        pending: dict = {}

        def launch() -> bool:
            while queue:
                key_id, key_value = queue.pop(0)
                if key_id in tried:
                    continue
                if state["n"] >= state.get("max_n", _max_attempts()) \
                        or time.monotonic() >= state["deadline"]:
                    return False
                if not _charge_lease_or_stop(state):
                    return False
                tried.add(key_id)
                state["n"] += 1
                fut = executor.submit(_attempt, key_id, key_value, url,
                                      payload, timeout, source)
                pending[fut] = key_id
                return True
            return False

        launch()
        hedge_armed = True
        while pending:
            wait_s = delay if (hedge_armed and len(pending) < 2) else None
            done, _ = wait(list(pending), timeout=wait_s,
                           return_when=FIRST_COMPLETED)
            if not done:
                # First attempt still running past the hedge delay: race
                # the next believed-healthy key.
                hedge_armed = False
                launch()
                continue
            for fut in done:
                winner = pending.pop(fut, None)
                ok, data, _seconds, error_type = fut.result()
                if ok:
                    if winner is not None:
                        state["served_key_id"] = winner
                        _so = state.get("status_out")
                        if _so is not None:
                            _so["served_key_id"] = winner
                    return data
                if error_type == "quota":
                    state["quota_failures"] = \
                        state.get("quota_failures", 0) + 1
                if error_type == "model":
                    state["model_failures"] = \
                        state.get("model_failures", 0) + 1
                    # The model went hot mid-race: subsequently launched
                    # attempts target the model now serving traffic.
                    # (Hedged mode does not re-try already-tried keys on
                    # the new model within this call; the next post_json
                    # call routes fresh.)
                    url = _route_url(url)
                if error_type == "request":
                    # The call itself was bad; racing more keys at it
                    # cannot help.
                    state["fatal"] = "request"
                    return None
            # A failure drained: top up to one running attempt.
            while len(pending) < 1:
                if not launch():
                    break
        return None
    finally:
        executor.shutdown(wait=False)


def _general_available(lane_ids: set[int]) -> bool:
    """True when at least one eligible key sits OUTSIDE the
    interactive lane reserve (a general key could serve bulk)."""
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            rows = _eligible_rows(conn)
            return any(r["id"] not in lane_ids for r in rows)
        finally:
            conn.close()
    except Exception:
        return True


def _set_lease_exhausted(status_out, state) -> None:
    """Type a failed call as lease_exhausted when its explicit lease
    ended the call fully spent (Stage 2d): the last unit may have
    been consumed by the final attempt itself, in which case the
    walk simply runs out of candidates without ever charging again
    — the exhaustion is still the truthful terminal signal."""
    if status_out is None or "error" in status_out:
        return
    lease_id = state.get("lease_id")
    if not lease_id or not state.get("lease_strict"):
        return
    st = lease_status(lease_id)
    if st and st["budget_left"] <= 0:
        status_out.update({"error": "lease_exhausted",
                           "lease_id": lease_id})


def _set_pool_busy(status_out, sclass: str, lane_ids: set[int]) -> None:
    """Type a bulk failure as pool_busy when the general pool is
    empty and only lane-reserved keys remain (Stage 2 lanes)."""
    if status_out is None or sclass != "bulk" \
            or "error" in status_out:
        return
    if lane_ids and not _general_available(lane_ids):
        status_out.update({
            "error": "pool_busy",
            "retry_after_s": _pool_busy_retry_after(lane_ids)})


def _post_json_core(url: str, payload: dict, timeout: int = 30,
                    source: str = "unknown", free_only: bool = False,
                    max_attempts: int | None = None,
                    fail_fast: bool = False,
                    lease_id: str | None = None,
                    status_out: dict | None = None,
                    session_id: str | None = None,
                    lane: str | None = None) -> dict | None:
    """POST JSON to a Gemini endpoint with transparent key rotation.

    Fail-fast phases (no sleeping anywhere on the request path):
    1. Believed-healthy keys (kept fresh by the background prober and by
       real attempt outcomes): tried with a short per-attempt timeout,
       active pool first, free first, ranked by today's usage then
       latency. Optionally hedged (GEMINI_HEDGE_AFTER_S) with at most
       2 requests in flight.
    2. The classic eligible-key sweep (free first, healthiest first) at
       the caller's timeout — covers cold starts and stale belief.
    3. Only when every active key has just failed into a pause/backoff:
       one bounded pass over eligible RESERVE keys (their backoff
       windows still respected), per the reserve pool's own rule.
    There is no ignore-backoff all-keys sweep.

    Model fallback (2026-10-17): when the URL names the chain's primary
    model, its model segment is rewritten to current_model() before
    each attempt phase — the primary itself while healthy (a no-op),
    the next GEMINI_MODEL_CHAIN model while the primary is hot
    (model-level overload cooldown, GEMINI_MODEL_COOLDOWN_S, default
    120 s). A failure whose error body names model overload
    ("overloaded"/"UNAVAILABLE") is the 'model' error class: it never
    touches key health, and once such failures span multiple keys the
    model goes hot. When the primary is healthy the rewrite is a no-op
    and behavior is unchanged.

    Every failure records health (by error class — see report_failure)
    and the next key is tried immediately. Attempts per call are
    capped (GEMINI_MAX_ATTEMPTS) inside a total time budget
    (GEMINI_ATTEMPT_BUDGET_S). Returns the parsed JSON response, or
    None when every attempt failed / no keys exist.

    `source` names the calling app (e.g. "whispr-web", "creator-bot");
    each attempt is logged to gemini_key_usage for the admin endpoint.

    Fail-fast is unconditional since the 2026-10-15 efficiency pass
    (the `fail_fast` parameter and the live-voice source prefix are
    kept for compatibility but no longer change anything): when the
    manager knows nothing can serve — no key believed healthy AND no
    key even eligible (every key is inside a backoff window or
    flagged) — post_json returns None with ZERO attempts instead of
    sweeping every key. That sweep fired up to the attempt cap of
    guaranteed-failure requests per call, and every failed attempt
    re-stamped the key's backoff, pushing pool recovery further out —
    one Rover utterance used to burn up to 16 doomed attempts
    (2026-10-14). The background prober still finds recovery; the
    next call succeeds normally.
    """
    if not fail_fast and (source or "").startswith(
            _FAIL_FAST_SOURCE_PREFIXES):
        fail_fast = True
    caller_timeout = int(timeout or 30)
    attempt_timeout = max(1, min(caller_timeout, int(_fast_timeout_s())))
    attempt_cap = _max_attempts() if max_attempts is None else max(1, int(max_attempts))
    state = {"n": 0, "deadline": time.monotonic() + _attempt_budget_s(),
             "max_n": attempt_cap, "quota_failures": 0}
    tried: set[int] = set()

    # --- Stage 2: storm breaker, lanes, leases -------------------------
    # Key Manager v2: the request's priority lane (explicit lane, else
    # derived from the source) decides the Stage-2 class — morning
    # proactive rides interactive, ambient/batch ride bulk.
    v2lane = _resolve_lane(source, lane)
    sclass = _lane_sclass(v2lane) if _v2_on() else _source_class(source)
    storm = _storm_stats()
    if _storm_breaker_on() and storm["open"]:
        if sclass == "bulk":
            # Fail fast with the typed signal instead of walking a
            # pool that is actively failing.
            if status_out is not None:
                status_out.update({
                    "error": "pool_busy", "reason": "storm",
                    "retry_after_s": int(storm["open_s_left"]) + 1})
            return None
        # Interactive rides out a storm on a shortened walk.
        state["max_n"] = min(state["max_n"], 2)
    elif _storm_breaker_on() and storm["events_60s"] >= 10:
        # Attempts ceiling proportional to recent successes: while
        # the window is mostly failures, a call may not march the
        # full key list and amplify the storm.
        state["max_n"] = max(2, min(state["max_n"],
                                    storm["successes_60s"]))
    if _v2_on() and max_attempts is None \
            and "GEMINI_MAX_ATTEMPTS" not in os.environ:
        # Key Manager v2 retry discipline: at most ONE cross-key
        # retry per request on key-fault failures (see
        # _try_candidates). An explicit caller max_attempts — or an
        # explicitly configured GEMINI_MAX_ATTEMPTS — still wins:
        # the v2 budget is the default posture, not an override of
        # a deliberate configuration.
        state["v2_retry_cap"] = 1 + _cross_key_retries()
    else:
        state["v2_retry_cap"] = None
    if _v2_on():
        # Priority-lane budget floor: when the pool's remaining
        # daily budget (projects with known limits) runs low, the
        # cheap-to-defer classes are refused up front — batch first,
        # chat never (interactive has no floor).
        floor = _lane_floor(v2lane)
        if floor is not None:
            frac = pool_remaining_fraction()
            if frac is not None and frac < floor:
                if status_out is not None:
                    status_out.update({
                        "error": "pool_busy", "reason": "lane_budget",
                        "lane": v2lane,
                        "retry_after_s": int(min(
                            seconds_until_reset(), 900.0)) + 1})
                logger.warning(
                    "gemini_keys: lane %s refused for source=%s — "
                    "pool budget %.3f below floor %.2f",
                    v2lane, source, frac, floor)
                return None
    lane_ids: set[int] = set()
    if sclass == "bulk" and _lanes_on():
        with _lock:
            conn = db.connect()
            try:
                _ensure_table(conn)
                lane_ids = _lane_reserve_ids(conn)
            finally:
                conn.close()
    if lease_id is not None and not _leases_on():
        lease_id = None  # leases disabled: ignore any supplied id
    strict_lease = lease_id is not None
    if lease_id is None and sclass == "bulk":
        lease_id = _default_source_lease(source)
    state["lease_id"] = lease_id
    state["lease_strict"] = strict_lease
    state["status_out"] = status_out
    # Stage 3 sticky sessions: the session's pinned key (if any and
    # still live) is tried first in every phase; the pin re-forms on
    # whichever key serves (handled by the post_json wrapper).
    pin_id = _session_key_id(session_id) \
        if (session_id and _sticky_on()) else None
    state["pin_id"] = pin_id

    if free_only:
        # Free-only mode (partner traffic, e.g. Roof Rover's Winston API):
        # restrict every selection phase to keys that are NOT paid. When
        # no free key can serve, return None so the caller can return a
        # retryable error - a paid key is never used for this traffic.
        with _lock:
            conn = db.connect()
            try:
                _ensure_table(conn)
                _free_ids = {r["id"] for r in conn.execute(
                    "SELECT id FROM gemini_keys WHERE is_paid = 0"
                    " AND is_active = 1").fetchall()}
            finally:
                conn.close()
        if not _free_ids:
            return None

        def _only_free(cands):
            return [c for c in cands if c[0] in _free_ids]
    else:
        def _only_free(cands):
            return cands

    def _only_general(cands):
        # Stage 2 lanes: bulk never draws the interactive reserve.
        if not lane_ids:
            return cands
        return [c for c in cands if c[0] not in lane_ids]

    # --- Key Manager v2: pre-call ledger, breakers, paid ceiling ---
    _v2_blocked: set[str] = set()
    _v2_kmap: dict[int, str] = {}
    if _v2_on():
        _v2_blocked = _ledger_blocked_projects(_model_from_url(url))
        if _v2_blocked:
            with _lock:
                conn = db.connect()
                try:
                    _ensure_table(conn)
                    _v2_kmap = _key_project_map(conn)
                finally:
                    conn.close()
    _v2_benched = _benched_ids()
    _v2_paid = _paid_state() if _v2_on() else {"ceiling_hit": False}
    if _v2_on():
        _note_paid_state_alerts(_v2_paid)

    def _v2_filter(cands):
        cands = _filter_projects(cands, _v2_blocked, _v2_kmap)
        cands = _filter_benched(cands, _v2_benched)
        return _filter_paid_capped(cands, bool(_v2_paid["ceiling_hit"]))

    def _set_ledger_verdict():
        # Nothing was even attempted and the ledger had every project
        # at a limit: the caller gets the local verdict (typed
        # pool_busy) instead of a provider 429 walk.
        if (status_out is not None and state["n"] == 0 and _v2_blocked
                and status_out.get("error") != "lease_exhausted"):
            status_out.update({
                "error": "pool_busy", "reason": "ledger",
                "retry_after_s": int(min(seconds_until_reset(),
                                         3600.0)) + 1})

    fast = _pin_first(
        _v2_filter(_only_general(_only_free(_fast_candidates()))),
        pin_id)
    if fast:
        url = _route_url(url)
        fast = _filter_cells(fast, _model_from_url(url),
                             _request_capability(url, payload))
    if fast:
        if _hedge_after_s() > 0 and len(fast) >= 2:
            data = _try_hedged(fast, url, payload, attempt_timeout, source,
                               tried, state)
        else:
            data = _try_candidates(fast, url, payload, attempt_timeout,
                                   source, tried, state)
        if data is not None:
            return data
        if state.get("fatal"):
            return None
    sweep = _pin_first(
        _v2_filter(_only_general(_only_free(get_candidate_keys()))),
        pin_id)
    if not sweep:
        # Nothing believed healthy (phase 1 empty or all its attempts
        # just failed into a pause/backoff) and nothing eligible for
        # phase 2: the pool is known down. Make the one no-healthy-key
        # decision and return — never sweep keys whose backoff says
        # they are out; those attempts almost always fail and every
        # failure re-arms a backoff, pushing recovery out.
        logger.warning(
            "gemini_keys: pool down (no believed-healthy or eligible "
            "keys), failing fast for source=%s", source)
        _set_lease_exhausted(status_out, state)
        _set_pool_busy(status_out, sclass, lane_ids)
        _set_ledger_verdict()
        return None
    url = _route_url(url)
    sweep = _filter_cells(sweep, _model_from_url(url),
                          _request_capability(url, payload))
    data = _try_candidates(sweep, url, payload,
                           caller_timeout, source, tried, state)
    if data is not None:
        return data
    if state.get("fatal"):
        return None
    # Every active key just failed into a pause/backoff, so the active
    # pool is now exhausted by the reserve pool's own definition: one
    # bounded pass over ELIGIBLE reserve keys, with any untried paid
    # key last (backoff windows still respected — this is not the old
    # ignore-everything sweep), under the same time budget.
    #
    # Attempt budget (2026-10-16 fix): the pass used to share the
    # global attempt cap, so whenever the active phases had eligible
    # keys numbering >= the cap — the normal case — the cap was spent
    # before this pass ran and the fallback could never fire in
    # exactly the failure it exists for (a request class quota-walled
    # on the whole active pool: grounded search and image generation
    # on 2026-10-15, where the held-back keys were never attempted).
    # The pass now earns its own bounded allowance, but ONLY when
    # every attempt so far failed with the quota class: quota is
    # per-project, so a wall on the actives says nothing about the
    # held-back keys. A transient storm earns no extension.
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            _active_ids, reserve_ids = _pool_ids(conn)
            eligible = _eligible_rows(conn)
            reserve_rows = [r for r in eligible
                            if r["id"] in reserve_ids]
            paid_rows = [r for r in eligible
                         if r["is_paid"] and r["id"] not in tried]
        finally:
            conn.close()
    fallback = _v2_filter(_only_general(_only_free(
        _order_by_health(reserve_rows + paid_rows))))
    if fallback:
        if state["n"] > 0 \
                and state.get("quota_failures", 0) >= state["n"]:
            grant = True
            if _v2_on():
                # v2 retry discipline: this extension is the ONE
                # sanctioned cross-key retry — it must reach a
                # project whose quota has not already answered.
                fb_map = {r["id"]: _project_of(r) for r in eligible}
                tried_projects = {fb_map[t] for t in tried
                                  if t in fb_map}
                grant = any(fb_map.get(c[0]) not in tried_projects
                            for c in fallback)
            if grant:
                state["max_n"] = max(state["max_n"], state["n"] + min(
                    len(fallback), _RESERVE_FALLBACK_MAX_EXTRA))
        url = _route_url(url)
        fallback = _pin_first(
            _filter_cells(fallback, _model_from_url(url),
                          _request_capability(url, payload)), pin_id)
        data = _try_candidates(fallback, url, payload,
                               caller_timeout, source, tried, state)
        if data is not None:
            return data
    _set_lease_exhausted(status_out, state)
    _set_pool_busy(status_out, sclass, lane_ids)
    _set_ledger_verdict()
    return None


def post_json(url: str, payload: dict, timeout: int = 30,
              source: str = "unknown", free_only: bool = False,
              max_attempts: int | None = None,
              fail_fast: bool = False,
              lease_id: str | None = None,
              status_out: dict | None = None,
              session_id: str | None = None,
              lane: str | None = None) -> dict | None:
    """POST JSON through the shared key pool (see _post_json_core for
    the rotation/failover semantics).

    Stage 2 additions:
    - lease_id: charge every upstream attempt against a manager
      lease (request_lease); exhaustion surfaces as a typed
      lease_exhausted in status_out and a None return.
    - status_out: when a dict is passed, typed failure signals are
      written into it ({"error": "pool_busy", "retry_after_s": N} or
      {"error": "lease_exhausted", ...}) so HTTP callers can answer
      429 instead of a bare 502.
    - Single-flight (GEMINI_SINGLEFLIGHT): concurrent identical
      unleased calls coalesce onto one execution; waiters share a
      deep copy of the leader's success, or run the call themselves
      when the leader failed or overran.

    Stage 3 addition:
    - session_id (GEMINI_STICKY_SESSIONS): the session's pinned key
      is tried first; after the call the pin re-forms on the key
      that actually served, and a fully failed call drops the pin
      (clean fallback next call).

    Key Manager v2:
    - lane: the request's priority class ("interactive",
      "proactive_morning", "proactive_ambient", "batch"); defaults
      from the source when omitted. Low-budget refusal hits batch
      first and interactive last (see _post_json_core)."""
    global _flight_hits, _flight_leader_runs
    sticky = bool(session_id) and _sticky_on()
    # The wrapper always passes a dict down so the serving key can
    # ride back for session pinning even when the caller passed none.
    so = status_out if status_out is not None else {}

    def _bookkeep(result):
        if sticky:
            served = so.get("served_key_id")
            if result is not None and served:
                _pin_session(session_id, served)
            elif result is None:
                _unpin_session(session_id)
        return result

    if not _singleflight_on() or lease_id is not None:
        return _bookkeep(_post_json_core(
            url, payload, timeout=timeout, source=source,
            free_only=free_only, max_attempts=max_attempts,
            fail_fast=fail_fast, lease_id=lease_id,
            status_out=so, session_id=session_id if sticky else None,
            lane=lane))
    fkey = _flight_key(source + "\n" + (session_id or ""), url, payload)
    with _flight_lock:
        entry = _flights.get(fkey)
        if entry is None:
            entry = {"event": threading.Event(), "result": None,
                     "done": False}
            _flights[fkey] = entry
            leader = True
        else:
            leader = False
    if leader:
        with _flight_lock:
            _flight_leader_runs += 1
        try:
            result = _post_json_core(
                url, payload, timeout=timeout, source=source,
                free_only=free_only, max_attempts=max_attempts,
                fail_fast=fail_fast, lease_id=None,
                status_out=so,
                session_id=session_id if sticky else None,
                lane=lane)
        except Exception:
            result = None
            entry["result"] = None
            entry["done"] = True
            with _flight_lock:
                _flights.pop(fkey, None)
            entry["event"].set()
            raise
        entry["result"] = result
        entry["done"] = True
        with _flight_lock:
            _flights.pop(fkey, None)
        entry["event"].set()
        return _bookkeep(result)
    # Waiter: share the leader's success; never share its failure —
    # a failed leader just means this caller runs the call itself.
    wait_s = _attempt_budget_s() + float(timeout or 30) + 5.0
    if entry["event"].wait(timeout=wait_s) and entry.get("done") \
            and entry.get("result") is not None:
        with _flight_lock:
            _flight_hits += 1
        # The shared result carries no served-key of our own; the
        # leader's bookkeeping already maintained the (shared) pin.
        return json.loads(json.dumps(entry["result"]))
    return _bookkeep(_post_json_core(
        url, payload, timeout=timeout, source=source,
        free_only=free_only, max_attempts=max_attempts,
        fail_fast=fail_fast, lease_id=None, status_out=so,
        session_id=session_id if sticky else None, lane=lane))


# ---------------------------------------------------------------------------
# Background prober — keeps the believed-healthy view fresh off the
# request path so recovery is discovered within ~a probe cycle, and
# death is known before a live request has to discover it.
# ---------------------------------------------------------------------------

def _probe_payload() -> dict:
    """Cheapest truthful generation: one token of output."""
    return {
        "contents": [{"role": "user", "parts": [{"text": "ok"}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 1},
    }


def _probe_cycle() -> int:
    """One prober pass (lean version, 2026-10-15; model-chain aware,
    2026-10-17). Sends 1-token probes for at most _PROBE_MAX_PER_CYCLE
    keys per cycle against the primary chain model, most valuable
    first:

    - priority 0: backoff expired or expiring within one cycle
      (recovery check — prompt, not next-request),
    - priority 1: unknown state (never succeeded, or marked bad
      outside backoff),
    - priority 2: believed healthy but idle past the confirm window
      (re-confirm before belief lapses).

    Key selection keeps the existing rules: never needs_attention or
    disabled keys, never deep inside a backoff window (only at/near
    expiry), never the same key more often than _PROBE_MIN_INTERVAL_S,
    and never when real traffic touched the key within _PROBE_QUIET_S
    (real evidence beats a probe, and probes must never starve real
    traffic — each probe counts toward the per-key dispatch window like
    any other dispatch).

    Fallback readiness: on a cycle with no due primary-model probes,
    each non-primary chain model that is itself due (at most every
    _PROBE_MODEL_INTERVAL_S) gets one probe with an eligible key, so a
    fallback model is known-good before live traffic needs it. A
    successful probe of a hot model lifts its overload cooldown early.
    Model probes share the per-cycle cap, so a long chain cannot turn
    one cycle into a quota burn. Returns probes sent."""
    global _prober_last_cycle_at, _probes_total, _probes_ok
    now_wall = time.time()
    now_mono = time.monotonic()
    interval = _probe_interval_s()
    chain = model_chain()
    primary = chain[0]
    slim = _prober_slim_on()
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            _seed_belief_locked(conn)
            rows = conn.execute(
                """SELECT id, key_value, quota_exhausted_until
                   FROM gemini_keys
                   WHERE is_active = 1 AND needs_attention = 0"""
            ).fetchall()
        finally:
            conn.close()
        due = []
        for r in rows:
            key_id = r["id"]
            if now_mono - _last_probe_mono.get(key_id, 0.0) \
                    < _PROBE_MIN_INTERVAL_S:
                continue
            dq = _dispatch_times.get(key_id)
            if dq and (now_mono - dq[-1]) < _PROBE_QUIET_S:
                continue
            until = _parse_ts(r["quota_exhausted_until"])
            if until and until > now_wall + interval:
                continue  # deep in backoff; check again near expiry
            belief = _belief.get(key_id)
            if until:
                if slim and _probe_validated_until.get(key_id) == until:
                    # Stage 3b: this exact cooldown exit already
                    # earned its ONE validation probe.
                    continue
                due.append((0, r))  # backoff expired / expiring: confirm
            elif belief and belief.get("ok"):
                if slim:
                    # Stage 3b: real traffic is the health evidence
                    # now; idle healthy keys are not re-confirmed.
                    continue
                if now_wall - belief.get("at", 0.0) >= _PROBE_CONFIRM_S:
                    due.append((2, r))  # idle healthy key: re-confirm
            else:
                if slim and key_id in _last_probe_mono:
                    # Stage 3b: an unknown-state key earns one probe,
                    # not an endless cycle.
                    continue
                due.append((1, r))  # unknown state: find out off-path
        due.sort(key=lambda d: d[0])
        due = [r for _prio, r in due[:_PROBE_MAX_PER_CYCLE]]
        if slim:
            for r in due:
                until = _parse_ts(r["quota_exhausted_until"])
                if until:
                    _probe_validated_until[r["id"]] = until
        # One probe task per due key against the primary model, plus —
        # only on cycles with nothing due — one readiness probe per
        # due non-primary chain model.
        tasks: list = [(r, primary) for r in due]
        if not due and rows:
            room = _PROBE_MAX_PER_CYCLE - len(tasks)
            for model in chain[1:]:
                if room <= 0:
                    break
                if now_mono - _model_probe_at.get(model, 0.0) \
                        < _PROBE_MODEL_INTERVAL_S:
                    continue
                if slim and not (
                        _model_dead_until.get(model, 0.0) > now_wall
                        or _model_hot_until.get(model, 0.0) > now_wall):
                    # Stage 3b: fallback models are probed only while
                    # they are actually in trouble (hot or dead).
                    continue
                probe_row = _pick_model_probe_row_locked(rows)
                if probe_row is None:
                    continue
                _model_probe_at[model] = now_mono
                tasks.append((probe_row, model))
                room -= 1
    sent = 0
    for r, model in tasks:
        _last_probe_mono[r["id"]] = time.monotonic()
        ok, _data, _seconds, _err = _attempt(
            r["id"], r["key_value"], model_url(model), _probe_payload(),
            _PROBE_TIMEOUT_S, "prober")
        sent += 1
        with _lock:
            _probes_total += 1
            if ok:
                _probes_ok += 1
            _model_belief[model] = {"ok": ok, "at": time.time(),
                                    "key_id": r["id"]}
            if ok and _model_hot_until.pop(model, None) is not None:
                # Recovery discovered off the request path: the model is
                # serving again, so lift its overload cooldown early
                # instead of waiting it out.
                logger.info("gemini_keys: model %s recovered (prober) - "
                            "cooldown lifted", model)
    _prober_last_cycle_at = time.time()
    return sent


def _prober_loop() -> None:
    interval = _probe_interval_s()
    while True:
        try:
            _probe_cycle()
        except Exception:
            logger.exception("gemini_keys: prober cycle failed")
        time.sleep(interval)


def start_prober() -> bool:
    """Start the background key prober thread (idempotent). Enabled by
    default; set GEMINI_PROBER=0 to disable. Returns True when this call
    started the thread."""
    global _prober_thread
    if (os.environ.get("GEMINI_PROBER", "1").strip() == "0"):
        return False
    with _lock:
        if _prober_thread is not None and _prober_thread.is_alive():
            return False
        _prober_thread = threading.Thread(
            target=_prober_loop, name="gemini-key-prober", daemon=True)
        thread = _prober_thread
    thread.start()
    logger.info("gemini_keys: background prober started (interval %.0fs)",
                _probe_interval_s())
    return True


def _log_usage_conn(key_id: int | None, source: str, success: bool,
                    seconds: float | None = None,
                    error_class: str | None = None) -> None:
    """Open a short-lived connection just for usage logging."""
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            _log_usage(conn, key_id, source, success,
                       int(seconds * 1000) if seconds is not None else None,
                       error_class)
        finally:
            conn.close()
    except Exception:
        pass



# --- Key Manager v2: projects, ledger, breakers, lanes, paid (2026-10-20) ---

def _v2_on() -> bool:
    return _flag("GEMINI_KEY_V2", "1")


def _project_of(row) -> str:
    """Effective project identity for a key row: the admin-set
    `project` tag when present, else "key-<id>" (an unassigned key is
    its own project — grouping is a no-op until tags are assigned)."""
    try:
        tag = (row["project"] or "").strip()
    except (KeyError, IndexError, TypeError):
        tag = ""
    if tag:
        return tag
    try:
        return "key-%s" % row["id"]
    except (KeyError, IndexError, TypeError):
        return "key-?"


def _key_project_map(conn) -> dict[int, str]:
    """key_id -> effective project for every key row."""
    _ensure_table(conn)
    out: dict[int, str] = {}
    for r in conn.execute("SELECT id, project FROM gemini_keys").fetchall():
        out[r["id"]] = _project_of(r)
    return out


def _seed_projects_from_env(conn) -> None:
    """GEMINI_KEY_PROJECTS="label=project,label2=project2": assign a
    project tag to keys whose label matches and whose tag is still
    empty. Idempotent and conservative: never overwrites a tag an
    admin already set."""
    raw = (os.environ.get("GEMINI_KEY_PROJECTS") or "").strip()
    if not raw:
        return
    pairs = {}
    for part in raw.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                pairs[k] = v
    if not pairs:
        return
    try:
        cols = [r["name"] for r in
                conn.execute("PRAGMA table_info(gemini_keys)").fetchall()]
        if "project" not in cols:
            return
        for label, project in pairs.items():
            conn.execute(
                """UPDATE gemini_keys SET project = ?
                   WHERE label = ? AND (project IS NULL OR project = '')""",
                (project, label))
        conn.commit()
    except Exception:
        pass


def set_project(key_id: int, project: str) -> dict | None:
    """Admin: set (or clear, with "") a key's project tag."""
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            conn.execute(
                "UPDATE gemini_keys SET project = ? WHERE id = ?",
                ((project or "").strip(), key_id))
            conn.commit()
            row = conn.execute(
                "SELECT * FROM gemini_keys WHERE id = ?", (key_id,)
            ).fetchone()
            return _row_to_public(row) if row else None
        finally:
            conn.close()


# Model-class free-tier defaults for the ledger (per project+model).
# Only the well-known free-tier shape is defaulted; anything else has
# no pre-call cap until a limit is configured or learned from a real
# RPD exhaustion. GEMINI_DAILY_LIMITS / GEMINI_RPM_LIMITS JSON maps
# override per "project:model", "model", or "*".
_MODEL_DAILY_DEFAULTS = (("flash-lite", 500),)
_MODEL_RPM_DEFAULTS = (("flash-lite", 10),)


def _limits_env(name: str) -> dict:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _configured_limit(name: str, project: str, model: str | None) -> int | None:
    table = _limits_env(name)
    if not table:
        return None
    model = model or ""
    for key in ("%s:%s" % (project, model), model, "*"):
        if key in table:
            try:
                return int(table[key])
            except (TypeError, ValueError):
                return None
    return None


def _learned_project_limit(conn, project: str, model: str) -> int | None:
    try:
        row = conn.execute(
            """SELECT daily_limit FROM gemini_project_limits
               WHERE project = ? AND model = ?""",
            (project, model)).fetchone()
        return int(row["daily_limit"]) if row else None
    except Exception:
        return None


def _daily_limit_for(conn, project: str, model: str | None) -> int | None:
    """The (project, model) daily request limit the ledger enforces:
    configured (env) first, then the limit learned from a real RPD
    exhaustion, then the model-class free-tier default, else None
    (no pre-call daily cap for this pair)."""
    model = model or ""
    cfg = _configured_limit("GEMINI_DAILY_LIMITS", project, model)
    if cfg is not None:
        return cfg
    learned = _learned_project_limit(conn, project, model)
    if learned is not None:
        return learned
    low = model.lower()
    for needle, limit in _MODEL_DAILY_DEFAULTS:
        if needle in low:
            return limit
    return None


def _rpm_limit_for(project: str, model: str | None) -> int | None:
    model = model or ""
    cfg = _configured_limit("GEMINI_RPM_LIMITS", project, model)
    if cfg is not None:
        return cfg
    low = model.lower()
    for needle, limit in _MODEL_RPM_DEFAULTS:
        if needle in low:
            return limit
    return None


def _project_usage_today(conn, model: str | None) -> dict[str, int]:
    """project -> upstream attempts today (Pacific day) against
    `model`, summed over the project's keys from the burn table."""
    day = _pt_day_str()
    kmap = _key_project_map(conn)
    used: dict[str, int] = {}
    try:
        q = """SELECT key_id, calls FROM gemini_key_model_day
               WHERE pt_day = ?"""
        params: list = [day]
        if model:
            q += " AND model = ?"
            params.append(model)
        for r in conn.execute(q, params).fetchall():
            proj = kmap.get(r["key_id"])
            if proj:
                used[proj] = used.get(proj, 0) + int(r["calls"] or 0)
    except Exception:
        pass
    return used


def _project_minute_usage(kmap: dict[int, str]) -> dict[str, int]:
    """project -> dispatches in the last 60 s (in-memory dispatch
    windows summed over the project's keys). Caller holds _lock or
    accepts a racy snapshot."""
    cutoff = time.monotonic() - _DISPATCH_WINDOW_S
    out: dict[str, int] = {}
    for key_id, dq in _dispatch_times.items():
        proj = kmap.get(key_id)
        if not proj:
            continue
        n = sum(1 for ts in dq if ts > cutoff)
        if n:
            out[proj] = out.get(proj, 0) + n
    return out


def _ledger_blocked_projects(model: str | None) -> set[str]:
    """Projects the pre-call ledger refuses for `model` right now:
    daily usage at/over the (project, model) limit, or the last
    minute's dispatches at/over the per-minute limit. Empty when v2
    is off or nothing is at a limit."""
    if not _v2_on():
        return set()
    blocked: set[str] = set()
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            kmap = _key_project_map(conn)
            used = _project_usage_today(conn, model)
            projects = set(kmap.values())
            for proj in projects:
                limit = _daily_limit_for(conn, proj, model)
                if limit is not None and used.get(proj, 0) >= limit:
                    blocked.add(proj)
            minute = _project_minute_usage(kmap)
            for proj in projects:
                rpm = _rpm_limit_for(proj, model)
                if rpm is not None and minute.get(proj, 0) >= rpm:
                    blocked.add(proj)
        finally:
            conn.close()
    except Exception:
        return set()
    return blocked


def _filter_projects(cands, blocked: set[str], kmap: dict[int, str]):
    if not blocked:
        return cands
    return [c for c in cands if kmap.get(c[0]) not in blocked]


def _remaining_fractions(model: str | None) -> dict[str, float | None]:
    """project -> remaining daily-budget fraction for `model`
    (1.0 = untouched, 0.0 = exhausted; None = no known limit, which
    ordering treats as fully available — unmeasured projects have
    never exhausted)."""
    if not model:
        chain = model_chain()
        model = chain[0] if chain else None
    out: dict[str, float | None] = {}
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            kmap = _key_project_map(conn)
            used = _project_usage_today(conn, model)
            for proj in set(kmap.values()):
                limit = _daily_limit_for(conn, proj, model)
                if not limit:
                    out[proj] = None
                else:
                    out[proj] = max(
                        0.0, (limit - used.get(proj, 0)) / float(limit))
        finally:
            conn.close()
    except Exception:
        pass
    return out


def pool_remaining_fraction(model: str | None = None) -> float | None:
    """Pool-wide remaining daily budget fraction for `model`
    (default: the chain primary): summed remaining / summed limits
    over projects WITH a known limit. None when no project has a
    known limit (nothing to budget against)."""
    model = model or (model_chain()[0] if model_chain() else None)
    fracs = _remaining_fractions(model)
    known = {p: f for p, f in fracs.items() if f is not None}
    if not known:
        return None
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            kmap = _key_project_map(conn)
            used = _project_usage_today(conn, model)
            total_limit = 0
            total_remaining = 0.0
            for proj in known:
                limit = _daily_limit_for(conn, proj, model) or 0
                total_limit += limit
                total_remaining += max(0.0, limit - used.get(proj, 0))
        finally:
            conn.close()
    except Exception:
        return None
    if total_limit <= 0:
        return None
    return total_remaining / float(total_limit)


def next_reset_epoch() -> float:
    """Epoch of the next Pacific-midnight quota reset."""
    return _next_pt_midnight_epoch()


def seconds_until_reset() -> float:
    return max(0.0, _next_pt_midnight_epoch() - time.time())


def batch_window() -> dict:
    """Reset-aware scheduling facts for batch work: the preferred
    window is the 3 hours right after the Pacific reset (03:00 ET),
    when every project's daily budget is full again — the Her Day
    batch belongs there, not stacked on the 08:00-10:00 wave."""
    reset = _next_pt_midnight_epoch()
    since_reset = 86400.0 - (reset - time.time())
    return {
        "next_reset_epoch": reset,
        "seconds_until_reset": max(0.0, reset - time.time()),
        "seconds_since_reset": max(0.0, since_reset),
        "in_preferred_window": 0.0 <= since_reset <= 3 * 3600.0,
    }


# --- v2 circuit breakers -----------------------------------------------------
# Rolling per-key outcome window (real traffic only; prober checks and
# model/request-class outcomes excluded, same as the storm breaker).
_outcomes: dict[int, deque] = {}
_bench_until: dict[int, float] = {}


def _breaker_on() -> bool:
    return _v2_on() and _flag("GEMINI_BREAKER", "1")


def _breaker_min_attempts() -> int:
    try:
        return max(1, int(os.environ.get("GEMINI_BREAKER_MIN_ATTEMPTS", "5")))
    except (TypeError, ValueError):
        return 5


def _breaker_cooldown_s() -> float:
    return max(1.0, _env_float("GEMINI_BREAKER_COOLDOWN_S", 60.0))


def _note_outcome_locked(key_id: int, ok: bool) -> None:
    """Record one real attempt outcome; trip the per-key breaker
    when the rolling minute goes bad. Caller holds _lock."""
    if not _breaker_on():
        return
    now = time.monotonic()
    dq = _outcomes.setdefault(key_id, deque())
    dq.append((now, ok))
    cutoff = now - 120.0
    while dq and dq[0][0] <= cutoff:
        dq.popleft()
    if ok:
        return
    recent = [(ts, o) for ts, o in dq if ts > now - 60.0]
    if len(recent) >= _breaker_min_attempts():
        fails = sum(1 for _ts, o in recent if not o)
        if fails / len(recent) > 0.5:
            _bench_until[key_id] = now + _breaker_cooldown_s()
            logger.warning(
                "gemini_keys: circuit breaker benched key %s for "
                "%.0fs (%d/%d failures in the last minute)",
                key_id, _breaker_cooldown_s(), fails, len(recent))


def _benched_ids() -> set[int]:
    if not _breaker_on():
        return set()
    now = time.monotonic()
    return {k for k, until in _bench_until.items() if until > now}


def _filter_benched(cands, benched: set[int]):
    if not benched:
        return cands
    return [c for c in cands if c[0] not in benched]


# --- v2 priority lanes ---------------------------------------------------------
_LANES = ("interactive", "proactive_morning", "proactive_ambient", "batch")
_LANE_FLOOR_ENV = {
    "batch": ("GEMINI_LANE_MIN_BUDGET_BATCH", 0.15),
    "proactive_ambient": ("GEMINI_LANE_MIN_BUDGET_AMBIENT", 0.08),
    "proactive_morning": ("GEMINI_LANE_MIN_BUDGET_MORNING", 0.03),
}


def _resolve_lane(source: str | None, lane: str | None) -> str:
    """The request's priority lane: an explicit valid lane wins;
    otherwise bulk-class sources are batch and everything else is
    interactive (the safe default for unmarked callers)."""
    if lane and lane in _LANES:
        return lane
    return "batch" if _source_class(source) == "bulk" else "interactive"


def _lane_sclass(lane: str) -> str:
    """Map a v2 lane onto the Stage-2 source class: morning
    proactive is time-critical and rides the interactive class
    (reserve access); ambient + batch are bulk."""
    return "interactive" if lane in ("interactive", "proactive_morning") \
        else "bulk"


def _lane_floor(lane: str) -> float | None:
    spec = _LANE_FLOOR_ENV.get(lane)
    if spec is None:
        return None
    return max(0.0, min(1.0, _env_float(spec[0], spec[1])))


# --- v2 paid guardrails ---------------------------------------------------------

def _paid_ceiling() -> int:
    try:
        return max(0, int(os.environ.get("GEMINI_PAID_DAILY_CEILING",
                                         "1000")))
    except (TypeError, ValueError):
        return 1000


def _paid_trickle() -> int:
    try:
        return max(0, int(os.environ.get("GEMINI_PAID_TRICKLE_DAILY",
                                         "25")))
    except (TypeError, ValueError):
        return 25


_paid_alert_day: dict[str, str] = {}


def _paid_calls_today() -> int:
    """Upstream attempts served by paid keys today (Pacific day),
    from the burn table; falls back to the spend ledger's paid
    count when burn tracking has nothing yet."""
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            day = _pt_day_str()
            row = conn.execute(
                """SELECT COALESCE(SUM(kmd.calls), 0) AS n
                   FROM gemini_key_model_day kmd
                   JOIN gemini_keys k ON k.id = kmd.key_id
                   WHERE kmd.pt_day = ? AND k.is_paid = 1""",
                (day,)).fetchone()
            n = int(row["n"] or 0) if row else 0
            if n:
                return n
            row = conn.execute(
                """SELECT COALESCE(SUM(paid_calls), 0) AS n
                   FROM gemini_source_day WHERE pt_day = ?""",
                (day,)).fetchone()
            return int(row["n"] or 0) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def _paid_state() -> dict:
    calls = _paid_calls_today()
    ceiling = _paid_ceiling()
    trickle = _paid_trickle()
    return {
        "calls_today": calls,
        "ceiling": ceiling,
        "ceiling_hit": bool(ceiling and calls >= ceiling),
        "trickle": trickle,
        "trickle_exceeded": bool(trickle and calls > trickle),
    }


def _note_paid_state_alerts(state: dict) -> None:
    """Once-a-day (Pacific) log alerts for trickle/ceiling crossings
    — the guardrail is only real if a human hears about it."""
    day = _pt_day_str()
    if state["trickle_exceeded"] and _paid_alert_day.get("trickle") != day:
        _paid_alert_day["trickle"] = day
        logger.warning(
            "gemini_keys: paid fallback past trickle (%d calls today "
            "> %d) — free pool is leaking into the paid lane",
            state["calls_today"], state["trickle"])
    if state["ceiling_hit"] and _paid_alert_day.get("ceiling") != day:
        _paid_alert_day["ceiling"] = day
        logger.warning(
            "gemini_keys: paid daily ceiling hit (%d/%d) — paid keys "
            "leave rotation until the Pacific reset",
            state["calls_today"], state["ceiling"])


def _filter_paid_capped(cands, paid_capped: bool):
    if not paid_capped:
        return cands
    # Lock-free on purpose: this filter runs inside selection paths
    # that already hold _lock, and the public paid_key_ids() takes
    # that (non-reentrant) lock — calling it here deadlocked the
    # pool the first time the ceiling was hit. A direct read is
    # fine: the paid id set changes only on admin key add/delete.
    paid: set[int] = set()
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            paid = {r["id"] for r in conn.execute(
                "SELECT id FROM gemini_keys WHERE is_paid = 1"
            ).fetchall()}
        finally:
            conn.close()
    except Exception:
        return cands
    return [c for c in cands if c[0] not in paid]


def _cross_key_retries() -> int:
    try:
        return max(0, int(os.environ.get("GEMINI_CROSS_KEY_RETRIES", "1")))
    except (TypeError, ValueError):
        return 1


def _v2_rollup() -> dict:
    """Admin rollup for Key Manager v2: per-project remaining daily
    budget (chain primary model), keys-by-state counts, paid usage
    vs ceiling, and a pool-exhaustion projection at the last hour's
    burn rate. Best-effort like the rest of degradation_info."""
    out: dict = {"projects": [], "keys_by_state": {},
                 "paid": _paid_state(), "projection": None,
                 "pool_remaining_fraction": None}
    try:
        chain = model_chain()
        primary = chain[0] if chain else None
        conn = db.connect()
        try:
            _ensure_table(conn)
            kmap = _key_project_map(conn)
            used = _project_usage_today(conn, primary)
            rows = conn.execute("SELECT * FROM gemini_keys").fetchall()
            by_project: dict[str, list] = {}
            for r in rows:
                by_project.setdefault(_project_of(r), []).append(r)
            total_limit = 0
            total_remaining = 0.0
            for proj in sorted(by_project):
                members = by_project[proj]
                states: dict[str, int] = {}
                for r in members:
                    st = _row_to_public(r)["status"]
                    states[st] = states.get(st, 0) + 1
                    out["keys_by_state"][st] = \
                        out["keys_by_state"].get(st, 0) + 1
                limit = _daily_limit_for(conn, proj, primary)
                u = int(used.get(proj, 0))
                remaining = (None if limit is None
                             else max(0, limit - u))
                if limit:
                    total_limit += limit
                    total_remaining += float(remaining or 0)
                out["projects"].append({
                    "project": proj,
                    "assigned": any(
                        (r["project"] or "").strip() for r in members),
                    "keys": len(members),
                    "keys_by_state": states,
                    "paid": any(bool(r["is_paid"]) for r in members),
                    "model": primary,
                    "used_today": u,
                    "daily_limit": limit,
                    "remaining": remaining,
                    "remaining_fraction": (
                        None if not limit else
                        round(float(remaining or 0) / limit, 3)),
                })
            rate = 0
            try:
                row = conn.execute(
                    """SELECT COUNT(*) AS n FROM gemini_key_usage
                       WHERE created_at >= datetime('now', '-1 hour')"""
                ).fetchone()
                rate = int(row["n"] or 0) if row else 0
            except Exception:
                rate = 0
        finally:
            conn.close()
        out["pool_remaining_fraction"] = pool_remaining_fraction(primary)
        if total_limit and rate > 0:
            hours = total_remaining / rate
            out["projection"] = {
                "status": "ok", "model": primary,
                "remaining_calls": int(total_remaining),
                "burn_rate_per_hour": rate,
                "hours": round(hours, 1),
                "exhaust_at_epoch": time.time() + hours * 3600.0,
            }
        elif total_limit:
            out["projection"] = {
                "status": "no_recent_burn", "model": primary,
                "remaining_calls": int(total_remaining), "hours": None}
        else:
            out["projection"] = {
                "status": "learning",
                "reason": "no project has a known daily limit yet",
                "hours": None}
    except Exception:
        pass
    return out


# --- Stage 3 recording: burn counters + spend ledger -------------------------

def _record_attempt_stats(key_id: int, model: str | None, source: str,
                          ok: bool) -> None:
    """One short transaction per attempt feeding the Stage-3 day
    tables: gemini_key_model_day (burn) and gemini_source_day (spend
    ledger). Best-effort like usage logging — accounting must never
    break a call."""
    if not (_burn_on() or _ledger_on()):
        return
    try:
        day = _pt_day_str()
        model = model or "(unknown)"
        conn = db.connect()
        try:
            _ensure_table(conn)
            is_paid = 0
            row = conn.execute(
                "SELECT is_paid FROM gemini_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
            if row:
                is_paid = 1 if row["is_paid"] else 0
            if _burn_on():
                conn.execute(
                    """INSERT INTO gemini_key_model_day
                           (pt_day, key_id, model, calls, ok, fail)
                       VALUES (?, ?, ?, 1, ?, ?)
                       ON CONFLICT(pt_day, key_id, model) DO UPDATE SET
                           calls = calls + 1,
                           ok = ok + excluded.ok,
                           fail = fail + excluded.fail""",
                    (day, key_id, model, 1 if ok else 0,
                     0 if ok else 1),
                )
            if _ledger_on():
                conn.execute(
                    """INSERT INTO gemini_source_day
                           (pt_day, source, calls, failures, paid_calls)
                       VALUES (?, ?, 1, ?, ?)
                       ON CONFLICT(pt_day, source) DO UPDATE SET
                           calls = calls + 1,
                           failures = failures + excluded.failures,
                           paid_calls = paid_calls + excluded.paid_calls""",
                    (day, source or "unknown", 0 if ok else 1, is_paid),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _learn_limit_from_rpd(key_id: int, model: str | None) -> None:
    """An RPD park just landed for (key, model): today's burn count
    for that pair is an OBSERVED daily-limit data point — store it
    (keeping the smallest observation, the tightest truthful bound).
    Only ever called with a real exhaustion event; published quota
    numbers are never written here."""
    if not _burn_on() or not model:
        return
    try:
        day = _pt_day_str()
        conn = db.connect()
        try:
            _ensure_table(conn)
            row = conn.execute(
                """SELECT calls FROM gemini_key_model_day
                   WHERE pt_day = ? AND key_id = ? AND model = ?""",
                (day, key_id, model),
            ).fetchone()
            if not row or not row["calls"]:
                return
            observed = int(row["calls"])
            conn.execute(
                """INSERT INTO gemini_key_limits
                       (key_id, model, daily_limit, learned_from,
                        updated_at)
                   VALUES (?, ?, ?, 'observed_rpd', datetime('now'))
                   ON CONFLICT(key_id, model) DO UPDATE SET
                       daily_limit = MIN(daily_limit,
                                         excluded.daily_limit),
                       learned_from = 'observed_rpd',
                       updated_at = datetime('now')""",
                (key_id, model, observed),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _learn_project_limit_from_rpd(key_id: int, model: str | None) -> None:
    """Key Manager v2: an RPD park just landed for (key, model);
    today's summed burn across the key's PROJECT is the project's
    observed daily limit (the shared quota the keys drew on). Kept
    at the smallest observation; best-effort like the per-key
    learner it sits beside."""
    if not _burn_on() or not model:
        return
    try:
        day = _pt_day_str()
        conn = db.connect()
        try:
            _ensure_table(conn)
            me = conn.execute(
                "SELECT id, project FROM gemini_keys WHERE id = ?",
                (key_id,)).fetchone()
            if me is None:
                return
            project = _project_of(me)
            ids = [r["id"] for r in conn.execute(
                "SELECT id, project FROM gemini_keys").fetchall()
                if _project_of(r) == project]
            if not ids:
                return
            marks = ",".join("?" for _ in ids)
            total = 0
            for r in conn.execute(
                    f"""SELECT calls FROM gemini_key_model_day
                        WHERE pt_day = ? AND model = ?
                          AND key_id IN ({marks})""",
                    [day, model] + ids).fetchall():
                total += int(r["calls"] or 0)
            if total <= 0:
                return
            conn.execute(
                """INSERT INTO gemini_project_limits
                       (project, model, daily_limit, learned_from,
                        updated_at)
                   VALUES (?, ?, ?, 'observed_rpd', datetime('now'))
                   ON CONFLICT(project, model) DO UPDATE SET
                       daily_limit = MIN(daily_limit,
                                         excluded.daily_limit),
                       learned_from = 'observed_rpd',
                       updated_at = datetime('now')""",
                (project, model, total),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# --- Stage 3 sticky sessions ---------------------------------------------------

def _session_key_id(session_id: str | None) -> int | None:
    if not session_id:
        return None
    now = time.monotonic()
    with _lock:
        entry = _sessions.get(session_id)
        if entry is None:
            return None
        if entry["expires"] <= now:
            _sessions.pop(session_id, None)
            return None
        return entry["key_id"]


def _pin_session(session_id: str, key_id: int) -> None:
    with _lock:
        _sessions[session_id] = {
            "key_id": key_id,
            "expires": time.monotonic() + _SESSION_TTL_S,
        }
        # Opportunistic sweep so the map cannot grow unbounded.
        if len(_sessions) > 512:
            now = time.monotonic()
            for sid in [s for s, e in _sessions.items()
                        if e["expires"] <= now]:
                _sessions.pop(sid, None)


def _unpin_session(session_id: str) -> None:
    with _lock:
        _sessions.pop(session_id, None)


def _pin_first(rows: list, key_id: int | None) -> list:
    """Move the pinned key's row to the front of a candidate list
    (in place, order otherwise preserved). Candidates are (id,
    value) tuples or sqlite rows — both index [0] to the id."""
    if not key_id or not rows:
        return rows
    for i, r in enumerate(rows):
        if r[0] == key_id:
            if i:
                rows.insert(0, rows.pop(i))
            break
    return rows


def _note_cached_tokens(data) -> None:
    """Count cached-content tokens ONLY when the API actually
    reports them (usageMetadata.cachedContentTokenCount). Observed
    accounting — nothing is estimated or invented."""
    global _cached_tokens_total
    try:
        meta = (data or {}).get("usageMetadata") or {}
        n = meta.get("cachedContentTokenCount")
        if isinstance(n, int) and n > 0:
            with _lock:
                _cached_tokens_total += n
    except Exception:
        pass


def get_usage(limit: int = 100) -> list[dict]:
    """Recent key usage for the admin endpoint: last N requests with
    source, key label, timestamp, success/failure and attempt latency."""
    limit = max(1, min(int(limit or 100), 500))
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            rows = conn.execute(
                """SELECT u.id, u.created_at, u.key_id, u.source, u.success,
                          u.latency_ms, u.error_class,
                          k.label AS key_label, k.is_paid AS is_paid
                   FROM gemini_key_usage u
                   LEFT JOIN gemini_keys k ON k.id = u.key_id
                   ORDER BY u.id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [{
                "id": r["id"],
                "created_at": r["created_at"],
                "key_id": r["key_id"],
                "key_label": r["key_label"],
                "is_paid": bool(r["is_paid"]) if r["is_paid"] is not None else None,
                "source": r["source"],
                "success": bool(r["success"]),
                "latency_ms": r["latency_ms"],
                "error_class": r["error_class"],
            } for r in rows]
        finally:
            conn.close()


def usage_by_source(limit: int = 100) -> list[dict]:
    """Aggregate counts per source over the recent usage window."""
    limit = max(1, min(int(limit or 100), 500))
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            rows = conn.execute(
                """SELECT source,
                          COUNT(*) AS requests,
                          SUM(success) AS successes,
                          SUM(1 - success) AS failures
                   FROM (SELECT source, success FROM gemini_key_usage
                         ORDER BY id DESC LIMIT ?)
                   GROUP BY source
                   ORDER BY requests DESC""",
                (limit,),
            ).fetchall()
            return [{
                "source": r["source"],
                "requests": r["requests"],
                "successes": r["successes"] or 0,
                "failures": r["failures"] or 0,
            } for r in rows]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Admin helpers (values are masked outside this module)
# ---------------------------------------------------------------------------

def _row_get(row, name):
    """Row value or None when the column isn't in the row (rows read
    before a migration ran)."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _row_to_public(row) -> dict:
    exhausted = bool(row["quota_exhausted_until"])
    if exhausted:
        try:
            until = datetime.strptime(row["quota_exhausted_until"],
                                      "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            exhausted = until > datetime.now(timezone.utc)
        except (ValueError, TypeError):
            exhausted = True
    if row["needs_attention"]:
        status = "needs_attention"
    elif not row["is_active"]:
        status = "disabled"
    elif exhausted:
        status = "cooling_down"
    elif row["consecutive_failures"]:
        status = "degraded"
    else:
        status = "healthy"
    return {
        "id": row["id"],
        "label": row["label"],
        "key_masked": mask_key(row["key_value"]),
        "is_paid": bool(row["is_paid"]),
        "is_active": bool(row["is_active"]),
        "status": status,
        "consecutive_failures": row["consecutive_failures"],
        "total_requests": row["total_requests"],
        "total_failures": row["total_failures"],
        "last_success_at": row["last_success_at"],
        "last_failure_at": row["last_failure_at"],
        "quota_exhausted_until": row["quota_exhausted_until"],
        "needs_attention": bool(row["needs_attention"]),
        # Stage 1: WHY a key needs attention (plain cause, forensics)
        # and WHICH quota dimension parked it ('rpm'/'rpd'/None).
        "attention_reason": _row_get(row, "attention_reason"),
        "quota_park_kind": _row_get(row, "quota_park_kind"),
        # Key Manager v2: the key's project identity (the tag an
        # admin assigned; the effective id falls back to "key-<id>").
        "project": _project_of(row),
        "project_assigned": bool((_row_get(row, "project") or "").strip()),
        "created_at": row["created_at"],
    }


def list_keys() -> list[dict]:
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            _seed_legacy_key(conn)
            rows = conn.execute(
                "SELECT * FROM gemini_keys ORDER BY is_paid ASC, id ASC"
            ).fetchall()
            return [_row_to_public(r) for r in rows]
        finally:
            conn.close()


def add_key(key_value: str, label: str = "", is_paid: bool = False,
            project: str = "") -> dict:
    key_value = (key_value or "").strip()
    if not key_value:
        raise ValueError("key is required")
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            _seed_legacy_key(conn)
            exists = conn.execute(
                "SELECT id FROM gemini_keys WHERE key_hash = ?",
                (_key_hash(key_value),),
            ).fetchone()
            if exists:
                raise ValueError("key already exists")
            cur = conn.execute(
                """INSERT INTO gemini_keys
                       (key_hash, key_value, label, is_paid, project)
                   VALUES (?, ?, ?, ?, ?)""",
                (_key_hash(key_value), key_value, (label or "").strip(),
                 1 if is_paid else 0, (project or "").strip()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM gemini_keys WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return _row_to_public(row)
        finally:
            conn.close()


def delete_key(key_id: int) -> bool:
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            cur = conn.execute(
                "DELETE FROM gemini_keys WHERE id = ?", (key_id,))
            conn.commit()
            return (cur.rowcount or 0) > 0
        finally:
            conn.close()


def set_active(key_id: int, is_active: bool) -> dict | None:
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            # Re-enabling also clears the attention flag and backoff so the
            # key is tried again right away.
            conn.execute(
                """UPDATE gemini_keys
                   SET is_active = ?,
                       needs_attention = CASE WHEN ? = 1 THEN 0
                                           ELSE needs_attention END,
                       attention_reason = CASE WHEN ? = 1 THEN NULL
                                               ELSE attention_reason END,
                       quota_exhausted_until = CASE WHEN ? = 1 THEN NULL
                                               ELSE quota_exhausted_until END
                   WHERE id = ?""",
                (1 if is_active else 0, 1 if is_active else 0,
                 1 if is_active else 0,
                 1 if is_active else 0, key_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM gemini_keys WHERE id = ?", (key_id,)
            ).fetchone()
            return _row_to_public(row) if row else None
        finally:
            conn.close()


def test_key(key_id: int) -> dict | None:
    """Minimal live call against one key. Returns health, never the key."""
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT * FROM gemini_keys WHERE id = ?", (key_id,)
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    payload = {
        "contents": [{"role": "user",
                      "parts": [{"text": "Reply with the single word: ok"}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 5},
    }
    try:
        # Tests the model currently serving traffic (the primary while
        # healthy, the fallback while the primary is hot).
        data = _http_post(model_url(current_model()), payload,
                          row["key_value"], 20)
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if p.get("text"))
        report_success(key_id)
        return {"ok": True, "status": "healthy",
                "sample": text.strip()[:40]}
    except urllib.error.HTTPError as e:
        error_type = _classify_http_error(e.code)
        report_failure(key_id, error_type)
        return {"ok": False, "status": error_type, "http_code": e.code}
    except Exception as e:
        report_failure(key_id, "other")
        return {"ok": False, "status": "other",
                "error": str(e)[:120]}
