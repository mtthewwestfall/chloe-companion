"""Reference fetching for app-relative media URLs (2026-10-14 fix).

Production failure: a stage>2 live give asked give_gen for the
topless reference frame; Brooklyn's latest stage-2 girl_media row is
an owner upload whose url is APP-RELATIVE (/generated/give-upload-…).
assist._fetch_first_frame handed that raw path to
media_gen._fetch_reference, whose _download feeds it straight to
urllib — which rejects any scheme-less URL ("unknown url type") on
every attempt. The localhost self-request rescue in
_fetch_first_frame never produced a frame either, so the topless
reference silently vanished and the render fell back to the avatar.

The fix lives in media_gen._fetch_reference: an app-relative URL is
resolved there — the on-disk file this process serves (copied to a
temp file; callers delete what they are handed, the library original
must survive), else an absolute URL built from config.PUBLIC_APP_URL.
Absolute URLs are untouched. The download layer is mocked
throughout — no network."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, config, db, give_gen, media_gen  # noqa: E402

STAGE2_NAME = "give-upload-brooklyn-stage2-719e8c49.mp4"
STAGE2_URL = "/generated/" + STAGE2_NAME
VIDEO_BYTES = b"\x00\x01fake-mp4" + b"x" * 4096


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


def _no_network(self, url, dest, timeout=300):
    raise AssertionError(f"download layer must not be used here: {url}")


class GeneratedRelativeUrlTest(unittest.TestCase):
    def setUp(self):
        self.gen_dir = tempfile.mkdtemp(prefix="ref-gen-")
        self.src = os.path.join(self.gen_dir, STAGE2_NAME)
        with open(self.src, "wb") as f:
            f.write(VIDEO_BYTES)
        self._patch = mock.patch.object(config, "GENERATED_DIR",
                                        self.gen_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_generated_url_resolves_to_local_copy(self):
        with mock.patch.object(media_gen, "_download", _no_network):
            path = media_gen._fetch_reference(STAGE2_URL)
        self.assertIsNotNone(path)
        self.assertNotEqual(os.path.abspath(path),
                            os.path.abspath(self.src))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), VIDEO_BYTES)
        # the library original survives — callers delete our return
        self.assertTrue(os.path.isfile(self.src))
        os.unlink(path)

    def test_fetch_first_frame_stage2_url_returns_frame_source(self):
        # The exact production entry point: assist._fetch_first_frame
        # on the owner-upload stage-2 URL.
        with mock.patch.object(media_gen, "_download", _no_network):
            path = assist._fetch_first_frame(STAGE2_URL)
        self.assertIsNotNone(path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), VIDEO_BYTES)
        os.unlink(path)

    def test_missing_generated_file_downloads_from_public_base(self):
        seen = {}

        def fake_download(url, dest, timeout=300):
            seen["url"] = url
            with open(dest, "wb") as f:
                f.write(VIDEO_BYTES)

        missing = "/generated/give-upload-brooklyn-stage2-00000000.mp4"
        with mock.patch.object(media_gen, "_download", fake_download):
            path = media_gen._fetch_reference(missing)
        self.assertEqual(seen["url"], config.PUBLIC_APP_URL + missing)
        self.assertIsNotNone(path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), VIDEO_BYTES)
        os.unlink(path)


class MediaRelativeUrlTest(unittest.TestCase):
    def test_media_url_resolves_to_local_copy(self):
        root = tempfile.mkdtemp(prefix="ref-root-")
        data_dir = os.path.join(root, "chloe-companion-server")
        media_dir = os.path.join(root, "chloe-companion-app", "media")
        os.makedirs(data_dir)
        os.makedirs(media_dir)
        src = os.path.join(media_dir, "brooklyn-give-stage2.mp4")
        with open(src, "wb") as f:
            f.write(VIDEO_BYTES)
        with mock.patch.object(config, "DATA_DIR", data_dir), \
                mock.patch.object(media_gen, "_download", _no_network):
            path = media_gen._fetch_reference(
                "/media/brooklyn-give-stage2.mp4")
        self.assertIsNotNone(path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), VIDEO_BYTES)
        self.assertTrue(os.path.isfile(src))
        os.unlink(path)


class AbsoluteUrlUnchangedTest(unittest.TestCase):
    def test_absolute_url_goes_straight_to_download(self):
        seen = {}

        def fake_download(url, dest, timeout=300):
            seen["url"] = url
            with open(dest, "wb") as f:
                f.write(VIDEO_BYTES)

        with mock.patch.object(media_gen, "_download", fake_download):
            path = media_gen._fetch_reference("https://example.com/a.jpg")
        self.assertEqual(seen["url"], "https://example.com/a.jpg")
        self.assertIsNotNone(path)
        os.unlink(path)

    def test_traversal_outside_served_dirs_is_not_local(self):
        gen_dir = tempfile.mkdtemp(prefix="ref-gen-")
        with mock.patch.object(config, "GENERATED_DIR", gen_dir):
            self.assertIsNone(
                media_gen._local_media_file("/generated/../secret.txt"))
            self.assertIsNone(
                media_gen._local_media_file("/media/../../etc/passwd"))
            self.assertIsNone(
                media_gen._local_media_file("https://example.com/x.jpg"))


class ToplessReferenceChainTest(unittest.TestCase):
    """The full production chain: girl_media stage-2 row with the
    app-relative owner-upload URL -> _topless_reference_frame ->
    _fetch_first_frame -> local copy -> ffmpeg last frame."""

    def setUp(self):
        self.conn = _db()
        self.gen_dir = tempfile.mkdtemp(prefix="ref-gen-")
        self.src = os.path.join(self.gen_dir, STAGE2_NAME)
        with open(self.src, "wb") as f:
            f.write(VIDEO_BYTES)
        self.conn.execute(
            "INSERT INTO girl_media (girl_id, kind, url, preset, stage) "
            "VALUES ('brooklyn', 'give', ?, 'give', 2)", (STAGE2_URL,))
        self.conn.commit()
        self._patch = mock.patch.object(config, "GENERATED_DIR",
                                        self.gen_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.conn.close()

    def test_stage2_relative_url_yields_extracted_frame(self):
        def fake_run(argv, **kwargs):
            with open(argv[-1], "wb") as f:
                f.write(b"extracted-frame-bytes")
            return mock.Mock(returncode=0)

        with mock.patch.object(media_gen, "_download", _no_network), \
                mock.patch.object(assist, "_ffmpeg_exe",
                                  return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(give_gen.subprocess, "run", fake_run):
            frame = give_gen._topless_reference_frame(self.conn,
                                                      "brooklyn")
        self.assertIsNotNone(frame)
        with open(frame, "rb") as f:
            self.assertEqual(f.read(), b"extracted-frame-bytes")
        # the library upload itself was NOT consumed by the chain
        self.assertTrue(os.path.isfile(self.src))
        os.unlink(frame)


if __name__ == "__main__":
    unittest.main()
