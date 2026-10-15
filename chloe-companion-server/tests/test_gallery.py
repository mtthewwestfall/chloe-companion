"""Public companion gallery tests (2026-10-15).

The gallery lists creator-made companions ONLY when the creator opted
in (gallery_public = 1) and the character is enabled. Default is
PRIVATE. Cards carry avatar (first photo), name, tagline, a persona
snippet taken from the user-written Personality section ONLY (never the
compiled persona file's private backstory), and the creator's display
name when the account carries one. Opting in also makes the character
visible/chat-able to everyone through the normal roster rules; opting
out restores owner-only privacy. Creation gates (paid tier, content
safety, attestation) are untouched.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import base64
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException  # noqa: E402

from app import characters, config, creator, db, store  # noqa: E402
from app import main as api  # noqa: E402

TINY_PNG = ("data:image/png;base64," + base64.b64encode(b"\x89PNG" + b"x" * 2000).decode())
DEPTH_SENTINEL = "SECRET DEPTH SENTINEL 7f3a"


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def _payload(**kw):
    d = {
        "name": "Nova",
        "persona": "A witty stargazer who texts like an old friend.",
        "blurb": "Your midnight co-pilot.",
        "gender": "female",
        "attest_18": True,
        "attest_rights": True,
        "images": [TINY_PNG],
    }
    d.update(kw)
    return d


def _create(conn, user="u1", **kw):
    with mock.patch.object(creator, "_generate_hidden_depth",
                           return_value=DEPTH_SENTINEL):
        return creator.create_companion(conn, user, _payload(**kw))


class GalleryListingTest(unittest.TestCase):
    def test_default_is_private_and_unlisted(self):
        conn = _mem_db()
        comp = _create(conn)
        row = conn.execute(
            "SELECT gallery_public FROM local_characters WHERE id = ?",
            (comp["id"],)).fetchone()
        self.assertEqual(row["gallery_public"], 0)
        self.assertFalse(comp["gallery_public"])
        self.assertEqual(store.list_gallery_characters(conn), [])

    def test_opt_in_lists_card_with_shape(self):
        conn = _mem_db()
        conn.execute(
            "INSERT INTO users (user_id, name) VALUES ('u1', 'Casey C')")
        comp = _create(conn, gallery_public=True)
        self.assertTrue(comp["gallery_public"])
        cards = store.list_gallery_characters(conn)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["id"], comp["id"])
        self.assertEqual(card["name"], "Nova")
        self.assertEqual(card["avatar_url"], TINY_PNG)  # first photo
        self.assertEqual(card["tagline"], "Your midnight co-pilot.")
        self.assertIn("witty stargazer", card["persona_snippet"])
        self.assertEqual(card["creator_name"], "Casey C")
        self.assertEqual(card["chat_url"], f"/?character={comp['id']}")

    def test_snippet_never_leaks_private_backstory(self):
        conn = _mem_db()
        _create(conn, gallery_public=True)
        card = store.list_gallery_characters(conn)[0]
        self.assertNotIn(DEPTH_SENTINEL, card["persona_snippet"])
        self.assertNotIn("backstory", card["persona_snippet"].lower())

    def test_creator_name_null_when_account_has_none(self):
        conn = _mem_db()
        _create(conn, gallery_public=True)  # no users row for u1
        card = store.list_gallery_characters(conn)[0]
        self.assertIsNone(card["creator_name"])

    def test_excludes_disabled_and_roster_rows(self):
        conn = _mem_db()
        comp = _create(conn, gallery_public=True)
        # Roster girl (no owner) who never opted in: not a gallery card.
        store.upsert_local_character(conn, {
            "id": "roster-girl", "name": "Roster", "enabled": True})
        cards = store.list_gallery_characters(conn)
        self.assertEqual([c["id"] for c in cards], [comp["id"]])
        # Disabled opted-in character drops out of the gallery.
        store.upsert_local_character(conn, {
            "id": comp["id"], "name": "Nova", "enabled": False,
            "gallery_public": True})
        self.assertEqual(store.list_gallery_characters(conn), [])

    def test_upsert_without_gallery_key_preserves_opt_in(self):
        conn = _mem_db()
        comp = _create(conn, gallery_public=True)
        # Routine admin-style edit (no gallery_public key) must not
        # silently unpublish her.
        store.upsert_local_character(conn, {
            "id": comp["id"], "name": "Nova Renamed", "enabled": True})
        self.assertEqual(len(store.list_gallery_characters(conn)), 1)

    def test_set_gallery_public_flips(self):
        conn = _mem_db()
        comp = _create(conn)
        self.assertTrue(
            store.set_character_gallery_public(conn, comp["id"], True))
        self.assertEqual(len(store.list_gallery_characters(conn)), 1)
        self.assertTrue(
            store.set_character_gallery_public(conn, comp["id"], False))
        self.assertEqual(store.list_gallery_characters(conn), [])
        self.assertFalse(
            store.set_character_gallery_public(conn, "nope", True))


class GalleryMigrationTest(unittest.TestCase):
    def test_old_db_gets_column_defaulting_private(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE local_characters ("
            " id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            " avatar_url TEXT NOT NULL DEFAULT '', "
            " persona TEXT NOT NULL DEFAULT '', "
            " companion_voice TEXT NOT NULL DEFAULT 'liora', "
            " blurb TEXT NOT NULL DEFAULT '', "
            " door_title TEXT NOT NULL DEFAULT '', "
            " sort_order INTEGER NOT NULL DEFAULT 0, "
            " enabled INTEGER NOT NULL DEFAULT 1, "
            " verified_fictional INTEGER NOT NULL DEFAULT 0, "
            " photos TEXT NOT NULL DEFAULT '[]', "
            " owner_user_id TEXT, "
            " roleplay_locked INTEGER NOT NULL DEFAULT 0, "
            " created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            " updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
        conn.execute(
            "INSERT INTO local_characters (id, name) VALUES ('old', 'Old')")
        db._migrate_gallery_column(conn)
        row = conn.execute(
            "SELECT gallery_public FROM local_characters WHERE id = 'old'"
        ).fetchone()
        self.assertEqual(row["gallery_public"], 0)


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


class GalleryApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        characters._cache = {"ts": 0.0, "chars": []}
        conn = db.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO users (user_id, name) VALUES ('u1', 'Casey C')")
            conn.commit()
            with mock.patch.object(creator, "_generate_hidden_depth",
                                   return_value=DEPTH_SENTINEL):
                self.comp = creator.create_companion(
                    conn, "u1", _payload(gallery_public=True))
        finally:
            conn.close()

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        characters._cache = {"ts": 0.0, "chars": []}
        self.tmp.cleanup()

    def test_gallery_json_lists_only_opted_in(self):
        data = api.gallery_list()
        self.assertEqual([c["id"] for c in data["companions"]],
                         [self.comp["id"]])

    def test_gallery_page_renders_cards_and_cta(self):
        page = api.gallery_page()
        self.assertIn("Nova", page)
        self.assertIn(f"/?character={self.comp['id']}", page)
        self.assertIn("/?create=1", page)          # creator funnel CTA
        self.assertIn("Casey C", page)
        self.assertNotIn(DEPTH_SENTINEL, page)

    def test_gallery_page_empty_state_still_funnels(self):
        conn = db.connect(self.db_path)
        try:
            store.set_character_gallery_public(conn, self.comp["id"], False)
        finally:
            conn.close()
        page = api.gallery_page()
        self.assertIn("No community companions yet", page)
        self.assertIn("/?create=1", page)

    def test_gallery_page_escapes_card_text(self):
        conn = db.connect(self.db_path)
        try:
            store.upsert_local_character(conn, {
                "id": "evil", "name": "<script>alert(1)</script>",
                "enabled": True, "owner_user_id": "u1",
                "gallery_public": True})
        finally:
            conn.close()
        page = api.gallery_page()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def _session_req(self, user_id):
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, user_id)
            token = f"tok-{user_id}"
            store.create_session(conn, user_id, token)
        finally:
            conn.close()
        return _Req({"authorization": f"Bearer {token}"})

    def test_toggle_requires_ownership(self):
        body = api.GalleryVisibilityIn(public=False)
        out = api.set_companion_gallery(
            self.comp["id"], body, self._session_req("u1"))
        self.assertEqual(out["gallery_public"], False)
        self.assertEqual(api.gallery_list()["companions"], [])
        # A different signed-in user may not flip her back.
        with self.assertRaises(HTTPException) as ctx:
            api.set_companion_gallery(
                self.comp["id"], api.GalleryVisibilityIn(public=True),
                self._session_req("someone-else"))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_toggle_unknown_character_404(self):
        with self.assertRaises(HTTPException) as ctx:
            api.set_companion_gallery(
                "missing", api.GalleryVisibilityIn(public=True),
                self._session_req("u1"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_toggle_requires_login(self):
        with self.assertRaises(HTTPException) as ctx:
            api.set_companion_gallery(
                self.comp["id"], api.GalleryVisibilityIn(public=True),
                _Req({}))
        self.assertEqual(ctx.exception.status_code, 401)


class GalleryVisibilityTest(unittest.TestCase):
    """Roster/chat visibility: opted-in companions resolve for everyone;
    private ones stay owner-only (chat falls back to Chloe for others)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        characters._cache = {"ts": 0.0, "chars": []}
        conn = db.connect(self.db_path)
        try:
            with mock.patch.object(creator, "_generate_hidden_depth",
                                   return_value=DEPTH_SENTINEL):
                self.pub = creator.create_companion(
                    conn, "u1", _payload(name="Pub", gallery_public=True))
                self.priv = creator.create_companion(
                    conn, "u1", _payload(name="Priv"))
        finally:
            conn.close()

    def tearDown(self):
        config.DB_PATH = self._real_db_path
        characters._cache = {"ts": 0.0, "chars": []}
        self.tmp.cleanup()

    def test_visible_to(self):
        self.assertTrue(characters.visible_to(self.pub["id"], "stranger"))
        self.assertTrue(characters.visible_to(self.pub["id"], "u1"))
        self.assertFalse(characters.visible_to(self.priv["id"], "stranger"))
        self.assertTrue(characters.visible_to(self.priv["id"], "u1"))

    def test_roster_resolution_for_strangers(self):
        with mock.patch.object(characters, "_fetch",
                               side_effect=RuntimeError("offline")):
            got = characters.get_character(self.pub["id"], user_id="stranger")
            self.assertEqual(got["id"], self.pub["id"])
            # Private companion: strangers fall back to Chloe, as today.
            got = characters.get_character(self.priv["id"], user_id="stranger")
            self.assertEqual(got["id"], "chloe")
            # ...and she appears in the public roster only when opted in.
            ids = [c["id"] for c in characters.public_list("stranger")]
            self.assertIn(self.pub["id"], ids)
            self.assertNotIn(self.priv["id"], ids)


if __name__ == "__main__":
    unittest.main()
