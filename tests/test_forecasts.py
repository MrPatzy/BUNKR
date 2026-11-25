"""Saved forecasts: saved every few hours, used when the internet is down, past periods dropped, sent to field nodes."""

import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bunkr import forecasts, meshinfo, wx
from bunkr.db import Store

T0 = datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc)   # 6 am in Denver


def periods(start=T0):
    """14 periods of 12 hours: today, tonight, and six more days and nights."""
    out = []
    for i in range(14):
        s = start + timedelta(hours=12 * i)
        day = i % 2 == 0
        out.append({"name": ("Today" if i == 0 else "Tonight" if i == 1 else s.strftime("%A") + ("" if day else " Night")),
                    "startTime": s.isoformat(), "endTime": (s + timedelta(hours=12)).isoformat(), "isDaytime": day,
                    "temperature": 50 + i if day else 25 + i, "windSpeed": "5 mph", "windDirection": "W",
                    "shortForecast": "Sunny" if day else "Clear", "probabilityOfPrecipitation": {"value": 20 if i == 2 else 0},
                    "detailedForecast": "long text that isn't kept"})
    return out


class Web:
    def __init__(self):
        self.down = False

    def __call__(self, url):
        if self.down:
            raise urllib.error.URLError("no internet")
        if "/points/" in url:
            return {"properties": {"forecast": "F", "forecastHourly": "H", "observationStations": "S"}}
        if url == "F":
            return {"properties": {"periods": periods()}}
        if url == "H":
            return {"properties": {"periods": [dict(periods()[0], name="", startTime=(T0 + timedelta(hours=h)).isoformat())
                                               for h in range(100)]}}
        if url == "S":
            return {"features": []}
        raise AssertionError(url)


class ForecastTests(unittest.TestCase):
    def setUp(self):
        wx._points.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "f.db")
        self.st.save_site({"name": "Home", "kind": "home", "address": "x", "lat": 39.88, "lon": -105.03})
        self.st.save_site({"name": "No coordinates", "kind": "other", "address": "y"})
        self.web = Web()

    def tearDown(self):
        wx._points.clear()
        self.tmp.cleanup()

    def test_saved_and_trimmed(self):
        st = forecasts.save_all(self.st, self.st.sites(), get=self.web, when=T0)
        self.assertEqual(st, {"Home": {"ok": True}})
        snap = forecasts.near(self.st, 39.881, -105.031)
        self.assertEqual((len(snap["periods"]), len(snap["hourly"])), (14, 72))
        self.assertNotIn("detailedForecast", snap["periods"][0])
        self.assertIsNone(forecasts.near(self.st, 40.5, -105.0))           # 70 km away: not this one
        # a failed refresh keeps the last good copy
        self.web.down = True
        wx._points.clear()
        st = forecasts.save_all(self.st, self.st.sites(), get=self.web, when=T0 + timedelta(hours=3))
        self.assertFalse(st["Home"]["ok"])
        self.assertEqual(forecasts.near(self.st, 39.88, -105.03)["at"], T0.isoformat(timespec="seconds"))

    def test_two_days_without_internet(self):
        forecasts.save_all(self.st, self.st.sites(), get=self.web, when=T0)
        later = T0 + timedelta(days=2, hours=1)
        v = forecasts.home_view(self.st, when=later)
        self.assertEqual(v["age"], "2 days ago")
        self.assertTrue(v["stale"])
        self.assertEqual(v["periods"][0]["temp"], 54)                        # two days on: day 3 is "today"
        self.assertEqual(len(forecasts.remaining(forecasts.near(self.st, 39.88, -105.03)["periods"], later)), 10)
        snap = forecasts.near(self.st, 39.88, -105.03)
        self.assertIn("(saved 2 days ago)", forecasts.now_and_next(snap, "Home", later))
        self.assertEqual(forecasts.home_view(self.st, when=T0 + timedelta(days=8))["periods"], [])   # run out

    def test_mesh_bot_answers_from_the_saved_copy(self):
        forecasts.save_all(self.st, self.st.sites(), get=self.web, when=T0)
        self.web.down = True
        wx._points.clear()
        ctx = {"home": {"lat": 39.88, "lon": -105.03, "label": "Home", "kind": "home", "state": "CO"},
               "saved_wx": lambda p: forecasts.near(self.st, p["lat"], p["lon"])}
        out = meshinfo.weather(ctx, "", get=self.web)
        self.assertEqual(len(out), 1)
        self.assertIn("(saved", out[0])
        out = " ".join(meshinfo.weather(ctx, "5", get=self.web))
        self.assertIn("saved", out)
        ctx["saved_wx"] = lambda p: None
        self.assertIn("no saved forecast", meshinfo.weather(ctx, "", get=self.web)[0])

    def test_field_nodes_get_them(self):
        forecasts.save_all(self.st, self.st.sites(), get=self.web, when=T0)
        out = forecasts.export(self.st, since="")
        self.assertEqual(len(out["wx_saved"]), 1)
        self.assertEqual(forecasts.export(self.st, since=(T0 + timedelta(hours=1)).isoformat())["wx_saved"], [])
        with tempfile.TemporaryDirectory() as t:
            node = Store(Path(t) / "n.db")
            self.assertEqual(forecasts.apply(node, out), 1)
            self.assertEqual(forecasts.apply(node, out), 0)                  # nothing newer: unchanged
            self.assertEqual(len(forecasts.saved(node)), 1)


if __name__ == "__main__":
    unittest.main()
