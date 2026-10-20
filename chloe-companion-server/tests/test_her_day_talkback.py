"""Improvement #4 tests (2026-10-19): Her Day posts talk back.

Two halves:
- MOOD IN THE POST: Her Day's daily moment pick and caption are
  shaped by the girl's Living World day (app/world.py) — a shared
  world event becomes the group post with that partner, a world
  activity matching a solo moment becomes that moment, and the
  caption's story input carries her world mood + what she's doing
  in that part of her day. With no world hint, the plan is the
  legacy seeded pick, byte-for-byte (pinned below).
- TAP-THROUGH CONTINUES THE MOMENT: POST /api/v1/her-day/open-chat
  parks a one-shot seed for (user, girl) — resolvable from his own
  rows, from the ONE global feed row (store.HER_DAY_GLOBAL_USER),
  and from public moments by slug; never from another user's
  personal row. Her next chat reply is generated with the moment in
  context (brain renders the continue-the-moment block), and the
  seed fires exactly once. Feed reads carry her world mood.

Run:  python -m pytest tests/test_her_day_talkback.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException  # noqa: E402

from app import brain, characters, config, db, her_day, store, world  # noqa: E402
from app import main as api  # noqa: E402

GIRLS = [{"id": g, "name": g.title(), "avatar_url": ""} for g in
         ("chloe", "mara", "isabella", "regan", "brittany", "brooklyn",
          "rina", "nova")]


def _db():
    conn = db.connect(":memory:")
    conn.executescript(db.SCHEMA)
    return conn


class MomentHintTest(unittest.TestCase):
    def test_shared_event_maps_to_group_moment(self):
        # 2026-10-27: Chloe + Mara share a "coffee" world event.
        hint = world.moment_hint("chloe", "2026-10-27")
        self.assertEqual(hint.get("group_moment"), "coffee_date")
        self.assertEqual(hint.get("group_partner"), "mara")
        hint_m = world.moment_hint("mara", "2026-10-27")
        self.assertEqual(hint_m.get("group_moment"), "coffee_date")
        self.assertEqual(hint_m.get("group_partner"), "chloe")

    def test_solo_activity_maps_to_solo_moment(self):
        # Rina's world signature block is a gym session.
        hint = world.moment_hint("rina", "2026-10-21")
        self.assertEqual(hint.get("solo_moment"), "gym")

    def test_unmappable_event_gives_no_group_hint(self):
        # 2026-10-23: Chloe's event is a gallery opening — no group
        # moment maps to it, so no group hint; her day still maps a
        # solo moment (coffee in bed / Luna Café).
        hint = world.moment_hint("chloe", "2026-10-23")
        self.assertNotIn("group_moment", hint)
        self.assertEqual(hint.get("solo_moment"), "morning_coffee")

    def test_hint_is_deterministic(self):
        self.assertEqual(world.moment_hint("chloe", "2026-10-27"),
                         world.moment_hint("chloe", "2026-10-27"))


class PlanDayWorldTest(unittest.TestCase):
    # Pinned from pristine main (herday-v3 seeded picks): with no
    # world hint the plan must not move by a single byte.
    LEGACY = {
        ("chloe", "2026-10-20"): [("out_and_about", False, [], [])],
        ("chloe", "2026-10-21"): [("night_out", True,
                                   ["brooklyn", "brittany"],
                                   ["Brooklyn", "Brittany"])],
        ("chloe", "2026-11-02"): [("out_and_about", False, [], [])],
        ("rina", "2026-10-20"): [("night_out", True, ["mara"], ["Mara"])],
        ("rina", "2026-10-21"): [("morning_coffee", False, [], [])],
        ("rina", "2026-11-02"): [("morning_coffee", False, [], [])],
    }

    def test_no_hint_is_legacy_pick(self):
        for (gid, d), want in self.LEGACY.items():
            self.assertEqual(her_day._plan_day(gid, d, GIRLS), want)
            self.assertEqual(her_day._plan_day(gid, d, GIRLS, hint={}),
                             want)
            self.assertEqual(her_day._plan_day(gid, d, GIRLS, hint=None),
                             want)

    def test_group_hint_becomes_the_post(self):
        hint = world.moment_hint("chloe", "2026-10-27")
        plan = her_day._plan_day("chloe", "2026-10-27", GIRLS, hint=hint)
        self.assertEqual(plan, [("coffee_date", True, ["mara"], ["Mara"])])

    def test_group_hint_is_symmetric_in_full_plans(self):
        hints = her_day._world_hints(
            "2026-10-27", [g["id"] for g in GIRLS])
        plans = her_day._build_all_plans(GIRLS, "2026-10-27", hints=hints)
        self.assertEqual(plans["chloe"],
                         [("coffee_date", True, ["mara"], ["Mara"])])
        self.assertEqual(plans["mara"],
                         [("coffee_date", True, ["chloe"], ["Chloe"])])

    def test_solo_hint_becomes_the_post(self):
        # Chloe's 2026-10-23 legacy pick is out_and_about; her world
        # day is coffee in bed + Luna Café -> morning_coffee.
        self.assertEqual(her_day._plan_day("chloe", "2026-10-23", GIRLS),
                         [("out_and_about", False, [], [])])
        hint = world.moment_hint("chloe", "2026-10-23")
        self.assertEqual(
            her_day._plan_day("chloe", "2026-10-23", GIRLS, hint=hint),
            [("morning_coffee", False, [], [])])

    def test_hint_with_absent_partner_falls_back(self):
        # Mara not on the roster -> the group hint can't fire; the
        # pick falls back to the seeded path (never crashes, never
        # invents a partner).
        roster = [g for g in GIRLS if g["id"] != "mara"]
        hint = {"group_moment": "coffee_date", "group_partner": "mara"}
        plan = her_day._plan_day("chloe", "2026-10-27", roster, hint=hint)
        self.assertEqual(len(plan), 1)
        self.assertNotEqual(plan[0][0], "coffee_date")


class MomentContextTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def tearDown(self):
        self.conn.close()

    def test_context_carries_mood_and_block(self):
        ctx = world.moment_context(self.conn, "chloe", "2026-10-20",
                                   block_idx=1)
        self.assertEqual(ctx["mood"], "playful")
        self.assertEqual(ctx["activity"], "a farmers market wander")
        self.assertEqual(ctx["place"], "the farmers market")
        self.assertIn("playful", ctx["line"])

    def test_event_overrides_its_block(self):
        # Regan + Brittany share a gallery event on 2026-10-20; the
        # context for the event's own block is the event itself.
        ev = next(e for e in world.events_for("2026-10-20")
                  if "regan" in (e["girl_a"], e["girl_b"]))
        ctx = world.moment_context(self.conn, "regan", "2026-10-20",
                                   block_idx=ev["block_idx"])
        self.assertEqual(ctx["place"], "the gallery district")
        self.assertEqual(ctx["activity"], ev["activity"])
        self.assertIn("gallery", ctx["event_text"])

    def test_context_fail_open(self):
        self.assertEqual(world.moment_context(None, "chloe",
                                              "2026-10-20"), {})


class CaptionWorldTest(unittest.TestCase):
    """The generated post itself: world-shaped moment choice, and a
    caption whose story input carries her world mood + activity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.conn = db.connect(self.db_path)
        self.conn.executescript(db.SCHEMA)
        self.conn.commit()
        self._old_connect = her_day.db.connect
        real_connect = db.connect
        her_day.db.connect = lambda *a, **k: real_connect(self.db_path)
        self._old_gen = config.GENERATED_DIR
        config.GENERATED_DIR = self.tmp.name

    def tearDown(self):
        her_day.db.connect = self._old_connect
        config.GENERATED_DIR = self._old_gen
        self.conn.close()
        self.tmp.cleanup()

    def test_caption_story_carries_world_mood_and_moment(self):
        captured = {}

        def fake_caption(name, moment_key, desc, pnames, cache_key,
                         story=""):
            captured.update(moment_key=moment_key, story=story)
            return "caption from her actual day"

        with mock.patch.object(her_day, "_get_all_girls",
                               return_value=GIRLS), \
                mock.patch.object(her_day, "_generate_caption",
                                  side_effect=fake_caption), \
                mock.patch.object(her_day, "_generate_sfw_image",
                                  return_value=""):
            posts = her_day.generate_for_user_girl(
                "user1", "chloe", "Chloe", date_str="2026-10-27")
        self.assertEqual(len(posts), 1)
        # Her world event that day (coffee with Mara) IS the post.
        self.assertEqual(captured["moment_key"], "coffee_date")
        # Chloe's world mood on 2026-10-27 is "focused"; the caption
        # writer received it, plus what she's doing that day.
        self.assertIn("focused", captured["story"])
        self.assertIn("In her world today", captured["story"])
        self.assertEqual(posts[0]["caption"],
                         "caption from her actual day")


class SeedStoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def tearDown(self):
        self.conn.close()

    def test_save_pop_is_one_shot(self):
        store.save_her_day_chat_seed(self.conn, "u1", "chloe", "seed one")
        self.assertEqual(
            store.pop_her_day_chat_seed(self.conn, "u1", "chloe"),
            "seed one")
        self.assertIsNone(
            store.pop_her_day_chat_seed(self.conn, "u1", "chloe"))

    def test_newer_tap_supersedes_older(self):
        store.save_her_day_chat_seed(self.conn, "u1", "chloe", "old tap")
        store.save_her_day_chat_seed(self.conn, "u1", "chloe", "new tap")
        self.assertEqual(
            store.pop_her_day_chat_seed(self.conn, "u1", "chloe"),
            "new tap")
        self.assertIsNone(
            store.pop_her_day_chat_seed(self.conn, "u1", "chloe"))

    def test_seed_scoped_to_user_and_girl(self):
        store.save_her_day_chat_seed(self.conn, "u1", "chloe", "for u1")
        self.assertIsNone(
            store.pop_her_day_chat_seed(self.conn, "u2", "chloe"))
        self.assertIsNone(
            store.pop_her_day_chat_seed(self.conn, "u1", "mara"))
        self.assertEqual(
            store.pop_her_day_chat_seed(self.conn, "u1", "chloe"),
            "for u1")

    def test_stale_seed_never_fires(self):
        store.save_her_day_chat_seed(self.conn, "u1", "chloe", "stale")
        self.conn.execute(
            "UPDATE her_day_chat_seeds SET created_at = "
            "datetime('now', '-2 days')")
        self.conn.commit()
        self.assertIsNone(
            store.pop_her_day_chat_seed(self.conn, "u1", "chloe"))


