"""The home weather station: three ways in, readings kept, warnings raised and cleared, the private address."""

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bunkr import auth, situation, station
from bunkr.db import Store
from tests import test_phase3 as p3

T0 = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)

_preview = mock.patch.dict(os.environ, {"BUNKR_PREVIEW": "weather_station"})   # held back from 1.3.0: off unless switched on


def setUpModule():
    _preview.start()


def tearDownModule():
    _preview.stop()


class StationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "s.db")
        self.heard = []
        situation.listeners[:] = [lambda ev, it: self.heard.append(it["title"])]

    def tearDown(self):
        situation.listeners.clear()
        self.tmp.cleanup()

    def put(self, minutes, **values):
        station.record(self.st, station.normalize(values, T0 + timedelta(minutes=minutes)))

    def test_three_formats(self):
        own = station.normalize({"temp_c": 5, "humidity": 63, "pressure_hpa": 1012.4, "wind_mph": 8, "wind_dir": 315})
        self.assertEqual((own["temp_f"], own["pressure_hpa"]), (41.0, 1012.4))
        wu = station.normalize({"ID": "x", "PASSWORD": "y", "tempf": "41.2", "baromin": "29.92", "windgustmph": "15", "dailyrainin": "0.12"})
        self.assertEqual((wu["temp_f"], round(wu["pressure_hpa"]), wu["gust_mph"], wu["rain_day_in"]), (41.2, 1013, 15.0, 0.12))
        eco = station.normalize({"PASSKEY": "abc", "tempf": "-9999", "baromrelin": "30.01", "rainratein": "0.5", "humidity": "250"})
        self.assertNotIn("temp_f", eco)              # -9999 is a missing sensor
        self.assertNotIn("humidity", eco)            # out of range
        self.assertEqual(eco["rain_rate_in"], 0.5)
        with self.assertRaises(ValueError):
            station.normalize({"ID": "x", "nonsense": 1})

    def test_readings_and_warnings(self):
        for m, p in ((0, 1015.0), (60, 1013.5), (120, 1012.0), (180, 1010.5)):
            self.put(m, pressure_hpa=p, temp_f=45, gust_mph=12)
        self.put(180, pressure_hpa=1010.5, temp_f=45, gust_mph=12)        # the same minute twice: kept once
        when = T0 + timedelta(minutes=181)
        self.assertEqual(len(station.history(self.st, 24, when)), 4)
        self.assertEqual(station.pressure_change(self.st, when), -4.5)
        v = station.now_view(self.st, when)
        self.assertEqual(v["pressure_trend"], "falling fast")
        station.publish(self.st, {"name": "Home"}, when)
        self.assertEqual(self.heard, ["Pressure falling fast at home (-4.5 hPa in 3 h)"])
        station.publish(self.st, {"name": "Home"}, when)                  # still true: not raised twice
        self.assertEqual(len(self.heard), 1)
        # gusts, heavy rain, and a freeze (good to know, not a push-worthy alert)
        self.put(182, pressure_hpa=1010.4, temp_f=30, gust_mph=52, rain_rate_in=1.2)
        tiers = {i["title"]: i["tier"] for i in station.warnings(self.st, {}, T0 + timedelta(minutes=183))}
        self.assertEqual(tiers["Gusts of 52 mph at home"], "priority")
        self.assertEqual(tiers["Heavy rain at home (1.2 in an hour)"], "priority")
        self.assertEqual(tiers["Freezing at home (30°F)"], "routine")
        # it clears itself: calm readings make the alerts inactive
        self.put(240, pressure_hpa=1011.0, temp_f=40, gust_mph=5)
        station.publish(self.st, {}, T0 + timedelta(minutes=241))
        self.assertEqual([i for i in situation.items(self.st) if i["source"] == "station"], [])
        # a station that went quiet says nothing
        self.assertEqual(station.warnings(self.st, {}, T0 + timedelta(hours=8)), [])
        self.assertIsNone(station.now_view(self.st, T0 + timedelta(hours=8)))


class StationHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        self.st = self.hub.store
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {u: self.login(u) for u in ("dad", "kid")}

    def tearDown(self):
        self.hub.close()

    def login(self, user):
        r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": user, "password": p3.PW}).encode(),
                                   method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
        with self.opener.open(r) as resp:
            return resp.headers["Set-Cookie"].split("bunkr_session=")[1].split(";")[0]

    def req(self, method, path, body=None, user="dad"):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def raw(self, method, path, data=None, ctype=None):
        r = urllib.request.Request(self.hub.url + path, method=method, data=data, headers={"Content-Type": ctype} if ctype else {})
        try:
            with self.opener.open(r) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_setup_and_readings(self):
        self.assertEqual(self.req("GET", "/api/station", user="kid")[0], 403)
        code, v = self.req("GET", "/api/station")
        self.assertEqual((code, v["set_up"]), (200, False))
        self.assertEqual(self.raw("GET", "/station/nope?tempf=40")[0], 403)
        v = self.req("POST", "/api/station", {"action": "new_key"})[1]
        path = "/station/" + v["url"].rsplit("/station/", 1)[1]
        # Weather Underground style: a GET with the reading in the query, no login, no BUNKR headers
        self.assertEqual(self.raw("GET", path + "?ID=x&PASSWORD=y&tempf=41.5&humidity=60&baromin=29.9"), (200, "success"))
        # Ecowitt style: a form POST
        self.assertEqual(self.raw("POST", path, b"PASSKEY=abc&tempf=42&windspeedmph=7", "application/x-www-form-urlencoded")[0], 200)
        # our own JSON
        code, out = self.raw("POST", path, json.dumps({"temp_f": 43, "pressure_hpa": 1011}).encode(), "application/json")
        self.assertEqual((code, json.loads(out)), (200, {"ok": True}))
        self.assertEqual(self.raw("POST", path, b"{}", "application/json")[0], 400)
        v = self.req("GET", "/api/station")[1]
        self.assertEqual(v["last"]["temp_f"], 43)
        self.assertTrue(self.req("GET", "/api/state", user="kid")[1]["station_now"])
        # a new address: the old one stops working
        self.req("POST", "/api/station", {"action": "new_key"})
        self.assertEqual(self.raw("GET", path + "?tempf=40")[0], 403)


if __name__ == "__main__":
    unittest.main()
