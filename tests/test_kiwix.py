"""Offline library: Kiwix catalog picks, the reader BUNKR runs on the hub, search, and the library's own address."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, kiwix, library, server  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

OPDS = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><title>Gardening &amp; Landscaping</title><updated>2026-01-01T00:00:00Z</updated><summary>Q&amp;A for gardeners</summary>
  <name>gardening.stackexchange.com_en_all</name><flavour></flavour><category>stack_exchange</category>
  <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim" length="933097472"
        href="https://download.kiwix.org/zim/stack_exchange/gardening.stackexchange.com_en_all_2026-01.zim.meta4"/></entry>
 <entry><title>Gardening &amp; Landscaping</title><updated>2025-06-01T00:00:00Z</updated><summary>older</summary>
  <name>gardening.stackexchange.com_en_all</name><flavour></flavour>
  <link type="application/x-zim" length="900000000" href="https://download.kiwix.org/zim/x/gardening.stackexchange.com_en_all_2025-06.zim.meta4"/></entry>
 <entry><title>Permaculture</title><updated>2026-02-01T00:00:00Z</updated><summary>Growing food</summary>
  <name>permaculture_en_all</name>
  <link type="application/x-zim" length="12000000" href="https://download.kiwix.org/zim/other/permaculture_en_all_maxi_2026-02.zim.meta4"/></entry>
</feed>"""

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>Boiling water</title><link>/kiwix/content/water/boil</link>
  <description>Search × Loading… Bring <b>water</b> to a rolling boil for one minute</description>
  <book><title>Water Treatment Library</title></book></item>
<item><title>Bad link</title><link>javascript:alert(1)</link><description>x</description></item>
</channel></rss>"""


class FakeKiwix(BaseHTTPRequestHandler):
    """Stands in for kiwix-serve --urlRootLocation /kiwix."""
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeKiwix.seen.append((self.path, self.headers.get("Range"), self.headers.get("Cookie")))
        if self.path.startswith("/kiwix/search"):
            body, ctype = RSS.encode(), "application/rss+xml"
        elif self.path.startswith("/kiwix/catalog"):
            body, ctype = b"<feed xmlns='http://www.w3.org/2005/Atom'/>", "application/atom+xml"
        elif self.path == "/kiwix/content/water/boil":
            body, ctype = b"<html><script>fetch('/api/state')</script>Boil it</html>", "text/html"
            if self.headers.get("Range") == "bytes=0-3":
                self.send_response(206)
                self.send_header("Content-Range", "bytes 0-3/52")
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(body[:4])
                return
        elif self.path == "/kiwix/content/water":
            self.send_response(302)
            self.send_header("Location", "/kiwix/content/water/boil")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "default-src 'self'; sandbox allow-scripts allow-same-origin")
        self.send_header("Set-Cookie", "userlang=en;Path=/kiwix")
        self.end_headers()
        self.wfile.write(body)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.real = library._get
        library._get = lambda url, timeout=30: OPDS

    def tearDown(self):
        library._get = self.real
        library._cache.clear()

    def test_search_keeps_newest_of_each(self):
        got = library.search_catalog("garden")
        self.assertEqual(len(got), 2)
        g = next(r for r in got if r["name"].startswith("gardening"))
        self.assertEqual((g["date"], g["size"], g["title"]), ("2026-01-01", 933097472, "Gardening & Landscaping"))
        p = next(r for r in got if r["name"] == "permaculture_en_all")
        self.assertEqual(p["flavour"], "maxi")  # read from the file name when the feed leaves it out
        self.assertEqual(library.search_catalog("g"), [])

    def test_extra_entries(self):
        e = library.extra_entry("permaculture_en_all", "maxi", "Permaculture", "Growing food", 12000000)
        self.assertEqual((e["id"], e["tier"], e["approx"], e["why"]), ("x_permaculture_en_all_maxi", 3, 12000000, "Permaculture: Growing food"))
        for bad in ("../etc", "a b", ""):
            with self.assertRaises(ValueError):
                library.extra_entry(bad, "")
        real = library.extra_source
        try:
            library.extra_source = lambda: [e, {"id": "water"}]  # a pick can't shadow a curated id
            ids = [x["id"] for x in library.entries()]
            self.assertEqual(ids[-1], e["id"])
            self.assertEqual(ids.count("water"), 1)
        finally:
            library.extra_source = real

    def test_new_curated_books(self):
        ids = {c["id"] for c in library.CURATED}
        self.assertTrue({"se_outdoors", "se_cooking", "se_ham", "appropedia", "se_diy"} <= ids)
        self.assertEqual(len(ids), len(library.CURATED))


class ManagerTests(unittest.TestCase):
    def test_build_library_skips_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "lib").mkdir()
            for n in ("a.zim", "bad.zim", "c.zim"):
                (d / "lib" / n).write_bytes(b"x")
            manage = d / "manage"
            manage.write_text("#!/bin/sh\ncase \"$3\" in *bad.zim) exit 1;; esac\necho \"$3\" >> \"$1\"\n")
            manage.chmod(0o755)
            count, skipped = kiwix.build_library(d / "lib", d / "state", str(manage))
            self.assertEqual((count, skipped), (2, ["bad.zim"]))
            self.assertEqual(len((d / "state" / "library.xml").read_text().split()), 2)
            for n in ("a.zim", "c.zim"):
                (d / "lib" / n).unlink()
            self.assertEqual(kiwix.build_library(d / "lib", d / "state", str(manage))[0], 0)
            self.assertFalse((d / "state" / "library.xml").exists())

    def test_refresh_starts_and_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "lib").mkdir()
            manage = d / "manage"
            manage.write_text("#!/bin/sh\necho \"$3\" >> \"$1\"\n")
            manage.chmod(0o755)
            # a stand-in kiwix-serve: answers the catalog on the port it's given
            serve = d / "serve"
            serve.write_text(f"#!{sys.executable}\n" + """import sys, http.server
