"""Proactive scheduler — live implementation (2026-10-16, feat/proactive-live).

Started explicitly from main._startup via start_scheduler() — nothing
happens on import. Every TICK_SECONDS (5 min) it asks
proactive.get_all_user_character_pairs() for every (user, character)
pair with real conversation history and, for each pair:

  morning — proactive.should_send_morning_message(...)
  nudge   — proactive.should_send_nudge(...)

Due messages are generated + delivered through
proactive.deliver_proactive_message(...). Per-pair errors are caught and
logged so one bad pair never stops the tick.
"""
from __future__ import annotations

import threading
import time
import traceback

from . import db, proactive

TICK_SECONDS = 300  # 5 minutes

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_started = False
_lock = threading.Lock()


def _tick() -> None:
    conn = db.connect()
    try:
        pairs = proactive.get_all_user_character_pairs(conn)
    except Exception:
        conn.close()
        return
    for pair in pairs:
        user_id = pair.get("user_id")
        character_id = pair.get("character_id") or "chloe"
        if not user_id:
            continue
        try:
            if proactive.should_send_morning_message(conn, user_id,
                                                    character_id):
                proactive.deliver_proactive_message(conn, user_id,
                                                  character_id, "morning")
            elif proactive.should_send_nudge(conn, user_id, character_id):
                proactive.deliver_proactive_message(conn, user_id,
                                                  character_id, "nudge")
            elif proactive.should_send_ambient(conn, user_id,
                                               character_id):
                proactive.deliver_proactive_message(conn, user_id,
                                                    character_id,
                                                    "ambient")
        except Exception:
            print(f"[proactive-scheduler] pair failed "
                  f"{user_id}/{character_id}:", flush=True)
            traceback.print_exc()
    conn.close()


def _loop() -> None:
    while not _stop_event.is_set():
        try:
            _tick()
        except Exception:
            print("[proactive-scheduler] tick failed:", flush=True)
            traceback.print_exc()
        _stop_event.wait(TICK_SECONDS)


def start_scheduler() -> bool:
    """Start the background scheduler thread. Idempotent — returns True
    when the scheduler is running (or was already). Called from
    main._startup; never on import."""
    global _thread, _started
    with _lock:
        if _started and _thread is not None and _thread.is_alive():
            return True
        _stop_event.clear()
        _thread = threading.Thread(target=_loop,
                                   name="proactive-scheduler",
                                   daemon=True)
        _thread.start()
        _started = True
        print("[proactive-scheduler] started (tick every 300s)", flush=True)
        return True


def stop_scheduler() -> None:
    """Stop the background thread (tests / shutdown)."""
    global _thread, _started
    with _lock:
        _stop_event.set()
        _thread = None
        _started = False
