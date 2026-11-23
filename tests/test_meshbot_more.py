"""The mesh bot's newer commands: places, weather, sun, quakes, space, aircraft, numbered news, the AI's short answer,
the route trace, alerts pushed to the mesh, the daily mesh brief and switching commands off. No network: every
lookup gets a fake fetcher."""

import hashlib
import io
import json
import sys
import tempfile
import time
import unittest
import uuid
import zipfile
import zoneinfo
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bunkr import astro, checkins, mesh, meshbot, meshcore, meshinfo, places, wx  # noqa: E402
from bunkr.db import Store  # noqa: E402

ZCTA = "GEOID|GEOIDFQ|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG\n" + "\n".join(
    [f"80202|x|1|1|1|1|39.751500|-104.997700", "80401|x|1|1|1|1|39.718900|-105.235300", "62701|x|1|1|1|1|39.800000|-89.650000"]
    + [f"{10000 + i:05d}|x|1|1|1|1|40.0|-75.0" for i in range(30000)])
PLACE = "USPS|GEOID|GEOIDFQ|ANSICODE|NAME|LSAD|FUNCSTAT|ALAND|AWATER|ALAND_SQMI|AWATER_SQMI|INTPTLAT|INTPTLONG\n" + "\n".join(
    ["CO|1|x|1|Denver city|25|A|1|1|153.0|1|39.761900|-104.881100", "CO|2|x|1|Edgewater city|25|A|1|1|0.7|0|39.753000|-105.064000",
     "CO|3|x|1|Golden city|25|A|1|1|10.0|0|39.740000|-105.210000", "MN|4|x|1|Golden Valley city|25|A|1|1|10|0|44.99|-93.36",
     "IL|5|x|1|Springfield city|25|A|1|1|60|1|39.791100|-89.644600", "MO|6|x|1|Springfield city|25|A|1|1|80|1|37.19|-93.29",
     "CO|7|x|1|Aspen Park CDP|57|S|1|1|2|0|39.54|-105.29"]
    + [f"PA|{100 + i}|x|1|Town{i} borough|21|A|1|1|1|0|40.0|-75.0" for i in range(25000)])


