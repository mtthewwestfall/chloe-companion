"""Web Push subscription API routes for the Whispr PWA (2026-10-16).

Registered from app/main.py via register(app, _current_user_id) so the
large main module only needs a one-line hook. Endpoints:

  GET    /api/v1/push/vapid-public-key  -> {"public_key": ...} (public)
  POST   /api/v1/push/subscribe         -> save subscription (auth)
  DELETE /api/v1/push/subscribe         -> remove subscription (auth)

Subscriptions are persisted by app/push.py and fanned out to by the
proactive scheduler's WebPushTransport.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from pydantic import BaseModel

from . import push as _push


class PushSubscribeIn(BaseModel):
    subscription: dict


class PushUnsubscribeIn(BaseModel):
    endpoint: str


def register(app, current_user_id) -> None:
    """Attach the push subscription routes to the FastAPI app."""

    @app.get("/api/v1/push/vapid-public-key")
    def vapid_public_key():
        return {"public_key": _push.vapid_public_key()}

    @app.post("/api/v1/push/subscribe")
    def push_subscribe(body: PushSubscribeIn, request: Request):
        user_id = current_user_id(request)
        try:
            _push.save_subscription(user_id, body.subscription)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True}

    @app.delete("/api/v1/push/subscribe")
    def push_unsubscribe(body: PushUnsubscribeIn, request: Request):
        user_id = current_user_id(request)
        removed = _push.remove_subscription(user_id, body.endpoint)
        return {"ok": True, "removed": removed}
