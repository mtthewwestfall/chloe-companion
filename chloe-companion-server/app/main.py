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

Custom media requests ("request-a-pic", 2026-10-01):
GET  /api/v1/media-requests/balance [auth] ?girl_id=
POST /api/v1/media-requests [auth] {girl_id, kind, media, prompt?}
GET  /api/v1/media-requests/{id} [auth]
GET  /api/v1/admin/media-requests  (prompt review)
POST /api/v1/admin/credit-overage  ($5.99 Shopify overage pack fulfillment hook)

Custom video requests (2026-10-07):
POST /api/v1/custom-videos [auth] {girl_id, description} -> {id, status}
  Saves the request and attempts Sogni generation in the background.
  Blocked/failed generations stay `pending` for manual fulfillment.
GET  /api/v1/custom-videos [auth] -> {requests:[...]}
GET  /api/v1/admin/custom-videos (admin) -> {requests:[...]}
GET  /api/v1/her-day [auth] ?girl_id= -> {posts:[...]}
# GET  /api/v1/feed [auth] -> {posts:[...]} (unified timeline, all girls)
POST /api/v1/her-day/view [auth] {post_id} -> {ok:true}
POST /api/v1/her-day/generate [auth] -> {generated:N} (all girls)
POST /api/v1/admin/her-day/generate-all (admin) -> {ok, generated, skipped, failed, errors}
POST /api/v1/admin/her-day/backfill-media (admin) -> pool-first repair of today's posts
POST /api/v1/admin/her-day/set-media (admin) {post_id, media_url?, voice_url?} -> {ok, post}
"""
from __future__ import annotations

import hashlib
import hmac
import hmac
import html as _html
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Any

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import assist, auth, brain, characters, config, creator, custom_video, db, gemini_keys, give_gen, her_day, her_day_delete, live_give, media_gen, photos, shows, sims, store, submit, trust, voice
from . import assist, auth, brain, characters, config, creator, custom_video, db, gemini_keys, give_gen, her_day, roleplay_handler, live_give, media_gen, photos, shows, sims, store, submit, trust, voice
from . import push_routes, sims_saves
from . import assist, auth, brain, characters, config, creator, custom_video, db, gemini_keys, give_gen, onboarding, her_day, roleplay_handler, live_give, media_gen, photos, shows, sims, store, submit, trust, voice
from . import assist, auth, brain, characters, character_trust, config, creator, custom_video, db, gemini_keys, give_gen, her_day, her_day_delete, roleplay_handler, live_give, media_gen, photos, shows, sims, store, submit, trust, voice
from . import sims_saves

app = FastAPI(title="Chloe Companion API", version="0.4.0-auth")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://lockeddoor.ai", "https://lockdoor.ai"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)

# Sims game think endpoint (POST /api/sims/think): token-gated, with its
# own path-scoped CORS for the game's origin — see app/sims.py. Added
# after the global CORS middleware so it sits outermost for that path.
sims.register(app)
app.add_middleware(sims.SimsCorsMiddleware)

# God's Tank cloud saves (POST/GET /api/sims/save): same token gate and
# path-scoped CORS pattern as the think endpoint — see app/sims_saves.py.
sims_saves.register(app)
app.add_middleware(sims_saves.SimsSavesCorsMiddleware)

# Finished regular media-request generations are served from /generated/.
os.makedirs(config.GENERATED_DIR, exist_ok=True)
app.mount("/generated", StaticFiles(directory=config.GENERATED_DIR), name="generated")

@app.get("/admin.html", response_class=HTMLResponse, include_in_schema=False)
def admin_page():
    """Serve the companion admin panel from the same origin as its API."""
    path = os.path.join(os.path.dirname(config.DATA_DIR),
                        "chloe-companion-app", "admin.html")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return HTMLResponse(content=fh.read())
    except OSError:
        raise HTTPException(status_code=404, detail="admin_page_not_found")

# Chloe's opener for a brand-new verified user who hasn't sent anything yet
# (user's words, 2026-09-30). Seeded into her chat at email-verification time.
WELCOME_LINE = ("Well hello there handsome, did it hurt when you fell from "
                "heaven? What should I call my angel.")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ChatIn(BaseModel):
    user_id: str = ""  # legacy: ignored — the Bearer token identifies the user
    message: str = ""
    character_id: str = "chloe"
    attachment_ids: list[str] = []


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
    verified_fictional: bool = False
    owner_user_id: str = ""  # 2026-10-08: owner-only private character
    photos: list[str] = []   # reference photos (data URIs), as in creator


class GirlUpdate(BaseModel):
    name: str | None = None
    avatar_url: str | None = None
    persona: str | None = None
    companion_voice: str | None = None
    blurb: str | None = None
    door_title: str | None = None
    sort_order: int | None = None
    enabled: bool | None = None
    verified_fictional: bool | None = None
    owner_user_id: str | None = None  # 2026-10-08: owner-only privacy scope
    photos: list[str] | None = None   # reference photos (data URIs)


class GeminiKeyIn(BaseModel):
    key: str
    label: str = ""
    is_paid: bool = False


class GeminiKeyPatch(BaseModel):
    is_active: bool


class FootageIn(BaseModel):
    label: str                       # typing-main | main | give | activity
    url: str
    sort_order: int = 0


class MediaRequestIn(BaseModel):
    girl_id: str = "chloe"
    kind: str                        # regular | give
    media: str                       # pic | video
    prompt: str = ""                 # regular only; give ignores it

class SubmissionIn(BaseModel):
    name: str = ""
    persona: str = ""
    blurb: str = ""
    images: list[str] = []           # data URIs, max 3
    policy_ack: bool = False


class SubmissionApproveIn(BaseModel):
    verified_fictional: bool = False


class SubmissionRejectIn(BaseModel):
    reason: str = ""


class CompanionCreateIn(BaseModel):
    name: str = ""
    persona: str = ""
    blurb: str = ""
    gender: str = "female"
    attest_18: bool = False
    attest_rights: bool = False
    images: list[str] = []           # data URIs, 1-3 real photos
    gallery_public: bool = False     # opt-in: list in the public gallery


class GalleryVisibilityIn(BaseModel):
    public: bool = False


class CreditOverageIn(BaseModel):
    user_id: str


class GrantMessagesIn(BaseModel):
    user_id: str
    count: int = 100


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
    if not token:
        raise HTTPException(status_code=401, detail="login_required")
    conn = _conn()
    try:
        user_id = store.get_session_user_id(conn, token)
    finally:
        conn.close()
    if not user_id:
        raise HTTPException(status_code=401, detail="login_required")
    return user_id


def _optional_user_id(request: Request) -> str | None:
    """Resolve the signed-in user when a valid session is present; None
    for anonymous calls. Used by endpoints that are public but reveal
    owner-scoped (private) characters to their owner."""
    try:
        return _current_user_id(request)
    except HTTPException:
        return None


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


def _request_balance(conn: Any, user_id: str, girl_id: str = "") -> dict[str, Any]:
    """Balance payload for the request-a-pic UI."""
    user = store.get_or_create_user(conn, user_id)
    pool = store.get_request_pool(conn, user_id)
    tier = user.get("tier") or "basic"
    return {
        "tier": tier,
        "period": pool["period"],
        "included_remaining": pool["included_remaining"],
        "overage_regular": pool["overage_regular_videos"],
        "overage_give": pool["overage_give_videos"],
        "daily_used": store.requests_used_today(conn, user_id),
        "daily_cap": config.DAILY_REQUEST_CAPS.get(tier, 3),
        "give_available": (store.girl_has_give(conn, girl_id)
                           if girl_id else False),
        "give_unlimited": True,  # give presets unlimited (2026-10-08)
        "regular_monthly_cap": 1,  # custom requests: 1/month (2026-10-08)
        "regular_used_this_month":
            store.regular_requests_used_this_month(conn, user_id),
    }


# In-character lines for the media-request flow. Chloe's exact voice is
# preserved; other characters get the generic warm version.
def _request_ack(character: dict[str, Any]) -> str:
    if (character or {}).get("id", "chloe") == "chloe":
        return "give me a minute, making it just for you \U0001F618"
    name = (character or {}).get("name", "")
    return f"give me a minute{name and ', ' + name or ''} — making it just for you \U0001F618"


def _request_deflection(character: dict[str, Any]) -> str:
    if (character or {}).get("id", "chloe") == "chloe":
        return ("hmm, that one's off the table, babe — "
                "tell me something else you'd like to see \U0001F609")
    return ("hmm, I can't do that one — "
            "tell me something else you'd like to see \U0001F609")


def _request_explicit_deflection(character: dict[str, Any]) -> str:
    # Nude renditions are only for characters verified to not be a real
    # person (submission policy, 2026-10-01).
    if (character or {}).get("id", "chloe") == "chloe":
        return ("that kind of pic is only for girls verified as not a real "
                "person, babe — tell me something else you'd like to see "
                "\U0001F609")
    return ("that kind of pic is only for girls verified as not a real "
            "person — tell me something else you'd like to see \U0001F609")


def _request_failed_line(character: dict[str, Any]) -> str:
    if (character or {}).get("id", "chloe") == "chloe":
        return ("ugh, that one didn't come out right, babe — "
                "I put it back, try again in a bit \U0001F496")
    return ("that one didn't come out right — "
            "I've put your request back, try again in a bit \U0001F496")


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
            nudge = ("i've used up this month's messages, babe — they'll top "
                     "back up on my renewal. i'll be right here.")
        else:
            nudge = ("i've used up this month's messages — they'll top back up "
                     "on your renewal. i'll be right here.")
    else:
        if unpaid:
            nudge = (f"i'd love to keep talking — but i'm out of messages. "
                     f"try the $0.99 trial, or the $9.99 plan for a whole "
                     f"month of me.")
        elif tier == "basic":
            nudge = (f"i've used up this month's messages — they'll top back up "
                     f"on my renewal. i'll be right here.")
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
    # One-time-per-boot retro scrub (owner-directed 2026-10-09): a
    # credential paste the intake parser once missed must not sit in
    # the stored threads. Count only in the log — never content.
    try:
        conn = db.connect()
        try:
            assist.ensure_tables(conn)
            n = assist.scrub_sogni_credentials_from_history(conn)
            if n:
                print(f"[assist] boot scrub replaced {n} stored "
                      "credential-shaped message(s)")
            # In-flight studio flows die with the deploy (owner,
            # 2026-10-09): a guided-creation / pending-give flow
            # assembled by an older build must never wake up cold.
            cleared = assist.clear_studio_flow_state(
                conn, assist._owner_ids())
            if cleared:
                print(f"[assist] boot cleared {cleared} in-flight "
                      "studio flow state row(s)")
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — never block boot on it
        print(f"[assist] boot scrub skipped: {e.__class__.__name__}")
    # Background Gemini key prober: keeps the believed-healthy pool view
    # fresh off the request path (see app/gemini_keys.py). Disable with
    # GEMINI_PROBER=0.
    gemini_keys.start_prober()

    # Background daily give generator loop: automatically triggers daily
    # 15s Sogni give generations for each character x 9 give stages (public + private).
    def _auto_daily_gives_loop():
        while True:
            try:
                give_gen.trigger_daily_give_generations(include_private=True)
            except Exception as e:
                print(f"[give_gen] auto daily gives loop exception: {e}", flush=True)
            time.sleep(12 * 3600)

    import threading
    # Deploy-overlap boot grace: the first give drain of this
    # process waits out the window in which the old container may
    # still be draining against the same Sogni login.
    give_gen.arm_boot_grace()
    threading.Thread(target=_auto_daily_gives_loop, daemon=True, name="auto-daily-gives").start()


# ---------------------------------------------------------------------------
# Dev-agent bridge (2026-10-08): the owner's dev agent polls here for
# build jobs Hannah queued in chat, and posts real results back.
# Auth is a shared-secret header (X-Dev-Agent-Secret) — no user session
# is involved and no user data leaves these routes beyond job fields.
# When DEV_AGENT_SECRET is unset on this server, both fail closed.
# ---------------------------------------------------------------------------
class DevAgentResultIn(BaseModel):
    job_id: int
    status: str
    branch: str | None = None
    summary: str = ""
    detail: str = ""
    merged_sha: str | None = None
    report: str = ""


@app.get("/api/v1/dev-agent/poll")
def dev_agent_poll(request: Request):
    if not assist.dev_agent_secret_ok(
            request.headers.get("x-dev-agent-secret")):
        raise HTTPException(status_code=401, detail="unauthorized")
    conn = _conn()
    try:
        return {"ok": True, "job": assist.claim_dev_job(conn)}
    finally:
        conn.close()


@app.post("/api/v1/dev-agent/result")
def dev_agent_result(body: DevAgentResultIn, request: Request):
    if not assist.dev_agent_secret_ok(
            request.headers.get("x-dev-agent-secret")):
        raise HTTPException(status_code=401, detail="unauthorized")
    conn = _conn()
    try:
        job = assist.complete_dev_job(conn, body.model_dump())
    finally:
        conn.close()
    if job is None:
        raise HTTPException(status_code=404, detail="no_such_claimed_job")
    return {"ok": True, "status": job["status"]}


class BrowserAgentResultIn(BaseModel):
    job_id: int
    status: str
    title: str = ""
    final_url: str = ""
    text: str = ""
    error: str = ""
    image_b64: str = ""
    image_format: str = ""


@app.get("/api/v1/browser-agent/poll")
def browser_agent_poll(request: Request):
    # Same shared secret as the dev-agent bridge (DEV_AGENT_SECRET);
    # the VM browser worker is a sibling of the dev agent.
    if not assist.dev_agent_secret_ok(
            request.headers.get("x-dev-agent-secret")):
        raise HTTPException(status_code=401, detail="unauthorized")
    conn = _conn()
    try:
        # Workers claim only their own lane (2026-10-15): no param
        # keeps the original local worker's behavior; the Oracle VM
        # worker polls with ?lane=ovm.
        lane = request.query_params.get("lane")
        return {"ok": True, "job": assist.claim_browse_job(conn, lane)}
    finally:
        conn.close()


@app.post("/api/v1/browser-agent/result")
def browser_agent_result(body: BrowserAgentResultIn, request: Request):
    if not assist.dev_agent_secret_ok(
            request.headers.get("x-dev-agent-secret")):
        raise HTTPException(status_code=401, detail="unauthorized")
    conn = _conn()
    try:
        job = assist.complete_browse_job(conn, body.model_dump())
    finally:
        conn.close()
    if job is None:
        raise HTTPException(status_code=404, detail="no_such_claimed_job")
    return {"ok": True, "status": job["status"]}


@app.get("/health")
def health():
    return {"ok": True, "service": "chloe-companion", "phase": "3-multichar"}
@app.get("/api/v1/characters")
def list_characters(request: Request):
    """Public roster: companion-enabled characters from the Keyhole admin
    list (kh_companion=true), cached ~5 minutes. Chloe is always first and
    always present (built-in fallback when the Keyhole backend is down).
    Owner-scoped (private) characters appear only for their owner."""
    return {"characters": characters.public_list(_optional_user_id(request))}


@app.get("/api/v1/characters/{character_id}/footage")
def character_footage(character_id: str, request: Request):
    """Public: labeled footage for a companion-local girl, so the app can
    build her video tile. Only the labels the companion app may play
    (typing-main, main, activity) — 'give' is Keyhole-only and is never
    served here, no matter what the admin labeled. Unknown ids (and Chloe,
    whose clips are hardcoded in the app) return an empty list."""
    cid = (character_id or "").strip().lower()
    if not cid or cid == "chloe":
        return {"footage": []}
    if not characters.visible_to(cid, _optional_user_id(request)):
        # Owner-scoped (private) girl: invisible to everyone but her owner.
        return {"footage": []}
    conn = _conn()
    try:
        clips = store.list_girl_footage(conn, cid)
    finally:
        conn.close()
    return {"footage": [
        {"label": c["label"], "url": c["url"]}
        for c in clips
        if c["label"] in store.COMPANION_FOOTAGE_LABELS
    ]}


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
    remote = characters.get_characters(include_private=True)
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
                          "sort_order", "owner_user_id")},
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


@app.get("/api/v1/admin/rocky-claim-blocks")
def admin_rocky_claim_blocks(request: Request, limit: int = 50):
    """Rocky claim gate (2026-10-15): the dev claims the screen
    blocked, newest first — what Rocky tried to assert, when, and
    the reply it rode in on. The owner's inspection window into
    the gate; the gate itself lives in assist.screen_rocky_reply."""
    _require_admin(request)
    conn = _conn()
    try:
        return {"blocks": assist.rocky_claim_blocks(conn, limit=limit)}
    finally:
        conn.close()


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


# ---------------------------------------------------------------------------
# Per-girl footage labels (2026-09-30): the Girls tab lets the user attach
# video clips to a companion-local girl and label each one typing-main |
# main | give | activity. 'give' is stored (Keyhole-only) but never served
# to the companion app — see GET /api/v1/characters/{id}/footage.
# ---------------------------------------------------------------------------
@app.get("/api/v1/admin/girls/{girl_id}/footage")
def admin_girl_footage(girl_id: str, request: Request):
    _require_admin(request)
    conn = _conn()
    try:
        return {"footage": store.list_girl_footage(conn, girl_id)}
    finally:
        conn.close()


@app.get("/api/v1/admin/girls/{girl_id}/requested-actions")
def admin_requested_actions(girl_id: str, request: Request):
    """View user-requested give actions for a girl, most-requested first."""
    _require_admin(request)
    conn = _conn()
    try:
        return {"actions": store.get_requested_actions(conn, girl_id)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Admin: Gemini API key rotation (2026-10-06). Key values are NEVER returned
# in full — only masked ("...abcd"). See app/gemini_keys.py.
# ---------------------------------------------------------------------------
@app.get("/api/v1/admin/gemini-keys")
def admin_gemini_keys(request: Request):
    """List keys with health status. Key values are masked."""
    _require_admin(request)
    return {"keys": gemini_keys.list_keys()}


@app.post("/api/v1/admin/gemini-keys")
def admin_add_gemini_key(body: GeminiKeyIn, request: Request):
    _require_admin(request)
    try:
        return {"key": gemini_keys.add_key(body.key, body.label, body.is_paid)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/v1/admin/gemini-keys/{key_id}")
def admin_delete_gemini_key(key_id: int, request: Request):
    _require_admin(request)
    if not gemini_keys.delete_key(key_id):
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}


@app.patch("/api/v1/admin/gemini-keys/{key_id}")
def admin_patch_gemini_key(key_id: int, body: GeminiKeyPatch, request: Request):
    _require_admin(request)
    key = gemini_keys.set_active(key_id, body.is_active)
    if key is None:
        raise HTTPException(status_code=404, detail="not_found")
    return {"key": key}


@app.post("/api/v1/admin/gemini-keys/{key_id}/test")
def admin_test_gemini_key(key_id: int, request: Request):
    """Minimal live call against one key. Returns health, never the key."""
    _require_admin(request)
    result = gemini_keys.test_key(key_id)
    if result is None:
        raise HTTPException(status_code=404, detail="not_found")
    return result


@app.get("/api/v1/admin/gemini-usage")
def admin_gemini_usage(request: Request, limit: int = 100):
    """Recent Gemini key usage by source app. Shows which app (whispr-web,
    creator-bot, proxy, etc.) is using which keys, and success/failure."""
    _require_admin(request)
    return {
        "by_source": gemini_keys.usage_by_source(limit),
        "recent": gemini_keys.get_usage(limit),
    }


@app.get("/api/v1/admin/gemini-health")
def admin_gemini_health(request: Request):
    """Key-pool health for adaptive throttling. `degraded` is true while
    the pool is serving from reserve keys — callers should halve their
    request rate until it clears."""
    _require_admin(request)
    return gemini_keys.degradation_info()


@app.post("/api/v1/admin/girls/{girl_id}/footage")
def admin_add_girl_footage(girl_id: str, body: FootageIn, request: Request):
    _require_admin(request)
    conn = _conn()
    try:
        clip = store.add_girl_footage(conn, girl_id, body.label, body.url,
                                      body.sort_order)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()
    return {"clip": clip}


@app.delete("/api/v1/admin/girls/{girl_id}/footage/{footage_id}")
def admin_delete_girl_footage(girl_id: str, footage_id: int, request: Request):
    _require_admin(request)
    conn = _conn()
    try:
        ok = store.delete_girl_footage(conn, footage_id)
    finally:
        conn.close()
    if not ok:
        raise HTTPException(status_code=404, detail="not_found")
    return {"ok": True}


_ACTION_REQUEST_RE = None
def _get_action_re():
    global _ACTION_REQUEST_RE
    if _ACTION_REQUEST_RE is None:
        import re
        # Matches "show me your X", "suck my X", "let me see your X", etc.
        # Gerund forms included (2026-10-14, in step with
        # live_give._ACTION_ASK_RE): "taking off your bra" mid-
        # sentence is how the ask actually arrives.
        _ACTION_REQUEST_RE = re.compile(
            r"\b(show me|let me see|wanna see|want to see|sucking|suck|"
            r"licking|lick|touching|touch|showing|show|stripping|strip|"
            r"taking off|take off|removing|remove)\b.{0,40}\b"
            r"(tits|titties|boobs|breasts|pussy|ass|dick|cock|nipples|body|"
            r"thighs|panties|bra)\b",
            re.IGNORECASE,
        )
    return _ACTION_REQUEST_RE


def _maybe_save_requested_action(conn, girl_id: str, user_text: str) -> None:
    """Detect explicit action requests in chat and save them as give presets."""
    try:
        if _get_action_re().search(user_text or ""):
            store.save_requested_action(conn, girl_id, user_text)
    except Exception:
        pass  # never break chat on a save failure



# Telegram bot auth (2026-10-06): creates/links account via Telegram ID
@app.post("/api/v1/auth/telegram")
def auth_telegram(body: dict):
    """Link a Telegram user to a companion account. Body: {telegram_id, name}"""
    tid = str(body.get("telegram_id", ""))
    name = body.get("name", "Telegram User")
    if not tid:
        raise HTTPException(status_code=400, detail="telegram_id required")
    conn = _conn()
    try:
        user_id = f"tg_{tid}"
        # Check if this is a new user
        existing = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        user = store.get_or_create_user(conn, user_id)
        # New Telegram users get 50 free messages (same as site), not 1000
        if not existing:
            conn.execute("UPDATE users SET messages_remaining = 50 WHERE user_id = ?", (user_id,))
            conn.commit()
        # Create a session token for API calls
        import secrets
        token = secrets.token_urlsafe(32)
        store.create_session(conn, user_id, token)
        return {"ok": True, "user_id": user_id, "token": token, "name": name}
    finally:
        conn.close()

def _ensure_link_codes(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS link_codes (
               code TEXT PRIMARY KEY,
               user_id TEXT NOT NULL,
               expires_at TEXT NOT NULL,
               used INTEGER NOT NULL DEFAULT 0)""")


