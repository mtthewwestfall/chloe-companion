"""Tests for custom video requests generation and character chat delivery."""

import unittest
from unittest import mock

from app import assist, characters, config, custom_video, db, store


class CustomVideoTest(unittest.TestCase):
    def setUp(self):
        db.init_db()
        self.conn = db.connect()
        # Seed test user and character
        store.get_or_create_user(self.conn, "test_user_1")
        self.user_id = "test_user_1"
        self.girl_id = "chloe"

    def tearDown(self):
        self.conn.close()

    def test_custom_video_request_creation_and_fulfillment(self):
        # Create custom video request
        req = store.save_custom_request(
            self.conn, self.user_id, self.girl_id, "dancing gracefully in a red dress"
        )
        self.assertEqual(req["status"], "pending")

        # Mock Sogni login session run
        mock_outcome = {
            "status": "done",
            "path": "/tmp/custom_test_video.mp4",
        }
        mock_session = {"token": "tok_123", "refreshToken": "ref_123"}

        with mock.patch.object(
            assist, "stored_sogni_session", return_value=mock_session
        ), mock.patch.object(
            assist, "_sogni_plan_run", return_value=(mock_outcome, None)
        ), mock.patch.object(
            assist, "_fetch_first_frame", return_value=None
        ), mock.patch(
            "os.path.getsize", return_value=1024
        ):
            custom_video.run_custom_generation_with_login(req["id"])

        # Check request status is updated to fulfilled
        row = self.conn.execute(
            "SELECT * FROM custom_video_requests WHERE id = ?", (req["id"],)
        ).fetchone()
        self.assertEqual(row["status"], "fulfilled")
        self.assertIn("custom_test_video.mp4", row["video_url"])

        # Check turn logged in conversation with video attachment
        turns = store.recent_turns(
            self.conn, self.user_id, limit=5, character_id=self.girl_id
        )
        self.assertTrue(len(turns) > 0)
        last_turn = turns[-1]
        self.assertEqual(last_turn["role"], self.girl_id)
        self.assertIn("I made that special video for you", last_turn["text"])
        self.assertTrue(len(last_turn["attachments"]) > 0)
        att = last_turn["attachments"][0]
        self.assertEqual(att["kind"], "video")
        self.assertIn("custom_test_video.mp4", att["url"])

    def test_custom_video_run_generation_api_key_path(self):
        req = store.save_custom_request(
            self.conn, self.user_id, self.girl_id, "smiling at sunset"
        )

        async def dummy_async(*args, **kwargs):
            return "/generated/custom_123.mp4", 0.12

        with mock.patch(
            "asyncio.run", side_effect=lambda coro: ("/generated/custom_123.mp4", 0.12)
        ), mock.patch("os.path.getsize", return_value=2048):
            custom_video.run_custom_generation(req["id"])

        row = self.conn.execute(
            "SELECT * FROM custom_video_requests WHERE id = ?", (req["id"],)
        ).fetchone()
        self.assertEqual(row["status"], "fulfilled")
        self.assertEqual(row["video_url"], "/generated/custom_123.mp4")

        turns = store.recent_turns(
            self.conn, self.user_id, limit=5, character_id=self.girl_id
        )
        self.assertTrue(len(turns) > 0)
        last_turn = turns[-1]
        self.assertEqual(last_turn["role"], self.girl_id)
        self.assertEqual(last_turn["attachments"][0]["kind"], "video")
        self.assertEqual(
            last_turn["attachments"][0]["url"], "/generated/custom_123.mp4"
        )


if __name__ == "__main__":
    unittest.main()
