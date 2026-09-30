"""HTTP API — the exact contract the companion app is built against.

AUTH (2026-09-30): email login is REQUIRED — no anonymous/free use. Every
endpoint below marked [auth] needs `Authorization: Bearer <session token>`.
POST /api/v1/auth/signup {email, password, name?} -> {ok, verify_sent}
POST /api/v1/auth/login {email, password} -> {ok, token, user_id, name, tier}
POST /api/v1/auth/logout [auth] -> {ok}
GET  /api/v1/auth/me [auth] -> {user_id, name, tier, messages_remaining, ...}
POST /api/v1/auth/name [auth] {name} -> {ok, name}
GET  /api/v1/auth/verify?token= -> 303 redirect to the app with #session=
  (first 200 verified signups also get 50 free messages, once)
POST /api/v1/auth/resend-verification {email} -> {ok}
GET  /api/v1/free-spots -> {cap, spots_left, messages} (public: powers the
  "100 free messages" promo on the signup/signin screen)

POST /api/v1/chat [auth] {message, character_id?}
  -> {reply_text, voice_note_url|null, photo_url|null, messages_remaining, character_id}
GET  /api/v1/chat/history [auth] ?character_id=&limit=
  -> {turns:[{role, text, created_at}]}  (oldest first; restores the saved
     conversation so logging back in picks up where you left off)
GET  /api/v1/characters
  -> {characters:[{id, name, avatar_url}]}  (admin roster, kh_companion only)
GET  /api/v1/me?user_id=  [auth — legacy; user_id param ignored, token wins]
  -> {user_id, name, tier, messages_remaining, voice_notes_remaining, proactive_enabled}
GET  /api/v1/tiers
  -> {plans:[{key,name,price_usd,interval,messages_per_month,
              voice_notes_per_month,voice_notes_unconfirmed,
              photo_drops,proactive,checkout_url}]}
GET  /api/v1/shows
  -> {shows:[{id,title,starts_at,ticket_url}]}
POST /api/v1/memory/export [auth] {character_id?} -> {memories:[...]}
POST /api/v1/memory/delete [auth] {character_id?} -> {ok:true}
POST /api/v1/stripe/webhook  (live: Stripe-signature-verified; credits message banks on purchase/renewal)
POST /api/v1/jvzoo/ipn  (live: JVZIPN-v2-verified; fulfills tiers for JVZoo affiliate sales — tiers only)
"""
from __future__ import annotations

import hashlib
import hmac
import hmac
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from . import auth, brain, characters, config, db, photos, shows, store, trust, voice

app = FastAPI(title="Chloe Companion API", version="0.4.0-auth")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ChatIn(BaseModel):
    user_id: str = ""  # legacy: ignored — the Bearer token identifies the user
    message: str
    character_id: str = "chloe"


class UserIdIn(BaseModel):
    user_id: str = ""  # legacy: ignored — the Bearer token identifies the user
    character_id: str = "chloe"


class MemoryWriteIn(BaseModel):
    user_id: str = ""  # legacy: ignored — the Bearer token identifies the user
    mem_type: str
    text: str
    character_id: str = "chloe"


