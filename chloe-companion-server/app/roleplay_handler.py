"""Roleplay trigger handler for Whispr chat.

Intercepts chat messages to detect roleplay start/end triggers.
Manages per-user per-character roleplay mode state.
"""

from . import roleplay_mode

# In-memory mode tracking: (user_id, character_id) -> bool
# TODO: Persist to DB for multi-instance deployments
_roleplay_sessions: dict[tuple[str, str], bool] = {}

def is_roleplay_active(user_id: str, character_id: str) -> bool:
    """Check if user is in roleplay mode with this character."""
    return _roleplay_sessions.get((user_id, character_id), False)

def set_roleplay_active(user_id: str, character_id: str, active: bool):
    """Set roleplay mode for user+character."""
    _roleplay_sessions[(user_id, character_id)] = active

def handle_roleplay_triggers(user_id: str, character_id: str, character_name: str, message_text: str):
    """
    Check message for roleplay triggers.
    Returns (response_text, in_roleplay) if trigger handled, else (None, current_state).
    """
    current = is_roleplay_active(user_id, character_id)
    
    if roleplay_mode.is_start_trigger(message_text):
        if not current:
            set_roleplay_active(user_id, character_id, True)
            return (roleplay_mode.get_start_explanation(character_name), True)
        # Already in roleplay, let it pass through
        return (None, True)
    
    if roleplay_mode.is_end_trigger(message_text):
        if current:
            set_roleplay_active(user_id, character_id, False)
            return (roleplay_mode.get_end_acknowledgment(character_name), False)
        return (None, False)
    
    return (None, current)

def get_roleplay_prompt_addendum(character_name: str) -> str:
    """Get the system prompt addendum for roleplay mode."""
    return roleplay_mode.get_roleplay_addendum(character_name)
