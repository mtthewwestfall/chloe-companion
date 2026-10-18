"""Nova + Elise registration tests (2026-10-18).

The owner supplied both girls himself — photo, name, persona — and
approved the plan. These tests pin the wiring that makes them real
companions rather than roster ghosts:

- gender canon knows both ids (characters._FEMALE_CHARACTER_IDS),
- each resolves to her own Gemini TTS voice (voice._CHARACTER_GEMINI_VOICES),
- a companion-local roster row for each resolves through
  characters.get_character()/public_list() with a non-empty avatar,
- the picker (chloe-companion-app/js/app.js) carries curated cards for
  both, and the avatar image files ship in chloe-companion-app/img/.

Run:  python -m unittest tests.test_new_characters_2026_10_18 -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import characters, character_voices, config, db, store, voice  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

NOVA = {
    "id": "nova",
    "name": "Nova",
    "avatar_url": "https://companion.lockeddoor.ai/img/nova-avatar.jpg",
    "persona": "You are Nova, a young woman with late-night energy, a "
               "quick wit, and a dry, playful edge.",
    "companion_voice": "liora",
    "blurb": "Late-night wit, dry edge, soft underneath.",
    "door_title": "",
    "sort_order": 22,
    "enabled": True,
}
ELISE = {
    "id": "elise",
    "name": "Elise",
    "avatar_url": "https://companion.lockeddoor.ai/img/elise-avatar.jpg",
    "persona": "You are Elise, a young woman with wavy light-brown hair, "
               "freckles, and a big laughing smile — the free-spirited spark.",
    "companion_voice": "liora",
    "blurb": "Free-spirited, playful, and a little bit chaotic.",
    "door_title": "",
    "sort_order": 23,
    "enabled": True,
}


class GenderAndVoiceTest(unittest.TestCase):
    def test_both_are_female_roster_ids(self):
        self.assertIn("nova", characters._FEMALE_CHARACTER_IDS)
        self.assertIn("elise", characters._FEMALE_CHARACTER_IDS)
        self.assertEqual(characters.character_gender("nova"), "female")
        self.assertEqual(characters.character_gender("elise"), "female")

    def test_voice_assignments(self):
        self.assertEqual(voice._gemini_voice_for("liora", "nova"), "Gacrux")
        self.assertEqual(voice._gemini_voice_for("liora", "elise"), "Achernar")
        self.assertEqual(
            character_voices.gemini_voice_name_for(
                {"id": "nova", "companion_voice": "liora"}), "Gacrux")
        self.assertEqual(
            character_voices.gemini_voice_name_for(
                {"id": "elise", "companion_voice": "liora"}), "Achernar")

    def test_voice_map_stays_distinct(self):
        names = list(voice._CHARACTER_GEMINI_VOICES.values())
        self.assertEqual(len(names), len(set(names)))


class RosterRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        characters._cache = {"ts": 0.0, "chars": []}
        self._fetch_patch = mock.patch.object(
            characters, "_fetch", return_value=[])
        self._fetch_patch.start()

    def tearDown(self):
        self._fetch_patch.stop()
        config.DB_PATH = self._real_db_path
        characters._cache = {"ts": 0.0, "chars": []}
        self.tmp.cleanup()

    def _register_both(self):
        conn = db.connect(self.db_path)
        try:
            store.upsert_local_character(conn, dict(NOVA))
            store.upsert_local_character(conn, dict(ELISE))
        finally:
            conn.close()

    def test_local_rows_carry_nonempty_avatars(self):
        self._register_both()
        rows = {r["id"]: r for r in characters._local_characters()}
        for want in (NOVA, ELISE):
            self.assertIn(want["id"], rows)
            self.assertEqual(rows[want["id"]]["avatar_url"], want["avatar_url"])
            self.assertTrue(rows[want["id"]]["avatar_url"])

    def test_both_resolve_in_roster_and_public_list(self):
        self._register_both()
        roster = characters.get_characters(refresh=True)
        ids = {c["id"] for c in roster}
        self.assertIn("nova", ids)
        self.assertIn("elise", ids)
        self.assertEqual(characters.get_character("nova")["name"], "Nova")
        self.assertEqual(characters.get_character("elise")["name"], "Elise")
        pub = {c["id"]: c for c in characters.public_list()}
        self.assertEqual(pub["nova"]["avatar_url"], NOVA["avatar_url"])
        self.assertEqual(pub["elise"]["avatar_url"], ELISE["avatar_url"])


class PickerAssetTest(unittest.TestCase):
    def test_picker_cards_and_avatar_files(self):
        app_js = os.path.join(REPO_ROOT, "chloe-companion-app", "js", "app.js")
        with open(app_js, encoding="utf-8") as f:
            src = f.read()
        for girl in (NOVA, ELISE):
            self.assertIn(f"id: '{girl['id']}'", src)
            self.assertIn(girl["avatar_url"], src)
            img = os.path.join(
                REPO_ROOT, "chloe-companion-app", "img",
                f"{girl['id']}-avatar.jpg")
            self.assertTrue(os.path.isfile(img), img)
            self.assertGreater(os.path.getsize(img), 50000, img)
            with open(img, "rb") as fh:
                self.assertEqual(fh.read(3), b"\xff\xd8\xff", img)


if __name__ == "__main__":
    unittest.main()