class SignupIn(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


class ResendIn(BaseModel):
    email: str


class NameIn(BaseModel):
    name: str


class GirlIn(BaseModel):
    id: str = ""          # optional slug; derived from name when blank
    name: str
    avatar_url: str = ""
    persona: str = ""
    companion_voice: str = "liora"
    blurb: str = ""
    door_title: str = ""
    sort_order: int = 0
    enabled: bool = True


class GirlUpdate(BaseModel):
    name: str | None = None
    avatar_url: str | None = None
    persona: str | None = None
    companion_voice: str | None = None
    blurb: str | None = None
    door_title: str | None = None
    sort_order: int | None = None
    enabled: bool | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _conn():
    return db.connect()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?"))


def _current_user_id(request: Request) -> str:
    """Resolve the Bearer session token to a user_id. 401 when missing or
    unknown — this is what keeps the app login-gated (no free use)."""
    token = auth.bearer_token(request.headers.get("authorization"))
    if not expected:
        raise HTTPException(status_code=401, detail="login_required")
    conn = _conn()
    try:
        user_id = store.get_session_user_id(conn, token)
    finally:
        conn.close()
    if not user_id:
        raise HTTPException(status_code=401, detail="login_required")
    return user_id


def _user_summary(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": user["user_id"],
        "name": user["name"],
        "email": user.get("email"),
        "tier": user["tier"],
        "messages_remaining": user["messages_remaining"],
        "voice_notes_remaining": user["voice_notes_remaining"],
        "proactive_enabled": bool(user["proactive_enabled"]),
    }


def _send_verify_email(conn: Any, user: dict[str, Any]) -> None:
    """Create a fresh verification token and email it. Raises HTTPException
    502 when email can't be sent (user can retry via resend)."""
    if not config.RESEND_API_KEY:
        print(f"[auth] RESEND_API_KEY unset; cannot email {user.get('email')}",
              flush=True)
        raise HTTPException(status_code=502, detail="email_unavailable")
    token = auth.new_token()
    store.create_email_verification(conn, user["user_id"], token,
                                    config.VERIFY_TTL_HOURS)
    verify_url = (f"{config.PUBLIC_APP_URL}/api/v1/auth/verify"
                  f"?token={token}")
    try:
        auth.send_verification_email(
            user["email"], user["name"] or "there", verify_url,
            config.RESEND_API_KEY, config.MAIL_FROM)
    except RuntimeError as e:
        print(f"[auth] verification email failed for {user.get('email')}: {e}",
              flush=True)
        raise HTTPException(status_code=502, detail="email_failed")


def _empty_bank_reply(user: dict[str, Any], character: dict[str, Any] | None = None,
                      unpaid: bool = False) -> dict[str, Any]:
    # Graceful refusal + upsell nudge. Never crashes, never goes negative.
    # Chloe's exact lines are preserved; other characters get a generic
    # warm version (no upsell wording tied to Chloe's voice).
    # `unpaid` = this account has never paid anything (no subscription, no
    # trial) — point at the $0.99 trial / $9.99 plan, never the Plus upgrade.
    char_id = (character or {}).get("id", "chloe")
    char_name = (character or {}).get("name", "Chloe")
    tier = user["tier"]
    if char_id == "chloe":
        if unpaid:
            nudge = ("i'd love to keep talking, babe — but my message jar's "
                     "empty. try me for $0.99, or grab the $9.99 plan and "
                     "i'm yours all month.")
        elif tier == "basic":
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
        if unpaid:
            nudge = (f"i'd love to keep talking — but i'm out of messages. "
                     f"try the $0.99 trial, or the $9.99 plan for a whole "
                     f"month of me.")
        elif tier == "basic":
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
@app.get("/api/v1/characters")
def list_characters():
    """Public roster: companion-enabled characters from the Keyhole admin
    list (kh_companion=true), cached ~5 minutes. Chloe is always first and
    always present (built-in fallback when the Keyhole backend is down)."""
    return {"characters": characters.public_list()}


# ---------------------------------------------------------------------------
# Admin panel (2026-09-30): /admin.html "Accounts" + "Girls" tabs.
# Guarded by the ADMIN_TOKEN env var via the X-Admin-Token header.
# ---------------------------------------------------------------------------
def _require_admin(request: Request) -> None:
    expected = config.ADMIN_TOKEN
    if not expected:
        raise HTTPException(status_code=503, detail="admin_disabled")
    provided = request.headers.get("x-admin-token", "")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "girl"


def _admin_accounts() -> dict:
    conn = _conn()
    try:
        return {"stats": store.account_stats(conn),
                "accounts": store.list_accounts(conn)}
    finally:
        conn.close()


def _admin_girls() -> dict:
    remote = characters.get_characters()
    conn = _conn()
    try:
        local = store.list_local_characters(conn)
    finally:
        conn.close()
    local_ids = {c["id"] for c in local}
    girls = []
    for c in remote:
        girls.append({**{k: c.get(k) for k in
                         ("id", "name", "avatar_url", "persona",
                          "companion_voice", "blurb", "door_title",
                          "sort_order")},
                      "source": "local" if c["id"] in local_ids else
                                ("builtin" if c["id"] == "chloe" else "keyhole")})
    return {"girls": girls}


def _admin_create_girl(data: dict) -> dict:
    cid = (data.get("id") or "").strip().lower() or _slugify(data.get("name", ""))
    if cid == "chloe":
        raise HTTPException(status_code=400, detail="chloe_id_reserved")
    conn = _conn()
    try:
        girl = store.upsert_local_character(conn, {**data, "id": cid})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()
    characters.bust_cache()
    return {"girl": girl}


def _admin_update_girl(girl_id: str, data: dict) -> dict:
    cid = (girl_id or "").strip().lower()
    if cid == "chloe":
        raise HTTPException(status_code=400, detail="chloe_id_reserved")
    conn = _conn()
    try:
        existing = next((c for c in store.list_local_characters(conn)
                         if c["id"] == cid), None)
        if not existing:
            raise HTTPException(status_code=404, detail="not_found")
        merged = {**existing, **{k: v for k, v in data.items()
                                 if v is not None}, "id": cid}
        girl = store.upsert_local_character(conn, merged)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()
    characters.bust_cache()
    return {"girl": girl}


def _admin_delete_girl(girl_id: str) -> dict:
    conn = _conn()
    try:
        ok = store.delete_local_character(conn, girl_id)
    finally:
        conn.close()
    if not ok:
        raise HTTPException(status_code=404, detail="not_found")
    characters.bust_cache()
    return {"ok": True}


@app.get("/api/v1/admin/accounts")
def admin_accounts(request: Request):
    _require_admin(request)
    return _admin_accounts()


@app.get("/api/v1/admin/girls")
def admin_girls(request: Request):
    _require_admin(request)
    return _admin_girls()


@app.post("/api/v1/admin/girls")
def admin_create_girl(body: GirlIn, request: Request):
    _require_admin(request)
    return _admin_create_girl(body.model_dump())


@app.put("/api/v1/admin/girls/{girl_id}")
def admin_update_girl(girl_id: str, body: GirlUpdate, request: Request):
    _require_admin(request)
    return _admin_update_girl(girl_id, body.model_dump())


@app.delete("/api/v1/admin/girls/{girl_id}")
def admin_delete_girl(girl_id: str, request: Request):
    _require_admin(request)
    return _admin_delete_girl(girl_id)


@app.post("/api/v1/chat")
def chat(body: ChatIn, request: Request):
    user_id = _current_user_id(request)  # login required — no free use
    conn = _conn()
    try:
        # Resolve the character (unknown ids fall back to Chloe).
        character = characters.get_character(body.character_id)
        cid = character["id"]

        user = store.get_or_create_user(conn, user_id)
        store.log_turn(conn, user_id, "user", body.message, cid)

        if not store.consume_message(conn, user_id):
            return _empty_bank_reply(user, character,
                                     unpaid=not store.has_paid(conn, user_id))

        # Trust posture: advance the stage clock (at most one stage per
        # check). This NEVER gates warmth — only how much of herself she
        # has volunteered so far.
        trust.maybe_advance(conn, user_id)

        # Brain context: 3-layer memory stack (recent turns -> rolling
        # digest -> pinned facts + relevant memories) + trust posture line.
        # Memory is namespaced per character: each one remembers separately.
        context = brain.build_brain_context(conn, user_id, user, body.message,
                                            character)

        # Brain: mouth -> auditor (auditor is a pass-through stub in Phase 2).
        reply_text = brain.compose_reply(body.message, context, cid)
        store.log_turn(conn, user_id, cid, reply_text, cid)

        # Slower brain path: learn durable typed memories from his message
        # and refresh the rolling digest. Runs after the reply is sent.
        brain.learn_from_turn(conn, user_id, body.message, cid)

        # Voice note: stubbed/disabled by default -> null. Uses the
        # character's xAI voice (Chloe: liora).
        voice_note_url = None
        tier_cfg = config.TIERS[user["tier"]]
        if tier_cfg["voice_notes_per_month"] > 0 and len(reply_text) <= 280:
            if store.consume_voice_note(conn, user_id):
                voice_note_url = voice.maybe_generate_voice_note(
                    reply_text, user_id, character.get("companion_voice") or "liora")

        # Photo drop: tier-gated serving hook. Chloe's curated drops only —
        # other characters get no photos (never Chloe's photos on them).
        photo_url = None
        if cid == "chloe":
            drop = photos.maybe_drop_photo(user["tier"], user["messages_used_total"] + 1)
            if drop and not drop.get("placeholder"):
                photo_url = f"/photo_drops/{drop['file']}"
            elif drop:
                photo_url = f"PLACEHOLDER:{drop['id']}"

        user = store.get_or_create_user(conn, user_id)
        return {
            "reply_text": reply_text,
            "voice_note_url": voice_note_url,
            "photo_url": photo_url,
            "messages_remaining": user["messages_remaining"],
            "character_id": cid,
        }
    finally:
        conn.close()


@app.get("/api/v1/chat/history")
def chat_history(request: Request, character_id: str = "chloe", limit: int = 50):
    """Saved conversation with a character, oldest first. Auth required.
    The app loads this on login so the chat picks up where you left off
    instead of resetting to a blank screen."""
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 50
        return {"turns": store.conversation_history(
            conn, user_id, limit, character_id or "chloe")}
    finally:
        conn.close()


@app.get("/api/v1/me")
def me(user_id: str = "", request: Request = None):
    # Legacy route: the query param is ignored — the Bearer token identifies
    # the user. Login required.
    auth_id = _current_user_id(request)
    conn = _conn()
    try:
        user = store.get_or_create_user(conn, auth_id)
        return _user_summary(user)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Email auth (2026-09-30): login required, no anonymous/free use.
# ---------------------------------------------------------------------------
@app.post("/api/v1/auth/signup")
def auth_signup(body: SignupIn, request: Request):
    if auth.rate_limited("signup:" + _client_ip(request)):
        raise HTTPException(status_code=429, detail="too_many_attempts")
    email = auth.normalize_email(body.email)
    if not auth.valid_email(email):
        raise HTTPException(status_code=400, detail="invalid_email")
    if not auth.valid_password(body.password):
        raise HTTPException(status_code=400,
                            detail="password must be 8-128 characters")
    name = (body.name or "").strip()[:60] or None
    conn = _conn()
    try:
        existing = store.get_user_by_email(conn, email)
        if existing:
            if existing.get("email_verified_at"):
                raise HTTPException(status_code=409, detail="email_taken")
            # Unverified: rotate the verification token and resend.
            _send_verify_email(conn, existing)
            return {"ok": True, "verify_sent": True, "resent": True}
        user_id = "cm_" + auth.new_token(12).replace("-", "").replace("_", "")[:16]
        user = store.create_user_with_email(conn, user_id, email,
                                            auth.hash_password(body.password), name)
        _send_verify_email(conn, user)
        return {"ok": True, "verify_sent": True}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="email_taken")
    finally:
        conn.close()


