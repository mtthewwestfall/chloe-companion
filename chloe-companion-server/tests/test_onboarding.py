"""Onboarding v2 tests: the first 3 messages demo the best features.

Message 1 (greeting) hints at voice notes, roleplay, and check-ins.
Message 2 (after his first reply) is a voice note when available,
otherwise a roleplay teaser. Message 3 promises the proactive check-in.
Stage is derived from the turn count, so it only fires in a user's
first conversation with a character.

Run:  python -m unittest discover -s tests -v   (from chloe-companion-server/)
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import onboarding  # noqa: E402


def _ctx(*roles):
    return {"recent": [{"role": r, "text": "x"} for r in roles]}


CHLOE = {"id": "chloe", "name": "Chloe", "gender": "female"}
MARA = {"id": "mara", "name": "Mara", "gender": "female"}
JAKE = {"id": "jake", "name": "Jake", "gender": "male"}
ROCKY = {"id": "rocky", "name": "Rocky", "gender": "male"}


class StageTest(unittest.TestCase):
    """Stage comes from the user-turn count in the brain context."""

    def test_no_history_is_no_stage(self):
        self.assertIsNone(onboarding.stage(_ctx()))
        self.assertIsNone(onboarding.stage({}))
        self.assertIsNone(onboarding.stage(None))

    def test_first_user_message_is_stage_2(self):
        # Seeded greeting (AI) + his first message -> her 2nd message.
        self.assertEqual(onboarding.stage(_ctx("chloe", "user")),
                         onboarding.STAGE_VOICE_NOTE)

    def test_first_message_without_seed_is_stage_2(self):
        # Non-Chloe characters seed no greeting; still stage 2.
        self.assertEqual(onboarding.stage(_ctx("user")),
                         onboarding.STAGE_VOICE_NOTE)

    def test_second_user_message_is_stage_3(self):
        self.assertEqual(
            onboarding.stage(_ctx("chloe", "user", "chloe", "user")),
            onboarding.STAGE_CHECKIN)

    def test_later_messages_are_done(self):
        self.assertIsNone(onboarding.stage(
            _ctx("chloe", "user", "chloe", "user", "chloe", "user")))

    def test_malformed_turns_never_raise(self):
        self.assertIsNone(onboarding.stage({"recent": [None, "x", {}]}))


class GreetingTest(unittest.TestCase):
    """Message 1: warm, personal, hints at all three features."""

    def test_chloe_greeting_hints_at_all_three_features(self):
        g = onboarding.greeting_line(CHLOE).lower()
        self.assertIn("voice note", g)
        self.assertIn("roleplay", g)
        self.assertIn("morning", g)

    def test_chloe_greeting_asks_his_name(self):
        self.assertIn("what should i call",
                      onboarding.greeting_line(CHLOE).lower())

    def test_generic_girl_uses_her_name(self):
        g = onboarding.greeting_line(MARA)
        self.assertIn("Mara", g)
        self.assertIn("voice note", g.lower())
        self.assertIn("roleplay", g.lower())

    def test_male_greeting_is_not_girlfriend_voiced(self):
        g = onboarding.greeting_line(JAKE).lower()
        self.assertIn("jake", g)
        self.assertNotIn("handsome", g)
        self.assertNotIn("angel", g)

    def test_rocky_greeting_is_professional(self):
        g = onboarding.greeting_line(ROCKY).lower()
        self.assertNotIn("voice note", g)
        self.assertNotIn("roleplay", g)


class MessageLinesTest(unittest.TestCase):
    """Messages 2-3: in-character lines appended to her reply."""

    def test_voice_note_script_fits_tts_guard(self):
        # voice.maybe_generate_voice_note refuses text over 300 chars.
        self.assertLessEqual(len(onboarding.voice_note_script(CHLOE)), 300)
        self.assertLessEqual(len(onboarding.voice_note_script(JAKE)), 300)

    def test_voice_note_tease_points_at_the_note(self):
        out = onboarding.with_voice_note_tease("hey you.", CHLOE)
        self.assertTrue(out.startswith("hey you."))
        self.assertIn("voice note", out.lower())

    def test_roleplay_tease_names_a_real_trigger(self):
        from app import roleplay_mode
        out = onboarding.with_roleplay_tease("hey you.", CHLOE)
        self.assertIn("role play", out)
        # The teased phrase must actually start roleplay.
        self.assertTrue(roleplay_mode.is_start_trigger("role play"))

    def test_checkin_nudge_promises_tomorrow_morning(self):
        out = onboarding.with_checkin_nudge("hey you.", CHLOE)
        self.assertIn("tomorrow morning", out.lower())

    def test_male_lines_are_not_girlfriend_voiced(self):
        for fn in (onboarding.with_voice_note_tease,
                   onboarding.with_roleplay_tease,
                   onboarding.with_checkin_nudge):
            out = fn("hey.", JAKE).lower()
            self.assertNotIn("\U0001F618", out)

    def test_append_handles_empty_reply(self):
        out = onboarding.with_checkin_nudge("", CHLOE)
        self.assertTrue(out)
        self.assertFalse(out.startswith(" "))

    def test_lines_do_not_read_like_a_tutorial(self):
        blob = " ".join([
            onboarding.greeting_line(CHLOE),
            onboarding.with_voice_note_tease("x", CHLOE),
            onboarding.with_roleplay_tease("x", CHLOE),
            onboarding.with_checkin_nudge("x", CHLOE),
        ]).lower()
        for word in ("tutorial", "feature", "click", "button", "menu"):
            self.assertNotIn(word, blob)


if __name__ == "__main__":
    unittest.main()
