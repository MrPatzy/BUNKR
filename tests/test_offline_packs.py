"""Offline packs (bunkr/packs.py) and careful downloads (bunkr/safeget.py). No real network: a local plain-HTTP server
stands in for the internet, and name lookups are faked."""

import hashlib
import http.client
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, kiwix, manifest, nodes, packages, packs, safeget, server, sync  # noqa: E402
from bunkr.db import Store  # noqa: E402
from bunkr.depot import Depot  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402
from tests.test_packages import FakeApt  # noqa: E402

PUBLIC = {"files.example": "93.184.216.34", "mirror.example": "93.184.216.35", "evil.example": "10.0.0.5",
          "meta.example": "169.254.169.254", "lan.example": "192.168.1.20", "loop.example": "127.0.0.1"}


class Web(BaseHTTPRequestHandler):
    """The internet, as far as these tests need it."""
    files = {}          # path -> bytes
    etags = {}
    short = set()       # paths that promise more than they send
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Web.seen.append((self.headers.get("Host"), self.path, self.headers.get("If-None-Match")))
        redirects = {"/to-http": "http://files.example/a.txt", "/to-evil": "https://evil.example/a.txt",
                     "/to-ok": "https://mirror.example/a.txt", "/loop": "/loop", "/to-file": "file:///etc/passwd"}
        if self.path in redirects:
            self.send_response(302)
            self.send_header("Location", redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = Web.files.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        tag = Web.etags.get(self.path)
        if tag and self.headers.get("If-None-Match") == tag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body) + (100 if self.path in Web.short else 0)))
        self.send_header("Last-Modified", "Sat, 21 Nov 2026 10:00:00 GMT")
        if tag:
            self.send_header("ETag", tag)
        self.end_headers()
        self.wfile.write(body)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):   # a download BUNKR broke off on purpose (over the size cap)
        pass


class WebCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = QuietServer(("127.0.0.1", 0), Web)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        Web.files = {"/a.txt": b"hello pack", "/manual.pdf": b"%PDF manual v1"}
        Web.etags, Web.short, Web.seen = {}, set(), []
        self.connected = []
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.store = Store(d / "p.db")
        self.depot = Depot(self.store, d / "store")
        self.depot.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def getaddrinfo(self, host, port, type=0):
        if host not in PUBLIC:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC[host], port))]

    def getter(self, allow_lan=False):
        def connect(host, ip, port, timeout):
            self.connected.append((host, ip))
            return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        return safeget.Getter(allow_lan=allow_lan, connect=connect, getaddrinfo=self.getaddrinfo, timeout=5)

    def refresh(self, **kw):
        log = []
        r = packs.refresh(self.store, self.depot, log=log.append, g=kw.pop("g", None) or self.getter(), **kw)
        return r, log

    def state(self, slug, item):
        return self.store.get_raw("packs_state", {})[slug][item["id"]]


