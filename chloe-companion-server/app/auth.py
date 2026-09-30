"""Email + password authentication for the companion app.

No anonymous use: every /api/v1/* endpoint except the auth endpoints,
/api/v1/tiers, /api/v1/shows, /api/v1/characters, /health and the Stripe
webhook requires a Bearer session token issued here.

Password hashing uses PBKDF2-HMAC-SHA256 from the stdlib (no new
dependencies). Session tokens are opaque, stored server-side in SQLite.
Verification emails go through Resend (same provider as the Keyhole
backend); RESEND_API_KEY and MAIL_FROM come from the environment.

Sessions are deliberately long-lived (no expiry): this is an installed PWA
and users expect to stay logged in. A session ends only on explicit
logout (token deleted server-side) or account deletion.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.request
from collections import defaultdict
from typing import Any

_HASH_ITERS = 200_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _HASH_ITERS)
    return f"pbkdf2_sha256${_HASH_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(iters),
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Tokens / email
# ---------------------------------------------------------------------------
def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


def valid_password(password: str) -> bool:
    return isinstance(password, str) and 8 <= len(password) <= 128


def send_verification_email(to_email: str, name: str, verify_url: str,
                            resend_key: str, mail_from: str) -> None:
    """Send the confirm-your-email message via Resend. Raises RuntimeError
    on any failure so callers can surface a 502 (retryable)."""
    body = json.dumps({
        "from": mail_from,
        "to": [to_email],
        "subject": "Confirm your Chloe account",
        "text": (
            f"Hi {name or 'there'},\n\n"
            "Confirm your email to start talking to Chloe:\n"
            f"{verify_url}\n\n"
            "This link expires in 24 hours. "
            "If you didn't sign up, ignore this message."
        ),
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=body, method="POST",
        headers={
            "Authorization": f"Bearer {resend_key}",
            "Content-Type": "application/json",
            "User-Agent": "chloe-companion/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"resend status {resp.status}")
    except Exception as e:  # noqa: BLE001 — surfaced as 502 by the caller
        raise RuntimeError(f"resend failed: {e}")


# ---------------------------------------------------------------------------
# Minimal in-memory rate limiting for the auth endpoints (per IP).
# ---------------------------------------------------------------------------
_attempts: dict[str, list[float]] = defaultdict(list)


def rate_limited(key: str, max_hits: int = 20, window_s: int = 600) -> bool:
    now = time.monotonic()
    hits = [t for t in _attempts[key] if now - t < window_s]
    _attempts[key] = hits
    if len(hits) >= max_hits:
        return True
    hits.append(now)
    return False


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()