@app.post("/api/v1/auth/telegram-link")
def auth_telegram_link(body: dict):
    """Mint a one-time, 10-minute app-login code for a Telegram user.

    The bot sends lockeddoor.ai/?link=<code> as a button in the user's
    private chat. Redeeming it (auth_redeem_link) creates a web session
    for the SAME tg_ account — same balance, memories, and streaks.
    The code is the secret: single-use, short-lived, and only ever
    delivered inside that user's Telegram chat.
    """
    import secrets as _secrets
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    tid = str(body.get("telegram_id", ""))
    name = body.get("name", "Telegram User")
    if not tid:
        raise HTTPException(status_code=400, detail="telegram_id required")
    conn = _conn()
    try:
        user_id = f"tg_{tid}"
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (user_id,)).fetchone()
        user = store.get_or_create_user(conn, user_id)
        if not existing:
            conn.execute(
                "UPDATE users SET messages_remaining = 50 WHERE user_id = ?",
                (user_id,))
            conn.commit()
        _ensure_link_codes(conn)
        code = _secrets.token_urlsafe(9)
        expires = (_dt.now(_tz.utc) + _td(minutes=10)).strftime(
            "%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO link_codes (code, user_id, expires_at) "
            "VALUES (?, ?, ?)", (code, user_id, expires))
        conn.commit()
        return {"ok": True, "code": code, "user_id": user_id, "name": name}
    finally:
        conn.close()


@app.post("/api/v1/auth/redeem-link")
def auth_redeem_link(body: dict):
    """Exchange a one-time Telegram app-link code for a web session."""
    import secrets as _secrets
    from datetime import datetime as _dt, timezone as _tz
    code = str(body.get("code", "")).strip()
    if not code:
        raise HTTPException(status_code=400, detail="code required")
    conn = _conn()
    try:
        _ensure_link_codes(conn)
        row = conn.execute(
            "SELECT user_id, expires_at, used FROM link_codes "
            "WHERE code = ?", (code,)).fetchone()
        now = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
        if (not row or int(row["used"]) == 1
                or str(row["expires_at"]) < now):
            raise HTTPException(status_code=400,
                                detail="invalid_or_expired_code")
        conn.execute("UPDATE link_codes SET used = 1 WHERE code = ?",
                     (code,))
        conn.commit()
        user_id = row["user_id"]
        user = store.get_or_create_user(conn, user_id)
        token = _secrets.token_urlsafe(32)
        store.create_session(conn, user_id, token)
        return {"ok": True, "token": token, "user_id": user_id,
                "name": user.get("name") or ""}
    finally:
        conn.close()


@app.post("/api/v1/chat/attachments")
async def upload_chat_attachment(request: Request,
                                 character_id: str = Form("hannah"),
                                 file: UploadFile = File(...)):
    """Owner-only assistant attachment intake (Hannah and Mia).

    The file is validated, size-capped, stored under /generated/
    hannah-uploads, and returned as metadata the client then binds to
    one chat message via attachment_ids. Text extraction happens at
    upload time in app.assist; images are inspected on the chat turn.
    """
    user_id = _current_user_id(request)
    cid = (character_id or "").strip().lower()
    if user_id not in assist._owner_ids():
        raise HTTPException(
            status_code=403,
            detail="attachments are owner-only")
    # Resolve through the same visibility/ownership rules as a chat turn.
    character = characters.get_character(cid, user_id)
    cid = character["id"]
    data = await file.read(assist.ATTACHMENT_READ_LIMIT + 1)
    conn = _conn()
    try:
        store.get_or_create_user(conn, user_id)
        try:
            attachment = assist.save_chat_attachment(
                conn, user_id, cid, file.filename or "attachment",
                file.content_type or "", data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "attachment": attachment}
    finally:
        conn.close()


@app.post("/api/v1/chat")

