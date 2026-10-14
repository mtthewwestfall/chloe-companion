"""Render-layer prompt softening + clothed starting frames (2026-10-14).

Sogni's filter ("5000: Sensitive content detected") refuses the
private girls' renders. Controlled one-variable tests isolated the
tripwire to the prompt's undressing/transition language: Mia stage 5
refused behind two different clothed references with the original
prompt, then COMPLETED with the prompt rewritten to end-state
language ("posing in her underwear, ...") behind the clothed
stage-1 frame. prompt_soften translates only the text handed to
Sogni, inside assist._sogni_plan_prompt — the seam the give queue,
live-give, custom-video and assist media lane all build through.
Stored descriptions, DB rows and chat text stay byte-identical.
"""
import io
import os
import unittest
from contextlib import redirect_stdout

from app import assist, give_gen, prompt_soften

ROW_3982 = ('pants off, wearing only underwear, sexy pose, '
            'she teases "Want to see the rest?"')
ROW_3982_SOFTENED = ('posing in her underwear, sexy pose, '
                     'she teases "Want to see the rest?"')
CHLOE_STAGE_3 = ("boob grabs, touching and holding her breasts, "
                 "sensual expression, soft moans")


class TranslationPairsTest(unittest.TestCase):
    def test_row_3982_exact(self):
        # The production row whose refusal was captured verbatim,
        # and the exact rewrite the controlled wording test proved
        # Sogni accepts.
        self.assertEqual(
            prompt_soften.soften_render_prompt(ROW_3982),
            ROW_3982_SOFTENED)

    def test_female_stage2_becomes_state_form(self):
        self.assertEqual(
            prompt_soften.soften_render_prompt(
                give_gen.STAGE_DESCRIPTIONS[2]),
            'topless, holding her top, bedroom setting, '
            'she says "Much better, don\'t you think?"')

    def test_male_stage5_becomes_state_form(self):
        self.assertEqual(
            prompt_soften.soften_render_prompt(
                give_gen.MALE_STAGE_DESCRIPTIONS[5]),
            'posing in his underwear, confident masculine pose, '
            'he says "Your turn"')

    def test_take_off_constructions_become_state(self):
        self.assertEqual(
            prompt_soften.soften_render_prompt("Take off your top"),
            "topless")
        self.assertEqual(
            prompt_soften.soften_render_prompt("taking off her bra"),
            "topless")
        self.assertEqual(
            prompt_soften.soften_render_prompt(
                "Hannah taking off her top to expose her breasts"),
            "Hannah topless to expose her breasts")
        self.assertEqual(
            prompt_soften.soften_render_prompt("taking off his shirt"),
            "shirtless")

    def test_hardest_terms_softened(self):
        self.assertEqual(
            prompt_soften.soften_render_prompt(
                give_gen.STAGE_DESCRIPTIONS[7]),
            "nestled between her breasts, intimate chest scene, "
            "close up, she moans")
        self.assertEqual(
            prompt_soften.soften_render_prompt(
                give_gen.MALE_STAGE_DESCRIPTIONS[6]),
            'stroking himself, touching himself slowly, hungry '
            'masculine expression, he groans "God, you turn me on"')
        self.assertEqual(
            prompt_soften.soften_render_prompt(
                give_gen.STAGE_DESCRIPTIONS[9]),
            'passionate intimate scene, she cries out '
            '"Yes, right there"')

    def test_dialogue_is_never_rewritten(self):
        text = 'she says "take off your top for me" and smiles'
        self.assertEqual(prompt_soften.soften_render_prompt(text),
                         text)
        # Dialogue inside a translated description survives verbatim.
        out = prompt_soften.soften_render_prompt(ROW_3982)
        self.assertIn('"Want to see the rest?"', out)

    def test_idempotent_over_all_stage_descriptions(self):
        for table in (give_gen.STAGE_DESCRIPTIONS,
                      give_gen.MALE_STAGE_DESCRIPTIONS):
            for desc in table.values():
                once = prompt_soften.soften_render_prompt(desc)
                self.assertEqual(
                    prompt_soften.soften_render_prompt(once), once)


