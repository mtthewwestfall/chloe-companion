"""Admin Sogni image-render tests (2026-10-17).

Covers POST /api/v1/admin/image-renders — the admin write path that
runs ONE plan-session image render (an arbitrary prompt plus a girl's
face-lock reference or an owner-uploaded reference) through assist's
locked seam, the Her Day way. The session lookup, the reference fetch
and the seam itself are mocked throughout: no Sogni session is ever
touched here.

Run:  python -m unittest discover -s tests -v   (from the project root)
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import characters, config, db  # noqa: E402
from app import main as api  # noqa: E402

ADMIN_ATTR = "ADMIN" + chr(95) + "TOKEN"
PROMPT = "her bending over toward the camera, showing a lot of cleavage"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


def _done(path):
    return ({"status": "done", "project_id": "proj-1", "path": path,
             "detail": "billingMode=subscription; project completed"},
            None)


class AdminImageRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.db_path)
        self._real_conn = api._conn
        api._conn = lambda: db.connect(self.db_path)  # noqa: E731
        self._real_db_path = config.DB_PATH
        config.DB_PATH = self.db_path
        self._real_admin = getattr(config, ADMIN_ATTR)
        setattr(config, ADMIN_ATTR, "test-admin-secret")
        characters._cache = {"ts": 0.0, "chars": []}
        self.out_path = os.path.join(self.tmp.name, "sogni-img-test.jpg")
        with open(self.out_path, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 64)
        api._admin_create_girl({"id": "nova", "name": "Nova"})

    def tearDown(self):
        api._conn = self._real_conn
        config.DB_PATH = self._real_db_path
        setattr(config, ADMIN_ATTR, self._real_admin)
        characters._cache = {"ts": 0.0, "chars": []}
        self.tmp.cleanup()

    def _req(self, tok="test-admin-secret"):
        return _Req({"x-admin-token": tok} if tok else {})

    def _post(self, prompt=PROMPT, girl_id=None, image_b64=None,
              tok="test-admin-secret"):
        body = api.ImageRenderIn(prompt=prompt, girl_id=girl_id,
                                 image_b64=image_b64)
        return api.admin_create_image_render(body, self._req(tok))

    def _session(self):
        return {"token": "tok", "refreshToken": "ref"}

    # -- guard ----------------------------------------------------------
    def test_post_rejects_wrong_token(self):
        with mock.patch.object(api.assist,
                               "_sogni_plan_image_run") as run:
            with self.assertRaises(Exception) as ctx:
                self._post(tok="nope")
        self.assertEqual(ctx.exception.status_code, 401)
        run.assert_not_called()

    # -- happy paths ------------------------------------------------------
    def test_uploaded_reference_render(self):
        seen = {}

        def fake_run(session, prompt, ref_path):
            with open(ref_path, "rb") as f:
                seen["ref_bytes"] = f.read()
            seen["ref_path"] = ref_path
            seen["prompt"] = prompt
            return _done(self.out_path)

        b64 = base64.b64encode(PNG_BYTES).decode()
        with mock.patch.object(api.assist, "stored_sogni_session",
                               return_value=self._session()), \
                mock.patch.object(api.assist, "_sogni_plan_image_run",
                                  side_effect=fake_run):
            res = self._post(image_b64=b64)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["url"], "/generated/sogni-img-test.jpg")
        self.assertEqual(res["project_id"], "proj-1")
        self.assertEqual(seen["ref_bytes"], PNG_BYTES)
        self.assertEqual(seen["prompt"], PROMPT)
        # The uploaded temp reference is cleaned up after the render.
        self.assertFalse(os.path.exists(seen["ref_path"]))

    def test_girl_reference_render(self):
        ref_tmp = os.path.join(self.tmp.name, "nova.ref")
        with open(ref_tmp, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 2048)
        roster = [{"id": "nova", "name": "Nova",
                   "avatar_url": "/img/nova-avatar.jpg"}]
        with mock.patch.object(api.characters, "get_characters",
                               return_value=roster), \
                mock.patch.object(api.assist, "stored_sogni_session",
                                  return_value=self._session()), \
                mock.patch.object(api.assist, "_fetch_first_frame",
                                  return_value=ref_tmp) as fetch, \
                mock.patch.object(api.assist, "_sogni_plan_image_run",
                                  return_value=_done(self.out_path)) as run:
            res = self._post(girl_id="nova")
        self.assertTrue(res["ok"])
        fetch.assert_called_once_with("/img/nova-avatar.jpg")
        self.assertEqual(run.call_args[0][1], PROMPT)
        self.assertEqual(run.call_args[0][2], ref_tmp)

    def test_prompt_only_render_passes_no_reference(self):
        with mock.patch.object(api.assist, "stored_sogni_session",
                               return_value=self._session()), \
                mock.patch.object(api.assist, "_sogni_plan_image_run",
                                  return_value=_done(self.out_path)) as run:
            res = self._post()
        self.assertTrue(res["ok"])
        self.assertIsNone(run.call_args[0][2])

    # -- validation -------------------------------------------------------
    def test_empty_prompt_rejected(self):
        with mock.patch.object(api.assist,
                               "_sogni_plan_image_run") as run:
            with self.assertRaises(Exception) as ctx:
                self._post(prompt="   ")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "prompt_required")
        run.assert_not_called()

    def test_unknown_girl_rejected(self):
        with mock.patch.object(api.assist,
                               "_sogni_plan_image_run") as run:
            with self.assertRaises(Exception) as ctx:
                self._post(girl_id="ghost")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "unknown_character")
        run.assert_not_called()

    def test_girl_without_reference_rejected(self):
        roster = [{"id": "nova", "name": "Nova", "avatar_url": ""}]
        with mock.patch.object(api.characters, "get_characters",
                               return_value=roster), \
                mock.patch.object(api.assist, "_fetch_first_frame",
                                  return_value=None), \
                mock.patch.object(api.assist,
                                  "_sogni_plan_image_run") as run:
            with self.assertRaises(Exception) as ctx:
                self._post(girl_id="nova")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "reference_unavailable")
        run.assert_not_called()

    def test_invalid_upload_rejected(self):
        with mock.patch.object(api.assist,
                               "_sogni_plan_image_run") as run:
            with self.assertRaises(Exception) as ctx:
                self._post(image_b64="!!! not base64 !!!")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "reference_invalid")
        run.assert_not_called()

    def test_both_references_rejected(self):
        b64 = base64.b64encode(PNG_BYTES).decode()
        with mock.patch.object(api.assist,
                               "_sogni_plan_image_run") as run:
            with self.assertRaises(Exception) as ctx:
                self._post(girl_id="nova", image_b64=b64)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "reference_ambiguous")
        run.assert_not_called()

    # -- session + outcome handling ---------------------------------------
    def test_no_plan_session_is_503_and_never_renders(self):
        with mock.patch.object(api.assist, "stored_sogni_session",
                               return_value=None), \
                mock.patch.object(api.assist,
                                  "_sogni_plan_image_run") as run:
            with self.assertRaises(Exception) as ctx:
                self._post()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail,
                         "sogni_session_unavailable")
        run.assert_not_called()

    def test_api_key_session_is_503_and_never_renders(self):
        # The dashboard API key is the paid path, not a plan session.
        with mock.patch.object(api.assist, "stored_sogni_session",
                               return_value={"api_key": True,
                                             "token": "k"}), \
                mock.patch.object(api.assist,
                                  "_sogni_plan_image_run") as run:
            with self.assertRaises(Exception) as ctx:
                self._post()
        self.assertEqual(ctx.exception.status_code, 503)
        run.assert_not_called()

    def test_failed_render_surfaces_502_with_status(self):
        failed = ({"status": "blocked", "project_id": "p2", "path": None,
                   "detail": "5000: Sensitive content detected"}, None)
        with mock.patch.object(api.assist, "stored_sogni_session",
                               return_value=self._session()), \
                mock.patch.object(api.assist, "_sogni_plan_image_run",
                                  return_value=failed):
            with self.assertRaises(Exception) as ctx:
                self._post()
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("blocked", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
