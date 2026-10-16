"""Proactive scheduler - auto-wires on import.

Checks for due proactive messages (morning + nudges) and generates them.
Just add `from . import proactive_scheduler` to main.py imports.
"""

import threading
import time
from . import proactive

# Configuration
CHECK_INTERVAL_SECONDS = 300  # Check every 5 minutes
_scheduler_started = False
_scheduler_thread = None

def _get_all_user_character_pairs():
    """
    Get all (user_id, character_id) pairs that should receive proactive messages.
    TODO: Query from database. For now, returns empty - needs DB integration.
    """
    # This needs to query the database for:
    # - All users with paid tier (or all users?)
    # - Their active character conversations
    # For now, return empty list - the wiring needs DB access
    return []

def _generate_and_queue_proactive(user_id: str, character_id: str, message_type: str):
    """
    Generate a proactive message and queue it for delivery.
    TODO: Integrate with brain.py for LLM generation and delivery mechanisms.
    """
    try:
        # Get character info
        # TODO: Get character name and persona from characters module
        
        # Generate prompt
        # prompt = proactive.get_proactive_prompt(message_type, character_name)
        
        # TODO: Call brain.py to generate the actual message text
        # TODO: Store in DB as outbound proactive message
        # TODO: Deliver via Telegram (if user has Telegram linked)
        # TODO: Make available for website polling
        
        proactive.record_proactive_sent(user_id, character_id)
        return True
    except Exception:
        return False

def _scheduler_loop():
    """Background loop that checks for due proactive messages."""
    while True:
        try:
            time.sleep(CHECK_INTERVAL_SECONDS)
            
            # Get all user/character pairs
            pairs = _get_all_user_character_pairs()
            
            for user_id, character_id in pairs:
                # Check morning message
                # TODO: Get user's timezone/hour
                if proactive.should_send_morning_message(user_id, character_id):
                    _generate_and_queue_proactive(user_id, character_id, "morning")
                
                # Check nudge
                elif proactive.should_send_nudge(user_id, character_id):
                    _generate_and_queue_proactive(user_id, character_id, "nudge")
                    
        except Exception:
            # Don't crash the scheduler on errors
            time.sleep(60)

def start_scheduler():
    """Start the background scheduler thread. Idempotent."""
    global _scheduler_started, _scheduler_thread
    if _scheduler_started:
        return
    _scheduler_started = True
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()

# Auto-start on import
start_scheduler()
