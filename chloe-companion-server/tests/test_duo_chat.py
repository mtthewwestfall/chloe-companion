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


if __name__ == "__main__":
    unittest.main()
