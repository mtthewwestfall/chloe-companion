"""Per-user store: memory (typed, Thoughtspace pattern) + message banks.

Isolation: user_id is a required parameter on every public function and is
always bound into the WHERE clause. Cross-user leakage is impossible by
construction as long as callers use this module (tested).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import config, db

MEM_TYPES = ("core", "fact", "preference", "boundary", "tone")


# ---------------------------------------------------------------------------
# Users / banks
# ---------------------------------------------------------------------------
def get_or_create_user(conn: sqlite3.Connection, user_id: str, name: str | None = None) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row:
        return dict(row)
    tier = config.TIERS["basic"]
    conn.execute(
        """INSERT INTO users (user_id, name, tier, messages_remaining,
                              voice_notes_remaining, proactive_enabled)
           VALUES (?, ?, 'basic', ?, ?, 0)""",
        (user_id, name, tier["messages_per_month"], tier["voice_notes_per_month"]),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone())


def set_tier(conn: sqlite3.Connection, user_id: str, tier: str) -> dict[str, Any]:
    """Set tier and TOP UP banks to the tier allocation (no stacking)."""
    if tier not in config.TIERS:
        raise ValueError(f"unknown tier: {tier}")
    t = config.TIERS[tier]
    get_or_create_user(conn, user_id)  # ensure row exists
    conn.execute(
        """UPDATE users SET tier = ?, messages_remaining = ?,
                              voice_notes_remaining = ?,
                              proactive_enabled = ?,
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (tier, t["messages_per_month"], t["voice_notes_per_month"],
         1 if t["proactive"] else 0, user_id),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone())


