"""Tests for the 2026-10-18 pool fix: truthful active-lease cap.

Production failure these reproduce: an evening of lead-flow scan
jobs each took a 6h lease and never released it (the consumer has
no release call). Once 8 unexpired 'active' rows existed for
source roof-rover, every new scan's lease request was denied
(429 lease_denied) and the scan paused with ZERO grading attempts
while 11 free keys sat healthy.

The cap must count only leases that can still represent real
concurrency: not idle-dead holders (reaped after
GEMINI_LEASE_IDLE_REAP_S of charge silence), not exhausted leases,
and not the manager's own default accounting leases. Live leases
(fresh or recently charged) still cap exactly as before, and
GEMINI_LEASE_TRUTHFUL_CAP=0 restores the Stage-2 accounting.

Same style as tests/test_gemini_keys_stage2.py: temp DB per test.
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config, gemini_keys


class PoolFixBase(unittest.TestCase):
    ENV_NAMES = ("GEMINI_LEASES", "GEMINI_LEASE_MAX_CALLS",
                 "GEMINI_LEASE_MAX_ACTIVE_PER_SOURCE",
                 "GEMINI_LEASE_DEFAULT_CALLS",
                 "GEMINI_LEASE_TRUTHFUL_CAP",
                 "GEMINI_LEASE_IDLE_REAP_S")

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db",
                                               delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self._orig_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._env = {}
        for name in self.ENV_NAMES:
            self._env[name] = os.environ.pop(name, None)
        os.environ["GEMINI_LEASE_MAX_ACTIVE_PER_SOURCE"] = "3"

    def tearDown(self):
        config.DB_PATH = self._orig_db_path
        for name, val in self._env.items():
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # -- helpers ------------------------------------------------------
    def _issue(self, job, calls=150, source="roof-rover"):
        return gemini_keys.request_lease(source, job, calls,
                                         ttl_s=21600.0)

    def _age(self, lease_id, idle_s, used=None):
        """Backdate a lease's activity by idle_s seconds (and
        optionally set its spend), as if its scan charged then and
        never came back."""
        past = time.time() - idle_s
        created = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.gmtime(past))
        conn = gemini_keys.db.connect()
        try:
            gemini_keys._ensure_table(conn)
            sets = ["created_at = ?"]
            params = [created]
            try:
                conn.execute(
                    "UPDATE gemini_leases SET last_charge_at = ? "
                    "WHERE lease_id = ?", (past, lease_id))
            except sqlite3.OperationalError:
                pass  # pristine schema: no activity column yet
            if used is not None:
                sets.append("budget_used = ?")
                params.append(used)
            conn.execute(
                "UPDATE gemini_leases SET " + ", ".join(sets) +
                " WHERE lease_id = ?", (*params, lease_id))
            conn.commit()
        finally:
            conn.close()


class TestTruthfulLeaseCap(PoolFixBase):
    def test_idle_dead_holders_do_not_block_new_scan(self):
        ids = []
        for i in range(3):
            lease = self._issue(f"scan-{i}")
            self.assertIsNotNone(lease)
            ids.append(lease["lease_id"])
            # Partially spent, then silent for an hour: the scan
            # finished/paused/died without releasing.
            gemini_keys._lease_charge(lease["lease_id"])
            self._age(lease["lease_id"], 3600)
        lease = self._issue("scan-new")
        self.assertIsNotNone(lease)
        for lid in ids:
            self.assertEqual(
                gemini_keys.lease_status(lid)["status"], "expired")

    def test_live_concurrency_still_capped(self):
        for i in range(3):
            lease = self._issue(f"live-{i}")
            self.assertIsNotNone(lease)
            gemini_keys._lease_charge(lease["lease_id"])
        self.assertIsNone(self._issue("live-overflow"))

    def test_fresh_uncharged_leases_still_count(self):
        for i in range(3):
            self.assertIsNotNone(self._issue(f"fresh-{i}"))
        self.assertIsNone(self._issue("fresh-overflow"))

    def test_exhausted_leases_do_not_count(self):
        for i in range(3):
            lease = self._issue(f"spent-{i}", calls=1)
            self.assertIsNotNone(lease)
            self.assertTrue(
                gemini_keys._lease_charge(lease["lease_id"]))
        self.assertIsNotNone(self._issue("after-spent"))

    def test_default_lease_does_not_eat_a_slot(self):
        self.assertIsNotNone(
            gemini_keys._default_source_lease("roof-rover"))
        for i in range(3):
            self.assertIsNotNone(self._issue(f"real-{i}"))
        self.assertIsNone(self._issue("real-overflow"))

    def test_recently_charged_lease_survives_reap(self):
        keeper = self._issue("keeper")
        self.assertIsNotNone(keeper)
        self.assertTrue(
            gemini_keys._lease_charge(keeper["lease_id"]))
        for i in range(2):
            dead = self._issue(f"dead-{i}")
            self.assertIsNotNone(dead)
            self._age(dead["lease_id"], 3600)
        self.assertIsNotNone(self._issue("newcomer"))
        again = self._issue("keeper")
        self.assertEqual(again["lease_id"], keeper["lease_id"])
        self.assertEqual(again["budget_used"], 1)

    def test_flag_off_restores_old_accounting(self):
        os.environ["GEMINI_LEASE_TRUTHFUL_CAP"] = "0"
        for i in range(3):
            lease = self._issue(f"old-{i}")
            self.assertIsNotNone(lease)
            self._age(lease["lease_id"], 3600)
        self.assertIsNone(self._issue("old-overflow"))


if __name__ == "__main__":
    unittest.main()
