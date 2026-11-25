"""QR codes for bin labels, and the label sheet.

The fingerprints below are of codes that matched the `qrcode` library module for module (every version 1-10 with
every mask was compared when bunkr/qr.py was written, and a printed sheet was read back with zxing-cpp)."""

import hashlib
import unittest

from bunkr import export, qr


def fingerprint(m):
    return hashlib.sha256("".join("".join(map(str, r)) for r in m).encode()).hexdigest()[:16]


class QRTests(unittest.TestCase):
    def test_matches_the_reference(self):
        for text, version, mask, want in (
                ("https://bunkr.example.org/#bin-0123456789abcdef0123456789abcdef", 5, 2, "d58a9ec365d9df6d"),
                ("hello", 1, 0, "99ccedcf0d82e92a"),
                ("y" * 120, 7, 5, "c5efbb0e9e388d2e"),         # versions 7+ carry version information too
                ("v" * 210, 10, 3, "ba38422c77b26af8")):        # several blocks of two sizes, interleaved
            self.assertEqual(fingerprint(qr.matrix(text, mask=mask, version=version)), want, (version, mask))

    def test_smallest_version_and_limits(self):
        self.assertEqual(len(qr.matrix("hello")), 21)                                   # version 1
        self.assertEqual(len(qr.matrix("https://bunkr.example.org/#bin-" + "0" * 32)), 37)   # version 5
        with self.assertRaises(ValueError):
            qr.matrix("x" * 300)
        s = qr.svg("hello")
        self.assertTrue(s.startswith("<svg") and 'fill="#fff"' in s)

    def test_label_sheet(self):
        locs = [{"id": 1, "uid": "u1", "name": "Basement"}, {"id": 2, "uid": "u2", "name": "Basement > Shelf A"}]
        items = [{"location_id": 2, "location": "Basement > Shelf A", "expires": "2099-01-01"},
                 {"location_id": 1, "location": "Basement", "expires": ""}]
        page = export.labels_html(locs, items, "https://bunkr.example.org/")
        self.assertEqual(page.count("<svg"), 2)
        self.assertIn("2 items", page)                       # the basement counts what's on its shelf
        self.assertIn("next expiry 2099-01-01", page)
        self.assertIn('<div class=path>Basement</div>', page)


if __name__ == "__main__":
    unittest.main()
