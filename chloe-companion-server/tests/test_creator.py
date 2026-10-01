"""Companion-creator tests (ported-creator, 2026-10-01): validation,
attestation gates, content-safety gate, photo-moderation mapping, and the
live-creation path (real photos kept, persona file built, slug de-duped).

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import base64
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import creator, db, store  # noqa: E402

TINY_PNG = ("data:image/png;base64," + base64.b64encode(b"\x89PNG" + b"x" * 2000).decode())
TINY_JPG = ("data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8" + b"y" * 2000).decode())


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def _payload(**kw):
    d = {
        "name": "Derek",
        "persona": "A rugged, kind-hearted firefighter in his early 30s.",
        "blurb": "Your steady shoulder.",
        "gender": "male",
        "attest_18": True,
        "attest_rights": True,
        "images": [TINY_PNG, TINY_JPG],
    }
    d.update(kw)
    return d


def _green_create(conn, **kw):
    with mock.patch.object(creator, "moderate_photo", return_value="allow"), \
         mock.patch.object(creator, "_generate_hidden_depth",
                           return_value="A quiet loss shaped him."):
        return creator.create_companion(conn, "u1", _payload(**kw))


class ValidationTest(unittest.TestCase):
    def test_name_required(self):
        with self.assertRaises(ValueError):
            _green_create(_db(), name="  ")

    def test_attest_18_required(self):
        with self.assertRaises(ValueError):
            _green_create(_db(), attest_18=False)

    def test_attest_rights_required(self):
        with self.assertRaises(ValueError):
            _green_create(_db(), attest_rights=False)

    def test_images_required(self):
        with self.assertRaises(ValueError):
            _green_create(_db(), images=[])

    def test_too_many_images(self):
        with self.assertRaises(ValueError):
            _green_create(_db(), images=[TINY_PNG] * 4)

    def test_bad_gender_defaults_female(self):
        comp = _green_create(_db(), gender="robot")
        self.assertEqual(comp["gender"], "female")


class ContentSafetyTest(unittest.TestCase):
    def test_prohibited_text_blocked(self):
        with self.assertRaises(ValueError):
            _green_create(_db(), persona="into non-consensual stuff")

    def test_clean_text_passes(self):
        comp = _green_create(_db())
        self.assertEqual(comp["name"], "Derek")


class ModerationTest(unittest.TestCase):
    def test_red_blocks(self):
        with mock.patch.object(creator, "_gemini_image_text",
                               return_value="RED"):
            with self.assertRaises(ValueError):
                creator.moderate_photo(b"fakebytes", "image/png")

    def test_green_allows(self):
        with mock.patch.object(creator, "_gemini_image_text",
                               return_value="GREEN"):
            self.assertEqual(
                creator.moderate_photo(b"fakebytes", "image/png"), "allow")

    def test_unavailable_fails_closed(self):
        with mock.patch.object(creator, "_gemini_image_text",
                               return_value=None):
            with self.assertRaises(ValueError):
                creator.moderate_photo(b"fakebytes", "image/png")

    def test_red_photo_blocks_creation(self):
        conn = _db()
        with mock.patch.object(creator, "moderate_photo",
                               side_effect=ValueError("Photo not allowed.")):
            with self.assertRaises(ValueError):
                creator.create_companion(conn, "u1", _payload())
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM local_characters").fetchone()["n"]
        self.assertEqual(rows, 0)


class LiveCreationTest(unittest.TestCase):
    def test_character_goes_live_with_real_photos(self):
        conn = _db()
        comp = _green_create(conn)
        self.assertEqual(comp["photo_count"], 2)
        self.assertEqual(comp["avatar_url"], TINY_PNG)  # real photo, not painted
        row = conn.execute(
            "SELECT * FROM local_characters WHERE id = ?",
            (comp["id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["enabled"], 1)
        import json
        photos = json.loads(row["photos"])
        self.assertEqual(photos, [TINY_PNG, TINY_JPG])
        self.assertIn("Derek", row["persona"])
        self.assertIn("A quiet loss shaped him.", row["persona"])

    def test_slug_dedupes(self):
        conn = _db()
        c1 = _green_create(conn)
        c2 = _green_create(conn)
        self.assertNotEqual(c1["id"], c2["id"])
        self.assertTrue(c2["id"].startswith(c1["id"]))

    def test_chloe_id_reserved(self):
        conn = _db()
        comp = _green_create(conn, name="Chloe")
        self.assertNotEqual(comp["id"], "chloe")


if __name__ == "__main__":
    unittest.main()
