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

    def test_airline_names_and_codes_resolve_the_same(self):
        want = {"ident": "WN2496", "callsign": "SWA2496", "airline": "Southwest"}
        for typed in ("Southwest 2496", "southwest 2496", "Southwest Airlines 2496", "WN 2496", "wn2496", "SWA2496"):
            self.assertEqual(flights.parse_number(typed), want, typed)
        self.assertEqual(flights.parse_number("Frontier 1234")["ident"], "F91234")
        self.assertEqual(flights.parse_number("united 512 to LAX"), {"ident": "UA512", "callsign": "UAL512", "airline": "United", "dest": "LAX"})

    def test_wrong_codes_ask(self):
        with self.assertRaises(flights.Unsure) as cm:
            flights.parse_number("SW 2496")
        self.assertEqual(str(cm.exception), "Did you mean Southwest (WN) 2496?")
        self.assertEqual([o["callsign"] for o in cm.exception.options], ["SWA2496"])
        with self.assertRaises(flights.Unsure) as cm:  # a real code, but often typed for Frontier
            flights.parse_number("FR 1234")
        self.assertEqual([o["airline"] for o in cm.exception.options], ["Ryanair", "Frontier"])
        with self.assertRaises(flights.Unsure) as cm:  # half a name is a guess
            flights.parse_number("south 12")
        self.assertIn("Southwest", [o["airline"] for o in cm.exception.options])

    def test_no_match_says_so_and_suggests(self):
        with self.assertRaises(flights.Unsure) as cm:
            flights.parse_number("ZZ 99")
        self.assertEqual(str(cm.exception), "Couldn't find ZZ 99. Type it like United 1234 or UA 1234.")
        self.assertEqual(cm.exception.options, [])
        with self.assertRaises(flights.Unsure) as cm:
            flights.parse_number("Zorblax 5")
        self.assertTrue(str(cm.exception).startswith("Couldn't find ZORBLAX 5"))
        self.assertEqual(flights.parse_number("SY 7")["airline"], "Sun Country")  # a real code is fine

    def test_ask_only_when_unsure(self):
        self.assertIsNone(flights.ask(self.st, "WN 2496"))  # clear, no key: track now
        q = flights.ask(self.st, "SW 2496")
        self.assertEqual(q["message"], "Did you mean Southwest (WN) 2496?")
        self.assertEqual((q["options"][0]["number"], q["options"][0]["callsign"]), ("WN2496", "SWA2496"))
        self.assertEqual(flights.ask(self.st, "Zorblax 5")["options"], [])
        # a clear entry whose FlightAware lookup agrees tracks with no prompt
        self.sky.flight(ident="SWA2496", atc_ident="SWA2496")
        self.assertIsNone(flights.ask(self.st, "WN 2496", "k" * 32, self.sky, T0))
        # FlightAware naming a different flight is asked about first, with where it goes and when
        self.sky.flight(ident="SWN2496", atc_ident="SWN2496")
        q = flights.ask(self.st, "WN 2496", "k" * 32, self.sky, T0)
        self.assertIn("different flight", q["message"])
        self.assertEqual(q["options"][0]["callsign"], "SWN2496")
        self.assertIn("Washington to Denver", q["options"][0]["label"])
        self.assertIn("5:40 PM", q["options"][0]["label"])  # 22:40 UTC in New York
        self.assertEqual(flights.lookups_this_month(self.st, T0), 2)

    def test_flightaware_never_swaps_the_callsign_silently(self):
        flights.add(self.st, self.alex, "WN 2496", origin="DCA")
        self.sky.flight(ident="SWN2496", atc_ident="SWN2496")
        f = self.check(0)
        self.assertEqual(f["callsign"], "SWA2496")
        self.assertEqual(flights.view(f)["callsign"], "SWA2496")
        self.assertFalse(any(u.endswith("SWN2496") for u in self.sky.calls if u.startswith(flights.ADSB)))
        # picked by the person: that callsign is tracked, and the links use it
        flights.add(self.st, self.alex, "WN 2496", callsign="swn2496")
        v = flights.view(flights.active(self.st)[0])
        self.assertEqual(v["callsign"], "SWN2496")
        self.assertIn("flightradar24.com/data/flights/swn2496", v["links"]["Flightradar24"])
        with self.assertRaises(ValueError):
            flights.add(self.st, self.alex, "WN 2496", callsign="no way!")

    def test_assumed_destination_is_a_guess_until_flightaware_knows(self):
        flights.add(self.st, self.alex, "UA 1234", origin="DCA", home=flights.airport("DEN"))
        self.assertTrue(flights.view(flights.active(self.st)[0])["dest_guess"])
        self.sky.flight(destination={"code_iata": "PHX", "name": "Phoenix", "timezone": "America/Phoenix"})
        f = self.check(0)
        self.assertEqual(f["dest"], "PHX")
        self.assertFalse(flights.view(f)["dest_guess"])
        flights.add(self.st, self.alex, "UA 1234", dest="LAX", home=flights.airport("DEN"))
        self.assertFalse(flights.view(flights.active(self.st)[0])["dest_guess"])

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
        self.assertEqual(flights.view(f)["faa"]["DEN"]["lines"], ["DEN: arrivals delayed about 45 min (weather)"])
        self.assertIn("Ground Delay (45 minutes) - weather", flights.view(f)["faa"]["DEN"]["raw"])
        # gone quiet low down near Denver after being in the air: landed
        self.sky.plane = {"lat": 39.95, "lon": -104.8, "alt_baro": 3000, "gs": 150, "seen_pos": 0}
        self.check(200, key=None)
        self.sky.plane = None
        f = self.check(230, key=None)
        self.assertEqual(f["status"], "landed")

    def test_faa_filters_private_plane_notices_and_speaks_plainly(self):
        notam = {"Type": "Closure", "Reason": "!LAX 09/123 LAX AD AP CLSD TO NON SKED TRANSIENT GA ACFT EXC PPR"}
        self.assertEqual(flights.faa_line("LAX", notam), "")
        stop = {"Type": "Ground Stop", "Reason": "thunderstorms", "EndTime": "3:30 PM"}
        self.assertEqual(flights.faa_line("LAX", stop), "LAX: ground stop until 3:30 PM (thunderstorms)")
        self.assertEqual(flights.expand("AD CLSD EXC PPR"), "airport closed except prior permission required")
        out = flights.faa("LAX", lambda url, **k: {"Delay": True, "Status": [notam]})
        self.assertEqual(out, {})
        out = flights.faa("LAX", lambda url, **k: {"Delay": True, "Status": [notam, stop]})
        self.assertEqual(len(out["lines"]), 1)
        self.assertIn("NON SKED", out["raw"])

    def test_late_notification_has_landing_time(self):
        flights.HOME_TZ = "America/Denver"
        self.addCleanup(setattr, flights, "HOME_TZ", "")
        bodies = []
        flights.listeners.append(lambda ev, d: bodies.append(d["body"]))
        flights.add(self.st, self.alex, "UA 1234", origin="DCA")
        self.sky.flight(estimated_in=iso(T0 + timedelta(hours=5, minutes=42)))  # 22:00Z + 5:42 = 03:42Z = 8:42 PM MST
        self.check(0)
        self.assertEqual(bodies, ["About 72 min late into Denver. Now landing 8:42 PM MST."])
        flights.HOME_TZ = "America/New_York"
        self.assertEqual(flights.local_time(T0, "America/Denver", "America/New_York"), "3:00 PM MST")

    def test_times_are_labeled_in_the_viewers_zone(self):
        t = datetime(2026, 11, 20, 23, 3, tzinfo=timezone.utc)
        lt = flights.labeled_time
        self.assertEqual(lt(t, "America/Denver", "America/Los_Angeles", "LAX"), "4:03 PM MST (3:03 PM PST at LAX)")
        self.assertEqual(lt(t, "America/Denver", "America/Denver", "DEN"), "4:03 PM MST")
        self.assertEqual(lt(t, "America/New_York", "America/Denver", "DEN"), "6:03 PM EST (4:03 PM MST at DEN)")
        self.assertEqual(lt(t, "", "America/Denver", "DEN"), "4:03 PM MST")

    def test_late_notification_shows_both_zones(self):
        flights.HOME_TZ = "America/New_York"
        self.addCleanup(setattr, flights, "HOME_TZ", "")
        bodies = []
        flights.listeners.append(lambda ev, d: bodies.append(d["body"]))
        flights.add(self.st, self.alex, "UA 1234", origin="DCA")
        self.sky.flight(estimated_in=iso(T0 + timedelta(hours=5, minutes=42)))
        self.check(0)
        self.assertEqual(bodies, ["About 72 min late into Denver. Now landing 10:42 PM EST (8:42 PM MST at DEN)."])

    def test_inbound_plane_before_departure(self):
        flights.add(self.st, self.alex, "UA 1234", origin="DCA")
        self.sky.flight()
        # the callsign is silent; the tail number is flying in to DCA on another flight, ~100 km out at 400 kt
        self.sky.plane = {"lat": 39.5, "lon": -77.0, "alt_baro": 9000, "gs": 400, "seen_pos": 2}
        real = self.sky
        def get(url, headers=None, timeout=15):
            if url.startswith(flights.ADSB):
                ac = [self.sky.plane] if url.endswith("N12345") else []
                return {"ac": ac}
            return real(url, headers, timeout)
        f = flights.active(self.st)[0]
        f = flights.check(self.st, f, key="k" * 32, get=get, when=T0)
        v = flights.view(f, T0)
        self.assertEqual(f["status"], "scheduled")
        self.assertIsNone(v["pos"])
        self.assertTrue(v["inbound"]["eta_min"] and not v["inbound"]["at_origin"])
        # parked at the origin: "at DCA", still scheduled
        self.sky.plane = {"lat": 38.85, "lon": -77.04, "alt_baro": "ground", "gs": 0, "seen_pos": 1}
        f = flights.check(self.st, f, key="k" * 32, get=get, when=T0 + timedelta(minutes=1))
        self.assertEqual(f["status"], "scheduled")
        self.assertTrue(flights.view(f, T0 + timedelta(minutes=1))["inbound"]["at_origin"])
        # moving at the origin near departure: it's hers
        self.sky.plane = {"lat": 38.85, "lon": -77.04, "alt_baro": "ground", "gs": 15, "seen_pos": 1}
        f = flights.check(self.st, f, key="k" * 32, get=get, when=T0 + timedelta(minutes=30))
        self.assertEqual(f["status"], "taxiing")
        self.assertIsNone(flights.view(f, T0 + timedelta(minutes=30))["inbound"])

    def test_registration_match_counts_after_departure(self):
        flights.add(self.st, self.alex, "UA 1234", origin="DCA")
        self.sky.flight(actual_out=iso(T0 - timedelta(minutes=20)), actual_off=iso(T0 - timedelta(minutes=10)))
        plane = {"lat": 39.5, "lon": -95.0, "alt_baro": 30000, "gs": 450, "seen_pos": 2}
        real = self.sky
        def get(url, headers=None, timeout=15):
            if url.startswith(flights.ADSB):
                return {"ac": [plane] if url.endswith("N12345") else []}
            return real(url, headers, timeout)
        f = flights.check(self.st, flights.active(self.st)[0], key="k" * 32, get=get, when=T0)
        self.assertEqual(f["status"], "airborne")
        self.assertEqual(flights.view(f, T0)["pos"]["alt_ft"], 30000)

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

    def test_unsure_flights_ask_first_and_track_nothing(self):
        code, r = self.req("POST", "/api/checkin", {"status": "moving", "flight": "SW 2496"})
        self.assertEqual(code, 200)
        self.assertEqual(r["confirm"]["message"], "Did you mean Southwest (WN) 2496?")
        self.assertNotIn("uid", r)  # not even checked in yet
        self.assertEqual(self.req("GET", "/api/flights")[1]["flights"], [])
        code, r = self.req("POST", "/api/flights", {"number": "FR 1234"})
        self.assertEqual([o["airline"] for o in [{"airline": o["label"].split(" (")[0]} for o in r["confirm"]["options"]]], ["Ryanair", "Frontier"])
        self.assertEqual(self.req("GET", "/api/flights")[1]["flights"], [])
        code, r = self.req("POST", "/api/flights", {"number": "Zorblax 5"})
        self.assertEqual((code, r["confirm"]["options"]), (200, []))
        # picking one tracks it
        code, r = self.req("POST", "/api/checkin", {"status": "moving", "flight": "WN2496", "callsign": "SWA2496"})
        self.assertTrue(r["flight"])
        f = self.req("GET", "/api/board")[1]["flights"][0]
        self.assertEqual((f["ident"], f["callsign"], f["airline"]), ("WN2496", "SWA2496", "Southwest"))
        self.assertEqual(self.req("POST", "/api/flights", {"number": "Southwest 2496"})[1]["ident"], "WN2496")
        names = [a["name"] for a in self.req("GET", "/api/flights/airlines")[1]["airlines"]]
        self.assertIn("Southwest", names)

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