def zipped(text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("gaz.txt", text)
    return b.getvalue()


class PlacesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        places.refresh(self.folder, get=lambda url: zipped(ZCTA if "zcta" in url else PLACE))

    def tearDown(self):
        self.tmp.cleanup()

    def test_lookups(self):
        self.assertEqual(places.find(self.folder, "80202")["label"], "80202 Denver, CO")  # a big city beats a nearer small one
        self.assertEqual(places.find(self.folder, "80202-1234")["state"], "CO")
        self.assertEqual(places.find(self.folder, "Denver CO")["label"], "Denver, CO")
        self.assertEqual(places.find(self.folder, "denver, colorado")["tz"], "America/Denver")
        self.assertIsNone(places.find(self.folder, "Springfield"))          # two states
        self.assertEqual(places.ambiguous(self.folder, "Springfield"), ["IL", "MO"])
        self.assertEqual(places.find(self.folder, "Springfield", prefer_state="IL")["state"], "IL")
        self.assertEqual(places.find(self.folder, "Aspen Park")["label"], "Aspen Park, CO")  # "CDP" taken off
        self.assertIsNone(places.find(self.folder, "99999"))
        self.assertIsNone(places.find(self.folder, ""))

    def test_short_files_are_refused(self):
        with self.assertRaises(ValueError):
            places.refresh(self.folder, get=lambda url: zipped(ZCTA.splitlines()[0] + "\n80202|x|1|1|1|1|39.7|-105.0"))
        self.assertIsNotNone(places.find(self.folder, "80202"))  # the good copy stays
        self.assertFalse(places.needs_refresh(self.folder))


def nws(url):
    """Fake api.weather.gov."""
    if "/points/" in url:
        return {"properties": {"forecast": "F", "observationStations": "S", "timeZone": "America/Denver"}}
    if url == "S":
        return {"features": [{"properties": {"stationIdentifier": "KDEN"}}]}
    if "/stations/KDEN/" in url:
        return {"properties": {"temperature": {"value": -1.1}, "windSpeed": {"value": 19.3}, "windDirection": {"value": 315},
                               "textDescription": "Mostly Cloudy"}}
    if url == "F":
        days = ["Monday", "Tuesday", "Wednesday", "Thanksgiving Day", "Friday", "Saturday", "Sunday"]
        ps = [{"name": "Tonight", "isDaytime": False, "temperature": 24, "shortForecast": "Partly Cloudy", "windSpeed": "5 mph",
               "windDirection": "S", "startTime": "2026-11-22T18:00:00-07:00", "probabilityOfPrecipitation": {"value": 3}}]
        for i, d in enumerate(days):
            ps.append({"name": d, "isDaytime": True, "temperature": 44 + i, "shortForecast": "Chance Snow Showers then Mostly Sunny",
                       "windSpeed": "10 to 20 mph", "windDirection": "NW", "startTime": f"2026-11-{23 + i}T06:00:00-07:00",
                       "probabilityOfPrecipitation": {"value": 40}})
            ps.append({"name": d + " Night", "isDaytime": False, "temperature": 25 + i, "shortForecast": "Clear",
                       "windSpeed": "5 mph", "windDirection": "W", "startTime": f"2026-11-{23 + i}T18:00:00-07:00"})
        return {"properties": {"periods": ps}}
    if "alerts/active" in url:
        return {"features": [{"properties": {"event": "Winter Weather Advisory", "severity": "Moderate", "ends": "2026-11-24T05:00:00-07:00"}},
                             {"properties": {"event": "Red Flag Warning", "severity": "Severe", "expires": "2026-11-23T20:00:00-07:00"}}]}
    raise AssertionError(url)


class WeatherTests(unittest.TestCase):
    def setUp(self):
        wx._points.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        places.refresh(self.folder, get=lambda url: zipped(ZCTA if "zcta" in url else PLACE))
        self.ctx = {"geo_folder": self.folder, "home": {"lat": 39.7, "lon": -105.0, "label": "Home", "state": "CO",
                                                         "tz": "America/Denver", "kind": "home"}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_now_and_next(self):
        out = meshinfo.weather(self.ctx, "80202", get=nws)
        self.assertEqual(len(out), 1)
        self.assertLessEqual(len(out[0]), 120)
        self.assertIn("80202 Denver, CO: now 30F m cldy, NW12", out[0])
        self.assertIn("Tonight: Pt cldy 24F", out[0])
        self.assertNotIn("3%", out[0])  # a few percent is noise

    def test_days(self):
        out = meshinfo.weather(self.ctx, "Denver CO 5", get=nws)
        text = " ".join(out)
        self.assertIn("Denver, CO 5d", text)
        self.assertIn("Tngt -/24", text)
        self.assertIn("Thu 47/28 Chc snow shwrs > m sunny 40%", text)  # the date's day, not "Thanksgiving Day"
        self.assertTrue(all(len(m) <= 120 for m in out))
        ten = " ".join(meshinfo.weather(self.ctx, "80202 10", get=nws))
        self.assertIn("(NWS gives 7)", ten)
        self.assertIn("Home", meshinfo.weather(self.ctx, "", get=nws)[0])  # nothing typed: home

    def test_unknown_and_ambiguous_places(self):
        self.assertIn("Which Springfield? IL MO", meshinfo.weather(self.ctx, "Springfield", get=nws)[0])
        self.assertIn("Try a ZIP code", meshinfo.weather(self.ctx, "Atlantis", get=nws)[0])

    def test_alerts_for_a_place(self):
        out = meshinfo.place_alerts(self.ctx, "80401", situation_items=[
            {"title": "Wildfire: Gap Fire", "lat": 39.75, "lon": -105.30, "category": "fire", "tier": "priority"},
            {"title": "Far away fire", "lat": 45.0, "lon": -110.0, "category": "fire", "tier": "priority"}], get=nws)
        text = " ".join(out)
        self.assertIn("Red Flag Wrn til Mon 8p", text)      # most severe first, in the place's time zone
        self.assertIn("Wntr Wx Adv til Tue 5a", text)
        self.assertIn("Near: Wildfire: Gap Fire", text)
        self.assertNotIn("Far away", text)


class SkyTests(unittest.TestCase):
    def test_sun_and_moon(self):
        tz = zoneinfo.ZoneInfo("America/Denver")
        line = astro.line("Denver", 39.7515, -104.9977, tz, datetime(2026, 11, 23, 12, tzinfo=tz))
        self.assertIn("rise 6:53a set 4:39p", line)   # NOAA's calculator says 6:53 and 4:39
        self.assertIn("Moon full", line)               # full moon 24 Nov 2026
        self.assertLessEqual(len(line), 120)
        polar = astro.line("Utqiagvik", 71.29, -156.79, zoneinfo.ZoneInfo("America/Anchorage"),
                           datetime(2026, 12, 21, 12, tzinfo=zoneinfo.ZoneInfo("America/Anchorage")))
        self.assertIn("no sunrise today", polar)

    def test_quakes(self):
        now = time.time() * 1000
        feed = {"features": [{"properties": {"mag": 3.1, "place": "12 km NW of Trinidad, CO", "time": now - 7200 * 1000},
                              "geometry": {"coordinates": [-104.6, 37.2, 5]}},
                             {"properties": {"mag": 6.2, "place": "Tonga", "time": now - 3600 * 1000},
                              "geometry": {"coordinates": [-175, -20, 10]}}]}
        ctx = {"home": {"lat": 39.7, "lon": -105.0, "label": "Home", "state": "CO", "tz": "", "kind": "home"}, "geo_folder": "/nonexistent"}
        self.assertIn("M3.1 12 km NW of Trinidad, CO 2h ago", " ".join(meshinfo.quake(ctx, "", get=lambda u: feed)))
        far = {"features": feed["features"][1:]}
        self.assertIn("Biggest: M6.2 Tonga", meshinfo.quake(ctx, "", get=lambda u: far)[0])

    def test_space(self):
        def get(url):
            if "scales" in url:
                return {"0": {"R": {"Scale": "1"}, "S": {"Scale": "0"}, "G": {"Scale": "2"}},
                        "1": {"R": {"MinorProb": "35"}, "G": {"Scale": "1"}}}
            return [{"time_tag": "x", "Kp": 5.33}]
        out = meshinfo.space({}, "", get=get)[0]
        self.assertIn("R1 HF minor fade", out)
        self.assertIn("G2 moderate storm", out)
        self.assertIn("Kp 5.33", out)
        self.assertIn("R1-2 35%", out)

    def test_overhead(self):
        ac = {"ac": [{"flight": "UAL1234 ", "t": "B738", "lat": 39.80, "lon": -105.0, "alt_baro": 12025},
                     {"flight": "", "r": "N123AB", "t": "C172", "lat": 39.71, "lon": -105.0, "alt_baro": 6500},
                     {"flight": "SWA1", "lat": 39.7, "lon": -105.0, "alt_baro": "ground"}]}
        ctx = {"home": {"lat": 39.7, "lon": -105.0, "label": "Home", "state": "CO", "tz": "", "kind": "home"}, "geo_folder": "/x"}
        seen = []
        out = meshinfo.overhead({**ctx, "sender_pos": (40.0, -105.1)}, "", get=lambda u: seen.append(u) or ac)
        self.assertIn("/40.0000/-105.1000/", seen[0])     # the sender's node, when it shares where it is
        self.assertIn("2 near your node", out[0])
        out = meshinfo.overhead(ctx, "", get=lambda u: ac)[0]
        self.assertTrue(out.index("N123AB") < out.index("UAL1234"))  # nearest first
        self.assertNotIn("SWA1", out)                                # on the ground


STORY = {"title": "COP31 climate talks collapse without a deal", "short": "COP31 talks collapse",
         "agree": "Talks in Belem ended without agreement on fossil fuel phase-out after two weeks. " * 3, "matters": "Little direct effect."}


class NewsTests(unittest.TestCase):
    def setUp(self):
        meshinfo._news_asked.clear()

    def rec(self, n):
        return {"ai": {"lead": "x", "stories": [dict(STORY, title=f"Story number {i} " + STORY["title"], short=f"Story {i} goes here")
                                                for i in range(1, n + 1)]}}

    def test_numbered_list_and_more(self):
        rec = self.rec(9)
        first = meshinfo.news(rec, "alex", "", now=100)[0]
        self.assertTrue(first.startswith("1 Story 1 goes here. 2 Story 2 goes here."), first)
        self.assertIn("MORE for next", first)
        self.assertLessEqual(len(first), 120)
        nxt = meshinfo.news(rec, "alex", "more", now=110)[0]
        self.assertNotIn("1 Story 1 goes", nxt)
        self.assertTrue(nxt.startswith("5 Story 5 goes here"), nxt)
        self.assertEqual(meshinfo.news_number("alex", "2", now=120), 2)
        self.assertIsNone(meshinfo.news_number("alex", "2", now=120 + meshinfo.NEWS_TTL))  # too long after
        self.assertIsNone(meshinfo.news_number("riley", "2", now=120))                     # someone else

    def test_one_story_in_parts(self):
        out = meshinfo.news(self.rec(3), "alex", "2")
        self.assertTrue(2 <= len(out) <= 3)
        self.assertTrue(out[0].startswith("2. Story number 2"))
        self.assertTrue(out[-1].endswith(f"{len(out)}/{len(out)}"))
        self.assertTrue(all(len(m) <= 120 for m in out))
        self.assertEqual(meshinfo.news(self.rec(3), "alex", "9"), ["Pick 1-3."])

    def test_without_ai(self):
        rec = {"groups": [{"headline": "Delta system outage grounds Thanksgiving flights across the US", "outlets": 5, "regions": ["US", "Europe"]}]}
        out = meshinfo.news(rec, "z", "")[0]
        self.assertIn("1 Delta system outage grounds", out)
        self.assertIn("Covered by 5 outlets (US, Europe)", meshinfo.news(rec, "z", "1")[0])
        self.assertEqual(meshinfo.news(None, "z", ""), ["No news brief yet today."])


class AskTests(unittest.TestCase):
    def test_short_and_marked(self):
        seen = []
        out = meshinfo.ask(lambda p: seen.append(p) or "A: 1/8tsp/gal unscented bleach. Wait 30min.", "How do I purify water with bleach?")
        self.assertEqual(out, ["AI: 1/8tsp/gal unscented bleach. Wait 30min."])
        self.assertIn("How do I purify water with bleach?", seen[0])
        long = meshinfo.ask(lambda p: "word " * 200, "q")
        self.assertEqual(len(long), 2)
        self.assertTrue(all(len(m) <= 120 for m in long))
        self.assertIn("didn't answer", meshinfo.ask(lambda p: (_ for _ in ()).throw(RuntimeError("x")), "q")[0])
        self.assertIn("!ask", meshinfo.ask(lambda p: "x", "")[0])


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_packet_path(self):
        secret = bytes(range(16))
        ch = hashlib.sha256(secret).digest()[0]
        raw = bytes([0x01 | (0x05 << 2), 2, 0xA1, 0xB2, ch, 0, 0]) + b"cipher"      # flood, 2 one-byte repeater IDs
        pk = meshcore.packet(raw)
        self.assertEqual(pk["path"], ["a1", "b2"])
        self.assertEqual(pk["type"], meshcore.GRP_TXT)
        self.assertEqual(pk["payload"][0], meshcore.channel_hash(secret))
        two = bytes([0x00 | (0x05 << 2), 1, 2, 3, 4, (1 << 6) | 2, 0xA1, 0xA2, 0xB2, 0xB3, ch, 0]) + b"x"  # transport codes, 2-byte IDs
        self.assertEqual(meshcore.packet(two)["path"], ["a1a2", "b2b3"])
        self.assertIsNone(meshcore.packet(bytes([0x05, 9, 1])))  # claims more path than it has
        self.assertEqual(meshcore.hops((1 << 6) | 3), 3)
        self.assertIsNone(meshcore.hops(0xFF))
        frame = meshcore.parse(bytes([meshcore.LOG_RX, 25, 0x9E]) + raw)
        self.assertEqual((frame["snr"], frame["rssi"], frame["raw"]), (6.25, -98, raw))

    def test_names_and_line(self):
        for key, name, t in (("a1" + "0" * 62, "Lookout RPT", 2), ("b2" + "1" * 62, "Golden RPT", 2), ("b2" + "2" * 62, "Other RPT", 2),
                             ("a1" + "3" * 62, "Somebody", 1)):
            mesh.save_nodes(self.st, [{"public_key": key, "name": name, "node_type": t}])
        self.assertEqual(mesh.path_names(self.st, ["a1", "b2", "c3"]), ["Lookout RPT", mesh.path_names(self.st, ["b2"])[0], "C3"])
        self.assertTrue(mesh.path_names(self.st, ["b2"])[0].endswith("?"))  # two repeaters share that ID
        line = meshinfo.trace(["Lookout RPT", "Golden RPT"], "Alex", "Home Base", snr=6.25, rssi=-98)[0]
        self.assertEqual(line, "Trace: Alex > Lookout RPT > Golden RPT > Home Base. 2 hops, SNR 6.25 RSSI -98")
        self.assertIn("direct, no repeaters", meshinfo.trace([], "Alex", "Home Base", hops=0)[0])

    def test_link_matches_the_heard_packet(self):
        s = mesh.save_settings(self.st, {"connect": "tcp:x:5000", "channels": [{"name": "Fam", "index": 4, "kind": "family",
                                                                                "secret": "00112233445566778899aabbccddeeff"}]})
        link = mesh.Link(self.st)
        want = meshcore.channel_hash(bytes.fromhex("00112233445566778899aabbccddeeff"))
        link.rx.append((100.0, {"type": 5, "path": ["a1"], "payload": bytes([want, 0]), "snr": 5.0, "rssi": -90}))
        link.rx.append((101.0, {"type": 5, "path": ["a1", "b2"], "payload": bytes([want ^ 1, 0]), "snr": 1.0, "rssi": -110}))
        got = link.heard_packet(s, {"channel": 4, "path_len": 1}, now_=105.0)
        self.assertEqual(got["snr"], 5.0)
        self.assertIsNone(link.heard_packet(s, {"channel": 4, "path_len": 1}, now_=200.0))  # too old


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "a.db")
        self.s = mesh.save_settings(self.st, {"enabled": True, "connect": "tcp:x:5000", "channels": [
            {"name": "Fam", "index": 1, "kind": "family"}, {"name": "Circle", "index": 2, "kind": "group"},
            {"name": "Quiet", "index": 3, "kind": "family", "alerts": "off"}]})
        self.link = mesh.Link(self.st)

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, event, data, at):
        sent = []
        for ch in self.s["channels"]:
            a = mesh.alert_text(event, data)
            if a and self.link.send_alert(ch, event, data, *a, at=at, mono=time.monotonic()):
                sent.append(ch["name"])
        return sent

    def test_levels_quiet_hours_and_repeats(self):
        day, night = datetime(2026, 11, 23, 14, 0), datetime(2026, 11, 23, 23, 30)
        flash = {"uid": "f1", "tier": "flash", "title": "Tornado Warning", "site": "Home", "km": 0}
        prio = {"uid": "p1", "tier": "priority", "title": "Red Flag Warning", "site": "Home", "km": 12}
        self.assertEqual(self.send("situation", flash, night), ["Fam", "Circle"])   # flash: any hour, group too
        self.assertEqual(self.send("situation", flash, day), [])                     # never twice
        self.assertEqual(self.send("situation", prio, night), [])                    # waits out quiet hours
        self.assertEqual(self.send("situation", prio, day), ["Fam"])                 # group channel: flash only
        self.assertEqual(self.send("situation", {"uid": "r", "tier": "routine", "title": "x"}, day), [])
        self.assertEqual(self.send("reminder", {"title": "Renew passport"}, day), [])  # never to the mesh
        self.assertEqual(self.send("news", {"title": "Hack", "user_ids": [3], "tag": "n1"}, day), [])  # one person's watch word
        self.assertEqual(self.send("flight", {"uid": "fl", "title": "Alex landed in Denver"}, day), ["Fam"])
        with self.link.lock:
            texts = [t for _, t in self.link.outbox]
        self.assertIn("BUNKR FLASH: Tornado Warning at Home", texts)
        self.assertIn("BUNKR ALERT: Red Flag Warning 7mi from Home", texts)

    def test_cap_per_hour(self):
        day = datetime(2026, 11, 23, 14, 0)
        n = [self.send("situation", {"uid": f"x{i}", "tier": "flash", "title": f"A{i}"}, day) for i in range(mesh.ALERTS_PER_HOUR + 2)]
        self.assertEqual(sum(1 for x in n if "Fam" in x), mesh.ALERTS_PER_HOUR)


