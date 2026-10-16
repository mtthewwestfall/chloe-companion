"""Roleplay trigger handler for Whispr chat.

Intercepts chat messages to detect roleplay start/end triggers.
Manages per-user per-character roleplay mode state.

Auto-wires into main.chat() on import — just add:
    from . import roleplay_handler
to main.py imports.
"""

from . import roleplay_mode

# In-memory mode tracking: (user_id, character_id) -> bool
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


# Auto-wire: patch main.chat to check triggers
def _wire_into_main():
    """Patch the chat function to handle roleplay triggers."""
    try:
        from . import main as main_module
        original_chat = main_module.chat
        
        def patched_chat(body, request):
            # Extract user_id, character, message (best effort)
            try:
                user_id = main_module._current_user_id(request)
                # Get character info from body
                cid = getattr(body, 'character_id', 'chloe') or 'chloe'
                msg = getattr(body, 'message', '') or ''
                
                # Try to get character name
                try:
                    char = main_module.characters.get_character(cid, user_id)
                    cname = char.get('name', cid)
                    cid = char.get('id', cid)
                except:
                    cname = cid
                
                # Check triggers
                response_text, in_rp = handle_roleplay_triggers(user_id, cid, cname, msg)
                if response_text:
                    # Return trigger response directly (bypass AI)
                    # Format matches normal chat response
                    return {"reply": response_text, "character_id": cid}
            except Exception:
                pass  # Fall through to original on any error
            
            return original_chat(body, request)
        
        main_module.chat = patched_chat
    except Exception:
        pass  # Wiring is best-effort

# Run wiring on import
_wire_into_main()
