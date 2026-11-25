"""The phone app, hub side: pairing, phone keys, the phones list, the notification stream, check-ins with a location."""

import http.client
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bunkr import auth, checkins, notify, phone, server  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

_preview = mock.patch.dict(os.environ, {"BUNKR_PREVIEW": "phone_app"})   # held back from 1.3.0: off unless switched on


def setUpModule():
    _preview.start()


def tearDownModule():
    _preview.stop()


def iso(d):
    return d.isoformat(timespec="seconds")


class Base(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        self.st = self.hub.store
        server._app_pair_failures.clear()
        self.dad = auth.create_user(self.st, "dad", p3.PW, "admin")
        self.mom = auth.create_user(self.st, "mom", p3.PW, "family")
        self.dad_uid, self.mom_uid = uuid.uuid4().hex, uuid.uuid4().hex
        self.st._exec("INSERT INTO people(name, role, uid, user_id) VALUES('Alex','adult',?,?)", (self.dad_uid, self.dad))
        self.st._exec("INSERT INTO people(name, role, uid, user_id) VALUES('Wife','adult',?,?)", (self.mom_uid, self.mom))
        self.opener = urllib.request.build_opener(p3.NoRedirect)

    def tearDown(self):
        checkins.listeners.clear()
        self.hub.close()

    def call(self, method, path, body=None, cookie=None, key=None):
        h = {"X-Bunkr": "1", "Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = f"bunkr_session={cookie}"
        if key:
            h["Authorization"] = f"Bearer {key}"
        r = urllib.request.Request(self.hub.url + path, method=method, headers=h,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers

    def login(self, name):
        st, _, hd = self.call("POST", "/api/login", {"username": name, "password": p3.PW})
        self.assertEqual(st, 200)
        return hd["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def pair(self, name="mom", phone_name="Mom's phone"):
        cookie = self.login(name)
        st, r, _ = self.call("POST", "/api/app/code", {}, cookie=cookie)
        self.assertEqual(st, 200)
        st, got, _ = self.call("POST", "/api/app/pair", {"code": r["code"], "name": phone_name})
        self.assertEqual(st, 200, got)
        return cookie, got

    def stream(self, key, last=None):
        c = http.client.HTTPConnection("127.0.0.1", int(self.hub.url.rsplit(":", 1)[1]), timeout=10)
        h = {"Authorization": f"Bearer {key}", "X-Bunkr": "1"}
        if last is not None:
            h["Last-Event-ID"] = str(last)
        c.request("GET", "/api/app/stream", headers=h)
        resp = c.getresponse()
        return c, resp

    @staticmethod
    def read_events(resp, n):
        """The next n 'notice' events (and nothing else) from an open stream."""
        out, ev = [], {}
        while len(out) < n:
            line = resp.fp.readline().decode()
            if not line:
                break
            line = line.rstrip("\n")
            if line.startswith(":"):
                continue
            if line == "":
                if ev.get("event") == "notice":
                    out.append(json.loads(ev["data"]) | {"sse_id": ev.get("id")})
                ev = {}
                continue
            k, _, v = line.partition(": ")
            ev[k] = v
        return out


class PairingTests(Base):
    def test_code_is_short_lived_and_works_once(self):
        cookie = self.login("mom")
        st, r, _ = self.call("POST", "/api/app/code", {}, cookie=cookie)
        self.assertEqual(st, 200)
        self.assertEqual(r["minutes"], 5)
        self.assertTrue(r["link"].startswith("bunkr://pair?code="))
        self.assertIn(self.hub.url, r["link"].replace("%3A", ":").replace("%2F", "/"))
        self.assertTrue(r["svg"].startswith("<svg"))
        st, got, _ = self.call("POST", "/api/app/pair", {"code": r["code"], "name": "Pixel"})
        self.assertEqual(st, 200)
        self.assertTrue(got["key"].startswith("bkp_"))
        self.assertEqual((got["username"], got["role"]), ("mom", "family"))
        st, _, _ = self.call("POST", "/api/app/pair", {"code": r["code"]})   # used
        self.assertEqual(st, 403)

    def test_expired_and_wrong_codes_are_refused(self):
        cookie = self.login("mom")
        _, r, _ = self.call("POST", "/api/app/code", {}, cookie=cookie)
        self.st._exec("UPDATE phone_codes SET expires_at=?", (iso(datetime.now(timezone.utc) - timedelta(seconds=1)),))
        self.assertEqual(self.call("POST", "/api/app/pair", {"code": r["code"]})[0], 403)
        self.assertEqual(self.call("POST", "/api/app/pair", {"code": "AAAA-BBBB-CCCC"})[0], 403)
        self.assertEqual(self.call("POST", "/api/app/pair", {})[0], 403)
        self.assertEqual(self.st._rows("SELECT COUNT(*) AS n FROM phone_keys")[0]["n"], 0)

    def test_a_new_code_replaces_the_old_one(self):
        cookie = self.login("mom")
        _, a, _ = self.call("POST", "/api/app/code", {}, cookie=cookie)
        _, b, _ = self.call("POST", "/api/app/code", {}, cookie=cookie)
        self.assertEqual(self.call("POST", "/api/app/pair", {"code": a["code"]})[0], 403)
        self.assertEqual(self.call("POST", "/api/app/pair", {"code": b["code"]})[0], 200)

    def test_wrong_tries_are_limited(self):
        for _ in range(10):
            self.assertEqual(self.call("POST", "/api/app/pair", {"code": "NOPE-NOPE-NOPE"})[0], 403)
        self.assertEqual(self.call("POST", "/api/app/pair", {"code": "NOPE-NOPE-NOPE"})[0], 429)
        cookie = self.login("mom")   # even a right code waits out the limit
        _, r, _ = self.call("POST", "/api/app/code", {}, cookie=cookie)
        self.assertEqual(self.call("POST", "/api/app/pair", {"code": r["code"]})[0], 429)

    def test_asking_for_a_code_needs_a_login(self):
        self.assertEqual(self.call("POST", "/api/app/code", {})[0], 401)

    def test_only_a_hash_of_the_key_is_stored(self):
        _, got = self.pair()
        dump = json.dumps(self.st._rows("SELECT * FROM phone_keys") + self.st._rows("SELECT * FROM phone_codes"))
        self.assertNotIn(got["key"], dump)
        self.assertNotIn(got["key"][len(phone.KEY_PREFIX):], dump)


class KeyTests(Base):
    def test_key_signs_in_as_that_person_with_their_access(self):
        _, got = self.pair("mom")
        st, me, _ = self.call("GET", "/api/me", key=got["key"])
        self.assertEqual((st, me["username"], me["role"]), (200, "mom", "family"))
        self.assertEqual(self.call("GET", "/api/state", key=got["key"])[0], 200)
        self.assertEqual(self.call("GET", "/api/users", key=got["key"])[0], 403)   # a family login can't, so neither can her phone
        _, got = self.pair("dad", "Dad's phone")
        self.assertEqual(self.call("GET", "/api/users", key=got["key"])[0], 200)

    def test_bad_keys_do_not_sign_in(self):
        self.assertEqual(self.call("GET", "/api/me")[0], 401)
        self.assertEqual(self.call("GET", "/api/me", key="bkp_nonsense")[0], 401)
        self.assertEqual(self.call("GET", "/api/me", key="other-token")[0], 401)

    def test_the_app_cannot_mint_more_pairing_codes(self):
        _, got = self.pair()
        self.assertEqual(self.call("POST", "/api/app/code", {}, key=got["key"])[0], 403)

    def test_use_renews_and_old_unused_keys_lapse(self):
        _, got = self.pair()
        old = iso(datetime.now(timezone.utc) - timedelta(days=phone.KEY_DAYS - 1))
        self.st._exec("UPDATE phone_keys SET last_seen=?", (old,))
        phone._touched.clear()
        self.assertEqual(self.call("GET", "/api/me", key=got["key"])[0], 200)
        self.assertGreater(self.st._rows("SELECT last_seen FROM phone_keys")[0]["last_seen"], old)   # renewed
        self.st._exec("UPDATE phone_keys SET last_seen=?", (iso(datetime.now(timezone.utc) - timedelta(days=phone.KEY_DAYS + 1)),))
        phone._touched.clear()
        self.assertEqual(self.call("GET", "/api/me", key=got["key"])[0], 401)
        self.assertEqual(self.st._rows("SELECT COUNT(*) AS n FROM phone_keys")[0]["n"], 0)

    def test_deleting_the_person_removes_their_phones(self):
        _, got = self.pair()
        auth.delete_user(self.st, self.mom)
        self.assertEqual(self.call("GET", "/api/me", key=got["key"])[0], 401)


class PhonesListTests(Base):
    def test_list_and_remove(self):
        mom, a = self.pair("mom", "Mom's phone")
        dad, b = self.pair("dad", "Dad's phone")
        st, r, _ = self.call("GET", "/api/app/phones", cookie=mom)
        self.assertEqual([p["name"] for p in r["phones"]], ["Mom's phone"])
        self.assertTrue(all(k in r["phones"][0] for k in ("created_at", "last_seen", "connected")))
        self.assertNotIn("key", json.dumps(r))
        st, r, _ = self.call("GET", "/api/app/phones?all=1", cookie=mom)   # not an admin: still just her own
        self.assertEqual(len(r["phones"]), 1)
        st, r, _ = self.call("GET", "/api/app/phones?all=1", cookie=dad)
        self.assertEqual(sorted(p["username"] for p in r["phones"]), ["dad", "mom"])
        dad_phone = next(p["id"] for p in r["phones"] if p["username"] == "dad")
        mom_phone = next(p["id"] for p in r["phones"] if p["username"] == "mom")
        self.assertEqual(self.call("DELETE", f"/api/app/phones/{dad_phone}", cookie=mom)[0], 403)   # not hers
        self.assertEqual(self.call("DELETE", "/api/app/phones/9999", cookie=mom)[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/app/phones/{mom_phone}", cookie=mom)[0], 200)
        self.assertEqual(self.call("GET", "/api/me", key=a["key"])[0], 401)   # cut off at once
        self.assertEqual(self.call("GET", "/api/me", key=b["key"])[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/app/phones/{dad_phone}", cookie=dad)[0], 200)

    def test_an_admin_can_remove_anyones_phone(self):
        mom, a = self.pair("mom")
        dad = self.login("dad")
        pid = self.st._rows("SELECT id FROM phone_keys")[0]["id"]
        self.assertEqual(self.call("DELETE", f"/api/app/phones/{pid}", cookie=dad)[0], 200)
        self.assertEqual(self.call("GET", "/api/me", key=a["key"])[0], 401)


class StreamTests(Base):
    def hook(self):
        """What the hub does: listeners hand every check-in and call to the notifier, which feeds the phone stream."""
        checkins.listeners.append(lambda ev, d: notify.send(self.st, None, ev, d, push=False, phone=True))

    def test_stream_needs_a_phone_key(self):
        cookie = self.login("mom")
        self.assertEqual(self.call("GET", "/api/app/stream", cookie=cookie)[0], 403)
        self.assertEqual(self.call("GET", "/api/app/stream")[0], 401)

    def test_call_checkin_and_alert_arrive_with_id_and_screen(self):
        self.hook()
        _, got = self.pair("mom")
        conn, resp = self.stream(got["key"])
        self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, "text/event-stream"))
        with mock.patch.object(notify, "deliver", side_effect=AssertionError("no outside calls")):
            checkins.start_call(self.st, "evacuate", "dad", place="Grandma's")
            checkins.add_checkin(self.st, self.dad_uid, "safe", "At the park")
            notify.send(self.st, None, "situation", {"tier": "flash", "title": "Tornado warning", "uid": "abc 1", "site": "Home",
                                                     "km": 0, "summary": "Take cover"}, push=False, phone=True)
            evs = self.read_events(resp, 3)
        conn.close()
        self.assertEqual([e["event"] for e in evs], ["call", "checkin", "situation"])
        self.assertEqual([e["screen"] for e in evs], ["home", "home", "sit-abc%201"])
        self.assertEqual([e["id"] for e in evs], sorted(e["id"] for e in evs))
        self.assertTrue(all(e["id"] == int(e["sse_id"]) and e["title"] and e["url"] for e in evs))
        self.assertIn("Grandma's", evs[0]["title"])

    def test_nothing_for_other_people_or_your_own_checkin(self):
        self.hook()
        _, got = self.pair("mom")
        conn, resp = self.stream(got["key"])
        checkins.add_checkin(self.st, self.mom_uid, "safe")          # her own: she isn't told
        checkins.add_checkin(self.st, self.dad_uid, "help", "Hurt")  # help always notifies
        evs = self.read_events(resp, 1)
        conn.close()
        self.assertEqual(len(evs), 1)
        self.assertIn("Alex", evs[0]["title"])
        self.assertEqual(phone.events_after(self.st, self.dad, 0), [])   # dad has no paired phone: nothing kept for him

    def test_reconnect_with_last_id_gets_what_was_missed(self):
        self.hook()
        _, got = self.pair("mom")
        conn, resp = self.stream(got["key"])
        checkins.add_checkin(self.st, self.dad_uid, "help", "one")
        first = self.read_events(resp, 1)[0]
        conn.close()
        checkins.add_checkin(self.st, self.dad_uid, "help", "two")   # while the phone was away
        checkins.add_checkin(self.st, self.dad_uid, "help", "three")
        conn, resp = self.stream(got["key"], last=first["id"])
        evs = self.read_events(resp, 2)
        conn.close()
        self.assertEqual([e["body"] for e in evs], ["two", "three"])
        # no last id: only what happens from now on, not the old ones
        conn, resp = self.stream(got["key"])
        checkins.add_checkin(self.st, self.dad_uid, "help", "four")
        self.assertEqual([e["body"] for e in self.read_events(resp, 1)], ["four"])
        conn.close()

    def test_removing_the_phone_closes_its_stream(self):
        _, got = self.pair("mom")
        conn, resp = self.stream(got["key"])
        resp.fp.readline()   # the "ready" event
        self.assertTrue(phone.phones(self.st)[0]["connected"])
        phone.remove(self.st, got["id"])
        rest = resp.read()   # ends, rather than waiting for the next heartbeat
        conn.close()
        self.assertNotIn(b"notice", rest)
        self.assertEqual(self.call("GET", "/api/app/stream", key=got["key"])[0], 401)


class EventUnitTests(Base):
    def test_backlog_is_short(self):
        _, got = self.pair("mom")
        for i in range(phone.BACKLOG + 5):
            phone.publish(self.st, self.mom, "test", {"title": f"t{i}", "tier": "family", "url": "/#home"})
        evs = phone.events_after(self.st, self.mom, 0, limit=1000)
        self.assertEqual(len(evs), phone.BACKLOG)
        self.assertEqual(evs[-1]["title"], f"t{phone.BACKLOG + 4}")
        self.st._exec("UPDATE app_events SET at=?", (iso(datetime.now(timezone.utc) - timedelta(days=phone.BACKLOG_DAYS + 1)),))
        phone.publish(self.st, self.mom, "test", {"title": "new"})
        self.assertEqual([e["title"] for e in phone.events_after(self.st, self.mom, 0)], ["new"])

    def test_no_phone_no_events_kept(self):
        self.assertIsNone(phone.publish(self.st, self.dad, "test", {"title": "x"}))

    def test_heartbeat_when_quiet(self):
        _, got = self.pair("mom")
        it = phone.follow(self.st, got["id"], None, beat=0.05)
        self.assertIn(b"event: ready", next(it))
        self.assertEqual(next(it), b": heartbeat\n\n")
        it.close()

    def test_a_last_id_from_the_future_starts_over(self):
        _, got = self.pair("mom")
        phone.publish(self.st, self.mom, "test", {"title": "x"})
        it = phone.follow(self.st, got["id"], 99999, beat=0.05)
        self.assertIn(b'"last_id":1', next(it).replace(b" ", b""))
        it.close()

    def test_hub_addresses_fit_a_qr_code(self):
        link = phone.pair_link("AAAA-BBBB-CCCC", ["http://bunkr.local:8080", "https://" + "x" * 300 + ".example"])
        self.assertLessEqual(len(link.encode()), 200)
        self.assertIn("bunkr.local", link)
        from bunkr import qr
        qr.svg(link)

    def test_a_code_made_through_a_tunnel_also_carries_the_home_address(self):
        """A QR code made at a tunnel address (which may sit behind Cloudflare Access, which the app can't get
        through) still lists the hub's own address on the network the tunnel reached it by, first."""
        class Conn:
            def __init__(self, addr):
                self.addr = addr

            def getsockname(self):
                return self.addr

        def addresses(sock, host="bunkr.example.org", proto="https"):
            h = server.Handler.__new__(server.Handler)
            h.connection = Conn(sock)
            h.headers = {"Host": host, "X-Forwarded-Proto": proto}
            return server.Handler._hub_addresses(h)

        self.assertEqual(addresses(("192.168.1.40", 8765)), ["http://192.168.1.40:8765", "https://bunkr.example.org"])
        self.assertEqual(addresses(("::ffff:192.168.1.40", 8765, 0, 0)), ["http://192.168.1.40:8765", "https://bunkr.example.org"])
        self.assertEqual(addresses(("fd7a:115c::5", 8765, 0, 0)), ["http://[fd7a:115c::5]:8765", "https://bunkr.example.org"])
        self.assertEqual(addresses(("127.0.0.1", 8765)), ["https://bunkr.example.org"])   # behind a proxy on this machine
        # browsing straight to the hub: one address, not two
        self.assertEqual(addresses(("192.168.1.40", 8765), "192.168.1.40:8765", ""), ["http://192.168.1.40:8765"])


class CheckinLocationTests(Base):
    def test_checkin_with_latitude_longitude_and_accuracy(self):
        _, got = self.pair("mom")
        st, rec, _ = self.call("POST", "/api/checkin", {"status": "safe", "note": "Home", "lat": 39.7392, "lon": -104.9903,
                                                         "accuracy": 12.34, "via": "app"}, key=got["key"])
        self.assertEqual(st, 200, rec)
        self.assertEqual((rec["lat"], rec["lon"], rec["accuracy"]), (39.7392, -104.9903, 12.3))
        st, board, _ = self.call("GET", "/api/board", key=got["key"])
        last = next(p["last"] for p in board["people"] if p["uid"] == self.mom_uid)
        self.assertEqual((last["lat"], last["lon"], last["accuracy"]), (39.7392, -104.9903, 12.3))
        self.assertEqual(checkins.export(self.st)["checkins"][0]["accuracy"], 12.3)

    def test_bad_accuracy_is_ignored(self):
        for bad in ("far", -5, None, 1e12):
            rec = checkins.add_checkin(self.st, self.mom_uid, "safe", lat=1, lon=2, accuracy=bad)
            self.assertIsNone(rec["accuracy"])
        rec = checkins.add_checkin(self.st, self.mom_uid, "safe", accuracy=10)   # no location: no accuracy either
        self.assertIsNone(rec["accuracy"])

    def test_sync_carries_accuracy(self):
        checkins.add_checkin(self.st, self.mom_uid, "safe", lat=1, lon=2, accuracy=7)
        out = checkins.export(self.st)
        self.st._exec("DELETE FROM checkins")
        checkins.apply(self.st, out)
        self.assertEqual(self.st._rows("SELECT accuracy FROM checkins")[0]["accuracy"], 7)


class AppFileAndSessionTests(Base):
    def put_apk(self, n, data=b"PK-fake"):
        d = self.hub.depot.root / "apps" / "bunkr-android" / str(n)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"bunkr-{n}.apk").write_bytes(data)

    def raw(self, path, key=None, cookie=None):
        h = {}
        if key:
            h["Authorization"] = f"Bearer {key}"
        if cookie:
            h["Cookie"] = f"bunkr_session={cookie}"
        try:
            with self.opener.open(urllib.request.Request(self.hub.url + path, headers=h)) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_no_app_file_yet(self):
        cookie, _ = self.pair()
        st, r, _ = self.call("GET", "/api/app/version", cookie=cookie)
        self.assertEqual((st, r), (200, {"available": False}))
        self.assertEqual(self.raw("/app/bunkr.apk", cookie=cookie)[0], 404)

    def test_newest_app_file_is_offered_with_its_checksum(self):
        import hashlib
        cookie, _ = self.pair()
        self.put_apk(9, b"old")
        self.put_apk(12, b"newer file")
        st, r, _ = self.call("GET", "/api/app/version", cookie=cookie)
        self.assertEqual((r["version"], r["size"], r["url"]), (12, 10, "/app/bunkr.apk"))
        self.assertEqual(r["sha256"], hashlib.sha256(b"newer file").hexdigest())
        st, body, hd = self.raw("/app/bunkr.apk", cookie=cookie)
        self.assertEqual((st, body), (200, b"newer file"))
        self.assertEqual(hd["Content-Type"], "application/vnd.android.package-archive")

    def test_app_file_needs_a_login_but_a_phone_key_will_do(self):
        _, got = self.pair()
        self.put_apk(3)
        st, _, hd = self.raw("/app/bunkr.apk")
        self.assertEqual(st, 303)
        self.assertIn("/login.html", hd["Location"])
        self.assertEqual(self.raw("/app/bunkr.apk", key=got["key"])[0], 200)

    def test_phone_key_opens_a_normal_session_for_the_web_view(self):
        _, got = self.pair("mom")
        st, _, hd = self.call("POST", "/api/app/open", {}, key=got["key"])
        self.assertEqual(st, 200)
        cookie = hd["Set-Cookie"].split(";")[0].split("=", 1)[1]
        st, me, _ = self.call("GET", "/api/me", cookie=cookie)
        self.assertEqual((st, me["username"], me["role"]), (200, "mom", "family"))

    def test_a_browser_login_can_not_use_open_and_a_bad_key_gets_nothing(self):
        cookie, _ = self.pair("mom")
        self.assertEqual(self.call("POST", "/api/app/open", {}, cookie=cookie)[0], 403)
        st, _, hd = self.call("POST", "/api/app/open", {}, key="bkp_wrong")
        self.assertEqual(st, 401)
        self.assertIsNone(hd.get("Set-Cookie"))

    def test_removed_phone_cannot_open_a_session(self):
        cookie, got = self.pair("mom")
        self.call("DELETE", f"/api/app/phones/{got['id']}", cookie=cookie)
        self.assertEqual(self.call("POST", "/api/app/open", {}, key=got["key"])[0], 401)

    def test_the_app_is_in_the_downloads_catalog(self):
        from bunkr import releases
        e = releases.BY_ID["bunkr-android"]
        self.assertEqual(e["kind"], "app")
        rel = {"tag_name": "app-v14", "assets": [{"name": "bunkr-14.apk", "browser_download_url": "u", "size": 1}]}
        other = {"tag_name": "v1.2.3", "assets": [{"name": "bunkr_1.2.3_all.deb", "browser_download_url": "u", "size": 1}]}
        got = releases.latest(e, {"MrPatzy/BUNKR": [other, rel]})
        self.assertEqual((got["version"], got["assets"][0]["name"]), ("14", "bunkr-14.apk"))


if __name__ == "__main__":
    unittest.main()