class ValidationTests(unittest.TestCase):
    def test_addresses(self):
        for bad in ("http://files.example/a", "file:///etc/passwd", "ftp://x.org/a", "https://user:pw@x.org/a",
                    "https://127.0.0.1/a", "https://[::1]/", "https://169.254.169.254/latest/meta-data",
                    "https://10.1.2.3/a", "https://192.168.1.5/a", "https://[::ffff:10.0.0.1]/", "https://localhost/a",
                    "https://nas.local/a", "https://[64:ff9b::a00:1]/", "https://x.org/a b", "https://x.org:99999/", "", "https://"):
            with self.assertRaises(safeget.Blocked, msg=bad):
                packs.clean_item({"type": "url", "url": bad})
        self.assertEqual(safeget.check_url("https://192.168.1.5/a", allow_lan=True)[1], "192.168.1.5")
        for never in ("https://127.0.0.1/", "https://169.254.169.254/", "https://localhost/"):
            with self.assertRaises(safeget.Blocked):
                safeget.check_url(never, allow_lan=True)

    def test_items(self):
        i = packs.clean_item({"type": "url", "url": "https://x.org/dl/..%2F..%2Fetc%2Fpasswd", "sha256": "AB" * 32})
        self.assertEqual((i["file"], i["sha256"]), ("passwd", "ab" * 32))
        for name, want in (("../../.bashrc", "bashrc"), ("pack.json", "download"), ("Packages", "download"),
                           ("a file (1).PDF", "a_file_1_.PDF"), ("x.part", "x.part.bin"), ("...", "download")):
            self.assertEqual(packs.safe_file(name), want, name)
        with self.assertRaises(ValueError):
            packs.clean_item({"type": "url", "url": "https://x.org/a", "sha256": "abc"})
        with self.assertRaises(ValueError):
            packs.clean_item({"type": "url", "url": "https://x.org/a", "run": "yes"})
        with self.assertRaises(ValueError):
            packs.clean_item({"type": "script", "url": "https://x.org/a"})
        for bad in ("A", "Tesseract", "x", "-rf", "a b", "a;rm", "foo/bar", "../x", "a" * 101):
            with self.assertRaises(ValueError, msg=bad):
                packs.clean_item({"type": "package", "package": bad})
        for good in ("gpsd", "g++", "libc6", "python3.12-venv", "0ad"):
            self.assertEqual(packs.clean_item({"type": "package", "package": good})["package"], good)
        z = packs.clean_item({"type": "zim", "name": "ifixit_en_all", "flavour": "maxi"})
        self.assertEqual(z["type"], "zim")
        with self.assertRaises(ValueError):
            packs.clean_item({"type": "zim", "name": "../x", "flavour": ""})

    def test_packs_and_duplicates(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "a.db")
        p = packs.create(store, "  Car   manuals ")
        self.assertEqual((p["name"], p["slug"]), ("Car manuals", "car-manuals"))
        with self.assertRaises(ValueError):
            packs.create(store, "car manuals")
        self.assertEqual(packs.create(store, "Import")["slug"], "import-2")     # a word the API uses
        packs.add_item(store, "car-manuals", {"type": "url", "url": "https://a.org/x/manual.pdf"})
        b = packs.add_item(store, "car-manuals", {"type": "url", "url": "https://b.org/manual.pdf"})
        self.assertTrue(b["file"].endswith("-manual.pdf") and b["file"] != "manual.pdf")   # two files, two names
        with self.assertRaises(ValueError):
            packs.add_item(store, "car-manuals", {"type": "url", "url": "https://b.org/manual.pdf"})
        with self.assertRaises(LookupError):
            packs.add_item(store, "nope", {"type": "url", "url": "https://b.org/m.pdf"})
        with self.assertRaises(ValueError):
            packs.save_settings(store, {"max_item_gb": 0})
        self.assertEqual(packs.save_settings(store, {"max_item_gb": "2.5", "allow_lan": "yes"}),
                         {"max_item_gb": 2.5, "allow_lan": False})                # only a real true turns it on


class ImportExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "a.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        p = packs.create(self.store, "Radio", "for the go bag")
        packs.add_item(self.store, p["slug"], {"type": "url", "url": "https://x.org/band.pdf", "sha256": "0" * 64, "label": "Band plan"})
        packs.add_item(self.store, p["slug"], {"type": "zim", "name": "ham.stackexchange.com_en_all", "flavour": ""})
        packs.add_item(self.store, p["slug"], {"type": "package", "package": "chirp"})
        out = packs.export(packs.get(self.store, p["slug"]))
        text = json.dumps(out)
        self.assertNotIn('"id"', text)
        self.assertNotIn("slug", text)
        self.assertEqual(out["bunkr_pack"], 1)
        again = packs.import_pack(self.store, text)
        self.assertEqual(again["name"], "Radio (2)")
        self.assertEqual([i["id"] for i in again["items"]], [i["id"] for i in packs.get(self.store, p["slug"])["items"]])
        self.assertEqual(packs.export(again)["items"], out["items"])

    def test_strict(self):
        ok = {"bunkr_pack": 1, "name": "X", "items": [{"type": "url", "url": "https://x.org/a"}]}
        packs.parse_import(json.dumps(ok))
        bad = [
            "not json", "[]", json.dumps({"name": "X", "items": ok["items"]}),
            json.dumps(ok | {"bunkr_pack": 2}), json.dumps(ok | {"bunkr_pack": True}),
            json.dumps(ok | {"extra": 1}), json.dumps(ok | {"name": ""}), json.dumps(ok | {"name": 5}),
            json.dumps(ok | {"items": []}), json.dumps(ok | {"items": "x"}),
            json.dumps(ok | {"items": [{"type": "url", "url": "http://x.org/a"}]}),
            json.dumps(ok | {"items": [{"type": "url", "url": "https://10.0.0.1/a"}]}),
            json.dumps(ok | {"items": [{"type": "url", "url": "https://x.org/a", "id": "i1"}]}),
            json.dumps(ok | {"items": [{"type": "url", "url": "https://x.org/a", "mode": "755"}]}),
            json.dumps(ok | {"items": [{"type": "url", "url": ["https://x.org/a"]}]}),
            json.dumps(ok | {"items": [{"type": "package", "package": "a;b"}]}),
            json.dumps(ok | {"items": ok["items"] * 2}),
            json.dumps(ok | {"items": [{"type": "url", "url": f"https://x.org/{n}"} for n in range(packs.MAX_ITEMS + 1)]}),
            json.dumps(ok | {"note": "x" * (packs.MAX_IMPORT_BYTES + 1)}),
        ]
        for b in bad:
            with self.assertRaises(ValueError, msg=b[:80]):
                packs.parse_import(b)
        with self.assertRaises(ValueError) as cm:
            packs.parse_import(json.dumps(ok | {"items": ok["items"] + [{"type": "url", "url": "file:///x"}]}))
        self.assertIn("item 2", str(cm.exception))
        self.assertEqual(packs.all_packs(self.store), [])    # nothing saved from a bad file


class SafeGetTests(WebCase):
    def test_redirects_and_lookups(self):
        g = self.getter()
        d = Path(self.tmp.name)
        r = g.download("https://files.example/to-ok", d / "x", 1000)
        self.assertEqual((r["status"], (d / "x").read_bytes()), (200, b"hello pack"))
        self.assertEqual([c[0] for c in self.connected], ["files.example", "mirror.example"])
        self.assertEqual(self.connected[1][1], PUBLIC["mirror.example"])   # connected to the address that was checked
        for url in ("https://files.example/to-http", "https://files.example/to-evil", "https://files.example/to-file",
                    "https://files.example/loop", "https://evil.example/a.txt", "https://meta.example/a.txt",
                    "https://loop.example/a.txt"):
            with self.assertRaises(safeget.Blocked, msg=url):
                g.download(url, d / "y", 1000)
            self.assertFalse((d / "y").exists())
        with self.assertRaises(IOError):
            g.download("https://nowhere.example/a.txt", d / "y", 1000)
        self.assertEqual(self.getter(allow_lan=True).download("https://lan.example/a.txt", d / "z", 1000)["status"], 200)
        with self.assertRaises(safeget.Blocked):      # home-network addresses on still never reach this machine
            self.getter(allow_lan=True).download("https://loop.example/a.txt", d / "z", 1000)

    def test_a_name_with_one_bad_address_is_refused(self):
        g = self.getter()
        g.getaddrinfo = lambda host, port, type=0: [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("10.0.0.1", port))]
        with self.assertRaises(safeget.Blocked):
            g.download("https://files.example/a.txt", Path(self.tmp.name) / "x", 1000)
        self.assertEqual(self.connected, [])

    def test_size_cap_and_short_downloads(self):
        d = Path(self.tmp.name)
        with self.assertRaises(safeget.TooBig):
            self.getter().download("https://files.example/a.txt", d / "x", 5)
        self.assertFalse((d / "x").exists())
        Web.short.add("/a.txt")
        with self.assertRaises(IOError):
            self.getter().download("https://files.example/a.txt", d / "x", 1000)
        self.assertFalse((d / "x").exists())


