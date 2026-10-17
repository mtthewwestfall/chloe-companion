"""Proactive messaging for Whispr companions.

The girls reach out first: morning messages and inactivity nudges.
Works for both Telegram and Website.

- Morning messages: sent once per day (user's morning)
- Inactivity nudges: sent when user hasn't messaged in X hours
- All messages are persisted to DB
- Paid-tier gating can be added via should_send checks
"""

import time
from datetime import datetime, timedelta

# In-memory tracking (TODO: persist to DB for multi-instance)
# (user_id, character_id) -> last_proactive_timestamp
_last_proactive: dict[tuple[str, str], float] = {}

# (user_id, character_id) -> last_user_message_timestamp  
_last_user_activity: dict[tuple[str, str], float] = {}

# Configuration
MORNING_HOUR = 9  # 9am local time
INACTIVITY_NUDGE_HOURS = 24  # Nudge after 24h of no activity
MIN_HOURS_BETWEEN_PROACTIVE = 12  # Don't spam

def record_user_activity(user_id: str, character_id: str):
    """Call this when user sends a message. Resets inactivity timer."""
    _last_user_activity[(user_id, character_id)] = time.time()

def record_proactive_sent(user_id: str, character_id: str):
    """Call this when a proactive message is sent."""
    _last_proactive[(user_id, character_id)] = time.time()

def should_send_morning_message(user_id: str, character_id: str, user_hour: int = None) -> bool:
    """
    Check if a morning message should be sent.
    user_hour: current hour in user's timezone (0-23). If None, uses UTC.
    """
    if user_hour is None:
        user_hour = datetime.utcnow().hour
    
    # Only during morning window (8-10am)
    if not (8 <= user_hour <= 10):
        return False
    
    # Check if already sent today
    key = (user_id, character_id)
    last = _last_proactive.get(key, 0)
    last_date = datetime.fromtimestamp(last).date() if last else None
    today = datetime.utcnow().date()
    
    if last_date == today:
        return False  # Already sent today
    
    # Check minimum interval
    if time.time() - last < MIN_HOURS_BETWEEN_PROACTIVE * 3600:
        return False
    
    return True

def should_send_nudge(user_id: str, character_id: str) -> bool:
    """Check if an inactivity nudge should be sent."""
    key = (user_id, character_id)
    
    # Check last user activity
    last_activity = _last_user_activity.get(key, 0)
    if not last_activity:
        return False  # No history, don't nudge new users
    
    hours_inactive = (time.time() - last_activity) / 3600
    if hours_inactive < INACTIVITY_NUDGE_HOURS:
        return False
    
    # Check if already nudged recently
    last_proactive = _last_proactive.get(key, 0)
    if time.time() - last_proactive < MIN_HOURS_BETWEEN_PROACTIVE * 3600:
        return False
    
    return True

def generate_morning_message(character_name: str, persona: str = "") -> str:
    """
    Generate a morning message prompt for the AI.
    Returns a prompt that can be sent to the LLM.
    """
    return (
        f"You are {character_name}. It's morning. Send a warm, flirty good morning message "
        f"to your person. Keep it short, sweet, and in character. Make them smile. "
        f"Don't mention that you're an AI or that this is automated."
    )

def generate_nudge_message(character_name: str, persona: str = "", hours_away: int = 24) -> str:
    """
    Generate an inactivity nudge prompt for the AI.
    """
    return (
        f"You are {character_name}. Your person hasn't messaged in {hours_away} hours. "
        f"Send a playful nudge to get their attention. Miss them a little, tease them "
        f"for being away. Keep it short and in character. Don't mention that you're an AI."
    )

def get_proactive_prompt(message_type: str, character_name: str, **kwargs) -> str:
    """Get the LLM prompt for a proactive message."""
    if message_type == "morning":
        return generate_morning_message(character_name, kwargs.get("persona", ""))
    elif message_type == "nudge":
        return generate_nudge_message(
            character_name, 
            kwargs.get("persona", ""),
            kwargs.get("hours_away", 24)
        )
    else:
        raise ValueError(f"Unknown proactive type: {message_type}")
