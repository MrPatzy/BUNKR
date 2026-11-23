"""Flights: spotting the airport, flight numbers, following a flight from FlightAware, adsb.lol and the FAA (all faked),
landing, the family's pushes, the monthly cap, and the HTTP side of checking in with a flight."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, checkins, flights, notify  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

T0 = datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)
iso = lambda dt: dt.isoformat().replace("+00:00", "Z")


class Sky:
    """Fake FlightAware, adsb.lol and FAA answers, changed as the test's flight goes on."""

    def __init__(self):
        self.calls = []
        self.fa = {"flights": []}
        self.plane = None
        self.faa = {"DEN": {"Delay": False}, "DCA": {"Delay": False}}

    def __call__(self, url, headers=None, timeout=15):
        self.calls.append(url)
        if url.startswith(flights.AEROAPI):
            assert headers and headers.get("x-apikey") == "k" * 32
            return self.fa
        if url.startswith(flights.ADSB):
            return {"ac": [self.plane] if self.plane and url.endswith("UAL1234") else []}
        if url.startswith(flights.FAA):
            return self.faa.get(url.rsplit("/", 1)[1], {})
        raise AssertionError(url)

    def flight(self, **kw):
        base = {"ident": "UAL1234", "atc_ident": "UAL1234", "registration": "N12345", "status": "Scheduled",
                "origin": {"code_iata": "DCA", "name": "Reagan National", "timezone": "America/New_York"},
                "destination": {"code_iata": "DEN", "name": "Denver Intl", "timezone": "America/Denver"},
                "scheduled_out": iso(T0 + timedelta(minutes=40)), "scheduled_in": iso(T0 + timedelta(hours=4, minutes=30)),
                "estimated_in": iso(T0 + timedelta(hours=4, minutes=30)), "gate_origin": "B12", "cancelled": False, "diverted": False}
        base.update(kw)
        self.fa = {"flights": [base]}


class FlightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "f.db")
        self.alex = uuid.uuid4().hex
        self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at) VALUES(?,?,?,?,?)", ("Alex", "adult", 0, self.alex, checkins.now()))
        self.told = []
        flights.listeners.append(lambda ev, d: self.told.append(d["title"]))
        self.sky = Sky()

    def tearDown(self):
        flights.listeners.clear()
        self.tmp.cleanup()

    def test_airports_and_numbers(self):
        self.assertEqual(flights.airport_near(38.8512, -77.0402)["iata"], "DCA")
        self.assertEqual(flights.airport_near(39.849, -104.674)["iata"], "DEN")
        self.assertIsNone(flights.airport_near(39.74, -104.99))  # downtown Denver
        self.assertIsNone(flights.airport_near(None, None))
        self.assertEqual(flights.parse_number("ua 1234"), {"ident": "UA1234", "callsign": "UAL1234", "airline": "United"})
        self.assertEqual(flights.parse_number("SKW5123")["ident"], "OO5123")
        self.assertEqual(flights.parse_number("ua 1234 to lax")["dest"], "LAX")
        with self.assertRaises(ValueError):
            flights.parse_number("UA 1 to Narnia")
        self.assertEqual(flights.home_airport(39.88, -105.03)["iata"], "DEN")
        self.assertIsNone(flights.home_airport(0, -150))  # nowhere near a big airport
        with self.assertRaises(ValueError):
            flights.parse_number("hello there")

    def check(self, minutes, key="k" * 32):
        f = flights.active(self.st)[0]
        return flights.check(self.st, f, key=key, get=self.sky, when=T0 + timedelta(minutes=minutes))

    def test_a_flight_from_gate_to_landing(self):
        flights.add(self.st, self.alex, "UA 1234", origin="DCA")
        self.sky.flight()
        f = self.check(0)
        self.assertEqual((f["status"], f["dest"]), ("scheduled", "DEN"))
        self.assertEqual(flights.view(f)["gates"], {"gate_origin": "B12"})
        # late, and FlightAware isn't asked again within 30 minutes (10 near departure)
        self.sky.flight(estimated_out=iso(T0 + timedelta(minutes=85)), estimated_in=iso(T0 + timedelta(hours=5, minutes=15)))
        self.check(5)
        self.assertEqual(sum(u.startswith(flights.AEROAPI) for u in self.sky.calls), 1)
        f = self.check(12)
        self.assertEqual(flights.view(f)["late_min"], 45)
        self.assertEqual(self.told, ["Alex's flight UA1234 is running late"])
        self.check(25)
        self.assertEqual(len(self.told), 1)  # not again until it slips another 30 minutes
        # in the air, heard by adsb.lol
        self.sky.flight(actual_out=iso(T0 + timedelta(minutes=85)), actual_off=iso(T0 + timedelta(minutes=100)),
                        estimated_in=iso(T0 + timedelta(hours=5, minutes=15)))
        self.sky.plane = {"flight": "UAL1234 ", "lat": 39.5, "lon": -95.0, "alt_baro": 36000, "gs": 470, "track": 270, "seen_pos": 3}
        f = self.check(150)
        v = flights.view(f, T0 + timedelta(minutes=150))
        self.assertEqual((f["status"], v["pos"]["alt_ft"]), ("airborne", 36000))
        self.assertGreater(v["pos"]["km_to_go"], 700)
        # on the ground at Denver: landed, told once
        self.sky.plane = {"flight": "UAL1234", "lat": 39.85, "lon": -104.67, "alt_baro": "ground", "gs": 12, "seen_pos": 1}
        f = self.check(320)
        self.assertEqual(f["status"], "landed")
        self.check(322)
        self.assertEqual(self.told[-1], "Alex landed in Denver")
        self.assertEqual(self.told.count("Alex landed in Denver"), 1)
        # cleared a few hours later
        self.check(320 + 200)
        self.assertEqual(flights.active(self.st), [])

    def test_free_sources_only_and_faa_delays(self):
        flights.add(self.st, self.alex, "UA1234", origin="DCA", dest="DEN")
        self.sky.faa["DEN"] = {"Delay": True, "Status": [{"Type": "Ground Delay", "AvgDelay": "45 minutes", "Reason": "weather"}]}
        self.sky.plane = {"lat": 38.9, "lon": -77.5, "alt_baro": 12000, "gs": 300, "seen_pos": 2}
        f = self.check(0, key=None)
        self.assertFalse(any(u.startswith(flights.AEROAPI) for u in self.sky.calls))
        self.assertEqual(f["status"], "airborne")
        self.assertIn("Ground Delay (45 minutes) - weather", flights.view(f)["faa"]["DEN"]["text"])
        # gone quiet low down near Denver after being in the air: landed
        self.sky.plane = {"lat": 39.95, "lon": -104.8, "alt_baro": 3000, "gs": 150, "seen_pos": 0}
        self.check(200, key=None)
        self.sky.plane = None
        f = self.check(230, key=None)
        self.assertEqual(f["status"], "landed")

    def test_monthly_cap(self):
        flights.add(self.st, self.alex, "UA1234")
        self.sky.flight()
        self.st.set_raw("aeroapi_usage", T0.strftime("%Y-%m") + f":{flights.MONTHLY_CAP}")
        self.check(0)
        self.assertFalse(any(u.startswith(flights.AEROAPI) for u in self.sky.calls))

    def test_cancelled_and_a_new_flight_replaces_the_old(self):
        flights.add(self.st, self.alex, "UA1234")
        self.sky.flight(cancelled=True)
        self.check(0)
        self.assertEqual(self.told, ["Alex's flight UA1234 was cancelled"])
        flights.add(self.st, self.alex, "UA 99")
        self.assertEqual([f["ident"] for f in flights.active(self.st)], ["UA99"])

    def test_push_goes_to_the_family_not_the_traveller(self):
        auth.create_user(self.st, "alex", p3.PW, "admin")
        auth.create_user(self.st, "mom", p3.PW, "family")
        zid = auth.user_by_name(self.st, "alex")["id"]
        self.st._exec("UPDATE people SET user_id=? WHERE uid=?", (zid, self.alex))
        noon = datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc)
        got = notify.plan(self.st, "flight", {"person_uid": self.alex, "uid": "x", "title": "Alex landed in Denver"}, at=noon)
        self.assertEqual([u for u, _ in got], [auth.user_by_name(self.st, "mom")["id"]])


class FlightHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        self.st = self.hub.store
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        for name, login, role in (("Dad", "dad", "adult"), ("Kid", "kid", "child")):
            self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at, user_id) VALUES(?,?,?,?,?,?)",
                          (name, role, 0, uuid.uuid4().hex, checkins.now(), auth.user_by_name(self.st, login)["id"]))
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {u: self.login(u) for u in ("dad", "kid")}
        from bunkr import server
        self.server, self.real_check = server, server.check_flights
        server.check_flights = lambda: None  # no network in tests: the flight check after adding one does nothing

    def tearDown(self):
        self.server.check_flights = self.real_check
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

    def test_check_in_at_the_airport(self):
        self.st.save_site({"name": "Home", "kind": "home", "address": "x", "lat": 39.88, "lon": -105.03, "state": "CO"})
        code, r = self.req("POST", "/api/checkin", {"status": "moving", "lat": 38.8512, "lon": -77.0402})
        self.assertEqual(code, 200)
        self.assertEqual((r["airport"]["iata"], r["airport"]["home"]), ("DCA", "DEN"))
        self.assertEqual(r["place"], "DCA airport")
        self.assertEqual(self.req("POST", "/api/checkin", {"status": "moving", "flight": "nope!"})[0], 400)
        code, r = self.req("POST", "/api/checkin", {"status": "moving", "lat": 38.8512, "lon": -77.0402, "flight": "UA 1234"})
        self.assertTrue(r["flight"])
        f = self.req("GET", "/api/board")[1]["flights"]
        self.assertEqual((f[0]["ident"], f[0]["origin"], f[0]["dest"], f[0]["person_name"]), ("UA1234", "DCA", "DEN", "Dad"))
        self.assertIn("flightaware.com/live/flight/UAL1234", f[0]["links"]["FlightAware"])
        self.assertNotIn("airport", self.req("POST", "/api/checkin", {"status": "moving", "lat": 38.8512, "lon": -77.0402})[1])

    def test_check_someone_else_in_on_a_flight(self):
        kid = [p for p in checkins.people(self.st) if p["name"] == "Kid"][0]["uid"]
        dad = [p for p in checkins.people(self.st) if p["name"] == "Dad"][0]["uid"]
        self.assertEqual(self.req("POST", "/api/checkin", {"person_uid": dad, "status": "safe"}, "kid")[0], 403)  # a child can't
        code, r = self.req("POST", "/api/checkin", {"person_uid": kid, "status": "moving", "note": "boarding", "flight": "WN 88"})
        self.assertEqual(code, 200)
        self.assertTrue(r["flight"])
        b = self.req("GET", "/api/board")[1]
        me = [p for p in b["people"] if p["uid"] == kid][0]["last"]
        self.assertEqual((me["status"], me["note"], me["by_name"]), ("moving", "boarding", "dad"))
        self.assertEqual((b["flights"][0]["ident"], b["flights"][0]["person_name"]), ("WN88", "Kid"))

    def test_who_may_add_and_end(self):
        dad = [p for p in checkins.people(self.st) if p["name"] == "Dad"][0]["uid"]
        self.assertEqual(self.req("POST", "/api/flights", {"person_uid": dad, "number": "DL 5"}, "kid")[0], 403)
        code, f = self.req("POST", "/api/flights", {"number": "DL 5"}, "kid")  # their own
        self.assertEqual((code, f["ident"], f["person_name"]), (200, "DL5", "Kid"))
        self.assertEqual(self.req("POST", f"/api/flights/{f['uid']}/end", user="dad")[0], 200)  # an adult may
        self.assertEqual(self.req("GET", "/api/flights")[1]["flights"], [])

    def test_key_settings_are_admin_only(self):
        self.assertEqual(self.req("GET", "/api/flights/settings", user="kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/flights/settings", {"key": "short"})[0], 400)
        self.assertTrue(self.req("POST", "/api/flights/settings", {"key": "a" * 32})[1]["key"])
        key_file = Path(self.st.path).parent / flights.KEY_FILE
        self.assertEqual(oct(key_file.stat().st_mode & 0o777), "0o600")
        self.assertFalse(self.req("POST", "/api/flights/settings", {"key": ""})[1]["key"])


if __name__ == "__main__":
    unittest.main()
