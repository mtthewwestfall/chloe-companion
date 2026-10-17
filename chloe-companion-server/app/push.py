"""Web Push (VAPID) for the Whispr PWA.

The browser subscribes via PushManager with our VAPID public key and POSTs
the subscription to /api/v1/push/subscribe (authenticated). Subscriptions
are stored in push_subscriptions.json next to the DB — on production the
DB lives on the Railway volume, so subscriptions survive redeploys.

WebPushTransport plugs into scheduler.run(): when the proactive scheduler
is not in dry-run mode, good-morning / missed-you / show-hype messages are
delivered as push notifications instead of the delivery stub.

VAPID keys: set VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY (and optionally
VAPID_SUBJECT) as env vars. If unset, a P-256 keypair is generated once on
first boot and persisted to vapid_keys.json (0600) next to the DB. Set
explicit env vars in production so keys survive volume changes.
NEVER commit real keys.
"""
from __future__ import annotations

import base64
import json
import os
import threading

from . import config

_SUBS_FILE = os.path.join(config.DATA_DIR, "push_subscriptions.json")
_KEYS_FILE = os.path.join(config.DATA_DIR, "vapid_keys.json")
_lock = threading.Lock()


class SubscriptionGone(Exception):
    """The push service reports the subscription is expired/invalid."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _generate_keypair() -> tuple[str, str]:
    """Generate a P-256 VAPID keypair -> (private_b64url, public_b64url)."""
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    priv_raw = key.private_numbers().private_value.to_bytes(32, "big")
    # Uncompressed point: 0x04 || X || Y (65 bytes).
    pub_raw = key.public_key().public_numbers().encode_point()
    return _b64url_encode(priv_raw), _b64url_encode(pub_raw)


def vapid_keys() -> tuple[str, str]:
    """(private_key, public_key) as base64url strings.

    Precedence: env vars -> persisted vapid_keys.json -> generate once and
    persist. Thread-safe; the file is written with 0600 permissions.
    """
    priv = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    pub = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
    if priv and pub:
        return priv, pub
    with _lock:
        if os.path.exists(_KEYS_FILE):
            try:
                with open(_KEYS_FILE) as f:
                    data = json.load(f)
                if data.get("private") and data.get("public"):
                    return data["private"], data["public"]
            except (ValueError, OSError):
                pass
        priv, pub = _generate_keypair()
        tmp = _KEYS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"private": priv, "public": pub}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _KEYS_FILE)
        return priv, pub


def vapid_public_key() -> str:
    """Public key the browser needs for PushManager.subscribe()."""
    return vapid_keys()[1]


def vapid_subject() -> str:
    return os.environ.get("VAPID_SUBJECT", "mailto:hello@lockeddoor.ai")


# ---------------------------------------------------------------------------
# Subscription storage: {user_id: [subscription, ...]} as JSON.
# ---------------------------------------------------------------------------

def _load_subs() -> dict:
    try:
        with open(_SUBS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_subs(data: dict) -> None:
    tmp = _SUBS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, _SUBS_FILE)


def save_subscription(user_id: str, subscription: dict) -> None:
    """Store (or refresh) a user's push subscription. Idempotent per endpoint."""
    endpoint = (subscription or {}).get("endpoint", "")
    if not user_id or not endpoint:
        raise ValueError("subscription needs an endpoint")
    with _lock:
        data = _load_subs()
        subs = [s for s in data.get(user_id, []) if s.get("endpoint") != endpoint]
        subs.append(subscription)
        data[user_id] = subs
        _save_subs(data)


def remove_subscription(user_id: str, endpoint: str) -> bool:
    """Remove one subscription. Returns True if anything was removed."""
    with _lock:
        data = _load_subs()
        subs = data.get(user_id, [])
        kept = [s for s in subs if s.get("endpoint") != endpoint]
        if len(kept) == len(subs):
            return False
        if kept:
            data[user_id] = kept
        else:
            data.pop(user_id, None)
        _save_subs(data)
        return True


def get_subscriptions(user_id: str) -> list:
    return list(_load_subs().get(user_id, []))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_push(subscription: dict, title: str, body: str, url: str = "/") -> None:
    """Send one push notification. Raises SubscriptionGone when the push
    service reports the subscription expired (caller should prune it)."""
    # Lazy import: the scheduler and API stay importable when pywebpush
    # isn't installed (e.g. local dev without the new dependency).
    from pywebpush import WebPushException, webpush

    private_key, _public_key = vapid_keys()
    payload = json.dumps({"title": title, "body": body, "url": url})
    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=private_key,
            vapid_claims={"sub": vapid_subject()},
            timeout=10,
        )
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            raise SubscriptionGone(subscription.get("endpoint", "")) from e
        raise


class WebPushTransport:
    """DeliveryTransport for scheduler.run(): proactive queue -> Web Push.

    Each queued message is pushed to every stored subscription of its
    user. Expired subscriptions (404/410) are pruned automatically.
    """

    def __init__(self, title: str = "Whispr AI", url: str = "/") -> None:
        self.title = title
        self.url = url

    def deliver(self, queue: list[dict]) -> list[dict]:
        report = []
        for m in queue:
            user_id = m.get("user_id", "")
            text = m.get("text", "")
            sent, pruned = 0, 0
            for sub in get_subscriptions(user_id):
                try:
                    send_push(sub, self.title, text, self.url)
                    sent += 1
                except SubscriptionGone:
                    remove_subscription(user_id, sub.get("endpoint", ""))
                    pruned += 1
                except Exception:
                    continue  # transient push-service error; try next time
            report.append({
                "to": user_id,
                "type": m.get("type"),
                "status": "push_sent" if sent else "no_subscription",
                "sent": sent,
                "pruned": pruned,
            })
        return report
