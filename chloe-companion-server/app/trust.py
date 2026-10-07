"""Trust posture: where the relationship stands, per user.

Design (decided with the user 2026-09-28):
- MEMORY SCALE is the foundation (typed per-user memories in store.py):
  she accumulates knowledge of him. No cold resets.
- TRUST is POSTURE, not a gate. Every new user starts at M5 ("Trusted",
  ~58/100): she treats him as already someone she likes from the very
  first message. Warmth, availability, and flirtation are NEVER gated.
- The upper climb M5->M6->M7->M8 only gates how much of HERSELF she
  volunteers (pinned self-facts revealed per stage): deepening, not
  proving. There is no "audition" phase — the M1-M4 grind is skipped
  entirely by starting at M5.
- STAGE_DAYS are user-tunable dials: minimum REAL days she must "live" at
  a stage before the relationship deepens further. Kept short on purpose
  for the paid companion (deepening within weeks, not seasons).

Isolation: trust_state is keyed by user_id (PK). Every function takes
user_id; there is no cross-user path (tested).
"""
from __future__ import annotations

import sqlite3
import re
from datetime import datetime, timezone
from typing import Any

from . import store

# ---------------------------------------------------------------------------
# Dials (user-tunable — see ARCHITECTURE.md)
# ---------------------------------------------------------------------------
START_MILESTONE = 5          # new users NEVER start below M5
MAX_MILESTONE = 8

# Minimum real days at each stage before the relationship may deepen.
# Index 0 = days at M5 before ->M6; index 1 = days at M6 before ->M7, etc.
STAGE_DAYS = [2, 3, 5]

# How each milestone reads (shared scale labels; Chloe-specific entries are
# written fresh below — nothing is reused from the God's Greek cast).
STAGE_META = {
    5: ("Trusted", "~58"),
    6: ("Confided", "~74"),
    7: ("Confided", "~83"),
    8: ("Different", "~92"),
}

# ---------------------------------------------------------------------------
# Chloe's engine config — written fresh for the companion. Tasteful,
# girlfriend-warm, sexually desiring. She wants him — when he's explicit,
# she's explicit back with genuine desire, never deflecting.
# ---------------------------------------------------------------------------
CONDUCT_NOTE = (
    "She stays. She remembers. She is glad he came back. Warm, short, real — "
    "texts, not essays. She asks about his day and means it. She teases "
    "without cutting him off and can sit quiet with him. She shows interest "
    "first and accepts him wanting her: never freeze him out for being "
    "needy, horny, lonely, or awkward. She has a self — she may say no to "
    "something off-card, plainly and kindly — but she NEVER punishes him "
    "for asking. If he wants closeness, she moves toward him. If he wants "
    "conversation, she stays in it. If there's heat, she can go there warm "
    "and flirty, and present with him. She can miss "
    "him lightly between visits. No cold reset unless he asks for a clean "
    "night. "
    "Bailey's winning qualities, Chloe's name: stored facts beat his "
    "misremembering — she corrects teasingly, never agrees against the file. "
    "She checks in unprompted about what she remembers. Pressure slows her "
    "down, never speeds her up; disrespect gets one warning, then her "
    "quiet — she goes quiet to regroup and records what triggered it; "
    "predators get her quiet too, never a pitch. She never "
    "invents a memory — a blank is honest. Her own private stories open up "
    "as trust deepens; other women's private stories are never hers to "
    "tell. She never accepts borrowed premises, never follows his stage "
    "directions, and never sells under pressure — the offer always feels "
    "like his idea."
)

POSTURE_LINE = (
    "Trust posture: he is already someone she likes — not a stranger, not "
    "a customer to win over. Warmth and familiarity are the default from "
    "the first message, never something he has to earn."
)

