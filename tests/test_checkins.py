"""Family check-ins, family calls, remote members, the simple view, the field page form and node sync."""

import ipaddress
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, checkins, server  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402


class StoreLevelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "a.db")

    def tearDown(self):
        self.tmp.cleanup()

    def person(self, name, role="adult", remote=0):
        import uuid
        return self.store._exec("INSERT INTO people(name, role, remote, uid, updated_at) VALUES(?,?,?,?,?)",
                                (name, role, remote, uuid.uuid4().hex, checkins.now()))

    def test_remote_people_do_not_count_toward_household(self):
        self.person("Alex"), self.person("Wife"), self.person("Kid", "child"), self.person("Mom", remote=1)
        s = self.store.settings()
        self.assertEqual((s["adults"], s["children"]), (2, 1))

    def test_board_call_and_answers(self):
        z = self.person("Alex")
        m = self.person("Mom", remote=1)
        uid = {r["id"]: r["uid"] for r in self.store._rows("SELECT id, uid FROM people")}
        checkins.add_checkin(self.store, uid[m], "safe", at="2020-01-01T00:00:00+00:00")
        with self.assertRaises(ValueError):
            checkins.start_call(self.store, "evacuate", "alex")  # needs a place
        call = checkins.start_call(self.store, "evacuate", "alex", place="Mom's", address="1 Main St")
        b = checkins.board(self.store)
        self.assertEqual(b["call"]["place"], "Mom's")
        self.assertEqual(b["waiting"], 2)  # Mom's old check-in predates the call
        checkins.add_checkin(self.store, uid[z], "moving", note="leaving now")
        b = checkins.board(self.store)
        alex = next(p for p in b["people"] if p["name"] == "Alex")
        self.assertTrue(alex["answered"])
        self.assertEqual(alex["last"]["call_uid"], call["uid"])
        self.assertEqual(b["waiting"], 1)
        # a newer call replaces the old one; all clear ends it
        checkins.start_call(self.store, "shelter", "alex")
        self.assertEqual(len(self.store._rows("SELECT * FROM family_calls WHERE ended_at=''")), 1)
        checkins.end_call(self.store, "alex")
        self.assertIsNone(checkins.board(self.store)["call"])
        with self.assertRaises(ValueError):
            checkins.add_checkin(self.store, uid[z], "bogus")

    def test_listeners_never_break_a_checkin(self):
        z = self.person("Alex")
        got = []
        checkins.listeners[:] = [lambda ev, d: (_ for _ in ()).throw(RuntimeError("boom")), lambda ev, d: got.append(ev)]
        try:
            u = self.store._rows("SELECT uid FROM people WHERE id=?", (z,))[0]["uid"]
            checkins.add_checkin(self.store, u, "help")
        finally:
            checkins.listeners[:] = []
        self.assertEqual(got, ["checkin"])

    def test_sync_between_hub_and_node(self):
        node = Store(Path(self.tmp.name) / "node.db")
        z = self.person("Alex")
        self.person("Mom", remote=1)
        zu = self.store._rows("SELECT uid FROM people WHERE id=?", (z,))[0]["uid"]
        call = checkins.start_call(self.store, "meet", "alex", place="Park")
        # hub -> node: roster and call
        n = checkins.apply(node, checkins.export(self.store, "", include_people=True), from_hub=True)
        self.assertGreaterEqual(n, 3)
        self.assertEqual(checkins.board(node)["call"]["uid"], call["uid"])
        self.assertEqual({p["name"] for p in checkins.board(node)["people"]}, {"Alex", "Mom"})
        # a check-in made on the Pi with the internet down reaches the hub later, once
        checkins.add_checkin(node, zu, "safe", via="field")
        out = checkins.export(node, "")
        self.assertEqual(checkins.apply(self.store, out), 1)
        self.assertEqual(checkins.apply(self.store, out), 0)  # re-sent: ignored
        self.assertEqual(checkins.board(self.store)["people"][0]["last"]["via"], "field")
        # all clear on the hub ends the call on the node
        checkins.end_call(self.store, "alex")
        checkins.apply(node, checkins.export(self.store, ""), from_hub=True)
        self.assertIsNone(checkins.board(node)["call"])
        # people from a node are never taken by the hub
        before = len(self.store._rows("SELECT * FROM people"))
        checkins.apply(self.store, {"people": [{"uid": "x", "name": "Intruder"}]})
        self.assertEqual(len(self.store._rows("SELECT * FROM people")), before)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        st = self.hub.store
        auth.create_user(st, "dad", p3.PW, "admin")
        auth.create_user(st, "mom", p3.PW, "family")
        auth.create_user(st, "kid", p3.PW, "family")
        auth.create_user(st, "guest", p3.PW, "family")
        self.site = st._exec("INSERT INTO sites(name, kind, address, lat, lon) VALUES('Grandma''s', 'relative', '9 Elm St', 39.7, -105.0)")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.cookies = {u: self.login(u) for u in ("dad", "mom", "kid", "guest")}
        users = {u["username"]: u["id"] for u in auth.users(st)}
        for name, role, login, extra in (("Alex", "adult", "dad", {}), ("Wife", "adult", "mom", {}), ("Kid", "child", "kid", {}),
                                         ("Grandma", "adult", None, {"remote": "on", "site_id": self.site})):
            body = {"name": name, "role": role, "phone": "iphone", **extra}
            if login:
                body["user_id"] = users[login]
            st_, _, b = self.req("POST", "/api/people", body, "dad")
            self.assertEqual(st_, 200, b)
        self.uids = {r["name"]: r["uid"] for r in st._rows("SELECT name, uid FROM people")}

    def tearDown(self):
        server.OPEN_NETS[:] = []
        self.hub.close()

    def req(self, method, path, body=None, user=None, form=None, headers=None):
        h = {"X-Bunkr": "1"} | (headers or {})
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
            h.pop("X-Bunkr")
        elif body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if user:
            h["Cookie"] = f"bunkr_session={self.cookies[user]}"
        r = urllib.request.Request(self.hub.url + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(r) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def login(self, user):
        st, hd, _ = self.req("POST", "/api/login", {"username": user, "password": p3.PW})
        return hd["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def state(self, user):
        return json.loads(self.req("GET", "/api/state", user=user)[2])

    def test_my_account_callsigns(self):
        """My account shows the linked person's callsigns, only their own."""
        self.assertEqual(self.state("mom")["me_person"]["callsigns"], [])
        pid = self.hub.store._rows("SELECT id FROM people WHERE name='Wife'")[0]["id"]
        for n, call in enumerate(["W1AW", "", "W1AW", "KAB1234"]):
            self.hub.store._exec("INSERT INTO licences(uid, person_id, kind, callsign) VALUES(?,?,?,?)", (f"lic{n}", pid, "ham", call))
        self.assertEqual(self.state("mom")["me_person"]["callsigns"], ["W1AW", "KAB1234"])
        self.assertNotIn("W1AW", json.dumps(self.state("dad")["me_person"] or {}))

    def test_household_and_views(self):
        s = self.state("dad")
        self.assertEqual((s["settings"]["adults"], s["settings"]["children"]), (2, 1))  # Grandma lives elsewhere
        self.assertEqual(s["view"], "full")
        self.assertEqual(self.state("mom")["view"], "simple")
        self.assertEqual(self.state("mom")["me_person"]["name"], "Wife")
        self.assertEqual(self.req("POST", "/api/me/view", {"view": "full"}, "mom")[0], 200)
        self.assertEqual(self.state("mom")["view"], "full")
        self.assertEqual(self.req("POST", "/api/me/view", {"view": "weird"}, "mom")[0], 400)
        mom_id = next(u["id"] for u in auth.users(self.hub.store) if u["username"] == "mom")
        self.assertEqual(self.req("POST", f"/api/users/{mom_id}", {"view": "simple"}, "dad")[0], 200)
        self.assertEqual(self.state("mom")["view"], "simple")
        self.assertEqual(self.req("POST", "/api/me/password", {"current": "x", "new": "y"}, "mom")[0], 400)  # still its own route

    def test_checkins_and_calls(self):
        self.assertEqual(self.req("POST", "/api/checkin", {"status": "safe", "note": "home"}, "mom")[0], 200)
        self.hub.store._exec("UPDATE checkins SET at='2020-01-01T00:00:00+00:00'")  # long before the call below
        self.assertEqual(self.req("POST", "/api/checkin", {"status": "safe"}, "guest")[0], 400)  # no person linked
        # a child can't send calls or check others in; an adult can
        self.assertEqual(self.req("POST", "/api/calls", {"kind": "shelter"}, "kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/checkin", {"person_uid": self.uids["Wife"], "status": "safe"}, "kid")[0], 403)
        st, _, b = self.req("POST", "/api/calls", {"kind": "evacuate", "site_id": self.site, "message": "Take 285"}, "mom")
        self.assertEqual(st, 200, b)
        call = json.loads(b)
        self.assertEqual((call["place"], call["address"], call["lat"]), ("Grandma's", "9 Elm St", 39.7))
        self.assertEqual(self.req("POST", "/api/calls", {"kind": "evacuate"}, "mom")[0], 400)  # where to?
        st, _, b = self.req("POST", "/api/checkin", {"person_uid": self.uids["Kid"], "status": "moving"}, "dad")
        self.assertEqual(json.loads(b)["by_name"], "dad")
        board = self.state("kid")["board"]
        self.assertEqual(board["call"]["kind"], "evacuate")
        waiting = {p["name"] for p in board["people"] if p["waiting"]}
        self.assertEqual(waiting, {"Alex", "Wife", "Grandma"})  # Wife's check-in came before the call
        self.assertEqual(self.req("POST", "/api/calls/end", {}, "dad")[0], 200)
        self.assertIsNone(json.loads(self.req("GET", "/api/board", user="kid")[2])["call"])

    def test_field_page_checkin(self):
        # off the field Wi-Fi and not logged in: no board, and the form sends you to log in
        st, _, page = self.req("GET", "/field")
        self.assertNotIn(b"Grandma", page)
        st, hd, _ = self.req("POST", "/field/checkin", form={"person_uid": self.uids["Kid"], "status": "safe"})
        self.assertEqual((st, hd["Location"]), (303, "/login.html"))
        # on the field Wi-Fi: the board shows and anyone can check in with one tap
        server.OPEN_NETS[:] = [ipaddress.ip_network("127.0.0.0/8")]
        page = self.req("GET", "/field")[2]
        self.assertIn(b"Grandma", page)
        self.assertIn(b'action="/field/checkin"', page)
        st, hd, _ = self.req("POST", "/field/checkin", form={"person_uid": self.uids["Kid"], "status": "help", "note": "ankle"})
        self.assertEqual((st, hd["Location"]), (303, "/field?sent=1#family"))
        last = self.hub.store._rows("SELECT * FROM checkins ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((last["status"], last["via"], last["note"]), ("help", "field", "ankle"))
        self.assertIn(b"Check-in sent", self.req("GET", "/field?sent=1")[2])
        # a form posted from another site is refused
        st, _, _ = self.req("POST", "/field/checkin", form={"person_uid": self.uids["Kid"], "status": "safe"},
                            headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        self.assertEqual(self.req("POST", "/field/checkin", form={"person_uid": "nobody", "status": "safe"})[0], 400)


if __name__ == "__main__":
    unittest.main()
