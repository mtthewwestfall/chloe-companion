"""Personality evolution: relationship-aware memory callbacks (2026-10-16).

The brain's _memory_callback brings back a relevant memory ~35% of the time
on open intents. This module adds EVOLUTION flavor for deep relationships
(M7+): she references their history with the weight it deserves -- not just
"you mentioned X", but "you told me X a while back, it's stuck with me."

Wired into brain._pick / _male_pick / _generic_pick alongside the standard
callback. Rarer than the standard callback -- history lands harder when it's
not constant.
"""
from __future__ import annotations

import random
import re
from typing import Any

# Evolution callbacks: for relationships with real history (M7+). They
# acknowledge the DEPTH, not just the fact -- "a while back", "stuck with
# me", "you've trusted me with the real stuff".
_EVOLUTION_CALLBACKS = [
    "you told me {m} a while back -- it's stuck with me.",
    "still thinking about this: {m} how's it going now?",
    "{m} -- you mentioned it before, so I know it matters.",
    "hey -- {m}. you've trusted me with the real stuff, I don't take that lightly.",
]


def _milestone(context: dict[str, Any]) -> int:
    """Trust milestone from context (trust_stage reads like 'M7 ...')."""
    m = re.match(r"M(\d+)", str(context.get("trust_stage") or ""))
    return int(m.group(1)) if m else 5


def evolution_callback(context: dict[str, Any]) -> str | None:
    """An evolution-flavored memory callback, or None.

    Only for deep relationships (M7+). Uses the same relevant_memories the
    standard callback uses. Returns None when there's nothing to say.
    """
    if _milestone(context) < 7:
        return None
    mems = context.get("relevant_memories") or []
    if not mems or random.random() > 0.15:
        return None
    m = str(mems[0]).strip().rstrip(".")
    if len(m) > 90:
        m = m[:90].rsplit(" ", 1)[0]
    return random.choice(_EVOLUTION_CALLBACKS).replace("{m}", m)