class FetchTests(WebCase):
    def add(self, **item):
        p = packs.all_packs(self.store)
        slug = p[0]["slug"] if p else packs.create(self.store, "Manuals")["slug"]
        return slug, packs.add_item(self.store, slug, {"type": "url", **item})

    def test_fetch_records_and_permissions(self):
        slug, item = self.add(url="https://files.example/manual.pdf", label="Generator manual")
        r, log = self.refresh()
        self.assertEqual(r, {"ok": 1, "failed": 0, "changed": 1})
        f = self.depot.root / "packs" / slug / "manual.pdf"
        self.assertEqual(f.read_bytes(), b"%PDF manual v1")
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o644)
        row = self.depot.by_path(f"packs/{slug}/manual.pdf")
        self.assertEqual((row["kind"], row["item_id"], row["sha256"]),
                         ("pack", f"{slug}:{item['id']}", hashlib.sha256(b"%PDF manual v1").hexdigest()))
        self.assertEqual(row["version"], "2026-11-21")
        st = self.state(slug, item)
        self.assertEqual((st["status"], st["size"]), ("ok", 14))
        self.assertTrue(st["checked_at"])
        listing = json.loads((self.depot.root / "packs" / slug / "pack.json").read_text())
        self.assertEqual(listing["items"][0]["url"], "https://files.example/manual.pdf")
        self.assertFalse((self.depot.root / "packs" / ".staging").exists())
        self.assertEqual(self.depot.verify(), [])
        v = packs.view(self.store, self.depot)[0]
        self.assertEqual((v["items"][0]["held"], v["files"]), (True, 2))

    def test_stage_then_swap_keeps_the_good_copy(self):
        slug, item = self.add(url="https://files.example/manual.pdf")
        Web.etags["/manual.pdf"] = '"v1"'
        self.refresh()
        dest = self.depot.root / "packs" / slug / "manual.pdf"
        # not changed: the server says so and nothing is downloaded
        r, _ = self.refresh()
        self.assertEqual(r["changed"], 0)
        self.assertEqual(Web.seen[-1][2], '"v1"')
        # a new version that breaks off half way: the old copy stays, the error is shown
        Web.files["/manual.pdf"], Web.etags["/manual.pdf"] = b"%PDF manual v2 longer", '"v2"'
        Web.short.add("/manual.pdf")
        r, log = self.refresh()
        self.assertEqual(r["failed"], 1)
        self.assertEqual(dest.read_bytes(), b"%PDF manual v1")
        self.assertEqual(self.depot.by_path(f"packs/{slug}/manual.pdf")["sha256"], hashlib.sha256(b"%PDF manual v1").hexdigest())
        st = self.state(slug, item)
        self.assertEqual(st["status"], "error")
        self.assertEqual(st["size"], 14)                 # still describes the copy we have
        self.assertTrue(any("old copy" in line for line in log))
        # then it works: swapped in
        Web.short.clear()
        r, _ = self.refresh()
        self.assertEqual(r["changed"], 1)
        self.assertEqual(dest.read_bytes(), b"%PDF manual v2 longer")
        self.assertEqual(self.depot.by_path(f"packs/{slug}/manual.pdf")["size"], 21)
        self.assertEqual(self.state(slug, item)["status"], "ok")

    def test_checksum_mismatch_stores_nothing(self):
        slug, item = self.add(url="https://files.example/a.txt", sha256="f" * 64)
        r, _ = self.refresh()
        self.assertEqual(r["failed"], 1)
        self.assertIn("checksum", self.state(slug, item)["error"])
        self.assertFalse((self.depot.root / "packs" / slug / "a.txt").exists())
        self.assertIsNone(self.depot.by_path(f"packs/{slug}/a.txt"))
        self.assertEqual(list((self.depot.root / "packs").glob(".staging/**/*.part")), [])

    def test_checksum_given_is_checked_and_pinned(self):
        good = hashlib.sha256(b"hello pack").hexdigest()
        slug, item = self.add(url="https://files.example/a.txt", sha256=good)
        self.assertEqual(self.refresh()[0]["changed"], 1)
        n = len(Web.seen)
        Web.files["/a.txt"] = b"changed!"
        self.assertEqual(self.refresh()[0], {"ok": 1, "failed": 0, "changed": 0})
        self.assertEqual(len(Web.seen), n)            # the pinned copy is never fetched again
        self.assertEqual((self.depot.root / "packs" / slug / "a.txt").read_bytes(), b"hello pack")

    def test_size_cap_and_blocked_address_at_fetch(self):
        packs.save_settings(self.store, {"max_item_gb": 0.01})
        Web.files["/big.iso"] = b"x" * 20_000_001
        slug, big = self.add(url="https://files.example/big.iso")
        _, evil = self.add(url="https://evil.example/a.txt")       # the name points at the home network
        r, _ = self.refresh()
        self.assertEqual(r["failed"], 2)
        self.assertIn("limit", self.state(slug, big)["error"])
        self.assertIn("10.0.0.5", self.state(slug, evil)["error"])
        self.assertFalse(any(c[0] == "evil.example" for c in self.connected))

    def test_remove_item_and_pack(self):
        slug, a = self.add(url="https://files.example/a.txt")
        _, m = self.add(url="https://files.example/manual.pdf")
        self.refresh()
        packs.remove_item(self.store, self.depot, slug, a["id"])
        self.assertFalse((self.depot.root / "packs" / slug / "a.txt").exists())
        self.assertTrue((self.depot.root / "packs" / slug / "manual.pdf").exists())
        packs.delete(self.store, self.depot, slug)
        self.assertFalse((self.depot.root / "packs" / slug).exists())
        self.assertEqual([r for r in self.depot.rows() if r["kind"] == "pack"], [])
        self.assertTrue((self.depot.root / "packs").is_dir())
        with self.assertRaises(LookupError):
            packs.delete(self.store, self.depot, "../library")

    def test_kiwix_book_in_a_pack(self):
        zim = b"ZIM" * 1000
        Web.files["/zim/ham_2026-10.zim"] = zim
        Web.files["/zim/ham_2026-10.zim.meta4"] = (
            '<?xml version="1.0"?><metalink xmlns="urn:ietf:params:xml:ns:metalink"><file name="ham_2026-10.zim">'
            f'<size>{len(zim)}</size><hash type="sha-256">{hashlib.sha256(zim).hexdigest()}</hash>'
            '<url priority="1">https://mirror.example/zim/ham_2026-10.zim</url></file></metalink>').encode()
        found = {"url": "https://files.example/zim/ham_2026-10.zim", "meta4": "https://files.example/zim/ham_2026-10.zim.meta4",
                 "file": "ham_2026-10.zim", "size": len(zim), "date": "2026-10-01"}
        p = packs.create(self.store, "Radio")
        item = packs.add_item(self.store, p["slug"], {"type": "zim", "name": "ham.stackexchange.com_en_all", "flavour": ""})
        asked = []
        r, _ = self.refresh(lookup=lambda e: asked.append(e) or found)
        self.assertEqual(r["changed"], 1)
        self.assertEqual(asked[0]["name"], "ham.stackexchange.com_en_all")
        f = self.depot.root / "packs" / "radio" / "ham_2026-10.zim"
        self.assertEqual(f.read_bytes(), zim)
        self.assertEqual(self.state("radio", item)["version"], "2026-10-01")
        self.assertEqual(self.refresh(lookup=lambda e: found)[0]["changed"], 0)     # same date: not fetched again
        # the reader serves it too, unless the library already has the same book
        self.assertIn(f, kiwix.zim_files(self.depot.root / "library"))
        (self.depot.root / "library" / "ham_2026-10.zim").write_bytes(zim)
        self.assertEqual([z.name for z in kiwix.zim_files(self.depot.root / "library")], ["ham_2026-10.zim"])
        # a corrupt book from the catalog is refused
        Web.files["/zim/ham_2026-10.zim"] = b"tampered" * 10
        found2 = found | {"date": "2026-11-01"}
        r, _ = self.refresh(lookup=lambda e: found2)
        self.assertEqual(r["failed"], 1)
        self.assertEqual(f.read_bytes(), zim)

    def test_two_items_with_one_file_name_do_not_overwrite_each_other(self):
        Web.files["/zim/book.zim"] = b"web copy"
        Web.files["/k/book.zim"] = b"ZIMZIM"
        slug, web = self.add(url="https://files.example/zim/book.zim")
        book = packs.add_item(self.store, slug, {"type": "zim", "name": "book_en_all", "flavour": ""})
        found = {"url": "https://files.example/k/book.zim", "file": "book.zim", "size": 6, "date": "2026-10-01"}
        self.assertEqual(self.refresh(lookup=lambda e: found)[0], {"ok": 2, "failed": 0, "changed": 2})
        folder = self.depot.root / "packs" / slug
        self.assertEqual((folder / "book.zim").read_bytes(), b"web copy")
        self.assertEqual((folder / f"{book['id']}-book.zim").read_bytes(), b"ZIMZIM")
        self.assertEqual(self.refresh(lookup=lambda e: found)[0]["changed"], 0)   # stable on the next run
        self.assertEqual(self.depot.verify(), [])

    def test_os_packages_in_a_pack(self):
        p = packs.create(self.store, "Mapping")
        item = packs.add_item(self.store, p["slug"], {"type": "package", "package": "tesseract-ocr"})
        r, _ = self.refresh(run=FakeApt())
        self.assertEqual(r["failed"], 0)
        sysid = packages.system_id(FakeApt())
        folder = self.depot.root / "packs" / "mapping" / "packages" / sysid
        self.assertEqual(sorted(x.name for x in folder.iterdir()),
                         ["Packages", "libtesseract5_5.3.4-1_amd64.deb", "tesseract-ocr_5.3.4-1_amd64.deb"])
        self.assertEqual(self.state("mapping", item)["version"], "5.3.4-1")
        rows = [x for x in self.depot.rows() if x["item_id"] == "mapping:packages"]
        self.assertEqual(len(rows), 3)
        packs.remove_item(self.store, self.depot, "mapping", item["id"])
        self.assertFalse(folder.exists())
        self.assertEqual([x for x in self.depot.rows() if x["item_id"] == "mapping:packages"], [])


