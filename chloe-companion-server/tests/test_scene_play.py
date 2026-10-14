"""Scene play: narration rule, canned-pool fencing, and the roleplay
start flow (2026-10-14).

Three layers under test:
1. SCENE_NARRATION_RULE rides in every companion character's Gemini
   system prompt — in a scene she describes and carries the narration
   herself instead of bouncing it back.
2. compose_reply fencing: mid-scene, the reply path retries the model
   once on failure, the canned listener pool never answers a scene
   turn, and a prompt-back reply from ANY source is replaced with an
   in-scene descriptive line. Ordinary chat is untouched.
3. The start flow (owner accounts only, per character): "role play"
   -> she asks for the casting; his next message is the brief, stored
   verbatim and injected (casting outranks default persona facts
   in-scene); "pickles" ends that character's scene.

Run:  python -m pytest tests/test_scene_play.py   (from the project
      root, with the venv's python)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import brain, db, store  # noqa: E402

OWNER = "owner_user"
OTHER = "regular_user"

BROOKLYN = {
    "id": "brooklyn",
    "name": "Brooklyn",
    "persona": ("Brooklyn is his new neighbor. She does not know him yet "
                "and does not know his name."),
    "owner_user_id": OWNER,
}
MIA = {"id": "mia", "name": "Mia", "persona": "Mia is his girlfriend."}
HANNAH = {"id": "hannah", "name": "Hannah", "persona": "Hannah."}
JAKE = {"id": "jake", "name": "Jake", "persona": "Jake is a lumberjack."}
ROCKY = {"id": "rocky", "name": "Rocky", "persona": "Rocky is his agent."}

BRIEF = ("You are Vivienne, a lounge singer in 1940s Paris who has "
         "been watching me for weeks. I am Daniel, the piano player "
         "who never talks. It starts at the bar after closing.")

SCREENSHOT_LINES = [
    "i like hearing you talk.",
    "and then what happened?",
    "go on — don't leave me hanging.",
]


def _hand_ctx(char, **kw):
    ctx = {
        "name": "Matty",
        "character": char,
        "character_name": char.get("name"),
        "recent": [],
    }
    ctx.update(kw)
    return ctx


class SceneNarrationRuleTest(unittest.TestCase):
    def test_rule_present_for_companion_characters(self):
        for char in (BROOKLYN, MIA, JAKE):
            ctx = _hand_ctx(char)
            text = brain._gemini_system_text(ctx, char["id"])
            self.assertIn(brain.SCENE_NARRATION_RULE, text, char["id"])
            self.assertIn("Never bounce the narration back", text)

    def test_rule_absent_for_rocky(self):
        ctx = _hand_ctx(ROCKY)
        text = brain._gemini_system_text(ctx, "rocky")
        self.assertNotIn(brain.SCENE_NARRATION_RULE, text)


class PromptBackTest(unittest.TestCase):
    def test_screenshot_lines_are_prompt_backs(self):
        for line in SCREENSHOT_LINES:
            self.assertTrue(brain._is_prompt_back(line), line)

    def test_bank_fallback_lines_are_prompt_backs(self):
        for line in brain._GENERIC_LINES["fallback"]:
            self.assertTrue(brain._is_prompt_back(line), line)

    def test_long_descriptive_reply_is_not_a_prompt_back(self):
        reply = ("i slide in close behind you, my hands finding your "
                 "hips, my mouth at your ear while i tell you exactly "
                 "what i'm doing to you — every slow inch of it, what "
                 "happened in the bar forgotten completely, until "
                 "there's nothing in the world but this and us.")
        self.assertGreater(len(reply), 120)
        self.assertFalse(brain._is_prompt_back(reply))

    def test_short_ordinary_line_is_not_a_prompt_back(self):
        self.assertFalse(brain._is_prompt_back("that's amazing, tell me "
                                               "about the game saturday."))
        self.assertFalse(brain._is_prompt_back("sleep well, i'll be here."))


class SceneActiveTest(unittest.TestCase):
    def test_lock_means_scene(self):
        self.assertTrue(brain._scene_active("hey", {"roleplay_locked": True}))

    def test_stored_state_means_scene(self):
        self.assertTrue(brain._scene_active("hey", {"scene_state": "active"}))
        self.assertTrue(brain._scene_active("hey", {"scene_state": "awaiting"}))

    def test_describe_ask_means_scene(self):
        self.assertTrue(brain._scene_active("describe it", {}))
        self.assertTrue(brain._scene_active("you describe it", {}))

    def test_previous_turn_scene_language_means_scene(self):
        ctx = {"recent": [{"role": "user", "text": "sit on it and ride me"},
                          {"role": "model", "text": "mm..."}]}
        self.assertTrue(brain._scene_active("yeah", ctx))

    def test_ordinary_chat_is_not_a_scene(self):
        self.assertFalse(brain._scene_active("how was your day", {}))
        ctx = {"recent": [{"role": "user", "text": "work was brutal today"}]}
        self.assertFalse(brain._scene_active("yeah, long one", ctx))


class ComposeFencingTest(unittest.TestCase):
    def _compose(self, user_text, ctx, cid, model):
        with mock.patch.object(brain, "gemini_generate", model):
            return brain.compose_reply(user_text, ctx, cid)

    def test_model_prompt_back_replaced_in_active_scene(self):
        ctx = _hand_ctx(MIA, scene_state="active", scene_brief=BRIEF)
        out = self._compose("describe it", ctx, "mia",
                            lambda *a: "and then what happened?")
        self.assertIn(out, brain._SCENE_FALLBACK_LINES)

    def test_screenshot_lines_fenced_under_roleplay_lock(self):
        for line in SCREENSHOT_LINES:
            ctx = _hand_ctx(BROOKLYN, roleplay_locked=True)
            out = self._compose("you describe it", ctx, "brooklyn",
                                lambda *a, line=line: line)
            self.assertIn(out, brain._SCENE_FALLBACK_LINES, line)

    def test_descriptive_model_reply_passes_through_in_scene(self):
        good = ("i'm right there with you, my dress slipping off my "
                "shoulder while i pull you closer and tell you exactly "
                "how it feels to finally have your hands on me.")
        ctx = _hand_ctx(MIA, scene_state="active", scene_brief=BRIEF)
        out = self._compose("describe it", ctx, "mia", lambda *a: good)
        self.assertEqual(out, good)

    def test_model_failure_retries_once_then_fences_pool(self):
        calls = {"n": 0}

        def dead(*a):
            calls["n"] += 1
            return None

        ctx = _hand_ctx(MIA, scene_state="active", scene_brief=BRIEF)
        out = self._compose("describe it", ctx, "mia", dead)
        self.assertEqual(calls["n"], 2)
        # Whatever the local engine picked, the fence guarantees an
        # in-scene line — the listener pool never answers a scene turn.
        self.assertIn(out, brain._SCENE_FALLBACK_LINES)

    def test_model_failure_no_scene_single_attempt_pool_answers(self):
        calls = {"n": 0}

        def dead(*a):
            calls["n"] += 1
            return None

        ctx = _hand_ctx(MIA)
        out = self._compose("how was your day", ctx, "mia", dead)
        self.assertEqual(calls["n"], 1)
        self.assertNotIn(out, brain._SCENE_FALLBACK_LINES)
        self.assertTrue(out)

    def test_prompt_back_allowed_in_ordinary_chat(self):
        ctx = _hand_ctx(MIA)
        out = self._compose("i went to the store today", ctx, "mia",
                            lambda *a: "and then what happened?")
        self.assertEqual(out, "and then what happened?")


class StartFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)
        self.user = store.get_or_create_user(self.conn, OWNER, "Matty")
        self.other = store.get_or_create_user(self.conn, OTHER, "Sam")
        store.upsert_local_character(self.conn, dict(BROOKLYN))
        store.upsert_local_character(self.conn, dict(MIA))
        store.upsert_local_character(self.conn, dict(HANNAH))
        self._env = mock.patch.dict(
            os.environ, {"ASSIST_OWNER_IDS": OWNER})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.conn.close()
        self.tmp.cleanup()

    def _owner_ctx(self, text, char):
        return brain.build_brain_context(
            self.conn, OWNER, self.user, text, char)

    def test_trigger_matcher(self):
        for msg in ("role play", "Role Play", "ROLE-PLAY", "roleplay",
                    "  role play!  ", "role play."):
            self.assertTrue(brain._is_roleplay_trigger(msg), msg)
        for msg in ("let's role play tonight", "role playing is fun",
                    "i love role play", "", None):
            self.assertFalse(brain._is_roleplay_trigger(msg), msg)

    def test_trigger_enters_awaiting_and_prompt_asks_for_casting(self):
        ctx = self._owner_ctx("role play", BROOKLYN)
        self.assertEqual(ctx["scene_state"], "awaiting")
        row = brain.get_scene_state(self.conn, OWNER, "brooklyn")
        self.assertEqual(row["state"], "awaiting")
        text = brain._gemini_system_text(ctx, "brooklyn")
        self.assertIn(brain.SCENE_ASK_RULE, text)
        self.assertIn("casting", text)

    def test_trigger_turn_never_answered_by_canned_pool(self):
        ctx = self._owner_ctx("role play", BROOKLYN)
        calls = {"n": 0}

        def dead(*a):
            calls["n"] += 1
            return None

        with mock.patch.object(brain, "gemini_generate", dead):
            out = brain.compose_reply("role play", ctx, "brooklyn")
        self.assertEqual(calls["n"], 2)  # one retry before any fallback
        self.assertEqual(out, brain._SCENE_ASK_LINE)
        # A live model ask, on the other hand, is used as-is.
        ask = "ooh, okay — who am i in this one, and who are you?"
        with mock.patch.object(brain, "gemini_generate", lambda *a: ask):
            out2 = brain.compose_reply("role play", ctx, "brooklyn")
        self.assertEqual(out2, ask)

    def test_brief_starts_scene_stored_verbatim_and_opens(self):
        self._owner_ctx("role play", BROOKLYN)
        ctx = self._owner_ctx(BRIEF, BROOKLYN)
        self.assertEqual(ctx["scene_state"], "active")
        self.assertTrue(ctx["scene_opening"])
        self.assertEqual(ctx["scene_brief"], BRIEF)
        row = brain.get_scene_state(self.conn, OWNER, "brooklyn")
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["brief"], BRIEF)  # verbatim, untrimmed
        text = brain._gemini_system_text(ctx, "brooklyn")
        self.assertIn(BRIEF, text)
        self.assertIn("Vivienne", text)
        self.assertIn("Daniel", text)
        self.assertIn("CASTING RULES THE SCENE", text)
        self.assertIn("Open the scene yourself", text)

    def test_brief_persists_on_later_turns(self):
        self._owner_ctx("role play", BROOKLYN)
        self._owner_ctx(BRIEF, BROOKLYN)
        ctx = self._owner_ctx("i sit down at the bar next to you", BROOKLYN)
        self.assertEqual(ctx["scene_state"], "active")
        self.assertFalse(ctx["scene_opening"])
        self.assertEqual(ctx["scene_brief"], BRIEF)
        text = brain._gemini_system_text(ctx, "brooklyn")
        self.assertIn(BRIEF, text)

    def test_casting_overrides_default_persona_then_restores(self):
        # Her default persona says she does NOT know him; the brief
        # casts her as Vivienne, who has watched Daniel for weeks.
        self._owner_ctx("role play", BROOKLYN)
        ctx = self._owner_ctx(BRIEF, BROOKLYN)
        text = brain._gemini_system_text(ctx, "brooklyn")
        # The casting block lands AFTER the persona block in the
        # prompt (recency wins the conflict while in-scene).
        self.assertGreater(text.index("CASTING RULES THE SCENE"),
                           text.index("YOUR PERSONA"))
        self.assertIn("override your default persona facts", text)
        # After pickles the scene is gone and defaults are all that's
        # left — nothing of the brief remains in her prompt.
        self._owner_ctx("pickles", BROOKLYN)
        ctx2 = self._owner_ctx("hey", BROOKLYN)
        self.assertIsNone(ctx2["scene_state"])
        text2 = brain._gemini_system_text(ctx2, "brooklyn")
        self.assertNotIn("Vivienne", text2)
        self.assertNotIn("CASTING RULES THE SCENE", text2)

    def test_pickles_clears_scene(self):
        self._owner_ctx("role play", BROOKLYN)
        self._owner_ctx(BRIEF, BROOKLYN)
        ctx = self._owner_ctx("pickles", BROOKLYN)
        self.assertIsNone(ctx["scene_state"])
        self.assertTrue(ctx["scene_just_ended"])
        self.assertIsNone(brain.get_scene_state(self.conn, OWNER, "brooklyn"))
        text = brain._gemini_system_text(ctx, "brooklyn")
        self.assertIn(brain.ROLEPLAY_RELEASE_NOTE, text)

    def test_pickles_clears_scene_and_brooklyn_lock_together(self):
        self.conn.execute(
            "UPDATE local_characters SET roleplay_locked = 1 "
            "WHERE id = 'brooklyn'")
        self.conn.commit()
        self._owner_ctx("role play", BROOKLYN)
        self._owner_ctx(BRIEF, BROOKLYN)
        ctx = self._owner_ctx("pickles", BROOKLYN)
        self.assertFalse(ctx["roleplay_locked"])
        self.assertTrue(ctx["roleplay_just_released"])
        self.assertIsNone(ctx["scene_state"])
        row = self.conn.execute(
            "SELECT roleplay_locked FROM local_characters "
            "WHERE id = 'brooklyn'").fetchone()
        self.assertEqual(row[0], 0)
        self.assertIsNone(brain.get_scene_state(self.conn, OWNER, "brooklyn"))

    def test_pickles_while_awaiting_cancels_setup(self):
        self._owner_ctx("role play", BROOKLYN)
        ctx = self._owner_ctx("pickles", BROOKLYN)
        self.assertIsNone(ctx["scene_state"])
        self.assertIsNone(brain.get_scene_state(self.conn, OWNER, "brooklyn"))

    def test_retrigger_while_awaiting_asks_again(self):
        self._owner_ctx("role play", BROOKLYN)
        ctx = self._owner_ctx("role-play", BROOKLYN)
        self.assertEqual(ctx["scene_state"], "awaiting")

    def test_trigger_phrase_inside_scene_is_dialogue_not_a_brief(self):
        self._owner_ctx("role play", BROOKLYN)
        self._owner_ctx(BRIEF, BROOKLYN)
        ctx = self._owner_ctx("role play", BROOKLYN)
        self.assertEqual(ctx["scene_state"], "active")
        self.assertEqual(ctx["scene_brief"], BRIEF)

    def test_flow_works_for_another_character(self):
        ctx = self._owner_ctx("role play", MIA)
        self.assertEqual(ctx["scene_state"], "awaiting")
        ctx = self._owner_ctx(BRIEF, MIA)
        self.assertEqual(ctx["scene_state"], "active")
        self.assertEqual(ctx["scene_brief"], BRIEF)
        ctx = self._owner_ctx("pickles", MIA)
        self.assertIsNone(ctx["scene_state"])

    def test_non_owner_gets_no_flow(self):
        ctx = brain.build_brain_context(
            self.conn, OTHER, self.other, "role play", BROOKLYN)
        self.assertIsNone(ctx["scene_state"])
        self.assertIsNone(brain.get_scene_state(self.conn, OTHER, "brooklyn"))
        ctx = brain.build_brain_context(
            self.conn, OTHER, self.other, BRIEF, BROOKLYN)
        self.assertIsNone(ctx["scene_state"])
        self.assertIsNone(brain.get_scene_state(self.conn, OTHER, "brooklyn"))

    def test_per_character_isolation(self):
        self._owner_ctx("role play", BROOKLYN)
        self._owner_ctx(BRIEF, BROOKLYN)
        # Hannah's chat is untouched by Brooklyn's scene.
        htx = self._owner_ctx("hey", HANNAH)
        self.assertIsNone(htx["scene_state"])
        htext = brain._gemini_system_text(htx, "hannah")
        self.assertNotIn("Vivienne", htext)
        # Hannah runs her own, different scene.
        mia_brief = "You are a lifeguard named June. I am the new hire."
        self._owner_ctx("role play", HANNAH)
        htx = self._owner_ctx(mia_brief, HANNAH)
        self.assertEqual(htx["scene_brief"], mia_brief)
        # Ending Brooklyn's scene leaves Hannah's running.
        self._owner_ctx("pickles", BROOKLYN)
        self.assertIsNone(brain.get_scene_state(self.conn, OWNER, "brooklyn"))
        hrow = brain.get_scene_state(self.conn, OWNER, "hannah")
        self.assertEqual(hrow["state"], "active")
        self.assertEqual(hrow["brief"], mia_brief)

    def test_scene_table_exists_after_init(self):
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'roleplay_scenes'").fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
