"""Per-user store: memory (typed, Thoughtspace pattern) + message banks.

Isolation: user_id is a required parameter on every public function and is
always bound into the WHERE clause. Cross-user leakage is impossible by
construction as long as callers use this module (tested).
"""
from __future__ import annotations

import random
import sqlite3
import time
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
    # Custom media requests (2026-10-01): refill the included request pool
    # to the new tier's allocation, same no-stacking rule as the banks.
    refill_request_pool(conn, user_id)
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
    # Free verified-signup grant also includes 2 PRESET give requests
    # (2026-10-01, user decision): kind=give only — never regular
    # generation; $0 cost. Only when the grant itself succeeded.
    credit_free_give(conn, user_id, config.FREE_SIGNUP_GIVE_VIDEOS)
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


# ---------------------------------------------------------------------------
# Admin panel (2026-09-30): account listing + companion-local characters.
# ---------------------------------------------------------------------------

def list_accounts(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    """Newest-first user rows for the admin Accounts tab, each with a
    last-active timestamp (latest conversation turn, if any)."""
    rows = conn.execute(
        "SELECT user_id, name, email, email_verified_at, tier, "
        "messages_remaining, voice_notes_remaining, messages_used_total, "
        "created_at FROM users ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        last = conn.execute(
            "SELECT MAX(created_at) AS m FROM conversation WHERE user_id = ?",
            (d["user_id"],),
        ).fetchone()
        d["last_active_at"] = last["m"] if last else None
        d["verified"] = bool(d.get("email_verified_at"))
        out.append(d)
    return out


def account_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    verified = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE email_verified_at IS NOT NULL"
    ).fetchone()["n"]
    tiers: dict[str, int] = {}
    for r in conn.execute(
        "SELECT tier, COUNT(*) AS n FROM users GROUP BY tier"
    ).fetchall():
        tiers[r["tier"] or "basic"] = r["n"]
    paid = conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM stripe_customers"
    ).fetchone()["n"] if _table_exists(conn, "stripe_customers") else 0
    jvzoo_paid = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(user_id, email)) AS n "
        "FROM jvzoo_subscriptions WHERE status = 'active'"
    ).fetchone()["n"] if _table_exists(conn, "jvzoo_subscriptions") else 0
    return {
        "total": total,
        "verified": verified,
        "by_tier": tiers,
        "stripe_paid_users": paid,
        "jvzoo_active_subs": jvzoo_paid,
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def list_local_characters(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, avatar_url, persona, companion_voice, blurb, "
            "door_title, sort_order, enabled, verified_fictional, created_at, "
            "updated_at "
            "FROM local_characters ORDER BY sort_order, name"
        ).fetchall()
    ]


