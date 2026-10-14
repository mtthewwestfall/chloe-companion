"""Photo drops: manifest-driven library.

Single paid tier (2026-10-08): all drops are eligible for "basic".
Legacy manifest entries may still carry tier_min "plus"|"vip" — ignored.
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import config

# Legacy rank map kept for reference; drops_for_tier no longer gates.
_TIER_RANK = {"basic": 2}


def manifest_path() -> str:
    return os.path.join(config.DATA_DIR, "photo_drops", "manifest.json")


def load_manifest() -> list[dict[str, Any]]:
    path = manifest_path()
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = json.load(f)
    return data.get("drops", [])


def drops_for_tier(tier: str) -> list[dict[str, Any]]:
    # Single paid tier: every paid user sees every drop.
    if tier == "basic":
        return load_manifest()
    return []


def maybe_drop_photo(tier: str, messages_used_total: int, every_n: int = 7) -> dict[str, Any] | None:
    """Serving hook for the chat flow. Tier-gated; drops at most one photo
    per `every_n` user messages, cycling through the eligible drops.

    Placeholder entries (manifest rows flagged "placeholder": true —
    slots the user has not curated a real photo into yet) are never
    served. A placeholder has no file behind it; serving it made the
    client paint its built-in placeholder artwork as if she had sent
    a picture (2026-10-14)."""
    eligible = [d for d in drops_for_tier(tier) if not d.get("placeholder")]
    if not eligible:
        return None
    if messages_used_total <= 0 or messages_used_total % every_n != 0:
        return None
    return eligible[(messages_used_total // every_n - 1) % len(eligible)]
