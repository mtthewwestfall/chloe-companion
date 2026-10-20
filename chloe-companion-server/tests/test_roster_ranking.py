"""Roster engagement ranking (improvement #10, 2026-10-20).

The roster surfaces order girls by the engagement the system ALREADY
records — chat turns in `conversation` + media/give asks in
`media_requests` (store.engagement_scores) — so the girls people bond
with stand at the front and the rest fade back. Pinned here:

- scores count HIS turns and his media requests, never her turns;
- synthetic group/event accounts (tg_<n >= 900_000_000_000>, the
  give_stage_cap predicate) never distort the counts;
- the public list is ranked AFTER the privacy filter: a private
  (owner-scoped) girl never surfaces publicly, however engaged;
- a signed-in user's own engagement outranks the global counts;
- Chloe stays pinned first (the flagship roster invariant);
- zero-engagement girls are all still listed, in roster order;
- ties keep the incoming relative order (stable);
- a scoring failure falls back to the plain roster order.

Run:  python -m unittest tests.test_roster_ranking -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import characters, config, db, store  # noqa: E402

# The Keyhole roster, in current (sort_order) order.
FETCHED = [
    {"id": "chloe", "name": "Chloe", "avatar_url": "https://x/chloe.png",
     "persona": "", "companion_voice": "liora", "blurb": "",
     "door_title": "", "sort_order": 0, "gender": "female"},
    {"id": "mara", "name": "Mara", "avatar_url": "https://x/mara.png",
     "persona": "", "companion_voice": "liora", "blurb": "",
     "door_title": "", "sort_order": 10, "gender": "female"},
    {"id": "isabella", "name": "Isabella", "avatar_url": "https://x/isa.png",
     "persona": "", "companion_voice": "liora", "blurb": "",
     "door_title": "", "sort_order": 20, "gender": "female"},
    {"id": "regan", "name": "Regan", "avatar_url": "https://x/regan.png",
     "persona": "", "companion_voice": "liora", "blurb": "",
     "door_title": "", "sort_order": 30, "gender": "female"},
    {"id": "brittany", "name": "Brittany", "avatar_url": "https://x/brit.png",
     "persona": "", "companion_voice": "liora", "blurb": "",
     "door_title": "", "sort_order": 40, "gender": "female"},
    {"id": "nova", "name": "Nova", "avatar_url": "https://x/nova.png",
     "persona": "", "companion_voice": "liora", "blurb": "",
     "door_title": "", "sort_order": 50, "gender": "female"},
]
ROSTER_ORDER = [c["id"] for c in FETCHED]
EVENT_USER = "tg_905469103781"  # synthetic shared event account


class RosterRankingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        characters._cache = {"ts": 0.0, "chars": []}
        characters._scores_cache = {"ts": 0.0, "global": None, "users": {}}
        self._real_fetch = characters._fetch
        characters._fetch = lambda: [dict(c) for c in FETCHED]

    def tearDown(self):
        config.DB_PATH = self._real_db_path
        characters._fetch = self._real_fetch
        characters._cache = {"ts": 0.0, "chars": []}
        characters._scores_cache = {"ts": 0.0, "global": None, "users": {}}
        self.tmp.cleanup()

    # -- seeding helpers ----------------------------------------------------
    def _conn(self):
        return db.connect(self.db_path)

    def _say(self, user, girl, n, role="user"):
        conn = self._conn()
        try:
            for _ in range(n):
                store.log_turn(conn, user, role, "hi", character_id=girl)
        finally:
            conn.close()

    def _ask(self, user, girl, n):
        conn = self._conn()
        try:
            for _ in range(n):
                store.create_media_request(conn, user, girl, "regular", "pic")
        finally:
            conn.close()

    def _bust_scores(self):
        characters._scores_cache = {"ts": 0.0, "global": None, "users": {}}

    def _ids(self, user_id=None):
        return [c["id"] for c in characters.public_list(user_id)]

    # -- the aggregate ------------------------------------------------------
    def test_scores_count_his_turns_and_requests_never_hers(self):
        self._say("u1", "mara", 3)
        self._say("u1", "mara", 5, role="mara")  # her replies: not demand
        self._ask("u1", "mara", 2)
        self._say("u2", "nova", 4)
        conn = self._conn()
        try:
            scores = store.engagement_scores(conn)
        finally:
            conn.close()
        self.assertEqual(scores, {"mara": 5, "nova": 4})

    def test_scores_exclude_synthetic_event_accounts(self):
        self._say(EVENT_USER, "regan", 100)
        self._ask(EVENT_USER, "regan", 50)
        self._say("tg_123456", "regan", 2)  # a real Telegram user counts
        self._say("u1", "mara", 3)
        conn = self._conn()
        try:
            scores = store.engagement_scores(conn)
        finally:
            conn.close()
        self.assertEqual(scores, {"regan": 2, "mara": 3})

    def test_scores_per_user_scope(self):
        self._say("u1", "mara", 3)
        self._say("u2", "nova", 7)
        conn = self._conn()
        try:
            self.assertEqual(store.engagement_scores(conn, "u1"), {"mara": 3})
            self.assertEqual(store.engagement_scores(conn, "u2"), {"nova": 7})
            self.assertEqual(store.engagement_scores(conn, "ghost"), {})
        finally:
            conn.close()

    # -- the ranking --------------------------------------------------------
    def test_global_ranking_orders_by_engagement(self):
        self._say("u1", "brittany", 9)
        self._say("u1", "regan", 5)
        self._say("u2", "nova", 5)  # tie with regan: regan is earlier on the roster
        self._bust_scores()
        self.assertEqual(
            self._ids(),
            ["chloe", "brittany", "regan", "nova", "mara", "isabella"])

    def test_zero_engagement_keeps_roster_order_and_everyone_listed(self):
        self.assertEqual(self._ids(), ROSTER_ORDER)
        self.assertEqual(self._ids("u1"), ROSTER_ORDER)

    def test_chloe_stays_pinned_despite_lower_engagement(self):
        self._say("u1", "brittany", 100)
        self._say("u1", "chloe", 1)
        self._bust_scores()
        self.assertEqual(self._ids()[0], "chloe")
        self.assertEqual(self._ids()[1], "brittany")

    def test_user_boost_outranks_global(self):
        self._say("u2", "brittany", 50)     # the global favorite
        self._say("u1", "isabella", 4)      # u1's own girl
        self._bust_scores()
        self.assertEqual(self._ids("u1")[:3], ["chloe", "isabella", "brittany"])
        self.assertEqual(self._ids()[:2], ["chloe", "brittany"])

    def test_private_girl_never_ranked_into_public_list(self):
        conn = self._conn()
        try:
            store.upsert_local_character(conn, {
                "id": "secretgirl", "name": "Secret",
                "avatar_url": "https://x/secret.png", "persona": "",
                "companion_voice": "liora", "blurb": "", "door_title": "",
                "sort_order": 1, "enabled": True,
                "owner_user_id": "owner1",
            })
        finally:
            conn.close()
        self._say("u9", "secretgirl", 500)  # huge engagement, still private
        self._bust_scores()
        characters._cache = {"ts": 0.0, "chars": []}
        self.assertNotIn("secretgirl", self._ids())
        self.assertNotIn("secretgirl", self._ids("someone-else"))
        owner_ids = self._ids("owner1")
        self.assertIn("secretgirl", owner_ids)
        # For her owner she ranks by engagement like anyone else.
        self.assertEqual(owner_ids[1], "secretgirl")

    def test_scoring_failure_falls_back_to_roster_order(self):
        self._say("u1", "brittany", 9)
        self._bust_scores()
        with mock.patch.object(store, "engagement_scores",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(self._ids(), ROSTER_ORDER)


if __name__ == "__main__":
    unittest.main()
