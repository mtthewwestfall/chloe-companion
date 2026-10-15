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
  Every usage row records its error class (quota / payment /
  transient / request) so failure rates can be reported by class.

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
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
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
# Total wall-clock budget for one post_json call across attempts.
_ATTEMPT_BUDGET_DEFAULT_S = 50.0
# Ranking latency assumed for a believed-healthy key with no samples yet.
_UNKNOWN_LATENCY_S = 2.0
# A confirmed success keeps a key "believed healthy" for this long
# (real traffic and the prober refresh it well before it lapses).
_BELIEF_TTL_S = 600.0

# --- Background prober ------------------------------------------------------
# Lean settings (efficiency pass, 2026-10-15): the prober used to
# generate the overwhelming majority of usage-log rows (455 of the last
# 500 at the 2026-10-15 baseline) by re-confirming idle healthy keys
# every ~2 minutes. It now wakes every 30s, sends at most
# _PROBE_MAX_PER_CYCLE probes (recovery checks first), re-confirms an
# idle healthy key only after _PROBE_CONFIRM_S, and never probes the
# same key more often than _PROBE_MIN_INTERVAL_S.
_PROBE_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "gemini-3.1-flash-lite:generateContent")
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
            try:
                _ensure_table(conn)
                _seed_belief_locked(conn)
                rows = conn.execute("SELECT * FROM gemini_keys").fetchall()
                statuses = [_row_to_public(r)["status"] for r in rows]
            finally:
                conn.close()
            now = time.time()
            healthy_ids = sorted(
                kid for kid, b in _belief.items()
                if b.get("ok") and (now - b.get("at", 0.0)) <= _BELIEF_TTL_S)
            info["believed_healthy_key_ids"] = healthy_ids
            info["pool"] = {s: statuses.count(s) for s in sorted(set(statuses))}
            info["prober"] = {
                "running": bool(_prober_thread is not None
                                and _prober_thread.is_alive()),
                "last_cycle_at": (
                    datetime.fromtimestamp(_prober_last_cycle_at,
                                           tz=timezone.utc).isoformat()
                    if _prober_last_cycle_at else None),
                "probes_total": _probes_total,
                "probes_ok": _probes_ok,
            }
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
        # transient / request; NULL on success and on pre-migration
        # rows) so failure rates can be reported by class.
        conn.execute("ALTER TABLE gemini_key_usage ADD COLUMN error_class TEXT")
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
                    day_requests, day_stamp
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
    least-recently-used."""
    def keyfn(r):
        return (_health_score(r["id"], r["consecutive_failures"]),
                _day_count(r),
                r["last_used_at"] or "")

    free = sorted((r for r in rows if not r["is_paid"]), key=keyfn)
    paid = sorted((r for r in rows if r["is_paid"]), key=keyfn)
    return [(r["id"], r["key_value"]) for r in free + paid]


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
                return _order_by_health(rows)
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
                    return _order_by_health(rows)
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

        def rank(r):
            b = _belief.get(r["id"]) or {}
            lat = b.get("lat")
            return (0 if not r["is_paid"] else 1,
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
        return [(r["id"], r["key_value"]) for r in pool]


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
                       quota_exhausted_until = NULL
                   WHERE id = ?""",
                (_now_str(), _now_str(), key_id),
            )
            conn.commit()
        finally:
            conn.close()
        # A success fully restores the key: any transient streak is
        # forgiven along with the failure counters.
        _transient_streaks.pop(key_id, None)
        entry = _belief_entry_locked(key_id)
        entry["ok"] = True
        entry["at"] = time.time()


def report_failure(key_id: int, error_type: str) -> None:
    """Record a failed attempt, by error class (2026-10-15 separation):

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
                return
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
            failures = (row["consecutive_failures"] or 0) + 1
            if error_type == "quota":
                wait = _BACKOFF_MINUTES[min(failures - 1, len(_BACKOFF_MINUTES) - 1)]
                until = (datetime.now(timezone.utc)
                         + timedelta(minutes=wait)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """UPDATE gemini_keys
                       SET consecutive_failures = ?,
                           last_failure_at = ?,
                           total_requests = total_requests + 1,
                           total_failures = total_failures + 1,
                           """ + _DAY_BUMP_SQL + """
                           quota_exhausted_until = ?
                       WHERE id = ?""",
                    (failures, _now_str(), until, key_id),
                )
                _note_failure_locked(key_id)
            elif error_type == "payment":
                conn.execute(
                    """UPDATE gemini_keys
                       SET consecutive_failures = ?,
                           last_failure_at = ?,
                           total_requests = total_requests + 1,
                           total_failures = total_failures + 1,
                           """ + _DAY_BUMP_SQL + """
                           needs_attention = 1
                       WHERE id = ?""",
                    (failures, _now_str(), key_id),
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
    'quota' / 'payment' / 'transient' / 'request' on failure. All
    failure semantics live in report_failure — an attempt never
    sleeps."""
    with _lock:
        _record_dispatch_locked(key_id)
    start = time.monotonic()
    data, error_type = None, None
    try:
        data = _http_post(url, payload, key_value, timeout)
    except urllib.error.HTTPError as e:
        error_type = _classify_http_error(e.code)
        try:
            _err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            _err_body = ""
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
    if error_type is None:
        report_success(key_id)
        _log_usage_conn(key_id, source, True, seconds)
        return True, data, seconds, None
    report_failure(key_id, error_type)
    _log_usage_conn(key_id, source, False, seconds, error_type)
    return False, None, seconds, error_type


