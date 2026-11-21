"""Sub-locations: depth limit, shared rules with the top level, paths, migration of flat room-prefixed locations."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import db, gaps, kits, sync  # noqa: E402
from bunkr.db import DEFAULT_SETTINGS, Store  # noqa: E402


def make_item(store, name, loc, **kw):
    base = {"name": name, "category": "other", "quantity": 1, "unit": "", "per_unit": 0, "essential": "",
            "location_id": loc, "expires": "", "notes": ""}
    return store.save_item(base | kw)


class SubLocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        s = self.store
        self.room = s.save_location({"name": "Master bedroom", "kind": "home", "counts_home": 1})
        self.bed = s.save_location({"name": "Under bed", "parent_id": self.room})
        self.safe = s.save_location({"name": "Gun safe", "parent_id": self.bed})

    def tearDown(self):
        self.store.close() if hasattr(self.store, "close") else None
        self.tmp.cleanup()

    def test_paths_and_depth(self):
        locs = {l["id"]: l for l in self.store.locations()}
        self.assertEqual(locs[self.safe]["name"], "Master bedroom > Under bed > Gun safe")
        self.assertEqual([locs[i]["depth"] for i in (self.room, self.bed, self.safe)], [0, 1, 2])

    def test_third_level_refused(self):
        with self.assertRaises(ValueError):
            self.store.save_location({"name": "Too deep", "parent_id": self.safe})

    def test_every_kind_can_nest_and_inherits_the_top_level(self):
        s = self.store
        car = s.save_location({"name": "4Runner", "kind": "vehicle", "people": 3, "counts_home": 0})
        panel = s.save_location({"name": "Rear MOLLE panel", "parent_id": car, "kind": "home", "counts_home": 1})
        bag = s.save_location({"name": "Dad's bag", "kind": "gobag", "counts_home": 1})
        pocket = s.save_location({"name": "Top pocket", "parent_id": bag})
        locs = {l["id"]: l for l in s.locations()}
        self.assertEqual((locs[panel]["kind"], locs[panel]["people"], locs[panel]["counts_home"]), ("vehicle", 3, 0))
        self.assertEqual(locs[pocket]["kind"], "gobag")
        self.assertEqual(locs[panel]["name"], "4Runner > Rear MOLLE panel")

    def test_same_leaf_name_in_different_rooms(self):
        other = self.store.save_location({"name": "Guest room", "kind": "home"})
        self.store.save_location({"name": "Under bed", "parent_id": other})
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_location({"name": "under bed", "parent_id": other})

    def test_items_in_any_level_count_like_the_top_level(self):
        s = self.store
        for loc in (self.room, self.bed, self.safe):
            make_item(s, "w", loc, quantity=1)
        by_name = {i["location"]: i for i in s.items()}
        self.assertEqual(set(by_name), {"Master bedroom", "Master bedroom > Under bed",
                                        "Master bedroom > Under bed > Gun safe"})
        self.assertTrue(all(i["top_location_id"] == self.room and i["counts_home"] == 1 for i in s.items()))
        # a vehicle's sub-location keeps its items out of home supply, and scores with the vehicle
        car = s.save_location({"name": "Car", "kind": "vehicle", "people": 1, "counts_home": 0})
        trunk = s.save_location({"name": "Trunk", "parent_id": car})
        boot = s.save_location({"name": "Tool bag", "parent_id": trunk})
        make_item(s, "Cables", trunk, essential="jumper_cables")
        make_item(s, "Cables2", boot, essential="jumper_cables")
        items = s.items()
        self.assertTrue(all(i["counts_home"] == 0 for i in items if i["location"].startswith("Car")))
        scores = kits.score_all(s.locations(), items, dict(DEFAULT_SETTINGS), None)
        self.assertEqual([k["name"] for k in scores], ["Car"])  # sub-locations aren't scored on their own
        line = next(l for l in scores[0]["lines"] if l["id"] == "jumper_cables")
        self.assertEqual(line["have"], 2)
        self.assertEqual(scores[0]["item_count"], 2)

    def test_home_readiness_counts_sub_location_items(self):
        s = self.store
        make_item(s, "Water", self.safe, category="water", quantity=10, per_unit=1)
        home = gaps.analyze(dict(DEFAULT_SETTINGS), s.items(), [])
        flat = Store(Path(self.tmp.name) / "f.db")
        lid = flat.save_location({"name": "Room", "kind": "home", "counts_home": 1})
        make_item(flat, "Water", lid, category="water", quantity=10, per_unit=1)
        base = gaps.analyze(dict(DEFAULT_SETTINGS), flat.items(), [])
        self.assertEqual(home["overall"], base["overall"])
        self.assertGreater(home["overall"], 0)

    def test_rename_updates_paths_below(self):
        self.store.save_location({"name": "Main bedroom"}, self.room)
        names = {l["id"]: l["name"] for l in self.store.locations()}
        self.assertEqual(names[self.safe], "Main bedroom > Under bed > Gun safe")

    def test_cannot_delete_with_sub_locations(self):
        with self.assertRaises(ValueError):
            self.store.delete_location(self.room)
        self.store.delete_location(self.safe)

    def test_sync_carries_nesting(self):
        out = sync.export_records(self.store)
        locs = {l["name"]: l for l in out["locations"]}
        self.assertEqual(locs["Master bedroom > Under bed"]["parent_uid"], locs["Master bedroom"]["uid"])
        other = Store(Path(self.tmp.name) / "other.db")
        sync.apply_records(other, {"locations": list(reversed(out["locations"])), "items": [], "tombstones": []}, "")
        got = {l["name"]: l for l in other.locations()}
        self.assertEqual(got["Master bedroom > Under bed > Gun safe"]["depth"], 2)
        self.assertEqual(got["Master bedroom > Under bed"]["label"], "Under bed")


class MigrationTests(unittest.TestCase):
    def test_flat_room_prefixed_locations_are_nested(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.executescript(db.BASE_SCHEMA)
            for m in db.MIGRATIONS[:10]:
                m(conn)
            conn.execute("PRAGMA user_version=10")
            for name in ("Master bedroom radio briefcase", "Master bedroom power outage kit", "Basement shelf",
                         "Basement Storeroom", "Truck"):
                kind = "vehicle" if name == "Truck" else "home"
                conn.execute("INSERT INTO locations(name, kind, uid, updated_at) VALUES(?,?,?,?)",
                             (name, kind, name.replace(" ", "-"), "2026-01-01"))
            conn.execute("INSERT INTO items(name, category, location_id) "
                         "SELECT 'Radio', 'other', id FROM locations WHERE name='Master bedroom radio briefcase'")
            conn.execute("INSERT INTO items(name, category, location_id) "
                         "SELECT 'Lantern', 'other', id FROM locations WHERE name='Truck'")
            conn.commit()
            conn.close()
            store = Store(path)
            names = sorted(l["name"] for l in store.locations())
            self.assertEqual(names, ["Basement", "Basement > Shelf", "Basement Storeroom", "Master bedroom",
                                     "Master bedroom > Power outage kit", "Master bedroom > Radio briefcase", "Truck"])
            self.assertIn("Master bedroom > Radio briefcase", names)
            self.assertIn("Master bedroom > Power outage kit", names)
            self.assertNotIn("Master bedroom radio briefcase", names)
            self.assertNotIn("Master bedroom power outage kit", names)
            self.assertIn("Truck", names)  # vehicles are left alone
            radio = next(i for i in store.items() if i["name"] == "Radio")
            self.assertEqual(radio["location"], "Master bedroom > Radio briefcase")
            self.assertEqual(len([n for n in names if n == "Master bedroom"]), 1)
            tombs = {r["uid"] for r in store._rows("SELECT uid FROM tombstones WHERE kind='location'")}
            self.assertIn("Master-bedroom-radio-briefcase", tombs)
            self.assertEqual(store._rows("PRAGMA user_version")[0]["user_version"], db.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
