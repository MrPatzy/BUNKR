"""Kroger barcode lookups: product ids, drafts, the token, the fallback after Open Food Facts, and the admin settings."""

import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, kroger, quickadd  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

BEANS = {"data": {"productId": "0001111083809", "brand": "Kroger", "description": "Kroger® Black Beans",
                  "categories": ["Canned & Packaged"], "items": [{"size": "15 oz"}]}}
WATER = {"data": {"brand": "Kroger", "description": "Purified Drinking Water", "categories": ["Beverages"], "items": [{"size": "1 gal"}]}}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeKroger:
    def __init__(self, products=None, token_ok=True):
        self.products, self.token_ok, self.asked = products or {}, token_ok, []

    def open(self, req, timeout=None):
        url = req.full_url
        self.asked.append((url, req.get_header("Authorization")))
        if url.endswith("/connect/oauth2/token"):
            if not self.token_ok:
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
            assert req.data == b"grant_type=client_credentials&scope=product.compact"
            return Resp(json.dumps({"access_token": "tok", "expires_in": 1800}).encode())
        pid = url.rsplit("/", 1)[1]
        if pid in self.products:
            return Resp(json.dumps(self.products[pid]).encode())
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


class KrogerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.st = Store(self.folder / "k.db")
        kroger._token.update(value="", until=0.0)

    def tearDown(self):
        kroger._token.update(value="", until=0.0)
        self.tmp.cleanup()

    def setup_key(self):
        kroger.save_settings(self.st, self.folder, {"client_id": "bunkr-test-1234", "client_secret": "s3cret-abcdefghijklmnop"})

    def test_ids_and_drafts(self):
        self.assertEqual(kroger.product_ids("011110838094"), ["0001111083809", "0011110838094"])  # UPC-A
        self.assertEqual(kroger.product_ids("0011110838094"), ["0001111083809", "0011110838094"])  # same, as EAN-13
        d = kroger.to_draft(BEANS["data"])
        self.assertEqual((d["name"], d["category"], d["note"]), ("Kroger Black Beans", "food", "Kroger Black Beans · 15 oz"))  # no doubled brand, no ®
        w = kroger.to_draft(WATER["data"])
        self.assertEqual((w["name"], w["category"], w["per_unit"], w["size_unit"]), ("Kroger Purified Drinking Water", "water", 1.0, "gal"))
        self.assertIsNone(kroger.to_draft({"brand": "Kroger"}))

    def test_calories_packs_and_storage(self):
        def nutrition(kcal, servings):
            return [{"servingsPerPackage": {"value": servings},
                     "nutrients": [{"code": "NA", "displayName": "Sodium", "quantity": 105}, {"code": "ENER-", "displayName": "Calories", "quantity": kcal}]}]
        four = {"brand": "Kroger", "description": "Kroger® Black Beans BIG DEAL!", "categories": ["Canned & Packaged"],
                "items": [{"size": "4 ct / 15.25 oz"}], "temperature": {"indicator": "Ambient"}, "nutritionInformation": nutrition(110, 14)}
        d = kroger.to_draft(four)
        self.assertEqual((d["unit"], d["pack_size"], d["pack_unit"], d["per_unit"]), ("packs", 4, "cans", 385))  # 110 x 14 / 4 a can
        self.assertEqual(d["parts"]["size"], "15.25 oz")
        self.assertNotIn("BIG DEAL", d["note"])
        one = dict(four, description="Simple Truth Organic® Low Sodium Black Beans", brand="Simple Truth Organic",
                   organicClaimName="ORGANIC CLAIM AND PRINTED ON PACKAGE", items=[{"size": "15 oz"}], nutritionInformation=nutrition(110, 3.5))
        d = kroger.to_draft(one)
        self.assertEqual((d["unit"], d["pack_size"], d["per_unit"]), ("cans", 1, 385))
        from bunkr import naming
        self.assertEqual(naming.render(naming.DEFAULT, d["parts"]), "[Dry, Canned] Beans, Black, Low Sodium, Organic, Canned, 15 oz")
        ice = dict(one, description="Kroger Sweet Peas", brand="Kroger", categories=["Frozen"], temperature={"indicator": "Frozen"},
                   organicClaimName="NO", items=[{"size": "12 oz"}])
        self.assertEqual(naming.render(naming.DEFAULT, kroger.to_draft(ice)["parts"]), "[Frozen, Vegetables] Peas, Sweet, 12 oz")
        self.assertEqual(kroger.split_size("24 ct / 16.9 fl oz"), (24, "16.9 fl oz"))
        self.assertEqual(kroger.split_size("1 gal"), (1, "1 gal"))

    def test_settings_keep_the_secret_in_a_file(self):
        self.assertFalse(kroger.view(self.st, self.folder)["ready"])
        v = kroger.save_settings(self.st, self.folder, {"client_id": "bunkr-test-1234", "client_secret": "s3cret-abcdefghijklmnop"})
        self.assertTrue(v["ready"])
        self.assertNotIn("s3cret", json.dumps(v))
        self.assertNotIn("s3cret", json.dumps(self.st.get_raw("kroger", {})))
        self.assertEqual((self.folder / "kroger.key").stat().st_mode & 0o777, 0o600)
        for bad in ({"client_id": "a b"}, {"client_secret": "short"}):
            with self.assertRaises(ValueError):
                kroger.save_settings(self.st, self.folder, bad)
        self.assertFalse(kroger.save_settings(self.st, self.folder, {"forget_secret": True})["ready"])

    def test_lookup(self):
        gh = FakeKroger()
        self.assertIsNone(kroger.lookup(self.st, "011110838094", opener=gh))
        self.assertEqual(gh.asked, [])                                            # not set up: nothing sent
        self.setup_key()
        gh = FakeKroger({"0011110838094": BEANS})
        self.assertEqual(kroger.lookup(self.st, "011110838094", opener=gh)["name"], "Kroger Black Beans")
        self.assertTrue(gh.asked[0][1].startswith("Basic "))
        self.assertEqual([u.rsplit("/", 1)[1] for u, _ in gh.asked[1:]], ["0001111083809", "0011110838094"])  # tried both ids
        self.assertEqual(gh.asked[1][1], "Bearer tok")
        self.assertTrue(kroger.view(self.st, self.folder)["status"]["found"])
        kroger._token.update(value="", until=0.0)
        with self.assertRaises(RuntimeError):
            kroger.lookup(self.st, "011110838094", opener=FakeKroger(token_ok=False))
        self.assertIn("didn't accept", kroger.view(self.st, self.folder)["status"]["error"])

    def test_after_open_food_facts(self):
        self.setup_key()
        kroger._token.update(value="tok", until=9e18)
        real = kroger._json
        fake = FakeKroger({"0001111083809": BEANS})
        kroger._json = lambda req, opener=None: real(req, fake)

        def missing(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        try:
            r = quickadd.lookup(self.st, "011110838094", get=missing)
            self.assertEqual((r["source"], r["draft"]["name"]), ("kroger", "[Canned] Beans, Black, Canned, 15 oz"))  # the household's naming
            r = quickadd.lookup(self.st, "099999999999", get=missing)
            self.assertEqual(r, {"code": "099999999999", "source": "none"})      # neither has it: plain not found
            r = quickadd.lookup(self.st, "011110838094", get=lambda url: {"status": 1, "product": {"product_name": "Beans"}})
            self.assertEqual(r["source"], "openfoodfacts")                        # Open Food Facts still goes first
        finally:
            kroger._json = real


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
        kroger._token.update(value="", until=0.0)
        self.hub.close()

    def req(self, method, path, body=None, user="dad"):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_admin_only_and_no_secret_out(self):
        self.assertEqual(self.req("GET", "/api/kroger", user="kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/kroger/test")[0], 400)           # not set up yet
        code, v = self.req("POST", "/api/kroger", {"client_id": "bunkr-test-1234", "client_secret": "s3cret-abcdefghijklmnop"})
        self.assertEqual((code, v["ready"], v["has_secret"]), (200, True, True))
        self.assertNotIn("s3cret", json.dumps(self.req("GET", "/api/kroger")[1]))

    def test_naming_route(self):
        self.assertEqual(self.req("GET", "/api/naming", user="kid")[0], 403)
        code, v = self.req("POST", "/api/naming", {"template": "{item}, {details}, {size}"})
        self.assertEqual((code, v["example"]), (200, "Beans, Black, Low Sodium, Organic, 15 oz"))
        self.assertEqual(self.req("POST", "/api/naming", {"template": "{nope}"})[0], 400)
        code, v = self.req("POST", "/api/naming/preview", {"template": "{item} ({size})", "caps": "upper"})
        self.assertEqual((code, v["examples"][0]), (200, "BEANS (15 OZ)"))
        self.assertEqual(self.req("GET", "/api/naming")[1]["template"], "{item}, {details}, {size}")   # preview doesn't save
        self.assertEqual(self.req("POST", "/api/naming/preview", {"template": "{item}"}, user="kid")[0], 403)


if __name__ == "__main__":
    unittest.main()