class OsPackageTests(unittest.TestCase):
    class Apt(FakeApt):
        def __call__(self, cmd, cwd=None, timeout=None):
            if cmd[:2] == ["apt-cache", "show"] and cmd[-1] == "nosuchthing":
                return SimpleNamespace(returncode=100, stdout="", stderr="E: No packages found")
            if cmd[:2] == ["apt-cache", "show"]:
                return SimpleNamespace(returncode=0, stderr="", stdout=f"Package: {cmd[-1]}\nPriority: "
                                       f"{'required' if cmd[-1] == 'libc6' else 'optional'}\nSize: 1000000\n")
            if cmd[:2] == ["apt-cache", "depends"] and "gpsd" in cmd:
                return SimpleNamespace(returncode=0, stderr="", stdout="tesseract-ocr\n  Depends: libtesseract5\n"
                                       "libtesseract5\ntesseract-ocr-eng\ngpsd\n  Depends: libgps28\nlibgps28\nlibc6\n")
            if cmd[:2] == ["apt-cache", "depends"]:
                return SimpleNamespace(returncode=0, stderr="", stdout="tesseract-ocr\n  Depends: libtesseract5\n"
                                       "libtesseract5\ntesseract-ocr-eng\nlibc6\n")
            return super().__call__(cmd, cwd, timeout)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.store = Store(d / "a.db")
        self.depot = Depot(self.store, d / "store")
        self.depot.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_check_remove(self):
        apt = self.Apt()
        with self.assertRaises(ValueError):
            packs.add_os(self.store, "GPSD; rm -rf /", apt)
        with self.assertRaises(ValueError):
            packs.add_os(self.store, "nosuchthing", apt)
        with self.assertRaises(ValueError):
            packs.add_os(self.store, "tesseract-ocr", apt)          # built in already
        info = packs.add_os(self.store, "gpsd", apt)
        self.assertEqual((info["before"], info["after"], info["new"]), (3_000_000, 5_000_000, ["gpsd", "libgps28"]))
        self.assertEqual(packs.os_names(self.store), packages.OPTIONAL + ["gpsd"])
        with self.assertRaises(ValueError):
            packs.add_os(self.store, "gpsd", apt)
        packs.remove_os(self.store, "gpsd")
        self.assertEqual(packs.os_names(self.store), packages.OPTIONAL)
        with self.assertRaises(LookupError):
            packs.remove_os(self.store, "gpsd")

    def test_an_unknown_name_does_not_stop_the_rest(self):
        r = packages.cache(self.depot, packages.OPTIONAL + ["nosuchthing"], run=self.Apt(), log=lambda *_: None)
        self.assertEqual(r["missing"], ["nosuchthing"])
        self.assertEqual(r["files"], 3)


