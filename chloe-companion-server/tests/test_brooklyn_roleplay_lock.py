"""Brooklyn roleplay lock: a persistent per-character mode (2026-10-13).

The lock lives on the girl's local_characters row (roleplay_locked). While
on, her Gemini system prompt carries ROLEPLAY_LOCK_RULE — she stays in
character and in-scene in every reply. The owner ends it with the release
word ("pickles") as a whole message; the flip happens inside
build_brain_context, before that turn's prompt is built, so the release
reply is already generated unlocked.

Run:  python -m unittest discover -s tests -v   (from the project root,
      with the venv's python)
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import brain, db, store  # noqa: E402

OWNER = "owner_user"

BROOKLYN = {
    "id": "brooklyn",
    "name": "Brooklyn",
    "persona": ("Brooklyn is his new neighbor. She does not know him yet "
                "and does not know his name."),
    "owner_user_id": OWNER,
}
HANNAH = {"id": "hannah", "name": "Hannah", "persona": "Hannah."}


def _flag(conn, cid):
    row = conn.execute(
        "SELECT roleplay_locked FROM local_characters WHERE id = ?",
        (cid,),
    ).fetchone()
    return None if row is None else row[0]


def _set_flag(conn, cid, value):
    conn.execute(
        "UPDATE local_characters SET roleplay_locked = ? WHERE id = ?",
        (value, cid),
    )
    conn.commit()


class RoleplayLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self.conn = db.connect(self.db_path)
        self.user = store.get_or_create_user(self.conn, OWNER, "Matty")
        store.upsert_local_character(self.conn, dict(BROOKLYN))
        store.upsert_local_character(self.conn, dict(HANNAH))
        self.brooklyn = dict(BROOKLYN)
        self.hannah = dict(HANNAH)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _context(self, text, char):
        return brain.build_brain_context(
            self.conn, OWNER, self.user, text, char)

    def test_default_off_for_every_character(self):
        self.assertEqual(_flag(self.conn, "brooklyn"), 0)
        self.assertEqual(_flag(self.conn, "hannah"), 0)

    def test_lock_on_adds_directive_for_brooklyn_only(self):
        _set_flag(self.conn, "brooklyn", 1)
        ctx = self._context("hey, come here often?", self.brooklyn)
        self.assertTrue(ctx["roleplay_locked"])
        self.assertFalse(ctx["roleplay_just_released"])
        text = brain._gemini_system_text(ctx, "brooklyn")
        self.assertIn("ROLEPLAY LOCK", text)
        self.assertIn(brain.ROLEPLAY_LOCK_RULE, text)
        # The standing scene-by-scene rule is still there too (extended,
        # not replaced).
        self.assertIn(brain.ROLEPLAY_RULE, text)

        htx = self._context("hey", self.hannah)
        self.assertFalse(htx["roleplay_locked"])
        htext = brain._gemini_system_text(htx, "hannah")
        self.assertNotIn("ROLEPLAY LOCK", htext)
        self.assertNotIn(brain.ROLEPLAY_LOCK_RULE, htext)

    def test_pickles_releases_before_the_reply_is_built(self):
        _set_flag(self.conn, "brooklyn", 1)
        ctx = self._context("pickles", self.brooklyn)
        # Flag flipped in the DB...
        self.assertEqual(_flag(self.conn, "brooklyn"), 0)
        # ...and this turn's context is already unlocked, with the
        # one-turn note so she acknowledges the release out of scene.
        self.assertFalse(ctx["roleplay_locked"])
        self.assertTrue(ctx["roleplay_just_released"])
        text = brain._gemini_system_text(ctx, "brooklyn")
        self.assertNotIn(brain.ROLEPLAY_LOCK_RULE, text)
        self.assertIn(brain.ROLEPLAY_RELEASE_NOTE, text)
        # The next turn is plain unlocked: no note, no directive.
        ctx2 = self._context("thanks", self.brooklyn)
        self.assertFalse(ctx2["roleplay_locked"])
        self.assertFalse(ctx2["roleplay_just_released"])
        text2 = brain._gemini_system_text(ctx2, "brooklyn")
        self.assertNotIn(brain.ROLEPLAY_LOCK_RULE, text2)
        self.assertNotIn(brain.ROLEPLAY_RELEASE_NOTE, text2)

    def test_pickles_case_and_punctuation_variants_release(self):
        for msg in ("Pickles", "PICKLES!", "  pickles.  "):
            _set_flag(self.conn, "brooklyn", 1)
            ctx = self._context(msg, self.brooklyn)
            self.assertFalse(ctx["roleplay_locked"], msg)
            self.assertEqual(_flag(self.conn, "brooklyn"), 0, msg)

    def test_pickles_inside_a_sentence_never_releases(self):
        _set_flag(self.conn, "brooklyn", 1)
        for msg in ("i could really go for some pickles right now",
                    "pass me the pickles, would you?",
                    "pickles are my favorite, stay in character"):
            ctx = self._context(msg, self.brooklyn)
            self.assertTrue(ctx["roleplay_locked"], msg)
            self.assertFalse(ctx["roleplay_just_released"], msg)
            self.assertEqual(_flag(self.conn, "brooklyn"), 1, msg)

    def test_pickles_while_unlocked_is_just_a_message(self):
        ctx = self._context("pickles", self.brooklyn)
        self.assertFalse(ctx["roleplay_locked"])
        self.assertFalse(ctx["roleplay_just_released"])

    def test_lock_off_prompt_is_byte_identical(self):
        ctx = self._context("hey", self.brooklyn)
        bare = {k: v for k, v in ctx.items()
                if k not in ("roleplay_locked", "roleplay_just_released")}
        self.assertEqual(
            brain._gemini_system_text(ctx, "brooklyn"),
            brain._gemini_system_text(bare, "brooklyn"),
        )

    def test_release_word_matcher(self):
        self.assertTrue(brain._is_roleplay_release("pickles"))
        self.assertTrue(brain._is_roleplay_release(" Pickles! "))
        self.assertFalse(brain._is_roleplay_release("pickles? no thanks"))
        self.assertFalse(brain._is_roleplay_release("dill pickles"))
        self.assertFalse(brain._is_roleplay_release(""))
        self.assertFalse(brain._is_roleplay_release(None))


class RoleplayLockMigrationTest(unittest.TestCase):
    """An existing install (table without the column) gets the column via
    migration, with Brooklyn — and only Brooklyn — seeded ON, exactly
    once: a later release is never undone by a restart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        conn = db.connect(self.db_path)
        try:
            store.upsert_local_character(conn, dict(BROOKLYN))
            store.upsert_local_character(conn, dict(HANNAH))
            # Simulate the pre-lock install: drop the column entirely.
            conn.execute(
                "ALTER TABLE local_characters DROP COLUMN roleplay_locked")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(sqlite3.sqlite_version_info >= (3, 35),
                         "DROP COLUMN needs sqlite >= 3.35")
    def test_migration_adds_column_and_seeds_brooklyn_once(self):
        db.init_db(self.db_path)
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(_flag(conn, "brooklyn"), 1)
            self.assertEqual(_flag(conn, "hannah"), 0)
            # Owner releases; a restart (init_db again) must NOT re-lock.
            _set_flag(conn, "brooklyn", 0)
        finally:
            conn.close()
        db.init_db(self.db_path)
        conn = db.connect(self.db_path)
        try:
            self.assertEqual(_flag(conn, "brooklyn"), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