port = int(sys.argv[sys.argv.index('--port') + 1])
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200 if self.path.startswith('/kiwix/catalog') else 404); self.end_headers(); self.wfile.write(b'ok')
http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()
""")
            serve.chmod(0o755)
            with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as probe:
                port = probe.server_address[1]
            m = kiwix.Manager(d / "lib", d / "state", port, str(serve), str(manage))
            self.assertFalse(m.refresh())  # no books: nothing to run
            self.assertFalse(m.running())
            (d / "lib" / "water.zim").write_bytes(b"x")
            self.assertTrue(m.refresh(), m.error)
            first = m.proc.pid
            self.assertTrue(kiwix.alive(f"http://127.0.0.1:{port}"))
            self.assertTrue(m.refresh(), m.error)
            self.assertNotEqual(m.proc.pid, first)
            self.assertEqual(m.status()["books"], 1)
            m.stop()
            self.assertFalse(m.running())


class LibraryHttpTests(unittest.TestCase):
    def setUp(self):
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), FakeKiwix)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.saved = dict(server.KIWIX)
        server.KIWIX.update(url=f"http://127.0.0.1:{self.up.server_address[1]}", manager=None)
        server._kiwix_seen[:] = [0.0, False]
        FakeKiwix.seen.clear()
        self.real_get = library._get
        library._get = lambda url, timeout=30: OPDS
        self.hub = p3.Hub()
        auth.create_user(self.hub.store, "dad", p3.PW, "admin")
        auth.create_user(self.hub.store, "kid", p3.PW, "family")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {}
        for u in ("dad", "kid"):
            r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": u, "password": p3.PW}).encode(),
                                       method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
            with self.opener.open(r) as resp:
                self.c[u] = resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def tearDown(self):
        self.hub.close()
        self.up.shutdown()
        self.up.server_close()
        server.KIWIX.clear()
        server.KIWIX.update(self.saved)
        server._kiwix_seen[:] = [0.0, False]
        library._get = self.real_get
        library._cache.clear()

    def raw(self, path, user="kid", headers=None):
        h = dict(headers or {})
        if user:
            h["Cookie"] = f"bunkr_session={self.c[user]}"
        try:
            return self.opener.open(urllib.request.Request(self.hub.url + path, headers=h))
        except urllib.error.HTTPError as e:
            return e

    def req(self, method, path, body=None, user="dad"):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_library_has_its_own_address(self):
        r = self.raw("/kiwix/content/water/boil", headers={"Host": "192.168.1.40:8765"})
        self.assertEqual((r.status, r.headers["Location"]), (303, "http://192.168.1.40:8080/kiwix/content/water/boil"))
        self.assertEqual(self.raw("/kiwix/", headers={"Host": "bunkr.local:8765"}).headers["Location"], "http://bunkr.local:8080/kiwix/")
        server.ALLOWED_HOSTS.add("192.168.1.40")
        try:
            # through the public hostname: the home address, marked home-only
            self.assertEqual(server.library_link("bunkr.example.org"), {"url": "http://192.168.1.40:8080/kiwix/", "home_only": True})
            self.assertEqual(self.req("POST", "/api/library/address", {"url": "javascript:alert(1)"})[0], 400)
            self.assertEqual(self.req("POST", "/api/library/address", {"url": "https://library.example.org/"}, user="kid")[0], 403)
            self.assertEqual(self.req("POST", "/api/library/address", {"url": "https://library.example.org/"})[0], 200)
            self.assertEqual(server.library_link("bunkr.example.org"), {"url": "https://library.example.org/kiwix/", "home_only": False})
            self.assertEqual(self.raw("/kiwix/content/x", headers={"Host": "bunkr.example.org"}).headers["Location"],
                             "https://library.example.org/kiwix/content/x")
            lib = self.req("GET", "/api/state", user="kid")[1]["library"]
            self.assertEqual((lib["address"], lib["port"]), ("https://library.example.org/", 8080))
        finally:
            server.ALLOWED_HOSTS.discard("192.168.1.40")

    def test_reader_down(self):
        server.KIWIX["url"] = "http://127.0.0.1:9"
        self.assertEqual(self.req("GET", "/api/kiwix/search?q=water", user="kid")[0], 503)
        self.assertFalse(self.req("GET", "/api/state", user="kid")[1]["library"]["ready"])

    def test_search_and_home(self):
        st, r = self.req("GET", "/api/kiwix/search?q=boil%20water", user="kid")
        self.assertEqual(st, 200)
        self.assertEqual(r["results"], [{"title": "Boiling water", "path": "/kiwix/content/water/boil",
                                         "book": "Water Treatment Library", "snippet": "Bring water to a rolling boil for one minute"}])
        self.assertIn("pattern=boil+water", FakeKiwix.seen[-1][0])
        self.assertEqual(self.req("GET", "/api/kiwix/search?q=", user="kid")[1]["results"], [])
        self.assertTrue(self.req("GET", "/api/state", user="kid")[1]["library"]["ready"])

    def test_catalog_picks(self):
        self.assertEqual(self.req("GET", "/api/library/catalog?q=garden", user="kid")[0], 403)
        st, r = self.req("GET", "/api/library/catalog?q=garden")
        by = {x["name"]: x for x in r["results"]}
        self.assertTrue(by["gardening.stackexchange.com_en_all"]["added"])  # curated now
        self.assertFalse(by["permaculture_en_all"]["added"])
        pick = by["permaculture_en_all"]
        self.assertEqual(self.req("POST", "/api/library/custom", pick)[0], 200)
        self.assertEqual(self.req("POST", "/api/library/custom", pick)[0], 400)  # already on the list
        self.assertEqual(self.req("POST", "/api/library/custom", {"name": "x y"})[0], 400)
        self.assertEqual(self.req("POST", "/api/library/custom", pick, user="kid")[0], 403)
        self.assertIn("x_permaculture_en_all_maxi", server.catalog_meta())
        self.assertTrue(self.req("GET", "/api/library/catalog?q=garden")[1]["results"][1]["added"])
        self.assertEqual(self.req("DELETE", "/api/library/custom/x_permaculture_en_all_maxi")[0], 200)
        self.assertEqual(library.entries(), library.CURATED)


if __name__ == "__main__":
    unittest.main()
