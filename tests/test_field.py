"""Field page: on the field Wi-Fi, read-only pages need no login; changes still do."""

import ipaddress
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import server  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402


class FieldPageTests(unittest.TestCase):
    req = p3.LoginHttpTests.req

    def setUp(self):
        self.hub = p3.Hub()
        root = self.hub.depot.root
        (root / "plan").mkdir(exist_ok=True)
        (root / "plan" / "family-plan.html").write_text("<h1>plan</h1>")
        (root / "apps").mkdir(exist_ok=True)
        (root / "apps" / "meshtastic-2.7.apk").write_bytes(b"PK" * 600_000)
        (root / "radio").mkdir(exist_ok=True)
        (root / "radio" / "uv-5r-3-card.html").write_text("card")
        import urllib.request
        self.opener = urllib.request.build_opener(p3.NoRedirect)

    def tearDown(self):
        server.OPEN_NETS[:] = []
        self.hub.close()

    def test_open_network_gets_read_only_pages(self):
        server.OPEN_NETS[:] = [ipaddress.ip_network("127.0.0.0/8")]
        st, _, body = self.req("GET", "/", host="bunkr.lan:8765")
        self.assertEqual(st, 200)
        page = body.decode()
        for want in ("Family plan", "/store/plan/family-plan.html", "meshtastic-2.7.apk", "Uv 5R channel card",
                     "/api/report.html", "Log in"):
            self.assertIn(want, page)
        self.assertNotIn("Maps", page)  # no maps pack on this node yet
        self.assertEqual(self.req("GET", "/store/plan/family-plan.html")[0], 200)
        st, hd, _ = self.req("GET", "/store/apps/meshtastic-2.7.apk")
        self.assertEqual((st, hd["Content-Disposition"]), (200, 'attachment; filename="meshtastic-2.7.apk"'))
        self.assertEqual(self.req("GET", "/api/report.html")[0], 200)
        # everything else still needs a login
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("POST", "/api/items", {"name": "x"})[0], 401)
        self.assertEqual(self.req("GET", "/index.html")[0], 303)
        self.assertNotEqual(self.req("GET", "/store/plan/../../hub.db")[0], 200)

    def test_other_networks_still_need_login(self):
        server.OPEN_NETS[:] = [ipaddress.ip_network("10.42.0.0/24")]
        self.assertEqual(self.req("GET", "/")[0], 303)
        self.assertEqual(self.req("GET", "/store/plan/family-plan.html")[0], 303)
        self.assertEqual(self.req("GET", "/api/report.html")[0], 401)


if __name__ == "__main__":
    unittest.main()
