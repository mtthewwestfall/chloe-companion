"""AI line generator for the owner's Sims game (2026-10-13).

POST /api/sims/think — the browser game (served from its own service at
https://godstown.lockeddoor.ai) sends one sim's situation; this returns ONE
short in-character line (dialogue or thought) produced through the app's
existing Gemini key manager (app/gemini_keys.py — free keys first, same
path brain.py uses, fast lite model). The prompt is deliberately tiny and
the game calls sparingly, to protect the shared free key pool.

Failure posture: on ANY model failure the endpoint still answers HTTP 200
with a generic in-kind fallback line — the game must never break because
of this endpoint. Failures are logged server-side.

Gates (all independent):
  * Token: header X-Sims-Token must equal env SIMS_THINK_TOKEN.
  * Origin: when an Origin or Referer header is present it must be the
    game origin (browser calls always carry it); headerless calls
    (curl, health checks) pass on the token alone.
  * Rate: 60 requests/minute per IP, in-memory.
  * Body: at most 4 KB.

CORS: this route alone answers cross-origin calls from anywhere
(Access-Control-Allow-Origin: *) — see SimsCorsMiddleware, which is
registered after the app's global CORSMiddleware so it is outermost for
this path and can answer preflights the global policy would reject.
The token is the real gate.
"""
from __future__ import annotations

import hmac
import json
import os
import random
import threading
import time
from collections import deque
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from . import gemini_keys

_GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               "gemini-3.1-flash-lite:generateContent")
_GAME_ORIGIN = "https://godstown.lockeddoor.ai"
_PATH = "/api/sims/think"
_MAX_BODY = 4096
_RATE_LIMIT = 60
_RATE_WINDOW_S = 60.0
_KINDS = ("chat", "thought", "greeting", "goodbye", "argue")

# Generic lines per kind, used whenever the model path fails for any
# reason. First-person, situation-neutral, safe for any sim.
_FALLBACKS: dict[str, list[str]] = {
    "chat": [
        "So, how has your day been treating you?",
        "This place has a certain charm, doesn't it?",
        "I keep meaning to catch up properly — today's the day.",
    ],
    "thought": [
        "Okay, one thing at a time.",
        "What do I actually need right now?",
        "Deep breath. Figure it out as I go.",
    ],
    "greeting": [
        "Hey! Good to see you.",
        "Oh, hi — come on in.",
        "Well, look who it is.",
    ],
    "goodbye": [
        "See you around, then.",
        "Alright, I'm off — take care.",
        "Good talking with you. Truly.",
    ],
    "argue": [
        "Oh, sure, because that makes total sense.",
        "You always do this, you know that?",
        "Fine. Whatever you say.",
    ],
}

_SYSTEM_TEXT = (
    "You voice one character in a life-simulation game. Given the sim's "
    "traits, needs, situation and memories, produce exactly ONE short "
    "in-character line — spoken dialogue or an inner thought, as "
    "requested. At most 22 words. Plain text only: no quotation marks, "
    "no stage directions, no name prefix, no emoji."
)

_rate_lock = threading.Lock()
_rate_hits: dict[str, deque] = {}


def _rate_ok(ip: str) -> bool:
    """Sliding-window per-IP cap (60/min). In-memory, per-process."""
    now = time.monotonic()
    with _rate_lock:
        dq = _rate_hits.setdefault(ip, deque())
        while dq and now - dq[0] > _RATE_WINDOW_S:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT:
            return False
        dq.append(now)
        return True


def _fallback(kind: str) -> str:
    return random.choice(_FALLBACKS.get(kind, _FALLBACKS["chat"]))


def _clean_line(text: str, name: str = "") -> str:
    """One line, no surrounding quotes, no name prefix, <= 22 words."""
    t = (text or "").strip().strip('"“”\'').strip()
    t = t.split("\n")[0].strip().strip('"“”\'').strip()
    if name and t.lower().startswith(name.lower() + ":"):
        t = t[len(name) + 1:].strip()
    words = t.split()
    if len(words) > 22:
        t = " ".join(words[:22])
    return t[:200].strip()


def _short(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _build_payload(body: dict) -> tuple[dict, str, str]:
    """(gemini payload, sim name, kind) from a lenient-parsed request."""
    name = _short(body.get("name"), 40) or "A sim"
    kind = str(body.get("kind") or "chat").strip().lower()
    if kind not in _KINDS:
        kind = "chat"
    traits = body.get("traits") or []
    if isinstance(traits, list):
        traits_txt = ", ".join(_short(t, 24) for t in traits[:6] if str(t).strip())
    else:
        traits_txt = _short(traits, 80)
    needs = body.get("needs") or {}
    if not isinstance(needs, dict):
        needs = {}

    def _need(key: str) -> int:
        try:
            return max(0, min(100, int(needs.get(key, 50))))
        except (TypeError, ValueError):
            return 50

    situation = _short(body.get("situation"), 200) or "hanging out at home"
    partner = _short(body.get("partner"), 40)
    memories = body.get("memories") or []
    if isinstance(memories, list):
        mem_txt = "; ".join(_short(m, 80) for m in memories[:5] if str(m).strip())
    else:
        mem_txt = _short(memories, 200)
    user_text = (
        f"Sim: {name}\n"
        f"Traits: {traits_txt or 'none specified'}\n"
        f"Needs, 0-100 (low is bad): hunger {_need('hunger')}, "
        f"energy {_need('energy')}, fun {_need('fun')}, "
        f"bladder {_need('bladder')}, social {_need('social')}\n"
        f"Situation: {situation}\n"
        f"With: {partner or 'alone'}\n"
        f"Recent memories: {mem_txt or 'none'}\n"
        f"Write the {kind} line now."
    )
    payload = {
        "system_instruction": {"parts": [{"text": _SYSTEM_TEXT}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 60},
    }
    return payload, name, kind


def _generate_line(body: dict) -> str:
    """Model line via the shared key manager; fallback on ANY failure."""
    payload, name, kind = _build_payload(body)
    try:
        data = gemini_keys.post_json(_GEMINI_URL, payload, timeout=15,
                                     source="sims-think")
    except Exception as e:  # post_json shouldn't raise, but never break
        print(f"[sims] think model call raised: {e}")
        data = None
    if data:
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts
                           if p.get("text")).strip()
            line = _clean_line(text, name)
            if line:
                return line
            print("[sims] think model returned empty text; using fallback")
        except (KeyError, IndexError, TypeError) as e:
            print(f"[sims] think model response unparseable: {e}")
    else:
        print("[sims] think model unavailable (key pool); using fallback")
    return _fallback(kind)


class SimsCorsMiddleware:
    """Path-scoped CORS for /api/sims/think only.

    Registered after the global CORSMiddleware in main.py, which makes it
    the outermost middleware: it answers preflights for this path itself
    (the global policy would reject the game's origin) and stamps
    Access-Control-Allow-Origin: * on every response for the path,
    including error responses produced downstream.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != _PATH:
            return await self.app(scope, receive, send)
        if scope.get("method") == "OPTIONS":
            headers = {
                "access-control-allow-origin": "*",
                "access-control-allow-methods": "POST, OPTIONS",
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
    """Attach POST /api/sims/think to the FastAPI app."""

    @app.post(_PATH)
    async def sims_think(request: Request):
        raw = await request.body()
        if len(raw) > _MAX_BODY:
            raise HTTPException(status_code=413, detail="body_too_large")
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
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        line = _generate_line(body)
        return JSONResponse({"line": line})
