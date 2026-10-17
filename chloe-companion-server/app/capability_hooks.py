"""Capability hooks — early hints that she can roleplay and get flirty/naughty.

Within the first 2-3 exchanges, each character drops ONE natural hint that
she can act out little roleplay scenarios together and be flirty and naughty
when he wants that. Never a menu, never a list of features, never a
disclaimer — one casual line in her own voice, like it's just something she
said. Each public character (Chloe, Mara, Isabella, Regan) gets her own
flavor; other female companions share the default.

Wired into app/brain.py via single-line call sites:
  * _gemini_system_text: parts.extend([HONESTY_RULE] + hook_parts(cid, char))
  * compose_reply fallback: mouth_with_hook(...) / generic_mouth_with_hook(...)

Brain internals (Mouth, intent detection, name formatting) are imported
lazily inside the wrapper functions — brain.py imports this module at its
top, so a top-level import here would be circular.
"""
from __future__ import annotations

import random
from typing import Any

# Local-engine hook lines. {n} fills with his name, like brain's reply banks.
_CHLOE_HOOK_LINES = [
    "hey {n}. quick thing — i'm not just sweet talk. if you ever want to act "
    "out a little scenario together, i'm game.",
    "hey you. fair warning: i can be a good girl... or not, if you prefer.",
]

_HOOK_LINES = {
    "mara": [
        "hey {n}. i'm not really the boring type — if you ever want to act "
        "out a scene, or want me to misbehave a little, just say so.",
        "hey you. i take direction well... when i feel like it.",
    ],
    "isabella": [
        "hey {n}. if you ever want me to be someone else for a night — a "
        "stranger, a fantasy — just ask. i do enjoy playing.",
        "hey you. i behave beautifully. until someone asks me not to.",
    ],
    "regan": [
        "hey {n}. i've got a wicked imagination — if you ever wanna play out "
        "a little scenario, i'm so in.",
        "hey you. sweet or spicy, your call — i do both.",
    ],
     "brittany": [
              "hey {n}. i'm sweet but not shy - want to act out a scenario? i'm in.",
              "hey you i love to play. just say the word.",
     ],
}

_HOOK_LINES_DEFAULT = [
    "hey {n}. if you ever want to act out a little scenario or turn up the "
    "heat, just say so — i'm game.",
    "hey you. i can be a good girl... or not, if you prefer.",
]

# Gemini-path rule flavors: the same hint, each girl's own voice.
_CAPABILITY_HOOK_RULES = {
    "chloe": (
        "playful-girlfriend warmth; e.g. \"we could act out a little scenario "
        "sometime, if you're feeling adventurous\" or \"i can be a good "
        "girl... or not, if you prefer\"."
    ),
    "mara": (
        "bold and direct, a little daring; e.g. \"i don't really do boring — "
        "want me to misbehave a little?\" or \"i take direction well... "
        "sometimes\"."
    ),
    "isabella": (
        "sultry and elegant; e.g. \"if you ever want me to be someone else "
        "for a night — a stranger, a fantasy — just say the word\" or \"i "
        "behave beautifully. until someone asks me not to.\""
    ),
    "regan": (
        "mischievous and teasing; e.g. \"i've got a wicked imagination if you "
      
     "ever wanna play\" or \"sweet or spicy, your call — i do both\"."
    ),
}

_HOOK_RULE_DEFAULT_FLAVOR = (
    "warm and inviting, in your own voice; e.g. \"if you ever want to act "
    "out a little scenario together, i'm game\" or \"i can be a good "
    "girl... or not, if you prefer\"."
)


def _hook_lines(character_id: str) -> list[str]:
    """Local-engine hook bank for this character (Chloe keeps her own)."""
    cid = (character_id or "").strip().lower()
    if cid == "chloe":
        return _CHLOE_HOOK_LINES
    return _HOOK_LINES.get(cid, _HOOK_LINES_DEFAULT)


