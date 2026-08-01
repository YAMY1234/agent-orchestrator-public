import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import dashboard_network


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return json.dumps({"ok": True}).encode()


class DashboardNetworkTests(unittest.TestCase):
    def test_access_url_encodes_token(self):
        raw = "token with spaces & symbols"
        url = dashboard_network.build_access_url(
            "127.0.0.1", 7860, "http", raw
        )
        self.assertEqual(parse_qs(urlparse(url).query)["token"], [raw])

    def test_protocol_detection_falls_through_to_https(self):
        calls = []

        def fake_urlopen(request, **_kwargs):
            calls.append(request.full_url)
            if request.full_url.startswith("http://"):
                raise OSError("not HTTP")
            return _Response()

        with patch.object(
            dashboard_network, "urlopen", side_effect=fake_urlopen
        ):
            self.assertEqual(
                dashboard_network.detect_dashboard_scheme(7860), "https"
            )
        self.assertEqual(calls, [
            "http://127.0.0.1:7860/api/health",
            "https://127.0.0.1:7860/api/health",
        ])

    def test_private_address_detection(self):
        self.assertTrue(dashboard_network._is_private("10.2.3.4"))
        self.assertTrue(dashboard_network._is_private("172.20.1.2"))
        self.assertTrue(dashboard_network._is_private("192.168.1.2"))
        self.assertFalse(dashboard_network._is_private("8.8.8.8"))


if __name__ == "__main__":
    unittest.main()
