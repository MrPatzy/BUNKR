"""Phase 1: locations, kit targets, hazards, shopping list, sites (network stubbed)."""

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import db, gaps, hazards, kits, server, sites  # noqa: E402
from bunkr.db import DEFAULT_SETTINGS, Store  # noqa: E402

DENVER = {"ratings": {"CWAV": "Relatively High", "WNTW": "Relatively High", "IFLD": "Relatively High",
                      "TRND": "Relatively High", "WFIR": "Very Low", "HRCN": "Not Applicable",
                      "ERQK": "Relatively Moderate"}, "overall": "Relatively High"}
FLORIDA = {"ratings": {"HRCN": "Very High", "WNTW": "Very Low", "CWAV": "Very Low", "ISTM": "Very Low"}}


def item(**kw):
    base = {"id": 1, "name": "x", "category": "other", "quantity": 1, "unit": "", "per_unit": 0,
            "essential": "", "location": "", "location_id": None, "counts_home": 1, "expires": "", "notes": ""}
    return base | kw


class MigrationTests(unittest.TestCase):
    def test_free_text_locations_become_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript(db.BASE_SCHEMA)
            for name, loc in [("Jug", "Garage"), ("Cables", "Car trunk"), ("Bars", "Kid go-bag"),
                              ("Jug2", "garage"), ("Cash", "")]:
                conn.execute("INSERT INTO items(name, category, location) VALUES(?, 'other', ?)", (name, loc))
            conn.commit()
            conn.close()
            s = Store(path)
            locs = {l["name"]: l for l in s.locations()}
            self.assertEqual(set(locs), {"Garage", "Car trunk", "Kid go-bag"})
            self.assertEqual(locs["Car trunk"]["kind"], "vehicle")
            self.assertEqual(locs["Car trunk"]["counts_home"], 0)
            self.assertEqual(locs["Kid go-bag"]["kind"], "gobag")
            self.assertEqual(locs["Garage"]["item_count"], 2)  # case-insensitive match
            items = {i["name"]: i for i in s.items()}
            self.assertEqual(items["Cash"]["location_id"], None)
            self.assertTrue(all(i["uid"] for i in items.values()))
            Store(path)  # reopening is a no-op
            self.assertEqual(len(Store(path).locations()), 3)


class HazardTests(unittest.TestCase):
    def setUp(self):
        self.s = dict(DEFAULT_SETTINGS, adults=2)

    def ids(self, profile):
        return {l["id"] for l in gaps.analyze(self.s, [], [], hazard_profile=profile)["lines"]}

    def test_colorado_adds_winter_and_flood_items_not_hurricane(self):
        ids = self.ids(DENVER)
        self.assertTrue({"cold_bedding", "safe_heat", "co_alarm", "waterproof_docs", "shelter_gear",
                         "quake_anchors"} <= ids)
        self.assertNotIn("window_protection", ids)
        self.assertNotIn("smoke_filter", ids)  # wildfire rated Very Low in this county

    def test_no_profile_means_base_checklist(self):
        base = {e["id"] for e in gaps.ESSENTIALS} - set(self.s["not_applicable"])
        self.assertTrue(self.ids(None) - {"water", "food"} <= base | {f"doc_{t}" for t in gaps.REQUIRED_DOC_TYPES})

    def test_serious_hazard_raises_category_weight(self):
        w = gaps.analyze(self.s, [], [], hazard_profile=DENVER)["weights"]
        self.assertEqual(w["shelter"], 2)
        self.assertEqual(w["water"], 3)

    def test_active_sorted_worst_first(self):
        self.assertEqual(hazards.active({"ratings": {"A": "Relatively Moderate", "B": "Very High",
                                                     "C": "Very Low"}}), [("B", 5), ("A", 3)])


class LocationScoringTests(unittest.TestCase):
    def setUp(self):
        self.s = dict(DEFAULT_SETTINGS, adults=2, target_days=14)

    def test_vehicle_stock_not_counted_at_home(self):
        items = [item(category="water", quantity=28, per_unit=1, counts_home=0, location_id=5)]
        self.assertEqual(gaps.analyze(self.s, items, [])["water_days"], 0)
        items[0]["counts_home"] = 1
        self.assertEqual(gaps.analyze(self.s, items, [])["water_days"], 14)

    def test_expired_items_in_any_location_are_flagged(self):
        items = [item(expires="2000-01-01", counts_home=0, location_id=5)]
        self.assertEqual(len(gaps.analyze(self.s, items, [])["expired"]), 1)

    def test_car_scores_separately(self):
        car = {"id": 7, "name": "Car", "kind": "vehicle", "people": 2}
        items = [item(id=1, essential="jumper_cables", location_id=7),
                 item(id=2, category="water", quantity=2, per_unit=1, location_id=7),
                 item(id=3, essential="jumper_cables", location_id=8)]  # elsewhere: ignored
        k = kits.score_kit(car, items, self.s, DENVER, today=date(2026, 1, 1))
        lines = {l["id"]: l for l in k["lines"]}
        self.assertEqual(lines["jumper_cables"]["have"], 1)
        self.assertEqual(lines["water"]["have"], 1.0)  # 2 gal / (2 people x 1 gal)
        self.assertEqual(lines["bedding"]["need"], 2)
        self.assertIn("ice_scraper", lines)
        self.assertTrue(0 < k["score"] < 100)

    def test_winter_items_dropped_where_winter_is_not_a_risk(self):
        car = {"id": 7, "name": "Car", "kind": "vehicle", "people": 1}
        lines = {l["id"] for l in kits.score_kit(car, [], self.s, FLORIDA)["lines"]}
        self.assertNotIn("ice_scraper", lines)
        self.assertIn("jumper_cables", lines)

    def test_gobag_targets_three_days_per_person(self):
        bag = {"id": 9, "name": "Bag", "kind": "gobag", "people": 1}
        items = [item(category="water", quantity=3, per_unit=1, location_id=9),
                 item(id=2, category="food", quantity=3, per_unit=2000, location_id=9)]
        k = kits.score_kit(bag, items, self.s, None)
        lines = {l["id"]: l for l in k["lines"]}
        self.assertEqual(lines["water"]["coverage"], 1)
        self.assertEqual(lines["food"]["coverage"], 1)

    def test_home_locations_have_no_kit_score(self):
        self.assertIsNone(kits.score_kit({"id": 1, "name": "Garage", "kind": "home"}, [], self.s))


