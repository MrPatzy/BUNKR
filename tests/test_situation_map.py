"""Situation map: warning-area outlines, satellite hot spots, Cloudflare Radar, key files, GeoJSON and the viewer."""

import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, server, sites, situation  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402
from tests.test_situation import HOME, feeds  # noqa: E402

NOW = datetime.now(timezone.utc)
DAYS = [(NOW - timedelta(days=n)).date().isoformat() for n in range(3)]
HEAD = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight\n"


def row(lat, lon, day, conf="n", frp=5.0):
    return f"{lat},{lon},330,0.4,0.4,{day},0912,N,VIIRS,{conf},2.0NRT,290,{frp},D\n"


FIRMS = HEAD + "".join([
    row(39.90, -105.05, DAYS[0], frp=12), row(39.901, -105.051, DAYS[0]),          # one new fire near home, two detections
    row(39.81, -104.95, DAYS[0]), row(39.81, -104.95, DAYS[1]), row(39.81, -104.95, DAYS[2]),  # a refinery: hot every day
    row(40.20, -105.40, DAYS[0]),                                                    # ~35 km away
    row(39.95, -105.10, DAYS[0], conf="l"),                                          # low confidence: dropped
    row(42.5, -108.0, DAYS[0]),                                                      # far away: dropped
])

RADAR = {"success": True, "result": {"annotations": [
    {"id": "a", "scope": "Colorado", "description": "Fiber cut", "startDate": NOW.isoformat(), "endDate": None,
     "locations": ["US"], "locationsDetails": [{"code": "US", "name": "United States"}], "asnsDetails": [{"asn": 7922, "name": "Comcast"}],
     "outage": {"outageCause": "CABLE_CUT", "outageType": "REGIONAL"}},
    {"id": "b", "scope": "", "description": "", "startDate": NOW.isoformat(), "endDate": None,
     "locations": ["US"], "locationsDetails": [{"code": "US", "name": "United States"}], "outage": {"outageType": "REGIONAL"}},
    {"id": "c", "scope": "", "startDate": NOW.isoformat(), "endDate": None, "locations": ["IR"],
     "locationsDetails": [{"code": "IR", "name": "Iran"}], "outage": {"outageCause": "GOVERNMENT_DIRECTED", "outageType": "NATIONWIDE"}},
    {"id": "d", "scope": "Ohio", "startDate": NOW.isoformat(), "endDate": None, "locations": ["DE"], "outage": {"outageType": "REGIONAL"}},
    {"id": "e", "scope": "Colorado", "startDate": "2020-01-01T00:00:00Z", "endDate": "2020-01-02T00:00:00Z", "locations": ["US"]},
]}}

SQUARE = {"type": "Polygon", "coordinates": [[[-105.2 + i * 0.0001, 39.8] for i in range(500)] + [[-105.0, 40.0], [-105.2, 39.8]]]}


class MapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "m.db")

    def tearDown(self):
        situation.listeners[:] = []
        self.tmp.cleanup()

    def test_outline_is_thinned_and_closed(self):
        g = sites.simplify(SQUARE)
        ring = g["coordinates"][0]
        self.assertLessEqual(len(ring), 130)
        self.assertEqual(ring[0], ring[-1])
        self.assertIsNone(sites.simplify(None))
        self.assertIsNone(sites.simplify({"type": "Point", "coordinates": [1, 2]}))
        multi = sites.simplify({"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"]]})
        self.assertEqual(multi["type"], "MultiPolygon")

    def test_firms_groups_and_labels_steady_heat(self):
        asked = []
        got = situation.fetch_firms([HOME], keys={"firms": "abc123KEY"}, text=lambda url: asked.append(url) or FIRMS)
        self.assertEqual(len(asked), 2)  # two satellites
        self.assertIn("/abc123KEY/VIIRS_SNPP_NRT/", asked[0])
        by = {round(i["km"]): i for i in got}
        near = [i for i in got if i["tier"] == "priority"]
        self.assertEqual(len(near), 1)  # the two detections near home are one item
        self.assertEqual(near[0]["trust"], "satellite")
        self.assertIn("Satellite hot spot", near[0]["title"])
        steady = [i for i in got if "Steady heat" in i["title"]]
        self.assertEqual(len(steady), 1)
        self.assertEqual(steady[0]["tier"], "routine")  # a refinery never alarms
        self.assertTrue(all(i["km"] <= 50 for i in got))
        self.assertEqual(len(got), 3)
        self.assertTrue(by)
        self.assertEqual(situation.fetch_firms([HOME], keys={}), [])
        with self.assertRaises(RuntimeError):
            situation.fetch_firms([HOME], keys={"firms": "bad"}, text=lambda url: "Invalid MAP_KEY.")

    def test_radar_ranks_ours_first(self):
        heard = []

        def get(url, headers=None, **_):
            heard.append(headers)
            return RADAR
        got = {i["uid"]: i for i in situation.fetch_radar([HOME], get=get, keys={"radar": "tok"})}
        self.assertEqual(heard[0]["Authorization"], "Bearer tok")
        self.assertEqual(got["radar:a"]["tier"], "priority")  # names our state
        self.assertIn("cable cut", got["radar:a"]["summary"])
        self.assertEqual(got["radar:b"]["tier"], "routine")
        self.assertEqual(got["radar:c"]["tier"], "routine")  # a whole country cut off
        self.assertNotIn("radar:d", got)
        self.assertNotIn("radar:e", got)  # long over
        with self.assertRaises(RuntimeError):
            situation.fetch_radar([HOME], get=lambda url, **_: {"success": False, "errors": [{"message": "Authentication error"}]},
                                  keys={"radar": "tok"})

    def test_keyed_sources_off_without_key(self):
        situation.refresh(self.store, [HOME], get=feeds())
        st = self.store.get_raw("situation_sources")
        self.assertEqual(st["firms"], {"ok": None, "needs_key": True})
        self.assertEqual(st["radar"], {"ok": None, "needs_key": True})
        self.assertTrue(st["usgs"]["ok"])
        get = feeds({"api.cloudflare.com": RADAR})
        situation.refresh(self.store, [HOME], get=get, only={"radar"}, keys={"radar": "tok"})
        self.assertTrue(self.store.get_raw("situation_sources")["radar"]["ok"])
        self.assertTrue(any(i["source"] == "radar" for i in situation.items(self.store)))

    def test_key_files(self):
        d = Path(self.tmp.name)
        self.assertEqual(situation.load_keys(d), {})
        self.assertTrue(situation.save_key(d, "firms", " abcdef0123456789 "))
        self.assertEqual(situation.load_keys(d), {"firms": "abcdef0123456789"})
        self.assertEqual(stat.S_IMODE(os.stat(d / "firms.key").st_mode), 0o600)
        with self.assertRaises(ValueError):
            situation.save_key(d, "radar", "has spaces in it; rm -rf")
        self.assertFalse(situation.save_key(d, "firms", ""))
        self.assertFalse((d / "firms.key").exists())

    def test_geojson_has_places_points_and_areas(self):
        home = dict(HOME, alerts=[{"id": "w1", "event": "Tornado Warning", "severity": "Extreme", "geometry": sites.simplify(SQUARE)}])
        situation.refresh(self.store, [home], get=feeds())
        g = situation.geo(self.store, [home])
        kinds = [f["geometry"]["type"] for f in g["features"]]
        self.assertEqual(g["features"][0]["properties"], {"kind": "site", "title": "Home", "site_kind": ""})
        self.assertIn("Polygon", kinds)
        area = next(f for f in g["features"] if f["geometry"]["type"] == "Polygon")
        self.assertEqual((area["properties"]["tier"], area["properties"]["scenario"]), ("flash", "tornado"))
        self.assertTrue(any(f["properties"].get("uid") == "usgs:near" for f in g["features"]))
        self.assertFalse(any(f["properties"].get("uid") == "kev:CVE-1" for f in g["features"]))  # no location, not on the map
        node = Store(Path(self.tmp.name) / "node.db")
        situation.apply(node, situation.export(self.store, ""))
        self.assertTrue(any(f["geometry"]["type"] == "Polygon" for f in situation.geo(node, [home])["features"]))


class MapHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        auth.create_user(self.hub.store, "dad", p3.PW, "admin")
        auth.create_user(self.hub.store, "kid", p3.PW, "family")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {}
        for u in ("dad", "kid"):
            r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": u, "password": p3.PW}).encode(),
                                       method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
            with self.opener.open(r) as resp:
                self.c[u] = resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.refreshed = []
        self.real = server.refresh_situation
        server.refresh_situation = lambda: self.refreshed.append(1)

    def tearDown(self):
        server.refresh_situation = self.real
        self.hub.close()

    def req(self, method, path, body=None, user="dad", raw=False):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                data = resp.read()
                return resp.status, data if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_geo_keys_and_viewer(self):
        st, g = self.req("GET", "/api/situation/geo", user="kid")
        self.assertEqual((st, g["type"]), (200, "FeatureCollection"))
        st, v = self.req("GET", "/api/situation", user="kid")
        self.assertEqual(v["map"], {"viewer": False, "pack": False})
        firms = next(s for s in v["sources"] if s["key"] == "firms")
        self.assertIn("firms.modaps", firms["key_url"])
        self.assertEqual(self.req("POST", "/api/situation/key", {"source": "firms", "value": "abcdef0123456789"}, "kid")[0], 403)
        st, r = self.req("POST", "/api/situation/key", {"source": "firms", "value": "abcdef0123456789"})
        self.assertEqual((st, r["saved"]), (200, True))
        self.assertEqual(situation.load_keys(self.hub.dir), {"firms": "abcdef0123456789"})
        self.assertEqual(self.req("POST", "/api/situation/key", {"source": "nope", "value": "x"})[0], 400)
        self.assertEqual(self.req("POST", "/api/situation/key", {"source": "radar", "value": "a b"})[0], 400)
        st, v = self.req("GET", "/api/situation")
        self.assertNotIn("abcdef0123456789", json.dumps(v))  # the key never comes back
        st, page = self.req("GET", "/store/web/maps/", raw=True)
        self.assertEqual(st, 200)
        self.assertIn(b"overlay=situation", page)  # this code's viewer, even before a maps pack exists


if __name__ == "__main__":
    unittest.main()
