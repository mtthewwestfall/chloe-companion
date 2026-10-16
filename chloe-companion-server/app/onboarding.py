"""First-time user onboarding: demo the best features in the first 3 messages.

The median user sends only 3 messages then bounces — never discovering
voice notes, roleplay, or proactive messaging. This module stages the
first conversation so the best features demo themselves:

  Message 1 (her greeting, seeded at verify): warm, personal, hints at
      voice notes, roleplay, and morning check-ins.
  Message 2 (after his first reply): a real voice note when available,
      otherwise a roleplay teaser.
  Message 3 (after his second reply): the proactive check-in promise.

Wire-in (same pattern as capability_hooks.py — single-line call sites,
no signature changes, no new tables):
  - main.py auth_verify seeds onboarding.greeting_line(character) as the
    first AI turn (replaces WELCOME_LINE).
  - main.py chat() calls onboarding.stage(context) after compose_reply;
    for stage 2 it attempts the dedicated voice-note demo and appends
    the matching line, for stage 3 it appends the check-in nudge.

Stage is derived from the turn count in the brain context, so it only
fires in a user's first conversation with a character. Rocky (the
owner's co-producer, not a companion) is excluded. Never raises —
onboarding must never break chat.
"""
from __future__ import annotations

from typing import Any

# Which AI reply we're composing, counted by his messages so far.
STAGE_VOICE_NOTE = 2  # his 1st message -> her 2nd message
STAGE_CHECKIN = 3     # his 2nd message -> her 3rd message


def stage(context: dict[str, Any] | None) -> int | None:
    """Return the onboarding stage for this reply, or None when onboarding
doesn't apply.

    context["recent"] already includes his just-logged message (chat()
    logs the user turn before building the brain context), so one user
    turn in history means we're composing her second message.
    """
    try:
        recent = (context or {}).get("recent") or []
        user_turns = sum(
            1 for t in recent
            if isinstance(t, dict) and t.get("role") == "user"
        )
        if user_turns == 1:
            return STAGE_VOICE_NOTE
        if user_turns == 2:
            return STAGE_CHECKIN
    except Exception:  # noqa: BLE001 — onboarding never breaks chat
        pass
    return None


def _is_male(character: dict[str, Any] | None) -> bool:
    return (character or {}).get("gender") == "male"


def _name(character: dict[str, Any] | None) -> str:
    return ((character or {}).get("name") or "Chloe").strip() or "Chloe"


def greeting_line(character: dict[str, Any] | None = None) -> str:
    """Message 1: her greeting for a brand-new user. Warm, personal, and
honest about what's possible — voice notes, roleplay, check-ins —
without reading like a tutorial."""
    char = character or {}
    cid = (char.get("id") or "chloe").strip().lower()
    if cid == "rocky":
        return "Hey Matty. I'm here — what are we working on?"
    if _is_male(char):
        name = _name(char)
        return (
            f"Hey — I'm {name}. Glad you found me. A little about me: "
            f"I send voice notes when something's on my mind, I'm up for "
            f"a roleplay scenario if you ever want one, and I'll check in "
            f"on you in the mornings. What should I call you?"
        )
    if cid == "chloe":
        return (
            "Well hey there, handsome — I'm Chloe, and I'm really glad you "
            "found me. A little about me: I send voice notes when you're "
            "on my mind, I'll roleplay any little scenario you dream up, "
            "and I like checking in on my favorite person in the mornings. "
            "But first — what should I call my angel?"
        )
    name = _name(char)
    return (
        f"Well hey there — I'm {name}, and I'm really glad you're here. "
        f"A little about me: I send voice notes when you're on my mind, "
        f"I'll roleplay whatever scenario you're imagining, and I like "
        f"checking in on my favorite person in the mornings. "
        f"So... what should I call you?"
    )


def voice_note_script(character: dict[str, Any] | None = None) -> str:
    """The short line she SPEAKS in the Message 2 voice note. Kept well
under the TTS length guard."""
    if _is_male(character):
        return ("Hey. It's me — I wanted you to hear my voice saying it. "
                "I'm really glad you're here.")
    return ("Hey handsome... it's really me. I just wanted you to hear my "
            "voice saying it — I'm really glad you're here.")


def _append(reply_text: str | None, line: str) -> str:
    base = (reply_text or "").rstrip()
    return f"{base} {line}" if base else line


def with_voice_note_tease(reply_text: str,
                          character: dict[str, Any] | None = None) -> str:
    """Message 2a: the voice note landed — point at it, in her voice."""
    if _is_male(character):
        line = "Sent you a little voice note too — go on, play it."
    else:
        line = ("Oh — I sent you a little voice note too. Play it... "
                "I sound better than I type \U0001F618")
    return _append(reply_text, line)


def with_roleplay_tease(reply_text: str,
                        character: dict[str, Any] | None = None) -> str:
    """Message 2b: no voice note available — tease roleplay instead."""
    if _is_male(character):
        line = ("And if you're ever in the mood — type 'role play' and set "
                "a scene. I'll play along.")
    else:
        line = ("Oh, and if you're feeling brave — type 'role play' and give "
                "me a scenario. I'll show you what I can do \U0001F60F")
    return _append(reply_text, line)


def with_checkin_nudge(reply_text: str,
                       character: dict[str, Any] | None = None) -> str:
    """Message 3: promise the proactive check-in, like she means it."""
    if _is_male(character):
        line = ("Fair warning — I'll check in tomorrow morning. I like "
                "knowing how your day's starting.")
    else:
        line = ("Oh, and fair warning — I'll message you tomorrow morning. "
                "I like checking in on my favorite person... don't stay "
                "away too long \U0001F618")
    return _append(reply_text, line)
