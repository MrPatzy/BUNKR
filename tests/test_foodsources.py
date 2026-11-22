"""More barcode sources (UPCitemdb, USDA) and reading calories off a nutrition label photo."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, foodsources as fs, labelread, naming, quickadd  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

BEANS = "039400016113"
USDA = {"foods": [{"fdcId": 2498241, "gtinUpc": "039400016113", "description": "ORIGINAL SEASONED WITH BACON & BROWN SUGAR BAKED BEANS, ORIGINAL",
                   "brandOwner": "Bush Brothers & Company ", "brandName": "BUSH'S", "servingSize": 130.0, "servingSizeUnit": "GRM",
                   "packageWeight": "16.5 oz/468 g", "foodNutrients": [{"nutrientId": 1003, "unitName": "G", "value": 5},
                                                                       {"nutrientId": 1008, "unitName": "KCAL", "value": 115}]}]}
UPC = {"code": "OK", "total": 1, "items": [{"upc": BEANS, "title": "BUSH'S Original Baked Beans 16.5 oz", "brand": "BUSH S",
                                            "size": "", "category": "Food, Beverages & Tobacco > Food Items"}]}
SOUP = {"code": "OK", "items": [{"title": "CAMPBELL'S SOUP CREAM MUSHROOM", "brand": "Campbell's", "size": "10.75 Ounce Can and 48 per case",
                                 "category": "Food, Beverages & Tobacco > Food Items > Soups & Broths"}]}
LABEL = """Nutrition Facts
3.5 servings per container
Serving size 1/2 cup (130g)
Amount per serving
Calories 14O
Total Fat 1.5g 2%"""


def fake(routes):
    """A stand-in for the internet: the first route whose key is in the URL answers; anything else is a 404."""
    asked = []

    def get(url):
        asked.append(url)
        for k, v in routes.items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return v
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    get.asked = asked
    return get


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.st = Store(self.folder / "f.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_grams(self):
        self.assertEqual((fs.grams("16.5 oz/468 g"), fs.grams("15 OZ"), fs.grams("1 lb"), fs.grams("")), (468.0, 425.2, 453.6, 0))

    def test_usda(self):
        d = fs.usda_lookup(self.st, BEANS, get=fake({"api.nal.usda.gov": USDA}))
        self.assertEqual((d["per_unit"], d["category"]), (538, "food"))       # 115 kcal per 100 g x 468 g
        self.assertIsNone(fs.usda_lookup(self.st, "012345678905", get=fake({"api.nal.usda.gov": USDA})))   # another code: no match
        g = fake({"api.nal.usda.gov": urllib.error.HTTPError("u", 429, "busy", {}, None)})
        with self.assertRaises(urllib.error.HTTPError):
            fs.usda_lookup(self.st, BEANS, get=g)
        self.assertIn("own free key", fs.view(self.st, self.folder)["status"]["usda"]["note"])
        self.assertIn("api_key=DEMO_KEY", g.asked[0])

    def test_upcitemdb(self):
        d = fs.upcitemdb_lookup(self.st, BEANS, get=fake({"upcitemdb": UPC}))
        self.assertEqual(naming.render(naming.DEFAULT, d["parts"]), "[Dry, Canned] Baked Beans, Original, Canned, 16.5 oz")
        self.assertTrue(d["name"].startswith("BUSH'S"))                        # "BUSH S" -> the title's own spelling
        d = fs.upcitemdb_lookup(self.st, "051000012616", get=fake({"upcitemdb": SOUP}))
        self.assertEqual((d["unit"], d["parts"]["size"]), ("cans", "10.75 oz"))
        self.assertIsNone(fs.upcitemdb_lookup(self.st, BEANS, get=fake({"upcitemdb": {"code": "OK", "items": []}})))

    def test_order_and_calories(self):
        g = fake({"upcitemdb": UPC, "api.nal.usda.gov": USDA})
        r = quickadd.lookup(self.st, BEANS, get=g)                           # Open Food Facts 404s: UPCitemdb names it, USDA counts it
        self.assertEqual((r["source"], r["draft"]["per_unit"], r["draft"]["kcal_from"]), ("upcitemdb", 538, "USDA"))
        self.assertEqual(r["draft"]["name"], "[Dry, Canned] Baked Beans, Original, Canned, 16.5 oz")
        r = quickadd.lookup(self.st, BEANS, get=fake({"api.nal.usda.gov": USDA}))
        self.assertEqual((r["source"], r["draft"]["per_unit"]), ("usda", 538))
        fs.save_settings(self.st, self.folder, {"usda": False, "upcitemdb": False})
        g = fake({"upcitemdb": UPC, "api.nal.usda.gov": USDA})
        self.assertEqual(quickadd.lookup(self.st, BEANS, get=g), {"code": BEANS, "source": "none"})
        self.assertTrue(all("openfoodfacts" in u for u in g.asked))          # switched off: never asked

    def test_settings_key_in_a_file(self):
        with self.assertRaises(ValueError):
            fs.save_settings(self.st, self.folder, {"usda_key": "short"})
        v = fs.save_settings(self.st, self.folder, {"usda_key": "A" * 40})
        self.assertTrue(v["usda_own_key"])
        self.assertNotIn("AAAA", json.dumps(v) + json.dumps(self.st.get_raw("food_sources", {})))
        self.assertEqual((self.folder / "usda.key").stat().st_mode & 0o777, 0o600)
        g = fake({"api.nal.usda.gov": USDA})
        fs.usda_lookup(self.st, BEANS, get=g)
        self.assertIn("api_key=" + "A" * 40, g.asked[0])


class LabelTests(unittest.TestCase):
    def test_parse(self):
        r = labelread.parse(LABEL)
        self.assertEqual((r["kcal"], r["servings"], r["total"], r["serving"]), (140, 3.5, 490, "1/2 cup (130g)"))
        r = labelread.parse("Servings Per Container about 2\nCalories 250 Calories from Fat 110")
        self.assertEqual((r["kcal"], r["servings"], r["total"]), (250, 2.0, 500))
        r = labelread.parse("Calories\n 90\nno servings line")
        self.assertEqual((r["kcal"], r["servings"], r["total"]), (90, 0.0, 0))
        self.assertEqual(labelread.parse("a photo of a cat")["kcal"], 0)

    def test_ocr_checks(self):
        with self.assertRaises(ValueError):
            labelread.ocr(b"<svg>not a photo</svg>")
        with mock.patch.object(labelread, "ready", lambda: False):
            with self.assertRaises(RuntimeError):
                labelread.ocr(b"\xff\xd8\xff\xe0 jpeg")
        done = mock.Mock(returncode=0, stdout=LABEL.encode())
        with mock.patch.object(labelread, "ready", lambda: True), mock.patch.object(labelread.subprocess, "run", return_value=done) as run:
            self.assertEqual(labelread.read(b"\x89PNG...")["total"], 490)
        self.assertEqual(run.call_args[0][0][:3], ["tesseract", "stdin", "stdout"])


class HttpTests(unittest.TestCase):
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

    def tearDown(self):
        self.hub.close()

    def req(self, method, path, body=None, user="dad", raw=None):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.hub.url + path, method=method, data=data,
                                   headers={"X-Bunkr": "1", "Content-Type": "image/jpeg" if raw is not None else "application/json",
                                            "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_routes(self):
        self.assertEqual(self.req("GET", "/api/foodsources", user="kid")[0], 403)
        code, v = self.req("POST", "/api/foodsources", {"upcitemdb": False})
        self.assertEqual((code, v["sources"]["upcitemdb"], v["sources"]["usda"]), (200, False, True))
        self.assertEqual(self.req("POST", "/api/label", raw=b"not a photo", user="kid")[0], 400)   # anyone can read a label
        with mock.patch.object(labelread, "ready", lambda: False):
            code, v = self.req("POST", "/api/label", raw=b"\xff\xd8\xff\xe0 jpeg", user="kid")
        self.assertEqual(code, 503)
        self.assertIn("tesseract", v["error"])


if __name__ == "__main__":
    unittest.main()
