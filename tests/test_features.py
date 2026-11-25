"""Features held back from a release (bunkr/features.py): off by default, answering 404, hidden from the screens and the demo."""

import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bunkr import auth, demo, features, station  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402


class FlagTests(unittest.TestCase):
    def test_off_unless_named(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("BUNKR_PREVIEW", None)
            self.assertEqual(features.on(), [])
        with mock.patch.dict(os.environ, {"BUNKR_PREVIEW": "csv_import, phone_app"}):
            self.assertEqual(features.on(), ["csv_import", "phone_app"])
            self.assertFalse(features.enabled("weather_station"))


class HeldBackHttpTests(unittest.TestCase):
    """The default build: weather station, CSV import and the phone app can't be reached."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop("BUNKR_PREVIEW", None)
        self.hub = p3.Hub()
        auth.create_user(self.hub.store, "dad", p3.PW, "admin")
        self.opener = urllib.request.build_opener(p3.NoRedirect)

    def tearDown(self):
        self.hub.close()
        self._env.stop()

    def req(self, method, path, body=None, cookie=None):
        h = {"Content-Type": "application/json", "X-Bunkr": "1"}
        if cookie:
            h["Cookie"] = f"bunkr_session={cookie}"
        r = urllib.request.Request(self.hub.url + path, method=method, headers=h,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def login(self):
        r = urllib.request.Request(self.hub.url + "/api/login", method="POST", headers={"Content-Type": "application/json", "X-Bunkr": "1"},
                                   data=json.dumps({"username": "dad", "password": p3.PW}).encode())
        with self.opener.open(r) as resp:
            return resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def test_routes_are_not_there(self):
        cookie = self.login()
        path = "/station/" + station.new_key(self.hub.store, "x")["key"]   # even a station set up before can't send
        self.assertEqual(self.req("POST", path, {"tempf": 40})[0], 404)
        self.assertEqual(self.req("GET", "/api/station", cookie=cookie)[0], 404)
        self.assertEqual(self.req("POST", "/api/items/import", {"csv": "name\nRice\n"}, cookie=cookie)[0], 404)
        self.assertEqual(self.req("GET", "/api/inventory-template.csv", cookie=cookie)[0], 404)
        self.assertEqual(self.req("POST", "/api/app/code", {}, cookie=cookie)[0], 404)
        self.assertEqual(self.req("POST", "/api/app/pair", {"code": "AAAA-BBBB-CCCC"})[0], 404)
        self.assertEqual(self.req("GET", "/api/app/stream", cookie=cookie)[0], 404)
        self.assertEqual(self.req("GET", "/api/app/version", cookie=cookie)[0], 404)
        self.assertEqual(self.req("POST", "/api/app/open", {}, cookie=cookie)[0], 404)
        self.assertNotEqual(self.req("GET", "/app/bunkr.apk", cookie=cookie)[1][:2], b"PK")   # the app file isn't served

    def test_downloads_do_not_offer_the_app(self):
        from bunkr import releases
        with mock.patch.object(releases, "latest", return_value=None):
            ids = [e["id"] for e in releases.resolve_all({})]
        self.assertNotIn("bunkr-android", ids)
        self.assertIn("meshtastic-android", ids)

    def test_state_shows_none_of_it(self):
        station.new_key(self.hub.store, "x")
        s = json.loads(self.req("GET", "/api/state", cookie=self.login())[1])
        self.assertEqual(s["preview"], [])
        self.assertIsNone(s["station_now"])

    def test_demo_has_no_station(self):
        demo.fill(self.hub.store)
        self.assertFalse(station.settings(self.hub.store).get("key"))


if __name__ == "__main__":
    unittest.main()
