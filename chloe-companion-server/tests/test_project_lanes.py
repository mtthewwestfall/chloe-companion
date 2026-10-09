"""Project-lane gates (2026-10-09, owner-directed three-lane split).

Hannah carries Roof Rover (roof-scout) ONLY, Mia carries Whispr
(chloe-companion) ONLY, Jordan carries OneFetch (realtor-app)
ONLY. The assist machinery is shared; the lane gates are
deterministic in the assist layer:

- a build / review / status / marketing ask about another girl's
  project gets the fixed in-voice redirect naming HER — never
  queued, never answered from the wrong brief;
- a repo outside all three lanes gets the outside-my-lane line;
- in-lane asks keep every existing rail (allowlist, checks,
  fast-forward-only merges on explicit ship words only).

Run: python -m unittest tests.test_project_lanes -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, db, store  # noqa: E402

OWNER = {"user_id": "owner_1", "tier": "basic"}
SECRET = "lane-tests-secret"

FAKE_STATUS = {"onefetch": {"founder_count": 3, "founder_spots_left": 197,
                            "founder_open": True, "ai_available": True,
                            "site_status": 200},
               "roof_rover": {"shelf_leads": 5, "site_status": 200},
               "gemini": {"believed_healthy": 2,
                          "pool": {"healthy": 2}, "degraded": False}}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    assist.ensure_tables(conn)
    return conn


class LaneBase(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS",
                         "DEV_AGENT_SECRET")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        os.environ["DEV_AGENT_SECRET"] = SECRET
        self.conn = _conn()

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def say(self, text, cid):
        return assist.maybe_assist(self.conn, OWNER, text, cid)

    def queued(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM dev_jobs").fetchone()["n"]

    def assertRedirect(self, res, girl):
        self.assertIsNotNone(res)
        self.assertTrue(
            res["brief"].startswith(assist.FIXED_REPLY_PREFIX),
            res["brief"])
        self.assertIn(f"that's {girl}'s project", res["brief"])


class LaneMapTest(LaneBase):
    def test_lane_map(self):
        self.assertEqual(assist.character_lane("hannah"), assist.LANE_ROOF)
        self.assertEqual(assist.character_lane("mia"), assist.LANE_WHISPR)
        self.assertEqual(assist.character_lane("jordan"),
                         assist.LANE_ONEFETCH)
        self.assertIsNone(assist.character_lane("mara"))
        self.assertEqual(assist.repo_lane("roof-scout"), assist.LANE_ROOF)
        self.assertEqual(assist.repo_lane("mtthewwestfall/chloe-companion"),
                         assist.LANE_WHISPR)
        self.assertEqual(assist.repo_lane("realtor-app"),
                         assist.LANE_ONEFETCH)
        self.assertIsNone(assist.repo_lane("dev-agent"))
        self.assertEqual(assist.LANE_REPOS[assist.LANE_ONEFETCH],
                         "realtor-app")


class CrossLaneBuildTest(LaneBase):
    def test_mia_asked_to_fix_roof_redirects_to_hannah(self):
        res = self.say("use your dev bot to fix the login bug in the "
                       "roof-scout repo", "mia")
        self.assertRedirect(res, "hannah")
        self.assertIn("roof rover", res["brief"])
        self.assertEqual(self.queued(), 0)

    def test_hannah_asked_to_fix_companion_redirects_to_mia(self):
        res = self.say("use your dev bot to fix the login bug in the "
                       "chloe-companion repo", "hannah")
        self.assertRedirect(res, "mia")
        self.assertIn("whispr", res["brief"])
        self.assertEqual(self.queued(), 0)

    def test_hannah_asked_for_onefetch_build_redirects_to_jordan(self):
        res = self.say("use your dev bot to fix the signup in the "
                       "realtor-app repo", "hannah")
        self.assertRedirect(res, "jordan")
        self.assertIn("onefetch", res["brief"])
        self.assertEqual(self.queued(), 0)

    def test_jordan_asked_about_roof_redirects_to_hannah(self):
        res = self.say("use your dev bot to fix the lead store in the "
                       "roof-scout repo", "jordan")
        self.assertRedirect(res, "hannah")
        self.assertEqual(self.queued(), 0)

    def test_jordan_asked_for_whispr_build_redirects_to_mia(self):
        res = self.say("use your dev bot to fix the roster page in "
                       "the chloe-companion repo", "jordan")
        self.assertRedirect(res, "mia")
        self.assertEqual(self.queued(), 0)

    def test_content_lane_redirect_without_a_repo_named(self):
        res = self.say("use your dev bot to fix the roof rover "
                       "warmup", "mia")
        self.assertRedirect(res, "hannah")
        self.assertEqual(self.queued(), 0)

    def test_neither_lane_repo_gets_outside_line(self):
        res = self.say("use your dev bot to fix the dev-agent repo",
                       "hannah")
        self.assertIsNotNone(res)
        self.assertIn("outside my lane", res["brief"])
        self.assertIn("onefetch", res["brief"])  # names Jordan's lane
        self.assertEqual(self.queued(), 0)


class InLaneBuildTest(LaneBase):
    def test_hannah_queues_roof_scout(self):
        res = self.say("use your dev bot to fix the login bug in the "
                       "roof-scout repo", "hannah")
        self.assertIn("queued build job #", res["brief"])
        row = self.conn.execute("SELECT repo FROM dev_jobs").fetchone()
        self.assertEqual(row["repo"], "mtthewwestfall/roof-scout")

    def test_mia_queues_chloe_companion(self):
        res = self.say("use your dev bot to fix the login bug in the "
                       "chloe-companion repo", "mia")
        self.assertIn("queued build job #", res["brief"])
        row = self.conn.execute("SELECT repo FROM dev_jobs").fetchone()
        self.assertEqual(row["repo"], "mtthewwestfall/chloe-companion")

    def test_jordan_queues_realtor_app(self):
        res = self.say("use your dev bot to fix the signup in the "
                       "realtor-app repo", "jordan")
        self.assertIn("queued build job #", res["brief"])
        row = self.conn.execute("SELECT repo FROM dev_jobs").fetchone()
        self.assertEqual(row["repo"], "mtthewwestfall/realtor-app")

    def test_own_project_content_defaults_to_her_repo(self):
        res = self.say("use your dev bot to fix the warmup", "hannah")
        self.assertIn("queued build job #", res["brief"])
        row = self.conn.execute("SELECT repo FROM dev_jobs").fetchone()
        self.assertEqual(row["repo"], "mtthewwestfall/roof-scout")
        res = self.say("use your dev bot to fix the onefetch "
                       "listing kit", "jordan")
        self.assertIn("queued build job #", res["brief"])


class CrossLaneReviewTest(LaneBase):
    def test_hannah_reviewing_companion_redirects_to_mia(self):
        res = self.say("review the chloe-companion repo", "hannah")
        self.assertRedirect(res, "mia")

    def test_jordan_reviewing_roof_redirects_to_hannah(self):
        res = self.say("review the roof-scout repo", "jordan")
        self.assertRedirect(res, "hannah")

    def test_mia_reviewing_realtor_redirects_to_jordan(self):
        res = self.say("review the realtor-app repo", "mia")
        self.assertRedirect(res, "jordan")

    def test_hannah_reviewing_neither_lane_repo(self):
        res = self.say("review the dev-agent repo", "hannah")
        self.assertIn("outside my lane", res["brief"])

    def test_in_lane_review_runs_normally(self):
        # No GitHub token in the test DB: the honest no-key brief,
        # proving the ask passed the lane gate.
        res = self.say("review the roof-scout repo", "hannah")
        self.assertIn("don't have a GitHub key", res["brief"])
        res = self.say("review the realtor-app repo", "jordan")
        self.assertIn("don't have a GitHub key", res["brief"])


class CrossLaneStatusTest(LaneBase):
    def test_hannah_asked_about_onefetch_founders_redirects(self):
        res = self.say("how's onefetch doing?", "hannah")
        self.assertRedirect(res, "jordan")

    def test_jordan_asked_about_roof_redirects(self):
        res = self.say("how's roof rover doing?", "jordan")
        self.assertRedirect(res, "hannah")

    def test_mia_asked_about_roof_redirects(self):
        res = self.say("how's roof rover doing?", "mia")
        self.assertRedirect(res, "hannah")

    def test_in_lane_status_reports(self):
        with mock.patch.object(assist, "fetch_app_status",
                               return_value=dict(FAKE_STATUS)):
            jor = self.say("how's onefetch doing?", "jordan")
            han = self.say("how's roof rover doing?", "hannah")
        self.assertIn("3 founders", jor["brief"])
        self.assertIn("5 leads on the $4.99 store shelf", han["brief"])


class CrossLaneMarketingTest(LaneBase):
    def test_mia_roof_promo_redirects_to_hannah(self):
        res = self.say("write a promo hook for roof rover", "mia")
        self.assertRedirect(res, "hannah")

    def test_hannah_onefetch_promo_redirects_to_jordan(self):
        res = self.say("write a caption promoting onefetch", "hannah")
        self.assertRedirect(res, "jordan")

    def test_jordan_whispr_promo_redirects_to_mia(self):
        res = self.say("write a promo for whispr", "jordan")
        self.assertRedirect(res, "mia")

    def test_in_lane_marketing_proceeds(self):
        res = self.say("write a caption promoting onefetch", "jordan")
        self.assertIn("verified facts below", res["brief"])
        self.assertIn("$19.99", res["brief"])


class MergeLaneTest(LaneBase):
    def _finished_job(self, repo, task="build the thing"):
        jid = assist.enqueue_dev_job(self.conn, "owner_1", "hannah",
                                     repo, task)
        self.conn.execute(
            "UPDATE dev_jobs SET status='done', branch='agent/x' "
            "WHERE id = ?", (jid,))
        self.conn.commit()
        return jid

    def test_named_other_lane_merge_redirects(self):
        self._finished_job("mtthewwestfall/chloe-companion")
        res = self.say("merge the chloe-companion fix", "hannah")
        self.assertRedirect(res, "mia")
        self.assertEqual(self.queued(), 1)  # nothing new queued

    def test_unnamed_merge_of_other_lane_job_redirects(self):
        self._finished_job("mtthewwestfall/realtor-app")
        res = self.say("merge it and push", "hannah")
        self.assertRedirect(res, "jordan")
        self.assertEqual(self.queued(), 1)

    def test_in_lane_merge_queues(self):
        self._finished_job("mtthewwestfall/chloe-companion")
        res = self.say("merge it and push", "mia")
        self.assertIn("queued merge job #", res["brief"])
        self.assertEqual(self.queued(), 2)

    def test_nothing_mergeable_still_honest(self):
        res = self.say("merge it and push", "jordan")
        self.assertIn("nothing to merge", res["brief"])


class OwnerBriefLaneTest(LaneBase):
    def test_brief_questions_use_her_lane(self):
        for cid, needle in (("hannah", "$4.99"), ("mia", "$9.99"),
                            ("jordan", "$19.99")):
            res = self.say("catch me up", cid)
            self.assertIsNotNone(res, cid)
            self.assertIn(needle, res["brief"])


if __name__ == "__main__":
    unittest.main()
