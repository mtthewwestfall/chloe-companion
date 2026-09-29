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

# Proactive scheduler: dry-run by default (writes to a queue file, no delivery).
SCHEDULER_DRY_RUN = os.environ.get("CHLOE_SCHEDULER_DRY_RUN", "1") == "1"

DB_PATH = os.environ.get("CHLOE_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "chloe.db"))
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
