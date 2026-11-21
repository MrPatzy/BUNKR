"""Situation feeds: ranking by distance to our places, calm tiers, scenario links, failures, sync, API."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, situation  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

HOME = {"id": 1, "name": "Home", "lat": 39.88, "lon": -105.03, "state": "CO", "alerts": []}
NOW = datetime.now(timezone.utc)
ms = lambda d: int(d.timestamp() * 1000)


def feeds(overrides=None):
    data = {
        "earthquake.usgs.gov": {"features": [
            {"id": "near", "properties": {"mag": 4.3, "place": "10 km N of Boulder", "time": ms(NOW)}, "geometry": {"coordinates": [-105.2, 40.1, 5]}},
            {"id": "small_far", "properties": {"mag": 3.1, "place": "Alaska", "time": ms(NOW)}, "geometry": {"coordinates": [-150, 61, 5]}},
            {"id": "big_far", "properties": {"mag": 7.1, "place": "Japan", "time": ms(NOW), "tsunami": 1}, "geometry": {"coordinates": [140, 36, 5]}}]},
        "gdacs.org": {"features": []},
        "arcgis.com": {"features": [
            {"geometry": {"coordinates": [-105.1, 39.95]}, "properties": {"IncidentName": "Close", "IncidentSize": 120, "PercentContained": 0, "FireDiscoveryDateTime": ms(NOW)}},
            {"geometry": {"coordinates": [-105.1, 39.95]}, "properties": {"IncidentName": "Piles RX", "IncidentTypeCategory": "RX", "FireDiscoveryDateTime": ms(NOW)}},
            {"geometry": {"coordinates": [-107.0, 37.3]}, "properties": {"IncidentName": "Faraway", "FireDiscoveryDateTime": ms(NOW)}}]},
        "noaa-scales": {"0": {"DateStamp": "2026-11-20", "TimeStamp": "05:00:00", "R": {"Scale": "3"}, "S": {"Scale": "0"}, "G": {"Scale": "1"}}},
        "known_exploited": {"vulnerabilities": [
            {"cveID": "CVE-1", "vendorProject": "Google", "product": "Chrome", "dateAdded": NOW.date().isoformat(), "shortDescription": "bad", "requiredAction": "Update"},
            {"cveID": "CVE-2", "vendorProject": "Acme", "product": "Widget", "dateAdded": NOW.date().isoformat()},
            {"cveID": "CVE-3", "vendorProject": "Google", "product": "Chrome Old", "dateAdded": "2020-01-01"}]},
        "ransomware.live": [
            {"victim": "Denver Water", "country": "US", "group": "x", "activity": "Utilities", "discovered": NOW.isoformat()},
            {"victim": "Some Clinic", "country": "US", "group": "y", "activity": "Healthcare", "discovered": NOW.isoformat()},
            {"victim": "Bakery", "country": "US", "group": "z", "activity": "Food", "discovered": NOW.isoformat()},
            {"victim": "Denver Water GmbH", "country": "DE", "group": "x", "activity": "Utilities", "discovered": NOW.isoformat()}],
        "ioda": {"data": [
            {"level": "critical", "datasource": "bgp", "value": 10, "historyValue": 95, "time": int(NOW.timestamp()),
             "entity": {"code": "4398", "name": "Colorado", "type": "region", "attrs": {"country_code": "US"}}},
            {"level": "normal", "entity": {"code": "1", "name": "Ohio", "attrs": {"country_code": "US"}}}]},
        "fema.gov": {"DisasterDeclarationsSummaries": []},
    }
    data.update(overrides or {})

    def get(url, **_):
        for key, body in data.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                if key == "ioda" and "entityType=country" in url:
                    return {"data": []}
                return body
        raise AssertionError("unexpected url " + url)
    return get


class SituationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "s.db")

    def tearDown(self):
        situation.listeners[:] = []
        self.tmp.cleanup()

    def by_uid(self):
        return {i["uid"]: i for i in situation.items(self.store)}

    def test_ranking_tiers_and_scenarios(self):
        home = dict(HOME, alerts=[
            {"id": "a1", "event": "Tornado Warning", "severity": "Extreme", "headline": "Take cover", "instruction": "Basement now"},
            {"id": "a2", "event": "Winter Storm Watch", "severity": "Moderate", "headline": "Snow"},
            {"id": "a3", "event": "Special Weather Statement", "severity": "Minor", "headline": "Wind"}])
        work = dict(HOME, id=2, name="Work", alerts=[{"id": "a1", "event": "Tornado Warning", "severity": "Extreme"}])
        situation.refresh(self.store, [home, work], get=feeds())
        it = self.by_uid()
        self.assertEqual((it["nws:a1"]["tier"], it["nws:a1"]["scenario"]), ("flash", "tornado"))
        self.assertEqual(it["nws:a1"]["where_text"], "Home, Work")  # one item, both places
        self.assertIn("What to do: Basement now", it["nws:a1"]["summary"])
        self.assertEqual((it["nws:a2"]["tier"], it["nws:a2"]["scenario"]), ("priority", "winter_storm"))
        self.assertEqual(it["nws:a3"]["tier"], "routine")
        self.assertEqual(it["usgs:near"]["tier"], "priority")
        self.assertEqual(it["usgs:big_far"]["tier"], "routine")
        self.assertNotIn("usgs:small_far", it)
        fires = {i["title"].split(" fire")[0]: i for i in it.values() if i["source"] == "wfigs"}
        self.assertEqual((fires["Close"]["tier"], fires["Close"]["scenario"], fires["Close"]["trust"]), ("priority", "evacuation", "confirmed"))
        self.assertEqual(fires["Piles RX"]["tier"], "routine")  # a planned burn never alarms
        self.assertNotIn("Faraway", fires)
        space = {i["title"]: i for i in it.values() if i["source"] == "swpc"}
        self.assertEqual(space["Radio blackout R3 now"]["tier"], "priority")
        self.assertIn("HF radio blackout", space["Radio blackout R3 now"]["summary"])
        self.assertEqual(space["Geomagnetic storm G1 now"]["tier"], "routine")
        self.assertEqual(it["kev:CVE-1"]["tier"], "priority")
        self.assertTrue(it["kev:CVE-1"]["title"].startswith("Patch now"))
        self.assertEqual(it["kev:CVE-2"]["tier"], "routine")
        self.assertNotIn("kev:CVE-3", it)
        rw = {i["title"].split(" listed")[0]: i for i in it.values() if i["source"] == "ransomware"}
        self.assertEqual((rw["Denver Water"]["tier"], rw["Denver Water"]["trust"]), ("priority", "reported"))
        self.assertEqual(rw["Some Clinic"]["tier"], "routine")
        self.assertEqual(set(rw), {"Denver Water", "Some Clinic"})  # bakery and non-US skipped
        ioda = [i for i in it.values() if i["source"] == "ioda"]
        self.assertEqual([(i["title"], i["tier"]) for i in ioda], [("Internet outage: Colorado, US", "priority")])
        order = [i["tier"] for i in situation.items(self.store)]
        self.assertEqual(order, sorted(order, key=situation.TIER_ORDER.get))

    def test_failures_keep_old_data_and_ended_items_go_inactive(self):
        situation.refresh(self.store, [HOME], get=feeds())
        n_quakes = len([i for i in situation.items(self.store) if i["source"] == "usgs"])
        situation.refresh(self.store, [HOME], get=feeds({"earthquake.usgs.gov": urllib.error.URLError("down"),
                                                         "arcgis.com": {"features": []}}))
        st = self.store.get_raw("situation_sources")
        self.assertFalse(st["usgs"]["ok"])
        self.assertIn("down", st["usgs"]["error"])
        self.assertTrue(st["usgs"]["at"])  # when the data shown was last good
        self.assertEqual(len([i for i in situation.items(self.store) if i["source"] == "usgs"]), n_quakes)
        self.assertEqual([i for i in situation.items(self.store) if i["source"] == "wfigs"], [])  # fires are out
        self.assertTrue(st["kev"]["ok"])  # one failure doesn't stop the rest

    def test_listeners_hear_new_and_worse_only(self):
        heard = []
        situation.listeners[:] = [lambda ev, it: heard.append(it["uid"])]
        alert = {"id": "w", "event": "Winter Storm Watch", "severity": "Moderate"}
        situation.refresh(self.store, [dict(HOME, alerts=[alert])], get=feeds())
        self.assertIn("nws:w", heard)
        heard.clear()
        situation.refresh(self.store, [dict(HOME, alerts=[alert])], get=feeds())
        self.assertEqual(heard, [])  # nothing new: silence
        situation.refresh(self.store, [dict(HOME, alerts=[dict(alert, event="Winter Storm Warning", severity="Severe")])], get=feeds())
        self.assertEqual(heard, ["nws:w"])  # it got worse

    def test_sync_to_node(self):
        situation.refresh(self.store, [HOME], get=feeds())
        node = Store(Path(self.tmp.name) / "node.db")
        n = situation.apply(node, situation.export(self.store, ""))
        self.assertEqual(n, len(situation.items(self.store, include_inactive=True)))
        self.assertEqual(len(situation.items(node)), len(situation.items(self.store)))
        self.assertTrue(node.get_raw("situation_sources")["usgs"]["ok"])


class SituationHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        auth.create_user(self.hub.store, "dad", p3.PW, "admin")
        auth.create_user(self.hub.store, "kid", p3.PW, "family")
        situation.refresh(self.hub.store, [HOME], get=feeds())
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {u: self.login(u) for u in ("dad", "kid")}

    def tearDown(self):
        self.hub.close()

    def req(self, method, path, body=None, user=None):
        h = {"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}" if user else ""}
        r = urllib.request.Request(self.hub.url + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def login(self, user):
        r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": user, "password": p3.PW}).encode(),
                                   method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
        with self.opener.open(r) as resp:
            return resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def test_view_prefs_and_home_summary(self):
        st, v = self.req("GET", "/api/situation", user="kid")
        self.assertEqual(st, 200)
        self.assertTrue(any(i["uid"] == "kev:CVE-1" for i in v["items"]))
        self.assertEqual({s["key"] for s in v["sources"]}, {"nws", *situation.SOURCES})
        self.assertEqual(self.req("POST", "/api/situation/prefs", {"devices": ["Acme"]}, "kid")[0], 403)
        st, p = self.req("POST", "/api/situation/prefs", {"devices": ["Acme", " "], "local_orgs": ["Boulder"], "state": "Colorado"}, "dad")
        self.assertEqual((st, p["devices"], p["local_orgs"]), (200, ["Acme"], ["Boulder"]))
        st, state = self.req("GET", "/api/state", user="kid")
        self.assertTrue(state["situation_top"])
        self.assertTrue(all(i["tier"] in ("flash", "priority") for i in state["situation_top"]))


if __name__ == "__main__":
    unittest.main()
