"""Photo drops: manifest-driven library, tier-gated.

Manifest schema (photo_drops/manifest.json):
  { "drops": [ { "id": str, "file": str, "tier_min": "plus"|"vip",
                 "caption": str, "placeholder": bool } ] }

The user curates real photos later; this phase ships 2 PLACEHOLDER entries.
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import config

_TIER_RANK = {"basic": 0, "plus": 1, "vip": 2}


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
    rank = _TIER_RANK.get(tier, 0)
    return [d for d in load_manifest() if _TIER_RANK.get(d.get("tier_min", "vip"), 99) <= rank]


def maybe_drop_photo(tier: str, messages_used_total: int, every_n: int = 7) -> dict[str, Any] | None:
    """Serving hook for the chat flow. Tier-gated; drops at most one photo
    per `every_n` user messages, cycling through the eligible drops."""
    eligible = drops_for_tier(tier)
    if not eligible:
        return None
    if messages_used_total <= 0 or messages_used_total % every_n != 0:
        return None
    return eligible[(messages_used_total // every_n - 1) % len(eligible)]
