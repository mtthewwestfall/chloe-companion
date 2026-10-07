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
- 402 / payment errors: needs_attention = 1 (surfaced in admin); the key
  is skipped until an admin re-enables or fixes it.
- Other errors (5xx, network, timeout): counted, 2-minute backoff applied
  so the next request immediately tries a different key instead of
  retrying the same failing key. The key becomes eligible again quickly
  if the issue was transient.

Thread-safe: a module-level lock guards key selection and counter
updates. Network I/O happens outside the lock. Each call opens and closes
its own sqlite connection (same pattern as the rest of the app).

Secret storage: key values live in the server-side sqlite DB — the same
trust boundary as Railway env vars (the repo has no encryption infra, and
every other secret here is an env var). The admin list endpoint only ever
returns masked values ("...abcd"). Never log full key values.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config, db

_lock = threading.Lock()
logger = logging.getLogger(__name__)

# Backoff ladder for quota/429 failures, indexed by consecutive_failures.
_BACKOFF_MINUTES = (5, 15, 60, 240)

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
    success    INTEGER NOT NULL DEFAULT 0
)
"""


def _now_str() -> str:
    # Microsecond precision so back-to-back calls within the same
    # second still rotate correctly (ORDER BY last_used_at ASC).
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _ensure_table(conn) -> None:
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_USAGE_TABLE)
    # Migration: add last_used_at to pre-existing tables
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(gemini_keys)").fetchall()]
    if "last_used_at" not in cols:
        conn.execute("ALTER TABLE gemini_keys ADD COLUMN last_used_at TEXT")
        conn.commit()


def _log_usage(conn, key_id: int | None, source: str, success: bool) -> None:
    """Lightweight per-request log: which app used which key, and whether
    it worked. Powers the admin usage endpoint."""
    try:
        conn.execute(
            "INSERT INTO gemini_key_usage (key_id, source, success)"
            " VALUES (?, ?, ?)",
            (key_id, (source or "unknown")[:64], 1 if success else 0),
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
    query = """SELECT id, key_value, is_paid, consecutive_failures
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


def get_candidate_keys() -> list[tuple[int, str]]:
    """Ordered (key_id, key_value) candidates. Empty when nothing usable.

    Two-pool system: the active pool rotates normally. The reserve pool
    (~30% of keys, see _reserve_count) is held back and only tapped when
    every active key is cooling down, failed, or disabled. Reserve usage
    is logged so exhaustion of the active pool is visible.
    """
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            active_ids, reserve_ids = _pool_ids(conn)
            # Tier 1: active pool
            rows = _eligible_rows(conn, active_ids)
            if rows:
                return [(r["id"], r["key_value"]) for r in rows]
            # Tier 2: reserve pool — active pool fully exhausted
            if reserve_ids:
                rows = _eligible_rows(conn, reserve_ids)
                if rows:
                    logger.warning(
                        "gemini_keys: active pool exhausted, tapping "
                        "reserve pool (%d reserve keys eligible)",
                        len(rows),
                    )
                    return [(r["id"], r["key_value"]) for r in rows]
            return []
        finally:
            conn.close()


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
                       quota_exhausted_until = NULL
                   WHERE id = ?""",
                (_now_str(), _now_str(), key_id),
            )
            conn.commit()
        finally:
            conn.close()


def report_failure(key_id: int, error_type: str) -> None:
    """error_type: 'quota' (429), 'payment' (402), or 'other'."""
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT consecutive_failures FROM gemini_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
            if not row:
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
                           quota_exhausted_until = ?
                       WHERE id = ?""",
                    (failures, _now_str(), until, key_id),
                )
            elif error_type == "payment":
                conn.execute(
                    """UPDATE gemini_keys
                       SET consecutive_failures = ?,
                           last_failure_at = ?,
                           total_requests = total_requests + 1,
                           total_failures = total_failures + 1,
                           needs_attention = 1
                       WHERE id = ?""",
                    (failures, _now_str(), key_id),
                )
            else:
                # "Other" errors (5xx, timeout, network): apply a short 2-minute
                # backoff so the next request immediately tries a different key
                # instead of retrying this same failing key. The key becomes
                # eligible again quickly if the issue was transient.
                until = (datetime.now(timezone.utc)
                         + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """UPDATE gemini_keys
                       SET consecutive_failures = ?,
                           last_failure_at = ?,
                           total_requests = total_requests + 1,
                           total_failures = total_failures + 1,
                           quota_exhausted_until = ?
                       WHERE id = ?""",
                    (failures, _now_str(), until, key_id),
                )
            conn.commit()
        finally:
            conn.close()


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
                   WHERE quota_exhausted_until IS NOT NULL
                     AND quota_exhausted_until <= datetime('now')"""
            )
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()


def _classify_http_error(code: int | None) -> str:
    if code == 429:
        return "quota"
    if code == 402:
        return "payment"
    return "other"


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


def post_json(url: str, payload: dict, timeout: int = 30,
              source: str = "unknown") -> dict | None:
    """POST JSON to a Gemini endpoint with transparent key rotation.

    Tries each eligible key in order (free first, healthiest first). On a
    failure the key's health is recorded (with backoff so the next request
    skips it) and the next key is tried immediately. If every eligible key
    is cooling down, falls back to trying all active keys ignoring backoff
    as a last resort. Returns the parsed JSON response, or None when every
    key failed / no keys exist.

    `source` names the calling app (e.g. "whispr-web", "creator-bot");
    each attempt is logged to gemini_key_usage for the admin endpoint.
    """
    candidates = get_candidate_keys()
    if not candidates:
        # Last resort: all keys are cooling down — try them anyway
        # ignoring backoff rather than failing the request outright.
        candidates = _all_active_keys()
    for key_id, key_value in candidates:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": key_value},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            report_failure(key_id, _classify_http_error(e.code))
            _log_usage_conn(key_id, source, False)
            continue
        except Exception:
            report_failure(key_id, "other")
            _log_usage_conn(key_id, source, False)
            continue
        report_success(key_id)
        _log_usage_conn(key_id, source, True)
        return data
    return None


def _log_usage_conn(key_id: int | None, source: str, success: bool) -> None:
    """Open a short-lived connection just for usage logging."""
    try:
        conn = db.connect()
        try:
            _ensure_table(conn)
            _log_usage(conn, key_id, source, success)
        finally:
            conn.close()
    except Exception:
        pass


def get_usage(limit: int = 100) -> list[dict]:
    """Recent key usage for the admin endpoint: last N requests with
    source, key label, timestamp, and success/failure."""
    limit = max(1, min(int(limit or 100), 500))
    with _lock:
        conn = db.connect()
        try:
            _ensure_table(conn)
            rows = conn.execute(
                """SELECT u.id, u.created_at, u.key_id, u.source, u.success,
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


_TEST_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
             "gemini-3.1-flash-lite:generateContent")


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
    req = urllib.request.Request(
        _TEST_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": row["key_value"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
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
