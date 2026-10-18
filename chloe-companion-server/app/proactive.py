"""Proactive messaging — live implementation (2026-10-16, feat/proactive-live).

The scheduler (app/proactive_scheduler.py, started from main._startup)
ticks every 5 minutes and, for every (user, character) pair with real
conversation history:

  morning — once a day, ~8am in the user's timezone (users.timezone,
            default America/New_York). Only when he was active in the
            last 48h — lapsed users get nudges instead.
  nudge   — when he hasn't written to that character in 24h+.

Generation goes through brain.gemini_generate (live Gemini with an
initiation instruction, run through her normal audit pipeline), so
every opener is in character. Falls back to a safe generic line when
Gemini is unavailable — the scheduler never breaks on generation.

Delivery:
  Telegram — user_ids shaped "tg_<chat_id>" (linked via
             POST /api/v1/auth/telegram) are sent through the Whispr
             Telegram bot's Bot API token (config.TELEGRAM_BOT_TOKEN —
             the same bot telegram/whispr_bot.py runs in the
             Sorority-house repo; Bot API sends are stateless, so the
             bot keeps polling there undisturbed).
  Website  — the message is stored in proactive_outbox with status
             'pending'; the PWA polls GET /api/v1/proactive/pending on
             login and on an interval, renders it, then acks it
             (POST /api/v1/proactive/ack — the ack writes the turn into
             chat history exactly once). A Web Push notification (VAPID)
             goes out at queue time when the user has a push
             subscription.

Gating (paid-tier): the owner, users with messages remaining, or users
who have ever paid (Stripe subscription, the $0.99 trial, or an active
JVZoo subscription). users.proactive_enabled is the user's own opt-out —
wired to the in-app toggle at POST /api/v1/proactive/preference and
backfilled to 1 for everyone who already passes the gate.

Tables (proactive_outbox, proactive_state, push_subscriptions) are
created lazily by _ensure_tables — the same pattern as
scheduler._ensure_tables — so any install self-heals on the next
scheduler tick or API call.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import brain, characters, config, db, store

MESSAGE_TYPES = ("morning", "nudge")

# Per-user daily cap (mirrors scheduler.MAX_PROACTIVE_PER_DAY).
MAX_PROACTIVE_PER_DAY = 2


def _owner_ids() -> set[str]:
    raw = os.environ.get("ASSIST_OWNER_IDS", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _is_owner(user_id: str | None) -> bool:
    return bool(user_id) and user_id in _owner_ids()


# ---------------------------------------------------------------------------
# Tables (lazy — same pattern as scheduler._ensure_tables)
# ---------------------------------------------------------------------------

def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS proactive_outbox (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT NOT NULL,
            character_id TEXT NOT NULL DEFAULT 'chloe',
            message_type TEXT NOT NULL DEFAULT 'morning',
            text         TEXT NOT NULL DEFAULT '',
            channel      TEXT NOT NULL DEFAULT 'website',
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            sent_at      TEXT,
            delivered_at TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_proactive_outbox_user "
        "ON proactive_outbox(user_id, status)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS proactive_state (
            user_id           TEXT NOT NULL,
            character_id      TEXT NOT NULL DEFAULT 'chloe',
            last_morning_date TEXT NOT NULL DEFAULT '',
            last_nudge_at     TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (user_id, character_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS push_subscriptions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT NOT NULL,
            endpoint   TEXT NOT NULL UNIQUE,
            p256dh     TEXT NOT NULL DEFAULT '',
            auth       TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_push_subs_user "
        "ON push_subscriptions(user_id)"
    )
    # Backfill the opt-in flag for everyone who already passes the
    # paid-tier gate. Idempotent — users who later toggle off in the app
    # are never re-enabled by this.
    try:
        conn.execute(
            """UPDATE users SET proactive_enabled = 1
               WHERE proactive_enabled = 0 AND (
                   messages_remaining > 0
                   OR user_id IN (SELECT user_id FROM stripe_customers)
                   OR user_id IN (SELECT user_id FROM trial_grants)
                   OR user_id IN (SELECT user_id FROM jvzoo_subscriptions
                                  WHERE status = 'active')
               )"""
        )
    except sqlite3.OperationalError:
        pass  # a purchase table is missing on a fresh install — nothing to do
    conn.commit()


# ---------------------------------------------------------------------------
# Database: pairs from actual conversation history
# ---------------------------------------------------------------------------

def get_all_user_character_pairs(
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Every (user_id, character_id) pair where the user has actually
    chatted (at least one user-role turn), with the user fields the gate
    and the scheduler need. Never raises — a DB hiccup reads as 'no
    pairs' so the scheduler never crashes the process."""
    own = conn is None
    conn = conn or db.connect()
    try:
        _ensure_tables(conn)
        rows = conn.execute(
            """SELECT DISTINCT c.user_id, c.character_id,
                      u.name, u.tier, u.messages_remaining,
                      u.proactive_enabled, u.timezone
               FROM conversation c
               JOIN users u ON u.user_id = c.user_id
               WHERE c.role = 'user'"""
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        if own:
            conn.close()


def user_can_receive(conn: sqlite3.Connection, user_id: str,
                     user: dict[str, Any] | None = None) -> bool:
    """Paid-tier gate: the owner, anyone with messages remaining, or
    anyone who has ever paid (Stripe subscription, the $0.99 trial, or an
    active JVZoo subscription). The user's proactive_enabled flag is
    their opt-out — off means no proactive messages."""
    if _is_owner(user_id):
        return True
    if user is None:
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        except Exception:
            return False
        user = dict(row) if row else {}
    if not user.get("proactive_enabled"):
        return False
    if (user.get("messages_remaining") or 0) > 0:
        return True
    try:
        return store.has_paid(conn, user_id)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

def _user_local_now(timezone: str | None,
                    now: datetime | None = None) -> datetime:
    base = now or datetime.now()
    try:
        tz = ZoneInfo(timezone or "America/New_York")
    except Exception:
        tz = ZoneInfo("America/New_York")
    if base.tzinfo is None:
        return base.replace(tzinfo=tz)
    return base.astimezone(tz)


def _last_user_turn_at(conn: sqlite3.Connection, user_id: str,
                       character_id: str) -> datetime | None:
    try:
        row = conn.execute(
            """SELECT created_at FROM conversation
               WHERE user_id = ? AND character_id = ? AND role = 'user'
               ORDER BY id DESC LIMIT 1""",
            (user_id, character_id)).fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


def _proactive_state(conn: sqlite3.Connection, user_id: str,
                     character_id: str) -> dict[str, Any]:
    try:
        row = conn.execute(
            "SELECT * FROM proactive_state WHERE user_id = ? AND character_id = ?",
            (user_id, character_id)).fetchone()
    except Exception:
        row = None
    if row:
        return dict(row)
    return {"user_id": user_id, "character_id": character_id,
            "last_morning_date": "", "last_nudge_at": ""}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _get_user(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    except Exception:
        return {}
    return dict(row) if row else {}


def should_send_morning_message(conn: sqlite3.Connection, user_id: str,
                                character_id: str,
                                now: datetime | None = None) -> bool:
    """Due when it's morning (8-10am) in the user's timezone and she
    hasn't sent today's morning message to this character yet. Only for
    recently-active users (a turn in the last 48h) — lapsed users get
    nudges instead. Skipped when he's been actively chatting in the last
    hour — she's already talking to him."""
    _ensure_tables(conn)
    user = _get_user(conn, user_id)
    if not user_can_receive(conn, user_id, user):
        return False
    local = _user_local_now(user.get("timezone"), now)
    if not (config.PROACTIVE_MORNING_HOUR
            <= local.hour < config.PROACTIVE_MORNING_HOUR + 2):
        return False
    state = _proactive_state(conn, user_id, character_id)
    if state["last_morning_date"] == local.strftime("%Y-%m-%d"):
        return False
    tick = now or datetime.now()
    last_turn = _last_user_turn_at(conn, user_id, character_id)
    if last_turn is None:
        return False
    if (tick - last_turn) < timedelta(hours=1):
        return False  # already chatting with her
    if (tick - last_turn) > timedelta(hours=48):
        return False  # lapsed — the nudge path owns re-engagement
    return True


def should_send_nudge(conn: sqlite3.Connection, user_id: str,
                      character_id: str,
                      now: datetime | None = None) -> bool:
    """Due when he hasn't written to this character in 24h+ and she
    hasn't nudged in the last 24h. Never on a day she already sent the
    morning message — one opener a day is enough."""
    _ensure_tables(conn)
    user = _get_user(conn, user_id)
    if not user_can_receive(conn, user_id, user):
        return False
    tick = now or datetime.now()
    last_turn = _last_user_turn_at(conn, user_id, character_id)
    if last_turn is None:
        return False
    if (tick - last_turn) < timedelta(hours=config.PROACTIVE_NUDGE_HOURS):
        return False
    state = _proactive_state(conn, user_id, character_id)
    last_nudge = _parse_ts(state["last_nudge_at"])
    if last_nudge and (tick - last_nudge) < timedelta(
            hours=config.PROACTIVE_NUDGE_HOURS):
        return False
    local = _user_local_now(user.get("timezone"), now)
    if state["last_morning_date"] == local.strftime("%Y-%m-%d"):
        return False
    return True


# ---------------------------------------------------------------------------
# Generation (brain.py) + delivery
# ---------------------------------------------------------------------------

def _moment_for(conn: sqlite3.Connection, user_id: str,
                character_id: str) -> dict[str, Any] | None:
    """Her latest visible Her Day post for this user, if any — the
    'scene' a proactive opener can hook into (2026-10-17)."""
    try:
        posts = store.get_her_day_posts(conn, user_id, character_id)
        if posts:
            return posts[0]
    except Exception:
        pass
    return None


def _abs_media(media_url: str) -> str:
    url = (media_url or "").strip()
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return config.PUBLIC_APP_URL + "/" + url.lstrip("/")


def generate_proactive_message(conn: sqlite3.Connection, user_id: str,
                               character_id: str, message_type: str) -> str:
    """Generate one opener in character via brain.py's live Gemini
    (initiation instruction, same audit pipeline as normal replies).
    Never raises — falls back to a safe generic line so the scheduler
    never dies on generation."""
    mtype = message_type if message_type in MESSAGE_TYPES else "nudge"
    try:
        user = store.get_or_create_user(conn, user_id)
        character = characters.get_character(character_id, user_id)
        cid = character.get("id") or "chloe"
        seed = "good morning" if mtype == "morning" else "it's been a while"
        ctx = brain.build_brain_context(conn, user_id, user, seed, character)
        anniv_clause = ""
        try:
            _alocal = _user_local_now(user.get("timezone"))
            _adays = store.anniversary_today(
                conn, user_id, character_id, _alocal.strftime("%Y-%m-%d"))
            if _adays:
                anniv_clause = (
                    f" Today marks {_adays} days since you two met — "
                    "make this message feel special: acknowledge the "
                    "milestone warmly and suggest doing something "
                    "together to celebrate.")
        except Exception:
            anniv_clause = ""
        streak_clause = ""
        if mtype == "morning":
            try:
                _local = _user_local_now(user.get("timezone"))
                _streak = store.get_streak(
                    conn, user_id, character_id,
                    _local.strftime("%Y-%m-%d"),
                    (_local - timedelta(days=1)).strftime("%Y-%m-%d"))
                if _streak >= 3:
                    streak_clause = (
                        f" You two have talked {_streak} days in a row — "
                        "if it fits naturally, acknowledge the streak "
                        "warmly in one short clause.")
            except Exception:
                streak_clause = ""
        if mtype == "morning":
            moment = _moment_for(conn, user_id, character_id)
            mcap = ((moment or {}).get("caption") or "").strip()
            if mcap:
                prompt = (
                    "Write a brief, warm morning greeting to him, in your "
                    "voice, as if you're texting him first thing. Today you "
                    "posted this: \"" + mcap[:160] + "\" Open with a playful "
                    "hook about it and invite him into your day, like you "
                    "want him there with you. 2-3 short sentences. "
                    "Do not mention being an AI."
                ) + streak_clause + anniv_clause
            else:
                prompt = (
                    "Write a brief, warm morning greeting to him, in your voice, "
                    "as if you're texting him first thing. 1-2 short sentences. "
                    "Do not mention being an AI."
                ) + streak_clause + anniv_clause
        else:
            prompt = (
                "Write a brief, warm check-in to him — it's been a while since "
                "he messaged you and you miss him. 1-2 short sentences, in your "
                "voice, like a text. Do not mention being an AI."
            )
        draft = brain.gemini_generate(prompt, ctx, cid)
        if draft and draft.strip():
            if cid == "rocky":
                draft = brain._sanitize_rocky_reply(draft)
            if not brain._polices_his_speech(draft):
                text = brain.audit_reply(draft.strip(), ctx)
                if text and text.strip():
                    return text.strip()
    except Exception as e:
        print(f"[proactive] generation failed for {user_id}/{character_id}: "
              f"{e.__class__.__name__}", flush=True)
    return ("good morning. hope today treats you right."
            if mtype == "morning"
            else "hey — it's been a while. you good?")


def _channel_for(user_id: str) -> str:
    return "telegram" if (user_id or "").startswith("tg_") else "website"


def queue_proactive_message(conn: sqlite3.Connection, user_id: str,
                            character_id: str, message_type: str, text: str,
                            channel: str) -> dict[str, Any]:
    cur = conn.execute(
        """INSERT INTO proactive_outbox
               (user_id, character_id, message_type, text, channel, status)
           VALUES (?, ?, ?, ?, ?, 'pending')""",
        (user_id, character_id, message_type, text, channel))
    conn.commit()
    row = conn.execute("SELECT * FROM proactive_outbox WHERE id = ?",
                       (cur.lastrowid,)).fetchone()
    return dict(row)


def send_telegram_message(chat_id: str, text: str) -> bool:
    """Send through the Whispr bot's Bot API. Stateless — the
    Sorority-house bot keeps polling; we only send."""
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        print("[proactive] TELEGRAM_BOT_TOKEN unset; skipping Telegram send",
              flush=True)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "chloe-companion/proactive"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            return bool(data.get("ok"))
    except Exception as e:
        print(f"[proactive] telegram send failed: {e.__class__.__name__}",
              flush=True)
        return False


def send_telegram_photo(chat_id: str, photo_url: str, caption: str) -> bool:
    """Send a photo with caption through the Whispr bot's Bot API
    (morning scene delivery: her today's-post photo + the opener)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not photo_url:
        return False
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = json.dumps({"chat_id": chat_id, "photo": photo_url,
                          "caption": (caption or "")[:1000]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "chloe-companion/proactive"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
            return bool(data.get("ok"))
    except Exception as e:
        print(f"[proactive] telegram photo send failed: {e.__class__.__name__}",
              flush=True)
        return False


def send_web_push(conn: sqlite3.Connection, user_id: str, title: str,
                  body: str, url: str = "", image: str = "") -> int:
    """Web Push to all of the user's subscriptions. Returns the number
    accepted. Gone (404/410) subscriptions are pruned. Never raises;
    returns 0 when pywebpush or the VAPID keys are missing."""
    if not config.VAPID_PRIVATE_KEY or not config.VAPID_PUBLIC_KEY:
        return 0
    try:
        from pywebpush import webpush
    except ImportError:
        return 0
    try:
        subs = conn.execute(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions "
            "WHERE user_id = ?", (user_id,)).fetchall()
    except Exception:
        return 0
    sent = 0
    for s in subs:
        sub_info = {"endpoint": s["endpoint"],
                    "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}
        try:
            payload = {"title": title, "body": body, "url": url}
            if image:
                payload["image"] = image
            webpush(subscription_info=sub_info,
                    data=json.dumps(payload),
                    vapid_private_key=config.VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": config.VAPID_MAILTO})
            sent += 1
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code",
                             None)
            if status in (404, 410):
                try:
                    conn.execute("DELETE FROM push_subscriptions WHERE id = ?",
                                 (s["id"],))
                    conn.commit()
                except Exception:
                    pass
            else:
                print(f"[proactive] web push failed: {e.__class__.__name__}",
                      flush=True)
    return sent


def _record_sent(conn: sqlite3.Connection, user_id: str, character_id: str,
                 message_type: str, today_str: str) -> None:
    conn.execute(
        """INSERT INTO proactive_state (user_id, character_id,
                                        last_morning_date, last_nudge_at)
           VALUES (?, ?, '', '')
           ON CONFLICT(user_id, character_id) DO NOTHING""",
        (user_id, character_id))
    if message_type == "morning":
        conn.execute(
            "UPDATE proactive_state SET last_morning_date = ? "
            "WHERE user_id = ? AND character_id = ?",
            (today_str, user_id, character_id))
    else:
        conn.execute(
            "UPDATE proactive_state SET last_nudge_at = datetime('now') "
            "WHERE user_id = ? AND character_id = ?",
            (user_id, character_id))
    conn.execute(
        "UPDATE users SET proactive_sent_today = proactive_sent_today + 1, "
        "proactive_day = ? WHERE user_id = ?",
        (today_str, user_id))
    conn.commit()


def record_proactive_sent(user_id: str, character_id: str,
                          message_type: str = "nudge",
                          conn: sqlite3.Connection | None = None) -> None:
    """Public bookkeeping (kept from the stub): mark a proactive message
    sent for this pair."""
    own = conn is None
    conn = conn or db.connect()
    try:
        _ensure_tables(conn)
        user = _get_user(conn, user_id)
        local = _user_local_now(user.get("timezone"))
        _record_sent(conn, user_id, character_id, message_type,
                     local.strftime("%Y-%m-%d"))
    finally:
        if own:
            conn.close()


def deliver_proactive_message(conn: sqlite3.Connection, user_id: str,
                              character_id: str, message_type: str) -> bool:
    """Generate + deliver one proactive message. Returns True when the
    message was generated and handed to a delivery path."""
    _ensure_tables(conn)
    mtype = message_type if message_type in MESSAGE_TYPES else "nudge"
    user = _get_user(conn, user_id)
    local = _user_local_now(user.get("timezone"))
    today_str = local.strftime("%Y-%m-%d")
    # Per-user daily cap: max 2 proactive messages a day (local day).
    if user.get("proactive_day") != today_str:
        try:
            conn.execute(
                "UPDATE users SET proactive_day = ?, proactive_sent_today = 0 "
                "WHERE user_id = ?", (today_str, user_id))
            conn.commit()
        except Exception:
            pass
        user["proactive_day"] = today_str
        user["proactive_sent_today"] = 0
    if (user.get("proactive_sent_today") or 0) >= MAX_PROACTIVE_PER_DAY:
        return False

    text = generate_proactive_message(conn, user_id, character_id, mtype)
    channel = _channel_for(user_id)
    msg = queue_proactive_message(conn, user_id, character_id, mtype, text,
                                  channel)

    if config.SCHEDULER_DRY_RUN:
        conn.execute("UPDATE proactive_outbox SET status = 'dry_run' "
                     "WHERE id = ?", (msg["id"],))
        conn.commit()
        # No _record_sent here: a dry run must not burn the day's slot.
        # Recording it as sent marks last_morning_date = today and
        # bumps the daily counter, so the real send never fires once
        # dry-run is switched off (exactly what ate the morning of
        # 2026-10-17 — dry-run ticks ran 8:00-8:56am, the fix deployed
        # mid-window, and every later tick saw "already sent today").
        print(f"[proactive] dry-run: {mtype} for {user_id}/{character_id}",
              flush=True)
        return True

    character = characters.get_character(character_id, user_id)
    char_name = character.get("name") or "Chloe"

    if channel == "telegram":
        ok = False
        if mtype == "morning":
            moment = _moment_for(conn, user_id, character_id)
            if (moment and moment.get("post_type") == "image"
                    and moment.get("media_url")):
                ok = send_telegram_photo(user_id[3:],
                                         _abs_media(moment["media_url"]),
                                         text)
        if not ok:
            ok = send_telegram_message(user_id[3:], text)
        conn.execute(
            "UPDATE proactive_outbox SET status = ?, sent_at = datetime('now') "
            "WHERE id = ?", ("sent" if ok else "failed", msg["id"]))
        if ok:
            # The turn lands in her chat history so every client sees it.
            store.log_turn(conn, user_id, character_id, text, character_id)
        conn.commit()
    else:
        # Website: stays 'pending' until the PWA polls + acks it (the ack
        # writes the turn into history). Best-effort push right now.
        try:
            push_url = config.PUBLIC_APP_URL + "/"
            push_image = ""
            if mtype == "morning":
                push_url = (config.PUBLIC_APP_URL + "/?character="
                            + character_id + "&moment=1")
                moment = _moment_for(conn, user_id, character_id)
                if (moment and moment.get("post_type") == "image"
                        and moment.get("media_url")):
                    push_image = _abs_media(moment["media_url"])
            send_web_push(conn, user_id, char_name, text,
                          url=push_url, image=push_image)
        except Exception:
            pass
        conn.execute(
            "UPDATE proactive_outbox SET sent_at = datetime('now') "
            "WHERE id = ?", (msg["id"],))
        conn.commit()

    _record_sent(conn, user_id, character_id, mtype, today_str)
    return True


# ---------------------------------------------------------------------------
# Website delivery: outbox polling
# ---------------------------------------------------------------------------

def get_pending_messages(conn: sqlite3.Connection,
                         user_id: str) -> list[dict[str, Any]]:
    _ensure_tables(conn)
    try:
        rows = conn.execute(
            """SELECT id, character_id, message_type, text, created_at
               FROM proactive_outbox
               WHERE user_id = ? AND status = 'pending'
               ORDER BY id""",
            (user_id,)).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


def ack_messages(conn: sqlite3.Connection, user_id: str,
                 ids: list[int] | None) -> int:
    """Mark outbox rows delivered (the client rendered them) and write
    the turns into conversation history. Idempotent."""
    _ensure_tables(conn)
    acked = 0
    clean_ids: list[int] = []
    for i in ids or []:
        try:
            clean_ids.append(int(i))
        except (TypeError, ValueError):
            continue
    for mid in clean_ids:
        row = conn.execute(
            """SELECT id, character_id, text FROM proactive_outbox
               WHERE id = ? AND user_id = ? AND status = 'pending'""",
            (mid, user_id)).fetchone()
        if not row:
            continue
        store.log_turn(conn, user_id, row["character_id"], row["text"],
                       row["character_id"])
        conn.execute(
            "UPDATE proactive_outbox SET status = 'delivered', "
            "delivered_at = datetime('now') WHERE id = ?", (mid,))
        acked += 1
    conn.commit()
    return acked


# ---------------------------------------------------------------------------
# Push subscriptions + user preference
# ---------------------------------------------------------------------------

def save_push_subscription(conn: sqlite3.Connection, user_id: str,
                           endpoint: str, p256dh: str, auth: str) -> bool:
    _ensure_tables(conn)
    endpoint = (endpoint or "").strip()
    if not endpoint or not endpoint.startswith("https://"):
        return False
    conn.execute(
        """INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(endpoint) DO UPDATE SET
               user_id = excluded.user_id,
               p256dh = excluded.p256dh,
               auth = excluded.auth""",
        (user_id, endpoint, p256dh or "", auth or ""))
    conn.commit()
    return True


def set_proactive_preference(conn: sqlite3.Connection, user_id: str,
                             enabled: bool) -> None:
    _ensure_tables(conn)
    conn.execute(
        "UPDATE users SET proactive_enabled = ?, updated_at = datetime('now') "
        "WHERE user_id = ?", (1 if enabled else 0, user_id))
    conn.commit()


def get_proactive_preference(conn: sqlite3.Connection,
                             user_id: str) -> bool:
    _ensure_tables(conn)
    try:
        row = conn.execute(
            "SELECT proactive_enabled FROM users WHERE user_id = ?",
            (user_id,)).fetchone()
    except Exception:
        return False
    return bool(row and row[0])