@app.get("/api/v1/auth/verify")
def auth_verify(token: str = ""):
    conn = _conn()
    try:
        user_id = store.consume_email_verification(conn, token) if token else None
        if not user_id:
            raise HTTPException(status_code=400, detail="invalid_or_expired_link")
        store.set_email_verified(conn, user_id)
        # Free verified-signup grant (2026-09-30): the first 200 verified
        # signups get 50 free messages, once. Silent no-op afterwards.
        store.grant_free_signup_messages(conn, user_id,
                                        config.FREE_SIGNUP_MESSAGES,
                                        config.FREE_SIGNUP_CAP)
        # JVZoo affiliate channel (2026-09-30): attach any paid JVZoo
        # subscription bought under this email before the account existed.
        _user = store.get_or_create_user(conn, user_id)
        if _user.get("email"):
            store.apply_jvzoo_pending(conn, user_id, _user["email"])
        session = auth.new_token()
        store.create_session(conn, user_id, session)
    finally:
        conn.close()
    # Land back in the app, handing it the session in the URL hash (the
    # frontend picks it up, stores it, and clears the hash).
    return RedirectResponse(
        url=f"{config.PUBLIC_APP_URL}/#session={session}", status_code=303)


@app.post("/api/v1/auth/resend-verification")
def auth_resend(body: ResendIn, request: Request):
    if auth.rate_limited("resend:" + _client_ip(request)):
        raise HTTPException(status_code=429, detail="too_many_attempts")
    email = auth.normalize_email(body.email)
    conn = _conn()
    try:
        user = store.get_user_by_email(conn, email) if auth.valid_email(email) else None
        # Always return ok (don't reveal whether the email exists), but only
        # unverified accounts actually get a new email.
        if user and not user.get("email_verified_at"):
            _send_verify_email(conn, user)
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/v1/auth/login")
def auth_login(body: LoginIn, request: Request):
    if auth.rate_limited("login:" + _client_ip(request)):
        raise HTTPException(status_code=429, detail="too_many_attempts")
    email = auth.normalize_email(body.email)
    conn = _conn()
    try:
        user = store.get_user_by_email(conn, email) if auth.valid_email(email) else None
        if not user or not user.get("password_hash") or not auth.verify_password(
                body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="wrong_email_or_password")
        if not user.get("email_verified_at"):
            raise HTTPException(status_code=403, detail="email_unverified")
        session = auth.new_token()
        store.create_session(conn, user["user_id"], session)
        summary = _user_summary(user)
        summary.update({"ok": True, "token": session})
        return summary
    finally:
        conn.close()


