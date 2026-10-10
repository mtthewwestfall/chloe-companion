import sqlite3
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app import assist, config, db, give_gen
from app import main as api


def _db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db._migrate_character_columns(conn)
    db._migrate_auth_columns(conn)
    db._migrate_submission_columns(conn)
    db._migrate_creator_columns(conn)
    db._migrate_owner_column(conn)
    db._migrate_referral_columns(conn)
    db._migrate_conversation_attachments(conn)
    db._migrate_girl_media_stage(conn)
    conn.executescript(db.SCHEMA)
    return conn


class GiveGenUnitTest(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def tearDown(self):
        self.conn.close()

    def test_sogni_plan_params_duration(self):
        params_default = assist._sogni_plan_params("test prompt")
        self.assertEqual(params_default["duration"], 6)

        params_15s = assist._sogni_plan_params("test prompt", duration=15)
        self.assertEqual(params_15s["duration"], 15)

    def test_create_and_list_give_generations(self):
        gen_id = give_gen.create_give_generation(
            self.conn, "chloe", 1, "lift tease prompt", "2026-10-10", status="pending"
        )
        self.assertIsInstance(gen_id, int)

        item = give_gen.get_give_generation(self.conn, gen_id)
        self.assertIsNotNone(item)
        self.assertEqual(item["girl_id"], "chloe")
        self.assertEqual(item["stage"], 1)
        self.assertEqual(item["stage_name"], "lift tease")
        self.assertEqual(item["status"], "pending")

        items = give_gen.list_give_generations(self.conn, status="pending", girl_id="chloe")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], gen_id)

    def test_approve_give_generation(self):
        gen_id = give_gen.create_give_generation(
            self.conn, "chloe", 2, "shirt off prompt", "2026-10-10", status="pending"
        )
        give_gen.update_give_generation_status(self.conn, gen_id, "pending", url="/generated/give_gen_1.mp4")

        approved = give_gen.approve_give_generation(self.conn, gen_id)
        self.assertIsNotNone(approved)
        self.assertEqual(approved["status"], "approved")

        # Verify insertion into girl_media
        media_row = self.conn.execute(
            "SELECT * FROM girl_media WHERE girl_id = 'chloe' AND url = '/generated/give_gen_1.mp4'"
        ).fetchone()
        self.assertIsNotNone(media_row)
        self.assertEqual(media_row["kind"], "give")
        self.assertEqual(media_row["stage"], 2)

    def test_delete_give_generation(self):
        gen_id = give_gen.create_give_generation(
            self.conn, "chloe", 3, "boob grabs prompt", "2026-10-10", status="pending"
        )
        deleted = give_gen.delete_give_generation(self.conn, gen_id)
        self.assertIsNotNone(deleted)
        self.assertEqual(deleted["status"], "deleted")


class GiveGenAdminApiTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)
        self.admin_token = "test_admin_token_123"
        self.patcher = mock.patch.object(config, "ADMIN_TOKEN", self.admin_token)
        self.patcher.start()
        self.headers = {"X-Admin-Token": self.admin_token}

    def tearDown(self):
        self.patcher.stop()

    @mock.patch("app.main._conn")
    def test_admin_list_and_manage_give_generations(self, mock_conn):
        conn = _db()
        mock_c = mock.MagicMock(wraps=conn)
        mock_c.close = mock.MagicMock()
        mock_conn.return_value = mock_c

        gen_id = give_gen.create_give_generation(
            conn, "mara", 1, "test prompt", "2026-10-10", status="pending"
        )
        give_gen.update_give_generation_status(conn, gen_id, "pending", url="/generated/test.mp4")

        # List
        res = self.client.get("/api/v1/admin/give-generations?status=pending", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("generations", data)
        self.assertEqual(len(data["generations"]), 1)

        # Approve
        res_app = self.client.post(f"/api/v1/admin/give-generations/{gen_id}/approve", headers=self.headers)
        self.assertEqual(res_app.status_code, 200)
        self.assertTrue(res_app.json()["ok"])

        # Delete
        gen_id2 = give_gen.create_give_generation(
            conn, "mara", 2, "test prompt 2", "2026-10-10", status="pending"
        )
        res_del = self.client.post(f"/api/v1/admin/give-generations/{gen_id2}/delete", headers=self.headers)
        self.assertEqual(res_del.status_code, 200)
        self.assertTrue(res_del.json()["ok"])

    @mock.patch("app.main._conn")
    @mock.patch("app.assist.stored_sogni_session")
    @mock.patch("app.give_gen.trigger_daily_give_generations")
    def test_admin_trigger_daily_give_generations(self, mock_trigger, mock_sogni_session, mock_conn):
        conn = _db()
        mock_c = mock.MagicMock(wraps=conn)
        mock_c.close = mock.MagicMock()
        mock_conn.return_value = mock_c
        mock_sogni_session.return_value = {"token": "fake_token", "refreshToken": "fake_refresh"}
        mock_trigger.return_value = [1, 2, 3]

        res = self.client.post("/api/v1/admin/give-generations/generate-daily", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["queued"], 3)

    def test_male_stage_names_and_descriptions(self):
        self.assertEqual(give_gen.stage_name_for("jake", 1), "shirt lift tease")
        self.assertEqual(give_gen.stage_name_for("eli", 6), "strokes himself")
        self.assertEqual(give_gen.stage_name_for("jake", 9), "fucking")
        for stage in range(1, 10):
            desc = give_gen.stage_description_for("eli", stage)
            for w in ("her ", "herself", "breasts", "titty"):
                self.assertNotIn(w, desc.lower(), f"stage {stage}: {desc!r}")
        self.assertIn("his cock", give_gen.stage_description_for("jake", 6))

    def test_female_stages_unchanged(self):
        self.assertEqual(give_gen.stage_name_for("mara", 3), "boob grabs")
        self.assertEqual(give_gen.stage_name_for("chloe", 6), "plays w/ herself")
        self.assertEqual(
            give_gen.stage_description_for("chloe", 1),
            "lift tease, lifting her shirt playfully, teasing smile",
        )
        self.assertEqual(
            give_gen.stage_description_for("mara", 7),
            "titty fuck, intimate chest scene, close up",
        )

    @mock.patch("app.give_gen._process_queued_give_generations")
    @mock.patch("app.give_gen.characters.get_characters")
    @mock.patch("app.give_gen.db.connect")
    def test_trigger_daily_uses_male_prompts_for_guys(
        self, mock_connect, mock_chars, mock_process
    ):
        mock_connect.return_value = self.conn
        mock_chars.return_value = [
            {"id": "jake", "name": "Jake"},
            {"id": "mara", "name": "Mara"},
        ]
        queued = give_gen.trigger_daily_give_generations()
        self.assertEqual(len(queued), 18)
        rows = self.conn.execute(
            "SELECT girl_id, stage, prompt FROM give_generations"
        ).fetchall()
        jake_prompts = [r["prompt"] for r in rows if r["girl_id"] == "jake"]
        mara_prompts = [r["prompt"] for r in rows if r["girl_id"] == "mara"]
        self.assertEqual(len(jake_prompts), 9)
        self.assertEqual(len(mara_prompts), 9)
        for p in jake_prompts:
            self.assertNotIn("titty fuck", p)
            self.assertNotIn("breasts", p)
            self.assertNotIn("herself", p)
        for p in mara_prompts:
            self.assertIn("her", p)
        # stage names on the listed rows are gender-aware too
        gens = give_gen.list_give_generations(self.conn, girl_id="jake")
        self.assertEqual(gens[0]["stage_name"],
                         give_gen.MALE_STAGE_NAMES[gens[0]["stage"]])

    def test_approved_reference_frame_preferred(self):
        with mock.patch("os.path.exists", return_value=True):
            path = give_gen._approved_reference_frame("mara")
        self.assertTrue(path.endswith("mara-topless.jpg"))
        with mock.patch("os.path.exists", return_value=False):
            self.assertIsNone(give_gen._approved_reference_frame("mara"))

    def _ok_run(self):
        m = mock.MagicMock()
        m.returncode = 0
        return m

    def test_topless_reference_frame_missing_without_stage2(self):
        self.assertIsNone(give_gen._topless_reference_frame(self.conn, "jake"))

    @mock.patch("app.give_gen.assist._ffmpeg_exe", return_value="/fake/ffmpeg")
    @mock.patch("app.give_gen.assist._fetch_first_frame",
                return_value="/tmp/fake-stage2.mp4")
    def test_topless_reference_frame_uses_stage2_last_frame(
        self, mock_fetch, mock_exe
    ):
        self.conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset, stage) "
            "VALUES ('jake', 'give', 'http://x/jake-stage2.mp4', 'give', 2)"
        )
        self.conn.commit()
        with mock.patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-frame.jpg")), \
             mock.patch("os.close"), mock.patch("os.unlink"), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.path.getsize", return_value=1234), \
             mock.patch("subprocess.run",
                        return_value=self._ok_run()) as mock_run:
            path = give_gen._topless_reference_frame(self.conn, "jake")
        self.assertEqual(path, "/tmp/fake-frame.jpg")
        mock_fetch.assert_called_once_with("http://x/jake-stage2.mp4")
        args = mock_run.call_args[0][0]
        self.assertIn("-sseof", args)

    @mock.patch("app.give_gen.assist._ffmpeg_exe", return_value="/fake/ffmpeg")
    @mock.patch("app.give_gen.assist._fetch_first_frame",
                return_value="/tmp/fake-stage2.mp4")
    def test_topless_reference_frame_none_when_grab_fails(
        self, mock_fetch, mock_exe
    ):
        bad = mock.MagicMock()
        bad.returncode = 1
        self.conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset, stage) "
            "VALUES ('eli', 'give', 'http://x/eli-stage2.mp4', 'give', 2)"
        )
        self.conn.commit()
        with mock.patch("tempfile.mkstemp", return_value=(99, "/tmp/fake-frame.jpg")), \
             mock.patch("os.close"), mock.patch("os.unlink"), \
             mock.patch("subprocess.run", return_value=bad):
            self.assertIsNone(give_gen._topless_reference_frame(self.conn, "eli"))


if __name__ == "__main__":
    unittest.main()
