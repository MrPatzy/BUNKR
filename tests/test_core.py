import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import backup, builder, gaps, planner  # noqa: E402
from bunkr.db import DEFAULT_SETTINGS, Store  # noqa: E402

GB = 1024 ** 3


def item(**kw):
    base = {"id": 1, "name": "x", "category": "other", "quantity": 1, "unit": "", "per_unit": 0,
            "essential": "", "location": "", "expires": "", "notes": ""}
    return base | kw


class GapTests(unittest.TestCase):
    def setUp(self):
        self.s = dict(DEFAULT_SETTINGS, adults=2, children=1, target_days=14)

    def line(self, result, line_id):
        return next(l for l in result["lines"] if l["id"] == line_id)

    def test_water_days_scale_with_household(self):
        items = [item(category="water", quantity=6, per_unit=5)]  # 30 gallons
        r = gaps.analyze(self.s, items, [])
        self.assertEqual(r["water_days"], 10.0)  # 3 people x 1 gal/day
        self.assertAlmostEqual(self.line(r, "water")["coverage"], 10 / 14)

    def test_pets_add_water_need(self):
        s = dict(self.s, pets=2)  # 3 + 2*0.5 = 4 gal/day
        r = gaps.analyze(s, [item(category="water", quantity=40, per_unit=1)], [])
        self.assertEqual(r["water_days"], 10.0)

    def test_expired_items_not_counted(self):
        items = [item(category="water", quantity=42, per_unit=1, expires="2020-01-01")]
        r = gaps.analyze(self.s, items, [], today=date(2026, 1, 1))
        self.assertEqual(r["water_days"], 0)
        self.assertEqual(len(r["expired"]), 1)

    def test_expiring_soon_still_counts(self):
        items = [item(category="food", quantity=1, per_unit=84000, expires="2026-01-20")]
        r = gaps.analyze(self.s, items, [], today=date(2026, 1, 1))
        self.assertEqual(r["food_days"], 14.0)
        self.assertEqual(len(r["expiring"]), 1)

    def test_per_person_essentials(self):
        r = gaps.analyze(self.s, [item(essential="flashlight", category="power", quantity=2)], [])
        line = self.line(r, "flashlight")
        self.assertEqual(line["need"], 3)
        self.assertAlmostEqual(line["coverage"], 2 / 3)

    def test_not_applicable_removed(self):
        s = dict(self.s, not_applicable=["rx_meds", "doc_property"])
        ids = {l["id"] for l in gaps.analyze(s, [], [])["lines"]}
        self.assertNotIn("rx_meds", ids)
        self.assertNotIn("doc_property", ids)

    def test_pet_lines_only_with_pets(self):
        ids = {l["id"] for l in gaps.analyze(self.s, [], [])["lines"]}
        self.assertNotIn("pet_food", ids)
        self.assertNotIn("doc_pets", ids)

    def test_documents_cover_types(self):
        docs = [{"doc_type": "identity"}]
        r = gaps.analyze(self.s, [], docs)
        self.assertEqual(self.line(r, "doc_identity")["coverage"], 1)
        self.assertEqual(self.line(r, "doc_insurance")["coverage"], 0)

    def test_gaps_sorted_worst_first(self):
        r = gaps.analyze(self.s, [item(category="water", quantity=21, per_unit=1)], [])
        cov = [g["coverage"] for g in r["gaps"]]
        self.assertEqual(cov, sorted(cov))