class SettingsAndSwitchTests(unittest.TestCase):
    def setUp(self):
        meshbot._recent.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "s.db")
        uid = uuid.uuid4().hex
        self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at) VALUES(?,?,?,?,?)", ("Alex", "adult", 0, uid, checkins.now()))
        self.alex = mesh.person_for(self.st, "Alex")

    def tearDown(self):
        self.tmp.cleanup()

    def test_settings(self):
        s = mesh.save_settings(self.st, {"connect": "tcp:x:5000", "bot_off": ["ask", "quake"], "brief_at": "07:15",
                                         "channels": [{"name": "Fam", "index": 1, "kind": "family", "alerts": "flash"}]})
        self.assertEqual((s["bot_off"], s["brief_at"], s["channels"][0]["alerts"]), (["ask", "quake"], "07:15", "flash"))
        for bad in ({"bot_off": ["who"]}, {"brief_at": "25:00"}, {"channels": [{"name": "F", "index": 1, "alerts": "loud"}]}):
            with self.assertRaises(ValueError):
                mesh.save_settings(self.st, bad)
        old = mesh._channel({"name": "Old", "index": 1, "kind": "group"})   # saved before alerts existed
        self.assertEqual(old["alerts"], "flash")

    def test_switched_off_and_slow_commands(self):
        ctx = {"wx": lambda rest, adult: ["sunny"], "sun": lambda rest, adult: ["rise 6:53a"], "ai": False, "off": ["sun"]}
        self.assertIsNone(meshbot.answer(self.st, self.alex, "SUN", ctx))
        later = meshbot.answer(self.st, self.alex, "!wx 80202", ctx)
        self.assertIsInstance(later, meshbot.Later)                 # needs the internet: off the radio's thread
        self.assertEqual(later.run(), ["sunny"])
        self.assertEqual(meshbot.answer(self.st, self.alex, "!ask what", ctx), "No AI is set up on this BUNKR.")
        menu = meshbot.answer(self.st, self.alex, "?", ctx)
        self.assertNotIn("SUN", menu.split("(?")[0] + "".join(meshbot.menu(p, {c for c, _ in meshbot.HELP_LINES} - {"sun", "ask"}, False) for p in (2, 3)))
        self.assertIsNone(meshbot.answer(self.st, None, "!wx 80202", ctx))  # strangers get nothing
        self.assertIsNone(meshbot.answer(self.st, self.alex, "5", ctx))     # a bare number is chat unless news was asked
        self.assertTrue(meshbot.answer(self.st, self.alex, "? 2", ctx).startswith("BUNKR 2/"))
        self.assertIsNone(meshbot.parse("? what"))

    def test_news_number_after_news(self):
        meshinfo._news_asked.clear()
        rec = {"groups": [{"headline": "One"}, {"headline": "Two two"}]}
        ctx = {"news": lambda rest, key: meshinfo.news(rec, key, rest), "news_rec": rec}
        self.assertIn("1 One.", meshbot.answer(self.st, self.alex, "NEWS", ctx)[0])
        self.assertTrue(meshbot.answer(self.st, self.alex, "2", ctx)[0].startswith("2. Two two"))

    def test_trace_through_handle(self):
        s = mesh.save_settings(self.st, {"connect": "tcp:x:5000", "channels": [{"name": "Fam", "index": 1, "kind": "family"}]})
        heard = {"path": [], "snr": 7.5, "rssi": -80}
        out = mesh.handle(self.st, {"kind": "channel", "channel": 1, "text": "Alex: TRACE", "snr": 7.5, "path_len": 0, "heard": heard},
                          s, {"me": "Home Base"})
        self.assertEqual(out, ["Trace: Alex > Home Base direct, no repeaters. SNR 7.5 RSSI -80"])


if __name__ == "__main__":
    unittest.main()
