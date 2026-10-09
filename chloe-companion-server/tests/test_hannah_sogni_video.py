"""Hannah Sogni video tests (2026-10-09): owner-only in-chat video
generation through the Sogni API, spend-safe by construction.

The rails under test:
  * intent detection ("Create a video of you taking off your top");
  * no key on file -> she says she can't YET and asks for the API key
    paste (never "I don't have access to that app"), zero spend;
  * key intake rides the assist_secrets rails (stored, scrubbed from
    history, FIXED confirmation, never echoed);
  * QUOTE BEFORE SPEND: an ask produces a plan + Spark estimate and a
    pending quote — the render seam is not touched;
  * no confirmation = no spend (cancel / moving on clears the quote);
  * one confirmation = exactly one render, logged, delivered in-thread
    as a video attachment;
  * Sogni filter block -> honest in-thread report, zero Spark recorded
    as spent;
  * daily cap and non-owner gating.

Sogni is fully mocked at the assist seams (_sogni_estimate_spark /
_sogni_render_video / spawn_sogni_video_job): zero real Spark here.

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, characters, config  # noqa: E402

OWNER_USER = {"user_id": "owner_1", "tier": "basic"}
FAKE_KEY = "sogni_test_key_0123456789abcdef"  # fake, realistic length
HANNAH = {"id": "hannah", "name": "Hannah",
          "avatar_url": "https://example.invalid/hannah.jpg"}


def _make_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, "
        "character_id TEXT, role TEXT, text TEXT, attachments TEXT, "
        "created_at TEXT)")
    assist.ensure_tables(conn)
    conn.commit()
    return conn


def _log_user_turn(conn, user_id: str, text: str, cid: str = "hannah"):
    conn.execute(
        "INSERT INTO conversation (user_id, character_id, role, text, "
        "attachments) VALUES (?, ?, 'user', ?, '[]')",
        (user_id, cid, text))
    conn.commit()


class HannahSogniVideoTest(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS",
                         "HANNAH_SOGNI_DAILY_CAP",
                         "HANNAH_SOGNI_VIDEO_MODEL")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        os.environ.pop("HANNAH_SOGNI_DAILY_CAP", None)
        os.environ.pop("HANNAH_SOGNI_VIDEO_MODEL", None)
        self.old_config_key = config.SOGNI_API_KEY
        config.SOGNI_API_KEY = ""
        # hermetic temp-file DB (the worker opens its OWN connection)
        self.tmp = tempfile.mkdtemp(prefix="sogni-video-test-")
        self.db_path = os.path.join(self.tmp, "test.db")
        self.conn = _make_conn(self.db_path)
        # seams: no real Sogni, no real roster fetch, sync worker
        self.orig = {
            "estimate": assist._sogni_estimate_spark,
            "render": assist._sogni_render_video,
            "spawn": assist.spawn_sogni_video_job,
            "get_character": characters.get_character,
            "get_characters": characters.get_characters,
        }
        self.render_calls = []
        self.estimate_calls = []

        def fake_estimate(api_key):
            self.estimate_calls.append(api_key)
            return 23.5

        def fake_render(api_key, prompt_text, girl_name, avatar_url):
            self.render_calls.append({
                "prompt": prompt_text, "girl": girl_name,
                "avatar": avatar_url})
            return {"project_id": "proj_test_1", "spark": 23.5,
                    "url": "/generated/hannah-sogni-test.mp4"}

        assist._sogni_estimate_spark = fake_estimate
        assist._sogni_render_video = fake_render
        assist.spawn_sogni_video_job = lambda job: assist._run_sogni_video_job(
            job, conn_factory=lambda: _make_conn(self.db_path))
        characters.get_character = lambda cid, user_id=None: dict(HANNAH)
        characters.get_characters = lambda **kw: [dict(HANNAH)]

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.SOGNI_API_KEY = self.old_config_key
        assist._sogni_estimate_spark = self.orig["estimate"]
        assist._sogni_render_video = self.orig["render"]
        assist.spawn_sogni_video_job = self.orig["spawn"]
        characters.get_character = self.orig["get_character"]
        characters.get_characters = self.orig["get_characters"]
        self.conn.close()

    # -- helpers ------------------------------------------------------
    def ask(self, text, user=OWNER_USER, cid="hannah"):
        return assist.maybe_assist(self.conn, user, text, cid)

    def log_rows(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM sogni_video_log ORDER BY id").fetchall()]

    def her_turns(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM conversation WHERE role = 'hannah' "
            "ORDER BY id").fetchall()]

    def quote(self, text="Create a video of you taking off your top"):
        assist.store_sogni_api_key(self.conn, FAKE_KEY)
        return self.ask(text)

    # -- intent -------------------------------------------------------
    def test_detect_video_intent(self):
        self.assertEqual(
            assist.detect_sogni_video_request(
                "Create a video of you taking off your top"),
            "you taking off your top")
        self.assertIn("dancing", assist.detect_sogni_video_request(
            "make me a video of Mara dancing in the kitchen"))
        self.assertIsNotNone(assist.detect_sogni_video_request(
            "Can you create a video of you blowing me a kiss?"))
        self.assertEqual(assist.detect_sogni_video_request(
            "make a video"), "")
        self.assertIsNone(assist.detect_sogni_video_request(
            "Yes you can through sogni"))
        self.assertIsNone(assist.detect_sogni_video_request(
            "I love watching videos with you"))
        self.assertIsNone(assist.detect_sogni_video_request(
            "create a video upload feature in the repo"))
        self.assertIsNone(assist.detect_sogni_video_request("hey babe"))

    # -- key missing --------------------------------------------------
    def test_missing_key_asks_for_paste_and_spends_nothing(self):
        def boom(*a, **k):
            raise AssertionError("Sogni must not be touched without a key")
        assist._sogni_estimate_spark = boom
        assist._sogni_render_video = boom
        hit = self.ask("Create a video of you taking off your top")
        self.assertIsNotNone(hit)
        self.assertTrue(hit["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        body = hit["brief"]
        self.assertIn("can't make that YET", body)
        self.assertIn("Sogni API key", body)
        self.assertIn("NOT your web login", body)
        self.assertNotIn("I don't have access", body)
        self.assertNotIn("strictly here", body)
        self.assertEqual(self.log_rows(), [])
        self.assertIsNone(
            assist._studio_get(self.conn, "owner_1", "sogni_video_quote"))
        # she is now waiting for the paste
        self.assertIsNotNone(
            assist._studio_get(self.conn, "owner_1", "sogni_awaiting_key"))

    # -- key intake ---------------------------------------------------
    def test_bare_key_paste_after_ask_stores_and_scrubs(self):
        self.ask("Create a video of you taking off your top")
        _log_user_turn(self.conn, "owner_1", FAKE_KEY)
        hit = self.ask(FAKE_KEY)
        self.assertIsNotNone(hit)
        self.assertTrue(hit["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        self.assertNotIn(FAKE_KEY, hit["brief"])
        self.assertNotIn(FAKE_KEY[:8], hit["brief"])
        self.assertEqual(assist.stored_sogni_api_key(self.conn), FAKE_KEY)
        rows = self.conn.execute(
            "SELECT text FROM conversation WHERE user_id = 'owner_1' "
            "AND role = 'user'").fetchall()
        self.assertTrue(rows)
        for r in rows:
            self.assertNotIn(FAKE_KEY, r[0])
        self.assertIsNone(
            assist._studio_get(self.conn, "owner_1", "sogni_awaiting_key"))

    def test_labeled_key_paste_stores(self):
        msg = f"here's my sogni api key: {FAKE_KEY} for the videos"
        _log_user_turn(self.conn, "owner_1", msg)
        hit = self.ask(msg)
        self.assertIsNotNone(hit)
        self.assertEqual(assist.stored_sogni_api_key(self.conn), FAKE_KEY)

    def test_sogni_key_revoke(self):
        assist.store_sogni_api_key(self.conn, FAKE_KEY)
        hit = self.ask("forget my sogni key")
        self.assertIsNotNone(hit)
        self.assertTrue(hit["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        self.assertEqual(assist.stored_sogni_api_key(self.conn), "")
        hit2 = self.ask("revoke the sogni key")
        self.assertIn("nothing to wipe", hit2["brief"])

    def test_sogni_word_in_chat_is_not_a_key(self):
        hit = self.ask("Yes you can through sogni")
        self.assertIsNone(hit)  # falls through to normal chat
        self.assertEqual(assist.stored_sogni_api_key(self.conn), "")

    # -- quote before spend -------------------------------------------
    def test_quote_before_spend(self):
        def no_render(*a, **k):
            raise AssertionError("render ran before confirmation")
        assist._sogni_render_video = no_render
        hit = self.quote()
        self.assertIsNotNone(hit)
        self.assertTrue(hit["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        body = hit["brief"]
        self.assertIn("23.5", body)          # live estimate quoted
        self.assertIn("Spark", body)
        self.assertIn("minimax-h3-fastvideo-int8_i2v_turbo", body)
        self.assertIn("taking off your top", body)
        self.assertIn("no yes, no spend", body)
        self.assertEqual(self.estimate_calls, [FAKE_KEY])
        self.assertEqual(self.log_rows(), [])  # nothing logged as spent
        pending = assist._studio_get(self.conn, "owner_1",
                                     "sogni_video_quote")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["estimate_spark"], 23.5)

    def test_estimate_unavailable_uses_labeled_ballpark(self):
        assist._sogni_estimate_spark = lambda api_key: None
        hit = self.quote("make a video of you dancing")
        body = hit["brief"]
        self.assertIn("23.5", body)
        self.assertIn("estimate", body.lower())
        self.assertIn("ballpark", body)

    # -- confirmation ---------------------------------------------------
    def test_confirm_spends_once_and_delivers_video(self):
        self.quote()
        hit = self.ask("yes")
        self.assertIsNotNone(hit)
        self.assertIn("making it now", hit["brief"])
        self.assertEqual(len(self.render_calls), 1)
        self.assertIn("taking off your top",
                      self.render_calls[0]["prompt"])
        rows = self.log_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["project_id"], "proj_test_1")
        self.assertEqual(rows[0]["result_url"],
                         "/generated/hannah-sogni-test.mp4")
        turns = self.her_turns()
        self.assertEqual(len(turns), 1)
        self.assertIn("made it for you", turns[0]["text"])
        import json as _json
        atts = _json.loads(turns[0]["attachments"] or "[]")
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0]["kind"], "video")
        self.assertEqual(atts[0]["url"], "/generated/hannah-sogni-test.mp4")
        # a second "yes" spends nothing — the quote is gone
        hit2 = self.ask("yes")
        self.assertEqual(len(self.render_calls), 1)
        self.assertEqual(len(self.log_rows()), 1)

    def test_no_confirmation_no_spend_on_cancel(self):
        self.quote()
        hit = self.ask("never mind")
        self.assertIsNotNone(hit)
        self.assertIn("nothing spent", hit["brief"])
        self.assertEqual(self.render_calls, [])
        self.assertEqual(self.log_rows(), [])
        self.assertIsNone(
            assist._studio_get(self.conn, "owner_1", "sogni_video_quote"))

    def test_no_confirmation_no_spend_when_he_moves_on(self):
        self.quote()
        self.ask("what's the weather like today?")
        self.assertEqual(self.render_calls, [])
        self.assertEqual(self.log_rows(), [])
        self.assertIsNone(
            assist._studio_get(self.conn, "owner_1", "sogni_video_quote"))

    def test_filter_block_reports_and_spends_nothing(self):
        def blocked(api_key, prompt_text, girl_name, avatar_url):
            raise assist.SogniVideoBlocked("Sensitive content detected")
        assist._sogni_render_video = blocked
        self.quote("Make a video of you taking off your top")
        self.ask("spend it")
        rows = self.log_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "blocked")
        self.assertFalse(rows[0]["actual_spark"])  # zero recorded spent
        turns = self.her_turns()
        self.assertEqual(len(turns), 1)
        self.assertIn("filter blocked", turns[0]["text"])
        self.assertIn("zero Spark was spent", turns[0]["text"])
        self.assertNotIn("strictly here", turns[0]["text"])

    def test_render_failure_is_honest(self):
        def boom(api_key, prompt_text, girl_name, avatar_url):
            raise RuntimeError("socket exploded")
        assist._sogni_render_video = boom
        self.quote("make a video of you waving")
        self.ask("do it")
        rows = self.log_rows()
        self.assertEqual(rows[0]["status"], "failed")
        turns = self.her_turns()
        self.assertEqual(len(turns), 1)
        self.assertIn("didn't land", turns[0]["text"])

    # -- caps / gates ---------------------------------------------------
    def test_daily_cap_blocks_quote(self):
        os.environ["HANNAH_SOGNI_DAILY_CAP"] = "1"
        assist.store_sogni_api_key(self.conn, FAKE_KEY)
        log_id = assist._log_sogni_video(
            self.conn, "owner_1", "hannah", "hannah",
            "an earlier one", "model-x", 23.5)
        assist._settle_sogni_video(self.conn, log_id, "completed",
                                   project_id="p0", actual_spark=23.5,
                                   result_url="/generated/x.mp4")
        hit = self.ask("make a video of you dancing")
        self.assertIsNotNone(hit)
        self.assertIn("today's video limit", hit["brief"])
        self.assertEqual(self.estimate_calls, [])  # cap before quoting
        self.assertIsNone(
            assist._studio_get(self.conn, "owner_1", "sogni_video_quote"))

    def test_non_owner_gets_no_video_lane(self):
        os.environ["ASSIST_ROLLOUT"] = "paid"
        customer = {"user_id": "customer_9", "tier": "plus"}
        hit = self.ask("Create a video of you taking off your top",
                       user=customer, cid="hannah")
        self.assertIsNone(hit)
        self.assertEqual(assist.stored_sogni_api_key(self.conn), "")
        self.assertEqual(self.log_rows(), [])
        self.assertEqual(self.render_calls, [])

    def test_other_character_chat_has_no_video_lane(self):
        assist.store_sogni_api_key(self.conn, FAKE_KEY)
        hit = self.ask("Create a video of you dancing", cid="chloe")
        self.assertIsNone(hit)
        self.assertEqual(self.render_calls, [])
        self.assertEqual(self.log_rows(), [])


if __name__ == "__main__":
    unittest.main()
