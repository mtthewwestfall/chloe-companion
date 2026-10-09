"""Assistant-girl parity tests (2026-10-09, owner-directed).

Three private assistant girls share ONE ability set — studio,
attachments, Sogni plan lane, image desk, status + continuity
brief — split by PROJECT lane so they stop confusing each other:
Hannah carries Roof Rover, Mia carries Whispr (this app), Jordan
carries OneFetch. The machinery is shared; the project each girl
may touch is not (see tests/test_project_lanes.py for the lane
gates themselves).

Rules pinned here: the assistant set is exactly
{hannah, mia, jordan}; non-owners get nothing in any thread;
non-assistant threads (even the owner's) stay walled off from the
studio / Sogni / attachment lanes; Sogni jobs queued from any
assistant thread carry HER character id and plan billing, never
credits.

All network seams are mocked — zero real renders, zero real calls.

Run:  python -m unittest tests.test_mia_assistant -v
      (from the project root, with the venv's python)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, characters, db, store  # noqa: E402

OWNER = {"user_id": "owner_1", "tier": "basic"}
STRANGER = {"user_id": "someone_else", "tier": "vip"}
TOKENS = {"token": "tok-aaa", "refreshToken": "ref-bbb"}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    assist.ensure_tables(conn)
    return conn


class MiaBase(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        self.conn = _conn()
        for cid, name in (("mia", "Mia"), ("jordan", "Jordan")):
            store.upsert_local_character(
                self.conn, {"id": cid, "name": name,
                            "owner_user_id": "owner_1"})
        store.upsert_local_character(
            self.conn, {"id": "mara", "name": "Mara"})

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def say(self, text, user=OWNER, cid="mia", attachments=None):
        if attachments:
            return assist.maybe_assist(self.conn, user, text, cid,
                                       attachments=attachments)
        return assist.maybe_assist(self.conn, user, text, cid)


class AssistantSetTest(MiaBase):
    def test_set_is_exactly_hannah_mia_jordan(self):
        self.assertEqual(assist.ASSISTANT_CHARACTERS,
                         frozenset({"hannah", "mia", "jordan"}))
        self.assertTrue(assist.is_assistant_character("jordan"))
        self.assertTrue(assist.is_assistant_character(" Mia "))
        self.assertFalse(assist.is_assistant_character("mara"))
        self.assertFalse(assist.is_assistant_character("chloe"))
        self.assertFalse(assist.is_assistant_character(None))
        self.assertFalse(assist.is_assistant_character(""))


class SogniParityTest(MiaBase):
    def test_video_ask_without_session_asks_for_login(self):
        res = self.say("make a video of you waving")
        self.assertIsNotNone(res)
        self.assertTrue(res["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        self.assertIn("sogni login", res["brief"])
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sogni_plan_jobs").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_jordan_video_ask_without_session_asks_for_login(self):
        res = self.say("jordan, make a video of you waving", cid="jordan")
        self.assertIsNotNone(res)
        self.assertIn("sogni login", res["brief"])

    def test_video_ask_with_session_enqueues_for_her_on_plan(self):
        assist.store_sogni_session(self.conn, TOKENS)
        spawned = []
        with mock.patch.object(assist, "_spawn_sogni_plan_job",
                               lambda job_id: spawned.append(job_id)):
            res = self.say("make a video of you waving")
            self.conn.execute(
                "UPDATE sogni_plan_jobs SET status = 'done'")
            self.conn.commit()
            jres = self.say("make a video of you dancing", cid="jordan")
        self.assertIn("rendering", res["brief"])
        self.assertIn("rendering", jres["brief"])
        self.assertEqual(len(spawned), 2)
        rows = self.conn.execute(
            "SELECT character_id, billing_mode, status FROM "
            "sogni_plan_jobs ORDER BY id").fetchall()
        self.assertEqual([r["character_id"] for r in rows],
                         ["mia", "jordan"])
        self.assertTrue(all(r["billing_mode"] == "subscription"
                            for r in rows))
        # First job was force-completed mid-test; the fresh one queues.
        self.assertEqual([r["status"] for r in rows], ["done", "queued"])

    def test_sogni_mention_with_session_confirms_signed_in(self):
        assist.store_sogni_session(self.conn, TOKENS)
        res = self.say("use my sogni account from now on", cid="jordan")
        self.assertIsNotNone(res)
        self.assertIn("signed into your sogni", res["brief"])

    def test_detect_prefixes(self):
        for girl in ("hannah", "mia", "jordan"):
            self.assertEqual(
                assist.detect_sogni_video_request(
                    f"{girl}, make a video of you waving"), "you waving")
            self.assertEqual(
                assist.detect_image_request(
                    f"{girl}, make a picture of a red barn"),
                "a red barn")

    def test_image_ask_reaches_the_picture_desk(self):
        with mock.patch.object(assist, "_image_post",
                               return_value=None):
            res = self.say("make a picture of a red barn", cid="jordan")
        self.assertIsNotNone(res)
        self.assertIn("free image keys", res["brief"])


class LaneBriefTest(MiaBase):
    def test_each_brief_knows_only_her_project(self):
        hannah = assist._owner_brief_reply("hannah")
        self.assertIn("ROOF ROVER", hannah)
        self.assertIn("$4.99", hannah)
        self.assertIn("Mia's project", hannah)
        self.assertIn("Jordan's project", hannah)
        mia = assist._owner_brief_reply("mia")
        self.assertIn("WHISPR", mia)
        self.assertIn("$9.99", mia)
        self.assertIn("Hannah's", mia)
        self.assertIn("Jordan's project", mia)
        jordan = assist._owner_brief_reply("jordan")
        self.assertIn("ONEFETCH", jordan)
        self.assertIn("$19.99", jordan)
        self.assertIn("Hannah's", jordan)
        self.assertIn("Mia's project", jordan)

    def test_default_and_legacy_briefs_unchanged(self):
        self.assertTrue(
            assist._owner_brief_reply().endswith(assist._HANNAH_BRIEF))
        self.assertTrue(
            assist._owner_brief_reply("mara").endswith(
                assist._OWNER_BRIEF))
        self.assertTrue(
            assist._owner_brief_reply("chloe").endswith(
                assist._OWNER_BRIEF))

    _FAKE = {"onefetch": {"founder_count": 3, "founder_spots_left": 197,
                          "founder_open": True, "ai_available": True,
                          "site_status": 200},
             "roof_rover": {"shelf_leads": 5, "site_status": 200},
             "gemini": {"believed_healthy": 2,
                        "pool": {"healthy": 2}, "degraded": False}}

    def test_status_reports_her_own_project_only(self):
        with mock.patch.object(assist, "fetch_app_status",
                               return_value=dict(self._FAKE)):
            han = self.say("how's the shelf?", cid="hannah")
            jor = self.say("how's onefetch doing?", cid="jordan")
        self.assertIn("5 leads on the $4.99 store shelf", han["brief"])
        self.assertNotIn("3 founders", han["brief"])
        self.assertIn("3 founders", jor["brief"])
        self.assertIn("ONEFETCH", jor["brief"])
        self.assertNotIn("$4.99 store shelf", jor["brief"])

    def test_status_in_mia_reports_whispr(self):
        with mock.patch.object(
                assist, "_whispr_status_lines",
                return_value=["whispr: 54 accounts, 5 verified"]):
            res = self.say("how's the app doing?")
        self.assertIn("54 accounts", res["brief"])
        self.assertIn("WHISPR", res["brief"])


class StudioParityTest(MiaBase):
    def setUp(self):
        super().setUp()
        self.old_roster = characters.get_characters
        characters.get_characters = _fake_roster(self.conn)

    def tearDown(self):
        characters.get_characters = self.old_roster
        super().tearDown()

    def test_add_give_from_mias_thread(self):
        att = {"id": "att_1", "kind": "image", "name": "pic.jpg",
               "filename": "pic.jpg", "mime_type": "image/jpeg",
               "size": 10, "url": "/generated/hannah-uploads/att_1.jpg",
               "extraction_status": "ok"}
        res = self.say("add this as a give for Mara", attachments=[att])
        self.assertIsNotNone(res)
        self.assertEqual(assist.give_count(self.conn, "mara"), 1)

    def test_create_flow_starts_in_mia(self):
        res = self.say("create a character")
        self.assertIsNotNone(res)
        self.assertIn("NAME", res["brief"])
        state = assist._studio_get(self.conn, "owner_1", "create_flow")
        self.assertIsNotNone(state)

    def test_studio_still_walled_in_other_threads(self):
        res = self.say("add this as a give for Mara", cid="mara")
        self.assertIsNone(res)
        self.assertEqual(assist.give_count(self.conn, "mara"), 0)
        att = {"id": "att_1", "kind": "image", "name": "pic.jpg",
               "filename": "pic.jpg", "mime_type": "image/jpeg",
               "size": 10, "url": "/generated/hannah-uploads/att_1.jpg",
               "extraction_status": "ok"}
        res = self.say("add this as a give for Mara", cid="mara",
                       attachments=[att])
        self.assertIn("cannot take files", res["brief"])
        self.assertEqual(assist.give_count(self.conn, "mara"), 0)


class WallTest(MiaBase):
    def test_stranger_gets_nothing_in_assistant_threads(self):
        for cid in ("mia", "jordan", "hannah"):
            self.assertIsNone(self.say("make a video of you waving",
                                       user=STRANGER, cid=cid))
            self.assertIsNone(self.say("how's onefetch doing?",
                                       user=STRANGER, cid=cid))
            self.assertIsNone(self.say("catch me up", user=STRANGER,
                                       cid=cid))

    def test_non_assistant_thread_stays_walled_for_owner(self):
        self.assertIsNone(self.say("make a video of you waving",
                                   cid="chloe"))
        self.assertIsNone(self.say("create a character", cid="chloe"))


class AttachmentParityTest(MiaBase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_generated = assist.config.GENERATED_DIR
        assist.config.GENERATED_DIR = self.tmp.name

    def tearDown(self):
        assist.config.GENERATED_DIR = self.old_generated
        self.tmp.cleanup()
        super().tearDown()

    def test_text_file_reaches_mia_without_vision_call(self):
        saved = assist.save_chat_attachment(
            self.conn, "owner_1", "mia", "brief.txt", "text/plain",
            b"Alpha bravo. The secret word is pineapple.")
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "mia", [saved["id"]])[0]
        with mock.patch.object(
                assist, "_attachment_post",
                side_effect=AssertionError("vision must not run")):
            res = assist.maybe_assist(
                self.conn, OWNER, "read this for me", "mia",
                attachments=[row])
        self.assertIsNotNone(res)
        self.assertIn("pineapple", res["brief"])

    def test_text_file_reaches_jordan_without_vision_call(self):
        saved = assist.save_chat_attachment(
            self.conn, "owner_1", "jordan", "leads.txt", "text/plain",
            b"The secret word is listingkit.")
        row = assist.get_chat_attachments(
            self.conn, "owner_1", "jordan", [saved["id"]])[0]
        with mock.patch.object(
                assist, "_attachment_post",
                side_effect=AssertionError("vision must not run")):
            res = assist.maybe_assist(
                self.conn, OWNER, "read this for me", "jordan",
                attachments=[row])
        self.assertIsNotNone(res)
        self.assertIn("listingkit", res["brief"])

    def test_attachments_scoped_per_character(self):
        saved = assist.save_chat_attachment(
            self.conn, "owner_1", "mia", "notes.txt", "text/plain",
            b"hello mia")
        with self.assertRaises(ValueError):
            assist.get_chat_attachments(
                self.conn, "owner_1", "hannah", [saved["id"]])

    def test_attachment_lane_still_walled_elsewhere(self):
        saved = assist.save_chat_attachment(
            self.conn, "owner_1", "mia", "brief.txt", "text/plain",
            b"Alpha bravo.")
        row = dict(saved)
        row["extracted_text"] = "Alpha bravo."
        res = assist.analyze_chat_attachments(
            self.conn, OWNER, "read this", "mara", [row])
        self.assertIn("cannot take files", res["brief"])


def _fake_roster(conn):
    """characters.get_characters stand-in fed from the test DB."""
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


if __name__ == "__main__":
    unittest.main()