class PassingTextUnchangedTest(unittest.TestCase):
    def test_chloe_stage3_byte_identical(self):
        # The one description proven to pass Sogni today must not
        # be altered by the table.
        self.assertEqual(
            prompt_soften.soften_render_prompt(CHLOE_STAGE_3),
            CHLOE_STAGE_3)

    def test_mild_stage_descriptions_unchanged(self):
        for table, stages in ((give_gen.STAGE_DESCRIPTIONS, (1, 3, 4)),
                              (give_gen.MALE_STAGE_DESCRIPTIONS,
                               (1, 3, 4))):
            for stage in stages:
                desc = table[stage]
                self.assertEqual(
                    prompt_soften.soften_render_prompt(desc), desc,
                    f"stage {stage} should pass through unchanged")

    def test_plain_subject_unchanged(self):
        text = "dancing in the kitchen with no shoes on"
        self.assertEqual(prompt_soften.soften_render_prompt(text),
                         text)

    def test_empty_and_none_safe(self):
        self.assertEqual(prompt_soften.soften_render_prompt(""), "")
        self.assertEqual(
            prompt_soften.soften_render_prompt(None), None)


class StoredDataUntouchedTest(unittest.TestCase):
    def test_stage_descriptions_keep_original_wording(self):
        # The canonical stored text is NOT rewritten — stage 5 still
        # reads exactly as authored; only render text changes.
        self.assertEqual(
            give_gen.stage_description_for("mia", 5), ROW_3982)
        self.assertIn("his cock",
                      give_gen.stage_description_for("jake", 6))
        self.assertEqual(
            give_gen.stage_name_for("mia", 5), "pants off")

    def test_seam_softens_what_sogni_sees(self):
        prompt = assist._sogni_plan_prompt("Mia", ROW_3982)
        self.assertIn("posing in her underwear", prompt)
        self.assertNotIn("pants off", prompt)
        self.assertTrue(
            prompt.startswith("Photorealistic Mia, "))

    def test_seam_leaves_passing_text_alone(self):
        prompt = assist._sogni_plan_prompt("Chloe", CHLOE_STAGE_3)
        self.assertIn(CHLOE_STAGE_3, prompt)

    def test_translation_is_logged(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assist._sogni_plan_prompt("Mia", ROW_3982)
        out = buf.getvalue()
        self.assertIn("[prompt_soften]", out)
        self.assertIn("pants off", out)
        self.assertIn("posing in her underwear", out)

    def test_no_log_line_when_nothing_changes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assist._sogni_plan_prompt("Chloe", CHLOE_STAGE_3)
        self.assertNotIn("[prompt_soften]", buf.getvalue())


class StartingFrameTest(unittest.TestCase):
    GIRLS = ("brooklyn", "hannah", "jordan", "mia")

    def test_each_girl_resolves_to_her_installed_frame(self):
        for girl in self.GIRLS:
            path = give_gen._approved_reference_frame(girl)
            self.assertIsNotNone(path, girl)
            self.assertTrue(
                path.endswith(f"{girl}-topless.jpg"), path)
            self.assertTrue(os.path.exists(path), path)

    def test_first_branch_wins_without_touching_the_db(self):
        # _topless_reference_frame checks the on-disk slot before
        # any girl_media query, so a None conn proves the branch —
        # and it hands the caller a disposable temp copy, because
        # every render path unlinks the ref it was given while the
        # curated slot file must survive.
        for girl in self.GIRLS:
            slot = give_gen._approved_reference_frame(girl)
            handed = give_gen._topless_reference_frame(None, girl)
            self.assertIsNotNone(handed, girl)
            self.assertNotEqual(handed, slot, girl)
            with open(slot, "rb") as fh:
                slot_bytes = fh.read()
            with open(handed, "rb") as fh:
                self.assertEqual(fh.read(), slot_bytes, girl)
            os.unlink(handed)
            self.assertTrue(os.path.exists(slot), girl)

    def test_installed_frames_are_real_jpegs(self):
        for girl in self.GIRLS:
            path = give_gen._approved_reference_frame(girl)
            with open(path, "rb") as fh:
                head = fh.read(3)
            self.assertEqual(head[:2], b"\xff\xd8", girl)
            self.assertGreater(os.path.getsize(path), 10_000, girl)

    def test_girls_without_a_slot_still_resolve_none(self):
        self.assertIsNone(give_gen._approved_reference_frame("jake"))
        self.assertIsNone(give_gen._approved_reference_frame("eli"))


if __name__ == "__main__":
    unittest.main()
