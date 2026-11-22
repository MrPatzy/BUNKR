"""Comms tab: licenses and their reminders, neighbors, nets on the calendar and the check-in log, ICS-213 text and
print page, sync to field nodes, the situation map pins, and the HTTP routes with who may change what."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, comms, family, mcp_tools  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

D0 = date(2026, 11, 2)  # a Monday


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        st = self.store = Store(Path(self.tmp.name) / "c.db")
        self.login = auth.create_user(st, "alex", p3.PW, "family")
        self.alex = st._exec("INSERT INTO people(name, role, uid, user_id) VALUES('Alex', 'adult', 'z', ?)", (self.login,))

    def tearDown(self):
        comms.listeners[:] = []
        self.tmp.cleanup()

    def test_licence_validation(self):
        lid = comms.save_licence(self.store, self.alex, {"kind": "ham", "class": "general", "callsign": " k0 abc"})
        row = comms.licences(self.store)[0]
        self.assertEqual((row["class"], row["callsign"], row["title"], row["status"]), ("General", "K0ABC", "Ham radio license (General)", None))
        for bad in ({"kind": "nope"}, {"kind": "ham", "class": "Advanced"}, {"kind": "other"}, {"kind": "cpr", "expires": "soon"},
                    {"kind": "gmrs", "callsign": "WR$123"}):
            with self.assertRaises(ValueError):
                comms.save_licence(self.store, self.alex, bad)
        with self.assertRaises(ValueError):
            comms.save_licence(self.store, 999, {"kind": "cpr"})
        comms.save_licence(self.store, self.alex, {"kind": "gmrs", "callsign": "WRXX123"}, lid)
        self.assertEqual(comms.licences(self.store)[0]["kind"], "gmrs")

    def test_reminders_soon_expired_done(self):
        sent = []
        comms.listeners.append(lambda ev, data: sent.append((ev, data)))
        lid = comms.save_licence(self.store, self.alex, {"kind": "cpr", "expires": (D0 + timedelta(days=90)).isoformat()})
        self.assertEqual(comms.remind(self.store, D0), [])  # 90 days out: later
        self.assertEqual(comms.licence_status(comms.licences(self.store)[0], D0)["state"], "later")
        day = D0 + timedelta(days=31)
        self.assertEqual(len(comms.remind(self.store, day)), 1)
        self.assertEqual(comms.remind(self.store, day + timedelta(days=1)), [])  # once while "soon"
        ev, msg = sent[0]
        self.assertEqual(ev, "reminder")
        self.assertEqual(msg["user_id"], self.login)  # the person's own login (admins get every reminder anyway)
        self.assertTrue(msg["title"].startswith("Renew soon: Alex's CPR"))
        self.assertEqual(comms.licence_calendar(self.store, day)[0]["kind"], "licence")
        late = D0 + timedelta(days=91)
        self.assertEqual(comms.licences_due(self.store, late)[0]["status"]["state"], "expired")
        self.assertEqual(len(comms.remind(self.store, late)), 1)
        self.assertEqual(comms.remind(self.store, late + timedelta(days=3)), [])
        self.assertEqual(len(comms.remind(self.store, late + timedelta(days=7))), 1)  # weekly once expired
        self.assertTrue(sent[-1][1]["title"].startswith("Expired:"))
        comms.mark_renewed(self.store, lid, late + timedelta(days=8))
        self.assertEqual(comms.licences_due(self.store, late + timedelta(days=8)), [])
        self.assertEqual(comms.remind(self.store, late + timedelta(days=14)), [])
        comms.save_licence(self.store, self.alex, {"kind": "cpr", "expires": (late + timedelta(days=30)).isoformat()}, lid)
        self.assertEqual(comms.licences_due(self.store, late + timedelta(days=8))[0]["status"]["state"], "soon")  # new date: again

    def test_neighbours(self):
        nid = comms.save_neighbour(self.store, {"name": "Jordan", "place": "Next door", "lat": "39.6", "lon": "-105.3",
                                                "skills": ["ham", "chainsaw", "bogus", "ham"], "skills_other": "Beekeeper"})
        n = comms.neighbours(self.store)[0]
        self.assertEqual(n["skills"], ["ham", "chainsaw"])
        self.assertEqual(n["skill_labels"], ["Ham radio", "Chainsaw / trees", "Beekeeper"])
        f = comms.neighbour_features(self.store)
        self.assertEqual((f[0]["geometry"]["coordinates"], f[0]["properties"]["kind"]), ([-105.3, 39.6], "neighbour"))
        with self.assertRaises(ValueError):
            comms.save_neighbour(self.store, {"name": "X", "lat": "39"})
        with self.assertRaises(ValueError):
            comms.save_neighbour(self.store, {"name": "X", "lat": "100", "lon": "1"})
        with self.assertRaises(ValueError):
            comms.save_neighbour(self.store, {"name": ""})
        comms.save_neighbour(self.store, {"name": "Jordan", "lat": "", "lon": ""}, nid)
        self.assertEqual(comms.neighbour_features(self.store), [])

    def test_neighbours_stay_out_of_the_connector(self):
        comms.save_neighbour(self.store, {"name": "Jordan Neighbour", "phone": "555-0100"})
        src = Path(mcp_tools.__file__).read_text()
        self.assertNotIn("neighbour", src.lower())
        self.assertNotIn("neighbor", src.lower())

    def test_nets_calendar_and_log(self):
        nid = comms.save_net(self.store, {"name": "ARES", "freq": "146.940", "weekday": "1", "time": "19:30", "net_control": "K0NC"})
        comms.save_net(self.store, {"name": "Family", "freq": "GMRS 20", "weekday": "-1", "time": "08:00"})
        comms.save_net(self.store, {"name": "Old", "weekday": "3", "active": False})
        cal = comms.calendar_entries(self.store, D0)
        self.assertEqual([(c["title"], c["due"]) for c in cal],
                         [("Net: Family (GMRS 20) at 08:00", "2026-11-02"), ("Net: ARES (146.940) at 19:30", "2026-11-03")])
        self.assertEqual(comms.next_net({"weekday": 0}, D0), D0)  # today counts
        whole = family.calendar([], [], [], today=D0, renewals=cal)
        self.assertEqual(whole[0]["title"], "Net: Family (GMRS 20) at 08:00")
        for bad in ({"name": ""}, {"name": "x", "weekday": 7}, {"name": "x", "time": "7pm"}):
            with self.assertRaises(ValueError):
                comms.save_net(self.store, bad)
        comms.log_checkin(self.store, {"net_id": str(nid), "who": "KD0XYZ", "signal": "59", "at": "2026-11-03T19:35"}, "alex")
        comms.log_checkin(self.store, {"net_name": "Simplex test", "who": "Mom"})
        log = comms.net_log(self.store)
        self.assertEqual({l["net_name"] for l in log}, {"ARES", "Simplex test"})
        with self.assertRaises(ValueError):
            comms.log_checkin(self.store, {"who": "x"})
        with self.assertRaises(ValueError):
            comms.log_checkin(self.store, {"net_id": nid, "who": ""})
        self.store._exec("DELETE FROM nets WHERE id=?", (nid,))
        self.assertIn("ARES", {l["net_name"] for l in comms.net_log(self.store)})  # the log keeps the name

    def test_ics213_text_and_print(self):
        m = {"incident": "Winter storm", "to_name": "Jordan", "to_position": "Neighbor", "from_name": "Alex", "from_position": "",
             "subject": "Road closed", "msg_date": "2026-11-22", "msg_time": "19:30",
             "message": "Main St closed at the bridge — use the north side.\r\nCafé open. “Stay safe” \U0001F600",
             "approved_name": "Alex", "approved_position": "Head of household", "reply": "Copy, thanks", "reply_name": "Jordan",
             "reply_position": "", "reply_at": "2026-11-22 1940"}
        text = comms.ics213_text(m)
        self.assertEqual(text, "GENERAL MESSAGE (ICS 213)\n1. INCIDENT: Winter storm\n2. TO: Jordan, Neighbor\n3. FROM: Alex\n"
                               "4. SUBJECT: Road closed\n5. DATE: 2026-11-22\n6. TIME: 1930\n7. MESSAGE:\n"
                               "Main St closed at the bridge - use the north side.\nCafe open. \"Stay safe\" \n"
                               "8. APPROVED BY: Alex, Head of household\nREPLY:\nCopy, thanks\n"
                               "REPLIED BY: Jordan DATE/TIME: 2026-11-22 1940\n")
        text.encode("ascii")  # plain ASCII for JS8Call and Winlink
        mid = comms.save_ics213(self.store, m | {"message": "<b>hi</b>"}, "alex")
        row = comms.messages(self.store)[0]
        self.assertEqual((row["id"], row["created_by"], row["direction"]), (mid, "alex", "sent"))
        page = comms.ics213_html(row)
        for label in ("GENERAL MESSAGE (ICS 213)", "1. Incident Name", "2. To (Name and Position)", "3. From (Name and Position)",
                      "4. Subject", "5. Date", "6. Time", "7. Message", "8. Approved by", "9. Reply", "10. Replied by"):
            self.assertIn(label, page)
        self.assertIn("&lt;b&gt;hi&lt;/b&gt;", page)
        with self.assertRaises(ValueError):
            comms.save_ics213(self.store, {"to_name": "a", "from_name": "b"})
        with self.assertRaises(ValueError):
            comms.save_ics213(self.store, {"message": "x"})
        short = comms.ics213_text({"to_name": "A", "from_name": "B", "message": "Hi"})
        self.assertNotIn("INCIDENT", short)
        self.assertNotIn("APPROVED", short)

    def test_sync_to_a_node(self):
        comms.save_neighbour(self.store, {"name": "Jordan", "lat": 39.6, "lon": -105.3, "skills": ["ham"]})
        comms.save_net(self.store, {"name": "ARES", "weekday": 1})
        comms.save_licence(self.store, self.alex, {"kind": "ham", "callsign": "K0ABC", "expires": "2030-01-01"})
        out = comms.export(self.store)
        node = Store(Path(self.tmp.name) / "node.db")
        node._exec("INSERT INTO people(name, role, uid) VALUES('Alex', 'adult', 'z')")
        node._exec("INSERT INTO neighbours(uid, name) VALUES('gone', 'Deleted on the hub')")
        self.assertEqual(comms.apply(node, out), 3)
        self.assertEqual([n["name"] for n in comms.neighbours(node)], ["Jordan"])
        self.assertEqual(comms.neighbours(node)[0]["lat"], 39.6)
        self.assertEqual(comms.licences(node)[0]["callsign"], "K0ABC")
        self.assertEqual(comms.nets(node)[0]["weekday"], 1)
        self.assertEqual(comms.apply(node, {}), 0)  # an older hub sends nothing: nothing changes
        self.assertEqual(len(comms.neighbours(node)), 1)


class CommsHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        st = self.hub.store
        auth.create_user(st, "dad", p3.PW, "admin")
        kid = auth.create_user(st, "kid", p3.PW, "family")
        self.kid = st._exec("INSERT INTO people(name, role, uid, user_id) VALUES('Kid', 'child', 'k', ?)", (kid,))
        self.wife = st._exec("INSERT INTO people(name, role, uid) VALUES('Wife', 'adult', 'w')")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {}
        for u in ("dad", "kid"):
            r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": u, "password": p3.PW}).encode(),
                                       method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
            with self.opener.open(r) as resp:
                self.c[u] = resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def tearDown(self):
        self.hub.close()

    def req(self, method, path, body=None, user="dad", raw=False):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                data = resp.read()
                return resp.status, data.decode() if raw else json.loads(data)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_routes_and_permissions(self):
        soon = (date.today() + timedelta(days=10)).isoformat()
        self.assertEqual(self.req("POST", "/api/licences", {"person_id": self.wife, "kind": "cpr", "expires": soon})[0], 200)
        self.assertEqual(self.req("POST", "/api/licences", {"person_id": self.wife, "kind": "cpr"}, user="kid")[0], 403)
        st, r = self.req("POST", "/api/licences", {"person_id": self.kid, "kind": "first_aid"}, user="kid")  # their own
        self.assertEqual(st, 200)
        self.assertEqual(self.req("POST", f"/api/licences/{r['id']}/renewed", user="kid")[0], 200)
        self.assertEqual(self.req("POST", "/api/licences", {"person_id": self.wife, "kind": "zzz"})[0], 400)

        st, r = self.req("POST", "/api/neighbours", {"name": "Jordan", "lat": 39.6, "lon": -105.3, "skills": ["ham"], "phone": "555"})
        self.assertEqual(st, 200)
        nid = r["id"]
        self.assertEqual(self.req("POST", "/api/neighbours", {"name": "X"}, user="kid")[0], 403)
        self.assertEqual(self.req("DELETE", f"/api/neighbours/{nid}", user="kid")[0], 403)
        st, r = self.req("POST", "/api/nets", {"name": "ARES", "freq": "146.940", "weekday": 2, "time": "19:00"})
        net = r["id"]
        self.assertEqual(self.req("POST", "/api/nets", {"name": "x"}, user="kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/net-log", {"net_id": net, "who": "KD0XYZ", "signal": "59"}, user="kid")[0], 200)  # anyone logs
        st, r = self.req("POST", "/api/ics213", {"to_name": "Jordan", "from_name": "Dad", "subject": "Water", "message": "20 gal spare"})
        mid = r["id"]
        self.assertEqual(self.req("POST", "/api/ics213", {"to_name": "a", "from_name": "b", "message": "c"}, user="kid")[0], 403)

        st, v = self.req("GET", "/api/comms", user="kid")
        self.assertEqual(st, 200)
        self.assertFalse(v["can_edit"])
        self.assertEqual(v["me"], self.kid)
        self.assertEqual(v["neighbours"][0]["name"], "Jordan")  # family visible
        self.assertEqual(v["net_log"][0]["who"], "KD0XYZ")
        self.assertTrue(v["messages"][0]["text"].startswith("GENERAL MESSAGE (ICS 213)"))
        self.assertEqual([d["kind"] for d in v["due"]], ["cpr"])
        st, page = self.req("GET", f"/api/ics213/{mid}/print.html", user="kid", raw=True)
        self.assertIn("20 gal spare", page)
        self.assertEqual(self.req("GET", "/api/ics213/999/print.html")[0], 404)

        cal = self.req("GET", "/api/family", user="kid")[1]["calendar"]
        self.assertTrue({"net", "licence"} <= {c["kind"] for c in cal})
        sit = self.req("GET", "/api/situation", user="kid")[1]
        self.assertEqual(sit["neighbours"][0]["name"], "Jordan")
        geo = self.req("GET", "/api/situation/geo", user="kid")[1]
        self.assertIn("neighbour", {f["properties"]["kind"] for f in geo["features"]})

        self.assertEqual(self.req("DELETE", f"/api/ics213/{mid}")[0], 200)
        self.assertEqual(self.req("DELETE", f"/api/ics213/{mid}")[0], 404)
        self.assertEqual(self.req("DELETE", f"/api/neighbours/{nid}")[0], 200)

    def test_field_node_edits_go_to_the_hub(self):
        from bunkr import server
        server.ROLE = "pi"
        try:
            st, r = self.req("POST", "/api/neighbours", {"name": "Jordan"})
            self.assertEqual(st, 400)
            self.assertIn("hub", r["error"])
            self.assertEqual(self.req("POST", "/api/net-log", {"net_name": "Simplex", "who": "Dad"})[0], 200)  # logs work anywhere
        finally:
            server.ROLE = "hub"


if __name__ == "__main__":
    unittest.main()
