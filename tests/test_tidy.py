"""1.2 tidy-up: the offline mesh kit in the store, retention of database copies and old store versions, and the
restart-needed notice. No network: GitHub, PyPI and pip are stand-ins."""

import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, dbcheck, homelayout, hostcheck, meshoffline, nodes, retention, server  # noqa: E402
from bunkr.db import Store  # noqa: E402
from bunkr.depot import Depot  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BOARDS_ALL = ["Heltec_v3", "Heltec_t114", "RAK_4631", "Xiao_s3"]


def blob(name):
    return f"firmware {name}".encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fake_releases(companion=("1.18.0", "1.17.0", "1.16.0"), extra=()):
    rels = []
    for kind, versions in (("companion", companion), ("repeater", ("1.18.0", "1.17.0", "1.16.0")),
                           ("room-server", ("1.18.0", "1.17.0"))):
        for v in versions:
            tag = f"{kind}-v{v}"
            assets = []
            for b in BOARDS_ALL:
                for name in (f"{b}_{kind.replace('-', '_')}-v{v}-abc.bin", f"{b}_{kind.replace('-', '_')}-v{v}-abc.uf2"):
                    assets.append({"name": name, "browser_download_url": "https://example.invalid/" + name,
                                   "size": len(blob(name)), "digest": "sha256:" + sha(blob(name))})
            rels.append({"tag_name": tag, "prerelease": False, "draft": False, "published_at": "2026-11-01T00:00:00Z", "assets": assets})
    rels.append({"tag_name": "companion-v9.9.9", "prerelease": True, "draft": False, "assets": []})
    return list(extra) + rels


def make_sdist():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b'[build-system]\nrequires = ["setuptools>=64"]\n'
        info = tarfile.TarInfo("esptool-5.1.0/pyproject.toml")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


SDIST = make_sdist()
PYPI = {"info": {"version": "5.1.0", "requires_dist": ["pyserial>=3.3", "reedsolo>=1.5.3", "pytest; extra == 'dev'"]},
        "urls": [{"packagetype": "sdist", "filename": "esptool-5.1.0.tar.gz", "url": "https://example.invalid/esptool-5.1.0.tar.gz",
                  "size": len(SDIST), "digests": {"sha256": sha(SDIST)}}]}


class MeshKitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.store = Store(d / "bunkr.db")
        self.depot = Depot(self.store, d / "store")
        self.depot.ensure()
        self.rels = fake_releases()
        self.downloads, self.cmds = [], []
        self.bad = set()
        self.patches = [mock.patch.object(meshoffline, "_get_json", self.get_json),
                        mock.patch.object(meshoffline, "_download", self.download),
                        mock.patch.object(meshoffline, "_run", self.run_pip)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def get_json(self, url):
        if "pypi.org" in url:
            return PYPI
        self.assertIn("meshcore-dev/MeshCore/releases", url)
        return self.rels

    def download(self, url, dest, size, digest, cancelled=lambda: False):
        name = url.rsplit("/", 1)[1]
        self.downloads.append(name)
        data = SDIST if name.endswith(".tar.gz") else blob(name)
        dest.write_bytes(b"tampered" if name in self.bad else data)

    def run_pip(self, cmd):
        self.cmds.append(cmd)
        out = Path(cmd[cmd.index("-d") + 1])
        py = cmd[cmd.index("--python-version") + 1].replace(".", "")
        arch = "aarch64" if any("aarch64" in c for c in cmd) else "x86_64"
        for n in ("meshcore_cli-1.2.0-py3-none-any.whl", "pip-25.0-py3-none-any.whl",
                  f"pycryptodome-3.21-cp{py}-manylinux_2_17_{arch}.whl"):
            (out / n).write_bytes(n.encode())

    def live(self):
        return self.depot.root / meshoffline.REL

    def test_kit_lands_in_the_store_for_the_chosen_boards(self):
        st = meshoffline.refresh(self.depot, self.store, pip=["pip"])
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["version"], "1.18.0")
        self.assertEqual(st["releases"], ["companion-v1.17.0", "companion-v1.18.0", "repeater-v1.17.0", "repeater-v1.18.0",
                                          "room-server-v1.17.0", "room-server-v1.18.0"])
        fw = sorted(p.name for p in (self.live() / "firmware").rglob("*") if p.is_file())
        self.assertEqual(len(fw), 6 * 3 * 2)   # 6 releases x 3 boards x 2 files
        self.assertFalse([n for n in fw if n.startswith("Xiao")])
        self.assertTrue((self.live() / "README.txt").read_text().startswith("Offline mesh tools"))
        self.assertIn("Heltec V3", (self.live() / "README.txt").read_text())
        status = json.loads((self.live() / "status.json").read_text())
        self.assertTrue(status["ok"])
        self.assertEqual(status["last_good"], status["checked_at"])
        wheels = sorted(p.name for p in (self.live() / "wheels").iterdir())
        self.assertIn("esptool-5.1.0.tar.gz", wheels)
        self.assertIn("meshcore_cli-1.2.0-py3-none-any.whl", wheels)
        # pip only ever downloads binaries, for 2 platforms x 3 Pythons, with esptool's needs (not its dev extras)
        self.assertEqual(len(self.cmds), 6)
        for c in self.cmds:
            self.assertEqual(c[1], "download")
            self.assertIn("--only-binary=:all:", c)
            self.assertIn("pyserial>=3.3", c)
            self.assertIn("setuptools>=64", c)
            self.assertNotIn("pytest; extra == 'dev'", c)
        # recorded in the store, so nodes sync it and the Downloads page shows it
        rows = [r for r in self.depot.rows() if r["item_id"] == meshoffline.ITEM]
        self.assertEqual(len(rows), sum(1 for p in self.live().rglob("*") if p.is_file()))
        self.assertTrue(all(r["kind"] == "firmware" and r["version"] == "1.18.0" for r in rows))
        self.assertEqual(self.depot.verify(), [])
        self.assertFalse(list(self.depot.root.glob("firmware/.mesh-offline-*")))
        # a second run reuses verified files instead of downloading them again
        self.downloads.clear()
        self.assertTrue(meshoffline.refresh(self.depot, self.store, pip=["pip"])["ok"])
        self.assertEqual(self.downloads, [])

    def test_failed_run_keeps_the_last_good_copy(self):
        self.assertTrue(meshoffline.refresh(self.depot, self.store, pip=None)["ok"])
        before = sorted(p.name for p in self.live().rglob("*.bin"))
        self.rels = fake_releases(companion=("1.19.0", "1.18.0"))
        self.bad = {"Heltec_v3_companion-v1.19.0-abc.bin"}
        st = meshoffline.refresh(self.depot, self.store, pip=None)
        self.assertFalse(st["ok"])
        self.assertIn("checksum mismatch", st["error"])
        self.assertEqual(sorted(p.name for p in self.live().rglob("*.bin")), before)
        self.assertEqual(self.depot.held("firmware", meshoffline.ITEM)["version"], "1.18.0")
        self.assertEqual(json.loads((self.live() / "status.json").read_text())["ok"], False)
        self.assertEqual(self.depot.verify(), [])  # status.json's record follows the rewrite
        self.assertFalse(list(self.depot.root.glob("firmware/.mesh-offline-*")))

    def test_no_pip_means_no_wheels_and_a_note(self):
        st = meshoffline.refresh(self.depot, self.store, pip=None)
        self.assertTrue(st["ok"])
        self.assertEqual(st["wheels"], 0)
        self.assertIn("pip isn't installed", st["wheels_note"])
        self.assertIn("pip isn't installed", (self.live() / "README.txt").read_text())
        self.assertEqual(self.cmds, [])

    def test_major_version_waits_unless_approved(self):
        meshoffline.refresh(self.depot, self.store, pip=None)
        self.rels = fake_releases(companion=("2.0.0", "1.18.0", "1.17.0"))
        st = meshoffline.refresh(self.depot, self.store, pip=None)   # the daily run stays on 1.x
        self.assertEqual(st["version"], "1.18.0")
        self.assertFalse(list(self.live().glob("firmware/companion-v2.0.0")))
        self.assertTrue(server.is_major("1.18.0", meshoffline.resolve(self.store, cache={})["latest"]))
        st = meshoffline.refresh(self.depot, self.store, allow_major=True, pip=None)   # approved on Downloads
        self.assertEqual(st["version"], "2.0.0")
        self.assertEqual(sorted(p.name for p in self.live().glob("firmware/companion-*")), ["companion-v1.18.0", "companion-v2.0.0"])

    def test_boards_setting(self):
        self.assertEqual(meshoffline.settings(self.store)["boards"], ["heltec_v3", "heltec_t114", "rak4631"])
        with self.assertRaises(ValueError):
            meshoffline.save_settings(self.store, {"boards": ["nope"]})
        meshoffline.save_settings(self.store, {"boards": ["rak4631", "nope"]})
        self.assertEqual(meshoffline.settings(self.store)["boards"], ["rak4631"])
        meshoffline.refresh(self.depot, self.store, pip=None)
        names = {p.name for p in self.live().rglob("*.bin")}
        self.assertTrue(names and all(n.startswith("RAK_4631") for n in names))

    def test_missing_digest_is_refused(self):
        for a in self.rels[0]["assets"]:
            a["digest"] = ""
        st = meshoffline.refresh(self.depot, self.store, pip=None)
        self.assertFalse(st["ok"])
        self.assertIn("SHA-256", st["error"])
        self.assertFalse(self.live().exists())

    def test_build_requires_read_without_unpacking(self):
        p = Path(self.tmp.name) / "e.tar.gz"
        p.write_bytes(SDIST)
        self.assertEqual(meshoffline._build_requires(p), ["setuptools>=64"])
        p.write_bytes(b"not a tar")
        self.assertEqual(meshoffline._build_requires(p), ["setuptools>=64", "wheel"])


class BackupRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bunkr.db")
        self.d = dbcheck.folder(self.store)
        self.d.mkdir()
        self.t = time.time() - 400 * 86400

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, age_days=None):
        p = self.d / name
        p.write_bytes(b"x" * 10)
        self.t += 60
        stamp = self.t if age_days is None else time.time() - age_days * 86400
        os.utime(p, (stamp, stamp))
        return p

    def test_dry_run_list(self):
        # nightly copies every day from 2025-12-01 to 2026-11-20, 15 deploy copies, two made by hand, one unfinished
        day = datetime(2025, 12, 1)
        names = []
        while day <= datetime(2026, 11, 20):
            names.append(self.put(f"bunkr-nightly-{day:%Y%m%d}.db").name)
            day = datetime.fromordinal(day.toordinal() + 1)
        deploys = [self.put(f"bunkr-before-deploy-abc{n:04x}-2026{11:02d}{n + 1:02d}-0930.db").name for n in range(15)]
        self.put("bunkr-pre-phase7.db")
        self.put("bunkr-before-schema10.db")
        self.put("bunkr-nightly-20261001.part", age_days=5)
        self.put("bunkr-nightly-20261121.part", age_days=0)
        self.store.set_raw("restore_test", {"backup": "bunkr-nightly-20260215.db", "ok": True})
        plan = retention.plan_backups(self.store, self.d, now=datetime(2026, 11, 21))
        act = {r["name"]: r["action"] for r in plan}
        keep_nightly = sorted(n for n in names if act[n] == "keep")
        self.assertEqual(keep_nightly[-14:], names[-14:])
        # first of each month for 12 months (Dec 2025 .. Nov 2026), plus the one the restore test uses
        months = sorted(n for n in keep_nightly if n not in names[-14:])
        self.assertEqual(months, sorted([f"bunkr-nightly-{y}{m:02d}01.db" for y, m in
                                         [(2025, 12)] + [(2026, m) for m in range(1, 12)]] + ["bunkr-nightly-20260215.db"]))
        self.assertEqual([n for n in deploys if act[n] == "keep"], deploys[5:])
        self.assertEqual(act["bunkr-pre-phase7.db"], "keep")
        self.assertEqual(act["bunkr-before-schema10.db"], "keep")
        self.assertEqual(act["bunkr-nightly-20261001.part"], "delete")
        self.assertEqual(act["bunkr-nightly-20261121.part"], "keep")
        why = {r["name"]: r["why"] for r in plan}
        self.assertEqual(why["bunkr-nightly-20260215.db"], "the restore test uses it")
        # dry run: nothing deleted yet
        self.assertEqual(len(list(self.d.iterdir())), len(plan))
        retention.prune_backups(self.store, self.d, now=datetime(2026, 11, 21))
        left = {p.name for p in self.d.iterdir()}
        self.assertEqual(left, {n for n, a in act.items() if a == "keep"})

    def test_newest_copy_always_kept_and_settings(self):
        retention.save_settings(self.store, {"deploy_keep": 1})
        a = self.put("bunkr-before-deploy-aaaaaaa-20261120-0100.db")
        b = self.put("bunkr-before-deploy-bbbbbbb-20261121-0100.db")
        # the older-named one was written last (e.g. a restore): newest by time is never removed
        os.utime(a, (time.time(), time.time()))
        act = {r["name"]: r["action"] for r in retention.plan_backups(self.store, self.d)}
        self.assertEqual((act[a.name], act[b.name]), ("keep", "keep"))
        with self.assertRaises(ValueError):
            retention.save_settings(self.store, {"nightly_keep": 0})
        with self.assertRaises(ValueError):
            retention.save_settings(self.store, {"deploy_keep": "ten"})
        self.store.set_raw("retention", {"deploy_keep": 9999, "monthly_keep": "x"})
        self.assertEqual(retention.settings(self.store), {"deploy_keep": 200, "nightly_keep": 14, "monthly_keep": 12, "clean_store": True})

    def test_nightly_job_prunes(self):
        for n in range(1, 18):
            self.put(f"bunkr-nightly-202610{n:02d}.db")
        self.put("bunkr-before-deploy-x.db")      # not the deploy pattern: never touched
        dbcheck.nightly(self.store)
        left = sorted(p.name for p in self.d.glob("bunkr-nightly-*.db"))
        self.assertIn("bunkr-nightly-20261001.db", left)       # the month's first copy
        self.assertNotIn("bunkr-nightly-20261002.db", left)
        self.assertTrue((self.d / "bunkr-before-deploy-x.db").exists())

    def test_autodeploy_prune_follows_the_setting(self):
        script = (ROOT / "deploy" / "autodeploy.sh").read_text()
        code = re.search(r"<<'PY'\n(.*?)\nPY\n", script, re.S).group(1)
        retention.save_settings(self.store, {"deploy_keep": 3})
        made = [self.put(f"bunkr-before-deploy-{n:07x}-202611{n + 1:02d}-0100.db").name for n in range(6)]
        self.store.set_raw("restore_test", {"backup": made[0]})
        self.put("bunkr-pre-phase7.db")
        self.put("bunkr-nightly-20261101.db")
        subprocess.run([sys.executable, "-I", "-", str(self.d), self.store.path, "10", str(self.d / made[-1])],
                       input=code, text=True, check=True)
        left = {p.name for p in self.d.iterdir()}
        self.assertEqual(left, {made[0], *made[3:], "bunkr-pre-phase7.db", "bunkr-nightly-20261101.db"})
        # no database to read: falls back to the number given
        subprocess.run([sys.executable, "-I", "-", str(self.d), str(self.d / "none.db"), "1", str(self.d / made[-1])],
                       input=code, text=True, check=True)
        self.assertEqual({p.name for p in self.d.glob("bunkr-before-deploy-*")}, {made[-1]})


class StoreRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.store = Store(d / "bunkr.db")
        self.depot = Depot(self.store, d / "store")
        self.depot.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, kind, item, rel, version, data=b"zim"):
        p = self.depot.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.depot.record(kind, item, rel, version, len(data), sha(data))
        return self.depot.by_path(rel)

    def test_dry_run_and_clean(self):
        self.add("library", "wiki", "library/wiki_2026-01.zim", "2026-01")
        self.add("library", "wiki", "library/wiki_2026-06.zim", "2026-06")
        kept = self.add("library", "med", "library/med_2025-01.zim", "2025-01")
        self.add("library", "med", "library/med_2026-01.zim", "2026-01")
        retention.set_pin(self.store, "library", "med", "2025-01", True)
        self.add("library", "bad", "library/bad_2025.zim", "2025")
        new = self.add("library", "bad", "library/bad_2026.zim", "2026")
        self.store._exec("UPDATE depot_files SET status='corrupt' WHERE id=?", (new["id"],))
        self.add("firmware", "meshcore-repeater-rak4631", "firmware/meshcore-repeater-rak4631/1.9.0/a.uf2", "1.9.0")
        self.add("firmware", "meshcore-repeater-rak4631", "firmware/meshcore-repeater-rak4631/1.18.0/a.uf2", "1.18.0")
        self.add("documents", "archive", "documents/documents-2026-01-01.7z", "2026-01-01")
        self.add("documents", "archive", "documents/documents-2026-02-01.7z", "2026-02-01")
        stray = self.depot.root / "library" / "hand-copied.zim"
        stray.write_bytes(b"mine")
        part = self.depot.root / "library" / "big.zim.part"
        part.write_bytes(b"half")
        old = time.time() - 10 * 86400
        os.utime(part, (old, old))
        fresh = self.depot.root / "library" / "now.zim.part"
        fresh.write_bytes(b"half")
        plan = retention.plan_store(self.depot)
        self.assertEqual(sorted(d["path"] for d in plan["delete"]),
                         ["firmware/meshcore-repeater-rak4631/1.9.0/a.uf2", "library/big.zim.part", "library/wiki_2026-01.zim"])
        self.assertEqual([u["path"] for u in plan["unknown"]], ["library/hand-copied.zim"])
        self.assertEqual(retention.plan_store(self.depot, job_running=True)["delete"][-1]["path"], "library/wiki_2026-01.zim")
        self.assertTrue((self.depot.root / "library/wiki_2026-01.zim").exists())  # dry run
        retention.prune_store(self.depot)
        self.assertFalse((self.depot.root / "library/wiki_2026-01.zim").exists())
        self.assertFalse(part.exists())
        self.assertTrue(fresh.exists() and stray.exists() and self.depot.path(kept).exists())
        self.assertTrue((self.depot.root / "library/bad_2025.zim").exists())   # the new one isn't verified
        self.assertEqual(len([r for r in self.depot.rows() if r["kind"] == "documents"]), 2)
        self.assertEqual(retention.plan_store(self.depot)["delete"], [])

    def test_pins_survive_normal_pruning(self):
        self.add("library", "med", "library/med_2025-01.zim", "2025-01")
        retention.set_pin(self.store, "library", "med", "2025-01", True)
        self.add("library", "med", "library/med_2026-01.zim", "2026-01")
        self.depot.prune_older("library", "med", "library/med_2026-01.zim")
        self.assertEqual(len(self.depot.rows()), 2)
        node = {"role": "pi", "profile": json.dumps({"tiers": [1, 2, 3]})}
        planned = nodes.plan(node, self.depot.rows(), {"med": {"tier": 1}}, 0)["files"]
        self.assertEqual([r["version"] for r in planned], ["2026-01"])   # nodes get the newest, not the kept one
        retention.set_pin(self.store, "library", "med", "2025-01", False)
        self.depot.prune_older("library", "med", "library/med_2026-01.zim")
        self.assertEqual([r["version"] for r in self.depot.rows()], ["2026-01"])

    def test_space_summary(self):
        self.add("library", "wiki", "library/wiki.zim", "1", b"x" * 1000)
        dbcheck.nightly_copy(self.store)
        s = retention.space(self.store, self.depot)
        self.assertEqual(s["store"]["library"], 1000)
        self.assertEqual(s["backups"]["nightly"]["files"], 1)
        self.assertGreater(s["database"], 0)
        self.assertGreater(s["total"], 0)


class HostCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.store = Store(self.d / "bunkr.db")
        self.sent = []
        hostcheck.listeners[:] = [lambda ev, data: self.sent.append((ev, data))]

    def tearDown(self):
        hostcheck.listeners[:] = []
        self.tmp.cleanup()

    def test_update_notifier_formats(self):
        self.assertEqual(hostcheck.pending_updates("\n12 updates can be applied immediately.\n5 of these updates are standard security updates.\n"), (12, 5))
        self.assertEqual(hostcheck.pending_updates("1 update can be applied immediately.\n1 of these updates is a standard security update.\n"), (1, 1))
        self.assertEqual(hostcheck.pending_updates("4 packages can be updated.\n2 updates are security updates.\n"), (4, 2))
        self.assertEqual(hostcheck.pending_updates("3 updates can be applied immediately.\n"), (3, 0))
        self.assertEqual(hostcheck.pending_updates(None), (None, None))

    def test_restart_notice_once_per_boot(self):
        run = self.d / "run"
        run.mkdir()
        st = hostcheck.check(self.store, run, self.d / "missing", boot="b1")
        self.assertEqual((st["reboot"], st["attention"], st["security"]), (False, False, None))
        self.assertEqual(self.sent, [])
        (run / "reboot-required").write_text("*** System restart required ***\n")
        (run / "reboot-required.pkgs").write_text("linux-image-6.8.0-50-generic\nlibc6\nlibc6\n$(evil)\n")
        (self.d / "ua").write_text("2 updates can be applied immediately.\n2 of these updates are standard security updates.\n")
        st = hostcheck.check(self.store, run, self.d / "ua", boot="b1")
        self.assertEqual(st["packages"], ["linux-image-6.8.0-50-generic", "libc6"])
        self.assertEqual(st["security"], 2)
        self.assertTrue(st["attention"])
        hostcheck.check(self.store, run, self.d / "ua", boot="b1")    # the next hour, the next day: no repeat
        hostcheck.check(self.store, run, self.d / "ua", boot="b1")
        self.assertEqual(len(self.sent), 1)
        ev, data = self.sent[0]
        self.assertEqual((ev, data["user_id"], data["tag"]), ("reminder", None, "reboot-needed"))
        self.assertIn("linux-image", data["body"])
        self.assertIn("won't do it", data["body"])
        self.assertTrue(self.store.get_raw("host_status")["reboot"])
        hostcheck.check(self.store, run, self.d / "ua", boot="b2")    # restarted, and another fix needs one
        self.assertEqual(len(self.sent), 2)

    def test_tile_is_admin_only(self):
        self.assertIn(("hub", "Hub computer", "admin"), homelayout.TILES)
        html = (ROOT / "bunkr" / "static" / "index.html").read_text()
        self.assertIn('data-tile="hub"', html)


class TidyHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        auth.create_user(self.hub.store, "dad", p3.PW, "admin")
        auth.create_user(self.hub.store, "kid", p3.PW, "family")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.cookie = {u: self.login(u) for u in ("dad", "kid")}
        self.patch = mock.patch.object(meshoffline, "_get_json", side_effect=OSError("offline"))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.hub.close()

    def login(self, u):
        r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": u, "password": p3.PW}).encode(),
                                   method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
        with self.opener.open(r) as resp:
            return resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def call(self, method, path, body=None, who="dad"):
        r = urllib.request.Request(self.hub.url + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.cookie[who]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_space_settings_and_admin_only(self):
        code, r = self.call("GET", "/api/depot/space")
        self.assertEqual(code, 200, r)
        self.assertEqual(r["settings"]["deploy_keep"], 10)
        self.assertIn("store", r["space"])
        code, r = self.call("POST", "/api/depot/retention", {"nightly_keep": 30})
        self.assertEqual((code, r["settings"]["nightly_keep"]), (200, 30))
        self.assertEqual(self.call("POST", "/api/depot/retention", {"nightly_keep": 0})[0], 400)
        self.assertEqual(self.call("GET", "/api/depot/space", who="kid")[0], 403)
        self.assertEqual(self.call("POST", "/api/depot/mesh-offline", {"boards": []})[0], 400)
        code, r = self.call("POST", "/api/depot/mesh-offline", {"boards": ["heltec_v3"]})
        self.assertEqual((code, r["boards"]), (200, ["heltec_v3"]))
        code, r = self.call("POST", "/api/depot/clean")
        self.assertEqual((code, r["removed"]), (200, 0))

    def test_host_status_on_home_for_admins(self):
        self.hub.store.set_raw("host_status", {"reboot": True, "attention": True, "packages": ["libc6"]})
        _, me = self.call("GET", "/api/state")
        self.assertTrue(me["host_status"]["reboot"])
        _, kid = self.call("GET", "/api/state", who="kid")
        self.assertIsNone(kid["host_status"])


if __name__ == "__main__":
    unittest.main()
