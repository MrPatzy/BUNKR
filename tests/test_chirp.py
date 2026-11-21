"""CHIRP in the store: nightly source from GitHub (faked here) and browser-downloaded installers."""

import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import chirp, family, nodes  # noqa: E402
from bunkr.db import Store  # noqa: E402
from bunkr.depot import Depot  # noqa: E402

SHA = "86e9d31cd34f2caaf2a63e064810e2a9e52b1645"


def source_zip(sha=SHA, good=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        top = f"chirp-{sha}/"
        z.writestr(top + "pyproject.toml", "[project]\nname='chirp'\n")
        if good:
            z.writestr(top + "chirp/__init__.py", "")
        z.writestr(top + "chirp/drivers/uv5r.py", "# driver\n")
    return buf.getvalue()


class FakeGitHub:
    def __init__(self, sha=SHA, zipdata=None):
        self.sha, self.zip, self.calls = sha, zipdata if zipdata is not None else source_zip(sha), []

    def __call__(self, req, timeout=None):
        self.calls.append(req.full_url)
        if req.full_url == chirp.API:
            body = json.dumps({"sha": self.sha, "commit": {"committer": {"date": "2026-11-18T23:28:36Z"}}}).encode()
        else:
            assert req.full_url == chirp.ZIP.format(sha=self.sha)
            body = self.zip
        return io.BytesIO(body)


class ChirpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.store = Store(d / "b.db")
        self.depot = Depot(self.store, d / "store")
        self.depot.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_fetched_once_per_commit(self):
        gh = FakeGitHub()
        row, changed = chirp.fetch_source(self.depot, gh)
        self.assertTrue(changed)
        self.assertEqual(row["version"], "20261118-86e9d31")
        self.assertTrue((self.depot.root / row["path"]).is_file())
        _, changed = chirp.fetch_source(self.depot, gh)
        self.assertFalse(changed)
        self.assertEqual(gh.calls.count(chirp.API), 2)
        # a new commit replaces the old copy
        newer = "1" * 40
        row2, changed = chirp.fetch_source(self.depot, FakeGitHub(newer))
        self.assertTrue(changed)
        self.assertFalse((self.depot.root / row["path"]).exists())
        # it travels to field nodes with the radio pack
        node = {"id": 1, "role": "pi", "profile": "{}", "budget_bytes": 0}
        self.assertIn(row2["path"], [f["path"] for f in nodes.plan(node, self.depot.rows(), {}, 0)["files"]])

    def test_bad_source_is_rejected(self):
        with self.assertRaises(ValueError):
            chirp.fetch_source(self.depot, FakeGitHub(zipdata=source_zip(good=False)))
        self.assertIsNone(self.depot.held("radio", chirp.SRC_ID))
        self.assertEqual(list((self.depot.root / chirp.SUBDIR).glob("*")), [])

    def test_installer_upload_and_90_day_reminder(self):
        data = b"MZ" + b"\0" * 5000
        with self.assertRaises(ValueError):
            chirp.add_installer(self.depot, "setup.exe", io.BytesIO(data), len(data))
        with self.assertRaises(ValueError):
            chirp.add_installer(self.depot, "chirp-next-20261118-installer.exe", io.BytesIO(b"hello"), 5)
        with self.assertRaises(ValueError):  # cut short
            chirp.add_installer(self.depot, "chirp-next-20261118-installer.exe", io.BytesIO(data[:100]), len(data))
        chirp.add_installer(self.depot, "chirp-next-20261118-installer.exe", io.BytesIO(data), len(data))
        st = chirp.status(self.depot, today=date(2026, 12, 1))
        self.assertEqual((st["windows"]["age_days"], st["windows"]["due"]), (13, False))
        self.assertTrue(chirp.status(self.depot, today=date(2027, 2, 17))["windows"]["due"])
        chirp.add_installer(self.depot, "../chirp-next-20270301-installer.exe", io.BytesIO(data), len(data))
        files = sorted(p.name for p in (self.depot.root / chirp.SUBDIR).glob("*"))
        self.assertEqual(files, ["chirp-next-20270301-installer.exe"])  # older one pruned, path stripped

    def test_reminder_task_exists(self):
        family.ensure_tasks(self.store)
        self.assertTrue(self.store._rows("SELECT * FROM tasks WHERE key='chirp_installer' AND every_days=90"))


if __name__ == "__main__":
    unittest.main()