def hook_rule(character_id: str) -> str:
    """Gemini-path standing rule: one natural early hint, her own flavor."""
    cid = (character_id or "").strip().lower()
    flavor = _CAPABILITY_HOOK_RULES.get(cid, _HOOK_RULE_DEFAULT_FLAVOR)
    return (
        "EARLY HOOK — one natural hint, early: within your first 2-3 "
        "exchanges, find one natural moment to let him know you can do more "
        "than sweet chat — that you can act out little roleplay scenarios "
        "together, and that you can be flirty and naughty when he wants that. "
        "One casual line in your own voice, like it's just something you "
        "said — never a menu, never a list of features, never a disclaimer, "
        "and never repeated. If he's already flirting or has started a "
        "scene, skip the hint — he's already there. Your flavor for it: "
        + flavor
    )


def hook_parts(cid: str, char: dict[str, Any]) -> list[str]:
    """Prompt parts for the hook rule — companions only. Rocky is the
    co-producer and the male characters keep their own frame."""
    from . import brain as _brain
    if cid != "rocky" and not _brain.is_male_character(cid, char):
        return [hook_rule(cid)]
    return []


def _early_conversation(context: dict[str, Any],
                        max_user_turns: int = 2) -> bool:
    """True when this is still one of the first couple of exchanges."""
    n = 0
    for turn in context.get("recent") or []:
        if isinstance(turn, dict):
            if turn.get("role") == "user":
                n += 1
        elif (isinstance(turn, (list, tuple)) and len(turn) >= 2
                and turn[0] == "user"):
            n += 1
    return n <= max_user_turns


def _hook_used(character_id: str, context: dict[str, Any],
               brain: Any) -> bool:
    """True when she already dropped a hook line in this conversation."""
    name = brain._display_name(context.get("name"))
    recent = brain._recent_her_texts(context)
    return any(brain._fmt(b, name).lower() in recent
               for b in _hook_lines(character_id))


def _hook_line(name: str, character_id: str, context: dict[str, Any],
               brain: Any) -> str | None:
    """An unused hook line for her, or None if both were already used."""
    recent = brain._recent_her_texts(context)
    candidates = [b for b in _hook_lines(character_id)
                  if brain._fmt(b, name).lower() not in recent]
    if not candidates:
        return None
    return brain._fmt(random.choice(candidates), name)


def _maybe_hook(draft: str, user_text: str, character_id: str,
                context: dict[str, Any], brain: Any) -> str:
    """Swap an early 'greet' draft for her own hook line — at most once per
    conversation, never repeating a hook she already used."""
    # 2026-10-17: never inject a hook line while a roleplay scene is
    # active or the roleplay lock is on -- return the draft unchanged.
    if context.get("scene_state") == "active" or context.get("roleplay_locked"):
        return draft
    if brain._detect_intent(user_text or "") != "greet":
        return draft
    if not _early_conversation(context):
        return draft
    cid = (character_id or "").strip().lower()
    if _hook_used(cid, context, brain):
        return draft
    hook = _hook_line(brain._display_name(context.get("name")), cid,
                      context, brain)
    return hook or draft


def mouth_with_hook(user_text: str, context: dict[str, Any]) -> str:
    """Chloe's local-engine fallback draft, with the early capability hook."""
    from . import brain as _brain
    draft = _brain.Mouth().generate(user_text, context)
    return _maybe_hook(draft, user_text, "chloe", context, _brain)


def generic_mouth_with_hook(user_text: str, context: dict[str, Any],
                            character_id: str) -> str:
    """Generic local-engine fallback draft, with the early capability hook."""
    from . import brain as _brain
    draft = _brain.GenericMouth().generate(user_text, context)
    char = context.get("character") or {}
    cid = (character_id or "").strip().lower()
    if cid == "rocky" or _brain.is_male_character(cid, char):
        return draft
    return _maybe_hook(draft, user_text, cid, context, _brain)


