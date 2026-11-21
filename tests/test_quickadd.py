"""Fast entry: pasted lists, barcode memory, Open Food Facts drafts, bulk add and the API."""

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, quickadd, server  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

BEANS = {"status": 1, "product": {"product_name": "Black Beans", "brands": "Bush's, Other", "product_quantity": "425",
                                  "product_quantity_unit": "g", "quantity": "15 oz", "nutriments": {"energy-kcal_100g": 91},
                                  "categories_tags": ["en:plant-based-foods", "en:legumes"]}}
WATER = {"status": 1, "product": {"product_name": "Spring Water", "product_quantity": 500, "categories_tags": ["en:beverages", "en:waters"]}}


class ParseTests(unittest.TestCase):
    def test_lines(self):
        got = quickadd.parse("12 cans black beans\n3 x 5 gal water\n\n$12.99\n- [x] 2 boxes bandages $4.99\nAA batteries x 24\n"
                             "2 gal water\nWater filter\n1 dozen eggs\nflashlight\n500 rounds 9mm")
        by = {g["name"]: g for g in got}
        self.assertEqual((by["black beans"]["quantity"], by["black beans"]["unit"], by["black beans"]["category"]), (12, "cans", "food"))
        jugs = next(g for g in got if g["name"] == "water")
        self.assertEqual((jugs["quantity"], jugs["per_unit"], jugs["size_unit"], jugs["unit"]), (3, 5.0, "gal", "jugs"))
        self.assertEqual((by["bandages"]["quantity"], by["bandages"]["category"]), (2, "medical"))
        self.assertEqual((by["AA batteries"]["quantity"], by["AA batteries"]["category"]), (24, "power"))
        self.assertEqual(by["Water filter"]["category"], "shelter")  # a filter is a tool, not water
        self.assertEqual((by["eggs"]["quantity"], by["eggs"]["category"]), (12, "food"))
        self.assertEqual(by["rounds 9mm"]["category"], "other")  # never guessed as a firearm
        self.assertEqual(len(got), 9)  # the price-only and blank lines are skipped
        two = [g for g in got if g["name"] == "water" and g["per_unit"] == 1.0]
        self.assertEqual(two[0]["quantity"], 2)

    def test_off_drafts(self):
        d = quickadd.from_off(BEANS["product"])
        self.assertEqual((d["name"], d["category"], d["per_unit"]), ("Bush's Black Beans", "food", round(91 * 425 / 100)))
        w = quickadd.from_off(WATER["product"])
        self.assertEqual((w["category"], w["size_unit"], w["per_unit"]), ("water", "ml", round(500 / 3785.411784, 4)))
        self.assertIsNone(quickadd.from_off({}))
        with self.assertRaises(ValueError):
            quickadd.clean_code("12ab")


class BarcodeHttpTests(unittest.TestCase):
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
        self.asked = []
        self.real = quickadd._get
        quickadd._get = lambda url: self.asked.append(url) or (BEANS if "039400016061" in url else {"status": 0})
        quickadd.lookup.__defaults__ = (quickadd._get, True)

    def tearDown(self):
        quickadd._get = self.real
        quickadd.lookup.__defaults__ = (self.real, True)
        self.hub.close()

    def req(self, method, path, body=None, user="dad"):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_scan_save_rescan_bump(self):
        st, r = self.req("POST", "/api/barcode", {"code": "0 39400 01606 1"}, user="kid")
        self.assertEqual((st, r["source"], r["draft"]["category"]), (200, "openfoodfacts", "food"))
        self.assertIn("/039400016061.json", self.asked[0])  # spaces stripped; only the number goes out
        st, r = self.req("POST", "/api/items", {"name": r["draft"]["name"], "category": "food", "quantity": 4, "per_unit": r["draft"]["per_unit"],
                                                "barcode": "039400016061"}, user="kid")
        self.assertEqual(st, 200)
        iid = r["id"]
        st, r = self.req("POST", "/api/barcode", {"code": "039400016061"}, user="kid")
        self.assertEqual((r["source"], r["item"]["id"]), ("saved", iid))
        self.assertEqual(len(self.asked), 1)  # a rescan doesn't go online
        self.assertEqual(self.req("POST", f"/api/items/{iid}/bump", {"amount": 1}, user="kid")[1]["quantity"], 5)
        self.assertEqual(self.req("POST", f"/api/items/{iid}/bump", {"amount": -9})[1]["quantity"], 0)
        self.assertEqual(self.req("POST", "/api/barcode", {"code": "12"})[0], 400)
        self.assertEqual(self.req("POST", "/api/barcode", {"code": "123456789"})[1]["source"], "none")

    def test_firearms_stay_admin_only(self):
        st, r = self.req("POST", "/api/items", {"name": "Ammo box", "category": "firearms", "subtype": "ammo", "quantity": 1, "barcode": "765432109"})
        gid = r["id"]
        self.assertEqual(self.req("POST", "/api/barcode", {"code": "765432109"}, user="kid")[1], {"code": "765432109", "source": "none"})
        self.assertEqual(self.req("POST", "/api/barcode", {"code": "765432109"})[1]["item"]["id"], gid)
        self.assertEqual(self.req("POST", f"/api/items/{gid}/bump", {"amount": 1}, user="kid")[0], 404)

    def test_paste_preview_and_bulk(self):
        st, r = self.req("POST", "/api/items/parse", {"text": "12 cans beans\n3 x 5 gal water"}, user="kid")
        self.assertEqual(len(r["items"]), 2)
        rows = r["items"] + [{"name": "Rifle", "category": "firearms"}, {"name": "", "category": "food"}]
        st, res = self.req("POST", "/api/items/bulk", {"items": rows, "location": "Pantry"}, user="kid")
        self.assertEqual((st, res["added"], len(res["problems"])), (200, 2, 2))
        items = {i["name"]: i for i in self.req("GET", "/api/state")[1]["items"]}
        self.assertEqual((items["water"]["per_unit"], items["water"]["size_unit"]), (5.0, "gal"))
        self.assertTrue(items["beans"]["location_id"])
        self.assertGreater(self.req("GET", "/api/state")[1]["analysis"]["water_days"], 0)
        self.assertEqual(self.req("POST", "/api/items/bulk", {"items": []})[0], 400)


if __name__ == "__main__":
    unittest.main()
