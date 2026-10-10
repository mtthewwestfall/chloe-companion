"""Show hooks: fetches live show data from Lockeddoor.ai / Keyhole backend,
cached ~5 minutes; falls back to local shows.json stub if offline.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

from . import config

CACHE_TTL_S = 300

_cache: dict[str, Any] = {"ts": 0.0, "shows": []}


def shows_path() -> str:
    return os.path.join(config.DATA_DIR, "shows.json")


def _fetch_local() -> list[dict[str, Any]]:
    path = shows_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("shows", [])
    except Exception:
        return []


def _fetch_remote() -> list[dict[str, Any]]:
    base = (config.KEYHOLE_BACKEND_URL or "https://keyhole.lockeddoor.ai").rstrip("/")
    url = base + "/keyhole/shows"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "chloe-companion/shows"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    raw_shows = data.get("shows", []) if isinstance(data, dict) else []
    out: list[dict[str, Any]] = []
    for s in raw_shows:
        if not isinstance(s, dict):
            continue
        show_id = str(s.get("id") or s.get("show_id") or "")
        title = str(s.get("title") or s.get("description") or "Live Room Experience")
        starts_at = str(s.get("starts_at") or s.get("scheduled_at") or s.get("created_at") or "")
        ticket_url = str(s.get("ticket_url") or "")
        item = dict(s)
        item["id"] = show_id
        item["title"] = title
        item["starts_at"] = starts_at
        item["ticket_url"] = ticket_url
        out.append(item)
    return out


def get_shows(refresh: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    if refresh or now - _cache["ts"] > CACHE_TTL_S:
        try:
            shows_list = _fetch_remote()
            if shows_list:
                _cache["shows"] = shows_list
                _cache["ts"] = now
            else:
                _cache["shows"] = _fetch_local()
                _cache["ts"] = now
        except Exception as e:
            print(f"[shows] fetch failed, falling back: {e}", flush=True)
            if not _cache["shows"]:
                _cache["shows"] = _fetch_local()
    return _cache["shows"]


def next_show() -> dict[str, Any] | None:
    shows_list = get_shows()
    return shows_list[0] if shows_list else None
