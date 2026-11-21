"""Phase 2: manifests, downloads store, node planning, drive reports (offline)."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import builder, manifest, nodes, planner, server  # noqa: E402
from bunkr.db import DEFAULT_SETTINGS, Store  # noqa: E402
from bunkr.depot import Depot  # noqa: E402

META = {"water": {"tier": 1, "group": None, "rank": 0}, "wiki_full": {"tier": 3, "group": "wikipedia", "rank": 13},
        "wiki_mini": {"tier": 3, "group": "wikipedia", "rank": 15}, "ifixit": {"tier": 2, "group": None, "rank": 10}}


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "t.db")
        self.depot = Depot(self.store, self.dir / "store")
        self.depot.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, kind, item, name, data, version="2026-01"):
        src = self.dir / name
        src.write_bytes(data)
        return self.depot.add_file(kind, item, src, version=version)


class ManifestTests(Base):
    def test_scan_reuses_and_verifies(self):
        root = self.dir / "kit"
        (root / "library").mkdir(parents=True)
        (root / "library" / "a.zim").write_bytes(b"aaa")
        (root / "library" / "b.zim.part").write_bytes(b"partial")
        files = manifest.scan(root, ["library"])
        self.assertEqual([f["path"] for f in files], ["library/a.zim"])
        self.assertEqual(files[0]["sha256"], sha(b"aaa"))
        files[0]["item"] = "water"
        data = manifest.write(root, {"uid": "u1", "name": "Stick"}, files)
        self.assertEqual(manifest.read(root)["node"]["uid"], "u1")
        again = manifest.scan(root, ["library"], previous=data, known={})
        self.assertEqual(again[0]["item"], "water")  # carried over
        self.assertEqual(manifest.verify(root, data), {"damaged": [], "missing": [], "checked": 1})
        (root / "library" / "a.zim").write_bytes(b"aab")
        self.assertEqual(manifest.verify(root, data)["damaged"], ["library/a.zim"])

    def test_compare(self):
        want = [{"path": "a", "sha256": "1"}, {"path": "b", "sha256": "2"}, {"path": "c", "sha256": "3"}]
        have = [{"path": "a", "sha256": "1"}, {"path": "b", "sha256": "X"}, {"path": "z", "sha256": "9"}]
        self.assertEqual(manifest.compare(want, have),
                         {"missing": ["c"], "changed": ["b"], "extra": ["z"], "ok": ["a"]})


class DepotTests(Base):
    def test_record_prune_verify(self):
        old = self.put("library", "water", "zimgit-water_en_2025-01.zim", b"old", "2025-01")
        new = self.put("library", "water", "zimgit-water_en_2026-01.zim", b"new", "2026-01")
        self.assertEqual(self.depot.held("library", "water")["id"], new["id"])
        self.depot.prune_older("library", "water", new["path"])
        self.assertFalse((self.depot.root / old["path"]).exists())
        self.assertEqual(len(self.depot.rows()), 1)
        (self.depot.root / new["path"]).write_bytes(b"bitrot")
        self.assertEqual(self.depot.verify(), [{"path": new["path"], "status": "corrupt"}])
        self.assertIsNone(self.depot.held("library", "water"))

    def test_catalog_flags_updates(self):
        self.put("library", "water", "w.zim", b"x", "2025-06-01")
        libs = [{"id": "water", "why": "Water", "tier": 1, "size": 5, "date": "2026-02-01", "online": True}]
        cat = self.depot.catalog(libs, [])
        self.assertTrue(cat[0]["update"])


class PlanTests(Base):
    def test_priority_and_budget(self):
        self.put("documents", "archive", "documents-2026-01-01_1200.7z", b"d" * 10, "2026-01-01_1200")
        self.put("library", "water", "water.zim", b"w" * 20)
        self.put("library", "wiki_full", "wiki_full.zim", b"F" * 500)
        self.put("library", "wiki_mini", "wiki_mini.zim", b"m" * 50)
        self.put("map", "region", "region.pmtiles", b"p" * 30)
        node = {"role": "pi", "profile": "{}", "budget_bytes": 120}
        p = nodes.plan(node, self.depot.rows(), META, 120)
        got = [r["item_id"] for r in p["files"]]
        self.assertEqual(got, ["archive", "water", "region", "wiki_mini"])  # full Wikipedia doesn't fit
        self.assertEqual(p["used"], 110)
        node["profile"] = json.dumps({"maps": False, "tiers": [1]})
        got = [r["item_id"] for r in nodes.plan(node, self.depot.rows(), META, 0)["files"]]
        self.assertEqual(got, ["archive", "water"])

    def test_status_behind_and_archive(self):
        node = {"id": 1, "role": "laptop", "manifest": json.dumps({"files": [
            {"path": "library/water.zim", "sha256": "1", "size": 5},
            {"path": "documents/documents-2026-01-01_1200.7z", "sha256": "d", "size": 9, "kind": "documents"}]}),
            "last_sync": "2026-01-02T00:00:00+00:00", "reminders": "{}", "created_at": "2025-01-01T00:00:00+00:00"}
        planned = [{"path": "library/water.zim", "sha256": "2", "size": 5},
                   {"path": "library/med.zim", "sha256": "3", "size": 7}]
        st = nodes.status(node, planned, "2026-03-01T00:00:00+00:00", 1)
        self.assertEqual((st["behind"], st["behind_bytes"]), (2, 12))
        self.assertTrue(st["archive"]["stale"])
        self.assertTrue(st["reminders"][0]["overdue"])
        self.assertEqual(st["majors_pending"], 1)


class DriveReportTests(Base):
    def test_out_of_date_files(self):
        self.put("library", "water", "water-new.zim", b"n", "2026-05")
        man = {"node": {"uid": "u", "name": "Stick"}, "files": [
            {"path": "library/water-old.zim", "kind": "library", "item": "water", "version": "2025-01", "sha256": "o"},
            {"path": "library/med.zim", "kind": "library", "item": "medical_lib", "version": "2026-05", "sha256": "m"},
            {"path": "documents/documents-2026-01-01_1200.7z", "kind": "documents", "item": "archive"},
            {"path": "inventory/inventory.csv", "sha256": "c"}]}
        cat = [{"kind": "library", "id": "water", "latest": "2026-05"},
               {"kind": "library", "id": "medical_lib", "latest": "2026-05"}]
        r = nodes.drive_report(man, cat, self.depot.rows(), "2026-02-01T00:00:00+00:00")
        paths = {f["path"]: f for f in r["out_of_date"]}
        self.assertEqual(set(paths), {"library/water-old.zim", "documents/documents-2026-01-01_1200.7z"})
        self.assertTrue(paths["library/water-old.zim"]["in_store"])
        self.assertEqual(r["current"], 1)


class BuildFromDepotTests(Base):
    def test_kit_copies_from_store_and_writes_manifest(self):
        row = self.put("library", "water", "zimgit-water_en_2026-01.zim", b"W" * 1000, "2026-01")
        mount = self.dir / "usb"
        mount.mkdir()
        lib = {"id": "water", "why": "Water", "tier": 1, "size": 1000, "file": "zimgit-water_en_2026-01.zim",
               "url": "http://invalid.example/never-used", "date": "2026-01", "enabled": True}
        drive = {"free": 10 ** 9, "fstype": "exfat", "max_file": None}
        plan = planner.plan(drive, [], [], [lib])
        seen = []
        job = builder.Job()
        job.start(builder.build, str(mount), plan, [], [], [lib], dict(DEFAULT_SETTINGS), [], "", self.depot,
                  seen.append, "TESTSTICK")
        job.thread.join(20)
        snap = job.snapshot()
        self.assertEqual(snap["state"], "done", snap["log"])
        self.assertTrue(any("copied from the downloads store" in l for l in snap["log"]))
        data = manifest.read(mount / "BUNKR")
        entry = next(f for f in data["files"] if f["path"].startswith("library/"))
        self.assertEqual((entry["sha256"], entry["item"], entry["version"]), (row["sha256"], "water", "2026-01"))
        self.assertEqual(data["node"]["name"], "TESTSTICK")
        self.assertEqual(seen[0]["node"]["uid"], data["node"]["uid"])
        # rebuild keeps the same identity
        job.start(builder.build, str(mount), plan, [], [], [lib], dict(DEFAULT_SETTINGS), [], "", self.depot, None, "")
        job.thread.join(20)
        self.assertEqual(manifest.read(mount / "BUNKR")["node"]["uid"], data["node"]["uid"])


class MajorTests(unittest.TestCase):
    def test_semver_major(self):
        self.assertTrue(server.is_major("1.18.0", "2.0.0"))
        self.assertFalse(server.is_major("v2.7.26", "v2.8.0"))
        self.assertFalse(server.is_major("2025-01", "2026-01"))


if __name__ == "__main__":
    unittest.main()