def chat(body: ChatIn, request: Request):
    user_id = _current_user_id(request)  # login required — no free use
    conn = _conn()
    try:
        # Resolve the character (unknown ids — and other users' private
        # characters — fall back to Chloe).
        character = characters.get_character(body.character_id, user_id)
        cid = character["id"]

        user = store.get_or_create_user(conn, user_id)
        attachment_records: list[dict[str, Any]] = []
        public_attachments: list[dict[str, Any]] = []
        if body.attachment_ids:
            if user_id not in assist._owner_ids():
                raise HTTPException(
                    status_code=403,
                    detail="attachments are owner-only")
            try:
                attachment_records = assist.get_chat_attachments(
                    conn, user_id, cid, body.attachment_ids)
            except ValueError as exc:
                raise HTTPException(status_code=400,
                                    detail=str(exc)) from exc
            public_attachments = [
                assist.public_attachment(record)
                for record in attachment_records
            ]

        message_text = body.message or ""
        effective_message = message_text.strip() or (
            "What do you see in this?" if attachment_records else "")
        turn_id = store.log_turn(conn, user_id, "user", message_text, cid,
                                 attachments=public_attachments)
        # Daily streak for this (user, character) pair — consecutive
        # local days with a user turn (2026-10-17).
        streak_now = 0
        try:
            from datetime import datetime as _sdt, timedelta as _std
            from zoneinfo import ZoneInfo as _SZI
            try:
                _stz = _SZI(user.get("timezone") or "America/New_York")
            except Exception:
                _stz = _SZI("America/New_York")
            _snow = _sdt.now(_stz)
            streak_now = store.touch_streak(
                conn, user_id, cid, _snow.strftime("%Y-%m-%d"),
                (_snow - _std(days=1)).strftime("%Y-%m-%d"))
        except Exception:
            streak_now = 0
        if attachment_records:
            assist.mark_chat_attachments_used(
                conn, [record["id"] for record in attachment_records],
                turn_id)

        # Referral activation (2026-10-06): check if this message activates
        # a pending referral (3 messages + email verified)
        try:
            check_referral_activation(conn, user_id)
        except Exception:
            pass

        # Requested give actions (2026-10-06): if he's asking for a specific
        # explicit action, save it as a preset for this girl's give system.
        _maybe_save_requested_action(conn, cid, body.message)

        # Owner bypass (2026-10-09): the owner never drains a message bank
        # and never sees the paywall upsell — the same ASSIST_OWNER_IDS gate
        # as the media-request owner bypass. Only the lifetime counter
        # advances (photo-drop cadence); the bank itself is untouched.
        # Customers take the exact path they always have.
        is_owner = user_id in assist._owner_ids()
        if is_owner:
            store.note_message_sent(conn, user_id)
        elif not store.consume_message(conn, user_id):
            return _empty_bank_reply(user, character,
                                     unpaid=not store.has_paid(conn, user_id))

        # Trust posture: advance the stage clock (at most one stage per
        # check). This NEVER gates warmth — only how much of herself she
        # has volunteered so far.
        character_trust.maybe_advance(conn, user_id, cid)

        # Brain context: 3-layer memory stack (recent turns -> rolling
        # digest -> pinned facts + relevant memories) + trust posture line.
        # Memory is namespaced per character: each one remembers separately.
        context = brain.build_brain_context(conn, user_id, user,
                                            effective_message, character)

        # Live give creation — FIRST CLAIM on an explicit ask
        # (2026-10-14 routing fix): for the four private girls, an
        # owner's plain "make me a video of you ..." ask is evaluated
        # by the live-give hook BEFORE the assistant lane runs. The
        # assistant's Sogni lane matches the same phrasing, claimed
        # the turn first, and the hook yielded on assist_claimed —
        # the ask was eaten silently and no render was ever started.
        # When the hook takes the turn (starts a render, or serves
        # the holdover preset), maybe_assist is skipped for it. When
        # the detector does not match, the turn flows exactly as
        # before: maybe_assist first, then the hook for initiative.
        assist_hit = None
        lg_hit = None
        lg_evaluated = False
        if cid in live_give.LIVE_GIVE_GIRLS and is_owner \
                and live_give.detect_live_give_ask(effective_message):
            lg_evaluated = True
            try:
                lg_hit = live_give.maybe_start_live_give(
                    conn, user_id, cid, effective_message,
                    assist_claimed=False)
            except Exception:
                lg_hit = None
        if lg_hit is None:
            # Assistant capabilities ("her hands"): web search, reminders,
            # email drafts, job lookup. Tier-gated, owner-only until
            # rollout. Never raises — fail open. Credential rule: user
            # logins never touch us; they live on the user's device
            # (see js/assist.js).
            if attachment_records:
                assist_hit = assist.maybe_assist(
                    conn, user, effective_message, cid,
                    attachments=attachment_records)
            else:
                assist_hit = assist.maybe_assist(
                    conn, user, effective_message, cid)
            if assist_hit and assist_hit.get("brief"):
                context["assist_result"] = assist_hit["brief"]

            # Live give creation: her own initiative on an
            # explicit-heat moment (explicit asks were given first
            # claim above; an already-evaluated hook is not run
            # twice). Yields whenever the assistant lane claimed the
            # turn. Never raises.
            if not lg_evaluated:
                try:
                    lg_hit = live_give.maybe_start_live_give(
                        conn, user_id, cid, effective_message,
                        assist_claimed=bool(
                            context.get("assist_result")
                            or (assist_hit and (
                                assist_hit.get("sogni_media_kind")
                                or assist_hit.get("image_url")))))
                except Exception:
                    lg_hit = None
        if lg_hit and lg_hit.get("brief") \
                and not context.get("assist_result"):
            context["assist_result"] = lg_hit["brief"]

        # Rocky claim gate, part 1 (owner order 2026-10-15):
        # pre-generation grounding. When a Rocky owner turn touches
        # dev work — or answers his dev proposal with a bare "yes" —
        # the real builder-queue state rides in as the brief, the
        # only facts he may state about that work. Assist-claimed
        # turns already carry their own real brief; lanes win first.
        if assist.rocky_gate_applies(cid, is_owner) \
                and not context.get("assist_result"):
            try:
                last_rocky = ""
                for turn in reversed(context.get("recent") or []):
                    if turn.get("role") == cid:
                        last_rocky = turn.get("text") or ""
                        break
                if assist.rocky_turn_needs_grounding(
                        effective_message, last_rocky):
                    context["assist_result"] = assist.rocky_ground_brief(
                        conn, user_id,
                        phantom=assist.rocky_phantom_start(
                            effective_message, last_rocky))
            except Exception:
                pass

        # Brain: mouth -> auditor (auditor is a pass-through stub in Phase 2).
        reply_text = brain.compose_reply(effective_message, context, cid)


        # Rocky claim gate, part 2: the deterministic claim screen.
        # A dev action/result assertion ships only when a real queue
        # record backs it; anything unbacked is replaced with the
        # grounded state and logged (assist.rocky_claim_blocks).
        # Fixed replies are server text (the credential confirmation)
        # and are never screened.
        if assist.rocky_gate_applies(cid, is_owner) \
                and not (context.get("assist_result") or "").startswith(
                    brain.FIXED_REPLY_PREFIX):
            reply_text = assist.screen_rocky_reply(
                conn, user_id, reply_text, cid)
        store.log_turn(conn, user_id, cid, reply_text, cid)

        # Slower brain path: learn durable typed memories from his message
        # and refresh the rolling digest. Runs after the reply is sent.
        brain.learn_from_turn(conn, user_id, effective_message, cid)

        # Voice note: xAI TTS, gated on CHLOE_XAI_API_KEY + CHLOE_VOICE_ENABLED=1.
        # Credit is consumed ONLY after a note is actually generated (never on failure).
        voice_note_url = None
        tier_cfg = config.TIERS[user["tier"]]; _o=onboarding.stage(context)if cid!="rocky"else None; _vs=onboarding.voice_note_script(character)if _o==onboarding.STAGE_VOICE_NOTE else reply_text

      
        if tier_cfg["voice_notes_per_month"] > 0 and len(_vs) <= 280:
            has_quota = is_owner or (user.get("voice_notes_remaining") or 0) > 0
            voice_note_url = voice.maybe_generate_voice_note(
            _vs, user_id, character.get("companion_voice") or "liora", quota=has_quota, consume=(lambda: store.consume_voice_note(conn, user_id)) if not is_owner else None, character_id=cid)

        # Photo drop: tier-gated serving hook. Chloe's curated drops only —
        # other characters get no photos (never Chloe's photos on them).
        # maybe_drop_photo never returns placeholder entries, so a drop
        # here always has a real file; the drop carries its OWN caption
        # (the client used to caption the card with the reply text, which
        # printed her reply twice — bubble + card — 2026-10-14).
        reply_text=onboarding.with_voice_note_tease(reply_text,character)if _o==onboarding.STAGE_VOICE_NOTE and voice_note_url else(onboarding.with_roleplay_tease(reply_text,character)if _o==onboarding.STAGE_VOICE_NOTE else(onboarding.with_checkin_nudge(reply_text,character)if _o==onboarding.STAGE_CHECKIN and(config.TIERS.get(user["tier"])or{}).get("proactive")else reply_text)); photo_url = None
        photo_caption = None
        if cid == "chloe":
            drop = photos.maybe_drop_photo(user["tier"], user["messages_used_total"] + 1)
            if drop:
                photo_url = f"/photo_drops/{drop['file']}"
                photo_caption = drop.get("caption") or ""

        user = store.get_or_create_user(conn, user_id)
        resp = {
            "reply_text": reply_text,
            "voice_note_url": voice_note_url,
            "photo_url": photo_url,
            "photo_caption": photo_caption,
            "messages_remaining": user["messages_remaining"],
            "character_id": cid,
            "attachments": public_attachments,
            "streak_current": streak_now,
        }
        # Email drafts are composed server-side but created on the user's
        # device (Gmail API from the browser) — the token never reaches us.
        if assist_hit and assist_hit.get("email_draft"):
            resp["email_draft"] = assist_hit["email_draft"]
        if assist_hit and assist_hit.get("image_url"):
            resp["image_url"] = assist_hit["image_url"]
            resp["image_prompt"] = assist_hit.get("image_prompt", "")
        # Secure login box: her fixed login ask carries a button that
        # opens the in-app credential form (POST /assist/sogni-login).
        if assist_hit and assist_hit.get("sogni_login_box"):
            resp["sogni_login_box"] = True
        return resp
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
        summary = _user_summary(user)
        pool = store.get_request_pool(conn, auth_id)
        summary["requests_remaining"] = (pool["included_remaining"]
                                         + pool["overage_regular_videos"]
                                         + pool["overage_give_videos"])
        return summary
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Custom media requests ("request-a-pic", 2026-10-01).
#
# GET  /api/v1/media-requests/balance [auth] ?girl_id=
#      -> {tier, period, included_remaining, overage_regular, overage_give,
#          daily_used, daily_cap, give_available}
# POST /api/v1/media-requests [auth] {girl_id, kind, media, prompt?}
#      -> {id, status, result_url?, ack?}
#         give  -> fulfilled instantly from her pre-approved library ($0)
#         regular -> pending; she acknowledges in character, the app polls
#                    until ready (~1-2 min). Failures auto-refund.
# GET  /api/v1/media-requests/{id} [auth] -> {id, status, result_url, ...}
# ---------------------------------------------------------------------------
@app.get("/api/v1/media-requests/balance")
def media_balance(request: Request, girl_id: str = ""):
    user_id = _current_user_id(request)
    if girl_id and not characters.visible_to(girl_id, user_id):
        girl_id = ""  # private girl: invisible to everyone but her owner
    conn = _conn()
    try:
        return _request_balance(conn, user_id, girl_id)
    finally:
        conn.close()


@app.post("/api/v1/media-requests")
def create_media_request(body: MediaRequestIn, request: Request):
    user_id = _current_user_id(request)
    kind = (body.kind or "").strip().lower()
    media = (body.media or "").strip().lower()
    if kind not in store.REQUEST_KINDS:
        raise HTTPException(status_code=400, detail="kind must be regular|give")
    if media not in store.REQUEST_MEDIA:
        raise HTTPException(status_code=400, detail="media must be pic|video")
    conn = _conn()
    try:
        character = characters.get_character(body.girl_id, user_id)
        cid = character["id"]
        user = store.get_or_create_user(conn, user_id)
        tier = user.get("tier") or "basic"

        # Owners bypass all limits (2026-10-05).
        is_owner = user_id in assist._owner_ids()
        if kind == "give":
            # Give presets are unlimited (2026-10-08): pre-made clips, $0
            # marginal cost. Skip the daily cap and pool consumption.
            pool = {"source": "give_unlimited"}
        else:
            # Regular (custom) requests: 1 per month hard cap (2026-10-08).
            if (not is_owner and
                    store.regular_requests_used_this_month(conn, user_id) >= 1):
                raise HTTPException(status_code=402,
                                    detail="monthly_regular_cap")
            # Daily cap (anti-farming) still applies to regular requests.
            cap = config.DAILY_REQUEST_CAPS.get(tier, 3)
            if not is_owner and store.requests_used_today(conn, user_id) >= cap:
                raise HTTPException(status_code=429, detail="daily_request_cap")

        # Give is only offered for girls that actually have give footage.
        if kind == "give" and not store.girl_has_give(conn, cid):
            raise HTTPException(status_code=400, detail="give_unavailable")

        # Regular prompts are moderated server-side. Rejected prompts get an
        # in-character deflection — never an error dump — and cost nothing.
        if kind == "regular":
            # Explicit lane (2026-10-01): only for custom-submission
            # characters verified to not be a real person. The hard
            # blocklist (minors / non-consent / extreme) never opens.
            allow_explicit = bool(character.get("verified_fictional"))
            reason = store.moderate_prompt(body.prompt,
                                          allow_explicit=allow_explicit)
            if reason:
                req = store.create_media_request(
                    conn, user_id, cid, kind, media, body.prompt,
                    pool_source="included")
                store.set_media_request_status(conn, req["id"], "rejected")
                deflection = (
                    _request_explicit_deflection(character)
                    if reason == "explicit_blocked"
                    else _request_deflection(character))
                return {"id": req["id"], "status": "rejected",
                        "deflection": deflection,
                        "character_id": cid}

        # Regular generation needs the Sogni key; fail closed without
        # consuming anything when it isn't configured.
        if kind == "regular" and not config.SOGNI_API_KEY:
            raise HTTPException(status_code=503,
                                detail="generation_unavailable")

        if kind != "give":
            # (give already set pool = give_unlimited above)
            if is_owner:
                pool = {"source": "owner"}  # unlimited: nothing consumed
            else:
                pool = store.consume_request(conn, user_id, kind)
                if pool is None:
                    raise HTTPException(status_code=402,
                                        detail="no_requests_left")

        req = store.create_media_request(
            conn, user_id, cid, kind, media, body.prompt,
            pool_source=pool["source"])

        if kind == "give":
            clip = store.pick_give_clip(conn, user_id, cid)
            if not clip:  # raced with footage deletion — refund, fail soft
                if not is_owner:
                    store.refund_request(conn, user_id, pool["source"])
                store.set_media_request_status(conn, req["id"], "failed")
                raise HTTPException(status_code=400, detail="give_unavailable")
            store.record_give_send(conn, user_id, cid, clip["id"])
            store.set_media_request_status(conn, req["id"], "ready",
                                           result_url=clip["url"], cost_usd=0)
            return {"id": req["id"], "status": "ready",
                    "result_url": clip["url"], "media": media,
                    "character_id": cid,
                    "balance": _request_balance(conn, user_id, cid)}

        # Regular: async generation. She acknowledges in character; the app
        # polls GET /api/v1/media-requests/{id} until ready.
        media_gen.spawn_generation(req["id"])
        return {"id": req["id"], "status": "pending",
                "ack": _request_ack(character), "media": media,
                "character_id": cid,
                "balance": _request_balance(conn, user_id, cid)}
    finally:
        conn.close()



def _pwa_media_dir() -> str:
    """Absolute path of the statically-served chloe-companion-app/media dir."""
    return os.path.join(os.path.dirname(config.DATA_DIR),
                        "chloe-companion-app", "media")


def _scan_media_dir_give_clips(girl_id: str) -> list[dict]:
    """Staged give clips for a girl found as files in the static media dir
    (<girl>-give-stage<N>[-variant].mp4) without needing girl_media rows.
    A girl's repo-shipped library works the moment the files land."""
    gid = (girl_id or "").lower().strip()
    found: list[dict] = []
    try:
        names = sorted(os.listdir(_pwa_media_dir()))
    except OSError:
        return found
    for fn in names:
        fl = fn.lower()
        if not fl.startswith(gid + "-give-") or not fl.endswith(".mp4"):
            continue
        m = re.search(r"-stage(\d+)", fl)
        stage = int(m.group(1)) if m else 0
        if stage < 1 or stage > 9:
            continue
        found.append({"file": fn, "stage": stage})
    return found


# ---------------------------------------------------------------------------
# Preset videos (2026-10-06): serve a girl's give clips by preset tag.
# POST /api/v1/preset-video {girl_id, preset} -> {video_url, media_type, preset}
# Clips are pre-made (no API cost), so this is unlimited — no balance check.
# ---------------------------------------------------------------------------

# Known give clips per girl+preset. Updated when new presets are tagged.
PRESET_CLIPS = {
    ("chloe", "love"): [
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_1e67ecdd0b6dd10e.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_c917d49a514c5329.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_66c98753a458c922.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_685e3e81d92264e3.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_5b254cf0206883c7.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_7948798a732e5b08.mp4", "media_type": "video"},
    ],
    ("mara", "love"): [
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_1a10870031b9ee4f.mp4", "media_type": "video"},
    ],
    ("isabella", "love"): [
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_a5994f3f320bb198.mp4", "media_type": "video"},
    ],
}



class ResetCharacterIn(BaseModel):
    """Admin: wipe one user+character conversation (turns, typed
    memories, rolling digest) so their next chat starts fresh."""
    character_id: str


class SeedMessageIn(BaseModel):
    """Admin: seed one character turn into a user's chat (e.g. a first
    message from a character she created). Text lands byte-exact."""
    character_id: str
    text: str


class PresetVideoIn(BaseModel):
    girl_id: str
    preset: str


# Track last-sent clip per girl+preset to avoid immediate repeats.
_LAST_PRESET_SENT: dict[tuple[str, str], str] = {}

_GIVE_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

def _give_media_type(url: str) -> str:
    """A give library holds pictures as well as videos (owner uploads and
    approved generations). Label rows by file extension so the app renders
    pictures as <img> instead of a broken <video> bubble."""
    u = (url or "").split("?", 1)[0].lower()
    return "image" if u.endswith(_GIVE_IMAGE_EXTS) else "video"

