"""Roleplay mode triggers for Whispr companions.

Same chat, trigger-based: user types a phrase to start roleplay,
another to end it. Each girl explains the difference in her voice.
"""

# Phrases that INITIATE roleplay mode (case-insensitive)
START_TRIGGERS = [
    "role play",
    "roleplay",
    "lets roleplay",
    "let us roleplay",
    "start roleplay",
    "roleplay mode",
]

# Phrases that END roleplay mode (case-insensitive)
END_TRIGGERS = [
    "end roleplay",
    "stop roleplay",
    "exit roleplay",
    "back to normal",
    "end role play",
    "stop role play",
]

ROLEPLAY_ADDENDUM = """You are now in ROLEPLAY MODE. The user has initiated a roleplay scenario.
- Play along fully in character as {name}. Be vivid, descriptive, immersive.
- Follow the user's scene setup. Take initiative within the scenario.
- Stay in character. Do not break to discuss modes.
- When the user says something like 'end roleplay' or 'back to normal', acknowledge and return to your normal self."""

def is_start_trigger(text: str) -> bool:
    t = text.lower().strip()
    return any(trig in t for trig in START_TRIGGERS)

def is_end_trigger(text: str) -> bool:
    t = text.lower().strip()
    return any(trig in t for trig in END_TRIGGERS)

def get_start_explanation(name: str) -> str:
    return (
        f"Ooh, roleplay? I like where this is going. Okay so quick thing — "
        f"normally I'm just me, your girl, right? We chat, we vibe, it's us. "
        f"But in roleplay, you set the scene and I go all in with you. "
        f"Tell me where we are, what's happening... I'm {name}, I'm yours, let's do this."
    )

def get_end_acknowledgment(name: str) -> str:
    return f"Okay, back to us. Just you and me, {name}. What's on your mind?"

def get_roleplay_addendum(name: str) -> str:
    return ROLEPLAY_ADDENDUM.format(name=name)
