"""OS packages kept in the store for installs with no internet (bunkr/packages.py)."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import nodes, packages  # noqa: E402
from bunkr.db import Store  # noqa: E402
from bunkr.depot import Depot  # noqa: E402

DEPENDS = """tesseract-ocr
  Depends: libtesseract5
  Depends: <libc6>
libtesseract5
  Depends: libc6
libc6
"""
PRIORITY = {"tesseract-ocr": "optional", "libtesseract5": "optional", "libc6": "required"}


class FakeApt:
    """apt-cache, apt-get download, dpkg and dpkg-deb, as far as packages.py uses them."""

    def __init__(self, version="5.3.4-1"):
        self.version, self.asked = version, []

    def __call__(self, cmd, cwd=None, timeout=None):
        self.asked.append(cmd)
        ok = lambda out="": SimpleNamespace(returncode=0, stdout=out, stderr="")
        if cmd[:2] == ["dpkg", "--print-architecture"]:
            return ok("amd64\n")
        if cmd[:2] == ["apt-cache", "depends"]:
            return ok(DEPENDS)
        if cmd[:2] == ["apt-cache", "show"]:
            return ok(f"Package: {cmd[-1]}\nPriority: {PRIORITY.get(cmd[-1], 'optional')}\n")
        if cmd[:2] == ["apt-get", "download"]:
            for p in cmd[2:]:
                (Path(cwd) / f"{p}_{self.version}_amd64.deb").write_bytes(f"deb {p} {self.version}".encode())
            return ok()
        if cmd[:2] == ["dpkg-deb", "-f"]:
            name = Path(cmd[2]).name.split("_")
            return ok(f"Package: {name[0]}\nVersion: {name[1]}\nArchitecture: amd64")
        raise AssertionError(cmd)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.depot = Depot(Store(d / "p.db"), d / "store")
        self.depot.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def test_cache_skips_the_base_system_and_writes_an_index(self):
        apt = FakeApt()
        sysid = packages.system_id(apt)
        self.assertRegex(sysid, r"^[a-z]+-[a-z]+-amd64$")
        r = packages.cache(self.depot, run=apt, log=lambda *_: None)
        self.assertEqual((r["files"], r["added"]), (2, 2))                    # libc6 is on every system
        folder = self.depot.root / "packages" / sysid
        self.assertEqual(sorted(p.name for p in folder.glob("*.deb")), ["libtesseract5_5.3.4-1_amd64.deb", "tesseract-ocr_5.3.4-1_amd64.deb"])
        idx = (folder / "Packages").read_text()
        self.assertIn("Package: tesseract-ocr\nVersion: 5.3.4-1", idx)
        self.assertIn("Filename: ./tesseract-ocr_5.3.4-1_amd64.deb", idx)
        self.assertIn("SHA256: ", idx)
        rows = [x for x in self.depot.rows() if x["kind"] == "package"]
        self.assertEqual(len(rows), 3)                                        # two packages and the index
        self.assertTrue(all(x["path"].startswith(f"packages/{sysid}/") for x in rows))

    def test_a_newer_version_replaces_the_old_one(self):
        packages.cache(self.depot, run=FakeApt("5.3.4-1"), log=lambda *_: None)
        packages.cache(self.depot, run=FakeApt("5.3.5-1"), log=lambda *_: None)
        debs = sorted(p.name for p in self.depot.root.glob("packages/*/*.deb"))
        self.assertEqual(debs, ["libtesseract5_5.3.5-1_amd64.deb", "tesseract-ocr_5.3.5-1_amd64.deb"])
        paths = {x["path"] for x in self.depot.rows() if x["kind"] == "package"}
        self.assertFalse(any("5.3.4" in p for p in paths))                    # the old records are gone too

    def test_nodes_get_them(self):
        packages.cache(self.depot, run=FakeApt(), log=lambda *_: None)
        plan = nodes.plan({"role": "pi", "profile": "{}"}, self.depot.rows(), {}, 0)
        self.assertEqual(sum(1 for r in plan["files"] if r["kind"] == "package"), 3)

    def test_subfolders_stay_inside_the_store(self):
        f = Path(self.tmp.name) / "x.deb"
        f.write_bytes(b"x")
        for bad in ("../etc", ".hidden", "a/b"):
            with self.assertRaises(ValueError):
                self.depot.add_file("package", "x", f, subdir=bad)


if __name__ == "__main__":
    unittest.main()