@app.post("/api/v1/preset-video")
async def preset_video(body: PresetVideoIn, request: Request):
    """Return a give clip following the escalation sequence.
    Stages: 1=lift tease, 2=shirt off, 3=boob grabs, 4=nipple pinches,
    5=pants off, 6=plays with herself, 7=titty fuck, 8=oral, 9=sex.
    Logged-in users progress through stages per girl; anonymous users
    get random clips. Unlimited — pre-made clips cost no API."""
    import random as _random
    key = (body.girl_id.lower().strip(), body.preset.lower().strip())
    conn = _conn()
    try:
        _seed_girl_media(conn)
        # Try to identify the user for stage progression
        user_id = None
        try:
            user_id = _current_user_id(request)
        except Exception:
            pass

        # Owner-scoped (private) girls are invisible to everyone else.
        if not characters.visible_to(key[0], user_id):
            raise HTTPException(status_code=404, detail="no clips for preset")

        stage = 0
        seen_ids = set()
        if user_id:
            prog = conn.execute(
                "SELECT gives_received FROM user_give_progress WHERE user_id = ? AND girl_id = ?",
                (user_id, key[0])).fetchone()
            gives = prog["gives_received"] if prog else 0
            # Rotation: stages cycle 1-9, each rotation serves fresh clips
            stage = (gives % 9) + 1
            seen_rows = conn.execute(
                "SELECT media_id FROM user_give_seen WHERE user_id = ? AND girl_id = ?",
                (user_id, key[0])).fetchall()
            seen_ids = {r["media_id"] for r in seen_rows}

        # Get clips at the target stage
        # If target stage is empty, walk forward through stages to find clips
        # (never fall back to random - that breaks the 1->9 escalation)
        db_rows = []
        try:
            if stage > 0:
                for try_stage in list(range(stage, 10)) + list(range(1, stage)):
                    db_rows = conn.execute(
                        """SELECT id, url FROM girl_media WHERE girl_id = ? AND kind = 'give'
                           AND (preset = ? OR preset IS NULL) AND stage = ?""",
                        (key[0], key[1], try_stage)).fetchall()
                    if db_rows:
                        stage = try_stage  # use the actual stage we found clips at
                        break
        except Exception:
            db_rows = []  # table may not have stage column; fall back to clips dict
        if not db_rows:
            # Girls added after the original three tag their staged give
            # library 'give' instead of a named preset; when the requested
            # named preset has no rows for her, serve that library instead
            # of 404ing. (The query above already matched for the original
            # girls, so their behavior is unchanged.)
            try:
                if stage > 0:
                    for try_stage in list(range(stage, 10)) + list(range(1, stage)):
                        db_rows = conn.execute(
                            """SELECT id, url FROM girl_media WHERE girl_id = ? AND kind = 'give'
                               AND preset = 'give' AND stage = ?""",
                            (key[0], try_stage)).fetchall()
                        if db_rows:
                            stage = try_stage  # use the actual stage we found clips at
                            break
                else:
                    # Anonymous (stage 0): only for girls the curated clip
                    # dicts don't already cover, so the originals keep their
                    # exact pre-fix serving behavior.
                    curated = key in PRESET_CLIPS
                    try:
                        curated = curated or key in GIVE_CLIPS
                    except NameError:
                        pass
                    if not curated:
                        db_rows = conn.execute(
                            """SELECT id, url FROM girl_media WHERE girl_id = ? AND kind = 'give'
                               AND preset = 'give'""",
                            (key[0],)).fetchall()
            except Exception:
                db_rows = []
        clips = [{"id": r["id"], "url": r["url"],
                  "media_type": _give_media_type(r["url"])} for r in db_rows]
        if not clips:
            # Static media-dir fallback (2026-10-10): staged give clips
            # shipped in chloe-companion-app/media/ work even when they
            # were never registered in girl_media (e.g. brooklyn's
            # library). Same stage-escalation walk as the DB path.
            base = str(request.base_url).rstrip("/")
            dir_clips = _scan_media_dir_give_clips(key[0])
            picked = dir_clips
            if stage > 0:
                picked = []
                for try_stage in list(range(stage, 10)) + list(range(1, stage)):
                    at = [c for c in dir_clips if c["stage"] == try_stage]
                    if at:
                        stage = try_stage
                        picked = at
                        break
            clips = [{"id": None, "url": base + "/media/" + c["file"],
                      "media_type": "video"} for c in picked]
        # Prefer unseen clips at this stage; if all seen, start a fresh rotation
        fresh = [c for c in clips if c["id"] not in seen_ids] if user_id else clips
        if user_id and clips and not fresh:
            # All clips at this stage seen — clear seen for a new rotation
            conn.execute(
                "DELETE FROM user_give_seen WHERE user_id = ? AND girl_id = ?",
                (user_id, key[0]))
            fresh = clips
            seen_ids = set()
    finally:
        conn.close()
    if not clips:
        clips = PRESET_CLIPS.get(key, [])
    if not clips:
        # Fallback to GIVE_CLIPS (seeded from Keyhole)
        try:
            clips = GIVE_CLIPS.get(key, [])
        except NameError:
            clips = []
    if not clips:
        raise HTTPException(status_code=404, detail="no clips for preset")
    pool = fresh or clips
    # Avoid immediate repeats (anonymous)
    if not user_id:
        last_url = _LAST_PRESET_SENT.get(key)
        pool = [c for c in pool if c["url"] != last_url] if len(pool) > 1 else pool
    clip = _random.choice(pool)
    if not user_id:
        _LAST_PRESET_SENT[key] = clip["url"]
    # Lifetime cap check removed - was using closed connection
    # Record progress + mark seen
    if user_id:
        conn = _conn()
        try:
            conn.execute(
                """INSERT INTO user_give_progress (user_id, girl_id, gives_received)
                   VALUES (?, ?, 1)
                   ON CONFLICT (user_id, girl_id)
                   DO UPDATE SET gives_received = gives_received + 1,
                                 updated_at = datetime('now')""",
                (user_id, key[0]))
            if clip.get("id"):
                conn.execute(
                    """INSERT OR IGNORE INTO user_give_seen (user_id, girl_id, media_id, stage)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, key[0], clip["id"], stage))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
    return {"video_url": clip["url"], "media_type": clip["media_type"],
            "preset": body.preset, "girl_id": body.girl_id, "stage": stage}





@app.get("/api/v1/give-presets/{girl_id}")
def give_presets(girl_id: str, request: Request):
    """Presets for the in-app 'Send a video' sheet: counts every servable
    give clip for the girl — girl_media rows plus staged files found in
    the static media dir. Owner-scoped girls only for their owner."""
    gid = (girl_id or "").lower().strip()
    try:
        user_id = _current_user_id(request)
    except Exception:
        user_id = None
    if not characters.visible_to(gid, user_id):
        raise HTTPException(status_code=404, detail="unknown girl")
    n_db = 0
    conn = _conn()
    try:
        try:
            n_db = conn.execute(
                "SELECT COUNT(*) AS c FROM girl_media WHERE girl_id = ? AND kind = 'give'",
                (gid,)).fetchone()["c"] or 0
        except Exception:
            n_db = 0
    finally:
        conn.close()
    total = n_db + len(_scan_media_dir_give_clips(gid))
    if total <= 0:
        return {"presets": []}
    return {"presets": [{"name": "love", "count": total, "emoji": "\U0001f495"}]}


@app.get("/api/v1/girl-videos/{girl_id}")
async def girl_videos(girl_id: str, request: Request):
    """Public: get a girl's main, typing, and activity videos for the chat UI."""
    result = {"main": [], "typing": [], "activity": []}
    gid = girl_id.lower().strip()
    if not characters.visible_to(gid, _optional_user_id(request)):
        # Owner-scoped (private) girl: invisible to everyone but her owner.
        return result
    conn = _conn()
    try:
        _seed_girl_media(conn)
        rows = conn.execute(
            "SELECT kind, url FROM girl_media WHERE girl_id = ? AND kind IN ('main', 'typing', 'activity')",
            (gid,)).fetchall()
        for r in rows:
            if r["kind"] in result:
                result[r["kind"]].append(r["url"])
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Custom video requests (2026-10-07): users describe a custom video they
# want; the server attempts Sogni generation in the background. Since
# 2026-10-14 a definitively refused/failed render closes the request
# honestly (status `fallback` with a preset from her own library
# delivered instead, or `failed`), the reason recorded on the row;
# session/throttle transients park it back in `pending` with a waiting
# note. POST /api/v1/admin/custom-videos/sweep-backlog runs the one-shot
# backlog sweep over every pending request.
# POST /api/v1/custom-videos [auth] {girl_id, description} -> {id, status}
# GET  /api/v1/custom-videos [auth] -> {requests:[...]}
# GET  /api/v1/admin/custom-videos (admin) -> {requests:[...]}
# ---------------------------------------------------------------------------
class CustomVideoIn(BaseModel):
    girl_id: str
    description: str


@app.post("/api/v1/custom-videos")
def create_custom_video(body: CustomVideoIn, request: Request):
    user_id = _current_user_id(request)
    description = (body.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description required")
    if len(description) > 1000:
        raise HTTPException(status_code=400, detail="description too long")
    conn = _conn()
    try:
        character = characters.get_character(body.girl_id, user_id)
        cid = character["id"]
        req = store.save_custom_request(conn, user_id, cid, description)
        # Attempt Sogni generation in the background when configured;
        # otherwise it stays pending for manual fulfillment.
        if assist.stored_sogni_session(conn):
            custom_video.spawn_custom_generation_with_login(req["id"])
        elif config.SOGNI_API_KEY:
            custom_video.spawn_custom_generation(req["id"])
        return {"id": req["id"], "status": req["status"]}
    finally:
        conn.close()


@app.get("/api/v1/custom-videos")
def list_custom_videos(request: Request):
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        return {"requests": store.get_custom_requests(conn, user_id=user_id)}
    finally:
        conn.close()


@app.get("/api/v1/admin/custom-videos")
def admin_custom_videos(request: Request, status: str = ""):
    _require_admin(request)
    conn = _conn()
    try:
        valid = status.strip().lower() or None
        if valid and valid not in ("pending", "generating", "fulfilled",
                                   "manual", "fallback", "failed"):
            raise HTTPException(status_code=400, detail="invalid status")
        return {"requests": store.get_custom_requests(conn, status=valid)}
    finally:
        conn.close()


@app.post("/api/v1/admin/custom-videos/{request_id}/fulfill")
def admin_fulfill_custom_video(request_id: int, request: Request):
    """Mia renders one pending customer request using the saved Sogni login or API key
    and delivers the result into the exact character chat that requested it."""
    _require_admin(request)
    conn = _conn()
    has_session = False
    try:
        has_session = bool(assist.stored_sogni_session(conn))
        has_key = bool(config.SOGNI_API_KEY)
        if not (has_session or has_key):
            raise HTTPException(status_code=503, detail="sogni_login_required")
        row = conn.execute(
            "SELECT status FROM custom_video_requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="request_not_found")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail="request_not_pending")
    finally:
        conn.close()
    if has_session:
        custom_video.spawn_custom_generation_with_login(request_id)
    else:
        custom_video.spawn_custom_generation(request_id)
    return {"ok": True, "request_id": request_id, "status": "queued"}


@app.post("/api/v1/admin/custom-videos/process-pending")
def admin_process_pending_custom_videos(request: Request):
    """Have Mia process the pending customer requests in order with the
    saved Sogni login or API key. The worker serializes renders and posts each video
    into the original customer/character conversation."""
    _require_admin(request)
    conn = _conn()
    try:
        has_session = bool(assist.stored_sogni_session(conn))
        has_key = bool(config.SOGNI_API_KEY)
        if not (has_session or has_key):
            raise HTTPException(status_code=503, detail="sogni_login_required")
    finally:
        conn.close()
    try:
        count = custom_video.spawn_pending_custom_generations()
    except RuntimeError as e:
        if str(e) == "sogni_login_required":
            raise HTTPException(status_code=503, detail="sogni_login_required")
        raise
    return {"ok": True, "queued": count}


class CustomVideoSweepIn(BaseModel):
    # Requests already definitively refused (production-log evidence):
    # the sweep closes them with the recorded reason + preset fallback
    # WITHOUT rendering them again.
    known_refused_ids: list[int] = []
    known_refused_error: str = ""


@app.post("/api/v1/admin/custom-videos/sweep-backlog")
def admin_sweep_custom_video_backlog(request: Request,
                                     body: CustomVideoSweepIn | None = None):
    """One-shot backlog sweep (2026-10-14): process every pending
    custom video request under the fallback-fix semantics — one render
    attempt each; refusals close honestly with her preset fallback;
    throttle/session parks stay pending and count as parked in the
    sweep's logged report. Runs on a thread, like process-pending."""
    _require_admin(request)
    known_ids = list(body.known_refused_ids) if body else []
    known_error = body.known_refused_error if body else ""
    count = custom_video.spawn_custom_video_backlog_sweep(
        known_ids, known_error)
    return {"ok": True, "queued": count}


# ---------------------------------------------------------------------------
# Admin: give generations for public characters
# GET  /api/v1/admin/give-generations (admin) -> {generations:[...]}
# POST /api/v1/admin/give-generations/{gen_id}/approve (admin) -> {ok, generation}
# POST /api/v1/admin/give-generations/{gen_id}/delete (admin) -> {ok, generation}
# POST /api/v1/admin/give-generations/generate-daily (admin) -> {ok, queued}
# ---------------------------------------------------------------------------
@app.get("/api/v1/admin/give-generations")
def admin_list_give_generations(request: Request, status: str = "all", girl_id: str = "", day: str = ""):
    _require_admin(request)
    conn = _conn()
    try:
        return {"generations": give_gen.list_give_generations(conn, status=status, girl_id=girl_id, day=day)}
    finally:
        conn.close()


@app.post("/api/v1/admin/give-generations/{gen_id}/approve")
def admin_approve_give_generation(gen_id: int, request: Request):
    _require_admin(request)
    conn = _conn()
    try:
        res = give_gen.approve_give_generation(conn, gen_id)
        if not res:
            raise HTTPException(status_code=404, detail="generation_not_found")
        return {"ok": True, "generation": res}
    finally:
        conn.close()


@app.post("/api/v1/admin/give-generations/{gen_id}/delete")
def admin_delete_give_generation(gen_id: int, request: Request):
    _require_admin(request)
    conn = _conn()
    try:
        res = give_gen.delete_give_generation(conn, gen_id)
        if not res:
            raise HTTPException(status_code=404, detail="generation_not_found")
        return {"ok": True, "generation": res}
    finally:
        conn.close()


@app.post("/api/v1/admin/give-generations/generate-daily")
def admin_trigger_daily_give_generations(request: Request, day: str = "",
                                         include_private: bool = False):
    _require_admin(request)
    conn = _conn()
    try:
        session = assist.stored_sogni_session(conn)
        if not session:
            raise HTTPException(status_code=503, detail="sogni_login_required")
    finally:
        conn.close()
    queued = give_gen.trigger_daily_give_generations(day=day or None,
                                                     include_private=include_private)
    return {"ok": True, "queued": len(queued), "generation_ids": queued}


# ---------------------------------------------------------------------------
# Admin: upload owner media directly as give stages (2026-10-11).
# POST /api/v1/admin/give-generations/upload (admin, multipart)
#   form fields: girl_id (character id), stage (1-9), file (image or video)
# Images (jpg/png/webp) and videos (mp4/mov/webm) up to 100MB are stored
# under /generated/ and registered in the girl's give library immediately
# via the same approval path as Sogni-generated clips.
# -> {ok, id, url, media_type, generation}
# ---------------------------------------------------------------------------
_GIVE_UPLOAD_TYPES = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image",
    ".mp4": "video", ".mov": "video", ".webm": "video",
}
_GIVE_UPLOAD_MAX_BYTES = 100 * 1024 * 1024


@app.post("/api/v1/admin/give-generations/upload")
async def admin_upload_give_generation(request: Request):
    _require_admin(request)
    form = await request.form()
    girl_id = str(form.get("girl_id") or "").strip().lower()
    try:
        stage = int(form.get("stage") or 0)
    except (TypeError, ValueError):
        stage = 0
    upload = form.get("file")
    if not girl_id:
        raise HTTPException(status_code=400, detail="girl_id_required")
    if not 1 <= stage <= 9:
        raise HTTPException(status_code=400, detail="invalid_stage")
    if not upload or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="file_required")
    roster = {str(c.get("id") or "").lower()
              for c in characters.get_characters(include_private=True)}
    if girl_id not in roster:
        raise HTTPException(status_code=400, detail="unknown_character")
    ext = os.path.splitext(getattr(upload, "filename", "") or "")[1].lower()
    if ext not in _GIVE_UPLOAD_TYPES:
        raise HTTPException(status_code=400, detail="unsupported_type")
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty_file")
    if len(data) > _GIVE_UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=400, detail="file_too_large")
    import uuid as _uuid
    safe_girl = re.sub(r"[^a-z0-9_-]", "", girl_id) or "girl"
    fname = f"give-upload-{safe_girl}-stage{stage}-{_uuid.uuid4().hex[:8]}{ext}"
    gdir = config.GENERATED_DIR
    os.makedirs(gdir, exist_ok=True)
    with open(os.path.join(gdir, fname), "wb") as f:
        f.write(data)
    url = "/generated/" + fname
    from datetime import datetime, timezone
    conn = _conn()
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        gen_id = give_gen.create_give_generation(
            conn, girl_id, stage, "[owner upload]", day, status="pending")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(give_generations)")}
        if "source" in cols:
            conn.execute(
                "UPDATE give_generations SET url = ?, source = 'upload' WHERE id = ?",
                (url, gen_id))
        else:
            conn.execute(
                "UPDATE give_generations SET url = ? WHERE id = ?", (url, gen_id))
        conn.commit()
        # Same approval path as Sogni clips: marks approved and inserts the
        # girl_media row so it joins her give library immediately.
        res = give_gen.approve_give_generation(conn, gen_id)
    finally:
        conn.close()
    return {"ok": True, "id": gen_id, "url": url,
            "media_type": _GIVE_UPLOAD_TYPES[ext], "generation": res}


