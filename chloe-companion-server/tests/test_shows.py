import unittest
from unittest import mock

from app import shows


class ShowsTest(unittest.TestCase):

    def setUp(self):
        shows._cache["ts"] = 0.0
        shows._cache["shows"] = []

    def test_get_shows_local_fallback(self):
        with mock.patch("app.shows._fetch_remote", side_effect=Exception("network error")):
            result = shows.get_shows(refresh=True)
            self.assertTrue(len(result) > 0)
            self.assertIn("id", result[0])
            self.assertIn("title", result[0])
            self.assertIn("starts_at", result[0])
            self.assertIn("ticket_url", result[0])

    def test_get_shows_remote(self):
        fake_remote_data = {
            "ok": True,
            "shows": [
                {
                    "show_id": "test_001",
                    "show_type": "public",
                    "character_id": "chloe",
                    "description": "Test Live Experience",
                    "price": 4.99,
                    "created_at": "2026-10-10T12:00:00Z",
                    "ticket_url": "https://buy.stripe.com/test",
                }
            ],
        }
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json_bytes(fake_remote_data)
        mock_resp.__enter__.return_value = mock_resp

        with mock.patch("urllib.request.urlopen", return_value=mock_resp):
            result = shows.get_shows(refresh=True)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["id"], "test_001")
            self.assertEqual(result[0]["title"], "Test Live Experience")
            self.assertEqual(result[0]["starts_at"], "2026-10-10T12:00:00Z")
            self.assertEqual(result[0]["ticket_url"], "https://buy.stripe.com/test")

    def test_next_show(self):
        with mock.patch("app.shows._fetch_remote", side_effect=Exception("offline")):
            nxt = shows.next_show()
            self.assertIsNotNone(nxt)
            self.assertIn("id", nxt)


def json_bytes(obj):
    import json
    return json.dumps(obj).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
