"""Packs and sizes: a case of 24 x 500 ml bottles counts as 3.17 gallons."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import export, gaps, kits, sync  # noqa: E402
from bunkr.db import Store  # noqa: E402

ML = gaps.GAL_PER["ml"]


class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.db")

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, **kw):
        row = {"name": "x", "category": "water", "quantity": 1, "unit": "", "pack_size": 1, "pack_unit": "",
               "per_unit": 0, "size_unit": "", "essential": "", "location_id": None, "expires": "", "notes": ""}
        row.update(kw)
        return self.store.save_item(row)

    def test_case_of_bottles_counts_in_gallons(self):
        self.add(name="Bottled water", quantity=2, unit="cases", pack_size=24, pack_unit="bottles",
                 per_unit=500 * ML, size_unit="ml")
        item = self.store.items()[0]
        self.assertEqual(gaps.count(item), 48)
        self.assertEqual(gaps.quantity_text(item), "2 cases of 24 × 500 ml bottles = 6.34 gal, 26 lb each")
        a = gaps.analyze(self.store.settings(), self.store.items(), [])
        water = next(l for l in a["lines"] if l["id"] == "water")
        self.assertIn("6.3 gal stored", water["detail"])

    def test_packs_count_toward_essentials_and_kits(self):
        self.add(name="AA batteries", category="power", quantity=1, unit="pack", pack_size=24, pack_unit="AA",
                 essential="batteries")
        a = gaps.analyze(self.store.settings(), self.store.items(), [])
        self.assertEqual(next(l for l in a["lines"] if l["id"] == "batteries")["have"], 24)
        loc = self.store.save_location({"name": "Car", "kind": "vehicle", "people": 1})
        self.add(name="Water", quantity=1, unit="case", pack_size=24, per_unit=500 * ML, size_unit="ml",
                 location_id=loc)
        score = kits.score_all(self.store.locations(), self.store.items(), self.store.settings(), None)
        water = next(l for l in score[0]["lines"] if l["id"] == "water")
        self.assertAlmostEqual(water["have"], 3.17, places=1)

    def test_old_items_and_printouts(self):
        self.add(name="Jug", quantity=3, unit="jugs", per_unit=5)  # made before packs existed
        item = self.store.items()[0]
        self.assertEqual(item["pack_size"], 1)
        self.assertEqual(gaps.quantity_text(item), "3 jugs × 5 gal = 15 gal, 42 lb each")
        self.assertIn("pack_size", export.inventory_csv(self.store.items()).splitlines()[0])

    def test_sync_from_a_node_without_pack_fields(self):
        rec = {"uid": "u1", "updated_at": "2026-11-19T10:00:00", "name": "Cans", "category": "food",
               "quantity": 4, "unit": "cans", "per_unit": 0, "essential": "", "expires": "", "notes": ""}
        sync.apply_records(self.store, {"items": [rec], "locations": [], "tombstones": []}, "")
        self.assertEqual(self.store.items()[0]["pack_size"], 1)
        out = sync.export_records(self.store)
        self.assertEqual(out["items"][0]["pack_size"], 1)

    def test_family_members_set_the_head_count(self):
        self.assertEqual((self.store.settings()["adults"], self.store.settings().get("household_from_people")), (2, None))
        for name, role in (("A", "adult"), ("B", "adult"), ("C", "adult"), ("D", "adult"), ("E", "child")):
            self.store._exec("INSERT INTO people(name, role) VALUES(?,?)", (name, role))
        self.add(name="Bottled water", quantity=4, unit="cases", pack_size=24, per_unit=500 * ML, size_unit="ml")
        s = self.store.settings()
        self.assertEqual((s["adults"], s["children"]), (4, 1))
        water = next(l for l in gaps.analyze(s, self.store.items(), [])["lines"] if l["id"] == "water")
        self.assertEqual(water["have"], 2.5)  # 12.7 gal for 5 people


if __name__ == "__main__":
    unittest.main()