class NodeTests(WebCase):
    def two_packs(self):
        for name, path in (("Manuals", "/manual.pdf"), ("Recipes", "/a.txt")):
            p = packs.create(self.store, name)
            packs.add_item(self.store, p["slug"], {"type": "url", "url": "https://files.example" + path})
        self.refresh()

    def test_plan_includes_packs_and_follows_the_choice(self):
        self.two_packs()
        rows = self.depot.rows()
        planned = lambda prof: sorted(r["path"] for r in nodes.plan({"role": "pi", "profile": json.dumps(prof)}, rows, {}, 0)["files"]
                                      if r["kind"] == "pack")
        everything = ["packs/manuals/manual.pdf", "packs/manuals/pack.json", "packs/recipes/a.txt", "packs/recipes/pack.json"]
        self.assertEqual(planned({}), everything)                               # on by default
        self.assertEqual(planned({"packs": True}), everything)
        self.assertEqual(planned({"packs": False}), [])
        self.assertEqual(planned({"packs": ["recipes"]}), ["packs/recipes/a.txt", "packs/recipes/pack.json"])
        self.assertEqual(planned({"packs": "junk"}), everything)
        self.assertEqual(nodes.profile_for({"role": "pi", "profile": json.dumps({"packs": ["a", 3, None]})})["packs"], ["a"])
        manifest_paths = {f["path"] for f in self.depot.store_manifest()}
        self.assertTrue(set(everything) <= manifest_paths)
        # a node short on space skips them rather than failing
        tight = nodes.plan({"role": "pi", "profile": "{}"}, rows, {}, 20)
        self.assertTrue(any(s["kind"] == "pack" for s in tight["skipped"]))


