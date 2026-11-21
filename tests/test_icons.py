"""App icons and the web-app manifest load without a login, so a phone can install BUNKR."""

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import test_phase3 as p3  # noqa: E402


class IconTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        self.opener = urllib.request.build_opener(p3.NoRedirect)

    def tearDown(self):
        self.hub.close()

    def get(self, path):
        try:
            with self.opener.open(self.hub.url + path) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def test_manifest_and_icons_are_public(self):
        st, ctype, body = self.get("/manifest.webmanifest")
        self.assertEqual(st, 200)
        self.assertIn("manifest+json", ctype)
        man = json.loads(body)
        self.assertEqual(man["display"], "standalone")
        purposes = set()
        for icon in man["icons"]:
            st, ctype, data = self.get(icon["src"])
            self.assertEqual((st, ctype), (200, "image/png"), icon["src"])
            self.assertTrue(data.startswith(b"\x89PNG"))
            purposes.add(icon["purpose"])
        self.assertEqual(purposes, {"any", "maskable"})
        for path in ("/icons/apple-touch-icon.png", "/favicon.ico", "/logo.jpg", "/icons/mark-96.png"):
            self.assertEqual(self.get(path)[0], 200, path)

    def test_pages_link_the_icons_and_the_rest_stays_private(self):
        _, _, page = self.get("/login.html")
        for tag in (b'rel="manifest"', b'rel="apple-touch-icon"', b'name="theme-color"'):
            self.assertIn(tag, page)
        self.assertEqual(self.get("/")[0], 303)
        self.assertEqual(self.get("/app.js")[0], 303)
        self.assertEqual(self.get("/icons/../app.js")[0], 303)
        self.assertEqual(self.get("/icons/missing.png")[0], 404)


if __name__ == "__main__":
    unittest.main()
