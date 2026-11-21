"""Medical: supply countdown, refills, go-bag expiry, reminders once per stage, cards, notifications and the API."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, medical, notify  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

D0 = date(2026, 11, 1)


class SupplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        st = self.store = Store(Path(self.tmp.name) / "m.db")
        self.wife_login = auth.create_user(st, "wife", p3.PW, "family")
        self.wife = st._exec("INSERT INTO people(name, role, uid, user_id, allergies) VALUES('Wife', 'adult', 'w', ?, 'Penicillin')",
                             (self.wife_login,))

    def tearDown(self):
        medical.listeners[:] = []
        self.tmp.cleanup()

    def add(self, **kw):
        base = {"name": "Lisinopril", "dose": "10 mg", "per_day": 1, "on_hand": 30, "on_hand_at": D0.isoformat()}
        return medical.save(self.store, self.wife, base | kw)

    def med(self, mid):
        return self.store._rows("SELECT * FROM meds WHERE id=?", (mid,))[0]

    def test_countdown_and_refill(self):
        mid = self.add()
        st = medical.status(self.med(mid), D0 + timedelta(days=10))
        self.assertEqual((st["days_left"], st["todo"]), (20, []))
        self.assertEqual(st["refill_by"], (D0 + timedelta(days=23)).isoformat())
        st = medical.status(self.med(mid), D0 + timedelta(days=25))
        self.assertEqual((st["days_left"], st["todo"][0]["kind"]), (5, "refill"))
        self.assertEqual(medical.status(self.med(mid), D0 + timedelta(days=40))["todo"][0]["kind"], "out")
        medical.refilled(self.store, mid, 90, today=D0 + timedelta(days=25))
        st = medical.status(self.med(mid), D0 + timedelta(days=25))
        self.assertEqual((st["days_left"], st["todo"]), (95, []))

    def test_as_needed_bag_and_validation(self):
        mid = self.add(name="EpiPen", per_day=0, on_hand=2, critical=True, bag_count=0, refill_by=(D0 + timedelta(days=3)).isoformat())
        kinds = [t["kind"] for t in medical.status(self.med(mid), D0)["todo"]]
        self.assertEqual(kinds, ["refill", "bag_missing"])  # no countdown: the date set by hand; essential and not in the bag
        mid2 = self.add(name="Ibuprofen", per_day=0, on_hand=0, bag_count=20, bag_expires=(D0 + timedelta(days=20)).isoformat())
        self.assertEqual([t["kind"] for t in medical.status(self.med(mid2), D0)["todo"]], ["bag"])
        with self.assertRaises(ValueError):
            medical.save(self.store, self.wife, {"name": " "})
        with self.assertRaises(ValueError):
            medical.save(self.store, 999, {"name": "x"})
        self.assertEqual(medical.clean({"name": "x", "per_day": "-3", "on_hand": "abc"})["per_day"], 0)

    def test_reminders_once_per_stage_and_again_after_refill(self):
        mid = self.add()
        heard = []
        medical.listeners[:] = [lambda ev, d: heard.append(d)]
        day = D0 + timedelta(days=25)
        self.assertEqual(len(medical.remind(self.store, day)), 1)
        self.assertEqual(heard[0]["user_id"], self.wife_login)
        self.assertIn("Wife: Refill Lisinopril: 5 days left", heard[0]["title"])
        self.assertEqual(medical.remind(self.store, day + timedelta(days=1)), [])  # said once
        self.assertEqual(medical.remind(self.store, D0 + timedelta(days=31))[0]["kind"], "out")  # a new stage is new
        medical.refilled(self.store, mid, 30, today=D0 + timedelta(days=31))
        self.assertEqual(medical.remind(self.store, D0 + timedelta(days=32)), [])
        self.assertEqual(len(medical.remind(self.store, D0 + timedelta(days=55))), 1)  # runs low again: reminds again

    def test_cards(self):
        self.add(critical=True, cold=True)
        self.store._exec("UPDATE people SET devices='CPAP' WHERE id=?", (self.wife,))
        page = medical.cards_html(medical.people_with_meds(self.store), [{"name": "Mom", "phone": "555", "out_of_area": 1}])
        for want in ("Wife", "Penicillin", "Lisinopril 10 mg", "keep cold", "essential", "CPAP", "Mom 555"):
            self.assertIn(want, page)
        self.assertNotIn("<script", medical.cards_html([{"name": "<script>x", "meds": []}], []))

    def test_notifications_quiet_unless_urgent(self):
        dad = auth.create_user(self.store, "dad", p3.PW, "admin")
        other = auth.create_user(self.store, "kid", p3.PW, "family")
        for u in (dad, other, self.wife_login):
            self.store._exec("UPDATE users SET push_prefs=? WHERE id=?", (json.dumps({"tz": "UTC", "quiet_start": "22:00", "quiet_end": "06:30"}), u))
        at = lambda hour: datetime(2026, 11, 20, hour, tzinfo=timezone.utc)
        who = lambda d, hour: sorted(u for u, _ in notify.plan(self.store, "reminder", d, at(hour)))
        d = {"title": "Wife: Refill", "user_id": self.wife_login}
        self.assertEqual(who(d, 9), sorted([dad, self.wife_login]))  # not the kid
        self.assertEqual(who(d, 3), [])
        self.assertEqual(who(dict(d, urgent=True), 3), sorted([dad, self.wife_login]))


class MedicalHttpTests(unittest.TestCase):
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

    def test_permissions_refill_and_cards(self):
        st, r = self.req("POST", "/api/meds", {"person_id": self.wife, "name": "Insulin", "per_day": 3, "on_hand": 9, "critical": True, "cold": True})
        self.assertEqual(st, 200)
        mid = r["id"]
        self.assertEqual(self.req("POST", "/api/meds", {"person_id": self.wife, "name": "x"}, user="kid")[0], 403)  # a child, not theirs
        self.assertEqual(self.req("POST", "/api/meds", {"person_id": self.kid, "name": "Inhaler"}, user="kid")[0], 200)  # their own
        self.assertEqual(self.req("POST", "/api/meds", {"person_id": self.wife, "name": ""})[0], 400)
        v = self.req("GET", "/api/medical", user="kid")[1]
        wife = next(p for p in v["people"] if p["name"] == "Wife")
        self.assertFalse(wife["can_edit"])
        self.assertEqual(wife["meds"][0]["status"]["days_left"], 3)
        self.assertEqual(v["due"][0]["kind"], "refill")
        self.assertEqual(self.req("GET", "/api/state", user="kid")[1]["med_due"], [])  # a child sees only their own
        self.assertEqual(self.req("GET", "/api/state")[1]["med_due"][0]["text"], "Wife: Refill Insulin: 3 days left")
        st, r = self.req("POST", f"/api/meds/{mid}/refilled", {"amount": 90})
        self.assertEqual((st, r["on_hand"]), (200, 99))
        st, page = self.req("GET", "/api/medical-cards.html", raw=True)
        self.assertIn("Insulin", page)
        self.assertEqual(self.req("DELETE", f"/api/meds/{mid}", user="kid")[0], 403)
        self.assertEqual(self.req("DELETE", f"/api/meds/{mid}")[0], 200)
        self.assertEqual(self.req("DELETE", f"/api/meds/{mid}")[0], 404)


if __name__ == "__main__":
    unittest.main()
