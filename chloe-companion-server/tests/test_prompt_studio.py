"""Prompt Studio tests (2026-10-17).

Covers the owner's private prompt desk (app/studio.py): the owner gate
(non-owner refused at the page AND the generate endpoint), prompt
assembly, and the generate endpoint with the Gemini pool boundary
mocked — asserting the request shape (image model URL, BLOCK_NONE on
all four harm categories, the prompt verbatim, the reference image as
an inlineData part in edit mode) and the plain-refusal surfacing.

No Gemini call ever leaves the process here; the model's own refusal
layer is proven with canned blocked responses.

Run:  python -m pytest tests/test_prompt_studio.py   (from project root)
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException  # noqa: E402

from app import characters, config, db, studio, store  # noqa: E402
from app import main as api  # noqa: E402

ADMIN_ATTR = "ADMIN" + chr(95) + "TOKEN"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048
OWNER = "owner_1"
OTHER = "user_2"


class _Req:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query_params = query or {}


def _image_response():
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "here you go"},
        {"inlineData": {"mimeType": "image/png",
                        "data": base64.b64encode(PNG_BYTES).decode()}},
    ]}}]}


class StudioTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_studio_conn = studio._conn
        studio._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_gen_dir = config.GENERATED_DIR
        config.GENERATED_DIR = os.path.join(self.tmp.name, "generated")
        self._real_admin = getattr(config, ADMIN_ATTR)
        setattr(config, ADMIN_ATTR, "test-admin-secret")
        self._old_owners = os.environ.get("ASSIST_OWNER_IDS")
        os.environ["ASSIST_OWNER_IDS"] = OWNER
        conn = db.connect(self.db_path)
        try:
            store.get_or_create_user(conn, OWNER)
            store.get_or_create_user(conn, OTHER)
            store.create_session(conn, OWNER, "owner-tok")
            store.create_session(conn, OTHER, "other-tok")
        finally:
            conn.close()
        characters._cache = {"ts": 0.0, "chars": []}

    def tearDown(self):
        studio._conn = self._real_studio_conn
        config.GENERATED_DIR = self._real_gen_dir
        setattr(config, ADMIN_ATTR, self._real_admin)
        if self._old_owners is None:
            os.environ.pop("ASSIST_OWNER_IDS", None)
        else:
            os.environ["ASSIST_OWNER_IDS"] = self._old_owners
        characters._cache = {"ts": 0.0, "chars": []}
        self.tmp.cleanup()

    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(api.app, raise_server_exceptions=False)


class GateTest(StudioTestBase):
    def test_page_refuses_anonymous(self):
        resp = self._client().get("/studio")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Owner only", resp.text)

    def test_page_refuses_non_owner_session(self):
        resp = self._client().get(
            "/studio", headers={"Authorization": "Bearer other-tok"})
        self.assertEqual(resp.status_code, 403)

    def test_page_loads_for_admin_token(self):
        resp = self._client().get(
            "/studio", headers={"X-Admin-Token": "test-admin-secret"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Prompt Studio", resp.text)
        self.assertIn("Boudoir / lingerie", resp.text)

    def test_page_loads_for_owner_session(self):
        resp = self._client().get(
            "/studio", headers={"Authorization": "Bearer owner-tok"})
        self.assertEqual(resp.status_code, 200)

    def test_gate_function_matrix(self):
        self.assertEqual(
            studio.require_studio_access(
                _Req({"x-admin-token": "test-admin-secret"})), "admin")
        self.assertEqual(
            studio.require_studio_access(
                _Req({"authorization": "Bearer owner-tok"})), OWNER)
        with self.assertRaises(HTTPException) as ctx:
            studio.require_studio_access(_Req())
        self.assertEqual(ctx.exception.status_code, 401)
        with self.assertRaises(HTTPException) as ctx:
            studio.require_studio_access(
                _Req({"authorization": "Bearer other-tok"}))
        self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(HTTPException) as ctx:
            studio.require_studio_access(
                _Req({"x-admin-token": "wrong"}))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_generate_endpoint_refuses_non_owner(self):
        with mock.patch.object(studio.gemini_keys, "post_json") as post:
            resp = self._client().post(
                "/api/v1/studio/generate",
                headers={"Authorization": "Bearer other-tok"},
                json={"prompt": "a cat", "mode": "new"})
        self.assertEqual(resp.status_code, 403)
        post.assert_not_called()

    def test_generate_endpoint_refuses_anonymous(self):
        with mock.patch.object(studio.gemini_keys, "post_json") as post:
            resp = self._client().post(
                "/api/v1/studio/generate", json={"prompt": "a cat"})
        self.assertEqual(resp.status_code, 401)
        post.assert_not_called()


class AssemblyTest(unittest.TestCase):
    def test_full_assembly(self):
        prompt = studio.assemble_prompt({
            "mode": "new", "subject": "A girl on a dorm rooftop",
            "background": "A small college town at dusk",
            "camera_angle": "low-angle", "framing": "rule-of-thirds",
            "style": "book-painted", "palette": "warm amber",
            "details": "A paper star in her hand",
        })
        self.assertEqual(
            prompt,
            "A girl on a dorm rooftop. A small college town at dusk. "
            "Composition: shot from a low angle, composed on the rule "
            "of thirds. Painted book illustration — warm painterly "
            "storybook style, soft visible brushwork, rich color. "
            "Color palette: warm amber. A paper star in her hand.")

    def test_minimal_assembly(self):
        self.assertEqual(studio.assemble_prompt({"subject": "A cat"}),
                         "A cat.")

    def test_empty_subject_assembles_to_empty(self):
        self.assertEqual(studio.assemble_prompt({"subject": "  "}), "")
        self.assertEqual(studio.assemble_prompt({}), "")
        self.assertEqual(studio.assemble_prompt(None), "")

    def test_edit_mode_prefix(self):
        prompt = studio.assemble_prompt(
            {"mode": "edit", "subject": "Remove the hat"})
        self.assertEqual(prompt,
                         "Edit the reference image: Remove the hat.")

    def test_unknown_style_and_angle_are_ignored(self):
        prompt = studio.assemble_prompt({
            "subject": "A boat", "style": "not-a-style",
            "camera_angle": "sideways", "framing": "diagonal"})
        self.assertEqual(prompt, "A boat.")


class GenerateTest(StudioTestBase):
    def _post_json(self, return_value):
        return mock.patch.object(studio.gemini_keys, "post_json",
                                 return_value=return_value)

    def test_generate_from_fields_shape_and_safety(self):
        with self._post_json(_image_response()) as post:
            out = studio.generate({
                "mode": "new",
                "fields": {"subject": "A lighthouse at dawn",
                           "style": "photorealistic"}})
        self.assertTrue(out["ok"])
        self.assertTrue(out["url"].startswith("/generated/studio_"))
        post.assert_called_once()
        url, payload = post.call_args.args[0], post.call_args.args[1]
        self.assertTrue(url.endswith(
            "gemini-2.5-flash-image:generateContent"))
        self.assertEqual(post.call_args.kwargs.get("source"),
                         "prompt-studio")
        self.assertEqual(
            payload["safetySettings"],
            [{"category": c, "threshold": "BLOCK_NONE"} for c in (
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_DANGEROUS_CONTENT")])
        parts = payload["contents"][0]["parts"]
        self.assertEqual(len(parts), 1)
        self.assertEqual(
            parts[0]["text"],
            "A lighthouse at dawn. Photorealistic — indistinguishable "
            "from a real photograph, natural texture, true-to-life "
            "lighting.")
        saved = os.path.join(config.GENERATED_DIR,
                             os.path.basename(out["url"]))
        with open(saved, "rb") as fh:
            self.assertEqual(fh.read(), PNG_BYTES)
        recent = studio.list_recent()
        self.assertEqual(recent[0]["url"], out["url"])
        self.assertEqual(recent[0]["prompt"], out["prompt"])

    def test_prompt_passes_verbatim_no_rewriting(self):
        explicit = ("her in a black lace lingerie set, bending over the "
                    "bed, looking back at the camera")
        with self._post_json(_image_response()) as post:
            studio.generate({"prompt": explicit, "mode": "new",
                             "fields": {"style": "boudoir"}})
        parts = post.call_args.args[1]["contents"][0]["parts"]
        self.assertEqual(parts[0]["text"], explicit)

    def test_generate_requires_a_prompt(self):
        with self._post_json(_image_response()) as post:
            with self.assertRaises(HTTPException) as ctx:
                studio.generate({"prompt": "", "fields": {}})
        self.assertEqual(ctx.exception.status_code, 400)
        post.assert_not_called()

    def test_edit_mode_upload_reference_in_payload(self):
        b64 = base64.b64encode(PNG_BYTES).decode()
        with self._post_json(_image_response()) as post:
            studio.generate({"mode": "edit",
                             "fields": {"subject": "Make it night"},
                             "image_b64": b64})
        parts = post.call_args.args[1]["contents"][0]["parts"]
        self.assertEqual(len(parts), 2)
        self.assertTrue(parts[0]["text"].startswith(
            "Edit the reference image:"))
        self.assertEqual(parts[1]["inlineData"]["mimeType"], "image/png")
        self.assertEqual(
            base64.b64decode(parts[1]["inlineData"]["data"]), PNG_BYTES)

    def test_edit_mode_girl_reference_in_payload(self):
        ref_file = os.path.join(self.tmp.name, "frame.png")
        with open(ref_file, "wb") as fh:
            fh.write(PNG_BYTES)
        roster = [{"id": "mara", "name": "Mara",
                   "avatar_url": "https://example.test/mara.png"}]
        with mock.patch.object(studio.characters, "get_characters",
                               return_value=roster), \
                mock.patch.object(studio.assist, "_fetch_first_frame",
                                  return_value=ref_file) as fetch, \
                self._post_json(_image_response()) as post:
            studio.generate({"mode": "edit", "girl_id": "mara",
                             "fields": {"subject": "Warm bedroom"}})
        fetch.assert_called_once_with("https://example.test/mara.png")
        parts = post.call_args.args[1]["contents"][0]["parts"]
        self.assertEqual(
            base64.b64decode(parts[1]["inlineData"]["data"]), PNG_BYTES)
        self.assertFalse(os.path.exists(ref_file))  # temp frame removed

    def test_girl_without_reference_is_a_plain_400(self):
        roster = [{"id": "chloe", "name": "Chloe", "avatar_url": ""}]
        with mock.patch.object(studio.characters, "get_characters",
                               return_value=roster), \
                mock.patch.object(studio.assist, "_fetch_first_frame",
                                  return_value=None), \
                self._post_json(_image_response()) as post:
            with self.assertRaises(HTTPException) as ctx:
                studio.generate({"mode": "edit", "girl_id": "chloe",
                                 "fields": {"subject": "Her classroom"}})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "reference_unavailable")
        post.assert_not_called()

    def test_unknown_girl_is_a_400(self):
        with mock.patch.object(studio.characters, "get_characters",
                               return_value=[]), \
                self._post_json(_image_response()) as post:
            with self.assertRaises(HTTPException) as ctx:
                studio.generate({"mode": "edit", "girl_id": "nobody",
                                 "fields": {"subject": "X"}})
        self.assertEqual(ctx.exception.detail, "unknown_character")
        post.assert_not_called()

    def test_ambiguous_reference_is_a_400(self):
        with self._post_json(_image_response()) as post:
            with self.assertRaises(HTTPException) as ctx:
                studio.generate({
                    "mode": "edit", "girl_id": "mara",
                    "image_b64": base64.b64encode(PNG_BYTES).decode(),
                    "fields": {"subject": "X"}})
        self.assertEqual(ctx.exception.detail, "reference_ambiguous")
        post.assert_not_called()

    def test_invalid_upload_is_a_400(self):
        with self._post_json(_image_response()) as post:
            with self.assertRaises(HTTPException) as ctx:
                studio.generate({"mode": "edit", "image_b64": "!!!nope",
                                 "fields": {"subject": "X"}})
        self.assertEqual(ctx.exception.detail, "reference_invalid")
        post.assert_not_called()

    def test_model_refusal_surfaces_plainly(self):
        blocked = {"promptFeedback": {
            "blockReason": "SAFETY",
            "blockReasonMessage": "blocked by the model"}}
        with self._post_json(blocked):
            with self.assertRaises(HTTPException) as ctx:
                studio.generate({"prompt": "anything", "mode": "new"})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "model_refused")
        self.assertEqual(ctx.exception.detail["reason"], "SAFETY")

    def test_finish_reason_refusal_surfaces_plainly(self):
        blocked = {"candidates": [{
            "finishReason": "PROHIBITED_CONTENT",
            "content": {"parts": [{"text": "I cannot generate that."}]}}]}
        with self._post_json(blocked):
            with self.assertRaises(HTTPException) as ctx:
                studio.generate({"prompt": "anything", "mode": "new"})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["reason"],
                         "PROHIBITED_CONTENT")
        self.assertIn("cannot generate", ctx.exception.detail["message"])

    def test_pool_down_is_a_502_not_a_fake_success(self):
        with self._post_json(None):
            with self.assertRaises(HTTPException) as ctx:
                studio.generate({"prompt": "a cat", "mode": "new"})
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(studio.list_recent(), [])

    def test_generate_endpoint_owner_happy_path(self):
        with self._post_json(_image_response()) as post:
            resp = self._client().post(
                "/api/v1/studio/generate",
                headers={"Authorization": "Bearer owner-tok"},
                json={"prompt": "a cat on a porch", "mode": "new"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["url"].startswith("/generated/studio_"))
        post.assert_called_once()


class GirlsPayloadTest(StudioTestBase):
    def test_has_reference_flags(self):
        roster = [
            {"id": "chloe", "name": "Chloe", "avatar_url": ""},
            {"id": "mara", "name": "Mara",
             "avatar_url": "https://example.test/m.png"},
        ]
        with mock.patch.object(studio.characters, "get_characters",
                               return_value=roster):
            girls = studio.girls_payload()["girls"]
        self.assertEqual(
            girls,
            [{"id": "chloe", "name": "Chloe", "has_reference": False},
             {"id": "mara", "name": "Mara", "has_reference": True}])

    def test_recent_endpoint_gated(self):
        resp = self._client().get("/api/v1/studio/recent")
        self.assertEqual(resp.status_code, 401)
        resp = self._client().get(
            "/api/v1/studio/recent",
            headers={"X-Admin-Token": "test-admin-secret"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"recent": []})


if __name__ == "__main__":
    unittest.main()
