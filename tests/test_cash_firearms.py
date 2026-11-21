"""Cash counted in dollars; firearms kept to admins, serial numbers kept to the hub."""

import ipaddress
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, export, gaps, kits, server, sync  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

BASE = {"name": "x", "category": "other", "quantity": 1, "unit": "", "pack_size": 1, "pack_unit": "", "per_unit": 0,
        "size_unit": "", "essential": "", "location_id": None, "expires": "", "notes": "", "subtype": "", "caliber": "",
        "serial": ""}


def add(store, **kw):
    return store.save_item({**BASE, **kw})


class CashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_bills_by_denomination_add_up(self):
        add(self.store, name="$20 bills", category="documents", essential="cash", quantity=10, unit="bills", per_unit=20)
        add(self.store, name="$5 bills", category="documents", essential="cash", quantity=20, unit="bills", per_unit=5)
        add(self.store, name="Envelope", category="documents", essential="cash", quantity=60)  # a dollar amount
        s = self.store.settings()
        a = gaps.analyze(s, self.store.items(), [])
        cash = next(l for l in a["lines"] if l["id"] == "cash")
        self.assertEqual((cash["have"], cash["need"], cash["unit"]), (360, 200, "dollars"))  # 2 adults x $100
        texts = sorted(gaps.quantity_text(i) for i in self.store.items())
        self.assertEqual(texts, ["$60", "10 bills × $20 = $200", "20 bills × $5 = $100"])

    def test_shortfall_is_bought_in_dollars(self):
        add(self.store, name="$1 bills", category="documents", essential="cash", quantity=50, per_unit=1)
        s = self.store.settings()
        a = gaps.analyze(s, self.store.items(), [])
        row = next(r for r in kits.shopping_list(a, [], s) if r["essential"] == "cash")
        self.assertEqual((row["qty"], row["unit"]), (150, "dollars"))


class FirearmTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        st = self.hub.store
        auth.create_user(st, "dad", p3.PW, "admin")
        auth.create_user(st, "kid", p3.PW, "family")
        add(st, name="Glock 19", category="firearms", subtype="firearm", caliber="9mm", serial="ABC123")
        add(st, name="9mm FMJ", category="firearms", subtype="ammo", caliber="9MM ", quantity=4, unit="boxes",
            pack_size=50, pack_unit="rounds")
        add(st, name="Remington 870", category="firearms", subtype="firearm", caliber="12 ga", serial="RR999")
        add(st, name="Flashlight", category="power", essential="flashlight")
        self.opener = p3.urllib.request.build_opener(p3.NoRedirect)

    req = p3.LoginHttpTests.req
    login = p3.LoginHttpTests.login

    def tearDown(self):
        server.OPEN_NETS[:] = []
        self.hub.close()

    def test_ammo_summary(self):
        rows = gaps.ammo_summary(self.hub.store.items())
        self.assertEqual([(r["caliber"], r["rounds"], r["firearms"]) for r in rows],
                         [("12 ga", 0, ["Remington 870"]), ("9mm", 200, ["Glock 19"])])

    def test_ammo_types_and_magazines(self):
        st = self.hub.store
        add(st, name="9mm HST", category="firearms", subtype="ammo", caliber="9mm", ammo_type="defensive", quantity=2,
            unit="boxes", pack_size=50)
        glock = next(i for i in st.items() if i["name"] == "Glock 19")
        add(st, name="Glock 15-rd", category="firearms", subtype="magazine", caliber="9mm", capacity=15, quantity=3,
            mag_status="loaded", fits=glock["uid"])
        add(st, name="Glock 15-rd", category="firearms", subtype="magazine", caliber="9mm", capacity=15, quantity=1,
            mag_status="service", fits=glock["uid"])
        add(st, name="Spare mag", category="firearms", subtype="magazine", capacity=10, mag_status="empty")
        nine = next(r for r in gaps.ammo_summary(st.items()) if r["caliber"] == "9mm")
        self.assertEqual((nine["rounds"], nine["in_mags"]), (300, 45))
        self.assertEqual(nine["by_type"], {"Type not set": 200, "Defensive (hollow point)": 100})
        mags = {m["firearm"]: m for m in gaps.magazine_summary(st.items())}
        self.assertEqual({k: mags["Glock 19"][k] for k in ("magazines", "loaded", "service", "capacity")},
                         {"magazines": 4, "loaded": 3, "service": 1, "capacity": [15.0]})
        self.assertEqual(mags["Remington 870"]["magazines"], 0)
        self.assertEqual(mags["Not matched to a firearm"]["magazines"], 1)
        loaded = next(i for i in st.items() if i["mag_status"] == "loaded")
        self.assertEqual(gaps.quantity_text(loaded), "3 magazines, 15 rounds each, loaded")
        record = server.archive_extras()["Firearms record.html"]
        self.assertIn("Defensive (hollow point): 100", record)
        self.assertIn("<h2>Magazines</h2>", record)

    def test_only_admins_see_firearms(self):
        import json
        names = lambda who: {i["name"] for i in json.loads(self.req("GET", "/api/state", cookie=self.login(who))[2])["items"]}
        self.assertIn("Glock 19", names("dad"))
        self.assertEqual(names("kid"), {"Flashlight"})
        self.assertNotIn(b"Glock", self.req("GET", "/api/report.html", cookie=self.login("kid"))[2])
        self.assertNotIn(b"Glock", self.req("GET", "/api/export.csv", cookie=self.login("kid"))[2])
        server.OPEN_NETS[:] = [ipaddress.ip_network("127.0.0.0/8")]  # field Wi-Fi, no login
        body = self.req("GET", "/api/report.html")[2]
        self.assertIn(b"Flashlight", body)
        self.assertNotIn(b"Glock", body)

    def test_serials_stay_on_the_hub_and_in_the_archive(self):
        out = sync.export_records(self.hub.store)
        self.assertTrue(out["items"] and all("serial" not in i for i in out["items"]))
        # a node's edit (no serial field) keeps the hub's serial number
        glock = next(i for i in out["items"] if i["name"] == "Glock 19")
        sync.apply_records(self.hub.store, {"items": [{**glock, "notes": "cleaned", "updated_at": "2999-01-01T00:00:00"}],
                                            "locations": [], "tombstones": []}, "")
        row = next(i for i in self.hub.store.items() if i["name"] == "Glock 19")
        self.assertEqual((row["serial"], row["notes"]), ("ABC123", "cleaned"))
        extras = server.archive_extras()
        self.assertIn("ABC123", extras["Firearms record.html"])
        self.assertIn("200", extras["Firearms record.html"])
        # plain USB inventory files leave firearms out
        self.assertNotIn("Glock", export.inventory_csv(gaps.visible(self.hub.store.items(), "family")))


if __name__ == "__main__":
    unittest.main()
