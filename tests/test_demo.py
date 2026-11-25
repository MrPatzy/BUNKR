"""The demo family: it fills a new database once, needs no network, and every screen has something to show."""

import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from bunkr import checkins, db, demo, flights, gaps


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = db.Store(str(Path(self.tmp.name) / "bunkr.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_fills_once_with_no_network(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("the demo must not fetch anything")):
            self.assertTrue(demo.fill(self.st))
        self.assertFalse(demo.fill(self.st))                     # a second start leaves it alone
        self.assertTrue(self.st.get_raw("demo"))

    def test_every_screen_has_something(self):
        demo.fill(self.st)
        names = [p["name"] for p in checkins.people(self.st)]
        self.assertIn("Grandma Rosa", names)
        board = checkins.board(self.st)
        self.assertTrue(any(p["last"] for p in board["people"]))
        self.assertEqual(flights.board(self.st)[0]["status"], "airborne")
        locs = {l["name"]: l for l in self.st.locations()}
        self.assertEqual(locs["Basement > Shelf A"]["depth"], 1)  # nested places, so the tree shows
        a = gaps.analyze(self.st.settings(), self.st.items(), self.st.documents())
        self.assertGreater(a["water_days"], 0)
        self.assertGreater(a["food_days"], 0)
        self.assertGreaterEqual(self.st._rows("SELECT COUNT(*) AS n FROM news_stories")[0]["n"], 3)
        self.assertEqual(self.st._rows("SELECT COUNT(*) AS n FROM users WHERE username='alex'")[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
