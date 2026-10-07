"""Gemini API key manager: rotation, health tracking, automatic failover.

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
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config, db

_lock = threading.Lock()

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


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_table(conn) -> None:
    conn.execute(_CREATE_TABLE)
    # Migration: add last_used_at to pre-existing tables
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(gemini_keys)").fetchall()]
    if "last_used_at" not in cols:
        conn.execute("ALTER TABLE gemini_keys ADD COLUMN last_used_at TEXT")
        conn.commit()


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


def _eligible_rows(conn):
    """Active keys whose backoff window has passed and which don't need
    attention, free keys first, healthiest first."""
    _ensure_table(conn)
    _seed_legacy_key(conn)
    return conn.execute(
        """SELECT id, key_value, is_paid, consecutive_failures
           FROM gemini_keys
           WHERE is_active = 1
             AND needs_attention = 0
             AND (quota_exhausted_until IS NULL
                  OR quota_exhausted_until <= datetime('now'))
           ORDER BY is_paid ASC,
                    last_used_at ASC,
                    consecutive_failures ASC,
                    id ASC"""
    ).fetchall()


def get_candidate_keys() -> list[tuple[int, str]]:
    """Ordered (key_id, key_value) candidates. Empty when nothing usable."""
    with _lock:
        conn = db.connect()
        try:
            rows = _eligible_rows(conn)
            return [(r["id"], r["key_value"]) for r in rows]
        finally:
            conn.close()


def get_key() -> tuple[int, str] | None:
    """Next key in round-robin rotation, or None when pool is exhausted.

    Returns the least-recently-used eligible key (free tier first) and
    marks it used so the next call rotates to a different key. This
    distributes load across all keys instead of hammering the first one.
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


def post_json(url: str, payload: dict, timeout: int = 30) -> dict | None:
    """POST JSON to a Gemini endpoint with transparent key rotation.

    Tries each eligible key in order (free first, healthiest first). On a
    failure the key's health is recorded (with backoff so the next request
    skips it) and the next key is tried immediately. If every eligible key
    is cooling down, falls back to trying all active keys ignoring backoff
    as a last resort. Returns the parsed JSON response, or None when every
    key failed / no keys exist.
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
            continue
        except Exception:
            report_failure(key_id, "other")
            continue
        report_success(key_id)
        return data
    return None


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
