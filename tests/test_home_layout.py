"""Home tiles: each login's own layout, the family's starting layout, resets, and new tiles finding their place."""

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import audit, auth, homelayout  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402


class LayoutTests(unittest.TestCase):
    def test_clean_drops_unknown_and_repeats(self):
        lay = homelayout.clean({"order": ["news", "nope", "news", 3, "board"], "hidden": ["backup", "<script>"], "x": 1})
        self.assertEqual(lay, {"order": ["news", "board"], "hidden": ["backup"], "collapsed": []})
        self.assertEqual(homelayout.clean("junk"), {"order": [], "hidden": [], "collapsed": []})

    def test_new_tiles_go_where_the_code_puts_them(self):
        saved = [t for t in homelayout.TILE_IDS if t != "news"][::-1]  # someone's reversed layout from before "news" existed
        lay = homelayout.complete({"order": saved, "hidden": [], "collapsed": []})
        self.assertEqual(sorted(lay["order"]), sorted(homelayout.TILE_IDS))
        self.assertEqual([t for t in lay["order"] if t != "news"], saved)  # their order is kept
        self.assertEqual(lay["order"].index("news"), lay["order"].index("alerts") + 1)  # news lands right after alerts, as built in


class HomeHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        self.st = self.hub.store
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {u: self.login(u) for u in ("dad", "kid")}

    def tearDown(self):
        self.hub.close()

    def login(self, user):
        r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": user, "password": p3.PW}).encode(),
                                   method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
        with self.opener.open(r) as resp:
            return resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def req(self, method, path, body=None, user="dad"):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def full(self, *first):
        return list(first) + [t for t in homelayout.TILE_IDS if t not in first]

    def layout(self, user):
        return self.req("GET", "/api/state", user=user)[1]["home_layout"]

    def test_each_login_keeps_its_own(self):
        self.assertEqual(self.layout("kid")["order"], homelayout.TILE_IDS)
        self.assertFalse(self.layout("kid")["custom"])
        code, lay = self.req("POST", "/api/me/home", {"layout": {"order": self.full("news", "board"), "hidden": ["ready"], "collapsed": ["board"]}}, "kid")
        self.assertEqual(code, 200)
        self.assertEqual(lay["order"][:2], ["news", "board"])
        self.assertTrue(lay["custom"])
        self.assertEqual(self.layout("kid")["hidden"], ["ready"])
        self.assertEqual(self.layout("dad")["hidden"], [])  # dad's is untouched
        self.assertEqual(self.req("DELETE", "/api/me/home", user="kid")[1]["custom"], False)
        self.assertEqual(self.layout("kid")["hidden"], [])

    def test_admin_sets_the_starting_layout_and_resets(self):
        self.assertEqual(self.req("POST", "/api/home/default", {"layout": {"order": ["alerts"], "hidden": ["lookup"]}}, "kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/home/default", {"layout": {"order": self.full("alerts"), "hidden": ["lookup"]}})[0], 200)
        self.assertEqual(self.layout("kid")["order"][0], "alerts")
        self.assertEqual(self.layout("kid")["hidden"], ["lookup"])
        self.req("POST", "/api/me/home", {"layout": {"order": self.full("contacts")}}, "kid")
        self.assertEqual(self.layout("kid")["order"][0], "contacts")
        kid = auth.user_by_name(self.st, "kid")["id"]
        self.assertEqual(self.req("DELETE", f"/api/users/{kid}/home", user="kid")[0], 403)
        self.assertEqual(self.req("DELETE", f"/api/users/{kid}/home")[0], 200)
        self.assertEqual(self.layout("kid")["order"][0], "alerts")  # back to the family's

    def test_layout_saves_stay_out_of_the_activity_log(self):
        before = len(audit.rows(self.st, 500))
        self.req("POST", "/api/me/home", {"layout": {"collapsed": ["board"]}}, "kid")
        self.assertEqual(len(audit.rows(self.st, 500)), before)
        self.req("POST", "/api/home/default", {"layout": {"order": ["alerts"]}})
        self.assertEqual(audit.rows(self.st, 1)[0]["what"], "home")


if __name__ == "__main__":
    unittest.main()