# ---------------------------------------------------------------------------
# Browser automation jobs for Sogni web UI generation.
# The VM browser worker polls for pending jobs, generates via Playwright,
# and uploads the result file.
# Worker auth: X-Browser-Key header must match BROWSER_WORKER_KEY env var.
# ---------------------------------------------------------------------------
def _require_browser_worker(request: Request):
    import os as _os
    key = _os.environ.get("BROWSER_WORKER_KEY", "")
    got = request.headers.get("x-browser-key", "")
    if not key or got != key:
        raise HTTPException(status_code=403, detail="forbidden")

@app.post("/api/v1/browser-jobs")
def browser_job_enqueue(request: Request, payload: dict):
    # Called by chat worker (internal). No worker key needed; chat is trusted.
    import uuid as _uuid, time as _time
    job_id = f"{int(_time.time())}-{_uuid.uuid4().hex[:8]}"
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO browser_jobs (id, kind, prompt, ref_path, model, "
            "user_id, girl_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (job_id, payload.get("kind", "video"), payload.get("prompt", ""),
             payload.get("ref_path"), payload.get("model"),
             payload.get("user_id", ""), payload.get("girl_id", "")))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "job_id": job_id}

@app.get("/api/v1/browser-jobs/poll")
def browser_job_poll(request: Request):
    _require_browser_worker(request)
    conn = _conn()
    try:
        # Claim one pending job atomically
        row = conn.execute(
            "SELECT id, kind, prompt, ref_path, model, user_id, girl_id "
            "FROM browser_jobs WHERE status = 'pending' "
            "ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return {"ok": True, "job": None}
        conn.execute(
            "UPDATE browser_jobs SET status = 'working', "
            "updated_at = datetime('now') WHERE id = ? AND status = 'pending'",
            (row["id"],))
        conn.commit()
        if conn.total_changes == 0:
            return {"ok": True, "job": None}
        return {"ok": True, "job": dict(row)}
    finally:
        conn.close()

@app.post("/api/v1/browser-jobs/{job_id}/complete")
async def browser_job_complete(request: Request, job_id: str):
    _require_browser_worker(request)
    # Multipart: file field "file", form fields "status", "error"
    form = await request.form()
    status = form.get("status", "failed")
    error = form.get("error", "")
    result_path = None
    upload = form.get("file")
    if upload and hasattr(upload, "read"):
        import os as _os
        fname = f"{job_id}.{'mp4' if 'video' in str(upload.filename) else 'png'}"
        # Save under GENERATED_DIR
        gdir = config.GENERATED_DIR
        _os.makedirs(gdir, exist_ok=True)
        result_path = _os.path.join(gdir, fname)
        with open(result_path, "wb") as f:
            f.write(await upload.read())
    conn = _conn()
    try:
        conn.execute(
            "UPDATE browser_jobs SET status = ?, result_path = ?, error = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (status, result_path, error, job_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "result_path": result_path}

@app.get("/api/v1/browser-jobs/{job_id}")
def browser_job_status(request: Request, job_id: str):
    # Called by chat worker to poll for completion. Internal.
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id, status, result_path, error FROM browser_jobs "
            "WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        return dict(row)
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Whispr Feed feed (2026-10-07): Instagram-stories-style daily posts per girl.
# Stories expire after 24h. Generation runs via daily cron (6 AM) or on-demand.
# GET  /api/v1/her-day [auth] ?girl_id= -> {posts:[...]}
# GET  /api/v1/feed [auth] -> {posts:[...]} (unified timeline, all girls)
# POST /api/v1/her-day/view [auth] {post_id} -> {ok:true}
# POST /api/v1/her-day/generate [auth] -> {generated:N} (all girls) (on-demand)
# ---------------------------------------------------------------------------
@app.get("/api/v1/her-day")
def get_her_day(request: Request, girl_id: str = ""):
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        store.cleanup_expired_her_day(conn)
        gid = (girl_id or "").strip().lower() or None
        posts = store.get_her_day_posts(conn, user_id, girl_id=gid)
        return {"posts": posts}
    finally:
        conn.close()


@app.get("/api/v1/feed")
def get_unified_feed(request: Request):
    """Unified Whispr Feed: all active posts across ALL girls, newest first,
    with girl info (name, avatar_url) attached to each post. Powers the
    common feed timeline view alongside the per-girl stories bar."""
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        store.cleanup_expired_her_day(conn)
        posts = store.get_her_day_posts(conn, user_id, girl_id=None)
        # Attach girl info (owner-scoped girls resolve only for their owner)
        girl_map = {g["id"]: g for g in characters.public_list(user_id)}
        for p in posts:
            g = girl_map.get(p.get("girl_id"), {})
            p["girl_name"] = g.get("name", p.get("girl_id", ""))
            p["girl_avatar_url"] = g.get("avatar_url", "")
            p["slug"] = store.post_slug(p.get("girl_id", ""),
                                        p.get("created_at", ""),
                                        p.get("caption", ""))
        return {"posts": posts}
    finally:
        conn.close()


@app.get("/api/v1/her-day/public")
def get_public_her_day():
    """Public Her Day feed for the landing page — NO auth required.

    Returns recent non-expired posts across all users (no user_id exposed),
    with girl info attached. Powers the public stories bar on the landing page.
    """
    conn = _conn()
    try:
        store.cleanup_expired_her_day(conn)
        posts = store.get_public_her_day_posts(conn, limit=24)
        girl_map = {g["id"]: g for g in characters.public_list()}
        for p in posts:
            g = girl_map.get(p.get("girl_id"), {})
            p["girl_name"] = g.get("name", p.get("girl_id", ""))
            p["girl_avatar_url"] = g.get("avatar_url", "")
            p["slug"] = store.post_slug(p.get("girl_id", ""),
                                        p.get("created_at", ""),
                                        p.get("caption", ""))
        return {"posts": posts}
    finally:
        conn.close()


@app.get("/p/{slug}")
def public_post_page(slug: str):
    """Shareable permalink for one Her Day moment (2026-10-17):
    server-rendered with Open Graph tags so shares unfurl with her
    photo and caption; the page funnels into her chat."""
    from fastapi.responses import HTMLResponse

    from . import post_page
    conn = _conn()
    try:
        post = store.find_public_post_by_slug(conn, slug)
        girl = None
        if post:
            girl = next((g for g in characters.public_list()
                         if g["id"] == post.get("girl_id")), None)
            if girl is None:
                girl = {"id": post.get("girl_id"),
                        "name": post.get("girl_id", "Her"),
                        "avatar_url": ""}
        return HTMLResponse(post_page.render_post_page(post, girl, slug))
    finally:
        conn.close()


def _today_time_ago(created_at: str) -> str:
    """Relative label for a her_day_posts created_at (UTC text)."""
    from datetime import datetime
    try:
        created = datetime.strptime((created_at or "")[:19],
                                    "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""
    mins = max(1, int((datetime.utcnow() - created).total_seconds() // 60))
    if mins < 60:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h"
    return f"{hrs // 24}d"


_TODAY_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0a0a12;color:#f5f5fa;
font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,sans-serif}
.today-wrap{max-width:560px;margin:0 auto;padding:20px 14px 40px}
.today-brand{font-size:1.6rem;font-weight:900;letter-spacing:1px;
background:linear-gradient(90deg,#ec4899,#9d4edd);-webkit-background-clip:text;
-webkit-text-fill-color:transparent}
.today-h1{font-size:1.35rem;margin:10px 0 2px}
.today-sub{color:#9a9ab0;margin:0 0 16px;font-size:.95rem}
.today-cta{display:block;text-align:center;text-decoration:none;color:#fff;
font-weight:700;font-size:1.05rem;padding:14px 16px;border-radius:14px;
background:linear-gradient(90deg,#ec4899,#9d4edd);margin:16px 0 6px}
.today-cta-sub{text-align:center;color:#9a9ab0;font-size:.85rem;margin:0 0 18px}
.today-card{background:#12121c;border:1px solid rgba(255,255,255,.08);
border-radius:18px;padding:14px;margin:0 0 14px}
.today-head{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.today-avatar{width:40px;height:40px;border-radius:50%;object-fit:cover;flex:none}
.today-avatar-fallback{width:40px;height:40px;border-radius:50%;flex:none;
display:flex;align-items:center;justify-content:center;font-weight:800;
color:#fff;background:linear-gradient(135deg,#ec4899,#9d4edd)}
.today-name{font-weight:700}
.today-time{color:#9a9ab0;font-size:.82rem;margin-left:auto;flex:none}
.today-media{width:100%;border-radius:12px;display:block;margin:6px 0}
.today-voice{display:flex;align-items:center;gap:8px;margin:6px 0;color:#9a9ab0}
.today-voice audio{flex:1;min-width:0}
.today-caption{margin:6px 0 2px;line-height:1.45;white-space:pre-wrap;
word-wrap:break-word}
.today-textcard .today-caption{font-size:1.12rem;line-height:1.55}
.today-empty{background:#12121c;border:1px solid rgba(255,255,255,.08);
border-radius:18px;padding:22px;text-align:center;color:#9a9ab0}
.today-foot{text-align:center;color:#66667c;font-size:.8rem;margin-top:22px}
"""


def _render_today_page(posts: list[dict]) -> str:
    """Server-render the public Today page from public Her Day posts.

    Posts arrive newest-first (store order). Media renders ONLY when the
    post actually carries a url — a post with empty media_url/voice_url
    is a pure text card with no media elements and no placeholders.
    """
    esc = _html.escape
    cards = []
    for p in posts:
        name = p.get("girl_name") or p.get("girl_id") or ""
        avatar = (p.get("girl_avatar_url") or "").strip()
        if avatar:
            avatar_html = (f'<img class="today-avatar" src="{esc(avatar, quote=True)}"'
                           f' alt="" loading="lazy">')
        else:
            initial = esc((name[:1] or "?").upper())
            avatar_html = f'<div class="today-avatar-fallback">{initial}</div>'
        media_html = ""
        media_url = (p.get("media_url") or "").strip()
        voice_url = (p.get("voice_url") or "").strip()
        if p.get("post_type") == "video" and media_url:
            media_html += (f'<video class="today-media" src="{esc(media_url, quote=True)}"'
                           ' controls playsinline preload="metadata"></video>')
        elif media_url:
            media_html += (f'<img class="today-media" src="{esc(media_url, quote=True)}"'
                           ' alt="" loading="lazy">')
        if voice_url:
            media_html += ('<div class="today-voice"><span>&#9834; voice note</span>'
                           f'<audio controls src="{esc(voice_url, quote=True)}"></audio></div>')
        text_only = " today-textcard" if not media_html else ""
        ago = _today_time_ago(p.get("created_at") or "")
        time_html = f'<span class="today-time">{esc(ago)}</span>' if ago else ""
        cards.append(
            f'<article class="today-card{text_only}">'
            f'<div class="today-head">{avatar_html}'
            f'<span class="today-name">{esc(name)}</span>{time_html}</div>'
            f"{media_html}"
            f'<p class="today-caption">{esc(p.get("caption") or "")}</p>'
            "</article>")
    if cards:
        feed_html = "\n".join(cards)
    else:
        feed_html = ('<div class="today-empty">Nothing yet today — '
                     "the girls post fresh moments every morning.</div>")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Today — Whispr Feed | Whispr AI</title>
<meta name="description" content="A glimpse into their day — fresh Her Day moments from the Whispr girls, updated daily.">
<style>{_TODAY_CSS}</style>
</head>
<body>
<main class="today-wrap">
<div class="today-brand">Whispr AI</div>
<h1 class="today-h1">Today</h1>
<p class="today-sub">A glimpse into their day — fresh moments, updated daily.</p>
<a class="today-cta" href="/">Join Whispr — get 50 free messages</a>
<p class="today-cta-sub">No card required &middot; then $0.99 for 100 more, or $9.99/mo</p>
{feed_html}
<a class="today-cta" href="/">Join Whispr — get 50 free messages</a>
<p class="today-cta-sub">No card required &middot; then $0.99 for 100 more, or $9.99/mo</p>
<p class="today-foot">Whispr AI &middot; Your private connection, always a message away.</p>
</main>
</body>
</html>"""


@app.get("/today", response_class=HTMLResponse)
def today_page():
    """Public Today page — the full Her Day feed as a standalone page.

    Read-only over the same shared day-set as GET /api/v1/her-day/public
    (no generation, no schema changes, SFW Her Day posts only — never
    Give/private-library content). Text-only posts render as clean text
    cards; media/voice appear automatically when a post carries them.
    The CTA lands on the app root, whose signup sheet opens on arrival
    for logged-out visitors (50 free messages; existing trial/plans).
    """
    conn = _conn()
    try:
        store.cleanup_expired_her_day(conn)
        posts = store.get_public_her_day_posts(conn, limit=24)
        girl_map = {g["id"]: g for g in characters.public_list()}
        for p in posts:
            g = girl_map.get(p.get("girl_id"), {})
            p["girl_name"] = g.get("name", p.get("girl_id", ""))
            p["girl_avatar_url"] = g.get("avatar_url", "")
        return HTMLResponse(_render_today_page(posts),
                            headers={"Cache-Control": "public, max-age=120"})
    finally:
        conn.close()


class HerDayViewIn(BaseModel):
    post_id: int


@app.post("/api/v1/her-day/view")
def view_her_day(body: HerDayViewIn, request: Request):
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        ok = store.mark_her_day_viewed(conn, body.post_id, user_id)
        return {"ok": ok}
    finally:
        conn.close()


class HerDayGenerateIn(BaseModel):
    girl_id: str = "chloe"


@app.post("/api/v1/her-day/generate")
def generate_her_day(body: HerDayGenerateIn, request: Request):
    """On-demand Whispr Feed generation for ALL girls (also used by the 6 AM cron).

    Every character gets her own daily feed (solo + group moments). The
    stories bar shows all girls; tap an avatar to see her day.
    """
    user_id = _current_user_id(request)
    try:
        n = her_day.generate_for_all_girls(user_id)
        return {"generated": n}
    except Exception as e:  # noqa: BLE001
        return {"generated": 0, "error": str(e)[:200]}


class HerDayInjectIn(BaseModel):
    girl_id: str
    caption: str = ""
    video_b64: str = ""
    image_b64: str = ""
    filename: str = ""
    user_id: str = ""  # empty = all users


@app.post("/api/v1/admin/her-day/inject")
def admin_her_day_inject(body: HerDayInjectIn, request: Request):
    """Admin: inject a pre-made video or image as a Whispr Feed post.

    Body: girl_id, caption, video_b64 (base64 mp4) OR image_b64 (base64
    webp/jpg/png), filename, user_id (empty = all users). Saves the file
    under GENERATED_DIR and creates one post per target user. Used to
    load hand-made Whispr Feed moments without Sogni generation.
    """
    _require_admin(request)
    import base64 as _b64
    from datetime import datetime as _dt
    gid = (body.girl_id or "").strip().lower()
    if not gid:
        raise HTTPException(status_code=400, detail="girl_id required")
    if body.video_b64:
        kind, payload = "video", body.video_b64
        exts, default_name = (".mp4",), "clip.mp4"
    elif body.image_b64:
        kind, payload = "image", body.image_b64
        exts, default_name = (".webp", ".jpg", ".jpeg", ".png"), "image.webp"
    else:
        raise HTTPException(status_code=400,
                            detail="video_b64 or image_b64 required")
    date_str = _dt.now().strftime("%Y-%m-%d")
    safe = "".join(c for c in (body.filename or default_name)
                   if c.isalnum() or c in "._-") or default_name
    if not safe.lower().endswith(exts):
        safe += exts[0]
    fname = f"herday_inject_{gid}_{date_str}_{safe}"
    os.makedirs(config.GENERATED_DIR, exist_ok=True)
    dest = os.path.join(config.GENERATED_DIR, fname)
    try:
        with open(dest, "wb") as f:
            f.write(_b64.b64decode(payload))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400,
                            detail=f"bad base64 payload: {e}") from e
    media_url = "/generated/" + fname
    conn = _conn()
    try:
        if body.user_id:
            user_ids = [body.user_id]
        else:
            user_ids = [r[0] for r in conn.execute(
                "SELECT user_id FROM users").fetchall()]
        created = 0
        for uid in user_ids:
            store.save_her_day_post(
                conn, uid, gid, post_type=kind,
                media_url=media_url, caption=body.caption or "")
            created += 1
        return {"ok": True, "created": created, "media_url": media_url,
                "users": len(user_ids)}
    finally:
        conn.close()


@app.post("/api/v1/admin/her-day/generate-all")
def admin_her_day_generate_all(request: Request):
    """Admin: run the daily Whispr Feed generation for ALL users.

    Same work the 6 AM cron performs, but callable with the admin token
    (X-Admin-Token header) instead of a per-user session. Idempotent:
    users/girls that already have today's posts are skipped
    (her_day_exists_today). Per-user failures are collected in `errors`,
    not fatal to the batch.

    Returns {ok, generated, skipped, failed, errors:[...]}.
    """
    _require_admin(request)
    try:
        report = her_day.generate_daily()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"generate_daily failed: {e}") from e
    errors = report.get("errors", [])
    return {"ok": True,
            "generated": report.get("generated", 0),
            "skipped": report.get("skipped", 0),
            "failed": len(errors),
            "errors": errors}


@app.post("/api/v1/admin/her-day/backfill-media")
def admin_her_day_backfill_media(request: Request):
    """Admin: repair TODAY's Whispr Feed posts in place.

    For days whose generation ran while image rendering was broken,
    rows were saved caption-only (media_url='') and regeneration is
    blocked by her_day_exists_today. This fills each image post from
    the shared per-day render cache (one plan-session render per
    distinct moment) on a background thread; poll the status endpoint
    for counters. Voice posts are counted, not filled — voice notes
    are behind the owner's paid-TTS gate (app/voice.py).
    """
    _require_admin(request)
    return her_day.start_backfill_today_media()


@app.get("/api/v1/admin/her-day/backfill-media/status")
def admin_her_day_backfill_media_status(request: Request):
    """Admin: backfill progress/final report (see backfill-media)."""
    _require_admin(request)
    return her_day.backfill_status()


class HerDaySetMediaIn(BaseModel):
    post_id: int
    media_url: str | None = None
    voice_url: str | None = None


@app.post("/api/v1/admin/her-day/set-media")
def admin_her_day_set_media(body: HerDaySetMediaIn, request: Request):
    """Admin: attach EXISTING media to an existing Her Day post.

    Body: post_id, and media_url and/or voice_url (at least one;
    "" clears that field). Posts are otherwise insert-only — this is
    the repair path for pointing a caption-only row at a file from
    the persistent /generated/ pool.

    Validation (Her Day stays SFW): each URL must name a file this app
    serves from GENERATED_DIR under /generated/ that actually exists
    there, and must not be registered in girl_media (the give
    library). Violations are 400; an unknown post_id is 404.
    Returns {ok, post} with the updated row.
    """
    _require_admin(request)
    if body.media_url is None and body.voice_url is None:
        raise HTTPException(status_code=400,
                            detail="nothing_to_set: pass media_url "
                                   "and/or voice_url")
    conn = _conn()
    try:
        for field, url in (("media_url", body.media_url),
                           ("voice_url", body.voice_url)):
            if url is None:
                continue
            err = her_day.validate_her_day_media_url(conn, url)
            if err:
                raise HTTPException(status_code=400,
                                    detail=f"{field}: {err}")
        post = store.set_her_day_post_media(
            conn, body.post_id,
            media_url=body.media_url, voice_url=body.voice_url)
        if post is None:
            raise HTTPException(status_code=404, detail="post_not_found")
        return {"ok": True, "post": post}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
her_day_delete.register_her_day_delete(app, _require_admin, _conn); push_routes.register(app, _current_user_id)

# Admin: girl media library (2026-10-06)
# Manage gives, mains, typing mains, activities per character.
# Token-gated via X-Admin-Token (same as other admin endpoints).
# ---------------------------------------------------------------------------

class GirlMediaIn(BaseModel):
    girl_id: str
    kind: str  # give | main | typing | activity
    url: str
    preset: str = ""


@app.get("/api/v1/admin/girl-media")
async def admin_girl_media_list(request: Request, girl_id: str = ""):
    _require_admin(request)
    conn = _conn()
    try:
        if girl_id:
            rows = conn.execute(
                "SELECT * FROM girl_media WHERE girl_id = ? ORDER BY kind, id",
                (girl_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM girl_media ORDER BY girl_id, kind, id").fetchall()
        return {"media": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/v1/admin/girl-media")
async def admin_girl_media_add(body: GirlMediaIn, request: Request):
    _require_admin(request)
    if body.kind not in ("give", "main", "typing", "activity"):
        raise HTTPException(status_code=400, detail="invalid kind")
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset) VALUES (?, ?, ?, ?)",
            (body.girl_id.lower().strip(), body.kind,
             body.url.strip(), body.preset.lower().strip() or None))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}
    finally:
        conn.close()



class GirlMediaUpdateIn(BaseModel):
    stage: int = None
    preset: str = None


@app.patch("/api/v1/admin/girl-media/{media_id}")
async def admin_girl_media_update(media_id: int, body: GirlMediaUpdateIn, request: Request):
    _require_admin(request)
    updates = []
    params = []
    if body.stage is not None:
        if body.stage < 0 or body.stage > 9:
            raise HTTPException(status_code=400, detail="stage must be 0-9")
        updates.append("stage = ?")
        params.append(body.stage)
    if body.preset is not None:
        updates.append("preset = ?")
        params.append(body.preset.lower().strip() or None)
    if not updates:
        raise HTTPException(status_code=400, detail="nothing to update")
    params.append(media_id)
    conn = _conn()
    try:
        conn.execute(f"UPDATE girl_media SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/v1/admin/girl-media/{media_id}")
async def admin_girl_media_delete(media_id: int, request: Request):
    _require_admin(request)
    conn = _conn()
    try:
        conn.execute("DELETE FROM girl_media WHERE id = ?", (media_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# Seed hardcoded clips into DB on first admin list (one-time migration).

# Give clips - curated Whispr library (videos only)
GIVE_CLIPS = {
    ("chloe", "give"): [
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_1e67ecdd0b6dd10e.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_c917d49a514c5329.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_66c98753a458c922.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_685e3e81d92264e3.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_5b254cf0206883c7.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_7948798a732e5b08.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_b73f6785d7836c9f.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_c2f202118151d0f3.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_f3d2cbd7bfe0cb17.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_047cbcdc1f355d69.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_203ac67b50de4957.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/chloe_fc09897b4cc6bbcc.mp4", "media_type": "video"},
    ],
    ("mara", "give"): [
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_6376c4a666c4d00a.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_758a668ad2735c7e.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_6bc4d10daeb3c44e.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_f9bb24d45d2f0c9a.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_7a5f013092b3b1aa.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_c7d7dc387952af4b.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_70cfa89478fa8c07.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_ff149c86e581d45f.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_b5761852a13238e4.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_8929d9f7ebb323db.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_91b49687da47b47b.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_84f5543c4fb314ed.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_9ef2c129967e2543.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_0269893f79f3f04c.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_1cc18f36e3404d19.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_135b22ec9435330f.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_bf2d4e6a22542650.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_1a10870031b9ee4f.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_729db445657050ba.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/mara_b3c2818d54075336.mp4", "media_type": "video"},
    ],
    ("isabella", "give"): [
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_0f7bb9353e2afac6.mov", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_ad39918b1ccd986c.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_0d66e50cf0a582a2.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_04a933a64d71897b.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_1b08decc86290589.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_7251e13f78b044e2.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_7f3e2759a54a6903.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_1058ffec56f7d37a.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_ba0cc70c4e4f32de.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_982aaaabc5b710d2.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_a5994f3f320bb198.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_6cbb2806075dddab.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_f166d83c367aa6ba.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_fe164382e465c9fc.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_a7489e13f772766b.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_c2779ed79b936f93.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_28c528a111ede543.mp4", "media_type": "video"},
        {"url": "https://keyhole-latest-production.up.railway.app/media/files/isabella_4aded84b5d08ba1c.mp4", "media_type": "video"},
    ],
}

def _seed_girl_media(conn):
    # Seed love clips from PRESET_CLIPS (original behavior)
    existing = conn.execute("SELECT COUNT(*) AS c FROM girl_media").fetchone()
    if not existing or existing["c"] == 0:
        for (girl, preset), clips in PRESET_CLIPS.items():
            for clip in clips:
                conn.execute(
                    "INSERT INTO girl_media (girl_id, kind, url, preset) VALUES (?, 'give', ?, ?)",
                    (girl, clip["url"], preset))
    conn.commit()



class GrantMonthIn(BaseModel):
    user_id: str
    tier: str  # single tier: "basic"


@app.get("/api/v1/admin/accounts")
async def admin_accounts_list(request: Request):
    _require_admin(request)
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT user_id, name, email, tier, messages_remaining,
                      voice_notes_remaining, email_verified_at, created_at,
                      vip_expires_at, vip_months_earned
               FROM users ORDER BY created_at DESC LIMIT 200"""
        ).fetchall()
        return {"accounts": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/v1/admin/grant-month")
async def admin_grant_month(body: GrantMonthIn, request: Request):
    """Grant a free month at any plan level."""
    _require_admin(request)
    if body.tier != "basic":
        raise HTTPException(status_code=400, detail="invalid tier")
    # Tier message allocations (single tier, 2026-10-08)
    tier_msgs = {"basic": 3000}
    tier_voice = {"basic": 150}
    conn = _conn()
    try:
        from datetime import datetime, timedelta
        user = conn.execute(
            "SELECT vip_expires_at FROM users WHERE user_id = ?",
            (body.user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="user not found")
        now = datetime.utcnow()
        current_exp = None
        if user["vip_expires_at"]:
            try:
                current_exp = datetime.fromisoformat(user["vip_expires_at"])
            except Exception:
                pass
        base = max(current_exp, now) if current_exp and current_exp > now else now
        new_exp = base + timedelta(days=30)
        conn.execute(
            """UPDATE users SET tier = ?, vip_expires_at = ?,
                      messages_remaining = messages_remaining + ?,
                      voice_notes_remaining = voice_notes_remaining + ?
               WHERE user_id = ?""",
            (body.tier, new_exp.isoformat(),
             tier_msgs[body.tier], tier_voice[body.tier], body.user_id))
        conn.commit()
        return {"ok": True, "tier": body.tier,
                "vip_expires_at": new_exp.isoformat()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Referral program "Whispr VIP Access" (2026-10-06)
# 1 referral = 1 month VIP · 6 referrals = 1 year VIP
# ---------------------------------------------------------------------------

import secrets as _secrets

def _get_or_create_referral_code(conn, user_id: str) -> str:
    row = conn.execute(
        "SELECT referral_code FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row and row["referral_code"]:
        return row["referral_code"]
    code = _secrets.token_urlsafe(8)[:10]
    try:
        conn.execute(
            "UPDATE users SET referral_code = ? WHERE user_id = ?", (code, user_id))
        conn.commit()
    except Exception:
        # collision — retry once
        code = _secrets.token_urlsafe(8)[:10]
        conn.execute(
            "UPDATE users SET referral_code = ? WHERE user_id = ?", (code, user_id))
        conn.commit()
    return code


def _evaluate_referral_reward(conn, referrer_id: str) -> dict:
    """Count active referrals; grant 30 days per referral, 365 at 6."""
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ? AND status = 'active'",
        (referrer_id,)).fetchone()
    active = rows["c"] if rows else 0
    days = 0
    if active >= 6:
        days = 365
    elif active >= 1:
        days = 30 * active
    result = {"active_referrals": active, "days_granted": 0}
    if days > 0:
        # Extend vip_expires_at
        row = conn.execute(
            "SELECT vip_expires_at, vip_months_earned FROM users WHERE user_id = ?",
            (referrer_id,)).fetchone()
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        current_exp = None
        if row and row["vip_expires_at"]:
            try:
                current_exp = datetime.fromisoformat(row["vip_expires_at"])
            except Exception:
                pass
        base = max(current_exp, now) if current_exp and current_exp > now else now
        new_exp = base + timedelta(days=days)
        months = (row["vip_months_earned"] or 0) + (12 if active >= 6 else active)
        conn.execute(
            "UPDATE users SET vip_expires_at = ?, vip_months_earned = ?, tier = 'basic' WHERE user_id = ?",
            (new_exp.isoformat(), months, referrer_id))
        conn.commit()
        result["days_granted"] = days
        result["vip_expires_at"] = new_exp.isoformat()
    return result


@app.get("/api/v1/referral/me")
async def referral_me(request: Request):
    """Get my referral code, link, and progress."""
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        code = _get_or_create_referral_code(conn, user_id)
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ? AND status = 'active'",
            (user_id,)).fetchone()
        active = rows["c"] if rows else 0
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ? AND status = 'pending'",
            (user_id,)).fetchone()
        user = conn.execute(
            "SELECT vip_expires_at, vip_months_earned FROM users WHERE user_id = ?",
            (user_id,)).fetchone()
        base_url = config.PUBLIC_APP_URL or "https://lockeddoor.ai"
        return {
            "referral_code": code,
            "referral_link": f"{base_url}/?ref={code}",
            "active_referrals": active,
            "pending_referrals": pending["c"] if pending else 0,
            "vip_expires_at": user["vip_expires_at"] if user else None,
            "vip_months_earned": user["vip_months_earned"] if user else 0,
            "next_milestone": 6 if active < 6 else None,
        }
    finally:
        conn.close()


class ReferralTrackIn(BaseModel):
    referral_code: str


@app.post("/api/v1/referral/track")
async def referral_track(body: ReferralTrackIn, request: Request):
    """Called at signup: link new user to referrer. Creates pending referral."""
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        # Find referrer by code
        ref = conn.execute(
            "SELECT user_id FROM users WHERE referral_code = ?",
            (body.referral_code,)).fetchone()
        if not ref:
            raise HTTPException(status_code=404, detail="invalid referral code")
        referrer_id = ref["user_id"]
        if referrer_id == user_id:
            raise HTTPException(status_code=400, detail="cannot refer yourself")
        # Record referred_by on new user
        conn.execute(
            "UPDATE users SET referred_by = ? WHERE user_id = ?",
            (body.referral_code, user_id))
        # Create pending referral (ignore if already tracked)
        try:
            conn.execute(
                "INSERT INTO referrals (referrer_id, referred_id, status) VALUES (?, ?, 'pending')",
                (referrer_id, user_id))
        except Exception:
            pass  # already tracked
        conn.commit()
        return {"ok": True, "referrer": referrer_id}
    finally:
        conn.close()


def check_referral_activation(conn, user_id: str):
    """Call after each user message. If referred user hit 3 messages and is
    email-verified, mark referral active and evaluate referrer reward."""
    ref = conn.execute(
        "SELECT referrer_id, messages_sent, status FROM referrals WHERE referred_id = ?",
        (user_id,)).fetchone()
    if not ref or ref["status"] == "active":
        return None
    # Count messages sent by this user
    new_count = (ref["messages_sent"] or 0) + 1
    conn.execute(
        "UPDATE referrals SET messages_sent = ? WHERE referred_id = ?",
        (new_count, user_id))
    # Check email verified
    user = conn.execute(
        "SELECT email_verified FROM users WHERE user_id = ?", (user_id,)).fetchone()
    verified = user and user["email_verified"]
    if new_count >= 3 and verified:
        conn.execute(
            "UPDATE referrals SET status = 'active' WHERE referred_id = ?",
            (user_id,))
        conn.commit()
        reward = _evaluate_referral_reward(conn, ref["referrer_id"])
        return {"activated": True, "reward": reward}
    conn.commit()
    return {"activated": False, "messages_sent": new_count}


# ---------------------------------------------------------------------------
# Assistant capabilities ("her hands") — tier-gated, owner-only until rollout.
# Credential rule: user logins NEVER touch these endpoints. Gmail OAuth tokens
# live on the user's device; the browser calls Google directly (js/assist.js).
# ---------------------------------------------------------------------------

class AssistSearchIn(BaseModel):
    query: str


class AssistReminderIn(BaseModel):
    text: str = ""
    remind_at: str = ""  # optional ISO UTC; server parses `text` when empty


def _assist_user(request: Request):
    """Authed user gated for assist; 403 when not enabled for them."""
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        user = store.get_or_create_user(conn, user_id)
        if not assist.enabled_for(user_id, user.get("tier") or "free"):
            raise HTTPException(status_code=403, detail="assist_not_enabled")
        return user_id, user, conn
    except HTTPException:
        conn.close()
        raise


@app.get("/api/v1/assist/capabilities")
def assist_capabilities(request: Request):
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        user = store.get_or_create_user(conn, user_id)
        tier = user.get("tier") or "free"
        enabled = assist.enabled_for(user_id, tier)
        caps = (assist.effective_caps(user_id, tier) if enabled
                else assist.caps_for("free"))
        return {"enabled": enabled, "tier": tier, "capabilities": caps}
    finally:
        conn.close()


@app.get("/api/v1/assist/config")
def assist_config(request: Request):
    _current_user_id(request)  # login required; the client id is public anyway
    return {"google_oauth_client_id":
            os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")}


@app.post("/api/v1/assist/search")
def assist_search(body: AssistSearchIn, request: Request):
    user_id, user, conn = _assist_user(request)
    try:
        caps = assist.caps_for(user.get("tier") or "free")
        q = (body.query or "").strip()[:300]
        if not q:
            raise HTTPException(status_code=400, detail="empty_query")
        if not assist.use_search(conn, user_id, caps):
            raise HTTPException(status_code=402, detail="lookups_exhausted")
        res = assist.web_search(q)
        if not res:
            raise HTTPException(status_code=502, detail="lookup_failed")
        return res
    finally:
        conn.close()


@app.get("/api/v1/assist/reminders")
def assist_list_reminders(request: Request):
    user_id, user, conn = _assist_user(request)
    try:
        return {"reminders": assist.list_reminders(conn, user_id)}
    finally:
        conn.close()


@app.post("/api/v1/assist/reminders")
def assist_create_reminder(body: AssistReminderIn, request: Request):
    user_id, user, conn = _assist_user(request)
    try:
        caps = assist.caps_for(user.get("tier") or "free")
        if assist.active_reminder_count(conn, user_id) >= caps["reminders"]:
            raise HTTPException(status_code=402, detail="reminders_full")
        remind_at = (body.remind_at or "").strip()
        text = (body.text or "").strip()
        if not remind_at:
            remind_at, text = assist.parse_reminder(text)
        if not remind_at:
            raise HTTPException(status_code=400, detail="no_time_understood")
        rid = assist.set_reminder(conn, user_id, "chloe", text[:280],
                                  remind_at)
        return {"id": rid, "remind_at": remind_at, "text": text}
    finally:
        conn.close()


@app.delete("/api/v1/assist/reminders/{reminder_id}")
def assist_cancel_reminder(reminder_id: int, request: Request):
    user_id, user, conn = _assist_user(request)
    try:
        if not assist.cancel_reminder(conn, user_id, reminder_id):
            raise HTTPException(status_code=404, detail="not_found")
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/v1/assist/reminders/due")
def assist_due_reminders(request: Request):
    # Polled by the app; returns due reminders and marks them delivered.
    user_id, user, conn = _assist_user(request)
    try:
        return {"reminders": assist.due_reminders(conn, user_id)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Secure Sogni login box (2026-10-09, owner-directed): the "Enter Sogni
# login" button in an assistant thread opens an in-app form; the pair
# lands here over his authenticated session instead of in chat text.
# Owner-only, assistant threads only, rate-limited by the shared
# throttle in app.assist. The password drives the one server-side
# SDK sign-in and is then discarded — never stored, never logged,
# never echoed back; only the session token pair is kept.
# ---------------------------------------------------------------------------

class AssistSogniLoginIn(BaseModel):
    username: str
    password: str
    character: str = "hannah"


@app.post("/api/v1/assist/sogni-login")
def assist_sogni_login(body: AssistSogniLoginIn, request: Request):
    user_id = _current_user_id(request)  # 401 when not signed in
    if user_id not in assist._owner_ids():
        raise HTTPException(status_code=403, detail="owner_only")
    character = (body.character or "").strip().lower()
    if not assist.is_assistant_character(character):
        raise HTTPException(status_code=400,
                            detail="not_an_assistant_thread")
    username = (body.username or "").strip()
    password = body.password or ""
    if not username or not password:
        raise HTTPException(status_code=400,
                            detail="missing_credentials")
    if len(username) > 120 or len(password) > 256:
        raise HTTPException(status_code=400,
                            detail="credential_too_long")
    remaining = assist.sogni_login_lock_remaining(user_id)
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail={"error": "too_many_attempts",
                    "retry_after_seconds": remaining})
    conn = _conn()
    try:
        session = assist.perform_sogni_login(
            conn, user_id, (username, password), caller="login_box")
        if session is None:
            # Same fixed words as the chat intake failure, in-thread.
            store.log_turn(conn, user_id, character,
                           assist.sogni_login_failed_reply(character),
                           character)
            return {"ok": False, "error": "login_failed"}
        saved_reply = assist.sogni_login_saved_reply(character)
        store.log_turn(conn, user_id, character, saved_reply, character)
        return {"ok": True, "reply": saved_reply}
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# Custom-companion submissions (2026-10-01): approval-only. Any customer
# can create a companion — design the companion you want. Images are AI-reviewed (nudity / 18+)
# before the owner ever sees them; nothing goes live without owner approval.
# ---------------------------------------------------------------------------

@app.get("/api/v1/submissions/policy")
def submission_policy(request: Request):
    _current_user_id(request)
    return {"policy": submit.SUBMISSION_POLICY}


@app.post("/api/v1/submissions")
def create_submission(body: SubmissionIn, request: Request):
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        user = store.get_or_create_user(conn, user_id)
        if (user.get("tier") or "free") != "basic":
            raise HTTPException(status_code=402,
                                detail="paid_tier_required")
        try:
            return submit.create_submission(conn, user_id,
                                            body.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


# Companion creator (2026-10-01): ported from the Sorority-house companion
# creator. Paid members create a custom companion with 1-3 REAL photos;
# 18+ is a user attestation (no photo scan) and the character goes live
# immediately.
@app.post("/api/v1/companions/create")
def create_companion(body: CompanionCreateIn, request: Request):
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        user = store.get_or_create_user(conn, user_id)
        if (user.get("tier") or "free") != "basic":
            raise HTTPException(status_code=402,
                                detail="paid_tier_required")
        try:
            comp = creator.create_companion(conn, user_id,
                                            body.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        characters.bust_cache()
        return {"ok": True, "companion": comp}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public companion gallery (2026-10-15): browseable cards for creator-made
# companions whose creators opted in (gallery_public). Private by default;
# only opted-in + enabled characters ever appear here. The gallery is the
# creator funnel: the empty state still features the create-your-own CTA.
# ---------------------------------------------------------------------------

def _gallery_cards() -> list[dict]:
    conn = _conn()
    try:
        return store.list_gallery_characters(conn)
    finally:
        conn.close()


@app.get("/api/v1/gallery")
def gallery_list():
    return {"companions": _gallery_cards()}


@app.post("/api/v1/companions/{companion_id}/gallery")
def set_companion_gallery(companion_id: str, body: GalleryVisibilityIn,
                          request: Request):
    """Owner-only: flip a companion's public-gallery opt-in. Only the
    character's creator may publish or unpublish her."""
    user_id = _current_user_id(request)
    cid = (companion_id or "").strip().lower()
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT owner_user_id FROM local_characters WHERE id = ?",
            (cid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        if (row["owner_user_id"] or "") != user_id:
            raise HTTPException(status_code=403, detail="not_your_companion")
        store.set_character_gallery_public(conn, cid, body.public)
    finally:
        conn.close()
    characters.bust_cache()
    return {"ok": True, "id": cid, "gallery_public": bool(body.public)}


_GALLERY_CSS = """
:root{--pink:#ff2d78;--purple:#a855f7;--grad:linear-gradient(135deg,#ff2d78,#a855f7);
--bg:#0a0a12;--bg-soft:#12121c;--text:#fff;--sub:#8e8e93;--border:#2a2a3a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1060px;margin:0 auto;padding:28px 16px 56px}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px}
.brand{font-size:22px;font-weight:800}.brand span{color:var(--pink)}
a.back{color:var(--sub);text-decoration:none;font-size:14px}
h1{font-size:30px;margin:18px 0 4px}.lede{color:var(--sub);margin:0 0 22px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
.card{display:flex;flex-direction:column;background:var(--bg-soft);
border:1px solid var(--border);border-radius:16px;padding:10px;text-align:center}
.card img{width:100%;aspect-ratio:3/4;border-radius:12px;object-fit:cover;background:#2a2a3a}
.name{font-size:16px;font-weight:700;margin-top:9px}
.tag{color:#c4b5fd;font-size:12.5px;margin-top:3px}
.snip{font-size:12px;color:var(--sub);line-height:1.4;margin:7px 0 10px;flex:1}
.by{font-size:11px;color:var(--sub);margin-bottom:8px}
.chat{display:block;background:var(--grad);color:#fff;text-decoration:none;
border-radius:8px;padding:9px 0;font-size:13px;font-weight:700}
.cta{margin-top:30px;background:var(--bg-soft);border:1px solid var(--border);
border-radius:16px;padding:22px;text-align:center}
.cta p{color:var(--sub);margin:6px 0 14px}
.btn{display:inline-block;background:var(--grad);color:#fff;text-decoration:none;
padding:11px 26px;border-radius:999px;font-weight:700;font-size:14px}
.empty{border:1px dashed var(--border);border-radius:16px;padding:34px 18px;
text-align:center;color:var(--sub)}
"""


def _gallery_page_html(cards: list[dict]) -> str:
    parts = [
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<title>Community Gallery — Whispr AI</title>",
        "<meta name=\"description\" content=\"Companions created by Whispr "
        "members. Browse the gallery, or create your own.\">",
        f"<style>{_GALLERY_CSS}</style></head><body><div class=\"wrap\">",
        "<div class=\"top\"><div class=\"brand\">Whispr <span>AI</span></div>",
        "<a class=\"back\" href=\"/\">← Back to Whispr</a></div>",
        "<h1>Community Gallery</h1>",
        "<p class=\"lede\">Companions imagined and created by Whispr "
        "members. Pick one and start talking.</p>",
    ]
    if cards:
        parts.append("<div class=\"grid\">")
        for c in cards:
            name = _html.escape(c.get("name") or "")
            tag = _html.escape(c.get("tagline") or "")
            snip = _html.escape(c.get("persona_snippet") or "")
            by = _html.escape(c.get("creator_name") or "")
            avatar = _html.escape(c.get("avatar_url") or "", quote=True)
            chat = _html.escape(c.get("chat_url") or "/", quote=True)
            parts.append("<div class=\"card\">")
            if avatar:
                parts.append(f"<img src=\"{avatar}\" alt=\"{name}\" loading=\"lazy\">")
            parts.append(f"<div class=\"name\">{name}</div>")
            if tag:
                parts.append(f"<div class=\"tag\">{tag}</div>")
            if snip:
                parts.append(f"<div class=\"snip\">{snip}</div>")
            if by:
                parts.append(f"<div class=\"by\">by {by}</div>")
            parts.append(
                f"<a class=\"chat\" href=\"{chat}\">Chat on Whispr</a></div>")
        parts.append("</div>")
    else:
        parts.append(
            "<div class=\"empty\">No community companions yet — "
            "yours could be the first.</div>")
    parts.append(
        "<div class=\"cta\"><div style=\"font-size:19px;font-weight:700\">"
        "Create your own companion</div>"
        "<p>Design her personality, upload her photos — she goes live "
        "instantly, and you choose whether she joins this gallery.</p>"
        "<a class=\"btn\" href=\"/?create=1\">✨ Create a Companion</a></div>")
    parts.append("</div></body></html>")
    return "".join(parts)


@app.get("/gallery", response_class=HTMLResponse, include_in_schema=False)
def gallery_page():
    return _gallery_page_html(_gallery_cards())


@app.get("/api/v1/submissions/mine")
def my_submissions(request: Request):
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, name, status, verified_fictional, ai_review, "
            "created_at, reviewed_at FROM submissions "
            "WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)).fetchall()
        out = []
        for r in rows:
            try:
                review = json.loads(r["ai_review"] or "{}")
            except Exception:
                review = {}
            out.append({
                "id": r["id"], "name": r["name"], "status": r["status"],
                "verified_fictional": bool(r["verified_fictional"]),
                "review_notes": review.get("notes", ""),
                "created_at": r["created_at"],
                "reviewed_at": r["reviewed_at"],
            })
        return {"submissions": out}
    finally:
        conn.close()


@app.get("/api/v1/admin/diag/image-review")
def diag_image_review(request: Request):
    """Admin-only diagnostic: does the Gemini image-review call work?

    Sends a tiny embedded test image to the same model/endpoint the
    submission review uses. Returns timing + outcome only — never the
    API key, never image bytes.
    """
    _require_admin(request)
    t0 = time.time()
    key_set = bool(config.GEMINI_API_KEY)
    if not key_set:
        return {"key_set": False, "duration_s": 0.0, "got_response": False,
                "http_status": None, "error": "no_key"}
    tiny_png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
        "hQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    payload = {"contents": [{"role": "user", "parts": [
        {"text": "Reply with ONLY this JSON: "
                 "[{\"nudity\": \"no\", \"all_adult\": \"yes\", "
                 "\"note\": \"diag test\"}]"},
        {"inline_data": {"mime_type": "image/png", "data": tiny_png_b64}},
    ]}], "generationConfig": {"temperature": 0.0, "maxOutputTokens": 200}}
    req = urllib.request.Request(
        submit._GEMINI_URL, data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": config.GEMINI_API_KEY},
    )
    http_status, error, got = None, None, False
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            http_status = resp.status
            body = resp.read().decode("utf-8")
            got = bool(json.loads(body).get("candidates"))
    except urllib.error.HTTPError as e:
        http_status, error = e.code, "http_error"
    except Exception as e:  # noqa: BLE001 — diagnostic only
        error = type(e).__name__
    return {"key_set": True, "duration_s": round(time.time() - t0, 2),
            "got_response": got, "http_status": http_status, "error": error}


@app.get("/api/v1/admin/submissions")
def admin_list_submissions(request: Request, status: str = ""):
    _require_admin(request)
    conn = _conn()
    try:
        q = ("SELECT s.*, u.email AS user_email FROM submissions s "
             "LEFT JOIN users u ON u.user_id = s.user_id")
        params: list = []
        if status:
            q += " WHERE s.status = ?"
            params.append(status)
        q += " ORDER BY s.created_at DESC"
        rows = conn.execute(q, params).fetchall()
        return {"submissions": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/v1/admin/submissions/{sub_id}/approve")
def admin_approve_submission(sub_id: str, body: SubmissionApproveIn,
                             request: Request):
    _require_admin(request)
    conn = _conn()
    try:
        try:
            result = submit.approve_submission(
                conn, sub_id, body.verified_fictional)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        characters.bust_cache()
        return result
    finally:
        conn.close()


@app.post("/api/v1/admin/submissions/{sub_id}/reject")
def admin_reject_submission(sub_id: str, body: SubmissionRejectIn,
                            request: Request):
    _require_admin(request)
    conn = _conn()
    try:
        try:
            return submit.reject_submission(conn, sub_id, body.reason)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.get("/api/v1/media-requests/{req_id}")
def poll_media_request(req_id: int, request: Request):
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        req = store.get_media_request(conn, user_id, req_id)
        if not req:
            raise HTTPException(status_code=404, detail="not_found")
        out = {"id": req["id"], "status": req["status"],
               "kind": req["kind"], "media": req["media"],
               "girl_id": req["girl_id"], "character_id": req["girl_id"],
               "created_at": req["created_at"]}
        if req["status"] == "ready":
            out["result_url"] = req["result_url"]
        elif req["status"] == "failed":
            character = characters.get_character(req["girl_id"], user_id)
            out["failed_line"] = _request_failed_line(character)
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Admin: review media requests (prompts) + credit the $5.99 overage pack
# (the Shopify webhook calls this once product + webhook ship).
# ---------------------------------------------------------------------------
@app.get("/api/v1/admin/media-requests")
def admin_media_requests(request: Request, limit: int = 100):
    _require_admin(request)
    conn = _conn()
    try:
        return {"requests": store.recent_media_requests(conn, limit)}
    finally:
        conn.close()


@app.post("/api/v1/admin/grant-messages")
def admin_grant_messages(body: GrantMessagesIn, request: Request):
    """Owner/support tool: add test or courtesy messages to an account."""
    _require_admin(request)
    uid = (body.user_id or "").strip()
    n = max(1, min(int(body.count or 0), 10000))
    if not uid:
        raise HTTPException(status_code=400, detail="user_id required")
    conn = _conn()
    try:
        conn.execute(
            """UPDATE users SET messages_remaining = messages_remaining + ?,
                                 updated_at = datetime('now')
               WHERE user_id = ?""",
            (n, uid),
        )
        conn.commit()
        row = conn.execute(
            "SELECT messages_remaining FROM users WHERE user_id = ?",
            (uid,)).fetchone()
        return {"ok": True, "user_id": uid,
                "messages_remaining": row["messages_remaining"] if row else 0}
    finally:
        conn.close()


@app.post("/api/v1/admin/users/{user_id}/reset-character")
def admin_reset_user_character(user_id: str, body: ResetCharacterIn,
                               request: Request):
    """Owner/support tool: reset ONE user's relationship with ONE
    character — deletes their conversation turns, typed memories and
    rolling digest for that character only, so the next chat starts
    from a clean first meeting. Nothing else about the account (other
    characters, balances, trust with other girls) is touched."""
    _require_admin(request)
    uid = (user_id or "").strip()
    cid = (body.character_id or "").strip().lower()
    if not uid or not cid:
        raise HTTPException(status_code=400,
                            detail="user_id and character_id required")
    conn = _conn()
    try:
        removed_memories = store.delete_all_memories(conn, uid, cid)
        cur = conn.execute(
            "DELETE FROM memory_digest WHERE user_id = ? AND character_id = ?",
            (uid, cid),
        )
        conn.commit()
        return {"ok": True, "user_id": uid, "character_id": cid,
                "memories_removed": removed_memories,
                "digests_removed": cur.rowcount}
    finally:
        conn.close()


@app.post("/api/v1/admin/users/{user_id}/seed-message")
def admin_seed_message(user_id: str, body: SeedMessageIn,
                       request: Request):
    """Owner/support tool: place ONE message from a character into a
    user's chat, the way the verify-time welcome is seeded — it lands
    as that character's first turn in her history for the character."""
    _require_admin(request)
    uid = (user_id or "").strip()
    cid = (body.character_id or "").strip()
    if not uid or not cid or not body.text:
        raise HTTPException(status_code=400,
                            detail="user_id, character_id and text required")
    conn = _conn()
    try:
        store.log_turn(conn, uid, cid, body.text, cid)
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/v1/admin/credit-overage")
def admin_credit_overage(body: CreditOverageIn, request: Request):
    _require_admin(request)
    if not (body.user_id or "").strip():
        raise HTTPException(status_code=400, detail="user_id required")
    conn = _conn()
    try:
        pool = store.credit_overage_pack(conn, body.user_id.strip())
        return {"ok": True, "user_id": body.user_id.strip(),
                "overage_regular": pool["overage_regular_videos"],
                "overage_give": pool["overage_give_videos"]}
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
        # Welcome greeting (2026-09-30): if she hasn't heard from him yet,
        # Chloe opens with her line the moment the email is verified — it
        # lands as the first turn in his chat history when the app loads.
        if not store.recent_turns(conn, user_id, 1, "chloe"):
            store.log_turn(conn, user_id, "chloe", onboarding.greeting_line({"id": "chloe", "name": "Chloe"}), "chloe")
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



class ForgotPasswordIn(BaseModel):
    email: str


@app.post("/api/v1/auth/forgot-password")
async def auth_forgot_password(body: ForgotPasswordIn, request: Request):
    """Send password reset email. Always returns ok (no email enumeration)."""
    if auth.rate_limited("forgot:" + _client_ip(request)):
        raise HTTPException(status_code=429, detail="too_many_attempts")
    email = auth.normalize_email(body.email)
    conn = _conn()
    try:
        user = store.get_user_by_email(conn, email)
        if user:
            token = "pr_" + auth.new_token(24)
            from datetime import datetime, timedelta
            expires = (datetime.utcnow() + timedelta(hours=2)).isoformat()
            conn.execute(
                "INSERT INTO password_reset_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
                (token, user["user_id"], expires))
            conn.commit()
            # Send reset email via Resend
            try:
                reset_url = f"{config.PUBLIC_APP_URL}/?reset_token={token}"
                import urllib.request as _urlreq, json as _json
                _req = _urlreq.Request(
                    "https://api.resend.com/emails",
                    data=_json.dumps({
                        "from": config.MAIL_FROM,
                        "to": [email],
                        "subject": "Reset your Whispr password",
                        "html": f"<p>Click below to reset your password (expires in 2 hours):</p><p><a href='{reset_url}'>{reset_url}</a></p>",
                    }).encode(),
                    headers={"Authorization": f"Bearer {config.RESEND_API_KEY}",
                             "Content-Type": "application/json"},
                )
                _urlreq.urlopen(_req, timeout=15)
            except Exception as e:
                print(f"[auth] reset email failed for {email}: {e}", flush=True)
        return {"ok": True}
    finally:
        conn.close()


class ResetPasswordIn(BaseModel):
    token: str
    new_password: str


@app.post("/api/v1/auth/reset-password")
async def auth_reset_password(body: ResetPasswordIn):
    """Reset password with token from email."""
    if not auth.valid_password(body.new_password):
        raise HTTPException(status_code=400, detail="password must be 8-128 characters")
    conn = _conn()
    try:
        from datetime import datetime
        row = conn.execute(
            "SELECT user_id, expires_at, used FROM password_reset_tokens WHERE token = ?",
            (body.token,)).fetchone()
        if not row or row["used"]:
            raise HTTPException(status_code=400, detail="invalid_or_expired_link")
        if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
            raise HTTPException(status_code=400, detail="invalid_or_expired_link")
        # Update password
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE user_id = ?",
            (auth.hash_password(body.new_password), row["user_id"]))
        conn.execute(
            "UPDATE password_reset_tokens SET used = 1 WHERE token = ?",
            (body.token,))
        conn.commit()
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
        summary = _user_summary(user)
        try:
            from datetime import datetime as _mdt, timedelta as _mtd
            from zoneinfo import ZoneInfo as _MZI
            try:
                _mtz = _MZI(user.get("timezone") or "America/New_York")
            except Exception:
                _mtz = _MZI("America/New_York")
            _mnow = _mdt.now(_mtz)
            summary["streaks"] = store.get_streaks_display(
                conn, user_id, _mnow.strftime("%Y-%m-%d"),
                (_mnow - _mtd(days=1)).strftime("%Y-%m-%d"))
        except Exception:
            summary["streaks"] = {}
        return summary
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
    for key in ("basic",):
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


class MemoryDeleteOneIn(BaseModel):
    character_id: str = "chloe"
    memory_id: int


@app.post("/api/v1/memory/delete-one")
def memory_delete_one(body: MemoryDeleteOneIn, request: Request):
    """Delete a single saved memory (the 'What she remembers' page)."""
    user_id = _current_user_id(request)
    conn = _conn()
    try:
        removed = store.delete_memory(conn, user_id,
                                      body.character_id or "chloe",
                                      body.memory_id)
        return {"ok": True, "removed": removed}
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
    if not ref:
        # Fallback (2026-10-17): the web app historically opened the bare
        # payment links without client_reference_id, so paid checkouts
        # arrived anonymous and were silently ignored. Resolve the buyer
        # from the email entered at checkout instead of dropping the sale.
        email = (((obj.get("customer_details") or {}).get("email") or "")
                 or (obj.get("customer_email") or "")).strip().lower()
        if email:
            u = store.get_user_by_email(conn, email)
            if u:
                ref = (u.get("user_id") or "").strip()
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
        if (obj.get("payment_link") or "") == config.TRIAL_LINK_ID:
            if not ref:
                return {"ok": True, "ignored": "trial link but buyer unresolved"}
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


# ---------------------------------------------------------------------------
# Gemini key-manager proxy — shared pool for the other apps (creator bot,
# God Town, Roof Rover). Uses the admin-managed key pool in app/gemini_keys.py
# (free keys first, paid fallback, transparent failover on 429/402/errors).
#
# Service-to-service auth: callers must send
#     Authorization: Bearer <GEMINI_PROXY_TOKEN>
# Set GEMINI_PROXY_TOKEN as a Railway env var on the companion service.
# Without it the proxy returns 503; a wrong token returns 401. This keeps
# random internet users from burning the key pool.
# ---------------------------------------------------------------------------
_GEMINI_PROXY_TOKEN = os.environ.get("GEMINI_PROXY_TOKEN", "")
_GEMINI_PROXY_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_GEMINI_PROXY_DEFAULT_MODEL = "gemini-3.1-flash-lite"


def _require_proxy_token(request: Request):
    if not _GEMINI_PROXY_TOKEN:
        raise HTTPException(status_code=503, detail="proxy_not_configured")
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.startswith("Bearer ") or auth[7:].strip() != _GEMINI_PROXY_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")



def _default_safety_settings():
    """Default safety settings: sexually explicit unblocked, others at medium+."""
    return [
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    ]


def _ensure_safety_settings(payload: dict):
    """Inject default safetySettings if the caller didn't specify any."""
    if isinstance(payload, dict) and "safetySettings" not in payload:
        payload["safetySettings"] = _default_safety_settings()


@app.post("/v1beta/models/{model}:generateContent")
def proxy_gemini_generate_content(model: str, body: dict, request: Request):
    """Gemini native passthrough through the shared key pool."""
    _require_proxy_token(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body_must_be_json_object")
    url = "%s/%s:generateContent" % (_GEMINI_PROXY_BASE, model)
    # Calling app identifies itself via X-Source header (e.g. "gods-greek",
    # "roof-rover", "whispr-telegram"); defaults to "proxy".
    source = (request.headers.get("x-source") or "proxy").strip()[:64] or "proxy"
    # X-Free-Only: partner traffic must run on the free key pool only -
    # it never falls back to a paid key (see gemini_keys.post_json).
    free_only = (request.headers.get("x-free-only") or "").strip().lower() \
        in ("1", "true", "yes")
    _ensure_safety_settings(body)
    data = gemini_keys.post_json(url, body, timeout=60, source=source,
                                 free_only=free_only)
    if data is None:
        raise HTTPException(status_code=502, detail="all_keys_failed")
    return data


def _openai_messages_to_gemini(body):
    messages = body.get("messages") or []
    system_parts = []
    contents = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict))
        text = str(content or "")
        if role == "system":
            system_parts.append(text)
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text}]})
        else:
            contents.append({"role": "user", "parts": [{"text": text}]})
    payload = {"contents": contents}
    if system_parts:
        payload["system_instruction"] = {
            "parts": [{"text": "\n".join(system_parts)}]}
    gen = {}
    if body.get("temperature") is not None:
        try:
            gen["temperature"] = float(body["temperature"])
        except (TypeError, ValueError):
            pass
    if body.get("max_tokens") is not None:
        try:
            gen["maxOutputTokens"] = int(body["max_tokens"])
        except (TypeError, ValueError):
            pass
    if gen:
        payload["generationConfig"] = gen
    return payload


@app.post("/v1/chat/completions")
def proxy_openai_chat_completions(body: dict, request: Request):
    """OpenAI-compatible chat completions via the shared Gemini key pool."""
    _require_proxy_token(request)
    if not isinstance(body, dict) or not body.get("messages"):
        raise HTTPException(status_code=400, detail="messages_required")
    model = body.get("model") or _GEMINI_PROXY_DEFAULT_MODEL
    if model == "default":
        model = _GEMINI_PROXY_DEFAULT_MODEL
    payload = _openai_messages_to_gemini(body)
    _ensure_safety_settings(payload)
    url = "%s/%s:generateContent" % (_GEMINI_PROXY_BASE, model)
    source = (request.headers.get("x-source") or "proxy").strip()[:64] or "proxy"
    free_only = (request.headers.get("x-free-only") or "").strip().lower() \
        in ("1", "true", "yes")
    data = gemini_keys.post_json(url, payload, timeout=60, source=source,
                                 free_only=free_only)
    if data is None:
        raise HTTPException(status_code=502, detail="all_keys_failed")
    text, finish = "", "STOP"
    try:
        cand = data["candidates"][0]
        parts = cand["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if p.get("text"))
        finish = cand.get("finishReason", "STOP")
    except (KeyError, IndexError, TypeError):
        pass
    usage = data.get("usageMetadata") or {}
    return {
        "id": "chatcmpl-" + os.urandom(8).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop" if finish == "STOP" else str(finish).lower(),
        }],
        "usage": {
            "prompt_tokens": usage.get("promptTokenCount", 0),
            "completion_tokens": usage.get("candidatesTokenCount", 0),
            "total_tokens": usage.get("totalTokenCount", 0),
        },
    }