class HubTests(WebCase):
    """Through the web API and a real sync to a paired node."""

    def setUp(self):
        super().setUp()
        self.hub = p3.Hub()
        auth.create_user(self.hub.store, "dad", p3.PW, "admin")
        auth.create_user(self.hub.store, "kid", p3.PW, "family")
        self.cookies = {u: self.login(u) for u in ("dad", "kid")}

    def tearDown(self):
        self.hub.close()
        super().tearDown()

    def req(self, method, path, body=None, who="dad"):
        h = {"Content-Type": "application/json", "X-Bunkr": "1", "Cookie": f"bunkr_session={self.cookies[who]}"}
        r = urllib.request.Request(self.hub.url + path, method=method, headers=h,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def login(self, user):
        r = urllib.request.Request(self.hub.url + "/api/login", method="POST",
                                   headers={"Content-Type": "application/json", "X-Bunkr": "1"},
                                   data=json.dumps({"username": user, "password": p3.PW}).encode())
        with urllib.request.urlopen(r) as resp:
            return resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def test_admin_api_and_sync_to_a_node(self):
        self.assertEqual(self.req("GET", "/api/depot/packs", who="kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/depot/packs", {"name": "X"}, who="kid")[0], 403)
        st, _, body = self.req("POST", "/api/depot/packs", {"name": "Car manuals"})
        self.assertEqual(st, 200)
        slug = json.loads(body)["slug"]
        st, _, body = self.req("POST", f"/api/depot/packs/{slug}/items", {"type": "url", "url": "http://files.example/a.txt"})
        self.assertEqual(st, 400)
        self.assertIn("https", json.loads(body)["error"])
        st, _, body = self.req("POST", f"/api/depot/packs/{slug}/items", {"type": "url", "url": "https://files.example/a.txt"})
        self.assertEqual(st, 200)
        self.assertEqual(self.req("POST", "/api/depot/packs/nope/items", {"type": "url", "url": "https://x.org/a"})[0], 404)
        st, hd, body = self.req("GET", f"/api/depot/packs/{slug}/export")
        self.assertEqual(st, 200)
        self.assertIn("attachment", hd["Content-Disposition"])
        shared = json.loads(body)
        self.assertEqual(shared["items"][0]["url"], "https://files.example/a.txt")
        st, _, body = self.req("POST", "/api/depot/packs/import", {"pack": shared | {"name": "From Grandma"}})
        self.assertEqual((st, json.loads(body)["slug"]), (200, "from-grandma"))
        self.assertEqual(self.req("POST", "/api/depot/packs/import", {"pack": shared | {"evil": 1}})[0], 400)
        self.assertEqual(self.req("POST", "/api/depot/packs/os", {"name": "Bad Name"})[0], 400)
        self.assertEqual(self.req("POST", "/api/depot/packs/settings", {"max_item_gb": 2})[0], 200)
        st, _, body = self.req("GET", "/api/depot/packs")
        view = json.loads(body)
        self.assertEqual([p["name"] for p in view["packs"]], ["Car manuals", "From Grandma"])
        self.assertEqual(view["settings"]["max_item_gb"], 2)
        self.assertIn("tesseract-ocr", view["os"]["builtin"])
        st, _, body = self.req("GET", "/api/nodes")
        self.assertEqual([p["slug"] for p in json.loads(body)["packs"]], ["car-manuals", "from-grandma"])
        self.assertEqual(self.req("DELETE", "/api/depot/packs/from-grandma")[0], 200)

        # fetch (with the test internet) and sync to a Pi that carries only this pack
        packs.refresh(self.hub.store, self.hub.depot, log=lambda *_: None, g=self.getter())
        w = Path(self.tmp.name)
        nid = nodes.save(self.hub.store, {"name": "Pi", "role": "pi", "profile": {"packs": [slug]}})
        code, _ = nodes.new_pair_code(self.hub.store, nid)
        sync.pair_node(self.hub.url, code, w / "pi.token")
        sync.run(w / "pi.token", w / "pi-store", w / "pi.db", log=lambda *_: None, records=False)
        got = w / "pi-store" / "packs" / slug / "a.txt"
        self.assertEqual(got.read_bytes(), b"hello pack")
        reported = json.loads(nodes.get(self.hub.store, nid)["manifest"])
        self.assertIn(f"packs/{slug}/a.txt", {f["path"] for f in reported["files"]})
        node = nodes.get(self.hub.store, nid)
        self.assertEqual(nodes.status(node, nodes.plan_files(server.node_plan(node)), "", 0)["behind"], 0)
        # deselect the pack: the node drops the files it got
        nodes.save(self.hub.store, {"profile": {"packs": False}}, nid)
        sync.run(w / "pi.token", w / "pi-store", w / "pi.db", log=lambda *_: None, records=False)
        self.assertFalse(got.exists())


if __name__ == "__main__":
    unittest.main()