class ShoppingTests(unittest.TestCase):
    def test_gaps_become_buy_list(self):
        s = dict(DEFAULT_SETTINGS, adults=2, target_days=14)
        items = [item(category="water", quantity=14, per_unit=1)]  # 7 of 14 days
        a = gaps.analyze(s, items, [], hazard_profile=DENVER)
        car = {"id": 7, "name": "Car", "kind": "vehicle", "people": 1}
        rows = kits.shopping_list(a, [kits.score_kit(car, [], s, DENVER)], s)
        water = next(r for r in rows if r["where"] == "Home" and r["label"] == "Drinking water")
        self.assertEqual((water["qty"], water["unit"]), (14.0, "gal"))
        self.assertTrue(any(r["where"] == "Car" and r["essential"] == "jumper_cables" for r in rows))
        self.assertFalse(any(r["label"].startswith("IDs") for r in rows))  # documents aren't bought
        self.assertEqual(rows, sorted(rows, key=lambda r: (-r["importance"], r["where"] != "Home", r["label"])))
        self.assertTrue(any(r["hazard"] for r in rows))


class ApiTests(unittest.TestCase):
    """End-to-end through the HTTP handler, with the Census/FEMA/NWS calls stubbed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.store = Store(Path(cls.tmp.name) / "t.db")
        cls._orig = (sites.geocode, sites.county_for, sites.fetch_alerts, hazards.fetch_county)
        sites.geocode = lambda a: {"lat": 39.74, "lon": -104.98, "matched": a.upper(), "state": "CO",
                                   "county_fips": "08031", "county_name": "Denver County"}
        sites.county_for = lambda lat, lon: {"county_fips": "08031", "county_name": "Denver County"}
        sites.fetch_alerts = lambda lat, lon: [{"id": "x", "event": "Winter Storm Warning", "severity": "Severe",
                                                "headline": "Snow", "onset": "", "ends": "", "expires": ""}]
        hazards.fetch_county = lambda fips: DENVER
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        sites.geocode, sites.county_for, sites.fetch_alerts, hazards.fetch_county = cls._orig
        cls.tmp.cleanup()

    def call(self, method, path, body=None, raw=False):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/{path}", method=method,
                                     headers={"X-Bunkr": "1", "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
            return data.decode() if raw else json.loads(data)

    def test_flow(self):
        car = self.call("POST", "locations", {"name": "Truck", "kind": "vehicle", "people": 2})["id"]
        bag = self.call("POST", "locations", {"name": "Dad go-bag", "kind": "gobag", "person": "Dad"})["id"]
        self.call("POST", "items", {"name": "Cables", "category": "vehicle", "essential": "jumper_cables",
                                    "quantity": 1, "location_id": car})
        jug = self.call("POST", "items", {"name": "Jug", "category": "water", "quantity": 2, "per_unit": 1,
                                          "location": "Pantry"})["id"]
        site = self.call("POST", "sites", {"name": "Home", "kind": "home", "address": "200 E Colfax Ave, Denver"})
        self.assertEqual(site["county_fips"], "08031")
        self.assertEqual(site["alerts"][0]["event"], "Winter Storm Warning")
        st = self.call("GET", "state")
        self.assertEqual({l["name"] for l in st["locations"]}, {"Truck", "Dad go-bag", "Pantry"})
        self.assertEqual({k["name"] for k in st["kits"]}, {"Truck", "Dad go-bag"})
        self.assertTrue(any(l["id"] == "cold_bedding" for l in st["analysis"]["lines"]))
        self.assertEqual(st["alerts"][0]["site"], "Home")
        self.assertTrue(st["shopping"])
        self.call("POST", "items/move", {"ids": [jug], "location_id": bag})
        st = self.call("GET", "state")
        self.assertEqual(next(i for i in st["items"] if i["id"] == jug)["location"], "Dad go-bag")
        self.assertIn("Dad go-bag", self.call("GET", f"packing/{bag}", raw=True))
        self.assertIn("Shopping list", self.call("GET", "shopping.html", raw=True))
        self.assertIn("where,label", self.call("GET", "shopping.csv", raw=True))
        self.call("DELETE", f"locations/{bag}")
        st = self.call("GET", "state")
        self.assertIsNone(next(i for i in st["items"] if i["id"] == jug)["location_id"])

    def test_bad_input_rejected(self):
        for path, body in [("items", {"name": "x", "category": "other", "location_id": 9999}),
                           ("locations", {"name": ""}), ("sites", {"name": "x"}),
                           ("sites", {"name": "x", "lat": 200, "lon": 0})]:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.call("POST", path, body)
            self.assertEqual(cm.exception.code, 400, path)


if __name__ == "__main__":
    unittest.main()
