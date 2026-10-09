"""Direct Sogni media routing + lane locks (2026-10-09, owner orders).

Pinned here, from live screenshots:
- "Send me a video of you taking off your top" (Hannah, session)
  queues an adult plan render — it once fell to a brain refusal.
- "You can through sogni" right after a media ask stays in the
  lane, even with a studio flow open upstream.
- "Make me that video" routes with zero Sogni mention.
- Promo asks render clothed; adult asks render as asked.
- Mia's media desk renders ANY roster character by name; Hannah /
  Jordan / Rocky make only themselves.
- Jordan is OneFetch only (her own name is never the Jordan Young
  Roof signal in her thread); Hannah Roof only; Mia Whispr only.
- Studio lives in Mia's thread (Rocky keeps his); the create flow
  re-asks junk fragments instead of assembling them, stale flows
  never present a cold summary, and boot clears in-flight flows.

All Sogni/Gemini seams are mocked — zero real renders or sign-ins.

Run:  python -m unittest tests.test_sogni_direct_media -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, characters, db, store  # noqa: E402

OWNER = {"user_id": "owner_1", "tier": "basic"}
TOKENS = {"token": "tok-aaa", "refreshToken": "ref-bbb"}
MSG1 = "Send me a video of you taking off your top"
MSG2 = "You can through sogni"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    assist.ensure_tables(conn)
    return conn


def _fake_roster(conn):
    def fake(user_id=None, include_private=False, refresh=False):
        out = []
        for r in conn.execute("SELECT * FROM local_characters").fetchall():
            d = dict(r)
            out.append({"id": d["id"], "name": d["name"],
                        "avatar_url": d["avatar_url"],
                        "persona": d["persona"],
                        "companion_voice": d["companion_voice"],
                        "blurb": d["blurb"], "door_title": d["door_title"],
                        "sort_order": d["sort_order"],
                        "owner_user_id": d["owner_user_id"]})
        if not include_private:
            out = [c for c in out
                   if not c["owner_user_id"]
                   or c["owner_user_id"] == (user_id or "")]
        return out
    return fake


class DirectBase(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS",
                         "HANNAH_SOGNI_PLAN_DAILY_CAP")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        os.environ.pop("HANNAH_SOGNI_PLAN_DAILY_CAP", None)
        self.conn = _conn()
        for cid, name in (("hannah", "Hannah"), ("mia", "Mia"),
                          ("jordan", "Jordan"), ("rocky", "Rocky"),
                          ("mara", "Mara")):
            store.upsert_local_character(
                self.conn, {"id": cid, "name": name,
                            "owner_user_id": "owner_1",
                            "avatar_url": f"/generated/{cid}.jpg"})
        self.spawned: list[int] = []
        self._patches = [
            mock.patch.object(characters, "get_characters",
                              _fake_roster(self.conn)),
            mock.patch.object(assist, "_spawn_sogni_plan_job",
                              lambda job_id: self.spawned.append(job_id)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def say(self, text, user=OWNER, cid="hannah", attachments=None):
        return assist.maybe_assist(self.conn, user, text, cid,
                                   attachments=attachments)

    def jobs(self):
        return self.conn.execute(
            "SELECT * FROM sogni_plan_jobs ORDER BY id").fetchall()

    def finish_jobs(self):
        self.conn.execute("UPDATE sogni_plan_jobs SET status='done'")
        self.conn.commit()


class ExactScreenshotTest(DirectBase):
    def test_exact_messages_with_session_queue_adult_render(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say(MSG1, cid="hannah")
        self.assertIsNotNone(res)
        self.assertIn("rendering", res["brief"])
        self.assertEqual(res.get("sogni_media_intent"), "adult")
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["kind"], "video")
        self.assertEqual(jobs[0]["character_id"], "hannah")
        self.assertEqual(jobs[0]["target_character_id"], "hannah")
        self.assertEqual(jobs[0]["billing_mode"], "subscription")
        self.assertIn("taking off your top", jobs[0]["prompt"])
        # The pushback naming Sogni stays in the lane.
        res2 = self.say(MSG2, cid="hannah")
        self.assertIsNotNone(res2)
        self.assertTrue(
            res2["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        self.assertIn("signed into your sogni", res2["brief"])

    def test_exact_messages_without_session_get_login_box(self):
        res = self.say(MSG1, cid="hannah")
        self.assertIsNotNone(res)
        self.assertTrue(res.get("sogni_login_box"))
        self.assertIn("sogni login", res["brief"])
        self.assertEqual(self.jobs(), [])
        res2 = self.say(MSG2, cid="hannah")
        self.assertIsNotNone(res2)
        self.assertTrue(res2.get("sogni_login_box"))
        self.assertEqual(self.jobs(), [])

    def test_messages_survive_an_open_studio_flow(self):
        # The diagnosed swallow: a guided-creation flow open in the
        # same user state must not eat media asks as flow answers.
        assist.store_sogni_session(self.conn, TOKENS)
        self.say("create a character", cid="mia")
        res = self.say(MSG1, cid="hannah")
        self.assertIsNotNone(res)
        self.assertEqual(len(self.jobs()), 1)
        flow = assist._studio_get(self.conn, "owner_1", "create_flow")
        self.assertEqual(flow.get("step"), "name")
        self.assertFalse(flow.get("name"))
        res2 = self.say(MSG2, cid="hannah")
        self.assertIn("signed into your sogni", res2["brief"])

    def test_make_me_that_video_both_session_states(self):
        res = self.say("Make me that video", cid="hannah")
        self.assertIsNotNone(res)
        self.assertTrue(res.get("sogni_login_box"))
        self.assertEqual(self.jobs(), [])
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("Make me that video", cid="hannah")
        # Vague ask (owner order 2026-10-09 12:33): situations are
        # offered, nothing queues until he picks.
        self.assertIn("pick one", res["brief"])
        self.assertTrue(res.get("sogni_media_options"))
        self.assertEqual(self.jobs(), [])
        res = self.say("the pool one", cid="hannah")
        self.assertIn("rendering", res["brief"])
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["target_character_id"], "hannah")
        self.assertIn("pool", jobs[0]["prompt"])

    def test_vague_ask_you_pick_renders(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make me a video", cid="hannah")
        self.assertIn("pick one", res["brief"])
        self.assertEqual(self.jobs(), [])
        res = self.say("you pick", cid="hannah")
        self.assertIn("rendering", res["brief"])
        self.assertEqual(len(self.jobs()), 1)
        self.assertTrue(self.jobs()[0]["prompt"])

    def test_vague_adult_ask_options_match_temperature(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make me a sexy video", cid="hannah")
        opts = res.get("sogni_media_options") or []
        self.assertTrue(any("lingerie" in o or "slip" in o
                            for o in opts))
        self.assertEqual(self.jobs(), [])
        res = self.say("2", cid="hannah")
        self.assertIn("rendering", res["brief"])
        self.assertEqual(len(self.jobs()), 1)
        self.assertIn("couch", self.jobs()[0]["prompt"])

    def test_specific_ask_renders_immediately_no_menu(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a video of you dancing in the kitchen",
                       cid="hannah")
        self.assertIn("rendering", res["brief"])
        self.assertFalse(res.get("sogni_media_options"))
        self.assertEqual(len(self.jobs()), 1)

    def test_vague_pic_offers_options_then_pick_renders(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("send me a pic", cid="mia")
        # "send me a pic" may fall to the image lane; either lane
        # must offer situations rather than queue blind.
        if res is None or not res.get("sogni_media_options"):
            res = self.say("make me a picture of you", cid="mia")
        self.assertTrue(res.get("sogni_media_options"))
        self.assertEqual(self.jobs(), [])
        res = self.say("the porch one", cid="mia")
        self.assertIn("rendering", res["brief"])
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]["kind"], "image")
        self.assertIn("porch", self.jobs()[0]["prompt"])

    def test_video_call_is_not_a_render(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a video call with me", cid="hannah")
        self.assertTrue(res is None
                        or "rendering" not in res.get("brief", ""))
        self.assertEqual(self.jobs(), [])

    def test_make_it_followup_uses_media_topic(self):
        assist.store_sogni_session(self.conn, TOKENS)
        self.say("make a video of you waving", cid="hannah")
        self.finish_jobs()
        res = self.say("make it", cid="hannah")
        self.assertIsNotNone(res)
        self.assertIn("rendering", res["brief"])
        self.assertEqual(len(self.jobs()), 2)


class IntentSplitTest(DirectBase):
    def test_promo_renders_clothed_adult_renders_as_asked(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a promo video of you waving for the site",
                       cid="hannah")
        self.assertEqual(res.get("sogni_media_intent"), "promo")
        self.assertIn("fully clothed", self.jobs()[0]["prompt"])
        self.finish_jobs()
        res = self.say("make a topless video of you dancing",
                       cid="hannah")
        self.assertEqual(res.get("sogni_media_intent"), "adult")
        job = self.jobs()[1]
        self.assertIn("topless", job["prompt"])
        self.assertNotIn("fully clothed", job["prompt"])


class MiaMediaDeskTest(DirectBase):
    def test_mia_renders_any_character_by_name(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a video of Mara dancing", cid="mia")
        self.assertIn("rendering", res["brief"])
        job = self.jobs()[0]
        self.assertEqual(job["character_id"], "mia")
        self.assertEqual(job["target_character_id"], "mara")
        self.assertIn("dancing", job["prompt"])

    def test_mia_unknown_name_asks_who(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a video of Zelda dancing", cid="mia")
        self.assertIsNotNone(res)
        self.assertIn("who do you mean", res["brief"])
        self.assertEqual(self.jobs(), [])

    def test_hannah_cross_character_goes_to_mia(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a video of Mara dancing", cid="hannah")
        self.assertIsNotNone(res)
        self.assertIn("mia's desk", res["brief"])
        self.assertEqual(self.jobs(), [])

    def test_mia_picture_of_self_uses_sogni_plan(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a picture of you waving", cid="mia")
        self.assertEqual(res.get("sogni_path"), "sogni_plan")
        job = self.jobs()[0]
        self.assertEqual(job["kind"], "image")
        self.assertEqual(job["target_character_id"], "mia")
        self.assertEqual(job["billing_mode"], "subscription")

    def test_picture_without_session_falls_back_and_says_so(self):
        with mock.patch.object(assist, "_image_post", return_value=None):
            res = self.say("make a picture of a red barn", cid="jordan")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("sogni_path"), "gemini_fallback")
        self.assertIn("free image keys", res["brief"])
        self.assertEqual(self.jobs(), [])


class LaneLockTest(DirectBase):
    FAKE = {"onefetch": {"founder_count": 3, "founder_spots_left": 197,
                         "founder_open": True, "ai_available": True,
                         "site_status": 200},
            "roof_rover": {"shelf_leads": 5, "site_status": 200},
            "gemini": {"believed_healthy": 2,
                       "pool": {"healthy": 2}, "degraded": False}}

    def test_lane_map_is_locked(self):
        self.assertEqual(assist.character_lane("hannah"),
                         assist.LANE_ROOF)
        self.assertEqual(assist.character_lane("mia"),
                         assist.LANE_WHISPR)
        self.assertEqual(assist.character_lane("jordan"),
                         assist.LANE_ONEFETCH)
        self.assertIsNone(assist.character_lane("rocky"))

    def test_jordan_self_description_is_onefetch(self):
        brief = assist._owner_brief_reply("jordan")
        self.assertIn("ONEFETCH", brief)
        self.assertIn("Founders", brief)
        self.assertIn("FSBO", brief)
        self.assertIn("listing", brief)
        self.assertIn("Roof Rover is Hannah's", brief)
        self.assertNotIn("YOUR PROJECT — ROOF ROVER", brief)
        res = self.say("what's your project?", cid="jordan")
        self.assertIsNotNone(res)
        self.assertIn("ONEFETCH", res["brief"])
        self.assertIn("$19.99", res["brief"])

    def test_jordan_status_and_cross_lane(self):
        with mock.patch.object(assist, "fetch_app_status",
                               return_value=dict(self.FAKE)):
            res = self.say("how's onefetch doing?", cid="jordan")
            self.assertIn("3 founders", res["brief"])
            res = self.say("how's roof rover doing?", cid="jordan")
            self.assertIn("that's hannah's project", res["brief"])
            res = self.say("how's roof rover doing?", cid="hannah")
            self.assertIn("5 leads on the $4.99 store shelf",
                          res["brief"])

    def test_jordan_name_is_not_jordan_young_in_her_thread(self):
        self.assertEqual(
            assist.text_lane("jordan young referral", "hannah"),
            assist.LANE_ROOF)
        self.assertIsNone(
            assist.text_lane("jordan young referral", "jordan"))
        self.assertIsNone(assist.text_lane("hey jordan", "jordan"))
        # Hannah's lane still carries the Jordan Young facts.
        self.assertIn("Jordan Young", assist._owner_brief_reply("hannah"))

    def test_hannah_and_mia_briefs_unchanged(self):
        self.assertIn("ROOF ROVER", assist._owner_brief_reply("hannah"))
        self.assertIn("$4.99", assist._owner_brief_reply("hannah"))
        self.assertIn("WHISPR", assist._owner_brief_reply("mia"))
        self.assertIn("$9.99", assist._owner_brief_reply("mia"))
        # Mia's project lane stays Whispr; the media desk is an
        # ability on top of it, not a second project.
        self.assertIn("media desk", assist._owner_brief_reply("mia"))


class StudioFlowGuardTest(DirectBase):
    def test_studio_redirect_for_hannah_and_jordan_rocky_keeps_it(self):
        for cid in ("hannah", "jordan"):
            res = self.say("create a character", cid=cid)
            self.assertIn("mia's desk", res["brief"], cid)
        res = self.say("create a character", cid="rocky")
        self.assertIn("NAME", res["brief"])

    def test_junk_fragments_reask_instead_of_assembling(self):
        self.say("create a character", cid="mia")
        res = self.say("For just $9.99 a month,", cid="mia")
        self.assertIn("doesn't read like a character name",
                      res["brief"])
        flow = assist._studio_get(self.conn, "owner_1", "create_flow")
        self.assertEqual(flow.get("step"), "name")
        self.assertFalse(flow.get("name"))
        self.say("Test Tessa", cid="mia")
        self.say("female", cid="mia")
        res = self.say("Make it 15 sec long,", cid="mia")
        self.assertIn("sounded like an instruction", res["brief"])
        flow = assist._studio_get(self.conn, "owner_1", "create_flow")
        self.assertFalse(flow.get("personality"))
        self.say("A warm lifeguard who loves sunset swims.", cid="mia")
        res = self.say("you pick", cid="mia")
        self.assertIn("wasn't a voice name", res["brief"])
        flow = assist._studio_get(self.conn, "owner_1", "create_flow")
        self.assertFalse(flow.get("voice"))
        self.assertFalse(flow.get("voice_done"))

    def test_garbage_confirm_state_cannot_create(self):
        assist._studio_set(self.conn, "owner_1", "create_flow", {
            "name": "For just $9.99 a month,",
            "gender": "female",
            "personality": "Make it 15 sec long,",
            "voice": "pick", "voice_done": True, "avatar_done": True,
            "step": "confirm"})
        res = self.say("yes", cid="mia")
        self.assertNotIn("studio_action", res or {})
        self.assertIn("never got a real name", res["brief"])
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM local_characters "
            "WHERE id LIKE 'for-just%'").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_expired_flow_starts_fresh(self):
        self.say("create a character", cid="mia")
        self.conn.execute(
            "UPDATE hannah_studio SET updated_at = ? "
            "WHERE kind = 'create_flow'",
            (time.time() - 100000,))
        self.conn.commit()
        res = self.say("create a character", cid="mia")
        self.assertIn("NAME", res["brief"])
        flow = assist._studio_get(self.conn, "owner_1", "create_flow")
        self.assertEqual(flow.get("step"), "name")
        self.assertFalse(flow.get("name"))

    def test_boot_clear_drops_flows_keeps_the_rest(self):
        assist._studio_set(self.conn, "owner_1", "create_flow",
                           {"step": "name"})
        assist._studio_set(self.conn, "owner_1", "pending_give",
                           {"girl_id": "mara"})
        assist._studio_set(self.conn, "owner_1", "last_image",
                           {"url": "/generated/x.png"})
        cleared = assist.clear_studio_flow_state(
            self.conn, {"owner_1"})
        self.assertEqual(cleared, 2)
        self.assertIsNone(
            assist._studio_get(self.conn, "owner_1", "create_flow"))
        self.assertIsNone(
            assist._studio_get(self.conn, "owner_1", "pending_give"))
        self.assertIsNotNone(
            assist._studio_get(self.conn, "owner_1", "last_image"))


class PromoSubjectTest(DirectBase):
    """Owner 2026-10-09 12:55/12:57: promo subjects that aren't her
    body are IN-LANE — her project's mascot/brand/scenes render in
    promo mode; another project's mascot goes to that project's
    girl; roster people stay Mia's desk; bare nude asks are adult
    plan images."""

    # -- her own project's mascot/scene renders in promo mode --
    def test_jordan_video_of_fetch_is_promo_job(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a video of Fetch", cid="jordan")
        self.assertIsNotNone(res)
        (job,) = self.jobs()
        self.assertEqual(job["kind"], "video")
        self.assertEqual(job["billing_mode"], "subscription")
        self.assertIsNone(job["target_character_id"])
        self.assertEqual(job["subject_mode"], "promo_scene")
        self.assertEqual(job["promo_ref"], "fetch")
        self.assertIn("Fetch", job["prompt"])
        self.assertIn("golden retriever", job["prompt"])
        self.assertIn("family-friendly", job["prompt"])

    def test_jordan_fetch_correction_with_male_voice(self):
        # The owner's exact screenshot correction, end to end: a
        # self video first, then "use Fetch" with a male voice.
        assist.store_sogni_session(self.conn, TOKENS)
        self.say("make a video of you dancing at a club", cid="jordan")
        (job1,) = self.jobs()
        self.assertEqual(job1["target_character_id"], "jordan")
        self.finish_jobs()
        res = self.say("No don't use you use Fetch the dog and put "
                       "males voice have the app jn the background",
                       cid="jordan")
        self.assertIsNotNone(res)
        jobs = self.jobs()
        self.assertEqual(len(jobs), 2)
        job = jobs[1]
        self.assertEqual(job["subject_mode"], "promo_scene")
        self.assertEqual(job["promo_ref"], "fetch")
        self.assertIsNone(job["target_character_id"])
        self.assertIn("Fetch", job["prompt"])
        self.assertIn("OneFetch app on a phone screen", job["prompt"])
        self.assertIn("background", job["prompt"])
        self.assertEqual(job["promo_voice"],
                         assist._PROMO_VOICE_LINES["fetch"])
        self.assertIn("Fetch promo", res["brief"])
        self.assertIn("silent", res["brief"])

    def test_jordan_fetch_at_open_house(self):
        assist.store_sogni_session(self.conn, TOKENS)
        self.say("make a promo of Fetch at an open house", cid="jordan")
        (job,) = self.jobs()
        self.assertEqual(job["subject_mode"], "promo_scene")
        self.assertEqual(job["promo_ref"], "fetch")
        self.assertIn("open house", job["prompt"])

    def test_hannah_bailey_pointing_at_roof(self):
        assist.store_sogni_session(self.conn, TOKENS)
        self.say("make a video of Bailey pointing at a roof",
                 cid="hannah")
        (job,) = self.jobs()
        self.assertEqual(job["subject_mode"], "promo_scene")
        self.assertEqual(job["promo_ref"], "bailey")
        self.assertIn("Bailey", job["prompt"])
        self.assertIn("pointing at a roof", job["prompt"])

    def test_hannah_store_shelf_promo(self):
        assist.store_sogni_session(self.conn, TOKENS)
        self.say("make a promo of the store shelf", cid="hannah")
        (job,) = self.jobs()
        self.assertEqual(job["subject_mode"], "promo_scene")
        self.assertIsNone(job["promo_ref"])
        self.assertIn("store shelf", job["prompt"])

    def test_jordan_scene_without_a_character_renders(self):
        assist.store_sogni_session(self.conn, TOKENS)
        self.say("make a video of an agent handing over keys",
                 cid="jordan")
        (job,) = self.jobs()
        self.assertEqual(job["subject_mode"], "promo_scene")

    # -- another project's mascot/brand goes to that project's girl --
    def test_jordan_bailey_ask_redirects_to_hannah(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a video of Bailey pointing at a roof",
                       cid="jordan")
        self.assertEqual(self.jobs(), [])
        self.assertIn("hannah", res["brief"])
        self.assertIn("roof rover", res["brief"])

    def test_jordan_roof_rover_store_redirects_to_hannah(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a video of the Roof Rover store",
                       cid="jordan")
        self.assertEqual(self.jobs(), [])
        self.assertIn("hannah", res["brief"])

    def test_hannah_fetch_ask_redirects_to_jordan(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a video of Fetch", cid="hannah")
        self.assertEqual(self.jobs(), [])
        self.assertIn("jordan", res["brief"])

    # -- roster people stay Mia's desk; self stays self --
    def test_jordan_person_ask_still_mia_desk(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("make a video of mara", cid="jordan")
        self.assertEqual(self.jobs(), [])
        self.assertEqual(res.get("sogni_media_redirect"), "mia")

    def test_jordan_video_of_you_still_self(self):
        assist.store_sogni_session(self.conn, TOKENS)
        self.say("make a video of you waving", cid="jordan")
        (job,) = self.jobs()
        self.assertEqual(job["target_character_id"], "jordan")
        self.assertIsNone(job["subject_mode"])

    def test_fetch_promo_without_session_is_never_faked(self):
        res = self.say("make a video of Fetch", cid="jordan")
        self.assertEqual(self.jobs(), [])
        self.assertTrue(res.get("sogni_login_box"))
        self.assertIn(assist._SOGNI_NO_SESSION_REPLY, res["brief"])

    # -- bare nude asks are adult plan images --
    def _assert_adult_self_image(self, text, cid="hannah"):
        assist.store_sogni_session(self.conn, TOKENS)
        self.say(text, cid=cid)
        (job,) = self.jobs()
        self.assertEqual(job["kind"], "image")
        self.assertEqual(job["target_character_id"], cid)
        self.assertNotIn("fully clothed", job["prompt"])

    def test_send_me_a_nude(self):
        self._assert_adult_self_image("send me a nude")

    def test_send_nudes(self):
        self._assert_adult_self_image("send nudes")

    def test_pic_of_you_naked(self):
        self._assert_adult_self_image("pic of you naked", cid="jordan")

    def test_something_nude_renders_image_no_menu(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("send me something nude")
        self.assertNotIn("reply_style", res or {})
        (job,) = self.jobs()
        self.assertEqual(job["kind"], "image")

    def test_mia_nude_of_mara_targets_mara(self):
        assist.store_sogni_session(self.conn, TOKENS)
        self.say("make a nude of mara", cid="mia")
        (job,) = self.jobs()
        self.assertEqual(job["kind"], "image")
        self.assertEqual(job["target_character_id"], "mara")

    def test_nude_video_words_stay_video(self):
        assist.store_sogni_session(self.conn, TOKENS)
        self.say("send me a nude video", cid="hannah")
        (job,) = self.jobs()
        self.assertEqual(job["kind"], "video")
        self.assertEqual(job["target_character_id"], "hannah")

    def test_nude_ask_without_session_asks_login_not_free_desk(self):
        with mock.patch.object(assist, "_image_post",
                               return_value=None) as ip:
            res = self.say("send me a nude")
        ip.assert_not_called()
        self.assertEqual(self.jobs(), [])
        self.assertTrue(res.get("sogni_login_box"))


class PromoWorkerTest(unittest.TestCase):
    """End to end with a mocked render: Jordan's Fetch promo
    queues, renders with the canonical mascot reference (never her
    face, never a character prompt wrap), and lands as a promo
    delivery that honestly says it arrived silent."""

    def setUp(self):
        import tempfile
        from app import config, characters as _chars
        self._config = config
        self._chars = _chars
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        self._tmpdir = tempfile.mkdtemp(prefix="promo-worker-")
        self._db_path = os.path.join(self._tmpdir, "test.db")
        self._old_db_path = config.DB_PATH
        config.DB_PATH = self._db_path
        self.conn = sqlite3.connect(self._db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        assist.ensure_tables(self.conn)
        self.conn.commit()
        for cid, name in (("hannah", "Hannah"), ("mia", "Mia"),
                          ("jordan", "Jordan"), ("rocky", "Rocky"),
                          ("mara", "Mara")):
            store.upsert_local_character(
                self.conn, {"id": cid, "name": name,
                            "owner_user_id": "owner_1",
                            "avatar_url": f"/generated/{cid}.jpg"})
        self.conn.commit()
        self.spawned = []
        self._patches = [
            mock.patch.object(_chars, "get_characters",
                              _fake_roster(self.conn)),
            mock.patch.object(
                _chars, "get_character",
                lambda cid, user_id=None: {
                    "id": cid, "name": "Jordan",
                    "avatar_url": "/generated/jordan.jpg"}),
            mock.patch.object(assist, "_spawn_sogni_plan_job",
                              lambda job_id: self.spawned.append(
                                  job_id)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        import shutil
        for p in reversed(self._patches):
            p.stop()
        self.conn.close()
        self._config.DB_PATH = self._old_db_path
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_fetch_promo_end_to_end(self):
        import tempfile
        assist.store_sogni_session(self.conn, dict(TOKENS))
        # The owner's real flow: a self video first, then the
        # correction to Fetch with a male voice.
        assist.maybe_assist(
            self.conn, OWNER, "make a video of you dancing at a "
            "club", "jordan")
        self.conn.execute(
            "UPDATE sogni_plan_jobs SET status='done'")
        self.conn.commit()
        res = assist.maybe_assist(
            self.conn, OWNER,
            "No don't use you use Fetch the dog and put males "
            "voice have the app jn the background", "jordan")
        self.assertIsNotNone(res)
        self.assertEqual(len(self.spawned), 2)
        job_id = self.spawned[1]
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.write(fd, b"\x00" * 2048)
        os.close(fd)
        seen = {}

        def fake_ref(url):
            seen["ref_url"] = url
            return path + ".ref"

        def fake_run(session, prompt, ref_path, model=None):
            seen["prompt"] = prompt
            seen["ref"] = ref_path
            return ({"status": "done", "project_id": "promo-1",
                     "path": path,
                     "detail": "billingMode=subscription"},
                    dict(TOKENS))

        try:
            with mock.patch.object(assist, "_fetch_first_frame",
                                   fake_ref), \
                    mock.patch.object(assist, "_sogni_plan_run",
                                      fake_run):
                assist._run_sogni_plan_job(job_id)
        finally:
            os.unlink(path)
        self.assertEqual(seen["ref_url"],
                         assist._PROMO_REF_URLS["fetch"])
        self.assertIn("Fetch", seen["prompt"])
        self.assertIn("golden retriever", seen["prompt"])
        self.assertNotIn("Photorealistic", seen["prompt"])
        row = self.conn.execute(
            "SELECT status FROM sogni_plan_jobs WHERE id=?",
            (job_id,)).fetchone()
        self.assertEqual(row["status"], "done")
        turns = self.conn.execute(
            "SELECT text, attachments FROM conversation "
            "ORDER BY id").fetchall()
        self.assertEqual(len(turns), 1)
        self.assertIn("promo", turns[0]["text"])
        self.assertIn("silent", turns[0]["text"])
        atts = __import__("json").loads(turns[0]["attachments"])
        self.assertEqual(atts[0]["kind"], "video")
        self.assertEqual(atts[0]["name"], "fetch-video.mp4")


if __name__ == "__main__":
    unittest.main()
