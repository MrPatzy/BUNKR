"""The .deb and the Upgrade button: versions, the request file, signatures, rollback, install kinds, building the .deb."""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from bunkr import __version__, auth, selfupdate, upgrader  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

SHA = "a" * 64


def keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    return k, k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sign(k, data):
    return base64.b64encode(k.sign(data)) + b"\n"


def bump(v, i):
    p = [int(x) for x in v.split(".")]
    p[i] += 1
    return ".".join(map(str, p[:i + 1] + [0] * (2 - i)))


def release_dir(folder, ver, k, deb=b"pretend deb", extra=""):
    """A release as the build and signing tools make it: the .deb, SHA256SUMS and its signature."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / upgrader.deb_name(ver)).write_bytes(deb)
    sums = f"{hashlib.sha256(deb).hexdigest()}  {upgrader.deb_name(ver)}\n{extra}".encode()
    (folder / "SHA256SUMS").write_bytes(sums)
    (folder / "SHA256SUMS.sig").write_bytes(sign(k, sums))
    return hashlib.sha256(deb).hexdigest()


class VersionTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(upgrader.version("1.2.10"), (1, 2, 10))
        for bad in ("v1.2.0", "1.2", "1.2.0-beta.1", "01.2.0", "1.2.0\n", " 1.2.0", 120, None, "1.2.0/../x"):
            self.assertIsNone(upgrader.version(bad), bad)
        self.assertTrue(upgrader.is_major("2.0.0", "1.9.9"))
        self.assertFalse(upgrader.is_major("1.10.0", "1.9.9"))
        self.assertEqual(upgrader.deb_name("1.2.0"), "bunkr_1.2.0_all.deb")

    def test_health_target(self):
        self.assertEqual(upgrader.health_target("--role hub --bind 0.0.0.0 --port 9000"), ("127.0.0.1", 9000))
        self.assertEqual(upgrader.health_target("--bind 10.0.0.5"), ("10.0.0.5", 8765))
        self.assertEqual(upgrader.health_target(""), ("127.0.0.1", 8765))


class RequestTests(unittest.TestCase):
    def good(self, **kw):
        d = {"version": "1.3.0", "sha256": SHA, "source": "github", "repo": "someone/bunkr", "confirm_major": False,
             "requested_by": "dad", "requested_at": "2026-11-22T10:00:00+00:00", **kw}
        return json.dumps(d).encode()

    def check(self, raw, current="1.2.0"):
        return upgrader.validate_request(raw, current, "someone/bunkr")

    def test_good(self):
        self.assertEqual(self.check(self.good())["version"], "1.3.0")
        self.assertEqual(self.check(self.good(source="store", repo=""))["source"], "store")
        self.assertEqual(self.check(self.good(version="2.0.0", confirm_major=True))["version"], "2.0.0")

    def test_every_field_checked(self):
        bad = [
            b"not json", b"[]", b"\xff\xfe", b" " * 5000,
            self.good()[:-1] + b', "extra": 1}',
            json.dumps({"version": "1.3.0"}).encode(),
            self.good(version="1.2.0"), self.good(version="1.1.9"), self.good(version="v1.3.0"),
            self.good(version="1.3.0-rc.1"), self.good(version=["1.3.0"]),
            self.good(sha256="A" * 64), self.good(sha256="a" * 63), self.good(sha256=None),
            self.good(source="http"), self.good(source="file:///etc"),
            self.good(repo="evil/bunkr"), self.good(repo="../../x"), self.good(repo=1),
            self.good(confirm_major="yes"), self.good(version="2.0.0"),
            self.good(requested_by=""), self.good(requested_by="a b"), self.good(requested_by="x\n"), self.good(requested_by=3),
            self.good(requested_at="yesterday"), self.good(requested_at=None),
        ]
        for raw in bad:
            with self.assertRaises(ValueError, msg=raw[:80]):
                self.check(raw)
        with self.assertRaises(ValueError):
            upgrader.validate_request(self.good(), "1.2.0", "")      # no configured repository: nothing from GitHub

    def test_make_and_write(self):
        with tempfile.TemporaryDirectory() as t:
            raw = upgrader.make_request("1.3.0", SHA, "github", "dad", current="1.2.0", repo="someone/bunkr")
            self.assertEqual(self.check(raw)["requested_by"], "dad")
            with self.assertRaises(ValueError):
                upgrader.make_request("1.3.0", SHA, "github", "dad", current="1.3.0", repo="someone/bunkr")
            with self.assertRaises(ValueError):
                upgrader.write_request(raw, Path(t) / "missing")
            upgrader.write_request(raw, t)
            self.assertEqual((Path(t) / "request.json").read_bytes(), raw)
            self.assertEqual(os.stat(Path(t) / "request.json").st_mode & 0o777, 0o640)
            with self.assertRaises(ValueError):
                upgrader.write_request(raw, t)                          # one at a time

    def test_take_request(self):
        with tempfile.TemporaryDirectory() as t:
            req = Path(t) / "request.json"
            req.write_bytes(b"{}")
            self.assertEqual(upgrader.take_request(req), b"{}")
            self.assertFalse(req.exists())                                # always removed
            secret = Path(t) / "secret"
            secret.write_text("root only")
            req.symlink_to(secret)
            self.assertIsNone(upgrader.take_request(req))                 # links aren't followed
            self.assertFalse(req.is_symlink())
            self.assertTrue(secret.exists())
            req.write_bytes(b"x" * 10000)
            self.assertEqual(len(upgrader.take_request(req)), upgrader.MAX_REQUEST + 1)   # read no further than needed
            self.assertIsNone(upgrader.take_request(req))                 # nothing there


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.k, self.pub = keypair()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_verify(self):
        data = b"abc  bunkr_1.3.0_all.deb\n"
        sig = sign(self.k, data)
        self.assertTrue(upgrader.verify_signature(data, sig, self.pub))
        self.assertTrue(upgrader.verify_signature(data, sig.decode(), self.pub))
        self.assertFalse(upgrader.verify_signature(data + b"x", sig, self.pub))
        self.assertFalse(upgrader.verify_signature(data, sig, keypair()[1]))
        self.assertFalse(upgrader.verify_signature(data, b"not base64!", self.pub))
        self.assertFalse(upgrader.verify_signature(data, sig, None))
        with self.assertRaises(ValueError):
            upgrader.signed_sums(data, sig, None)

    def test_key_file(self):
        f = self.dir / "release_key.pub"
        self.assertIsNone(upgrader.load_key(f))
        f.write_text("# comment\n" + base64.b64encode(self.pub).decode() + "\n")
        self.assertEqual(upgrader.load_key(f), self.pub)
        f.write_text(base64.b64encode(b"short").decode())
        self.assertIsNone(upgrader.load_key(f))

    def test_check_release(self):
        sha = release_dir(self.dir, "1.3.0", self.k)
        self.assertEqual(upgrader.check_release(self.dir, "1.3.0", sha, self.pub).name, "bunkr_1.3.0_all.deb")
        self.assertTrue(upgrader.check_release(self.dir, "1.3.0", None, self.pub))
        with self.assertRaisesRegex(ValueError, "asked for"):
            upgrader.check_release(self.dir, "1.3.0", SHA, self.pub)
        with self.assertRaisesRegex(ValueError, "don't list"):
            upgrader.check_release(self.dir, "1.4.0", None, self.pub)
        with self.assertRaisesRegex(ValueError, "signature"):
            upgrader.check_release(self.dir, "1.3.0", sha, keypair()[1])
        (self.dir / "bunkr_1.3.0_all.deb").write_bytes(b"swapped")
        with self.assertRaisesRegex(ValueError, "doesn't match"):
            upgrader.check_release(self.dir, "1.3.0", sha, self.pub)
        sums = (self.dir / "SHA256SUMS").read_bytes()
        (self.dir / "SHA256SUMS").write_bytes(sums.replace(sha.encode(), hashlib.sha256(b"swapped").hexdigest().encode()))
        with self.assertRaisesRegex(ValueError, "signature"):
            upgrader.check_release(self.dir, "1.3.0", None, self.pub)  # editing the checksums breaks the signature

    def test_store_offer(self):
        self.assertIsNone(upgrader.store_offer(self.dir, self.pub, "1.2.0"))
        release_dir(self.dir, "1.3.0", self.k)
        self.assertEqual(upgrader.store_offer(self.dir, self.pub, "1.2.0")["version"], "1.3.0")
        self.assertIsNone(upgrader.store_offer(self.dir, self.pub, "1.3.0"))
        self.assertIsNone(upgrader.store_offer(self.dir, keypair()[1], "1.2.0"))
        (self.dir / "bunkr_1.3.0_all.deb").write_bytes(b"swapped")
        self.assertIsNone(upgrader.store_offer(self.dir, self.pub, "1.2.0"))

    def test_release_sign_tool(self):
        import release_sign
        keydir = self.dir / "key"
        key_file, pub = release_sign.keygen(keydir, self.dir / "release_key.pub")
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(keydir.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(b"PRIVATE", pub.read_bytes())
        with self.assertRaises(SystemExit):
            release_sign.keygen(keydir, self.dir / "again.pub")       # made once
        sums = self.dir / "SHA256SUMS"
        sums.write_text(f"{SHA}  bunkr_1.3.0_all.deb\n")
        release_sign.sign(sums, key_file)
        self.assertTrue(release_sign.verify(sums, pub))
        sums.write_text(f"{'b' * 64}  bunkr_1.3.0_all.deb\n")
        self.assertFalse(release_sign.verify(sums, pub))
        os.chmod(key_file, 0o644)
        with self.assertRaises(SystemExit):
            release_sign.sign(sums, key_file)                          # a key others can read is refused


class InstallKindTests(unittest.TestCase):
    def test_kinds(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            app, dpkg, timer = t / "usr/lib/bunkr", t / "bunkr.list", t / "bunkr-deploy.timer"
            app.mkdir(parents=True)
            kw = dict(app_dir=app, dpkg_list=dpkg, deploy_timer=timer)
            self.assertEqual(upgrader.install_kind(app, **kw), "manual")
            dpkg.write_text("/usr/lib/bunkr\n")
            self.assertEqual(upgrader.install_kind(app, **kw), "deb")
            self.assertEqual(upgrader.install_kind(t / "opt", **kw), "manual")   # a copy elsewhere isn't the package
            timer.write_text("")
            self.assertEqual(upgrader.install_kind(app, **kw), "git")             # bunkr-deploy.timer: updates from git
            timer.unlink()
            (app / ".git").write_text("gitdir: x")
            self.assertEqual(upgrader.install_kind(app, **kw), "git")
        self.assertEqual(upgrader.install_kind(), "git")                          # this checkout


class FakeRun:
    def __init__(self, fail=()):
        self.cmds, self.fail = [], fail

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        if cmd[0] in self.fail:
            raise subprocess.CalledProcessError(100, cmd, "", "E: broken")
        if cmd[0] == "dpkg-deb":
            ver = Path(cmd[2]).name.split("_")[1]
            return subprocess.CompletedProcess(cmd, 0, f"Package: bunkr\nVersion: {ver}\n", "")
        if cmd[0] == "runuser":
            Path(cmd[-1]).write_text("backup")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def names(self):
        return [" ".join(c[:2]) for c in self.cmds]


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.k, self.pub = keypair()
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.work, self.store, self.backups, self.db = t / "work", t / "store", t / "backups", t / "bunkr.db"
        self.work.mkdir()
        self.backups.mkdir()
        self.db.write_text("db")
        self.sha = release_dir(self.store, "1.3.0", self.k, b"new deb",
                               extra=f"{hashlib.sha256(b'old deb').hexdigest()}  bunkr_1.2.0_all.deb\n")
        (self.store / "bunkr_1.2.0_all.deb").write_bytes(b"old deb")

    def tearDown(self):
        self.tmp.cleanup()

    def up(self, run, healthy=True, key=None):
        return upgrader.Upgrader(run=run, healthy=(lambda: healthy) if not callable(healthy) else healthy,
                                 key=self.pub if key is None else key, current="1.2.0", work=self.work, db=self.db,
                                 backups=self.backups, store=self.store, log=lambda m: None)

    def req(self, **kw):
        d = {"version": "1.3.0", "sha256": self.sha, "source": "store", "repo": "", "confirm_major": False,
             "requested_by": "dad", "requested_at": "2026-11-22T10:00:00+00:00", **kw}
        return json.dumps(d).encode()

    def status(self):
        return upgrader.read_status(self.work / "status.json")

    def test_decisions(self):
        self.assertEqual(upgrader.decide(True, True), "done")
        self.assertEqual(upgrader.decide(True, False), "rollback")
        self.assertEqual(upgrader.decide(False, False), "rollback")
        self.assertEqual(upgrader.rollback_plan("old.deb", "b.db"),
                         [("stop",), ("restore_db", "b.db"), ("install_old", "old.deb"), ("start",)])
        self.assertEqual(upgrader.rollback_plan("old.deb", None), [("stop",), ("install_old", "old.deb"), ("start",)])

    def test_success(self):
        run = FakeRun()
        self.assertEqual(self.up(run).process(self.req()), 0)
        self.assertEqual(self.status()["state"], "done")
        self.assertEqual(self.status()["message"], "Upgraded to 1.3.0.")
        self.assertIn("apt-get install", run.names())
        self.assertLess(run.names().index("runuser -u"), run.names().index("apt-get install"))   # backup first
        self.assertTrue((self.work / "debs/bunkr_1.3.0_all.deb").exists())                      # kept for next time
        self.assertTrue((self.work / "debs/bunkr_1.2.0_all.deb").exists())
        self.assertEqual(len(list(self.backups.glob("bunkr-before-upgrade-1.2.0-to-1.3.0-*.db"))), 1)

    def test_unhealthy_rolls_back(self):
        run = FakeRun()
        calls = iter([False, True])
        self.assertEqual(self.up(run, healthy=lambda: next(calls)).process(self.req()), 1)
        st = self.status()
        self.assertEqual(st["state"], "rolled_back")
        self.assertTrue(st["message"].startswith("Rolled back to 1.2.0: BUNKR didn't answer"))
        names = run.names()
        self.assertEqual(names[names.index("apt-get install"):],
                         ["apt-get install", "systemctl restart", "systemctl stop", "install -o", "dpkg --force-confold", "systemctl start"])
        self.assertTrue(run.cmds[-2][-1].endswith("bunkr_1.2.0_all.deb"))

    def test_install_failure_rolls_back(self):
        run = FakeRun(fail=("apt-get",))
        self.assertEqual(self.up(run).process(self.req()), 1)
        self.assertEqual(self.status()["state"], "rolled_back")
        self.assertIn("the install failed (apt-get exit 100: E: broken)", self.status()["message"])

    def test_rollback_that_fails_says_so(self):
        self.up(FakeRun(), healthy=False).process(self.req())
        self.assertEqual(self.status()["state"], "failed")
        self.assertIn("needs attention", self.status()["message"])

    def test_nothing_changes_when_the_checks_fail(self):
        for raw, key, why in [(self.req(sha256=SHA), None, "asked for"), (self.req(), keypair()[1], "signature"),
                              (self.req(version="1.2.0"), None, "isn't newer"),
                              (self.req(version="1.4.0"), None, "store has no")]:
            run = FakeRun()
            self.assertEqual(self.up(run, key=key).process(raw), 1)
            self.assertIn(self.status()["state"], ("failed", "refused"))
            self.assertIn(why, self.status()["message"])
            self.assertFalse([c for c in run.names() if c.startswith(("apt-get", "dpkg -", "systemctl"))], why)
        run = FakeRun()
        self.assertEqual(self.up(run, key=b"").process(self.req()), 1)   # no release key in this build
        self.assertIn("no release key", self.status()["message"])

    def test_no_previous_version_means_no_upgrade(self):
        (self.store / "bunkr_1.2.0_all.deb").unlink()
        run = FakeRun()
        with mock.patch.object(upgrader, "allowed_repo", return_value=""):
            self.assertEqual(self.up(run).process(self.req()), 1)
        self.assertIn("no signed copy of 1.2.0", self.status()["message"])
        self.assertNotIn("apt-get install", run.names())

    def test_wrong_package_in_the_deb(self):
        run = FakeRun()
        run_ = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "Package: other\nVersion: 1.3.0\n", "") \
            if cmd[0] == "dpkg-deb" else run(cmd, **kw)
        self.assertEqual(self.up(run_).process(self.req()), 1)
        self.assertIn("isn't the BUNKR version", self.status()["message"])


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHub:
    def __init__(self, files):
        self.files, self.asked = files, []

    def open(self, req, timeout=None):
        self.asked.append(req.full_url)
        if req.full_url not in self.files:
            raise urllib.error.URLError("not found")
        return Resp(self.files[req.full_url])


class AppSideTests(unittest.TestCase):
    """What the hub shows and offers, and the route that writes the request."""

    def setUp(self):
        self.k, self.pub = keypair()
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "key.pub").write_text(base64.b64encode(self.pub).decode())
        (t / "spool").mkdir()
        self.spool = t / "spool"
        self.patches = [mock.patch.object(upgrader, "KEY_FILE", t / "key.pub"), mock.patch.object(upgrader, "SPOOL", self.spool),
                        mock.patch.object(upgrader, "REQUEST", self.spool / "request.json"),
                        mock.patch.object(upgrader, "STATUS", t / "status.json"),
                        mock.patch.object(upgrader, "STORE_RELEASES", t / "releases")]
        for p in self.patches:
            p.start()
        self.new = bump(__version__, 1)
        self.t = t

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def github_release(self, sig_key=None):
        sums = f"{SHA}  {upgrader.deb_name(self.new)}\n".encode()
        base = "https://github.com/someone/bunkr/releases/download/v" + self.new
        rel = {"tag_name": "v" + self.new, "name": "x", "body": "", "html_url": "", "published_at": "", "draft": False, "prerelease": False,
               "assets": [{"name": n, "browser_download_url": f"{base}/{n}"} for n in ("SHA256SUMS", "SHA256SUMS.sig", upgrader.deb_name(self.new))]}
        return FakeGitHub({"https://api.github.com/repos/someone/bunkr/releases/latest": json.dumps(rel).encode(),
                           f"{base}/SHA256SUMS": sums, f"{base}/SHA256SUMS.sig": sign(sig_key or self.k, sums)})

    def test_check_fetches_signed_checksum_on_deb_installs(self):
        st = Store(self.t / "v.db")
        selfupdate.save_settings(st, {"repo": "someone/bunkr"})
        with mock.patch.object(upgrader, "install_kind", return_value="deb"), \
                mock.patch.object(upgrader, "allowed_repo", return_value="someone/bunkr"):
            v = selfupdate.check(st, opener=self.github_release())
            self.assertEqual(v["latest"]["deb"]["sha256"], SHA)
            self.assertEqual(v["upgrade"]["offer"], {"version": self.new, "sha256": SHA, "source": "github",
                                                     "major": False})
            v = selfupdate.check(st, opener=self.github_release(sig_key=keypair()[0]))
            self.assertIsNone(v["upgrade"]["offer"])                              # a bad signature: no button
            self.assertIn("signature", v["upgrade"]["why"])
            selfupdate.save_settings(st, {"repo": "other/fork"})
            v = selfupdate.view(st)
            self.assertIsNone(v["upgrade"]["offer"])
        gh = self.github_release()
        selfupdate.save_settings(st, {"repo": "someone/bunkr"})
        v = selfupdate.check(st, opener=gh)                                        # this git checkout: no extra downloads
        self.assertEqual(len(gh.asked), 1)
        self.assertEqual((v["upgrade"]["kind"], v["upgrade"]["why"], v["upgrade"]["offer"]),
                         ("git", "This hub updates from git.", None))

    def test_store_copy_offered_without_internet(self):
        release_dir(upgrader.STORE_RELEASES, self.new, self.k)
        v = upgrader.upgrade_view({}, kind="deb")
        self.assertEqual((v["offer"]["version"], v["offer"]["source"]), (self.new, "store"))
        self.assertEqual(upgrader.upgrade_view({}, kind="manual")["offer"], None)
        upgrader.request_upgrade(v, self.new, "dad")
        d = upgrader.validate_request((self.spool / "request.json").read_bytes())
        self.assertEqual((d["source"], d["version"], d["requested_by"]), ("store", self.new, "dad"))
        v = upgrader.upgrade_view({}, kind="deb")
        self.assertEqual(v["pending"], self.new)
        with self.assertRaisesRegex(ValueError, "under way"):
            upgrader.request_upgrade(v, self.new, "dad")

    def test_major_needs_confirm(self):
        major = bump(__version__, 0)
        release_dir(upgrader.STORE_RELEASES, major, self.k)
        v = upgrader.upgrade_view({}, kind="deb")
        self.assertTrue(v["offer"]["major"])
        with self.assertRaisesRegex(ValueError, "confirm"):
            upgrader.request_upgrade(v, major, "dad")
        with self.assertRaisesRegex(ValueError, "isn't on offer"):
            upgrader.request_upgrade(v, "99.0.0", "dad", True)
        upgrader.request_upgrade(v, major, "dad", confirm_major=True)
        self.assertTrue(json.loads((self.spool / "request.json").read_text())["confirm_major"])

    def test_status_shown(self):
        upgrader.write_status("rolled_back", "1.3.0", "Rolled back to 1.2.0: BUNKR didn't answer.")
        self.assertEqual(upgrader.read_status()["state"], "rolled_back")
        upgrader.STATUS.write_text('{"state": "pwned"}')
        self.assertEqual(upgrader.read_status(), {})
        release_dir(upgrader.STORE_RELEASES, self.new, self.k)
        upgrader.write_status("running", self.new, "Upgrading…")
        with self.assertRaisesRegex(ValueError, "under way"):
            upgrader.request_upgrade(upgrader.upgrade_view({}, kind="deb"), self.new, "dad")
        upgrader.STATUS.write_text(json.dumps({"state": "running", "version": self.new, "at": "2026-01-01T00:00:00+00:00"}))
        upgrader.request_upgrade(upgrader.upgrade_view({}, kind="deb"), self.new, "dad")   # a stale "running" doesn't block

    def test_route(self):
        hub = p3.Hub()
        try:
            auth.create_user(hub.store, "dad", p3.PW, "admin")
            auth.create_user(hub.store, "kid", p3.PW, "family")
            opener = urllib.request.build_opener(p3.NoRedirect)

            def call(user, body):
                r = urllib.request.Request(hub.url + "/api/login", data=json.dumps({"username": user, "password": p3.PW}).encode(),
                                           method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
                with opener.open(r) as resp:
                    c = resp.headers["Set-Cookie"].split("bunkr_session=")[1].split(";")[0]
                r = urllib.request.Request(hub.url + "/api/version/upgrade", method="POST", data=json.dumps(body).encode(),
                                           headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={c}"})
                try:
                    with opener.open(r) as resp:
                        return resp.status, json.loads(resp.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read() or b"{}")

            release_dir(upgrader.STORE_RELEASES, self.new, self.k)
            self.assertEqual(call("kid", {"version": self.new})[0], 403)
            code, r = call("dad", {"version": self.new})
            self.assertEqual((code, r.get("error")), (400, "This hub updates from git."))   # this checkout
            self.assertFalse((self.spool / "request.json").exists())
            with mock.patch.object(upgrader, "install_kind", return_value="deb"):
                code, r = call("dad", {"version": self.new})
            self.assertEqual((code, r["upgrade"]["pending"]), (200, self.new))
            self.assertEqual(json.loads((self.spool / "request.json").read_text())["requested_by"], "dad")
        finally:
            hub.close()


@unittest.skipUnless(shutil.which("dpkg-deb"), "needs dpkg-deb")
class BuildDebTests(unittest.TestCase):
    def test_build(self):
        import build_deb
        with tempfile.TemporaryDirectory() as t:
            deb = build_deb.build(ROOT, t, "Test Builder <test@example.org>", epoch=1_700_000_000)
            self.assertEqual(deb.name, f"bunkr_{__version__}_all.deb")
            sums = (Path(t) / "SHA256SUMS").read_text()
            self.assertEqual(sums, f"{hashlib.sha256(deb.read_bytes()).hexdigest()}  {deb.name}\n")
            info = subprocess.run(["dpkg-deb", "--info", str(deb)], capture_output=True, text=True, check=True).stdout
            for want in ("Package: bunkr", f"Version: {__version__}", "Architecture: all",
                         "Depends: python3 (>= 3.11), python3-cryptography, 7zip | p7zip-full, rsync",
                         "Maintainer: Test Builder <test@example.org>", "postinst", "preinst", "prerm", "postrm", "conffiles"):
                self.assertIn(want, info)
            fields = subprocess.run(["dpkg-deb", "-f", str(deb), "Package", "Version"], capture_output=True, text=True).stdout
            self.assertEqual(fields, f"Package: bunkr\nVersion: {__version__}\n")
            listing = subprocess.run(["dpkg-deb", "--contents", str(deb)], capture_output=True, text=True, check=True).stdout
            paths = {line.split()[-1] for line in listing.splitlines()}
            for want in ("./usr/lib/bunkr/bunkr/__init__.py", "./usr/lib/bunkr/bunkr/upgrader.py", "./usr/lib/bunkr/bunkr/static/app.js",
                         "./usr/lib/bunkr/bunkr-upgrade", "./usr/bin/bunkr", "./etc/default/bunkr",
                         "./usr/lib/systemd/system/bunkr.service", "./usr/lib/systemd/system/bunkr-upgrade.path",
                         "./usr/lib/systemd/system/bunkr-upgrade.service", "./usr/share/doc/bunkr/copyright"):
                self.assertIn(want, paths)
            self.assertFalse([p for p in paths if "__pycache__" in p or p.endswith(".pyc") or "/tests/" in p or "/opt/" in p])
            self.assertFalse([line for line in listing.splitlines() if "root/root" not in line])
            ctl = Path(t) / "ctl"
            subprocess.run(["dpkg-deb", "--control", str(deb), str(ctl)], check=True)
            self.assertEqual((ctl / "conffiles").read_text(), "/etc/default/bunkr\n")
            for s in ("preinst", "postinst", "prerm", "postrm"):
                self.assertTrue(os.access(ctl / s, os.X_OK))
                subprocess.run(["sh", "-n", str(ctl / s)], check=True)
            self.assertIn("rm -rf /var/lib/bunkr-upgrade /var/lib/bunkr", (ctl / "postrm").read_text().split("purge)")[1])
            self.assertNotIn("rm -rf", (ctl / "postrm").read_text().split("purge)")[0])   # remove keeps the data
            again = build_deb.build(ROOT, Path(t) / "again", "Test Builder <test@example.org>", epoch=1_700_000_000)
            self.assertEqual(deb.read_bytes(), again.read_bytes())                         # reproducible


class UnitFileTests(unittest.TestCase):
    def test_units(self):
        path = (ROOT / "deploy/bunkr-upgrade.path").read_text()
        self.assertIn("PathExists=/var/lib/bunkr/upgrade/request.json", path)
        svc = (ROOT / "deploy/bunkr-upgrade.service").read_text()
        self.assertIn("ExecStart=/usr/lib/bunkr/bunkr-upgrade", svc)
        self.assertNotIn("User=", svc)                                                   # root, on purpose
        app = (ROOT / "deploy/deb/bunkr.service").read_text()
        self.assertIn("User=bunkr", app)
        self.assertIn("NoNewPrivileges=yes", app)
        self.assertIn("/usr/lib/bunkr", (ROOT / "deploy/deb/preinst").read_text().split("grep -q")[1])
        self.assertEqual(upgrader.SPOOL.as_posix() + "/request.json", "/var/lib/bunkr/upgrade/request.json")


if __name__ == "__main__":
    unittest.main()