class _Client:
    host = "127.0.0.1"


class _Req:
    def __init__(self, token):
        self.headers = {"authorization": f"Bearer {token}"}
        self.client = _Client()


class TapThroughTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_cache = characters._cache
        characters._cache = {"ts": time.time(), "chars": [], "local": []}
        for uid in ("u1", "u2"):
            self._mk_user(uid)

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        characters._cache = self._real_cache
        self.tmp.cleanup()

    def _mk_user(self, uid):
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, uid)
            conn.execute(
                "UPDATE users SET messages_remaining = 50 "
                "WHERE user_id = ?", (uid,))
            store.create_session(conn, uid, f"tok_{uid}")
            conn.commit()
        finally:
            conn.close()

    def _post(self, user_id, caption, girl_id="chloe"):
        conn = db.connect(self.db_path)
        try:
            return store.save_her_day_post(
                conn, user_id, girl_id, post_type="image",
                media_url="/generated/herday_inject_chloe_x.jpg",
                caption=caption)
        finally:
            conn.close()

    def _seed(self, user_id, girl_id="chloe"):
        conn = db.connect(self.db_path)
        try:
            return store.pop_her_day_chat_seed(conn, user_id, girl_id)
        finally:
            conn.close()

    def test_open_chat_from_global_post(self):
        post = self._post(store.HER_DAY_GLOBAL_USER,
                          "market morning, flowers everywhere")
        out = api.her_day_open_chat(
            api.HerDayOpenChatIn(post_id=post["id"]), _Req("tok_u1"))
        self.assertEqual(out, {"ok": True, "girl_id": "chloe"})
        seed = self._seed("u1")
        self.assertIsNotNone(seed)
        self.assertIn("market morning, flowers everywhere", seed)
        # The seed carries her Living World state for the post's day:
        # her mood that day is in it.
        mood = world.plan_for("chloe", post["created_at"][:10])["mood"]
        self.assertIn(mood, seed)

    def test_open_chat_from_personal_post(self):
        post = self._post("u1", "my own balcony coffee")
        out = api.her_day_open_chat(
            api.HerDayOpenChatIn(post_id=post["id"]), _Req("tok_u1"))
        self.assertTrue(out["ok"])
        self.assertIn("my own balcony coffee", self._seed("u1"))

    def test_open_chat_from_global_post_by_slug(self):
        post = self._post(store.HER_DAY_GLOBAL_USER,
                          "golden hour on the boardwalk")
        slug = store.post_slug("chloe", post["created_at"],
                               post["caption"])
        out = api.her_day_open_chat(
            api.HerDayOpenChatIn(slug=slug), _Req("tok_u2"))
        self.assertTrue(out["ok"])
        self.assertIn("golden hour on the boardwalk", self._seed("u2"))

    def test_other_users_personal_post_is_not_found(self):
        post = self._post("u1", "u1 only moment")
        with self.assertRaises(HTTPException) as ctx:
            api.her_day_open_chat(
                api.HerDayOpenChatIn(post_id=post["id"]), _Req("tok_u2"))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIsNone(self._seed("u2"))

    def test_unknown_post_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            api.her_day_open_chat(
                api.HerDayOpenChatIn(post_id=99999), _Req("tok_u1"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_chat_continues_the_moment_once(self):
        post = self._post(store.HER_DAY_GLOBAL_USER,
                          "market morning, flowers everywhere")
        api.her_day_open_chat(
            api.HerDayOpenChatIn(post_id=post["id"]), _Req("tok_u1"))
        seen = {}

        def fake_gen(user_text, context, character_id="chloe"):
            seen["context"] = dict(context)
            seen["system"] = brain._gemini_system_text(
                context, character_id)
            return ("still right here between the flower stalls — "
                    "come find me")

        body = api.ChatIn(
            message='Tell me more about your post — '
                    '"market morning, flowers everywhere"',
            character_id="chloe")
        with mock.patch.object(brain, "gemini_generate", fake_gen):
            resp = api.chat(body, _Req("tok_u1"))
        # Her reply came back, and the model was seeded with the
        # tapped moment: caption + world mood in context, and the
        # continue-the-moment instruction in the system text.
        self.assertTrue(resp["reply_text"].startswith(
            "still right here between the flower stalls"))
        moment = seen["context"].get("her_day_moment") or ""
        self.assertIn("market morning, flowers everywhere", moment)
        self.assertIn("continue that exact moment", seen["system"])
        self.assertIn("market morning, flowers everywhere",
                      seen["system"])
        # One-shot: the next message is a normal chat again.
        seen.clear()
        with mock.patch.object(brain, "gemini_generate", fake_gen):
            api.chat(api.ChatIn(message="what else is around?",
                                character_id="chloe"), _Req("tok_u1"))
        self.assertNotIn("her_day_moment", seen["context"])

    def test_feed_posts_carry_world_mood(self):
        post = self._post(store.HER_DAY_GLOBAL_USER,
                          "sunset over the marina")
        out = api.get_public_her_day()
        mine = [p for p in out["posts"]
                if p["caption"] == "sunset over the marina"]
        self.assertEqual(len(mine), 1)
        mood = world.plan_for("chloe", post["created_at"][:10])["mood"]
        self.assertEqual(mine[0]["world"]["mood"], mood)
        self.assertTrue(mine[0]["world"]["activity"])


if __name__ == "__main__":
    unittest.main()
