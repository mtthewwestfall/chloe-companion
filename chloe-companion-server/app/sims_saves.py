"""Cloud saves for the God's Tank game (2026-10-13).

POST /api/sims/save — body {"player_id": <uuid>, "save": {...}} stores
    (or overwrites) that player's save; answers {"ok": true,
    "updated_at": <iso ts>}.
GET  /api/sims/save?player_id=<uuid> — answers {"save": {...},
    "updated_at": <iso ts>}, or 404 {"detail": "no_save"}.

The game client generates a UUID per player and keeps it locally; the
UUID is the only identity — there are no accounts here. Saves live in a
sims_saves table in the app's existing SQLite DB (via app/db.py's
connect(), i.e. config.DB_PATH); the table is created lazily by this
module so db.py's schema stays untouched.

Gates mirror POST /api/sims/think (app/sims.py), all independent:
  * Token: header X-Sims-Token must equal env SIMS_THINK_TOKEN
    (fails closed when the env var is unset).
  * Origin: when an Origin or Referer header is present it must be the
    game origin (https://godstank.lockeddoor.ai); headerless calls pass
    on the token alone.
  * Rate: 120 requests/minute per IP, in-memory.
  * Size: the serialized save object is capped at 256 KB (413).

CORS: this route alone answers cross-origin calls from anywhere
(Access-Control-Allow-Origin: *) — see SimsSavesCorsMiddleware,
registered after the app's global CORSMiddleware so it is outermost for
this path. The token is the real gate.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import db

_GAME_ORIGIN = "https://godstank.lockeddoor.ai"
_PATH = "/api/sims/save"
_MAX_SAVE_BYTES = 256 * 1024
# The request envelope (player_id + JSON syntax) rides on top of the
# save itself; reject absurd bodies before parsing.
_MAX_BODY_BYTES = _MAX_SAVE_BYTES + 64 * 1024
_RATE_LIMIT = 120
_RATE_WINDOW_S = 60.0
_PLAYER_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sims_saves (
    player_id  TEXT PRIMARY KEY,
    save_json  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_rate_lock = threading.Lock()
_rate_hits: dict[str, deque] = {}


def _rate_ok(ip: str) -> bool:
    """Sliding-window per-IP cap (120/min). In-memory, per-process."""
    now = time.monotonic()
    with _rate_lock:
        dq = _rate_hits.setdefault(ip, deque())
        while dq and now - dq[0] > _RATE_WINDOW_S:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT:
            return False
        dq.append(now)
        return True


def _check_gates(request: Request) -> None:
    """Token, then origin, then rate — same shape as sims.py."""
    expected = os.environ.get("SIMS_THINK_TOKEN", "").strip()
    given = request.headers.get("x-sims-token", "")
    if not expected or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="bad_token")
    origin = (request.headers.get("origin")
              or request.headers.get("referer") or "")
    if origin and not (origin == _GAME_ORIGIN
                       or origin.startswith(_GAME_ORIGIN + "/")):
        raise HTTPException(status_code=403, detail="bad_origin")
    ip = request.client.host if request.client else "unknown"
    if not _rate_ok(ip):
        raise HTTPException(status_code=429, detail="rate_limited")


def _valid_player_id(value) -> str:
    """Return the normalized (lowercase) id, or raise 400."""
    if (not isinstance(value, str) or len(value) != 36
            or not _PLAYER_ID_RE.match(value)):
        raise HTTPException(status_code=400, detail="bad_player_id")
    return value.lower()


def _connect():
    conn = db.connect()
    conn.execute(_SCHEMA)
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SimsSavesCorsMiddleware:
    """Path-scoped CORS for /api/sims/save only.

    Registered after the global CORSMiddleware in main.py, which makes
    it the outermost middleware for this path: it answers preflights
    itself (the global policy would reject the game's origin) and
    stamps Access-Control-Allow-Origin: * on every response for the
    path, including error responses produced downstream.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != _PATH:
            return await self.app(scope, receive, send)
        if scope.get("method") == "OPTIONS":
            headers = {
                "access-control-allow-origin": "*",
                "access-control-allow-methods": "GET, POST, OPTIONS",
                "access-control-allow-headers": "Content-Type, X-Sims-Token",
                "access-control-max-age": "86400",
                "content-length": "0",
            }
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(k.encode(), v.encode())
                                    for k, v in headers.items()]})
            await send({"type": "http.response.body", "body": b""})
            return

        async def _send_with_cors(message):
            if message.get("type") == "http.response.start":
                hdrs = [(k, v) for k, v in message.get("headers", [])
                        if k.lower() != b"access-control-allow-origin"]
                hdrs.append((b"access-control-allow-origin", b"*"))
                message = dict(message, headers=hdrs)
            await send(message)

        return await self.app(scope, receive, _send_with_cors)


def register(app) -> None:
    """Attach POST + GET /api/sims/save to the FastAPI app."""

    @app.post(_PATH)
    async def sims_save_post(request: Request):
        raw = await request.body()
        if len(raw) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="body_too_large")
        _check_gates(request)
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="bad_body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="bad_body")
        player_id = _valid_player_id(body.get("player_id"))
        save = body.get("save")
        if not isinstance(save, dict):
            raise HTTPException(status_code=400, detail="bad_save")
        serialized = json.dumps(save)
        if len(serialized.encode("utf-8")) > _MAX_SAVE_BYTES:
            raise HTTPException(status_code=413, detail="save_too_large")
        updated_at = _now_iso()
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO sims_saves (player_id, save_json, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(player_id) DO UPDATE SET"
                " save_json = excluded.save_json,"
                " updated_at = excluded.updated_at",
                (player_id, serialized, updated_at))
            conn.commit()
        finally:
            conn.close()
        return JSONResponse({"ok": True, "updated_at": updated_at})

    @app.get(_PATH)
    async def sims_save_get(request: Request):
        _check_gates(request)
        player_id = _valid_player_id(request.query_params.get("player_id"))
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT save_json, updated_at FROM sims_saves"
                " WHERE player_id = ?", (player_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="no_save")
        return JSONResponse({"save": json.loads(row["save_json"]),
                             "updated_at": row["updated_at"]})
