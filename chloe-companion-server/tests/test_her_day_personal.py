"""Her Day personal day-set tests (2026-10-09).

User-created characters (enabled companion-local characters with an
owner_user_id) get a daily Her Day set stored under their OWNER's
user_id — visible only in that owner's feed, never under the shared
sentinel, never on the public landing feed. Moments and captions are
driven by the character's persona (profile palettes), generation is
capped per owner and per run, and probe/test owners are skipped.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import config, db, her_day, store  # noqa: E402

GIRLS = [
    {"id": g, "name": g.title(), "avatar_url": ""}
    for g in ("chloe", "mara", "isabella", "regan", "jake", "eli")
]
SHARED = store.HER_DAY_SHARED_USER

CEO_PERSONA = (
    "Adrian 'Marcus' Vale, 38. A venture-backed CEO running a logistics "
    "company. Boardrooms, investor meetings, tailored suits and early "
    "espressos. Dry humor, always three steps ahead."
)
FARM_PERSONA = (
    "June Callahan, 29. Runs her family's farm — horses, hay fields, "
    "dawn chores and big country skies. Flannel, strong coffee, "
    "stronger opinions."
)
CHEF_PERSONA = (
    "Bibi Laurent, 31. A pastry chef with a tiny bakery, up before "
    "dawn, flour on her sleeves, kind to everyone."
)


class PersonalHerDayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_get_girls = her_day._get_all_girls
        her_day._get_all_girls = lambda: [dict(g) for g in GIRLS]
        self._real_image = her_day._generate_sfw_image
        self.scene_by_girl: dict[str, list[str]] = {}
        self._seen_imgs = set()

        def fake_image(names, avatar_url, participant_ids, moment_key,
                       scene, date_str):
            key = ("_".join(sorted(participant_ids)), moment_key, date_str)
            if key not in self._seen_imgs:  # mirrors the on-disk cache
                self._seen_imgs.add(key)
                self.scene_by_girl.setdefault(
                    participant_ids[0], []).append(scene)
            return "/generated/fake.jpg"

        her_day._generate_sfw_image = fake_image
        her_day._caption_cache.clear()
        self._env = {k: os.environ.get(k) for k in (
            "HER_DAY_PERSONAL_CHAR_CAP", "HER_DAY_PERSONAL_MAX_SETS_PER_RUN")}
        os.environ.pop("HER_DAY_PERSONAL_CHAR_CAP", None)
        os.environ.pop("HER_DAY_PERSONAL_MAX_SETS_PER_RUN", None)

    def tearDown(self):
        config.DB_PATH = self._real_db_path
        her_day._get_all_girls = self._real_get_girls
        her_day._generate_sfw_image = self._real_image
        her_day._caption_cache.clear()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _mk_users(self, *uids):
        conn = db.connect(self.db_path)
        try:
            for uid in uids:
                conn.execute(
                    "INSERT INTO users (user_id, name, email)"
                    " VALUES (?, ?, ?)",
                    (uid, uid, f"{uid}@x.test"))
            conn.commit()
        finally:
            conn.close()

    def _mk_char(self, cid, name, owner, persona, sort_order=0):
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO local_characters (id, name, avatar_url,"
                " persona, companion_voice, blurb, door_title, sort_order,"
                " enabled, verified_fictional, photos, owner_user_id)"
                " VALUES (?, ?, ?, ?, 'liora', '', '', ?, 1, 0, '[]', ?)",
                (cid, name, f"/avatars/{cid}.jpg", persona, sort_order,
                 owner))
            conn.commit()
        finally:
            conn.close()

    def _seed_people(self):
        self._mk_users("cas", "other-fan", "plain", "probe-7")
        self._mk_char("marcus", "Marcus", "cas", CEO_PERSONA, sort_order=0)
        self._mk_char("june", "June", "cas", FARM_PERSONA, sort_order=1)
        self._mk_char("bibi", "Bibi", "other-fan", CHEF_PERSONA)

    def _rows(self):
        conn = db.connect(self.db_path)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM her_day_posts").fetchall()]
        finally:
            conn.close()

    def _feed(self, uid):
        conn = db.connect(self.db_path)
        try:
            return store.get_her_day_posts(conn, uid)
        finally:
            conn.close()

    def _plan_total(self):
        plans = her_day._build_all_plans(
            GIRLS, datetime.now().strftime("%Y-%m-%d"))
        return sum(len(p) for p in plans.values())

    # -- profile palettes --------------------------------------------------
    def test_palette_selection_differs_by_persona(self):
        ceo = her_day._profile_palette({"persona": CEO_PERSONA, "blurb": ""})
        farm = her_day._profile_palette({"persona": FARM_PERSONA, "blurb": ""})
        self.assertIsNotNone(ceo)
        self.assertIsNotNone(farm)
        self.assertEqual(ceo["key"], "executive")
        self.assertEqual(farm["key"], "farm")

        date = datetime.now().strftime("%Y-%m-%d")
        ceo_plan = her_day._plan_personal_day(
            {"id": "marcus"}, date, ceo)
        farm_plan = her_day._plan_personal_day(
            {"id": "june"}, date, farm)
        # Personal days are always solo — no roster girls get pulled in.
        for plan in (ceo_plan, farm_plan):
            self.assertTrue(all(not is_group
                                for (_k, is_group, _p, _n) in plan))

        ceo_scenes = {her_day._personal_moment_def(ceo, k)[1]
                      for (k, _g, _p, _n) in ceo_plan}
        farm_scenes = {her_day._personal_moment_def(farm, k)[1]
                       for (k, _g, _p, _n) in farm_plan}
        self.assertTrue(any("high-rise" in (s or "") for s in ceo_scenes))
        self.assertTrue(any("porch" in (s or "") for s in farm_scenes))
        self.assertNotEqual(ceo_scenes, farm_scenes)

    def test_unknown_persona_gets_everyday_default(self):
        palette = her_day._profile_palette(
            {"persona": "A quiet person who likes evenings.", "blurb": ""})
        self.assertIsNone(palette)

    # -- owner scoping -----------------------------------------------------
    def test_personal_sets_are_scoped_to_their_owner(self):
        self._seed_people()
        report = her_day.generate_personal_days()
        self.assertEqual(report["errors"], [])
        rows = self._rows()
        self.assertTrue(rows)
        personal = [r for r in rows if r["girl_id"] in ("marcus", "june",
                                                        "bibi")]
        self.assertTrue(personal)
        # Stored under the owner only — never the shared sentinel.
        self.assertEqual({r["user_id"] for r in rows}, {"cas", "other-fan"})
        self.assertNotIn(SHARED, {r["user_id"] for r in rows})

        cas_girls = {p["girl_id"] for p in self._feed("cas")}
        self.assertIn("marcus", cas_girls)
        self.assertIn("june", cas_girls)
        self.assertNotIn("bibi", cas_girls)

        other_girls = {p["girl_id"] for p in self._feed("other-fan")}
        self.assertEqual(other_girls, {"bibi"})

        plain_girls = {p["girl_id"] for p in self._feed("plain")}
        self.assertEqual(plain_girls, set())

    def test_profile_drives_generated_scenes(self):
        self._seed_people()
        her_day.generate_personal_days()
        ceo_scenes = " ".join(self.scene_by_girl.get("marcus", []))
        farm_scenes = " ".join(self.scene_by_girl.get("june", []))
        self.assertIn("high-rise", ceo_scenes)
        self.assertIn("porch", farm_scenes)
        self.assertNotIn("porch", ceo_scenes)
        self.assertNotIn("high-rise", farm_scenes)

    def test_personal_generation_is_idempotent_per_day(self):
        self._seed_people()
        first = her_day.generate_personal_days()
        self.assertGreater(first["generated"], 0)
        second = her_day.generate_personal_days()
        self.assertEqual(second["generated"], 0)
        self.assertEqual(second["sets"], 0)

    # -- interaction with the shared engine --------------------------------
    def test_daily_run_keeps_shared_contract_and_adds_personal(self):
        self._seed_people()
        total = self._plan_total()
        report = her_day.generate_daily()
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["generated"], total)
        self.assertGreater(report["personal_generated"], 0)
        rows = self._rows()
        self.assertEqual(
            len([r for r in rows if r["user_id"] == SHARED]), total)
        cas_girls = {p["girl_id"] for p in self._feed("cas")}
        self.assertIn("marcus", cas_girls)
        self.assertIn("chloe", cas_girls)  # shared set rides along

    def test_public_feed_never_shows_personal_characters(self):
        self._seed_people()
        her_day.generate_personal_days()  # personal only, no shared set
        conn = db.connect(self.db_path)
        try:
            posts = store.get_public_her_day_posts(conn, limit=100)
        finally:
            conn.close()
        self.assertEqual(
            {p["girl_id"] for p in posts} & {"marcus", "june", "bibi"},
            set())

    def test_on_demand_generates_only_callers_characters(self):
        self._seed_people()
        total = self._plan_total()
        n = her_day.generate_for_all_girls("cas")
        self.assertGreater(n, total)  # shared + cas's two personal sets
        girls = {r["girl_id"] for r in self._rows()}
        self.assertIn("marcus", girls)
        self.assertNotIn("bibi", girls)  # not the caller's — untouched
        n2 = her_day.generate_for_all_girls("other-fan")
        self.assertGreater(n2, 0)
        self.assertIn("bibi", {r["girl_id"] for r in self._rows()})

    # -- caps and skips ------------------------------------------------------
    def test_char_cap_limits_sets_per_owner(self):
        self._seed_people()
        os.environ["HER_DAY_PERSONAL_CHAR_CAP"] = "1"
        report = her_day.generate_personal_days()
        girls = {r["girl_id"] for r in self._rows()
                 if r["user_id"] == "cas"}
        self.assertEqual(girls, {"marcus"})  # sort_order 0 wins the cap
        self.assertEqual(report["sets"], 2)  # marcus + bibi

    def test_probe_and_dangling_owners_get_nothing(self):
        self._mk_users("cas", "probe-7")
        self._mk_char("marcus", "Marcus", "cas", CEO_PERSONA)
        self._mk_char("probie", "Probie", "probe-7", FARM_PERSONA)
        self._mk_char("ghosty", "Ghosty", "ghost", FARM_PERSONA)
        report = her_day.generate_personal_days()
        self.assertEqual(report["errors"], [])
        self.assertEqual({r["user_id"] for r in self._rows()}, {"cas"})

    def test_owner_without_characters_gets_no_personal_rows(self):
        self._mk_users("plain")
        report = her_day.generate_personal_days()
        self.assertEqual(report["generated"], 0)
        self.assertEqual(self._rows(), [])

    # -- viewed semantics ----------------------------------------------------
    def test_viewed_marks_personal_post_for_owner_only(self):
        self._seed_people()
        her_day.generate_personal_days()
        conn = db.connect(self.db_path)
        try:
            post = next(p for p in store.get_her_day_posts(conn, "cas")
                        if p["girl_id"] == "marcus")
            self.assertFalse(store.mark_her_day_viewed(
                conn, post["id"], "plain"))
            self.assertTrue(store.mark_her_day_viewed(
                conn, post["id"], "cas"))
            mine = {p["id"]: p for p in store.get_her_day_posts(
                conn, "cas")}
            self.assertEqual(mine[post["id"]]["viewed"], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
