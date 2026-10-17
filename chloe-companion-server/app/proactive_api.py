"""Proactive messaging API routes (2026-10-16, feat/proactive-live).

Registered from main.py via register_proactive_routes(app, ...). The
scheduler is started lazily (idempotent) on the first proactive API
call, so no main._startup changes are needed.

Endpoints:
  GET  /api/v1/proactive/pending        — queued website messages
  POST /api/v1/proactive/ack            — mark rendered (writes history)
  GET  /api/v1/proactive/config         — VAPID public key for push
  POST /api/v1/proactive/push-subscribe  — save a Web Push subscription
  GET  /api/v1/proactive/preference     — read the in-app toggle
  POST /api/v1/proactive/preference     — set the in-app toggle
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from pydantic import BaseModel

from . import characters, config, proactive, proactive_scheduler


class ProactiveAckIn(BaseModel):
    ids: list[int] = []


class PushSubscribeIn(BaseModel):
    endpoint: str = ""
    p256dh: str = ""
    auth: str = ""


class ProactivePreferenceIn(BaseModel):
    enabled: bool = True


def _ensure_scheduler() -> None:
    try:
        proactive_scheduler.start_scheduler()
    except Exception:
        pass


def register_proactive_routes(app, current_user_id_fn, conn_fn) -> None:
    """Mount the six proactive endpoints on the FastAPI app."""
    _ensure_scheduler()

    @app.get("/api/v1/proactive/pending")
    async def proactive_pending(request: Request):
        _ensure_scheduler()
        user_id = current_user_id_fn(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="not signed in")
        conn = conn_fn()
        try:
            msgs = proactive.get_pending_messages(conn, user_id)
        finally:
            conn.close()
        out = []
        for m in msgs:
            ch = characters.get_character(m.get("character_id") or "chloe",
                                          user_id)
            out.append({
                "id": m["id"],
                "character_id": m.get("character_id") or "chloe",
                "character_name": ch.get("name") or "Chloe",
                "message_type": m.get("message_type") or "nudge",
                "text": m.get("text") or "",
                "created_at": m.get("created_at") or "",
            })
        return {"messages": out}

    @app.post("/api/v1/proactive/ack")
    async def proactive_ack(body: ProactiveAckIn, request: Request):
        user_id = current_user_id_fn(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="not signed in")
        conn = conn_fn()
        try:
            n = proactive.ack_messages(conn, user_id, body.ids)
        finally:
            conn.close()
        return {"acked": n}

    @app.get("/api/v1/proactive/config")
    async def proactive_config(request: Request):
        user_id = current_user_id_fn(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="not signed in")
        return {"vapid_public_key": config.VAPID_PUBLIC_KEY or ""}

    @app.post("/api/v1/proactive/push-subscribe")
    async def proactive_push_subscribe(body: PushSubscribeIn,
                                        request: Request):
        user_id = current_user_id_fn(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="not signed in")
        conn = conn_fn()
        try:
            ok = proactive.save_push_subscription(
                conn, user_id, body.endpoint, body.p256dh, body.auth)
        finally:
            conn.close()
        return {"ok": ok}

    @app.get("/api/v1/proactive/preference")
    async def proactive_preference_get(request: Request):
        user_id = current_user_id_fn(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="not signed in")
        conn = conn_fn()
        try:
            enabled = proactive.get_proactive_preference(conn, user_id)
        finally:
            conn.close()
        return {"enabled": enabled}

    @app.post("/api/v1/proactive/preference")
    async def proactive_preference_set(body: ProactivePreferenceIn,
                                       request: Request):
        user_id = current_user_id_fn(request)
        if not user_id:
            raise HTTPException(status_code=401, detail="not signed in")
        conn = conn_fn()
        try:
            proactive.set_proactive_preference(conn, user_id, body.enabled)
        finally:
            conn.close()
        return {"enabled": body.enabled}
