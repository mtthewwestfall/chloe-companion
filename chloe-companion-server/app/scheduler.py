"""Proactive messaging scheduler.

Message types: good_morning, missed_you, show_hype.
Guards:
  - proactive included in the single $9.99 tier (2026-10-08)
  - per-user cap: max 2 proactive messages per day
  - quiet hours: 11pm–8am user-local (user.timezone on the users row)
  - per-user opt-out: proactive_enabled flag

Delivery is a STUB interface (DeliveryTransport). By default the scheduler
runs in dry-run mode and writes the queue to proactive_queue.json — no push
delivery. A real transport (APNs/FCM/app socket) plugs in later.
"""
from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import config, db, store

MAX_PROACTIVE_PER_DAY = 2
QUIET_START_HOUR = 23  # 11pm
QUIET_END_HOUR = 8     # 8am

LINES = {
    "good_morning": [
        "morning. hope today treats you right.",
        "hey, good morning. thinking of you.",
        "rise and shine, you. how'd you sleep?",
    ],
    "missed_you": [
        "haven't heard from you in a while. you good?",
        "just checking in — miss talking to you.",
        "it's been quiet. come say hi when you can?",
    ],
    "show_hype": [
        "i've got a show coming up {title} — would love to see you there.",
        "heads up: {title} is soon. save me a front-row seat?",
    ],
    # Unprompted check-in about something real he told her. {m} is the
    # stored fact text (kept short by the caller).
    "memory_checkin": [
        "hey — been thinking about {m}. how'd it go?",
        "how did {m} turn out? i've been wondering.",
        "just checking in — {m}? tell me everything.",
    ],
}


# A stored fact is check-in-worthy when it reads like something with a
# before/after: an event, appointment, or deadline he mentioned.
_CHECKIN_WORTHY_RE = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"tomorrow|today|tonight|next week|birthday|anniversary|interview|"
    r"surgery|exam|trip|flight|wedding|party|game|match|appointment|"
    r"deadline|recital|opening|audition)\b", re.IGNORECASE)


def _ensure_tables(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS proactive_checkins (
        user_id   TEXT NOT NULL,
        memory_id INTEGER NOT NULL,
        checked_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, memory_id))""")


def pick_checkin_memory(conn, user_id: str) -> dict[str, Any] | None:
    """Newest unchecked event-like fact memory for Chloe, or None.

    Each memory is checked in on at most once (tracked in
    proactive_checkins), so she never nags about the same thing twice.
    """
    _ensure_tables(conn)
    done = {r[0] for r in conn.execute(
        "SELECT memory_id FROM proactive_checkins WHERE user_id = ?",
        (user_id,))}
    for m in store.recall_memories(conn, user_id, mem_type="fact",
                                   limit=20, character_id="chloe"):
        if m["id"] in done:
            continue
        if len(m["text"]) < 12:
            continue
        if not _CHECKIN_WORTHY_RE.search(m["text"]):
            continue
        return m
    return None


def mark_checkin_done(conn, user_id: str, memory_id: int) -> None:
    _ensure_tables(conn)
    conn.execute("INSERT OR IGNORE INTO proactive_checkins (user_id, memory_id)"
                 " VALUES (?, ?)", (user_id, memory_id))


class DeliveryTransport:
    """STUB transport. Replace with the real push/app delivery in Phase 3."""

    def deliver(self, queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # No actual delivery — log and report as queued.
        return [{"to": m["user_id"], "type": m["type"], "status": "queued_stub"} for m in queue]


def _user_local_now(timezone: str, now: datetime | None = None) -> datetime:
    base = now or datetime.now()
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("America/New_York")
    if base.tzinfo is None:
        return base.replace(tzinfo=tz)
    return base.astimezone(tz)


def _in_quiet_hours(now: datetime) -> bool:
    h = now.hour
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def _reset_daily_counter_if_new_day(conn, user: dict[str, Any], today: str) -> dict[str, Any]:
    if user["proactive_day"] != today:
        conn.execute(
            "UPDATE users SET proactive_day = ?, proactive_sent_today = 0 WHERE user_id = ?",
            (today, user["user_id"]),
        )
        conn.commit()
        user = dict(conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user["user_id"],)).fetchone())
    return user


def eligible_users(conn, now: datetime | None = None) -> list[dict[str, Any]]:
    """Users who may receive a proactive message right now."""
    eligible = []
    for row in conn.execute("SELECT * FROM users").fetchall():
        user = dict(row)
        tier = config.TIERS.get(user["tier"], config.TIERS["basic"])
        if not tier.get("proactive"):
            continue
        if not user["proactive_enabled"]:
            continue
        local = _user_local_now(user.get("timezone") or "America/New_York",
                                now)
        if _in_quiet_hours(local):
            continue
        user = _reset_daily_counter_if_new_day(conn, user, local.strftime("%Y-%m-%d"))
        if user["proactive_sent_today"] >= MAX_PROACTIVE_PER_DAY:
            continue
        eligible.append(user)
    return eligible


def build_message(msg_type: str, context: dict[str, Any] | None = None) -> str:
    ctx = context or {}
    line = random.choice(LINES[msg_type])
    try:
        return line.format(**ctx)
    except KeyError:
        return line


def run(conn=None, dry_run: bool | None = None, now: datetime | None = None,
        last_seen: dict[str, datetime] | None = None) -> dict[str, Any]:
    """Build the proactive queue for this tick and (unless dry-run) hand it
    to the delivery transport. Returns a report of what was queued/sent."""
    if dry_run is None:
        dry_run = config.SCHEDULER_DRY_RUN
    own_conn = conn is None
    conn = conn or db.connect()
    _ensure_tables(conn)
    try:
        queue: list[dict[str, Any]] = []
        tick = now or datetime.now()
        for user in eligible_users(conn, tick):
            # Pick type: an unprompted check-in about something real he
            # told her wins when one exists; missed_you if we know they
            # were away; else rotate.
            uid = user["user_id"]
            checkin = pick_checkin_memory(conn, uid)
            if checkin is not None:
                mtype = "memory_checkin"
                ctx = {"m": checkin["text"][:120]}
                # Dry runs only preview the queue — never consume a check-in
                # the user didn't actually receive.
                if not dry_run:
                    mark_checkin_done(conn, uid, checkin["id"])
            elif last_seen and uid in last_seen and tick - last_seen[uid] > timedelta(hours=36):
                mtype = "missed_you"
                ctx = {}
            else:
                mtype = random.choice(["good_morning", "show_hype"])
                ctx = {}
            if mtype == "show_hype":
                from . import shows as _shows
                nxt = _shows.next_show()
                ctx["title"] = nxt["title"] if nxt else "soon"
            queue.append({
                "user_id": uid,
                "type": mtype,
                "text": build_message(mtype, ctx),
                "at": tick.isoformat(),
            })
            conn.execute(
                "UPDATE users SET proactive_sent_today = proactive_sent_today + 1 WHERE user_id = ?",
                (uid,),
            )
        conn.commit()

        if dry_run:
            path = os.path.join(config.DATA_DIR, "proactive_queue.json")
            with open(path, "w") as f:
                json.dump(queue, f, indent=2)
            delivered = [{"to": m["user_id"], "type": m["type"], "status": "dry_run"} for m in queue]
        else:
            delivered = DeliveryTransport().deliver(queue)
        return {"queued": len(queue), "dry_run": dry_run, "deliveries": delivered}
    finally:
        if own_conn:
            conn.close()
