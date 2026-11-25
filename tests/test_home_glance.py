"""Home as a glance: what lives on Home, on My account and on the screens things moved to."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HTML = (ROOT / "bunkr" / "static" / "index.html").read_text()
JS = (ROOT / "bunkr" / "static" / "app.js").read_text()


def section(tab):
    m = re.search(rf'<section id="tab-{tab}".*?\n  </section>', HTML, re.S)
    assert m, tab
    return m.group(0)


class HomeGlanceTests(unittest.TestCase):
    def test_ids_are_unique(self):
        ids = re.findall(r'\bid="([^"]+)"', HTML)
        self.assertEqual(sorted({i for i in ids if ids.count(i) > 1}), [])

    def test_home_keeps_only_glance_tiles(self):
        home = section("home")
        tiles = re.findall(r'data-tile="(\w+)"', home)
        self.assertEqual(tiles, ["board", "alerts", "med", "lastcall", "backup", "hub"])
        for gone in ("push-prefs", "lookup-form", "home-weather", "home-news", "home-places", "home-contacts", "me-name"):
            self.assertNotIn(f'id="{gone}"', home)
        self.assertIn('id="home-chips"', home)
        self.assertIn('id="call-open"', home)  # Send a family call is a button on the Family card
        self.assertNotIn("Let the family know how you are", HTML)

    def test_moved_things_are_still_reachable(self):
        self.assertIn('id="home-weather"', section("situation"))
        comms = section("comms")
        for want in ("lookup-form", "home-places", "home-contacts"):
            self.assertIn(f'id="{want}"', comms)
        self.assertIn('id="home-ready"', section("readiness"))
        self.assertIn('id="call-form"', section("home"))  # the same form, opened from the Family card

    def test_my_account_holds_personal_settings(self):
        acct = section("account")
        for want in ("push-prefs", "mypw-form", "ts-status", "account-logout", "me-person-name", "me-person-call"):
            self.assertIn(f'id="{want}"', acct)
        house = section("household")
        for gone in ("mypw-form", "ts-status", "ts-add-passkey"):
            self.assertNotIn(f'id="{gone}"', house)
        self.assertLess(HTML.index('data-tab="account"'), HTML.index('data-tab="home"'))  # first in the menu

    def test_one_shared_help_helper(self):
        self.assertIn("function foldHelp(", JS)
        self.assertEqual(JS.count("function foldHelp("), 1)
        self.assertIn("foldHelp();", JS)


if __name__ == "__main__":
    unittest.main()