def upsert_local_character(conn: sqlite3.Connection, char: dict[str, Any]) -> dict[str, Any]:
    """Insert or replace a companion-local character. `id` must already be
    a clean slug; raises ValueError on the reserved 'chloe' id."""
    cid = (char.get("id") or "").strip().lower()
    if not cid or cid == "chloe":
        raise ValueError("character id must be a non-empty slug other than 'chloe'")
    conn.execute(
        "INSERT INTO local_characters "
        "(id, name, avatar_url, persona, companion_voice, blurb, door_title, "
        " sort_order, enabled, verified_fictional, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(id) DO UPDATE SET "
        "name=excluded.name, avatar_url=excluded.avatar_url, "
        "persona=excluded.persona, companion_voice=excluded.companion_voice, "
        "blurb=excluded.blurb, door_title=excluded.door_title, "
        "sort_order=excluded.sort_order, enabled=excluded.enabled, "
        "verified_fictional=excluded.verified_fictional, "
        "updated_at=datetime('now')",
        (
            cid,
            (char.get("name") or cid).strip(),
            (char.get("avatar_url") or "").strip(),
            (char.get("persona") or "").strip(),
            (char.get("companion_voice") or "liora").strip() or "liora",
            (char.get("blurb") or "").strip(),
            (char.get("door_title") or "").strip(),
            int(char.get("sort_order") or 0),
            1 if char.get("enabled", True) else 0,
            1 if char.get("verified_fictional") else 0,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM local_characters WHERE id = ?", (cid,)
    ).fetchone()
    return dict(row)


def delete_local_character(conn: sqlite3.Connection, char_id: str) -> bool:
    gid = (char_id or "").strip().lower()
    # Manual cascade: girl_footage no longer has an FK to local_characters
    # (girl_id may be the reserved builtin 'chloe').
    conn.execute("DELETE FROM girl_footage WHERE girl_id = ?", (gid,))
    cur = conn.execute("DELETE FROM local_characters WHERE id = ?", (gid,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Per-girl footage labels (2026-09-30). The admin panel's Girls tab lets the
# user attach video clips to a companion-local girl and label each one
# typing-main | main | give | activity. The companion app plays typing-main /
# main / activity; 'give' exists so all of her footage lives in one place
# but is Keyhole-only — the public footage endpoint never serves it.
# ---------------------------------------------------------------------------
FOOTAGE_LABELS = ("typing-main", "main", "give", "activity")
# Labels the companion app is allowed to play.
COMPANION_FOOTAGE_LABELS = ("typing-main", "main", "activity")


def list_girl_footage(conn: sqlite3.Connection,
                      girl_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, girl_id, label, url, sort_order, created_at "
            "FROM girl_footage WHERE girl_id = ? "
            "ORDER BY sort_order, id",
            ((girl_id or "").strip().lower(),),
        ).fetchall()
    ]


def add_girl_footage(conn: sqlite3.Connection, girl_id: str, label: str,
                     url: str, sort_order: int = 0) -> dict[str, Any]:
    """Attach a labeled clip to a companion-local girl. Raises ValueError on
    a bad label, a blank URL, or an unknown / non-local girl id."""
    gid = (girl_id or "").strip().lower()
    label = (label or "").strip().lower()
    url = (url or "").strip()
    if label not in FOOTAGE_LABELS:
        raise ValueError(f"label must be one of {', '.join(FOOTAGE_LABELS)}")
    if not url:
        raise ValueError("url is required")
    exists = conn.execute(
        "SELECT 1 FROM local_characters WHERE id = ?", (gid,)
    ).fetchone()
    if not exists and gid != "chloe":
        raise ValueError("unknown girl id")
    cur = conn.execute(
        "INSERT INTO girl_footage (girl_id, label, url, sort_order) "
        "VALUES (?, ?, ?, ?)",
        (gid, label, url, int(sort_order or 0)),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, girl_id, label, url, sort_order, created_at "
        "FROM girl_footage WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return dict(row)


def delete_girl_footage(conn: sqlite3.Connection, footage_id: int) -> bool:
    cur = conn.execute("DELETE FROM girl_footage WHERE id = ?", (footage_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Custom media requests ("request-a-pic", 2026-10-01).
#
# Two kinds:
#   regular — the user's prompt goes to Sogni for on-demand generation
#             (async; ~$0.03/pic, ~$0.12/video). Prompt is moderated.
#   give    — a pre-approved clip from the girl's give-labeled footage,
#             served instantly ($0 marginal). Never in any free rotation.
#
# Balance model: each user has a request_pools row. included_remaining
# refills to the tier allocation monthly (no stacking); overage_* balances
# grow only via the $5.99 overage pack (credit_overage_pack) or the free
# verified-signup grant's 2 preset give sends (credit_free_give).
# ---------------------------------------------------------------------------
REQUEST_KINDS = ("regular", "give")
REQUEST_MEDIA = ("pic", "video")
REQUEST_STATUSES = ("pending", "ready", "failed", "rejected")


def _pool_period() -> str:
    """YYYY-MM of the included pool (UTC)."""
    return time.strftime("%Y-%m", time.gmtime())


def get_request_pool(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Fetch the user's request pool, auto-creating it and rolling the
    included pool when the month turns over. Overage balances persist."""
    user = get_or_create_user(conn, user_id)  # ensure row exists
    tier = user.get("tier") or "basic"
    period = _pool_period()
    row = conn.execute(
        "SELECT * FROM request_pools WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO request_pools (user_id, period, included_remaining) "
            "VALUES (?, ?, ?)",
            (user_id, period, config.REQUEST_POOLS.get(tier, 5)),
        )
        conn.commit()
    elif row["period"] != period:
        conn.execute(
            "UPDATE request_pools SET period = ?, included_remaining = ?, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (period, config.REQUEST_POOLS.get(tier, 5), user_id),
        )
        conn.commit()
    row = conn.execute(
        "SELECT * FROM request_pools WHERE user_id = ?", (user_id,)
    ).fetchone()
    return dict(row)


def refill_request_pool(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Top the included pool back UP TO the tier allocation (no stacking).
    Called from set_tier and therefore from every purchase/renewal path."""
    user = get_or_create_user(conn, user_id)  # ensure row exists
    tier = user.get("tier") or "basic"
    period = _pool_period()
    conn.execute(
        """INSERT INTO request_pools (user_id, period, included_remaining,
                                      updated_at)
           VALUES (?, ?, ?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
               period = excluded.period,
               included_remaining = excluded.included_remaining,
               updated_at = datetime('now')""",
        (user_id, period, config.REQUEST_POOLS.get(tier, 5)),
    )
    conn.commit()
    return dict(
        conn.execute(
            "SELECT * FROM request_pools WHERE user_id = ?", (user_id,)
        ).fetchone()
    )


def consume_request(conn: sqlite3.Connection, user_id: str,
                    kind: str) -> dict[str, Any] | None:
    """Consume one request from the pool: included_remaining first, then the
    matching overage bucket (regular -> overage_regular_videos,
    give -> overage_give_videos). Returns the pool dict with a "source" key
    (included | overage_regular | overage_give), or None when empty."""
    pool = get_request_pool(conn, user_id)  # rolls the period
    if pool["included_remaining"] > 0:
        conn.execute(
            "UPDATE request_pools SET included_remaining = included_remaining - 1, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        pool["included_remaining"] -= 1
        pool["source"] = "included"
        return pool
    col = ("overage_regular_videos" if kind == "regular"
           else "overage_give_videos")
    if pool[col] > 0:
        conn.execute(
            f"UPDATE request_pools SET {col} = {col} - 1, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        pool[col] -= 1
        pool["source"] = ("overage_regular" if kind == "regular"
                          else "overage_give")
        return pool
    return None


def refund_request(conn: sqlite3.Connection, user_id: str,
                   pool_source: str) -> None:
    """Return one consumed request to the bucket it came from (used when a
    regular generation fails or a request is rejected after consuming)."""
    col = {
        "included": "included_remaining",
        "overage_regular": "overage_regular_videos",
        "overage_give": "overage_give_videos",
    }.get(pool_source or "included", "included_remaining")
    get_request_pool(conn, user_id)  # ensure row exists
    conn.execute(
        f"UPDATE request_pools SET {col} = {col} + 1, "
        "updated_at = datetime('now') WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()


def credit_overage_pack(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Fulfill the $5.99 overage pack (paid through Shopify — fulfillment
    polls new orders and calls this): +300 messages, +14 preset video
    sends (give clips only — no custom generation in the pack)."""
    get_or_create_user(conn, user_id)  # ensure row exists
    pack = config.OVERAGE_PACK
    conn.execute(
        """UPDATE users SET messages_remaining = messages_remaining + ?,
                              updated_at = datetime('now') WHERE user_id = ?""",
        (pack["messages"], user_id),
    )
    pool = get_request_pool(conn, user_id)
    conn.execute(
        """UPDATE request_pools
           SET overage_regular_videos = overage_regular_videos + ?,
               overage_give_videos = overage_give_videos + ?,
               updated_at = datetime('now')
           WHERE user_id = ?""",
        (pack["regular_videos"], pack["give_videos"], user_id),
    )
    conn.commit()
    return get_request_pool(conn, user_id)


def credit_free_give(conn: sqlite3.Connection, user_id: str, n: int) -> None:
    """Free verified-signup grant add-on: n PRESET give sends (kind=give
    only — never regular generation; $0 marginal cost)."""
    get_request_pool(conn, user_id)  # ensure row exists
    conn.execute(
        "UPDATE request_pools SET overage_give_videos = overage_give_videos + ?, "
        "updated_at = datetime('now') WHERE user_id = ?",
        (int(n or 0), user_id),
    )
    conn.commit()


def requests_used_today(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM media_requests "
        "WHERE user_id = ? AND date(created_at) = date('now')",
        (user_id,),
    ).fetchone()
    return row["c"] if row else 0


# ---------------------------------------------------------------------------
# media_requests rows
# ---------------------------------------------------------------------------
def create_media_request(conn: sqlite3.Connection, user_id: str, girl_id: str,
                         kind: str, media: str, prompt: str = "",
                         pool_source: str = "included") -> dict[str, Any]:
    if kind not in REQUEST_KINDS:
        raise ValueError(f"kind must be one of {REQUEST_KINDS}")
    if media not in REQUEST_MEDIA:
        raise ValueError(f"media must be one of {REQUEST_MEDIA}")
    cur = conn.execute(
        """INSERT INTO media_requests
               (user_id, girl_id, kind, media, prompt, status, pool_source)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (user_id, (girl_id or "chloe").strip().lower(),
         kind, media, prompt or "", pool_source),
    )
    conn.commit()
    return dict(
        conn.execute(
            "SELECT * FROM media_requests WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    )


def get_media_request(conn: sqlite3.Connection, user_id: str,
                      req_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM media_requests WHERE id = ? AND user_id = ?",
        (req_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def set_media_request_status(conn: sqlite3.Connection, req_id: int,
                             status: str, result_url: str = "",
                             cost_usd: float = 0.0) -> None:
    if status not in REQUEST_STATUSES:
        raise ValueError(f"status must be one of {REQUEST_STATUSES}")
    conn.execute(
        "UPDATE media_requests SET status = ?, result_url = ?, cost_usd = ? "
        "WHERE id = ?",
        (status, result_url or "", float(cost_usd or 0.0), req_id),
    )
    conn.commit()


def recent_media_requests(conn: sqlite3.Connection,
                          limit: int = 100) -> list[dict[str, Any]]:
    """Admin review: newest requests with prompts, for moderation oversight."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, user_id, girl_id, kind, media, prompt, status, "
            "result_url, cost_usd, created_at FROM media_requests "
            "ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit or 100), 500)),),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# Prompt moderation (regular requests only). Server-side blocklist; every
# prompt is logged (the row itself) for admin review. Rejected prompts get
# an in-character deflection from the endpoint — never an error dump.
# ---------------------------------------------------------------------------
# Hard blocklist: minors / non-consent / extreme — rejected for EVERYONE,
# always. No flag or tier opens this lane.
_PROMPT_HARD_BLOCK = (
    "child", "kid", "teen", "minor", "underage", "baby",
    "forced", "non-consent", "nonconsent", "rape", "drugged",
    "unconscious", "sleeping",  # sexualized unconsciousness
    "incest", "bestiality", "animal",
    "gore", "blood", "torture", "mutilat",
)

# Explicit lane (2026-10-01): opened ONLY for custom-submission characters
# verified to not be a real person (verified_fictional=1). Everyone else
# stays in the GIVE lane (pre-approved clips only).
_PROMPT_EXPLICIT = (
    "nude", "naked", "topless", "explicit", "porn", "sex ",
    " sex", "blowjob", "handjob", "orgasm", "masturbat",
    "nipple", "pussy", "dick", "cock", "tits",
)


def prompt_has_explicit(prompt: str) -> bool:
    """True when the prompt asks for the explicit lane."""
    low = (prompt or "").lower()
    return any(bad in low for bad in _PROMPT_EXPLICIT)


def moderate_prompt(prompt: str, allow_explicit: bool = False) -> str | None:
    """Return a rejection reason when the prompt is blocked, else None.

    allow_explicit opens the explicit lane — only for custom-submission
    characters verified to not be a real person. The hard blocklist
    (minors / non-consent / extreme) is never opened.
    """
    p = (prompt or "").strip()
    if not p:
        return "empty prompt — tell her what you'd like to see"
    if len(p) > 500:
        return "prompt too long (500 chars max)"
    low = p.lower()
    for bad in _PROMPT_HARD_BLOCK:
        if bad in low:
            return "that one's off the table, babe"
    if not allow_explicit:
        for bad in _PROMPT_EXPLICIT:
            if bad in low:
                return "explicit_blocked"
    return None


# ---------------------------------------------------------------------------
# Give library: pre-approved clips only. Rotation never repeats a clip to
# the same user until every clip has been sent (then it reshuffles), and
# never sends the same clip twice in a row.
# ---------------------------------------------------------------------------
def girl_has_give(conn: sqlite3.Connection, girl_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM girl_footage WHERE girl_id = ? AND label = 'give' LIMIT 1",
        ((girl_id or "").strip().lower(),),
    ).fetchone()
    return bool(row)


def pick_give_clip(conn: sqlite3.Connection, user_id: str,
                   girl_id: str) -> dict[str, Any] | None:
    """Pick a give clip for this user: prefer clips never sent to them,
    never the most recent one. Returns {id, url} or None."""
    gid = (girl_id or "").strip().lower()
    clips = [
        dict(r)
        for r in conn.execute(
            "SELECT id, url FROM girl_footage WHERE girl_id = ? AND label = 'give' "
            "ORDER BY sort_order, id",
            (gid,),
        ).fetchall()
    ]
    if not clips:
        return None
    sent = [
        dict(r)
        for r in conn.execute(
            "SELECT footage_id FROM user_give_history "
            "WHERE user_id = ? AND girl_id = ? ORDER BY id",
            (user_id, gid),
        ).fetchall()
    ]
    sent_ids = [r["footage_id"] for r in sent]
    last_id = sent_ids[-1] if sent_ids else None
    fresh = [c for c in clips
             if c["id"] not in sent_ids and c["id"] != last_id]
    pool = fresh or [c for c in clips if c["id"] != last_id] or clips
    return random.choice(pool)


def record_give_send(conn: sqlite3.Connection, user_id: str, girl_id: str,
                     footage_id: int) -> None:
    conn.execute(
        "INSERT INTO user_give_history (user_id, girl_id, footage_id) "
        "VALUES (?, ?, ?)",
        (user_id, (girl_id or "").strip().lower(), int(footage_id)),
    )
    conn.commit()
