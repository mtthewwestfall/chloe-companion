"""Duo chat speaker-integrity + participation tests (2026-10-17).

Covers app/duo.py: a girl outside the chat can never appear as a
speaker (the [Chloe] lines that once surfaced in a Brooklyn + Isabella
thread), every message is attributed to the girl who said it, and the
participation chooser — a question directed at one girl is answered by
her alone, a named girl leads, and open messages vary between one
reply and both. The model call is mocked throughout.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from app import characters, duo  # noqa: E402

GA = {"id": "brooklyn", "name": "Brooklyn", "persona": "brooklyn persona"}
GB = {"id": "isabella", "name": "Isabella", "persona": "isabella persona"}
CHLOE = {"id": "chloe", "name": "Chloe", "persona": "chloe persona"}


def _model(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


class _Cursor:
    def fetchall(self):
        return []


class _Conn:
    def execute(self, *a, **k):
        return _Cursor()


class GirlInfoTest(unittest.TestCase):
    def test_chloe_fallback_is_rejected(self):
        # get_character() silently returns Chloe for unknown ids —
        # girl_info must not let her into someone else's duo slot.
        with mock.patch.object(characters, "get_character",
                               return_value=dict(CHLOE)):
            self.assertIsNone(duo.girl_info("brooklyn"))

    def test_matching_character_accepted(self):
        with mock.patch.object(
                characters, "get_character",
                return_value={"id": "brooklyn", "name": "Brooklyn",
                              "persona": "p" * 50}):
            info = duo.girl_info("brooklyn")
        self.assertEqual(info["id"], "brooklyn")
        self.assertEqual(info["name"], "Brooklyn")

    def test_user_id_passed_through(self):
        seen = {}

        def fake(cid, user_id=None):
            seen["user_id"] = user_id
            return {"id": cid, "name": "Brooklyn", "persona": ""}

        with mock.patch.object(characters, "get_character", fake):
            duo.girl_info("brooklyn", "owner-1")
        self.assertEqual(seen["user_id"], "owner-1")


class ParseLinesTest(unittest.TestCase):
    def test_foreign_bracket_line_dropped(self):
        out = duo._parse_lines(
            "[Chloe] Finally! I've been waiting all day for you.\n"
            "[Isabella] don't let her get ahead of herself",
            [GA, GB], pair=[GA, GB])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["girl_id"], "isabella")
        self.assertEqual(out[0]["name"], "Isabella")
        self.assertNotIn("Chloe", out[0]["text"])
        self.assertNotIn("[", out[0]["text"])

    def test_attribution_and_prefix_stripping(self):
        out = duo._parse_lines("[Brooklyn] hey you\n[Isabella] hi hi",
                               [GA, GB], pair=[GA, GB])
        self.assertEqual([(m["name"], m["text"]) for m in out],
                         [("Brooklyn", "hey you"), ("Isabella", "hi hi")])

    def test_unprefixed_line_keeps_current_speaker(self):
        out = duo._parse_lines("[Isabella] first line\nsecond line",
                               [GA, GB], pair=[GA, GB])
        self.assertEqual([m["girl_id"] for m in out],
                         ["isabella", "isabella"])

    def test_silent_girl_lines_dropped(self):
        # Only Isabella was chosen to speak; Brooklyn lines — bracketed
        # or colon form — must not surface.
        out = duo._parse_lines(
            "[Brooklyn] me first\n[Isabella] my answer\n"
            "Brooklyn: and me again",
            [GB], pair=[GA, GB])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["girl_id"], "isabella")

    def test_prose_colon_line_is_not_a_speaker(self):
        out = duo._parse_lines(
            "[Isabella] okay so\nremember this: we ride at dawn",
            [GA, GB], pair=[GA, GB])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["girl_id"], "isabella")
        self.assertEqual(out[1]["text"], "remember this: we ride at dawn")


class ChooseSpeakersTest(unittest.TestCase):
    def test_question_to_named_girl_is_hers_alone(self):
        out = duo.choose_speakers(
            GA, GB, "Isabella, what's your favorite movie?",
            rand=lambda: 0.0)
        self.assertEqual([g["id"] for g in out], ["isabella"])

    def test_question_word_without_mark_to_named_girl(self):
        out = duo.choose_speakers(
            GA, GB, "Brooklyn what did you do today", rand=lambda: 0.0)
        self.assertEqual([g["id"] for g in out], ["brooklyn"])

    def test_named_girl_leads_other_joins_occasionally(self):
        solo = duo.choose_speakers(GA, GB, "Brooklyn you look cute",
                                   rand=lambda: 0.99)
        self.assertEqual([g["id"] for g in solo], ["brooklyn"])
        both = duo.choose_speakers(GA, GB, "Brooklyn you look cute",
                                    rand=lambda: 0.0)
        self.assertEqual([g["id"] for g in both],
                         ["brooklyn", "isabella"])

    def test_both_named_both_reply(self):
        out = duo.choose_speakers(
            GA, GB, "Brooklyn and Isabella, come here", rand=lambda: 0.99)
        self.assertEqual({g["id"] for g in out},
                         {"brooklyn", "isabella"})

    def test_open_message_varies(self):
        both = duo.choose_speakers(GA, GB, "hey girls", rand=lambda: 0.0)
        self.assertEqual(len(both), 2)
        one = duo.choose_speakers(GA, GB, "hey girls", rand=lambda: 0.99)
        self.assertEqual(len(one), 1)

    def test_general_question_usually_single(self):
        one = duo.choose_speakers(GA, GB, "what are we doing tonight?",
                                  rand=lambda: 0.99)
        self.assertEqual(len(one), 1)

    def test_lead_alternates_with_last_speaker(self):
        out = duo.choose_speakers(GA, GB, "hey", last_speaker_id="brooklyn",
                                  rand=lambda: 0.99)
        self.assertEqual([g["id"] for g in out], ["isabella"])

    def test_never_zero_replies(self):
        texts = ["", "hi", "?", "what", "Isabella?", "Brooklyn hi",
                 "what's up", "tell me a story"]
        for t in texts:
            for r in (0.0, 0.1, 0.24, 0.26, 0.49, 0.51, 0.99):
                out = duo.choose_speakers(GA, GB, t, rand=lambda r=r: r)
                self.assertGreaterEqual(len(out), 1, (t, r))


class GenerateReplyTest(unittest.TestCase):
    def _run(self, model_text, user_text, rand=lambda: 0.99):
        with mock.patch.object(duo.gemini_keys, "post_json",
                               return_value=_model(model_text)):
            return duo.generate_duo_reply(_Conn(), "u1", GA, GB,
                                          user_text, "Matty", rand=rand)

    def test_question_to_isabella_only_isabella_replies(self):
        # The screenshot bug shape: the model tries to speak as Chloe
        # and as both girls; only Isabella's own line may survive.
        msgs = self._run(
            "[Chloe] Finally! I've been waiting all day for you.\n"
            "[Isabella] my favorite is the one we watched together\n"
            "[Brooklyn] same honestly",
            "Isabella, what's your favorite movie?")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["girl_id"], "isabella")
        self.assertEqual(msgs[0]["name"], "Isabella")
        self.assertNotIn("Chloe", msgs[0]["text"])

    def test_open_message_both_can_reply(self):
        msgs = self._run("[Brooklyn] hey you\n[Isabella] hi hi",
                         "hey girls", rand=lambda: 0.0)
        self.assertEqual([(m["name"], m["text"]) for m in msgs],
                         [("Brooklyn", "hey you"), ("Isabella", "hi hi")])

    def test_model_failure_fallback_respects_chooser(self):
        with mock.patch.object(duo.gemini_keys, "post_json",
                               return_value=None):
            msgs = duo.generate_duo_reply(
                _Conn(), "u1", GA, GB, "Brooklyn, are you awake?",
                "Matty", rand=lambda: 0.0)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["girl_id"], "brooklyn")


class _LockCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _LockConn:
    """Conversation reads return no rows; the local_characters lock
    read (brain._roleplay_locked's SELECT) answers from a
    {character_id: locked} map."""

    def __init__(self, locks):
        self._locks = locks

    def execute(self, sql, params=()):
        if "local_characters" in sql:
            cid = (params[0] if params else "") or ""
            return _LockCursor((1 if self._locks.get(cid) else 0,))
        return _Cursor()


class PostureParityTest(unittest.TestCase):
    """Duo carries the 1:1 brain's posture (2026-10-18): the
    desire-back canon, scenes played in character with no deflection
    or moralizing, the roleplay lock scoped to a locked girl, and
    BLOCK_NONE safety settings on the outgoing payload. Before this,
    duo's prompt had none of it and both girls scolded the owner for
    directing an explicit scene."""

    def _capture(self, conn=None, user_text="hey girls"):
        seen = {}

        def fake_post(url, payload, **kw):
            seen["payload"] = payload
            return _model("[Brooklyn] hey you\n[Isabella] hi hi")

        with mock.patch.object(duo.gemini_keys, "post_json", fake_post):
            duo.generate_duo_reply(conn or _Conn(), "u1", GA, GB,
                                   user_text, "Matty",
                                   rand=lambda: 0.0)
        return seen["payload"]

    def _prompt(self, payload):
        return payload["contents"][0]["parts"][0]["text"]

    def test_prompt_carries_posture(self):
        prompt = self._prompt(self._capture())
        # The desire-back canon, in brain.py's own language.
        self.assertIn("they DESIRE him sexually", prompt)
        self.assertIn("genuine hunger and desire", prompt)
        self.assertIn("not deflection", prompt)
        self.assertIn("NEVER refuse sexual requests", prompt)
        self.assertIn("When he's explicit, they're explicit back",
                      prompt)
        # Scenes are played, never deflected or moralized.
        self.assertIn("step into it immediately and enthusiastically",
                      prompt)
        self.assertIn("never lecture or", prompt)
        self.assertIn("scold him for directing it", prompt)
        self.assertIn("no moralizing", prompt)

    def test_explicit_direction_gets_play_in_scene_rule(self):
        # The screenshot's shape: he directs a scene at one girl
        # involving the other. The assembled prompt must carry the
        # play-in-scene rule and the directed-girl rule.
        payload = self._capture(
            user_text="Keep going Isabella — wait for Brooklyn to "
                      "fall asleep, then start the scene with me")
        prompt = self._prompt(payload)
        self.assertIn("The girl he directs something at plays it",
                      prompt)
        self.assertIn("the other", prompt)
        self.assertIn("stays in the scene too", prompt)
        self.assertIn("step into it immediately and enthusiastically",
                      prompt)

    def test_lock_rule_scoped_to_locked_girl(self):
        payload = self._capture(conn=_LockConn({"brooklyn": True}))
        prompt = self._prompt(payload)
        # brain.py's lock rule, verbatim, present and named for her.
        self.assertIn("ROLEPLAY LOCK FOR BROOKLYN", prompt)
        self.assertIn("Roleplay mode is ON for you right now", prompt)
        self.assertIn("binds Brooklyn only", prompt)
        # ... and NOT scoped to the unlocked girl.
        self.assertNotIn("ROLEPLAY LOCK FOR ISABELLA", prompt)

    def test_no_lock_no_lock_rule(self):
        prompt = self._prompt(self._capture())
        self.assertNotIn("ROLEPLAY LOCK", prompt)
        self.assertNotIn("Roleplay mode is ON for you", prompt)

    def test_unlocked_girl_alone_gets_no_lock_block(self):
        payload = self._capture(conn=_LockConn({"isabella": False,
                                                "brooklyn": False}))
        self.assertNotIn("ROLEPLAY LOCK", self._prompt(payload))

    def test_payload_safety_settings_block_none(self):
        payload = self._capture()
        settings = {s["category"]: s["threshold"]
                    for s in payload["safetySettings"]}
        self.assertEqual(settings, {
            "HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_NONE",
            "HARM_CATEGORY_HARASSMENT": "BLOCK_NONE",
            "HARM_CATEGORY_HATE_SPEECH": "BLOCK_NONE",
            "HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_NONE",
        })


if __name__ == "__main__":
    unittest.main()
