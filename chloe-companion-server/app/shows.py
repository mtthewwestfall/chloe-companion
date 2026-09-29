"""Show hooks: reads the local shows.json stub; the companion can mention
upcoming shows in chat, and GET /api/v1/shows serves them to the app.
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import config


def shows_path() -> str:
    return os.path.join(config.DATA_DIR, "shows.json")


def get_shows() -> list[dict[str, Any]]:
    path = shows_path()
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = json.load(f)
    return data.get("shows", [])


def next_show() -> dict[str, Any] | None:
    shows = get_shows()
    return shows[0] if shows else None
