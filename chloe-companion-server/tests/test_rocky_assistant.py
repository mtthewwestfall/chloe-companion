"""Rocky, the owner's main agent as a character (2026-10-09).

Rocky is private + owner-only and sits ABOVE the three project
lanes, not in a fourth one: Hannah carries Roof Rover, Mia carries
Whispr, Jordan carries OneFetch, and the lane machinery keys off
character_lane() — which returns None for Rocky, so every
cross-lane redirect exempts him. He gets the full assistant suite
across ALL projects, plus one tool the girls don't have: page
reading (open/read/check a URL, server-side fetch, SSRF-guarded,
20k-char cap, honest failures).

All network seams are mocked — zero real calls, zero real fetches.

Run:  python -m unittest tests.test_rocky_assistant -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import assist, db, store  # noqa: E402

OWNER = {"user_id": "owner_1", "tier": "basic"}
STRANGER = {"user_id": "someone_else", "tier": "vip"}
TOKENS = {"token": "tok-aaa", "refreshToken": "ref-bbb"}
SECRET = "rocky-tests-secret"

FAKE_STATUS = {"onefetch": {"founder_count": 3, "founder_spots_left": 197,
                            "founder_open": True, "ai_available": True,
                            "site_status": 200},
               "roof_rover": {"shelf_leads": 5, "site_status": 200},
               "gemini": {"believed_healthy": 2,
                          "pool": {"healthy": 2}, "degraded": False}}

PAGE_HTML = (
    b"<html><head><title>t</title><style>body{color:red}</style>"
    b"<script>var secret = 'script-noise';</script></head><body>"
    b"<nav>NAV NOISE Home Products</nav>"
    b"<h1>Real Heading</h1><p>The launch price is $19.99 a month.</p>"
    b"<p>Second paragraph with the actual facts.</p>"
    b"<footer>FOOTER NOISE</footer></body></html>")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    assist.ensure_tables(conn)
    return conn


class RockyBase(unittest.TestCase):
    def setUp(self):
        self.old_env = {k: os.environ.get(k) for k in
                        ("ASSIST_ROLLOUT", "ASSIST_OWNER_IDS",
                         "DEV_AGENT_SECRET")}
        os.environ["ASSIST_ROLLOUT"] = "owner"
        os.environ["ASSIST_OWNER_IDS"] = "owner_1"
        os.environ["DEV_AGENT_SECRET"] = SECRET
        self.conn = _conn()
        store.upsert_local_character(
            self.conn, {"id": "rocky", "name": "Rocky",
                        "owner_user_id": "owner_1",
                        "companion_voice": "rex"})

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.conn.close()
        assist._PAGE_CACHE.clear()

    def say(self, text, user=OWNER, cid="rocky"):
        return assist.maybe_assist(self.conn, user, text, cid)


class SetAndLaneTest(RockyBase):
    def test_rocky_in_assistant_set_without_a_lane(self):
        self.assertIn("rocky", assist.ASSISTANT_CHARACTERS)
        self.assertTrue(assist.is_assistant_character("rocky"))
        self.assertIsNone(assist.character_lane("rocky"))
        self.assertIsNone(assist._lane_repo_block(
            "rocky", "mtthewwestfall/roof-scout"))

    def test_status_redirect_exempts_rocky(self):
        self.assertIsNone(
            assist._status_lane_redirect("rocky", "how's onefetch doing?"))
        self.assertIsNone(
            assist._status_lane_redirect("rocky", "how's roof rover?"))

    def test_brief_covers_all_projects_and_rules(self):
        brief = assist._owner_brief_reply("rocky")
        self.assertTrue(brief.endswith(assist._ROCKY_BRIEF))
        for needle in ("$4.99", "$9.99", "$19.99", "$29.99",
                       "Roof Rover", "Whispr", "OneFetch", "Hannah",
                       "Mia", "Jordan", "Winston", "$3.00",
                       "credits", "free Gemini"):
            self.assertIn(needle, brief, needle)
        self.assertIn("want me to", assist._ROCKY_BRIEF)  # banned closer named
        catch_up = self.say("catch me up")
        self.assertIn("$19.99", catch_up["brief"])


class DevAndStatusTest(RockyBase):
    def _queued_repos(self):
        return [r["repo"] for r in self.conn.execute(
            "SELECT repo FROM dev_jobs ORDER BY id").fetchall()]

    def test_rocky_queues_builds_in_every_lane(self):
        res = self.say("use your dev bot to fix the login bug in the "
                       "roof-scout repo")
        self.assertIn("queued build job #", res["brief"])
        res = self.say("use your dev bot to fix the roster page in "
                       "the chloe-companion repo")
        self.assertIn("queued build job #", res["brief"])
        res = self.say("use your dev bot to fix the signup in the "
                       "realtor-app repo")
        self.assertIn("queued build job #", res["brief"])
        self.assertEqual(self._queued_repos(),
                         ["mtthewwestfall/roof-scout",
                          "mtthewwestfall/chloe-companion",
                          "mtthewwestfall/realtor-app"])

    def test_rocky_review_passes_the_lane_gate(self):
        # No GitHub token in the test DB: the honest no-key brief
        # proves the ask got past routing (no redirect, no block).
        res = self.say("review the roof-scout repo")
        self.assertIn("don't have a GitHub key", res["brief"])
        res = self.say("review the realtor-app repo")
        self.assertIn("don't have a GitHub key", res["brief"])

    def test_status_spans_all_projects(self):
        with mock.patch.object(assist, "fetch_app_status",
                               return_value=dict(FAKE_STATUS)):
            res = self.say("how's everything doing?")
        self.assertIsNotNone(res)
        self.assertIn("3 founders", res["brief"])
        self.assertIn("5 leads on the $4.99 store shelf", res["brief"])
        self.assertNotIn("that's", res["brief"][:60])  # no redirect lead-in

    def test_stranger_gets_nothing_in_rockys_thread(self):
        self.assertIsNone(self.say("how's everything doing?",
                                   user=STRANGER))
        self.assertIsNone(self.say(
            "use your dev bot to fix the roof-scout repo", user=STRANGER))
        self.assertIsNone(self.say("make a video of you waving",
                                   user=STRANGER))

    def test_girls_lane_behavior_unchanged(self):
        res = self.say("how's onefetch doing?", cid="hannah")
        self.assertTrue(
            res["brief"].startswith(assist.FIXED_REPLY_PREFIX))
        self.assertIn("that's jordan's project", res["brief"])
        res = self.say("use your dev bot to fix the realtor-app repo",
                       cid="mia")
        self.assertIn("that's jordan's project", res["brief"])


class SogniParityTest(RockyBase):
    def test_sogni_mention_without_session_asks_for_login(self):
        res = self.say("rocky, make a video of you waving")
        self.assertIsNotNone(res)
        self.assertIn("sogni login", res["brief"])
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sogni_plan_jobs").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_video_ask_with_session_enqueues_as_rocky_on_plan(self):
        assist.store_sogni_session(self.conn, TOKENS)
        spawned = []
        with mock.patch.object(assist, "_spawn_sogni_plan_job",
                               lambda job_id: spawned.append(job_id)):
            res = self.say("make a video of you waving at the depot")
        self.assertIn("rendering", res["brief"])
        self.assertEqual(len(spawned), 1)
        row = self.conn.execute(
            "SELECT character_id, billing_mode FROM sogni_plan_jobs"
        ).fetchone()
        self.assertEqual(row["character_id"], "rocky")
        self.assertEqual(row["billing_mode"], "subscription")


class PageReadTriggerTest(RockyBase):
    def test_url_trigger_shapes(self):
        self.assertEqual(
            assist.detect_page_read("open this https://example.com/x"),
            {"url": "https://example.com/x"})
        self.assertEqual(
            assist.detect_page_read("read https://example.com/a?b=1."),
            {"url": "https://example.com/a?b=1"})
        self.assertEqual(
            assist.detect_page_read("https://example.com/page — what's this?"),
            {"url": "https://example.com/page"})
        self.assertEqual(
            assist.detect_page_read("what's on example.com today?"),
            {"url": "https://example.com"})
        self.assertEqual(
            assist.detect_page_read("rocky, check tryroofrover.com please"),
            {"url": "https://tryroofrover.com"})

    def test_non_page_asks_do_not_trigger(self):
        self.assertIsNone(assist.detect_page_read("read my mind"))
        self.assertIsNone(assist.detect_page_read("check the shelf count"))
        self.assertIsNone(assist.detect_page_read("what's on tv tonight"))
        self.assertIsNone(assist.detect_page_read(""))


class PageReadFetchTest(RockyBase):
    def _fetcher(self, mapping, calls=None):
        def fake(url, timeout=assist._PAGE_FETCH_TIMEOUT_S):
            if calls is not None:
                calls.append(url)
            if url not in mapping:
                raise AssertionError(f"unexpected fetch: {url}")
            return mapping[url]
        return fake

    def test_reads_extracts_and_names_the_url(self):
        with mock.patch.object(
                assist, "_fetch_url_once",
                self._fetcher({"https://example.com/x":
                               (200, {}, PAGE_HTML)})), \
                mock.patch.object(
                    assist, "_public_url_guard", return_value=None):
            res = self.say("open this https://example.com/x and tell "
                           "me the price")
        self.assertIsNotNone(res)
        self.assertIn("https://example.com/x", res["brief"])
        self.assertIn("Real Heading", res["brief"])
        self.assertIn("$19.99", res["brief"])
        self.assertNotIn("script-noise", res["brief"])
        self.assertNotIn("NAV NOISE", res["brief"])
        self.assertNotIn("FOOTER NOISE", res["brief"])

    def test_char_cap_is_honored(self):
        big = (200, {}, b"<p>" + b"x" * 50000 + b"</p>")
        with mock.patch.object(
                assist, "_fetch_url_once",
                self._fetcher({"https://example.com/big": big})), \
                mock.patch.object(
                    assist, "_public_url_guard", return_value=None):
            got = assist._read_page("https://example.com/big")
        self.assertTrue(got["ok"])
        self.assertLessEqual(len(got["text"]), 20000)

    def test_cache_one_fetch_per_url_within_ttl(self):
        calls = []
        fake = self._fetcher(
            {"https://example.com/x": (200, {}, PAGE_HTML)}, calls)
        with mock.patch.object(assist, "_fetch_url_once", fake), \
                mock.patch.object(
                    assist, "_public_url_guard", return_value=None):
            assist._read_page("https://example.com/x")
            assist._read_page("https://example.com/x")
        self.assertEqual(len(calls), 1)

    def test_redirect_target_is_revalidated(self):
        mapping = {"https://example.com/go":
                   (302, {"Location": "http://127.0.0.1/admin"}, b"")}
        guard_calls = []

        def guard(url):
            guard_calls.append(url)
            if "127.0.0.1" in url:
                return "that host resolves to a non-public address"
            return None

        with mock.patch.object(
                assist, "_fetch_url_once", self._fetcher(mapping)), \
                mock.patch.object(assist, "_public_url_guard", guard):
            got = assist._read_page("https://example.com/go")
        self.assertFalse(got["ok"])
        self.assertIn("non-public", got["reason"])
        self.assertEqual(guard_calls[-1], "http://127.0.0.1/admin")

    def test_failure_is_honest_never_invented(self):
        def boom(url, timeout=assist._PAGE_FETCH_TIMEOUT_S):
            raise TimeoutError("timed out")

        with mock.patch.object(assist, "_fetch_url_once", boom), \
                mock.patch.object(
                    assist, "_public_url_guard", return_value=None):
            res = self.say("read https://example.com/slow for me")
        self.assertIsNotNone(res)
        self.assertIn("couldn't read that page", res["brief"])
        self.assertIn("NEVER invent", res["brief"])

    def test_botwall_status_is_honest(self):
        mapping = {"https://example.com/wall": (403, {}, b"denied")}
        with mock.patch.object(
                assist, "_fetch_url_once", self._fetcher(mapping)), \
                mock.patch.object(
                    assist, "_public_url_guard", return_value=None):
            got = assist._read_page("https://example.com/wall")
        self.assertFalse(got["ok"])
        self.assertIn("HTTP 403", got["reason"])

    def test_girls_threads_have_no_page_read(self):
        calls = []
        fake = self._fetcher({}, calls)
        with mock.patch.object(assist, "_fetch_url_once", fake):
            for cid in ("hannah", "mia", "jordan"):
                res = self.say("open this https://example.com/x", cid=cid)
                self.assertTrue(
                    res is None or "PAGE TEXT" not in res["brief"],
                    cid)
        self.assertEqual(calls, [])


class SsrfGuardTest(unittest.TestCase):
    def test_ip_literals_blocked(self):
        for url in ("http://127.0.0.1/x", "http://10.0.0.5/",
                    "http://172.16.0.9/a", "http://192.168.1.1/",
                    "http://169.254.169.254/latest/meta-data",
                    "http://[::1]/", "http://0.0.0.0/"):
            self.assertIsNotNone(assist._public_url_guard(url), url)

    def test_local_names_and_schemes_blocked(self):
        for url in ("http://localhost/admin", "https://printer.local/",
                    "ftp://example.com/x", "file:///etc/passwd",
                    "gopher://example.com/"):
            self.assertIsNotNone(assist._public_url_guard(url), url)

    def test_dns_answer_decides(self):
        def fake_infos(ip):
            def _getaddrinfo(host, port, proto=0):
                return [(2, 1, 6, "", (ip, 0))]
            return _getaddrinfo

        with mock.patch.object(assist.socket, "getaddrinfo",
                               fake_infos("192.168.0.44")):
            self.assertIsNotNone(
                assist._public_url_guard("https://sneaky.example.com/"))
        with mock.patch.object(assist.socket, "getaddrinfo",
                               fake_infos("93.184.216.34")):
            self.assertIsNone(
                assist._public_url_guard("https://example.com/"))


if __name__ == "__main__":
    unittest.main()