@app.post("/api/v1/auth/logout")
def auth_logout(request: Request):
    token = auth.bearer_token(request.headers.get("authorization"))
    if token:
        conn = _conn()
        try:
            store.delete_session(conn, token)
        finally:
            conn.close()
    return {"ok": True}


@app.get("/api/v1/auth/me")
def auth_me(request: Request):
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        user = store.get_or_create_user(conn, user_id)
        return _user_summary(user)
    finally:
        conn.close()


@app.post("/api/v1/auth/name")
def auth_name(body: NameIn, request: Request):
    user_id = _current_user_id(request)
    name = (body.name or "").strip()[:60]
    if not name:
        raise HTTPException(status_code=400, detail="name_required")
    conn = _conn()
    try:
        store.set_user_name(conn, user_id, name)
        return {"ok": True, "name": name}
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


@app.get("/api/v1/free-spots")
def free_spots():
    """Public: how many of the first-FREE_SIGNUP_CAP free verified-signup
    grants remain. Powers the "50 free messages" promo on the
    signup/signin screen."""
    conn = _conn()
    try:
        left = store.free_spots_left(conn, config.FREE_SIGNUP_CAP)
    finally:
        conn.close()
    return {
        "cap": config.FREE_SIGNUP_CAP,
        "spots_left": left,
        "messages": config.FREE_SIGNUP_MESSAGES,
    }


