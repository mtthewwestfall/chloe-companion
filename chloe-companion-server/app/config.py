"""Configuration: tiers, Stripe product/price definitions, feature flags.

No live keys anywhere. Stripe IDs are test-mode placeholders.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Tiers (updated 2026-09-28 per user: $9.99 / $19.99 / $29.99).
# $9.99 -> 1,000 msgs (text-only); $19.99 -> 2,000 msgs + 60 voice notes +
# photos + proactive; $29.99 -> 3,000 msgs + 150 voice notes + photos +
# proactive. Voice-note counts confirmed by user 2026-09-29
# ("use the preset options for now").
# Banks top up monthly, never stack: a renewal refills TO the allocation.
# ---------------------------------------------------------------------------
TIERS = {
    "basic": {
        "name": "Companion",
        "price_usd": 9.99,
        "messages_per_month": 1000,
        "voice_notes_per_month": 0,
        "photo_drops": False,
        "proactive": False,
    },
    "plus": {
        "name": "Companion Plus",
        "price_usd": 19.99,
        "messages_per_month": 2000,
        "voice_notes_per_month": 60,
        "photo_drops": True,
        "proactive": True,
    },
    "vip": {
        "name": "Companion VIP",
        "price_usd": 29.99,
        "messages_per_month": 3000,
        # Confirmed by user 2026-09-29 ("use the preset options for now").
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
    "plus": "https://buy.stripe.com/bJecMYdHd38H81xfUldjO0d",
    "vip": "https://buy.stripe.com/4gM4gs8mT10zchN9vXdjO0e",
}

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
    "price_1UKiwL6qicU1CK4UBFqunzeH": "plus",   # $19.99/mo Companion Plus
    "price_1UKqZl6qicU1CK4U5QNulgOY": "vip",    # $29.99/mo Companion VIP
}

# LIVE payment link -> tier (checkout.session.completed carries payment_link).
STRIPE_LINK_TIERS = {
    "plink_1UKjPK6qicU1CK4UJsgj7721": "basic",  # buy.stripe.com/9B67sE9qXeRp3Lh4bDdjO0c
    "plink_1UKjPv6qicU1CK4UpaV1VAHb": "plus",   # buy.stripe.com/bJecMYdHd38H81xfUldjO0d
    "plink_1UKqaT6qicU1CK4UoO0GbptK": "vip",    # buy.stripe.com/4gM4gs8mT10zchN9vXdjO0e
}

TIER_RANK = {"basic": 1, "plus": 2, "vip": 3}

# Companion -> Keyhole free-show grant. The companion webhook calls the
# Keyhole backend's /internal/companion-show-grant with the shared secret;
# inert until both env vars are set on the respective Railway services.
KEYHOLE_BACKEND_URL = os.environ.get(
    "KEYHOLE_BACKEND_URL",
    "https://sorority-house-production-aeb5.up.railway.app",
)
COMPANION_GRANT_SECRET = os.environ.get("COMPANION_GRANT_SECRET", "")
SHOW_GRANT_MINUTES = {"basic": 10, "plus": 15, "vip": 30}

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
        "plus": {
            "product_id": "prod_TEST_PLACEHOLDER_PLUS",
            "price_id": "price_TEST_PLACEHOLDER_PLUS",
            "tier": "plus",
        },
        "vip": {
            "product_id": "prod_TEST_PLACEHOLDER_VIP",
            "price_id": "price_TEST_PLACEHOLDER_VIP",
            "tier": "vip",
        },
    },
}

# ---------------------------------------------------------------------------
# Feature flags — everything paid/external is OFF by default.
# ---------------------------------------------------------------------------
# xAI TTS (Liora voice notes). Disabled until the user explicitly approves
# paid TTS use AND supplies a key. No network calls without both.
XAI_API_KEY = os.environ.get("CHLOE_XAI_API_KEY", "")
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

DB_PATH = os.environ.get("CHLOE_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "chloe.db"))
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
