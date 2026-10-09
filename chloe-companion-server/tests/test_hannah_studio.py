"""Studio tests (2026-10-09): owner-only add-a-give and guided
character creation. Studio is Mia's desk (Rocky keeps his own);
Hannah/Jordan get the fixed Mia redirect for studio-shaped asks.

Rules pinned here: the owner names characters (Mia never coins);
uploads file directly, generated images file only on his explicit add
after he's seen them; new characters are PRIVATE by default; she never
deletes and never touches the reserved 'chloe' id; media is only ever
filed under a character he actually named (ambiguous/missing -> ask).

Admin/media internals are the real in-DB functions (the same rows the
admin endpoints write); the Gemini depth generator is mocked — zero
real pool calls.

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

from app import assist, characters, creator, db, store  # noqa: E402

OWNER = {"user_id": "owner_1", "tier": "basic"}
STRANGER = {"user_id": "someone_else", "tier": "basic"}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    assist.ensure_tables(conn)
    return conn


def _seed_girl(conn, cid: str, name: str, owner: str | None = None):
    store.upsert_local_character(conn, {"id": cid, "name": name,
                                        "owner_user_id": owner})


def _fake_roster(conn):
    """characters.get_characters stand-in fed from the test DB."""
    def fake(user_id=None, include_private=False, refresh=False):
        out = []
        for r in conn.execute("SELECT * FROM local_characters").fetchall():
            d = dict(r)
            out.append({"id": d["id"], "name": d["name"],
                        "avatar_url": d["avatar_url"],
                        "persona": d["persona"],
                        "companion_voice": d["companion_voice"],
                        "blurb": d["blurb"], "door_title": d["door_title"],
                        "sort_order": d["sort_order"],
                        "owner_user_id": d["owner_user_id"]})
        if not include_private:
            out = [c for c in out
                   if not c["owner_user_id"]
                   or c["owner_user_id"] == (user_id or "")]
        return out
    return fake


def _img_att(url="/generated/hannah-uploads/att_1.jpg"):
    return {"id": "att_1", "kind": "image", "name": "pic.jpg",
            "mime_type": "image/jpeg", "size": 10, "url": url,
            "extraction_status": "ok"}


class StudioBase(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        self.conn = _conn()
        _seed_girl(self.conn, "mara", "Mara")
        _seed_girl(self.conn, "hannah", "Hannah", owner="owner_1")
        _seed_girl(self.conn, "sam-a", "Sam")
        _seed_girl(self.conn, "sam-b", "Sam")
        self._patches = [
            mock.patch.object(characters, "get_characters",
                              _fake_roster(self.conn)),
            mock.patch.object(creator, "_generate_hidden_depth",
                              return_value="A quiet loss shaped her."),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()

    def say(self, text, user=OWNER, cid="mia", attachments=None):
        return assist.maybe_assist(self.conn, user, text, cid,
                                   attachments=attachments)

    def gives(self, girl_id):
        return assist.give_count(self.conn, girl_id)


class DetectTest(StudioBase):
    def test_add_give_shapes(self):
        det = assist._detect_add_give("add this as a give for Mara")
        self.assertEqual(det["target"], "mara")
        det = assist._detect_add_give("make that a give for hannah please")
        self.assertEqual(det["target"], "hannah")
        det = assist._detect_add_give("give that to Brook")
        self.assertEqual(det["target"], "brook")

    def test_give_that_to_me_is_not_a_target(self):
        det = assist._detect_add_give("give that to me")
        self.assertFalse((det or {}).get("target"))
        self.assertIsNone(assist._detect_add_give("give me a hug"))

    def test_resolve_missing_and_ambiguous(self):
        self.assertEqual(
            assist.resolve_character_name(self.conn, "owner_1", "sam")
            ["status"], "ambiguous")
        self.assertEqual(
            assist.resolve_character_name(self.conn, "owner_1", "zorp")
            ["status"], "missing")
        ok = assist.resolve_character_name(self.conn, "owner_1", "Mara")
        self.assertEqual(ok["character"]["id"], "mara")


class GiveTest(StudioBase):
    def test_upload_files_directly(self):
        res = self.say("add this as a give for Mara",
                       attachments=[_img_att()])
        self.assertEqual(res["give"]["count"], 1)
        self.assertFalse(res["give"]["duplicate"])
        self.assertEqual(self.gives("mara"), 1)

    def test_upload_dedupes(self):
        self.say("add this as a give for Mara", attachments=[_img_att()])
        res = self.say("add this as a give for Mara",
                       attachments=[_img_att()])
        self.assertTrue(res["give"]["duplicate"])
        self.assertEqual(self.gives("mara"), 1)

    def test_unknown_character_never_files(self):
        res = self.say("add this as a give for Zorp",
                       attachments=[_img_att()])
        self.assertIn("brief", res)
        self.assertEqual(self.gives("zorp"), 0)
        self.assertNotIn("give", res)

    def test_ambiguous_character_asks(self):
        res = self.say("add this as a give for Sam",
                       attachments=[_img_att()])
        self.assertEqual(self.gives("sam-a"), 0)
        self.assertEqual(self.gives("sam-b"), 0)
        self.assertNotIn("give", res)

    def test_no_media_no_file(self):
        res = self.say("add this as a give for Mara")
        self.assertEqual(self.gives("mara"), 0)
        self.assertNotIn("give", res)

    def test_url_reference(self):
        det = assist._detect_add_give(
            "add https://example.com/x.png as a give for Mara")
        self.assertEqual(det["target"], "mara")
        self.assertEqual(det["url"], "https://example.com/x.png")
        res = self.say(
            "add https://example.com/x.png as a give for Mara")
        self.assertEqual(res["give"]["count"], 1)

    def test_media_id_reference(self):
        cur = self.conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset) "
            "VALUES ('hannah', 'give', '/generated/x.png', 'give')")
        self.conn.commit()
        res = self.say(f"add media {cur.lastrowid} as a give for Mara")
        self.assertEqual(res["give"]["count"], 1)

    def test_generated_image_needs_his_yes_after_seeing_it(self):
        tmp = tempfile.TemporaryDirectory()
        old_dir, old_post = assist.config.GENERATED_DIR, assist._image_post
        assist.config.GENERATED_DIR = tmp.name
        png = base64.b64encode(b"\x89PNG" + b"x" * 64).decode()
        assist._image_post = lambda prompt, model: {
            "candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": png}}]}}]}
        try:
            res = self.say(
                "make a picture of a lighthouse and add it as a give "
                "for Mara")
            self.assertIn("image_url", res)  # generated and shown
            self.assertEqual(self.gives("mara"), 0)  # NOT filed yet
            res = self.say("yes")
            self.assertEqual(res["give"]["count"], 1)
            self.assertEqual(self.gives("mara"), 1)
        finally:
            assist._image_post = old_post
            assist.config.GENERATED_DIR = old_dir
            tmp.cleanup()

    def test_seen_generated_image_files_on_explicit_add(self):
        assist._studio_set(self.conn, "owner_1", "last_image",
                           {"url": "/generated/hannah-img-9.png",
                            "prompt": "x", "model": "m"})
        res = self.say("add that as a give for Hannah")
        self.assertEqual(res["give"]["count"], 1)

    def test_delete_is_refused_from_chat(self):
        res = self.say("delete the character Mara")
        self.assertIn("admin", res["brief"])
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM local_characters WHERE id='mara'"
        ).fetchone()
        self.assertEqual(row["n"], 1)

    def test_stranger_gets_nothing(self):
        res = self.say("add this as a give for Mara", user=STRANGER,
                       attachments=[_img_att()])
        self.assertIsNone(res)
        self.assertEqual(self.gives("mara"), 0)

    def test_only_in_mias_thread(self):
        res = self.say("add this as a give for Mara", cid="mara")
        self.assertIsNone(res)
        self.assertEqual(self.gives("mara"), 0)
        # Hannah/Jordan get the fixed Mia redirect, never the filing.
        for cid in ("hannah", "jordan"):
            res = self.say("add this as a give for Mara", cid=cid,
                           attachments=[_img_att()])
            self.assertIsNotNone(res, cid)
            self.assertIn("mia's desk", res["brief"])
            self.assertEqual(self.gives("mara"), 0)


class CreateTest(StudioBase):
    def _walk_to_confirm(self, name_line="Test Tessa"):
        self.say("create a character")
        self.say(name_line)
        self.say("female")
        self.say("A warm, flirty lifeguard who loves sunset swims.")
        self.say("skip")   # voice
        return self.say("skip")  # avatar -> confirm

    def test_name_is_asked_never_coined(self):
        res = self.say("create a character, she's a redhead painter")
        self.assertIsNotNone(res)
        self.assertIn("NAME", res["brief"])
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM local_characters "
            "WHERE id != 'test-tessa'").fetchone()
        self.assertEqual(rows["n"], 4)  # only the seeded four

    def test_full_flow_creates_private_character(self):
        res = self._walk_to_confirm()
        self.assertIn("visibility", res["brief"])
        res = self.say("yes")
        self.assertEqual(res["studio_action"], "character_created")
        self.assertEqual(res["character"]["id"], "test-tessa")
        row = self.conn.execute(
            "SELECT * FROM local_characters WHERE id='test-tessa'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["owner_user_id"], "owner_1")  # private
        self.assertIn("his girlfriend", row["persona"])     # canon
        self.assertIn("A quiet loss shaped her.", row["persona"])

    def test_public_only_when_he_says_so(self):
        self.say("create a character named Public Pam, she's a painter, "
                 "make her public")
        self.say("skip")
        self.say("skip")
        res = self.say("yes")
        self.assertTrue(res["character"]["public"])
        row = self.conn.execute(
            "SELECT owner_user_id FROM local_characters "
            "WHERE id='public-pam'").fetchone()
        self.assertIsNone(row["owner_user_id"])

    def test_collision_stops_and_asks(self):
        res = self._walk_to_confirm(name_line="Mara")
        res = self.say("yes")
        self.assertNotIn("studio_action", res)
        self.assertIn("already exists", res["brief"])
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM local_characters WHERE id='mara'"
        ).fetchone()
        self.assertEqual(rows["n"], 1)

    def test_chloe_can_never_be_created(self):
        res = self._walk_to_confirm(name_line="Chloe")
        res = self.say("yes")
        self.assertNotIn("studio_action", res)
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM local_characters WHERE id='chloe'"
        ).fetchone()
        self.assertEqual(row["n"], 0)

    def test_cancel_creates_nothing(self):
        self.say("create a character")
        self.say("Test Tessa")
        res = self.say("cancel")
        self.assertIn("dropped", res["brief"])
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM local_characters "
            "WHERE id='test-tessa'").fetchone()
        self.assertEqual(row["n"], 0)

    def test_personality_comes_from_him_not_her(self):
        self.say("create a character")
        self.say("Test Tessa")
        self.say("female")
        res = self.say("you decide")
        self.assertIn("invent", res["brief"])  # she refuses to make it up
        self.assertIn("personality", self.say("a quiet poet")["brief"])

    def test_avatar_from_his_photo(self):
        self.say("create a character")
        self.say("Photo Fran")
        self.say("female")
        self.say("A sunny surfer who collects old postcards.")
        self.say("skip")
        res = self.say("here's her picture",
                       attachments=[_img_att("/generated/up/fran.jpg")])
        res = self.say("yes")
        self.assertEqual(res["character"]["avatar_url"],
                         "/generated/up/fran.jpg")

    def test_use_last_generated_as_avatar(self):
        self._walk_to_confirm()
        res = self.say("yes")
        self.assertEqual(res["character"]["id"], "test-tessa")
        assist._studio_set(self.conn, "owner_1", "last_image",
                           {"url": "/generated/portrait.png",
                            "prompt": "her", "model": "m"})
        res = self.say("use that as Test Tessa's avatar")
        self.assertEqual(res["studio_action"], "avatar_set")
        row = self.conn.execute(
            "SELECT avatar_url FROM local_characters "
            "WHERE id='test-tessa'").fetchone()
        self.assertEqual(row["avatar_url"], "/generated/portrait.png")

    def test_avatar_only_for_his_own_characters(self):
        row = self.conn.execute(
            "SELECT * FROM local_characters WHERE id='mara'").fetchone()
        store.upsert_local_character(
            self.conn, {**dict(row), "owner_user_id": "someone_else"})
        assist._studio_set(self.conn, "owner_1", "last_image",
                           {"url": "/generated/p.png", "prompt": "x",
                            "model": "m"})
        res = self.say("use that as Mara's avatar")
        self.assertNotIn("studio_action", res)


if __name__ == "__main__":
    unittest.main()
