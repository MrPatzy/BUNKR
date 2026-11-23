"""The mesh bot (WHO, HAVE, ADD, USED, READY...) and Home Assistant (events out, HOME commands in), against a fake HA."""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import audit, auth, checkins, homeassistant as ha, mesh, meshbot  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.fake-home-assistant-token"


class FakeHA:
    """Answers like Home Assistant's REST API and remembers what it was asked."""

    def __init__(self):
        calls = self.calls = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._reply({"message": "401"}, 401)
                calls.append(("GET", self.path, None))
                if self.path == "/api/":
                    return self._reply({"message": "API running."})
                if self.path == "/api/states/sensor.battery":
                    return self._reply({"state": "87", "attributes": {"unit_of_measurement": "%"}})
                return self._reply({"message": "Entity not found."}, 404)

            def do_POST(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._reply({"message": "401"}, 401)
                n = int(self.headers.get("Content-Length") or 0)
                calls.append(("POST", self.path, json.loads(self.rfile.read(n) or b"null")))
                return self._reply([] if "/services/" in self.path else {"message": "Event fired."})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class BotTests(unittest.TestCase):
    def setUp(self):
        meshbot._recent.clear()
        self.limits = meshbot.PER_PERSON, meshbot.OVERALL
        meshbot.PER_PERSON, meshbot.OVERALL = (100, 60), (100, 300)  # one test asks a lot; the limits have their own test
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.st = Store(self.folder / "b.db")
        self.people = {}
        for name, role in (("Alex", "adult"), ("Riley", "child")):
            uid = uuid.uuid4().hex
            self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at) VALUES(?,?,?,?,?)", (name, role, 0, uid, checkins.now()))
            self.people[name] = mesh.person_for(self.st, name)  # as the radio link finds them, role included
        garage = self.st.save_location({"name": "Garage", "kind": "home"})
        self.st.save_item({"name": "Drinking water", "category": "water", "quantity": 3, "unit": "jugs", "per_unit": 5, "size_unit": "gal",
                           "location_id": garage, "notes": "keep"})
        self.st.save_item({"name": "Rifle", "category": "firearms", "quantity": 1, "serial": "X1"})
        self.s = mesh.save_settings(self.st, {"connect": "tcp:127.0.0.1:5000", "channels": [{"name": "BUNKR family", "index": 7, "kind": "family"}, {"name": "Circle", "index": 2, "kind": "group"}]})
        self.fake = FakeHA()
        ha.save_settings(self.st, {"url": self.fake.url, "token": TOKEN, "actions": "lights = scene.outside_on\ngenerator = script.gen_start adults",
                                   "sensors": "battery = sensor.battery Battery"}, self.folder)
        self.ctx = {"home": lambda rest, adult: ha.home_command(self.st, self.folder, rest, adult),
                    "ready": lambda rest, adult: "50% ready."}

    def tearDown(self):
        meshbot.PER_PERSON, meshbot.OVERALL = self.limits
        self.fake.close()
        self.tmp.cleanup()

    def ask(self, who, text):
        return meshbot.answer(self.st, self.people.get(who), text, self.ctx)

    def test_commands(self):
        self.assertIsNone(meshbot.parse("dinner at 6?"))
        self.assertEqual(meshbot.parse("!have water"), ("have", "water"))
        for chat in ("got it", "home in 10", "have you eaten?", "used to be fine", "who is coming tonight", "add milk to the list"):
            self.assertIsNone(meshbot.parse(chat), chat)
        self.assertEqual(meshbot.parse("Who?"), ("who", ""))
        self.assertTrue(self.ask("Alex", "?").startswith("BUNKR 1/"))
        self.assertIn("SAFE/OMW/HELP", self.ask("Alex", "?"))
        self.assertIn("Alex ?", self.ask("Alex", "WHO"))
        self.assertEqual(self.ask("Alex", "!have water"), "Drinking water 3 jugs (15 gal) @Garage")
        self.assertIn("Nothing called rifle", self.ask("Alex", "!have rifle"))  # never firearms
        self.assertEqual(self.ask("Alex", "ready"), "50% ready.")
        self.assertEqual(self.ask("Alex", "ping"), "pong")

    def test_inventory_from_the_mesh(self):
        self.assertEqual(self.ask("Alex", "!ADD 2 drinking water"), "Now 5 jugs Drinking water")
        water = [i for i in self.st.items() if i["name"] == "Drinking water"][0]
        self.assertEqual((water["quantity"], water["notes"], water["location"]), (5, "keep", "Garage"))  # nothing else touched
        self.assertEqual(self.ask("Alex", "!add 12 cans black beans @ garage"), "Added 12 cans black beans (food) @Garage")
        self.assertIn("No place called attic", self.ask("Alex", "/add 1 tarp @ attic"))
        self.assertEqual(self.ask("Alex", "!used 2 beans"), "black beans: 10 left")
        self.assertIn("0 left", self.ask("Alex", "!used 99 beans"))
        self.assertIn("Only an adult", self.ask("Riley", "!add 1 candy bar"))
        self.assertIn("mesh:Alex", [r["username"] for r in audit.rows(self.st, 50)])

    def test_strangers_and_rate_limits(self):
        self.assertIsNone(meshbot.answer(self.st, None, "who", self.ctx))  # nothing about the family to strangers
        self.assertEqual(meshbot.answer(self.st, None, "ping", self.ctx), "pong")
        meshbot.PER_PERSON = (4, 60)
        meshbot._recent.clear()
        answers = [self.ask("Alex", "ping") for _ in range(6)]
        self.assertEqual(answers.count("pong"), 4)

    def test_home_assistant_from_the_mesh(self):
        self.assertEqual(self.ask("Alex", "!HOME"), "!home lights, generator, status")
        self.assertEqual(self.ask("Riley", "!home lights"), "Done: lights (turn on).")
        self.assertEqual(self.ask("Riley", "!home generator"), "!home generator is for adults.")
        self.assertEqual(self.ask("Alex", "!home generator"), "Done: generator (turn on).")
        self.assertEqual(self.ask("Alex", "!home status"), "Battery 87%")
        self.assertIn("No !home garage", self.ask("Alex", "!home garage"))
        posts = [(p, b) for m, p, b in self.fake.calls if m == "POST"]
        self.assertEqual(posts, [("/api/services/scene/turn_on", {"entity_id": "scene.outside_on"}),
                                 ("/api/services/script/turn_on", {"entity_id": "script.gen_start"})])

    def test_through_the_radio_handler(self):
        r = mesh.handle(self.st, {"kind": "channel", "channel": 7, "text": "Alex: !HAVE water", "snr": 6.5, "path_len": 2}, self.s, self.ctx)
        self.assertEqual(r, "Drinking water 3 jugs (15 gal) @Garage")
        r = mesh.handle(self.st, {"kind": "channel", "channel": 7, "text": "Alex: !add 1 tarp"}, self.s, self.ctx)
        self.assertEqual(r, "Added 1 tarp (shelter)")  # an adult, found by name on the radio
        self.assertEqual(mesh.handle(self.st, {"kind": "channel", "channel": 7, "text": "Alex: ping", "snr": 6.5, "path_len": 2}, self.s, self.ctx),
                         "pong (SNR 6.5, 2 hops)")
        self.assertEqual(mesh.recent(self.st)[0]["result"], "command")
        # HELP is always a check-in, never the command list
        self.assertTrue(mesh.handle(self.st, {"kind": "channel", "channel": 7, "text": "Alex: help"}, self.s, self.ctx).startswith("Got it: Alex needs help"))
        # the trusted group gets the basic commands only: no inventory, no Home Assistant
        self.assertIn("WHO, ALERTS", mesh.handle(self.st, {"kind": "channel", "channel": 2, "text": "Alex: ?"}, self.s, self.ctx))
        self.assertIn("Alex", mesh.handle(self.st, {"kind": "channel", "channel": 2, "text": "Riley: who"}, self.s, self.ctx))
        for cmd in ("!have water", "!add 1 tarp", "!home lights", "ready"):
            self.assertIsNone(mesh.handle(self.st, {"kind": "channel", "channel": 2, "text": "Alex: " + cmd}, self.s, self.ctx), cmd)
        off = mesh.save_settings(self.st, {"channels": [c | {"bot": "off"} for c in self.s["channels"]]})
        self.assertIsNone(mesh.handle(self.st, {"kind": "channel", "channel": 7, "text": "Alex: who"}, off, self.ctx))

    def test_events_to_home_assistant(self):
        ev = ha.Events(lambda: self.st, lambda: self.folder)
        ev.send(*ha.event_for("checkin", {"person_name": "Alex", "status": "safe", "via": "mesh", "note": "home"}))
        ev.send(*ha.event_for("call", {"kind": "evacuate", "label": "Evacuate to", "place": "Grandma's", "drill": False}))
        self.assertIsNone(ha.event_for("situation", {"tier": "routine"}))
        posts = [(p, b) for m, p, b in self.fake.calls if m == "POST"]
        self.assertEqual(posts[0], ("/api/events/bunkr_checkin", {"person": "Alex", "status": "safe", "note": "home", "place": "", "via": "mesh"}))
        self.assertEqual(posts[1][0], "/api/events/bunkr_call")
        self.assertEqual(ha.status["last"], "bunkr_call")

    def test_settings_checks(self):
        rows, errors = ha.parse_lines("lights = scene.x\nbad line\nstatus = sensor.y", "actions")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(errors), 2)
        self.assertEqual(ha.parse_lines("door = cover.garage close_cover adults", "actions")[0][0],
                         {"word": "door", "entity_id": "cover.garage", "service": "close_cover", "adults": True})
        for bad in ({"url": "javascript:alert(1)"}, {"token": "short"}, {"actions": "x = notanentity"}):
            with self.assertRaises(ValueError):
                ha.save_settings(self.st, bad, self.folder)
        self.assertEqual(oct((self.folder / ha.TOKEN_FILE).stat().st_mode & 0o777), "0o600")
        self.assertNotIn(TOKEN, self.st.get_raw("ha_settings", ""))


class HAHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        self.st = self.hub.store
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {u: self.login(u) for u in ("dad", "kid")}
        self.fake = FakeHA()

    def tearDown(self):
        self.fake.close()
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

    def test_admin_connects_home_assistant(self):
        self.assertEqual(self.req("GET", "/api/homeassistant", user="kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/homeassistant/test")[0], 400)
        code, r = self.req("POST", "/api/homeassistant", {"url": self.fake.url, "token": TOKEN, "actions": "lights = scene.a"})
        self.assertEqual(code, 200)
        r = self.req("GET", "/api/homeassistant")[1]
        self.assertEqual((r["token"], r["actions"]), (True, "lights = scene.a turn_on"))
        self.assertNotIn(TOKEN, json.dumps(r))
        code, t = self.req("POST", "/api/homeassistant/test")
        self.assertEqual((code, t["message"]), (200, "API running."))
        self.assertIn(("POST", "/api/events/bunkr_test", {"from": "BUNKR"}), self.fake.calls)


if __name__ == "__main__":
    unittest.main()