class PlannerTests(unittest.TestCase):
    def libs(self):
        return [
            {"id": "small", "tier": 1, "size": 1 * GB, "file": "small.zim", "why": "s"},
            {"id": "big", "tier": 3, "group": "wp", "size": 50 * GB, "file": "big.zim", "why": "b"},
            {"id": "mid", "tier": 3, "group": "wp", "size": 10 * GB, "file": "mid.zim", "why": "m"},
            {"id": "tiny", "tier": 3, "group": "wp", "size": 1 * GB, "file": "tiny.zim", "why": "t"},
        ]

    def docs(self):
        return [{"id": 1, "label": "ID", "priority": 1, "size": 2 * GB},
                {"id": 2, "label": "Photos", "priority": 3, "size": 20 * GB}]

    def ids(self, p, kind):
        return [i["id"] for i in p["included"] if i["kind"] == kind]

    def test_picks_best_alternative_that_fits(self):
        p = planner.plan({"free": 32 * GB, "fstype": "exfat"}, self.docs()[:1], [], self.libs())
        self.assertEqual(self.ids(p, "library"), ["small", "mid"])
        skipped = {s["id"] for s in p["skipped"]}
        self.assertIn("big", skipped)       # tried first, reported as not fitting
        self.assertNotIn("tiny", skipped)   # never needed: a better version fit

    def test_documents_before_libraries_and_by_priority(self):
        p = planner.plan({"free": 8 * GB, "fstype": "exfat"}, self.docs(), [], self.libs())
        self.assertEqual(self.ids(p, "document"), [1])
        self.assertFalse(p["documents_complete"])
        self.assertIn("small", self.ids(p, "library"))

    def test_fat32_excludes_big_files(self):
        p = planner.plan({"free": 100 * GB, "fstype": "vfat", "max_file": 4 * GB - 1}, [], [], self.libs())
        self.assertEqual(self.ids(p, "library"), ["small", "tiny"])
        reasons = {s["id"]: s["reason"] for s in p["skipped"]}
        self.assertIn("FAT32", reasons["big"])

    def test_existing_files_count_as_space(self):
        p = planner.plan({"free": 2 * GB, "fstype": "exfat"}, [], [], self.libs(), existing={"mid.zim": 10 * GB})
        self.assertIn("mid", self.ids(p, "library"))

    def test_disabled_libraries_skipped(self):
        libs = self.libs()
        libs[0]["enabled"] = False
        p = planner.plan({"free": 100 * GB, "fstype": "exfat"}, [], [], libs)
        self.assertNotIn("small", self.ids(p, "library"))


class StoreTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / "t.db")
            i = s.save_item({"name": "Water", "category": "water", "quantity": 3, "per_unit": 5})
            s.save_item({"quantity": 4}, i)
            self.assertEqual(s.items()[0]["quantity"], 4)
            s.update_settings({"adults": "3", "bogus": 1, "not_applicable": ["glasses"]})
            self.assertEqual(s.settings()["adults"], 3)
            self.assertEqual(s.settings()["not_applicable"], ["glasses"])


class ArchiveAndBackupTests(unittest.TestCase):
    def test_encrypted_archive_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "src").mkdir()
            (d / "src" / "passport.pdf").write_bytes(b"passport scan")
            (d / "src" / "insurance").mkdir()
            (d / "src" / "insurance" / "home.pdf").write_bytes(b"policy")
            out = d / "out"
            out.mkdir()
            docs = [{"id": 1, "path": str(d / "src" / "passport.pdf"), "label": "Passport", "doc_type": "identity"},
                    {"id": 2, "path": str(d / "src" / "insurance"), "label": "Insurance", "doc_type": "insurance"}]
            names = builder._encrypt_documents(builder.Job(), docs, "correct horse battery", out, split=False)
            archive = out / names[0]
            listing = subprocess.run(["7z", "l", str(archive)], input="correct horse battery\n",
                                     capture_output=True, text=True).stdout
            self.assertIn("Identity/Passport.pdf", listing)
            self.assertIn("Insurance/Insurance/home.pdf", listing)
            wrong = subprocess.run(["7z", "l", "-pwrongpassword", str(archive)], capture_output=True, text=True)
            self.assertNotEqual(wrong.returncode, 0)
            # file names are hidden without the passphrase (header encryption)
            self.assertNotIn(b"Passport", archive.read_bytes())

    def test_backup_snapshots_and_retention(self):
        with tempfile.TemporaryDirectory() as d:
            src, dest = Path(d) / "src", Path(d) / "dest"
            src.mkdir()
            (src / "a.txt").write_text("one")
            job = {"source": str(src), "dest": str(dest), "keep": 2}
            backup.run(job)
            first = backup.snapshots(dest)[0]
            os.utime(src / "a.txt", (0, 0))
            for n in range(2):
                (src / f"b{n}.txt").write_text("x")
                import time
                time.sleep(1.1)
                backup.run(job)
            snaps = backup.snapshots(dest)
            self.assertEqual(len(snaps), 2)
            self.assertFalse(first.exists())
            self.assertTrue((snaps[-1] / "b1.txt").exists())

    def test_backup_refuses_dest_inside_source(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                backup.run({"source": d, "dest": os.path.join(d, "bk"), "keep": 1})


if __name__ == "__main__":
    unittest.main()


class HostGuardTests(unittest.TestCase):
    def test_host_allowed(self):
        from bunkr.server import host_allowed
        allowed = {"127.0.0.1", "localhost", "bunkr.example.org"}
        for ok in ["127.0.0.1:8765", "localhost", "BUNKR.example.org", "bunkr.example.org:443"]:
            self.assertTrue(host_allowed(ok, allowed), ok)
        for bad in ["", None, "evil.example", "bunkr.example.org.evil.example", "example.org"]:
            self.assertFalse(host_allowed(bad, allowed), bad)
