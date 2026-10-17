"""Configuration: tiers, Stripe product/price definitions, feature flags.

No live keys anywhere. Stripe IDs are test-mode placeholders.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Tiers (updated 2026-10-08 per user: single $9.99 tier).
# $9.99 -> 3,000 msgs + 150 voice notes + photos + proactive.
# The old $19.99 (plus) and $29.99 (vip) tiers were removed; their Stripe
# payment links are ARCHIVED (not deleted) in STRIPE_LINKS_ARCHIVE.md at the
# repo root for reuse later.
# Banks top up monthly, never stack: a renewal refills TO the allocation.
# ---------------------------------------------------------------------------
TIERS = {
    "basic": {
        "name": "Companion",
        "price_usd": 9.99,
        "messages_per_month": 3000,
        "voice_notes_per_month": 150,
        "photo_drops": True,
        "proactive": True,
    },
}

# ---------------------------------------------------------------------------
# LIVE Stripe checkout links — provided by the user 2026-09-28.
# basic -> the $9.99/mo recurring link, plus -> the $19.99/mo recurring link,
# vip -> the $29.99/mo recurring link. All three verified resolving to live
# Stripe Checkout pages on 2026-09-28 (HTTP 200, title "Stripe Checkout").
# NOTE: the $9.99/$19.99 products still carry their old "BOGO" names in the
# Stripe dashboard until renamed. Price/interval is taken on the user's word —
# Stripe renders it via JS, so it can't be read from the checkout page HTML.
# ---------------------------------------------------------------------------
STRIPE_LIVE_CHECKOUT = {
    "basic": "https://buy.stripe.com/9B67sE9qXeRp3Lh4bDdjO0c",
}
# ARCHIVED (2026-10-08): old tier checkout links, disconnected from the tier
# system but NOT deleted from Stripe — saved for reuse. Full details in
# STRIPE_LINKS_ARCHIVE.md.
#   plus ($19.99/mo): https://buy.stripe.com/bJecMYdHd38H81xfUldjO0d
#   vip  ($29.99/mo): https://buy.stripe.com/4gM4gs8mT10zchN9vXdjO0e

# ---------------------------------------------------------------------------
# Purchase fulfillment (live, 2026-09-29). The webhook at
# POST /api/v1/stripe/webhook verifies Stripe signatures with
# STRIPE_WEBHOOK_SECRET and credits banks automatically.
# ---------------------------------------------------------------------------
# Signing secret of the live Stripe webhook endpoint (whsec_...). Set as a
# Railway env var on the companion service; the webhook refuses with 503
# until it is set. NEVER commit the real value here.
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# LIVE price -> tier, read from the Stripe dashboard 2026-09-29.
STRIPE_PRICE_TIERS = {
    "price_1UKjDn6qicU1CK4U7LwzZr9A": "basic",  # $9.99/mo Companion
}
# ARCHIVED (2026-10-08) price -> tier mappings, disconnected from fulfillment.
# See STRIPE_LINKS_ARCHIVE.md for reuse.
#   "price_1UKiwL6qicU1CK4UBFqunzeH": "plus",   # $19.99/mo Companion Plus
#   "price_1UKqZl6qicU1CK4U5QNulgOY": "vip",    # $29.99/mo Companion VIP

# LIVE payment link -> tier (checkout.session.completed carries payment_link).
STRIPE_LINK_TIERS = {
    "plink_1UKjPK6qicU1CK4UJsgj7721": "basic",  # buy.stripe.com/9B67sE9qXeRp3Lh4bDdjO0c
}
# ARCHIVED (2026-10-08) payment-link -> tier mappings, disconnected.
# See STRIPE_LINKS_ARCHIVE.md for reuse.
#   "plink_1UKjPv6qicU1CK4UpaV1VAHb": "plus",   # buy.stripe.com/bJecMYdHd38H81xfUldjO0d
#   "plink_1UKqaT6qicU1CK4UoO0GbptK": "vip",    # buy.stripe.com/4gM4gs8mT10zchN9vXdjO0e

TIER_RANK = {"basic": 1}  # single tier (2026-10-08)

# Companion -> Keyhole free-show grant. The companion webhook calls the
# Keyhole backend's /internal/companion-show-grant with the shared secret;
# inert until both env vars are set on the respective Railway services.
KEYHOLE_BACKEND_URL = os.environ.get(
    "KEYHOLE_BACKEND_URL",
    "https://sorority-house-production-aeb5.up.railway.app",
)
COMPANION_GRANT_SECRET = os.environ.get("COMPANION_GRANT_SECRET", "")
SHOW_GRANT_MINUTES = {"basic": 30}  # single tier gets the old VIP grant (2026-10-08)

# $0.99 one-time trial -> 100 messages, one per customer (live, 2026-09-29).
TRIAL_PRODUCT_ID = "prod_VLZInf7B1PDPRZ"
TRIAL_PRICE_ID = "price_1UKs9f6qicU1CK4Ubf94Lmv7"
TRIAL_LINK_ID = "plink_1UKs9h6qicU1CK4UhoKvOvl3"
TRIAL_CHECKOUT_URL = "https://buy.stripe.com/3cI14ggTpfVtbdJ4bDdjO0f"
TRIAL_MESSAGES = 100

# Free verified-signup grant (2026-09-30, per user): the first
# FREE_SIGNUP_CAP verified signups each get FREE_SIGNUP_MESSAGES free
# messages, granted once at email-verification time. Set to 50 (not 100)
# so the $0.99 trial (100 msgs) stays a real upgrade.
FREE_SIGNUP_MESSAGES = 50
FREE_SIGNUP_CAP = 200

# ---------------------------------------------------------------------------
# JVZoo affiliate channel (2026-09-30). JVZIPN v2 posts order events to
# POST /api/v1/jvzoo/ipn; each post carries cverify =
# UPPERCASE(SHA1("paykey|customer_email|product_name|transaction_type|"
#                "date|" + secret)[:8]).
# The secret is generated in the JVZoo dashboard (My Account -> JVZIPN Secret
# Key) and set as a Railway env var on the companion service; the endpoint
# refuses with 503 until it is set. NEVER commit the real value here.
# ---------------------------------------------------------------------------
JVZIPN_SECRET = os.environ.get("JVZIPN_SECRET", "")

# JVZIPN v2 sends `total` already in dollars. Map paid totals to tiers.
# A $0 trial SALE never changes the tier by itself — the 50-free-message
# trial is the app's own verified-signup grant; the paid tier is fulfilled
# on the first BILL (paid rebill), which is also when the affiliate earns.
JVZOO_TIER_TOTALS = {
    9.99: "basic",
}
# ARCHIVED (2026-10-08): 19.99 -> "plus", 29.99 -> "vip" removed with the tiers.

# ---------------------------------------------------------------------------
# Stripe product/price DEFINITIONS — TEST-MODE PLACEHOLDERS.
# Clearly marked: these IDs are fake. Replace with real test-mode IDs when the
# user approves creating the Stripe products. NEVER create live products here.
# ---------------------------------------------------------------------------
STRIPE_TEST_PLACEHOLDERS = {
    "mode": "test",
    "products": {
        "basic": {
            "product_id": "prod_TEST_PLACEHOLDER_BASIC",
            "price_id": "price_TEST_PLACEHOLDER_BASIC",
            "tier": "basic",
        },
    },
}

# ---------------------------------------------------------------------------
# Feature flags — everything paid/external is OFF by default.
# ---------------------------------------------------------------------------
# ElevenLabs TTS (Liora voice notes). Disabled until the user explicitly approves
# paid TTS use AND supplies a key. No network calls without both.
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
VOICE_NOTES_ENABLED = os.environ.get("CHLOE_VOICE_ENABLED", "0") == "1"

# Gemini (real AI chat). Set GEMINI_API_KEY as a Railway env var on the
# companion service to switch the mouth from the canned-reply engine to
# live Gemini. When unset, the local engine is used — the app never fails
# for lack of a key. NEVER commit the real value here.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Proactive scheduler: dry-run by default (writes to a queue file, no delivery).
SCHEDULER_DRY_RUN = os.environ.get("CHLOE_SCHEDULER_DRY_RUN", "1") == "1"

# ---------------------------------------------------------------------------
# Email auth (2026-09-30): login is required — no anonymous/free use.
# Verification emails go through Resend (same provider as the Keyhole
# backend). NEVER commit real values here.
# ---------------------------------------------------------------------------
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "Chloe <hello@lockeddoor.ai>")
PUBLIC_APP_URL = os.environ.get("PUBLIC_APP_URL", "https://companion.lockeddoor.ai").rstrip("/")
VERIFY_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Admin panel (2026-09-30): browser-based admin at /admin.html with
# "Accounts" and "Girls" tabs. Guarded by ADMIN_TOKEN (Railway env var),
# sent as the X-Admin-Token header. Endpoints 503 until the token is set.
# NEVER commit the real value here.
# ---------------------------------------------------------------------------
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

DB_PATH = os.environ.get("CHLOE_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "chloe.db"))
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Custom media requests ("request-a-pic", 2026-10-01). Per-tier monthly
# included pools — regular (custom) requests only since 2026-10-08; give
# presets are unlimited and bypass the pool entirely. Pools top up like
# the message banks (no stacking): set_tier and the Stripe renewal path
# both refill included_remaining to the tier allocation via
# store.refill_request_pool().
#
# Effective limits (2026-10-08): give = unlimited; regular = 1/month hard
# cap (enforced in main.create_media_request via
# store.regular_requests_used_this_month). The pool below is a formality
# for regular — 1/month can never exhaust it.
# ---------------------------------------------------------------------------
REQUEST_POOLS = {
    "basic": 20,   # single $9.99 tier gets the old VIP pool (2026-10-08)
}

# Daily per-user request caps (anti-farming). Applies to regular requests
# only since 2026-10-08 (give is unlimited). Admin-configurable later.
DAILY_REQUEST_CAPS = {
    "basic": 10,   # single tier gets the old VIP cap (2026-10-08)
}

# Lifetime video request cap (2026-10-08): removed — with a single paid tier
# there is nothing to upgrade to, so no cap.
LIFETIME_VIDEO_CAPS = {}

# Overage pack ($5.99, paid through Shopify — product + webhook are a
# separate item; fulfillment calls store.credit_overage_pack()).
# All-preset since 2026-09-30: no custom generation in the pack — the 14
# video sends are preset give clips only ($0 marginal generation cost).
OVERAGE_PACK = {
    "price_usd": 5.99,
    "messages": 300,
    "regular_videos": 0,
    "give_videos": 14,
}

# Free verified-signup grant (first 200) also gets this many PRESET give
# sends (kind=give only — never regular generation; $0 cost). Credited in
# store.grant_free_signup_messages alongside the 50 free messages.
FREE_SIGNUP_GIVE_VIDEOS = 2

# Server-side Sogni generation for REGULAR requests. Raw API key as a
# Railway env var; generation stays OFF until it is set. NEVER commit the
# real value here.
SOGNI_API_KEY = os.environ.get("SOGNI_API_KEY", "")
SOGNI_VIDEO_MODEL = os.environ.get(
    "SOGNI_VIDEO_MODEL", "minimax-h3-fastvideo-int8_i2v_turbo")
SOGNI_IMAGE_MODEL = os.environ.get(
    "SOGNI_IMAGE_MODEL", "qwen_image_edit_2511_fp8")
# Prompt-only image renders (no reference picture to edit) cannot use
# the edit model above — Sogni refuses them outright (error 4007:
# "For a prompt-only render, choose a non-edit model"). This non-edit
# sibling is used whenever a plan image render carries no reference.
SOGNI_IMAGE_TEXT_MODEL = os.environ.get(
    "SOGNI_IMAGE_TEXT_MODEL", "qwen_image_2512_fp8")

# Booked cost per fulfilled regular request (live Sogni quotes 2026-09-30:
# ~$0.1175 per 6s clip, ~$0.01-0.03 per image). Logged per request.
MEDIA_REQUEST_COST_USD = {
    "regular_pic": 0.03,
    "regular_video": 0.12,
    "give": 0.0,
}

# Where finished regular generations are saved and served from.
# Persistence (2026-10-15): with no explicit CHLOE_GENERATED_DIR, the
# media lives NEXT TO the SQLite database. Production points
# CHLOE_DB_PATH at the Railway volume (/data/chloe.db), so generated
# media lands on that same persistent volume instead of a
# container-local folder that every redeploy wipes — a reusable media
# pool can finally accumulate across days and deploys.
GENERATED_DIR = os.environ.get(
    "CHLOE_GENERATED_DIR",
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "generated"))

