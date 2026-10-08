"""JVZoo IPN fulfillment tests (2026-09-30).

Covers the JVZoo affiliate channel: JVZIPN v2 signature verification,
$0-trial SALE recording (no tier change), BILL fulfillment of the paid
tier, idempotent replays, RFND downgrade, and pending subscriptions
applied at email verification.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import config, db, store  # noqa: E402
from app import main as api  # noqa: E402


def _sign(form: dict, secret: str) -> str:
    parts = [form["paykey"], form["customer_email"], form["product_name"],
             form["transaction_type"], form["date"], secret]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8].upper()


class JvzooIpnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self.secret = "testsecret123"
        self._real_secret = config.JVZIPN_SECRET
        config.JVZIPN_SECRET = self.secret

    def tearDown(self):
        api._conn = self._real_conn
        config.JVZIPN_SECRET = self._real_secret
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _form(self, **kw) -> dict:
        f = {"paykey": "PK1",
             "customer_email": "buyer@example.com",
             "product_name": "Chloe AI Companion",
             "transaction_type": "SALE",
             "date": "2026-09-30 00:00:00",
             "total": "0.00"}
        f.update(kw)
        f["cverify"] = _sign(f, self.secret)
        return f

    def _make_user(self, email: str, uid: str = "u_jv1") -> dict:
        conn = db.connect(self.db_path)
        try:
            return store.create_user_with_email(conn, uid, email, "hash")
        finally:
            conn.close()

    def _user(self, uid: str) -> dict:
        conn = db.connect(self.db_path)
        try:
            return store.get_or_create_user(conn, uid)
        finally:
            conn.close()

    # -- signature -------------------------------------------------------
    def test_verify_ok(self):
        self.assertTrue(api._jvzoo_verify_ok(self._form(), self.secret))

    def test_verify_rejects_tampered_type(self):
        f = self._form()
        f["transaction_type"] = "BILL"  # tampered after signing
        self.assertFalse(api._jvzoo_verify_ok(f, self.secret))

    def test_verify_rejects_wrong_secret(self):
        self.assertFalse(api._jvzoo_verify_ok(self._form(), "wrongsecret"))

    def test_verify_rejects_missing_cverify(self):
        f = self._form()
        del f["cverify"]
        self.assertFalse(api._jvzoo_verify_ok(f, self.secret))

    # -- event flow ------------------------------------------------------
    def test_trial_sale_records_only_no_tier_change(self):
        self._make_user("buyer@example.com")
        res = api._handle_jvzoo_ipn(self._form())  # SALE, total 0.00
        self.assertEqual(res["action"], "trial_recorded")
        u = self._user("u_jv1")
        self.assertEqual(u["tier"], "basic")
        self.assertEqual(u["messages_remaining"], 0)

    def test_bill_fulfills_paid_tier(self):
        self._make_user("buyer@example.com")
        res = api._handle_jvzoo_ipn(
            self._form(transaction_type="BILL", total="9.99", paykey="PK2"))
        self.assertEqual(res["action"], "rebill_topup")
        self.assertEqual(res["tier"], "basic")
        u = self._user("u_jv1")
        self.assertEqual(u["tier"], "basic")
        self.assertEqual(u["messages_remaining"], 3000)
        self.assertEqual(u["voice_notes_remaining"], 150)

    def test_duplicate_paykey_ignored(self):
        self._make_user("buyer@example.com")
        f = self._form(transaction_type="BILL", total="9.99", paykey="PK3")
        first = api._handle_jvzoo_ipn(f)
        self.assertEqual(first["action"], "rebill_topup")
        second = api._handle_jvzoo_ipn(dict(f))
        self.assertTrue(second.get("duplicate"))

    def test_refund_downgrades_to_basic(self):
        self._make_user("buyer@example.com")
        api._handle_jvzoo_ipn(
            self._form(transaction_type="BILL", total="9.99", paykey="PK4"))
        self.assertEqual(self._user("u_jv1")["tier"], "basic")
        res = api._handle_jvzoo_ipn(
            self._form(transaction_type="RFND", total="9.99", paykey="PK5"))
        self.assertEqual(res["action"], "downgraded_to_basic")
        self.assertEqual(self._user("u_jv1")["tier"], "basic")

    def test_unknown_total_ignored(self):
        self._make_user("buyer@example.com")
        res = api._handle_jvzoo_ipn(
            self._form(transaction_type="BILL", total="4.99", paykey="PK6"))
        self.assertEqual(res["ignored"], "unknown total for tier")

    def test_pending_applied_when_account_verifies_later(self):
        # Buyer pays on JVZoo before creating an app account.
        res = api._handle_jvzoo_ipn(
            self._form(transaction_type="BILL", total="9.99", paykey="PK7",
                       customer_email="late@example.com"))
        self.assertEqual(res["action"], "pending_verification")
        # ...then signs up and verifies with the same email.
        self._make_user("late@example.com", uid="u_jv2")
        conn = db.connect(self.db_path)
        try:
            applied = store.apply_jvzoo_pending(conn, "u_jv2",
                                                "late@example.com")
        finally:
            conn.close()
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["tier"], "basic")
        u = self._user("u_jv2")
        self.assertEqual(u["tier"], "basic")
        self.assertEqual(u["messages_remaining"], 1000)

    def test_refunded_pending_never_applies(self):
        api._handle_jvzoo_ipn(
            self._form(transaction_type="BILL", total="9.99", paykey="PK8",
                       customer_email="ghost@example.com"))
        api._handle_jvzoo_ipn(
            self._form(transaction_type="RFND", total="9.99", paykey="PK9",
                       customer_email="ghost@example.com"))
        self._make_user("ghost@example.com", uid="u_jv3")
        conn = db.connect(self.db_path)
        try:
            applied = store.apply_jvzoo_pending(conn, "u_jv3",
                                                "ghost@example.com")
        finally:
            conn.close()
        self.assertEqual(applied, [])
        self.assertEqual(self._user("u_jv3")["tier"], "basic")

    def test_has_paid_counts_jvzoo(self):
        self._make_user("buyer@example.com")
        conn = db.connect(self.db_path)
        try:
            self.assertFalse(store.has_paid(conn, "u_jv1"))
        finally:
            conn.close()
        api._handle_jvzoo_ipn(
            self._form(transaction_type="BILL", total="9.99", paykey="PK10"))
        conn = db.connect(self.db_path)
        try:
            self.assertTrue(store.has_paid(conn, "u_jv1"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