def consume_message(conn: sqlite3.Connection, user_id: str) -> bool:
    """Decrement the message bank by one. Returns False (no crash, no
    negative balance) when the bank is empty."""
    row = conn.execute(
        "SELECT messages_remaining FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row:
        return False
    if row["messages_remaining"] <= 0:
        return False
    conn.execute(
        """UPDATE users SET messages_remaining = messages_remaining - 1,
                              messages_used_total = messages_used_total + 1,
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (user_id,),
    )
    conn.commit()
    return True


def consume_voice_note(conn: sqlite3.Connection, user_id: str) -> bool:
    row = conn.execute(
        "SELECT voice_notes_remaining FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if not row or row["voice_notes_remaining"] <= 0:
        return False
    conn.execute(
        """UPDATE users SET voice_notes_remaining = voice_notes_remaining - 1,
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (user_id,),
    )
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Purchase fulfillment — Stripe webhook support (2026-09-29)
# ---------------------------------------------------------------------------
def note_stripe_event(conn: sqlite3.Connection, event_id: str, event_type: str = "") -> bool:
    """Record a Stripe event id. Returns True when first seen, False on
    replay (idempotency: Stripe may redeliver events)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO stripe_events (event_id, event_type) VALUES (?, ?)",
        (event_id, event_type),
    )
    conn.commit()
    return cur.rowcount == 1


def link_stripe_customer(conn: sqlite3.Connection, customer_id: str,
                         subscription_id: str, user_id: str, tier: str) -> None:
    """Remember which companion user owns a Stripe customer/subscription so
    later invoices (renewals) and cancellations resolve without the checkout
    session's client_reference_id."""
    conn.execute(
        """INSERT INTO stripe_customers
               (stripe_customer_id, stripe_subscription_id, user_id, tier, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(stripe_customer_id) DO UPDATE SET
               stripe_subscription_id = excluded.stripe_subscription_id,
               user_id = excluded.user_id,
               tier = excluded.tier,
               updated_at = datetime('now')""",
        (customer_id, subscription_id, user_id, tier),
    )
    conn.commit()


def user_for_stripe_subscription(conn: sqlite3.Connection, subscription_id: str) -> str | None:
    row = conn.execute(
        "SELECT user_id FROM stripe_customers WHERE stripe_subscription_id = ?",
        (subscription_id,),
    ).fetchone()
    return row["user_id"] if row else None


def user_for_stripe_customer(conn: sqlite3.Connection, customer_id: str) -> str | None:
    row = conn.execute(
        "SELECT user_id FROM stripe_customers WHERE stripe_customer_id = ?",
        (customer_id,),
    ).fetchone()
    return row["user_id"] if row else None


def grant_trial_messages(conn: sqlite3.Connection, user_id: str, n: int) -> bool:
    """One-time trial credit: adds n messages. Returns True when granted,
    False when this user already used the trial (one per customer)."""
    get_or_create_user(conn, user_id)  # ensure row exists
    cur = conn.execute("INSERT OR IGNORE INTO trial_grants (user_id) VALUES (?)", (user_id,))
    if cur.rowcount == 0:
        conn.commit()
        return False
    conn.execute(
        """UPDATE users SET messages_remaining = messages_remaining + ?,
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (n, user_id),
    )
    conn.commit()
    return True


def grant_free_signup_messages(conn: sqlite3.Connection, user_id: str, n: int,
                               cap: int) -> bool:
    """Free verified-signup grant (2026-09-30): the first `cap` verified users
    each get `n` messages, once. Returns True when granted, False when this
    user already received it or the cap is reached. The single-statement
    INSERT ... WHERE keeps the cap exact under concurrent verifications."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO free_grants (user_id)
           SELECT ? WHERE (SELECT COUNT(*) FROM free_grants) < ?""",
        (user_id, cap),
    )
    if cur.rowcount == 0:
        conn.commit()
        return False
    conn.execute(
        """UPDATE users SET messages_remaining = messages_remaining + ?,
                              updated_at = datetime('now')
           WHERE user_id = ?""",
        (n, user_id),
    )
    conn.commit()
    return True


def free_spots_left(conn: sqlite3.Connection, cap: int) -> int:
    """How many of the first-`cap` free verified-signup grants remain."""
    row = conn.execute("SELECT COUNT(*) AS c FROM free_grants").fetchone()
    return max(0, cap - (row["c"] if row else 0))


def downgrade_to_basic(conn: sqlite3.Connection, user_id: str) -> None:
    """Subscription ended: tier back to the default; remaining banks drain
    naturally instead of being zeroed."""
    conn.execute(
        "UPDATE users SET tier = 'basic', updated_at = datetime('now') WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()

# ---------------------------------------------------------------------------
# JVZoo fulfillment (2026-09-30). Identity is the buyer's email: JVZoo
# checkouts happen on jvzoo.com, so there is no client_reference_id to carry
# the app user_id. TIERS ONLY — no Keyhole show grants on this path (user).
# ---------------------------------------------------------------------------
def note_jvzoo_event(conn: sqlite3.Connection, paykey: str,
                     event_type: str = "") -> bool:
    """Record a JVZIPN paykey. Returns True when first seen, False on replay
    (idempotency: JVZoo may redeliver notifications)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO jvzoo_events (paykey, event_type) VALUES (?, ?)",
        (paykey, event_type),
    )
    conn.commit()
    return cur.rowcount == 1


def upsert_jvzoo_subscription(conn: sqlite3.Connection, paykey: str,
                              email: str, user_id: str = "",
                              tier: str = "", status: str = "trial") -> None:
    """Record or update a JVZoo subscription row keyed by JVZoo's paykey."""
    conn.execute(
        """INSERT INTO jvzoo_subscriptions
               (paykey, email, user_id, tier, status, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(paykey) DO UPDATE SET
               email = excluded.email,
               user_id = CASE WHEN excluded.user_id != ''
                              THEN excluded.user_id
                              ELSE jvzoo_subscriptions.user_id END,
               tier = CASE WHEN excluded.tier != ''
                           THEN excluded.tier
                           ELSE jvzoo_subscriptions.tier END,
               status = excluded.status,
               updated_at = datetime('now')""",
        (paykey, email, user_id, tier, status),
    )
    conn.commit()


def end_jvzoo_subscriptions_for_email(conn: sqlite3.Connection,
                                      email: str) -> None:
    """Mark every trial/active JVZoo subscription for this email ended
    (refund or rebill cancellation)."""
    conn.execute(
        """UPDATE jvzoo_subscriptions
           SET status = 'ended', updated_at = datetime('now')
           WHERE email = ? AND status IN ('trial', 'active')""",
        (email,),
    )
    conn.commit()


def apply_jvzoo_pending(conn: sqlite3.Connection, user_id: str,
                        email: str) -> list[dict[str, Any]]:
    """At email verification: attach any ACTIVE JVZoo subscription bought
    under this email before the account existed, and upgrade to the tier.
    Refunded/cancelled subscriptions are never applied."""
    rows = conn.execute(
        """SELECT paykey, tier FROM jvzoo_subscriptions
           WHERE email = ? AND status = 'active'""",
        (email,),
    ).fetchall()
    applied: list[dict[str, Any]] = []
    for r in rows:
        tier = r["tier"]
        if tier in config.TIERS:
            set_tier(conn, user_id, tier)
            conn.execute(
                """UPDATE jvzoo_subscriptions
                   SET user_id = ?, updated_at = datetime('now')
                   WHERE paykey = ?""",
                (user_id, r["paykey"]),
            )
            applied.append({"paykey": r["paykey"], "tier": tier})
    conn.commit()
    return applied


# ---------------------------------------------------------------------------
# Email auth (2026-09-30): login required, no anonymous use.
# ---------------------------------------------------------------------------
def create_user_with_email(conn: sqlite3.Connection, user_id: str, email: str,
                           password_hash: str, name: str | None = None) -> dict[str, Any]:
    """Create a new account row for an email signup (unverified). Raises
    sqlite3.IntegrityError when the email is already taken.

    New accounts start with EMPTY banks: 0 messages, 0 voice notes. Nothing
    is free — the $0.99 trial grants 100 messages via grant_trial_messages()
    and a subscription fills the paid bank via set_tier()."""
    conn.execute(
        """INSERT INTO users (user_id, name, email, password_hash, tier,
                              messages_remaining, voice_notes_remaining,
                              proactive_enabled)
           VALUES (?, ?, ?, ?, 'basic', 0, 0, 0)""",
        (user_id, name, email, password_hash),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone())


def has_paid(conn: sqlite3.Connection, user_id: str) -> bool:
    """True when this user has ever paid anything: a Stripe subscription
    (current or past), the one-time $0.99 trial, or a JVZoo subscription.
    Used to pick the right empty-bank upsell — a brand-new unpaid account
    should hear about the $0.99 trial / $9.99 plan, not the $19.99 Plus
    upgrade."""
    row = conn.execute(
        "SELECT 1 FROM stripe_customers WHERE user_id = ? LIMIT 1",
        (user_id,),
    ).fetchone()
    if row:
        return True
    row = conn.execute(
        "SELECT 1 FROM trial_grants WHERE user_id = ? LIMIT 1",
        (user_id,),
    ).fetchone()
    if row:
        return True
    row = conn.execute(
        "SELECT 1 FROM jvzoo_subscriptions WHERE user_id = ? "
        "AND status = 'active' LIMIT 1",
        (user_id,),
    ).fetchone()
    return row is not None


def get_user_by_email(conn: sqlite3.Connection, email: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def set_email_verified(conn: sqlite3.Connection, user_id: str) -> None:
    conn.execute(
        "UPDATE users SET email_verified_at = datetime('now'), "
        "updated_at = datetime('now') WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()


def set_user_name(conn: sqlite3.Connection, user_id: str, name: str) -> None:
    conn.execute(
        "UPDATE users SET name = ?, updated_at = datetime('now') WHERE user_id = ?",
        (name, user_id),
    )
    conn.commit()


def create_session(conn: sqlite3.Connection, user_id: str, token: str) -> None:
    conn.execute(
        "INSERT INTO auth_sessions (token, user_id) VALUES (?, ?)",
        (token, user_id),
    )
    conn.commit()


def get_session_user_id(conn: sqlite3.Connection, token: str) -> str | None:
    row = conn.execute(
        "SELECT user_id FROM auth_sessions WHERE token = ?", (token,)
    ).fetchone()
    return row["user_id"] if row else None


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
    conn.commit()


def create_email_verification(conn: sqlite3.Connection, user_id: str,
                              token: str, ttl_hours: int = 24) -> None:
    conn.execute("DELETE FROM email_verifications WHERE user_id = ?", (user_id,))
    conn.execute(
        """INSERT INTO email_verifications (token, user_id, expires_at)
           VALUES (?, ?, datetime('now', ?))""",
        (token, user_id, f"+{ttl_hours} hours"),
    )
    conn.commit()


def consume_email_verification(conn: sqlite3.Connection, token: str) -> str | None:
    """Single-use: returns the user_id when the token is valid and unexpired,
    else None. Consumes the token on success."""
    row = conn.execute(
        """SELECT user_id FROM email_verifications
           WHERE token = ? AND expires_at > datetime('now')""",
        (token,),
    ).fetchone()
    if not row:
        return None
    user_id = row["user_id"]
    conn.execute("DELETE FROM email_verifications WHERE token = ?", (token,))
    conn.commit()
    return user_id


# ---------------------------------------------------------------------------
# Memory — typed per Thoughtspace pattern
# ---------------------------------------------------------------------------
def write_memory(conn: sqlite3.Connection, user_id: str, mem_type: str, text: str,
                 character_id: str = "chloe") -> int:
    if mem_type not in MEM_TYPES:
        raise ValueError(f"mem_type must be one of {MEM_TYPES}")
    get_or_create_user(conn, user_id)  # ensure row exists (memory belongs to a user)
    cur = conn.execute(
        "INSERT INTO memories (user_id, character_id, mem_type, text) VALUES (?, ?, ?, ?)",
        (user_id, character_id, mem_type, text),
    )
    conn.commit()
    return cur.lastrowid


def recall_memories(
    conn: sqlite3.Connection,
    user_id: str,
    mem_type: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
    character_id: str = "chloe",
) -> list[dict[str, Any]]:
    """Recall memories for ONE user AND one character. Never returns another
    user's rows — or another character's."""
    sql = "SELECT id, mem_type, text, created_at FROM memories WHERE user_id = ? AND character_id = ?"
    params: list[Any] = [user_id, character_id]
    if mem_type:
        sql += " AND mem_type = ?"
        params.append(mem_type)
    if keyword:
        sql += " AND text LIKE ?"
        params.append(f"%{keyword}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def export_memories(conn: sqlite3.Connection, user_id: str,
                    character_id: str = "chloe") -> list[dict[str, Any]]:
    return recall_memories(conn, user_id, limit=100000, character_id=character_id)


def delete_all_memories(conn: sqlite3.Connection, user_id: str,
                        character_id: str = "chloe") -> int:
    cur = conn.execute(
        "DELETE FROM memories WHERE user_id = ? AND character_id = ?",
        (user_id, character_id),
    )
    conn.execute(
        "DELETE FROM conversation WHERE user_id = ? AND character_id = ?",
        (user_id, character_id),
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Conversation log (per user, for recall of "last conversation")
# ---------------------------------------------------------------------------
def log_turn(conn: sqlite3.Connection, user_id: str, role: str, text: str,
             character_id: str = "chloe") -> None:
    conn.execute(
        "INSERT INTO conversation (user_id, character_id, role, text) VALUES (?, ?, ?, ?)",
        (user_id, character_id, role, text),
    )
    conn.commit()


def recent_turns(conn: sqlite3.Connection, user_id: str, limit: int = 12,
                 character_id: str = "chloe") -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT role, text FROM conversation WHERE user_id = ? AND character_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, character_id, limit),
        ).fetchall()
    ][::-1]


def conversation_history(conn: sqlite3.Connection, user_id: str, limit: int = 50,
                         character_id: str = "chloe") -> list[dict[str, Any]]:
    """Full saved conversation for the history view: oldest first, with
    timestamps. Backs GET /api/v1/chat/history — this is what keeps the chat
    from resetting on every login."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT role, text, created_at FROM conversation "
            "WHERE user_id = ? AND character_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, character_id, limit),
        ).fetchall()
    ][::-1]
