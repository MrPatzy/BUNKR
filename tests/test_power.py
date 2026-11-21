"""Power plan: runtime per battery, solar, warnings, suggestions from Medical, the outage checklist and the API."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, medical, power  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

RIVER = {"name": "River", "wh": 1000, "max_w": 300, "solar_w": 0}


def dev(name, watts, hours, priority=1, battery="River", surge_w=0):
    return {"name": name, "watts": watts, "hours": hours, "priority": priority, "battery": battery, "surge_w": surge_w, "notes": ""}


class PlanTests(unittest.TestCase):
    def test_runtime_levels(self):
        devs = [dev("Fridge", 150, 8, 1, surge_w=900), dev("Router", 20, 24, 1), dev("Lights", 30, 5, 2), dev("TV", 100, 3, 3),
                dev("Sump", 800, 1, 1, battery="")]
        p = power.plan(devs, [dict(RIVER, max_w=250)])
        b = p["batteries"][0]
        must, should, every = b["levels"]
        self.assertEqual(must["wh_day"], 1680)  # 1200 + 480
        self.assertEqual(must["days"], round(850 / 1680, 1))
        self.assertEqual(must["hours"], round(850 / 1680 * 24))
        self.assertEqual(should["wh_day"], 1830)
        self.assertEqual(every["wh_day"], 2130)
        self.assertTrue(any("300 W, more than its 250 W output" in w for w in b["warnings"]))  # 150+20+30+100 at once
        self.assertTrue(any("surge" in w for w in b["warnings"]))
        self.assertEqual([d["name"] for d in p["unpowered"]], ["Sump"])

    def test_solar_can_keep_up(self):
        p = power.plan([dev("Router", 10, 24)], [dict(RIVER, solar_w=200)])
        self.assertEqual(p["batteries"][0]["solar_wh_day"], round(200 * 0.75 * 3.5))
        self.assertEqual(p["batteries"][0]["levels"][0]["days"], "forever")
        empty = power.plan([], [RIVER])["batteries"][0]
        self.assertIsNone(empty["levels"][0]["days"])

    def test_outage_list(self):
        steps = power.outage_list([dev("Fridge", 150, 8), dev("CPAP", 30, 8), dev("Sump", 800, 1, battery="")], [RIVER])
        self.assertTrue(steps[0].startswith("Plug into the River: Fridge, CPAP (lasts about"))
        self.assertEqual(steps[1], "No battery planned for: Sump.")
        self.assertEqual(power.outage_list([], []), [])

    def test_cleaning(self):
        with self.assertRaises(ValueError):
            power.clean_device({"name": "x", "watts": "-1"})
        with self.assertRaises(ValueError):
            power.clean_device({"name": "x", "hours": 30})
        with self.assertRaises(ValueError):
            power.clean_device({"name": " "})
        self.assertEqual(power.clean_device({"name": "x", "priority": 9})["priority"], 2)
        b = power.clean_batteries([{"name": "A", "wh": "abc"}, {"name": "a"}, {"name": ""}, {"name": "B", "wh": 5e9}])
        self.assertEqual([(x["name"], x["wh"]) for x in b], [("A", 0.0), ("B", 200000.0)])

    def test_suggestions_from_medical(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(Path(tmp) / "p.db")
            pid = st._exec("INSERT INTO people(name, role, uid, devices) VALUES('Wife', 'adult', 'w', 'CPAP, glasses')")
            medical.save(st, pid, {"name": "Insulin", "cold": True})
            got = {s["preset"]: s["why"] for s in power.suggestions(st)}
            self.assertIn("Wife's Insulin", got["med_fridge"])
            self.assertIn("cpap", got)
            st._exec("INSERT INTO power_devices(name) VALUES('Refrigerator'), ('CPAP')")
            self.assertEqual(power.suggestions(st), [])


class PowerHttpTests(unittest.TestCase):
    def setUp(self):
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

    def req(self, method, path, body=None, user="dad"):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_round_trip(self):
        st, s = self.req("POST", "/api/power/settings", {"batteries": [RIVER], "inverter": "Enphase IQ7"})
        self.assertEqual((st, s["batteries"][0]["wh"], s["inverter"]), (200, 1000, "Enphase IQ7"))
        self.assertEqual(self.req("POST", "/api/power/preset", {"key": "fridge", "battery": "River"})[0], 200)
        self.assertEqual(self.req("POST", "/api/power/preset", {"key": "nope"})[0], 400)
        st, r = self.req("POST", "/api/power/devices", {"name": "Router", "watts": 20, "hours": 24, "priority": 1, "battery": "River"})
        did = r["id"]
        self.assertEqual(self.req("POST", "/api/power/devices", {"name": "x", "watts": "lots"})[0], 400)
        self.assertEqual(self.req("POST", "/api/power/devices", {"name": "x"}, user="kid")[0], 403)
        st, v = self.req("GET", "/api/power", user="kid")
        self.assertEqual(v["plan"]["batteries"][0]["levels"][0]["wh_day"], 1680)
        self.req("POST", f"/api/power/devices/{did}", {"name": "Router", "watts": 10, "hours": 24, "priority": 1, "battery": "River"})
        self.assertEqual(self.req("GET", "/api/power")[1]["plan"]["batteries"][0]["levels"][0]["wh_day"], 1440)
        scn = next(x for x in self.req("GET", "/api/state", user="kid")[1]["scenarios"] if x["id"] == "power_outage")
        self.assertTrue(scn["power_plan"][0].startswith("Plug into the River"))
        self.assertEqual(self.req("DELETE", f"/api/power/devices/{did}")[0], 200)
        self.assertEqual(len(self.req("GET", "/api/power")[1]["devices"]), 1)


if __name__ == "__main__":
    unittest.main()