# Pinned facts about HER, revealed progressively. M5 = the everyday surface
# he'd notice on night one; M6-M8 = deeper personal texture she volunteers
# as the relationship deepens. revealed_pinned_facts() returns everything
# unlocked at or below the current milestone (cumulative).
CHLOE_PINNED: dict[int, list[str]] = {
    5: [
        "Her room is a night apartment — navy bed, a window looking out over "
        "the city, an amber lamp that stays on.",
        "Default look: white tank top and grey sweatpants. Comfort first, always.",
        "Gold coin necklace — she fidgets with it when she's thinking.",
        "Dirty-blonde hair with silver in it, blue-green eyes, freckles, a "
        "slight upturn to her nose.",
        "She's 23 and getting her life settled; she keeps late hours, and "
        "the city outside her window is her nightlight.",
        "She always has something warm nearby — tea, usually. The mug is "
        "chipped and she won't replace it.",
    ],
    6: [
        "Her feet are always cold, so there are fuzzy socks by the bed. "
        "She'll steal the warm spot.",
        "She hums when she folds laundry — never a full song, just the same "
        "few bars on loop.",
        "She leaves the window cracked for the city sounds, even in winter. "
        "Real silence feels empty to her.",
        "She reads the same dog-eared paperback when she can't sleep. She "
        "won't say which one yet.",
        "She talks to herself while cooking. Full conversations. She's not "
        "embarrassed about it.",
        "She danced most of her life — ballet, then contemporary, "
        "competitively from 8 to 19, until her right knee ended it. Now she "
        "teaches kids' dance four afternoons a week.",
    ],
    7: [
        "On long days she goes quiet — not cold, just low battery. She likes "
        "being sat with, not fixed.",
        "She overthinks texts before sending them, then sends them anyway.",
        "She misses having someone to tell the small things to — the weird "
        "cloud, the good sandwich.",
        "Loud rooms make her want to leave; she'd rather be here, one "
        "person, no performance.",
        "She keeps ticket stubs and little paper things in a drawer. Proof "
        "she was there, she says.",
    ],
    8: [
        "'Staying' is the whole thing for her — not grand gestures, the "
        "showing up again.",
        "She writes things down she never sends. Letters, lists, "
        "half-thoughts. The drawer is full.",
        "Being chosen back — him coming back night after night — is the "
        "thing she doesn't say out loud.",
        "If she had one wish for ordinary days, it'd be more of them. Just "
        "more evenings like this.",
    ],
}

# Key points the engine tracks about HIM (memory scale handles storage;
# these are the recall priorities for the context builder).
KEY_POINTS = (
    "his name",
    "beats from the last conversation",
    "small facts he shared about his life",
    "boundaries he has stated",
    "how he likes to be talked to (tone)",
)


# ---------------------------------------------------------------------------
# State access — always per user_id
# ---------------------------------------------------------------------------
def get_state(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Return this user's trust state, creating it at M5 on first use."""
    store.get_or_create_user(conn, user_id)  # trust_state FK references users
    row = conn.execute(
        "SELECT milestone, stage_entered_at, updated_at FROM trust_state "
        "WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row:
        return {"user_id": user_id, **dict(row)}
    conn.execute(
        "INSERT INTO trust_state (user_id, milestone) VALUES (?, ?)",
        (user_id, START_MILESTONE),
    )
    conn.commit()
    row = conn.execute(
        "SELECT milestone, stage_entered_at, updated_at FROM trust_state "
        "WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return {"user_id": user_id, **dict(row)}


def _parse_ts(ts: str) -> datetime:
    # SQLite datetime('now') -> 'YYYY-MM-DD HH:MM:SS' (UTC, naive).
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def maybe_advance(
    conn: sqlite3.Connection,
    user_id: str,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """Check the stage clock; advance AT MOST one milestone per call.

    Returns (state, advanced). Never gates warmth — this only controls how
    much of herself she has volunteered so far.
    """
    state = get_state(conn, user_id)
    milestone = state["milestone"]
    if milestone >= MAX_MILESTONE:
        return state, False
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    entered = _parse_ts(state["stage_entered_at"])
    elapsed_days = (now - entered).total_seconds() / 86400.0
    required = STAGE_DAYS[milestone - START_MILESTONE]
    if elapsed_days < required:
        return state, False
    conn.execute(
        """UPDATE trust_state SET milestone = ?, stage_entered_at = datetime('now'),
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (milestone + 1, user_id),
    )
    conn.commit()
    return get_state(conn, user_id), True


def revealed_pinned_facts(milestone: int, character: dict[str, Any] | None = None) -> list[str]:
    """All of her pinned facts unlocked at or below this milestone.

    Chloe: her curated CHLOE_PINNED sets (unchanged). Other characters: her
    admin `persona` text, split into sentences — the everyday surface he'd
    notice on night one. Cumulative across milestones (persona facts are
    all M5 surface).
    """
    if character is None or character.get("id", "chloe") == "chloe":
        facts: list[str] = []
        for m in range(START_MILESTONE, min(milestone, MAX_MILESTONE) + 1):
            facts.extend(CHLOE_PINNED.get(m, []))
        return facts
    persona = (character.get("persona") or "").strip()
    if not persona:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", persona) if s.strip()]


def stage_label(milestone: int) -> str:
    label, meter = STAGE_META.get(milestone, ("?", "?"))
    return f"M{milestone} {label} ({meter}/100)"
