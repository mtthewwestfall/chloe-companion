"""Male voice layer (2026-10-09, owner-directed).

Jake and Eli are Whispr's male companions for female users. The chat
brain used to wrap every non-Chloe character in girlfriend-coded
language; these tests pin the male register (short, dry, steady,
personality-shaped), Rocky's separate no-pet-names co-producer
register, and the unchanged female path.

Run:  python -m unittest tests.test_male_voice -v
"""
from __future__ import annotations

import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import brain, characters  # noqa: E402


def _mouth(cid, name, persona, user_text, user_name):
    random.seed(hash((cid, user_text)) % 10000)
    char = {"id": cid, "name": name, "persona": persona}
    return brain.GenericMouth().generate(
        user_text,
        {"character": char, "name": user_name, "recent": []})


def _bank_lines(lines, user_name):
    return [brain._fmt(line, user_name) for line in lines]


class GenderInferenceTest(unittest.TestCase):
    def test_roster_males_and_generic_persona_male(self):
        self.assertEqual(brain.character_gender("jake", {}), "male")
        self.assertEqual(brain.character_gender("eli", {}), "male")
        self.assertEqual(brain.character_gender("rocky", {}), "male")
        self.assertEqual(
            brain.character_gender(
                "etienne",
                {"persona": "You are Etienne, a man. Faithful, loyal, "
                            "humorous, quick-witted."}),
            "male")
        self.assertEqual(characters.character_gender("etienne"), "")
        self.assertEqual(
            characters.character_gender(
                "etienne", "You are Etienne, a man."),
            "male")

    def test_females_stay_female(self):
        for cid in ("chloe", "mara", "hannah", "mia", "jordan"):
            self.assertEqual(brain.character_gender(cid, {}), "female", cid)
            self.assertEqual(characters.character_gender(cid), "female", cid)


class MaleSystemTextTest(unittest.TestCase):
    def _system(self, cid, name, persona, user_name="Alex"):
        return brain._gemini_system_text(
            {"character": {"id": cid, "name": name, "persona": persona},
             "character_name": name, "name": user_name}, cid)

    def test_jake_gets_lumberjack_register(self):
        text = self._system(
            "jake", "Jake",
            "Jake, a rugged older lumberjack in a tan flannel.")
        self.assertIn("MALE VOICE", text)
        self.assertIn("JAKE'S REGISTER", text)
        self.assertIn("woods / workman", text.lower())
        self.assertIn("her boyfriend", text)
        self.assertNotIn("girlfriend-like", text)

    def test_eli_gets_surfer_register(self):
        text = self._system(
            "eli", "Eli",
            "Eli, a blonde surfer with a man bun and a faded red tee.")
        self.assertIn("MALE VOICE", text)
        self.assertIn("ELI'S REGISTER", text)
        self.assertIn("Laid-back pace", text)
        self.assertIn("never in a hurry", text)
        self.assertNotIn("girlfriend-like", text)

    def test_generic_male_gets_common_layer_only(self):
        text = self._system(
            "etienne", "Etienne", "You are Etienne, a man.")
        self.assertIn("MALE VOICE", text)
        self.assertNotIn("JAKE'S REGISTER", text)
        self.assertNotIn("ELI'S REGISTER", text)
        self.assertNotIn("girlfriend-like", text)

    def test_female_system_text_unchanged(self):
        text = self._system("mara", "Mara", "Mara, 24, warm brunette.",
                            user_name="Matty")
        self.assertIn("girlfriend-like", text)
        self.assertNotIn("MALE VOICE", text)

    def test_rocky_bans_pet_names(self):
        text = self._system("rocky", "Rocky", "Rocky is direct.",
                            user_name="Matty")
        self.assertIn("ROCKY'S REGISTER", text)
        self.assertIn("NEVER use pet names", text)
        self.assertIn("no baby, babe, honey", text)
        self.assertNotIn("girlfriend-like", text)


class LocalMouthDriveTest(unittest.TestCase):
    def test_jake_drive_lines_are_from_his_bank(self):
        greeting = _mouth("jake", "Jake", "Jake, a lumberjack.", "hey", "Alex")
        flirty = _mouth("jake", "Jake", "Jake, a lumberjack.",
                        "you're cute", "Alex")
        comfort = _mouth("jake", "Jake", "Jake, a lumberjack.",
                         "i had a rough day", "Alex")
        self.assertIn(greeting, _bank_lines(brain._JAKE_LINES["greet"], "Alex"))
        self.assertIn(flirty, _bank_lines(brain._JAKE_LINES["compliment"], "Alex"))
        self.assertIn(comfort, _bank_lines(brain._JAKE_LINES["tired"], "Alex"))
        for line in (greeting, flirty, comfort):
            self.assertNotIn(line, _bank_lines(brain._GENERIC_LINES["fallback"], "Alex"))

    def test_eli_drive_lines_are_from_his_bank(self):
        greeting = _mouth("eli", "Eli", "Eli, a surfer.", "hey", "Alex")
        flirty = _mouth("eli", "Eli", "Eli, a surfer.",
                        "you're cute", "Alex")
        comfort = _mouth("eli", "Eli", "Eli, a surfer.",
                         "i had a rough day", "Alex")
        self.assertIn(greeting, _bank_lines(brain._ELI_LINES["greet"], "Alex"))
        self.assertIn(flirty, _bank_lines(brain._ELI_LINES["compliment"], "Alex"))
        self.assertIn(comfort, _bank_lines(brain._ELI_LINES["tired"], "Alex"))

    def test_female_drive_unchanged(self):
        greeting = _mouth("mara", "Mara", "Mara, warm brunette.", "hey", "Matty")
        self.assertIn(greeting, _bank_lines(brain._GENERIC_LINES["greet"], "Matty"))

    def test_rocky_local_lines_have_no_pet_names(self):
        for user_text in ("hey", "thanks", "i had a rough day",
                          "good morning", "what are you doing"):
            line = _mouth("rocky", "Rocky", "Rocky is direct.",
                          user_text, "Matty")
            self.assertFalse(brain._rocky_pet_name_hit(line), line)


class RockyPetNameGuardTest(unittest.TestCase):
    def test_sanitizer_strips_vocatives(self):
        out = brain._sanitize_rocky_reply(
            "Done, babe. The build is live, baby.")
        self.assertEqual(out, "Done. The build is live.")
        self.assertFalse(brain._rocky_pet_name_hit(out))

    def test_ordinary_words_are_not_addresses(self):
        self.assertFalse(brain._rocky_pet_name_hit(
            "The honey is on the grocery list."))
        self.assertFalse(brain._rocky_pet_name_hit(
            "Dearborn, MI shows 3 leads."))

    def test_compose_reply_sanitizes_a_model_draft(self):
        with mock.patch.object(brain, "gemini_generate",
                               return_value="Done, babe — it's live."):
            out = brain.compose_reply(
                "is it live?", {"character": {"id": "rocky"}}, "rocky")
        self.assertEqual(out, "Done, it's live.")
        self.assertFalse(brain._rocky_pet_name_hit(out))


if __name__ == "__main__":
    unittest.main()
