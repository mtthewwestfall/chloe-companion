"""HTTP API — the exact contract the companion app is built against.

POST /api/v1/chat {user_id, message}
  -> {reply_text, voice_note_url|null, photo_url|null, messages_remaining}
GET  /api/v1/me?user_id=
  -> {user_id, name, tier, messages_remaining, voice_notes_remaining, proactive_enabled}
GET  /api/v1/tiers
  -> {plans:[{key,name,price_usd,interval,messages_per_month,
              voice_notes_per_month,voice_notes_unconfirmed,
              photo_drops,proactive,checkout_url}]}
GET  /api/v1/shows
  -> {shows:[{id,title,starts_at,ticket_url}]}
POST /api/v1/memory/export {user_id} -> {memories:[...]}
POST /api/v1/memory/delete {user_id} -> {ok:true}
POST /api/v1/stripe/webhook  (STUB — test events only, no live keys)
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import brain, config, db, photos, shows, store, trust, voice

app = FastAPI(title="Chloe Companion API", version="0.2.0-phase2")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ChatIn(BaseModel):
    user_id: str
    message: str


class UserIdIn(BaseModel):
    user_id: str


class MemoryWriteIn(BaseModel):
    user_id: str
    mem_type: str
    text: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _conn():
    return db.connect()


def _empty_bank_reply(user: dict[str, Any]) -> dict[str, Any]:
    # Graceful refusal + upsell nudge. Never crashes, never goes negative.
    tier = user["tier"]
    if tier == "basic":
        nudge = ("i'm out of messages for tonight, babe — "
                 "want me to stay up with you? plus is $19.99 and i get "
                 "voice notes too.")
    elif tier == "plus":
        nudge = ("i've used up this month's messages, love — "
                 "vip is $29.99 with way more of me, if you want.")
    else:
        nudge = ("i've used up this month's messages — they'll top back up "
                 "on your renewal. i'll be right here.")
    return {
        "reply_text": nudge,
        "voice_note_url": None,
        "photo_url": None,
        "messages_remaining": 0,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _startup():
    db.init_db()


@app.get("/health")
def health():
    return {"ok": True, "service": "chloe-companion", "phase": "2-scaffold"}


@app.post("/api/v1/chat")
def chat(body: ChatIn):
    conn = _conn()
    try:
        user = store.get_or_create_user(conn, body.user_id)
        store.log_turn(conn, body.user_id, "user", body.message)

        if not store.consume_message(conn, body.user_id):
            return _empty_bank_reply(user)

        # Trust posture: advance the stage clock (at most one stage per
        # check). This NEVER gates warmth — only how much of herself she
        # has volunteered so far.
        trust.maybe_advance(conn, body.user_id)

        # Brain context: 3-layer memory stack (recent turns -> rolling
        # digest -> pinned facts + relevant memories) + trust posture line.
        context = brain.build_brain_context(conn, body.user_id, user, body.message)

        # Brain: mouth -> auditor (auditor is a pass-through stub in Phase 2).
        reply_text = brain.compose_reply(body.message, context)
        store.log_turn(conn, body.user_id, "chloe", reply_text)

        # Slower brain path: learn durable typed memories from his message
        # and refresh the rolling digest. Runs after the reply is sent.
        brain.learn_from_turn(conn, body.user_id, body.message)

        # Voice note: stubbed/disabled by default -> null.
        voice_note_url = None
        tier_cfg = config.TIERS[user["tier"]]
        if tier_cfg["voice_notes_per_month"] > 0 and len(reply_text) <= 280:
            if store.consume_voice_note(conn, body.user_id):
                voice_note_url = voice.maybe_generate_voice_note(reply_text, body.user_id)

        # Photo drop: tier-gated serving hook.
        photo_url = None
        drop = photos.maybe_drop_photo(user["tier"], user["messages_used_total"] + 1)
        if drop and not drop.get("placeholder"):
            photo_url = f"/photo_drops/{drop['file']}"
        elif drop:
            photo_url = f"PLACEHOLDER:{drop['id']}"

        user = store.get_or_create_user(conn, body.user_id)
        return {
            "reply_text": reply_text,
            "voice_note_url": voice_note_url,
            "photo_url": photo_url,
            "messages_remaining": user["messages_remaining"],
        }
    finally:
        conn.close()


@app.get("/api/v1/me")
def me(user_id: str):
    conn = _conn()
    try:
        user = store.get_or_create_user(conn, user_id)
        return {
            "user_id": user["user_id"],
            "name": user["name"],
            "tier": user["tier"],
            "messages_remaining": user["messages_remaining"],
            "voice_notes_remaining": user["voice_notes_remaining"],
            "proactive_enabled": bool(user["proactive_enabled"]),
        }
    finally:
        conn.close()


@app.get("/api/v1/shows")
def list_shows():
    return {"shows": shows.get_shows()}


@app.get("/api/v1/tiers")
def list_tiers():
    """Public pricing: tier names, prices, benefits, and Stripe checkout links.

    Links are the user-supplied live Stripe Payment Links (see
    config.STRIPE_LIVE_CHECKOUT) — they open in a new tab and redirect to
    Stripe for the actual purchase. NOTE: the /api/v1/stripe/webhook stub has
    no signature verification; a checkout redirect is NOT fulfillment on our
    side.
    """
    plans = []
    for key in ("basic", "plus", "vip"):
        t = config.TIERS[key]
        plans.append({
            "key": key,
            "name": t["name"],
            "price_usd": t["price_usd"],
            "interval": "month",
            "messages_per_month": t["messages_per_month"],
            "voice_notes_per_month": t["voice_notes_per_month"],
            # All voice-note counts confirmed by user 2026-09-29; the field is
            # kept (always false) so existing clients don't break.
            "voice_notes_unconfirmed": False,
            "photo_drops": t["photo_drops"],
            "proactive": t["proactive"],
            "checkout_url": config.STRIPE_LIVE_CHECKOUT[key],
        })
    return {"plans": plans}


@app.post("/api/v1/memory/export")
def memory_export(body: UserIdIn):
    conn = _conn()
    try:
        return {"memories": store.export_memories(conn, body.user_id)}
    finally:
        conn.close()


@app.post("/api/v1/memory/delete")
def memory_delete(body: UserIdIn):
    conn = _conn()
    try:
        store.delete_all_memories(conn, body.user_id)
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/v1/memory/write")
def memory_write(body: MemoryWriteIn):
    """Companion-internal endpoint: the brain (Phase 3) writes typed memories
    it learns mid-conversation. Typed per Thoughtspace: core/fact/preference/
    boundary/tone."""
    if body.mem_type not in store.MEM_TYPES:
        raise HTTPException(400, f"mem_type must be one of {store.MEM_TYPES}")
    conn = _conn()
    try:
        mid = store.write_memory(conn, body.user_id, body.mem_type, body.text)
        return {"ok": True, "id": mid}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stripe webhook — STUB. Test events only. No live keys, no real charges, ever.
# Handles: subscription created / renewed / canceled.
# Renewal tops the banks back UP TO the tier allocation (no stacking).
# ---------------------------------------------------------------------------
@app.post("/api/v1/stripe/webhook")
async def stripe_webhook(request: Request):
    # NOTE: no signature verification in the stub. Phase 3 must verify the
    # Stripe-Signature header before trusting any event.
    try:
        event = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")
    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {}) or {}
    user_id = obj.get("metadata", {}).get("user_id") or obj.get("client_reference_id")
    price_id = (obj.get("items", {}).get("data", [{}])[0].get("price", {}) or {}).get("id")

    tier = _tier_for_price(price_id)
    conn = _conn()
    try:
        if etype in ("customer.subscription.created", "customer.subscription.updated"):
            if user_id and tier:
                # Renewal: top up TO allocation, never stack.
                store.set_tier(conn, user_id, tier)
                return {"ok": True, "action": f"topped_up:{tier}"}
            return {"ok": False, "reason": "missing user_id or unknown price_id (stub)"}
        if etype == "customer.subscription.deleted":
            if user_id:
                # Cancel: keep access to end of period in real Stripe; here we
                # just note it. Banks are NOT zeroed by the stub.
                return {"ok": True, "action": "canceled_noted"}
            return {"ok": False, "reason": "missing user_id (stub)"}
        return {"ok": True, "action": "ignored"}
    finally:
        conn.close()


def _tier_for_price(price_id: str | None) -> str | None:
    for tier, p in config.STRIPE_TEST_PLACEHOLDERS["products"].items():
        if p["price_id"] == price_id:
            return tier
    return None
