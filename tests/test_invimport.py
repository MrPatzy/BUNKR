"""Inventory CSV import: template, column guessing, preview (no saving), duplicates and locations."""

import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, export, invimport  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

_preview = mock.patch.dict(os.environ, {"BUNKR_PREVIEW": "csv_import"})   # held back from 1.3.0: off unless switched on


def setUpModule():
    _preview.start()


def tearDownModule():
    _preview.stop()


class LogicTests(unittest.TestCase):
    def test_template_has_export_headers_and_examples(self):
        headers, rows = invimport.read(invimport.template_csv())
        self.assertEqual(headers, invimport.FIELDS)
        self.assertTrue(set(invimport.FIELDS) <= set(export.CSV_FIELDS))
        self.assertGreaterEqual(len(rows), 2)

    def test_export_headers_map_exactly(self):
        headers, _ = invimport.read(export.inventory_csv([]))
        m = invimport.guess(headers)
        for f in invimport.FIELDS:
            self.assertEqual(headers[m[f]], f)

    def test_outside_headers_are_guessed(self):
        m = invimport.guess(["Product", "Qty", "Room", "Best By", "Comments", "Stuff"])
        got = {f: i for f, i in m.items() if i is not None}
        self.assertEqual(got, {"name": 0, "quantity": 1, "location": 2, "expires": 3, "notes": 4})

    def test_row_cleaning(self):
        h, t = invimport.read("Item,Qty,Date,Type\nTomatoes,\"1,200\",6/30/2027,Food\nBad,x,soon,Nonsense\nGun,1,,Firearms\n")
        r = invimport.rows_for(h, t, invimport.guess(h))
        self.assertEqual((r[0]["data"]["quantity"], r[0]["data"]["expires"], r[0]["data"]["category"], r[0]["problem"]),
                         (1200.0, "2027-06-30", "food", ""))
        self.assertTrue(r[1]["problem"])
        self.assertIn("one at a time", r[2]["problem"])


class ImportHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        auth.create_user(self.hub.store, "dad", p3.PW, "admin")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": "dad", "password": p3.PW}).encode(),
                                   method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
        with self.opener.open(r) as resp:
            self.cookie = resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def tearDown(self):
        self.hub.close()

    def req(self, method, path, body=None, raw=False):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.cookie}"})
        try:
            with self.opener.open(r) as resp:
                data = resp.read()
                return resp.status, data.decode() if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def items(self):
        return [i for i in self.req("GET", "/api/state")[1]["items"] if i["category"] != "firearms"]

    def test_template_download(self):
        st, text = self.req("GET", "/api/inventory-template.csv", raw=True)
        self.assertEqual(st, 200)
        self.assertTrue(text.startswith(",".join(invimport.FIELDS)))

    def test_preview_saves_nothing_then_confirm_creates_location(self):
        csv_text = "Product,Qty,Room,Best By\nRice,3,Garage shelf,2027-01-01\n"
        st, v = self.req("POST", "/api/items/import", {"csv": csv_text})
        self.assertEqual(st, 200)
        self.assertTrue(v["rows"][0]["new_location"])
        self.assertEqual(self.items(), [])
        self.assertEqual([l for l in self.req("GET", "/api/state")[1]["locations"] if l["name"] == "Garage shelf"], [])
        st, r = self.req("POST", "/api/items/import", {"csv": csv_text, "mapping": v["mapping"], "confirm": True})
        self.assertEqual((r["added"], r["problems"]), (1, []))
        item = self.items()[0]
        self.assertEqual((item["name"], item["quantity"], item["expires"], item["location"]), ("Rice", 3, "2027-01-01", "Garage shelf"))
        # the same location name is found, not created twice
        self.req("POST", "/api/items/import", {"csv": csv_text.replace("Rice", "Beans"), "confirm": True})
        self.assertEqual(len([l for l in self.req("GET", "/api/state")[1]["locations"] if l["name"] == "Garage shelf"]), 1)

    def test_duplicates_match_on_expiry_and_modes(self):
        base = "name,category,quantity,location,expires\nTomatoes,food,2,Pantry,2027-06-30\n"
        self.req("POST", "/api/items/import", {"csv": base, "confirm": True})
        both = base + "Tomatoes,food,2,Pantry,2028-06-30\n"
        v = self.req("POST", "/api/items/import", {"csv": both})[1]
        self.assertEqual([bool(r["dup"]) for r in v["rows"]], [True, False])  # other expiry is a separate item
        r = self.req("POST", "/api/items/import", {"csv": both, "mode": "add", "confirm": True})[1]
        self.assertEqual((r["added"], r["summed"]), (1, 1))
        self.assertEqual(sorted((i["expires"], i["quantity"]) for i in self.items()), [("2027-06-30", 4), ("2028-06-30", 2)])
        # one choice for the import, changed on a single row (line 2)
        r = self.req("POST", "/api/items/import", {"csv": "name,category,quantity,location,expires\nTomatoes,food,9,Pantry,2027-06-30\n"
                                                         "Tomatoes,food,7,Pantry,2028-06-30\n",
                                                   "mode": "overwrite", "modes": {"3": "new"}, "confirm": True})[1]
        self.assertEqual((r["overwritten"], r["added"]), (1, 1))
        self.assertEqual(sorted((i["expires"], i["quantity"]) for i in self.items()),
                         [("2027-06-30", 9), ("2028-06-30", 2), ("2028-06-30", 7)])

    def test_add_as_new_and_firearms_skipped(self):
        base = "name,category,quantity,expires\nSalt,food,1,\n"
        self.req("POST", "/api/items/import", {"csv": base, "confirm": True})
        r = self.req("POST", "/api/items/import", {"csv": base + "Pistol,firearms,1,\n", "mode": "new", "confirm": True})[1]
        self.assertEqual((r["added"], len(r["problems"])), (1, 1))
        self.assertEqual(len(self.items()), 2)
        self.assertEqual(self.req("POST", "/api/items/import", {"csv": ""})[0], 400)


if __name__ == "__main__":
    unittest.main()