def _try_candidates(candidates, url, payload, timeout, source, tried,
                    state) -> dict | None:
    """Try candidates in order until one succeeds. `tried` (set of key
    ids) and `state` ({"n": attempts made, "deadline": monotonic}) are
    shared across the phases of one post_json call so the attempt cap
    and time budget hold globally. A 'request'-class failure (the call
    itself was bad) sets state["fatal"] and stops the walk: no other
    key can fix a bad request."""
    for key_id, key_value in candidates:
        if key_id in tried:
            continue
        if state["n"] >= state.get("max_n", _max_attempts()) or time.monotonic() >= state["deadline"]:
            break
        tried.add(key_id)
        state["n"] += 1
        ok, data, _seconds, error_type = _attempt(
            key_id, key_value, url, payload, timeout, source)
        if ok:
            return data
        if error_type == "request":
            state["fatal"] = "request"
            break
    return None


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
                pending.pop(fut, None)
                ok, data, _seconds, error_type = fut.result()
                if ok:
                    return data
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


def post_json(url: str, payload: dict, timeout: int = 30,
              source: str = "unknown", free_only: bool = False,
              max_attempts: int | None = None,
              fail_fast: bool = False) -> dict | None:
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
             "max_n": attempt_cap}
    tried: set[int] = set()

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

    fast = _only_free(_fast_candidates())
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
    sweep = _only_free(get_candidate_keys())
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
        return None
    data = _try_candidates(sweep, url, payload,
                           caller_timeout, source, tried, state)
    if data is not None:
        return data
    if state.get("fatal"):
        return None
    # Every active key just failed into a pause/backoff, so the active
    # pool is now exhausted by the reserve pool's own definition: one
    # bounded pass over ELIGIBLE reserve keys (backoff windows still
    # respected — this is not the old ignore-everything sweep), under
    # the same attempt cap and time budget.
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            _active_ids, reserve_ids = _pool_ids(conn)
            reserve_rows = [r for r in _eligible_rows(conn)
                            if r["id"] in reserve_ids]
        finally:
            conn.close()
    reserve = _only_free(_order_by_health(reserve_rows))
    if reserve:
        return _try_candidates(reserve, url, payload,
                               caller_timeout, source, tried, state)
    return None


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
    """One prober pass (lean version, 2026-10-15). Sends a 1-token probe
    to at most _PROBE_MAX_PER_CYCLE keys whose true state is stale or
    unknown, most valuable first:

    - priority 0: backoff expired or expiring within one cycle
      (recovery check — prompt, not next-request),
    - priority 1: unknown state (never succeeded, or marked bad
      outside backoff),
    - priority 2: believed healthy but idle past the confirm window
      (re-confirm before belief lapses).

    Never probes needs_attention or disabled keys, never deep inside a
    backoff window (only near expiry — probing a key hours from
    recovery teaches nothing and spends its quota), never the same key
    more often than _PROBE_MIN_INTERVAL_S, and never when real traffic
    touched the key within _PROBE_QUIET_S (real evidence beats a
    probe, and probes must never starve real traffic — each probe
    counts toward the per-key dispatch window like any other
    dispatch). Returns probes sent."""
    global _prober_last_cycle_at, _probes_total, _probes_ok
    now_wall = time.time()
    now_mono = time.monotonic()
    interval = _probe_interval_s()
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
                due.append((0, r))  # backoff expired / expiring: confirm
            elif belief and belief.get("ok"):
                if now_wall - belief.get("at", 0.0) >= _PROBE_CONFIRM_S:
                    due.append((2, r))  # idle healthy key: re-confirm
            else:
                due.append((1, r))  # unknown state: find out off-path
        due.sort(key=lambda d: d[0])
        due = [r for _prio, r in due[:_PROBE_MAX_PER_CYCLE]]
    sent = 0
    for r in due:
        _last_probe_mono[r["id"]] = time.monotonic()
        ok, _data, _seconds, _err = _attempt(
            r["id"], r["key_value"], _PROBE_URL, _probe_payload(),
            _PROBE_TIMEOUT_S, "prober")
        sent += 1
        with _lock:
            _probes_total += 1
            if ok:
                _probes_ok += 1
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


def add_key(key_value: str, label: str = "", is_paid: bool = False) -> dict:
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
                """INSERT INTO gemini_keys (key_hash, key_value, label, is_paid)
                   VALUES (?, ?, ?, ?)""",
                (_key_hash(key_value), key_value, (label or "").strip(),
                 1 if is_paid else 0),
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
                       quota_exhausted_until = CASE WHEN ? = 1 THEN NULL
                                               ELSE quota_exhausted_until END
                   WHERE id = ?""",
                (1 if is_active else 0, 1 if is_active else 0,
                 1 if is_active else 0, key_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM gemini_keys WHERE id = ?", (key_id,)
            ).fetchone()
            return _row_to_public(row) if row else None
        finally:
            conn.close()


_TEST_URL = _PROBE_URL


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
        data = _http_post(_TEST_URL, payload, row["key_value"], 20)
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