@app.post("/api/v1/memory/export")
def memory_export(body: UserIdIn, request: Request):
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        return {"memories": store.export_memories(conn, user_id,
                                                  body.character_id or "chloe")}
    finally:
        conn.close()


@app.post("/api/v1/memory/delete")
def memory_delete(body: UserIdIn, request: Request):
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        store.delete_all_memories(conn, user_id, body.character_id or "chloe")
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/v1/memory/write")
def memory_write(body: MemoryWriteIn, request: Request):
    """Companion-internal endpoint: the brain (Phase 3) writes typed memories
    it learns mid-conversation. Typed per Thoughtspace: core/fact/preference/
    boundary/tone."""
    user_id = _current_user_id(request)
    if body.mem_type not in store.MEM_TYPES:
        raise HTTPException(400, f"mem_type must be one of {store.MEM_TYPES}")
    conn = _conn()
    try:
        mid = store.write_memory(conn, user_id, body.mem_type, body.text,
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

# ---------------------------------------------------------------------------
# JVZoo IPN — purchase fulfillment for the JVZoo affiliate channel (2026-09-30).
#
# Verification: JVZIPN v2 signs every post with cverify =
#   UPPERCASE( SHA1( paykey|customer_email|product_name|transaction_type|date
#                    |JVZIPN_SECRET )[:8] )
# Refuses with 503 until JVZIPN_SECRET is configured so JVZoo keeps retrying
# rather than recording a failure.
#
# Model: the user's "50 free messages" trial is the app's own verified-signup
# grant — a $0 trial SALE only records the subscription. The affiliate earns
# their 50% only on paid membership, so the tier is fulfilled on BILL (paid
# rebill): set_tier() tops message/voice banks to the tier allocation. RFND
# and rebill cancellations downgrade to basic. TIERS ONLY — no Keyhole show
# grants on this path (user direction 2026-09-30).
#
# Identity: JVZoo checkouts happen on jvzoo.com, so the buyer's email is the
# key. When the email has no account yet, the subscription waits and is
# applied at email verification (see auth_verify).
# ---------------------------------------------------------------------------
def _jvzoo_verify_ok(form: dict[str, str], secret: str) -> bool:
    """JVZIPN v2 signature check (support.jvzoo.com: 'JVZIPN v2 – How to
    Receive JVZoo Order Data in Your Script')."""
    incoming = (form.get("cverify") or "").strip().upper()
    if not incoming or not secret:
        return False
    parts = [form.get("paykey", ""), form.get("customer_email", ""),
             form.get("product_name", ""), form.get("transaction_type", ""),
             form.get("date", ""), secret]
    raw = "|".join(parts).encode("utf-8")
    expect = hashlib.sha1(raw).hexdigest()[:8].upper()
    return hmac.compare_digest(expect, incoming)


def _jvzoo_total(form: dict[str, str]) -> float:
    try:
        return round(float(form.get("total", "0") or "0"), 2)
    except ValueError:
        return 0.0


def _jvzoo_tier_for(form: dict[str, str]) -> str:
    return config.JVZOO_TIER_TOTALS.get(_jvzoo_total(form), "")


@app.post("/api/v1/jvzoo/ipn")
async def jvzoo_ipn(request: Request):
    secret = config.JVZIPN_SECRET
    if not secret:
        raise HTTPException(503, "JVZIPN_SECRET not configured")
    if int(request.headers.get("content-length") or 0) > WEBHOOK_MAX_BYTES:
        raise HTTPException(413, "payload too large")
    raw = await request.body()
    if len(raw) > WEBHOOK_MAX_BYTES:
        raise HTTPException(413, "payload too large")
    # Parsed by hand: this Starlette build requires python-multipart for
    # request.form(), and JVZoo only ever posts urlencoded bodies.
    try:
        form = {k: v for k, v in
                urllib.parse.parse_qsl(raw.decode("utf-8"),
                                       keep_blank_values=True)}
    except ValueError:
        raise HTTPException(400, "bad form encoding")
    if not _jvzoo_verify_ok(form, secret):
        raise HTTPException(401, "bad jvzoo signature")
    return _handle_jvzoo_ipn(form)


def _handle_jvzoo_ipn(form: dict[str, str]) -> dict[str, Any]:
    paykey = (form.get("paykey") or "").strip()
    txn_type = (form.get("transaction_type") or "").strip().upper()
    email = auth.normalize_email(form.get("customer_email"))
    if not paykey or not txn_type or not email:
        return {"ok": True, "ignored": "missing paykey/type/email"}
    conn = _conn()
    try:
        if not store.note_jvzoo_event(conn, paykey, txn_type):
            return {"ok": True, "duplicate": True}
        tier = _jvzoo_tier_for(form)
        user = store.get_user_by_email(conn, email)
        user_id = user["user_id"] if user else ""
        if txn_type == "SALE":
            # $0 trial start: record only. The 50-free-message trial is the
            # app's own verified-signup grant; no tier change here.
            if _jvzoo_total(form) > 0 and tier:
                store.upsert_jvzoo_subscription(conn, paykey, email,
                                                user_id, tier, "active")
                if user_id:
                    store.set_tier(conn, user_id, tier)
                    return {"ok": True, "user_id": user_id, "tier": tier,
                            "action": "subscribed"}
                return {"ok": True, "tier": tier,
                        "action": "pending_verification"}
            store.upsert_jvzoo_subscription(conn, paykey, email,
                                            user_id, tier, "trial")
            return {"ok": True, "action": "trial_recorded"}
        if txn_type == "BILL":
            # Paid rebill: this is the paid membership — fulfill the tier.
            if not tier:
                return {"ok": True, "ignored": "unknown total for tier"}
            store.upsert_jvzoo_subscription(conn, paykey, email,
                                            user_id, tier, "active")
            if user_id:
                store.set_tier(conn, user_id, tier)
                return {"ok": True, "user_id": user_id, "tier": tier,
                        "action": "rebill_topup"}
            return {"ok": True, "tier": tier,
                    "action": "pending_verification"}
        if txn_type == "RFND" or "CANCEL" in txn_type:
            store.end_jvzoo_subscriptions_for_email(conn, email)
            if user_id:
                store.downgrade_to_basic(conn, user_id)
                return {"ok": True, "user_id": user_id,
                        "action": "downgraded_to_basic"}
            return {"ok": True, "action": "ended_no_account"}
        return {"ok": True, "ignored": txn_type}
    finally:
        conn.close()


