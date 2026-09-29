"""HTTP API — the exact contract the companion app is built against.

POST /api/v1/chat {user_id, message, character_id?}
  -> {reply_text, voice_note_url|null, photo_url|null, messages_remaining, character_id}
GET  /api/v1/characters
  -> {characters:[{id, name, avatar_url}]}  (admin roster, kh_companion only)
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
POST /api/v1/stripe/webhook  (live: Stripe-signature-verified; credits message banks on purchase/renewal)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.request
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import brain, characters, config, db, photos, shows, store, trust, voice

app = FastAPI(title="Chloe Companion API", version="0.3.0-multichar")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ChatIn(BaseModel):
    user_id: str
    message: str
    character_id: str = "chloe"


class UserIdIn(BaseModel):
    user_id: str
    character_id: str = "chloe"


class MemoryWriteIn(BaseModel):
    user_id: str
    mem_type: str
    text: str
    character_id: str = "chloe"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _conn():
    return db.connect()


def _empty_bank_reply(user: dict[str, Any], character: dict[str, Any] | None = None) -> dict[str, Any]:
    # Graceful refusal + upsell nudge. Never crashes, never goes negative.
    # Chloe's exact lines are preserved; other characters get a generic
    # warm version (no upsell wording tied to Chloe's voice).
    char_id = (character or {}).get("id", "chloe")
    char_name = (character or {}).get("name", "Chloe")
    tier = user["tier"]
    if char_id == "chloe":
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
    else:
        if tier == "basic":
            nudge = (f"i'm out of messages for tonight — "
                     f"want more of me? {char_name} plus is $19.99 and i get "
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
        "character_id": char_id,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _startup():
    db.init_db()


@app.get("/health")
def health():
    return {"ok": True, "service": "chloe-companion", "phase": "3-multichar"}


# TEMPORARY DIAGNOSTIC (2026-09-29 debug only — REMOVE after diagnosis).
@app.get("/api/v1/diag/gemini")
def diag_gemini():
    ok, error = brain._gemini_debug()
    return {"gemini_ok": ok, "error": error or None,
            "key_present": bool(brain.config.GEMINI_API_KEY)}


@app.get("/api/v1/characters")
def list_characters():
    """Public roster: companion-enabled characters from the Keyhole admin
    list (kh_companion=true), cached ~5 minutes. Chloe is always first and
    always present (built-in fallback when the Keyhole backend is down)."""
    return {"characters": characters.public_list()}


@app.post("/api/v1/chat")
def chat(body: ChatIn):
    conn = _conn()
    try:
        # Resolve the character (unknown ids fall back to Chloe).
        character = characters.get_character(body.character_id)
        cid = character["id"]

        user = store.get_or_create_user(conn, body.user_id)
        store.log_turn(conn, body.user_id, "user", body.message, cid)

        if not store.consume_message(conn, body.user_id):
            return _empty_bank_reply(user, character)

        # Trust posture: advance the stage clock (at most one stage per
        # check). This NEVER gates warmth — only how much of herself she
        # has volunteered so far.
        trust.maybe_advance(conn, body.user_id)

        # Brain context: 3-layer memory stack (recent turns -> rolling
        # digest -> pinned facts + relevant memories) + trust posture line.
        # Memory is namespaced per character: each one remembers separately.
        context = brain.build_brain_context(conn, body.user_id, user, body.message,
                                            character)

        # Brain: mouth -> auditor (auditor is a pass-through stub in Phase 2).
        reply_text = brain.compose_reply(body.message, context, cid)
        store.log_turn(conn, body.user_id, cid, reply_text, cid)

        # Slower brain path: learn durable typed memories from his message
        # and refresh the rolling digest. Runs after the reply is sent.
        brain.learn_from_turn(conn, body.user_id, body.message, cid)

        # Voice note: stubbed/disabled by default -> null. Uses the
        # character's xAI voice (Chloe: liora).
        voice_note_url = None
        tier_cfg = config.TIERS[user["tier"]]
        if tier_cfg["voice_notes_per_month"] > 0 and len(reply_text) <= 280:
            if store.consume_voice_note(conn, body.user_id):
                voice_note_url = voice.maybe_generate_voice_note(
                    reply_text, body.user_id, character.get("companion_voice") or "liora")

        # Photo drop: tier-gated serving hook. Chloe's curated drops only —
        # other characters get no photos (never Chloe's photos on them).
        photo_url = None
        if cid == "chloe":
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
            "character_id": cid,
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
    plans = [{
        "key": "trial",
        "name": "Companion Trial",
        "price_usd": 0.99,
        "interval": "once",
        "messages_per_month": config.TRIAL_MESSAGES,
        "voice_notes_per_month": 0,
        "voice_notes_unconfirmed": False,
        "photo_drops": False,
        "proactive": False,
        "checkout_url": config.TRIAL_CHECKOUT_URL,
    }]
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
        return {"memories": store.export_memories(conn, body.user_id,
                                                  body.character_id or "chloe")}
    finally:
        conn.close()


@app.post("/api/v1/memory/delete")
def memory_delete(body: UserIdIn):
    conn = _conn()
    try:
        store.delete_all_memories(conn, body.user_id, body.character_id or "chloe")
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
        mid = store.write_memory(conn, body.user_id, body.mem_type, body.text,
                                 body.character_id or "chloe")
        return {"ok": True, "id": mid}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stripe webhook — LIVE purchase fulfillment (2026-09-29).
#
# Verifies the Stripe-Signature header with STRIPE_WEBHOOK_SECRET (HMAC-SHA256,
# 5-minute tolerance). Refuses with 503 until the secret is configured so
# Stripe keeps retrying rather than recording a failure.
#
# Identity: the app appends ?client_reference_id=<user_id> to the Stripe
# Payment Link. checkout.session.completed stores the customer/subscription ->
# user mapping; later invoices and cancellations resolve through it.
#
# Idempotency: every event id is recorded; replays are acknowledged as
# duplicates. Renewals top banks back UP TO the tier allocation (no stacking).
# ---------------------------------------------------------------------------
WEBHOOK_MAX_BYTES = 1_000_000
WEBHOOK_TOLERANCE_S = 300


def _stripe_sig_ok(payload: bytes, header: str, secret: str) -> bool:
    """Stripe's signature scheme: header 't=<ts>,v1=<hex>,...' where hex is
    HMAC-SHA256(secret, '<ts>.' + payload)."""
    try:
        parts: dict[str, list[str]] = {}
        for item in (header or "").split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                parts.setdefault(k.strip(), []).append(v.strip())
        ts = int(parts.get("t", [""])[0])
        if abs(time.time() - ts) > WEBHOOK_TOLERANCE_S:
            return False
        signed = ("%d." % ts).encode() + payload
        expect = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expect, v) for v in parts.get("v1", []))
    except (ValueError, IndexError):
        return False


def _as_id(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        v = value.get("id", "")
        return v if isinstance(v, str) else ""
    return ""


def _rank(tier: str) -> int:
    return config.TIER_RANK.get(tier, 0)


def _grant_keyhole_show(tier: str, email: str, grant_key: str, user_id: str) -> None:
    """Task 3: companion subscription -> free Keyhole private show each billing
    month. Calls the Keyhole backend's internal grant endpoint (shared
    secret). Never blocks message crediting: failures are logged, not raised."""
    minutes = config.SHOW_GRANT_MINUTES.get(tier, 0)
    url = config.KEYHOLE_BACKEND_URL
    secret = config.COMPANION_GRANT_SECRET
    if not (url and secret and minutes and email and grant_key):
        print(f"[stripe] show grant skipped (config/email): tier={tier} user={user_id}",
              flush=True)
        return
    body = json.dumps({"email": email, "minutes": minutes, "grant_key": grant_key}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/internal/companion-show-grant",
        data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Grant-Secret": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[stripe] show grant -> {resp.status} tier={tier} user={user_id}", flush=True)
    except Exception as e:  # noqa: BLE001 — fulfillment must not fail on the grant call
        print(f"[stripe] show grant FAILED tier={tier} user={user_id}: {e}", flush=True)


@app.post("/api/v1/stripe/webhook")
async def stripe_webhook(request: Request):
    secret = config.STRIPE_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(503, "STRIPE_WEBHOOK_SECRET not configured")
    if int(request.headers.get("content-length") or 0) > WEBHOOK_MAX_BYTES:
        raise HTTPException(413, "payload too large")
    raw = await request.body()
    if len(raw) > WEBHOOK_MAX_BYTES:
        raise HTTPException(413, "payload too large")
    if not _stripe_sig_ok(raw, request.headers.get("stripe-signature", ""), secret):
        raise HTTPException(401, "bad stripe signature")
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "bad JSON")
    return _handle_stripe_event(event)


def _handle_stripe_event(event: dict[str, Any]) -> dict[str, Any]:
    eid = event.get("id") or ""
    kind = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    conn = _conn()
    try:
        if eid and not store.note_stripe_event(conn, eid, kind):
            return {"ok": True, "duplicate": True}
        if kind == "checkout.session.completed":
            return _on_checkout_completed(conn, obj)
        if kind == "invoice.paid":
            return _on_invoice_paid(conn, obj, eid)
        if kind == "customer.subscription.deleted":
            return _on_subscription_deleted(conn, obj)
        return {"ok": True, "ignored": kind}
    finally:
        conn.close()


def _on_checkout_completed(conn: Any, obj: dict[str, Any]) -> dict[str, Any]:
    mode = obj.get("mode")
    ref = ((obj.get("client_reference_id") or "").strip()
           or ((obj.get("metadata") or {}).get("user_id") or "").strip())
    if obj.get("payment_status") != "paid":
        return {"ok": True, "ignored": "not paid yet"}
    if mode == "subscription":
        sub = _as_id(obj.get("subscription"))
        cust = _as_id(obj.get("customer"))
        tier = config.STRIPE_LINK_TIERS.get(obj.get("payment_link") or "")
        if not tier:
            return {"ok": True, "ignored": "unknown payment link"}
        if not ref or not sub:
            return {"ok": True, "ignored": "missing client_reference_id or subscription"}
        # Remember the mapping so renewals/cancels resolve without the ref.
        store.link_stripe_customer(conn, cust, sub, ref, tier)
        # Initial top-up; the first invoice.paid (subscription_create) grants
        # the monthly Keyhole show via the same grant key scheme.
        store.set_tier(conn, ref, tier)
        return {"ok": True, "user_id": ref, "tier": tier, "action": "subscribed"}
    if mode == "payment":
        # One-time $0.99 trial -> 100 messages, one per customer (no tier change).
        if (obj.get("payment_link") or "") == config.TRIAL_LINK_ID and ref:
            granted = store.grant_trial_messages(conn, ref, config.TRIAL_MESSAGES)
            return {"ok": True, "user_id": ref,
                    "action": "trial_granted" if granted else "trial_already_used"}
        return {"ok": True, "ignored": "unknown one-time link"}
    return {"ok": True, "ignored": f"mode {mode}"}


def _on_invoice_paid(conn: Any, obj: dict[str, Any], eid: str) -> dict[str, Any]:
    sub = _as_id(obj.get("subscription"))
    if not sub:
        sub = _as_id(((obj.get("parent") or {}).get("subscription_details") or {}))
    if not sub:
        return {"ok": True, "ignored": "no subscription"}
    lines = (obj.get("lines") or {}).get("data") or []
    tier = ""
    for li in lines:
        price = ((li.get("price") or {}).get("id")
                 or (((li.get("pricing") or {}).get("price_details") or {}).get("price")) or "")
        t = config.STRIPE_PRICE_TIERS.get(price, "")
        if t and _rank(t) > _rank(tier):
            tier = t
    if not tier:
        return {"ok": True, "ignored": "unknown price"}
    user_id = store.user_for_stripe_subscription(conn, sub)
    if not user_id:
        user_id = store.user_for_stripe_customer(conn, _as_id(obj.get("customer")))
    if not user_id:
        return {"ok": True, "ignored": "unknown customer mapping"}
    # Renewal (and the initial subscription_create invoice): top banks back UP
    # TO the tier allocation — never stack.
    store.set_tier(conn, user_id, tier)
    billing_reason = obj.get("billing_reason") or ""
    if billing_reason in ("subscription_create", "subscription_cycle", "subscription_update"):
        period_end = 0
        if lines:
            period_end = int((lines[0].get("period") or {}).get("end") or 0)
        email = (obj.get("customer_email") or "").strip()
        _grant_keyhole_show(tier, email, f"companion-show:{sub}:{period_end or eid}", user_id)
    return {"ok": True, "user_id": user_id, "tier": tier, "action": "invoice_topup"}


def _on_subscription_deleted(conn: Any, obj: dict[str, Any]) -> dict[str, Any]:
    sub = _as_id(obj.get("id"))
    if not sub:
        return {"ok": True, "ignored": "no subscription id"}
    user_id = store.user_for_stripe_subscription(conn, sub)
    if not user_id:
        return {"ok": True, "ignored": "unknown subscription"}
    # Cancel: tier back to the default; remaining banks drain naturally.
    store.downgrade_to_basic(conn, user_id)
    return {"ok": True, "user_id": user_id, "action": "downgraded_to_basic"}


