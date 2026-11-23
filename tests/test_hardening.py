"""1.2 hardening: firearms writes kept to admins, safety headers, request size limits, login limits per address."""

import http.client
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, server, situation  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402
from tests.test_cash_firearms import add  # noqa: E402


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        st = self.hub.store
        auth.create_user(st, "dad", p3.PW, "admin")
        auth.create_user(st, "kid", p3.PW, "family")
        self.gun = add(st, name="Glock 19", category="firearms", subtype="firearm", caliber="9mm", serial="ABC123")
        self.light = add(st, name="Flashlight", category="power", essential="flashlight")
        self.opener = p3.urllib.request.build_opener(p3.NoRedirect)

    req = p3.LoginHttpTests.req
    login = p3.LoginHttpTests.login

    def tearDown(self):
        server.MAX_BODY = 4 * 1024 * 1024
        self.hub.close()

    def gun_row(self):
        return self.hub.store._rows("SELECT * FROM items WHERE id=?", (self.gun,))

    def test_family_cannot_change_firearms(self):
        kid = self.login("kid")
        item = {"name": "Mine now", "category": "other", "quantity": 1}
        self.assertEqual(self.req("POST", f"/api/items/{self.gun}", item, cookie=kid)[0], 404)
        self.assertEqual(self.req("DELETE", f"/api/items/{self.gun}", cookie=kid)[0], 404)
        self.assertEqual(self.req("POST", "/api/items/move", {"ids": [self.light, self.gun], "location": "Car"}, cookie=kid)[0], 404)
        st, _, body = self.req("POST", "/api/items", {"name": "Rifle", "category": "firearms", "serial": "X1"}, cookie=kid)
        self.assertEqual(st, 403)
        row = self.gun_row()[0]
        self.assertEqual((row["name"], row["serial"], row["location_id"]), ("Glock 19", "ABC123", None))
        self.assertFalse(self.hub.store._rows("SELECT 1 FROM items WHERE name='Rifle'"))
        # their own things still work
        self.assertEqual(self.req("POST", f"/api/items/{self.light}", {"name": "Torch", "category": "power"}, cookie=kid)[0], 200)
        self.assertEqual(self.req("POST", "/api/items/move", {"ids": [self.light], "location": "Car"}, cookie=kid)[0], 200)
        # admins can
        dad = self.login("dad")
        self.assertEqual(self.req("POST", f"/api/items/{self.gun}", {"name": "Glock 19", "category": "firearms",
                                                                    "serial": "ABC123", "notes": "cleaned"}, cookie=dad)[0], 200)
        self.assertEqual(self.gun_row()[0]["notes"], "cleaned")
        self.assertEqual(self.req("DELETE", f"/api/items/{self.gun}", cookie=dad)[0], 200)
        self.assertFalse(self.gun_row())

    def test_safety_headers(self):
        _, hd, _ = self.req("GET", "/login.html")
        self.assertEqual(hd["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(hd["Referrer-Policy"], "same-origin")
        self.assertEqual(hd["X-Content-Type-Options"], "nosniff")
        self.assertIn("script-src 'self'", hd["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'self'", hd["Content-Security-Policy"])
        kid = self.login("kid")
        _, hd, _ = self.req("GET", "/api/report.html", cookie=kid)
        self.assertIn("default-src 'none'", hd["Content-Security-Policy"])
        _, hd, _ = self.req("GET", "/api/state", cookie=kid)
        self.assertIsNone(hd["Content-Security-Policy"])  # JSON
        self.assertEqual(hd["X-Frame-Options"], "SAMEORIGIN")

    def raw_post(self, path, headers, body=b""):
        c = http.client.HTTPConnection("127.0.0.1", self.hub.httpd.server_address[1], timeout=10)
        c.putrequest("POST", path)
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders()
        if body:
            c.send(body)
        r = c.getresponse()
        out = r.status, r.read()
        c.close()
        return out

    def test_request_size_limits(self):
        server.MAX_BODY = 1000
        big = json.dumps({"username": "dad", "password": "x" * 2000}).encode()
        st, _ = self.raw_post("/api/login", {"X-Bunkr": "1", "Content-Length": str(len(big))}, big)
        self.assertEqual(st, 413)
        st, _ = self.raw_post("/api/login", {"X-Bunkr": "1", "Content-Length": "-1"})
        self.assertEqual(st, 400)
        st, _ = self.raw_post("/api/sync/pair", {"Content-Length": "5000000"})
        self.assertEqual(st, 413)

    def test_logins_limited_per_address(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for _ in range(server.LOGIN_IP_FAILS):
            self.hub.store._exec("INSERT INTO audit_log(at, action, what, ip) VALUES(?, 'login_failed', 'login', '127.0.0.1')", (now,))
        st, _, body = self.req("POST", "/api/login", {"username": "dad", "password": p3.PW})
        self.assertEqual(st, 429)
        self.assertIn(b"wait 15 minutes", body)
        self.hub.store._exec("UPDATE audit_log SET at='2000-01-01T00:00:00+00:00'")  # long ago
        self.login("dad")

    def test_errors_before_login_say_little(self):
        st, _, body = self.req("POST", "/api/login", ["not", "an", "object"])
        self.assertEqual(st, 500)
        self.assertNotIn(b"AttributeError", body)


class SituationLinkTests(unittest.TestCase):
    def test_only_web_links_from_feeds(self):
        self.assertEqual(situation._item("usgs", "1", "quake", 2, "M5", url="javascript:alert(1)")["url"], "")
        self.assertEqual(situation._item("usgs", "1", "quake", 2, "M5", url=" JavaScript:x")["url"], "")
        self.assertEqual(situation._item("usgs", "1", "quake", 2, "M5", url="https://usgs.gov/e/1")["url"], "https://usgs.gov/e/1")


if __name__ == "__main__":
    unittest.main()
