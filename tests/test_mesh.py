"""MeshCore check-ins: frames, reading messages, matching people, what goes out, and the whole link against a simulated
companion radio on TCP (the same frames a Heltec on Wi-Fi speaks)."""

import base64
import json
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, checkins, mesh, meshcore as mc  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402


def out_frame(payload):
    return b">" + struct.pack("<H", len(payload)) + payload


def contact(key_byte, name, node_type=1, hops=1, lat=39.74, lon=-104.99, lastmod=100, code=mc.CONTACT):
    """A contact record as the firmware writes it: 148 bytes with a 64-byte out_path."""
    return (bytes([code]) + bytes([key_byte]) * 32 + bytes([node_type, 0, hops]) + bytes(64) + name.encode().ljust(32, b"\0")
            + struct.pack("<IiiI", 1_800_000_000, int(lat * 1e6), int(lon * 1e6), lastmod))


def channel_msg(idx, text, ts=1_800_000_000, snr=10):
    return bytes([mc.CHANNEL_MSG_V3, (snr * 4) & 0xFF, 0, 0, idx, 1, 0]) + struct.pack("<I", ts) + text.encode()


class FakeRadio:
    """A MeshCore companion radio on a TCP port: answers the commands BUNKR uses and holds queued messages."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.channels = {0: ("Public", bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72"))}
        self.inbox, self.sent, self.conn = [], [], None
        self.contacts = [contact(0xA1, "T-Deck 7F"), contact(0xB2, "Roof RAK", node_type=2, hops=0)]
        self.lock = threading.Lock()
        threading.Thread(target=self.serve, daemon=True).start()

    def advert(self, record):
        """A node the radio hasn't seen before sends its advert (the radio pushes the whole record)."""
        if self.conn:
            self.conn.sendall(out_frame(record))

    def push(self, text, idx=7):
        with self.lock:
            self.inbox.append(channel_msg(idx, text))
            if self.conn:
                self.conn.sendall(out_frame(bytes([mc.MESSAGES_WAITING])))

    def reply(self, cmd):
        c = cmd[0]
        if c == mc.CMD_APP_START:
            return (bytes([mc.SELF_INFO, 1, 22, 22]) + bytes(range(32)) + bytes(8) + bytes(4)
                    + struct.pack("<II", 910525, 62500) + bytes([7, 5]) + b"BUNKR Hub")
        if c == mc.CMD_DEVICE_QUERY:
            return (bytes([mc.DEVICE_INFO, 9, 175, 8]) + bytes(4) + b"build".ljust(12, b"\0")
                    + b"Heltec V3".ljust(40, b"\0") + b"v1.18.0".ljust(20, b"\0") + bytes(2))
        if c == mc.CMD_GET_CHANNEL:
            name, sec = self.channels.get(cmd[1], ("", bytes(16)))
            return bytes([mc.CHANNEL_INFO, cmd[1]]) + name.encode().ljust(32, b"\0") + sec
        if c == mc.CMD_SET_CHANNEL:
            self.channels[cmd[1]] = (cmd[2:34].split(b"\0")[0].decode(), cmd[34:50])
            return bytes([mc.OK])
        if c == mc.CMD_GET_CONTACTS:
            since = struct.unpack("<I", cmd[1:5])[0] if len(cmd) >= 5 else 0
            fresh = [x for x in self.contacts if struct.unpack("<I", x[-4:])[0] > since]
            return ([bytes([mc.CONTACTS_START]) + struct.pack("<I", len(self.contacts))] + fresh
                    + [bytes([mc.END_OF_CONTACTS]) + struct.pack("<I", max([struct.unpack("<I", x[-4:])[0] for x in fresh] or [since]))])
        if c == mc.CMD_GET_MESSAGE:
            with self.lock:
                return self.inbox.pop(0) if self.inbox else bytes([mc.NO_MORE_MSGS])
        if c == mc.CMD_SEND_CHANNEL_TXT:
            self.sent.append((cmd[2], cmd[7:].decode()))
            return bytes([mc.MSG_SENT, 1]) + bytes(8)
        return bytes([mc.ERROR, 1])

    def serve(self):
        self.conn, _ = self.srv.accept()
        d, buf = None, b""
        while True:
            try:
                data = self.conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            buf += data
            while len(buf) >= 3 and buf[0:1] == b"<":
                n = struct.unpack("<H", buf[1:3])[0]
                if len(buf) < 3 + n:
                    break
                cmd, buf = buf[3:3 + n], buf[3 + n:]
                try:
                    r = self.reply(cmd)
                    self.conn.sendall(b"\x88noise" + b"".join(out_frame(x) for x in (r if isinstance(r, list) else [r])))  # stray bytes the deframer must skip
                except OSError:  # the test closed us
                    return

    def close(self):
        for s in (self.conn, self.srv):
            try:
                s and s.close()
            except OSError:
                pass


def wait_for(fn, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


class FrameTests(unittest.TestCase):
    def test_deframer_and_parsing(self):
        d = mc.Deframer()
        raw = out_frame(channel_msg(7, "Alex: SAFE"))
        got = d.feed(b"junk>" + raw[:5]) + d.feed(raw[5:])
        self.assertEqual(len(got), 1)
        p = mc.parse(got[0])
        self.assertEqual((p["kind"], p["channel"], p["text"], p["snr"]), ("channel", 7, "Alex: SAFE", 10.0))
        direct = bytes([mc.CONTACT_MSG_V3, 8, 0, 0]) + bytes.fromhex("a1b2c3d4e5f6") + bytes([2, 2]) + struct.pack("<I", 5) + b"SIGN" + b"hi"
        p = mc.parse(direct)
        self.assertEqual((p["kind"], p["key_prefix"], p["text"]), ("direct", "a1b2c3d4e5f6", "hi"))
        self.assertTrue(mc.parse(bytes([mc.CHANNEL_MSG_V3, 1])).get("bad"))
        self.assertEqual(mc.frame(b"\x0a"), b"<\x01\x00\x0a")
        with self.assertRaises(ValueError):
            mc.set_channel(1, "x", bytes(16))  # never a zero secret
        self.assertEqual(len(mc.fit("x" * 500)), mc.MAX_TEXT)


class ContactFrameTests(unittest.TestCase):
    def test_contact_record(self):
        p = mc.parse(contact(0xA1, "T-Deck 7F", node_type=1, hops=2, lat=39.7392, lon=-104.9903, lastmod=77))
        self.assertEqual((p["public_key"], p["node_type"], p["hops"], p["name"], p["lastmod"]), ("a1" * 32, 1, 2, "T-Deck 7F", 77))
        self.assertAlmostEqual(p["lat"], 39.7392, places=5)
        self.assertAlmostEqual(p["lon"], -104.9903, places=5)
        flood = mc.parse(contact(0xA1, "x", hops=0xFF, lat=0, lon=0))
        self.assertEqual((flood["hops"], flood["lat"]), (None, None))  # no path yet, no location shared
        self.assertEqual(mc.parse(bytes([mc.PUSH_ADVERT]) + bytes([7]) * 32)["public_key"], "07" * 32)
        self.assertEqual(mc.parse(bytes([mc.END_OF_CONTACTS]) + struct.pack("<I", 9))["lastmod"], 9)
        self.assertNotIn("name", mc.parse(bytes([mc.CONTACT]) + bytes(40)))  # too short: ignored, not misread
        self.assertEqual(mc.get_contacts(), bytes([4]))
        self.assertEqual(mc.get_contacts(5), bytes([4, 5, 0, 0, 0]))

    def test_frames_in_one_read_are_kept(self):
        """The radio often sends a reply and what follows it in one burst: nothing after the answer may be lost."""
        a, b = socket.socketpair()
        conn = mc.Connection.__new__(mc.Connection)
        conn.where, conn.timeout, conn.fd, conn.sock, conn.deframer, conn.pending = "test", 2.0, None, a, mc.Deframer(), []
        a.setblocking(False)
        burst = [bytes([mc.CONTACTS_START]) + struct.pack("<I", 2), contact(0xA1, "One"), contact(0xA2, "Two"),
                 bytes([mc.END_OF_CONTACTS]) + struct.pack("<I", 5), bytes([mc.MESSAGES_WAITING])]
        b.sendall(b"".join(out_frame(x) for x in burst))
        found, since = mc.Radio(conn).contacts()
        self.assertEqual(([c["name"] for c in found], since), (["One", "Two"], 5))
        self.assertEqual([x["type"] for x in conn.poll()], [mc.MESSAGES_WAITING])
        a.close(); b.close()


class NodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "n.db")
        self.uid = {}
        for name in ("Jordan", "Mom", "Alex"):
            self.uid[name] = uuid.uuid4().hex
            self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at) VALUES(?,?,?,?,?)",
                          (name, "adult", 1, self.uid[name], checkins.now()))
        self.s = mesh.save_settings(self.st, {"connect": "/dev/ttyUSB0", "channels": [{"name": "Circle", "index": 2, "kind": "group"}]})
        recs = [mc.parse(contact(0xA1, "T-Deck 7F")), mc.parse(contact(0xA2, "Ty handheld")), mc.parse(contact(0xC3, "Base 3", node_type=1)),
                mc.parse(contact(0xB2, "Roof RAK", node_type=2))]
        self.assertEqual(mesh.save_nodes(self.st, recs), 4)

    def tearDown(self):
        self.tmp.cleanup()

    def test_names_that_dont_match(self):
        self.assertIsNone(mesh.person_for(self.st, "T-Deck 7F"))
        mesh.assign_node(self.st, "a1" * 32, self.uid["Jordan"])
        mesh.assign_node(self.st, "a2" * 32, self.uid["Jordan"], label="truck")  # more than one node each
        mesh.assign_node(self.st, "c3" * 32, self.uid["Mom"])
        r = mesh.handle(self.st, {"kind": "channel", "channel": 2, "text": "T-Deck 7F: SAFE at work"}, self.s)
        self.assertEqual(r, "Got it: Jordan is safe.")
        self.assertEqual(mesh.person_for(self.st, "ty handheld")["name"], "Jordan")
        self.assertEqual(mesh.person_for(self.st, "Base 3")["name"], "Mom")
        rows = {n["name"]: n for n in mesh.nodes(self.st)}
        self.assertEqual((rows["T-Deck 7F"]["person_name"], rows["Ty handheld"]["label"]), ("Jordan", "truck"))
        self.assertTrue(rows["T-Deck 7F"]["last_heard"])
        self.assertEqual(rows["Roof RAK"]["type_name"], "repeater")
        self.assertEqual([n["person_uid"] != "" for n in mesh.nodes(self.st)], [True, True, True, False])  # people's first
        # the node renames itself: the next advert updates the name, the owner stays
        mesh.save_nodes(self.st, [mc.parse(contact(0xA1, "Jordan T-Deck"))])
        self.assertEqual(mesh.person_for(self.st, "Jordan T-Deck")["name"], "Jordan")
        self.assertIsNone(mesh.person_for(self.st, "T-Deck 7F"))

    def test_a_shared_name_isnt_trusted(self):
        mesh.save_nodes(self.st, [mc.parse(contact(0xD4, "T-Deck 7F"))])
        mesh.assign_node(self.st, "a1" * 32, self.uid["Jordan"])
        mesh.assign_node(self.st, "d4" * 32, self.uid["Mom"])
        self.assertIsNone(mesh.person_for(self.st, "T-Deck 7F"))  # two people's nodes: can't say which
        self.assertTrue(all(n["name_clash"] for n in mesh.nodes(self.st) if n["name"] == "T-Deck 7F"))

    def test_by_hand_and_forget(self):
        key = "e5" * 32
        mesh.add_node(self.st, f"meshcore://contact/add?name=Mom%20house&public_key={key}&type=1", "Mom house", self.uid["Mom"])
        self.assertEqual(mesh.person_for(self.st, "Mom house")["name"], "Mom")
        mesh.save_nodes(self.st, [mc.parse(contact(0xE5, "Mom house"))])  # heard later: still Mom's
        self.assertEqual([n["source"] for n in mesh.nodes(self.st) if n["public_key"] == key], ["radio"])
        self.assertEqual(mesh.person_for(self.st, "Mom house")["name"], "Mom")
        for bad in ("abc", "zz" * 32, ""):
            with self.assertRaises(ValueError):
                mesh.add_node(self.st, bad)
        with self.assertRaises(ValueError):
            mesh.assign_node(self.st, key, "no-such-person")
        with self.assertRaises(LookupError):
            mesh.assign_node(self.st, "f6" * 32, self.uid["Mom"])
        mesh.forget_node(self.st, key.upper())
        self.assertFalse([n for n in mesh.nodes(self.st) if n["public_key"] == key])


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "m.db")
        for name, meshname in (("Alex Smith", ""), ("Mom", "Grandma B"), ("Sam", "")):
            self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at, mesh_name) VALUES(?,?,?,?,?,?)",
                          (name, "adult", 0, uuid.uuid4().hex, checkins.now(), meshname))
        self.s = mesh.save_settings(self.st, {"connect": "tcp:127.0.0.1:5000", "channels": [{"name": "BUNKR family", "index": 7, "kind": "family"}, {"name": "Circle", "index": 2, "kind": "group"}]})
        self.fam, self.group = self.s["channels"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_words_and_mecp(self):
        self.assertEqual(mesh.interpret("SAFE at Grandma's")["note"], "at Grandma's")
        self.assertEqual(mesh.interpret("omw 20 min")["status"], "moving")
        self.assertEqual(mesh.interpret("Need help - flat tire")["note"], "flat tire")
        self.assertIsNone(mesh.interpret("ok see you soon"))
        self.assertIsNone(mesh.interpret("safely home"))
        self.assertEqual(mesh.interpret("MECP/0/M01 broken leg")["status"], "help")
        self.assertEqual(mesh.interpret("MECP/0/M01 broken leg")["note"], "MAYDAY broken leg (MECP M01)")
        self.assertEqual(mesh.interpret("MECP/3/B03 at the cabin")["status"], "safe")
        self.assertTrue(mesh.interpret("MECP/1/D01 practice")["drill"])
        self.assertIsNone(mesh.interpret("MECP/3/W01 roads icy"))

    def test_who_sent_it(self):
        self.assertEqual(mesh.person_for(self.st, "Grandma B")["name"], "Mom")  # mesh name first
        self.assertEqual(mesh.person_for(self.st, "alex smith")["name"], "Alex Smith")
        self.assertEqual(mesh.person_for(self.st, "Alex")["name"], "Alex Smith")  # a unique first name
        self.assertIsNone(mesh.person_for(self.st, "Stranger"))

    def test_handle(self):
        r = mesh.handle(self.st, {"kind": "channel", "channel": 7, "text": "Alex: SAFE at the cabin"}, self.s)
        self.assertEqual(r, "Got it: Alex Smith is safe.")
        last = checkins.board(self.st)["people"]
        z = [p for p in last if p["name"] == "Alex Smith"][0]["last"]
        self.assertEqual((z["status"], z["via"], z["note"]), ("safe", "mesh", "at the cabin"))
        self.assertIn("doesn't know", mesh.handle(self.st, {"kind": "channel", "channel": 7, "text": "Bob: HELP"}, self.s))
        self.assertIsNone(mesh.handle(self.st, {"kind": "channel", "channel": 0, "text": "Alex: HELP"}, self.s))  # public channel
        self.assertIsNone(mesh.handle(self.st, {"kind": "channel", "channel": 7, "text": "Sam: MECP/0/D01 drill"}, self.s))
        self.assertIsNone(mesh.handle(self.st, {"kind": "channel", "channel": 7, "text": "Sam: dinner at 6?"}, self.s))
        results = [x["result"] for x in mesh.recent(self.st)]
        self.assertIn("MECP drill (logged only)", results)
        self.assertIn("chat", results)
        self.assertEqual([p["last"] for p in checkins.board(self.st)["people"] if p["name"] == "Sam"], [None])

    def test_what_goes_out(self):
        call = mesh.outgoing("call", {"label": "Evacuate to", "place": "Grandma's", "message": "Take 285", "drill": False}, self.fam)
        self.assertEqual(call, "BUNKR CALL: Evacuate to Grandma's. Take 285 Reply SAFE, OMW or HELP")
        self.assertTrue(mesh.outgoing("call", {"label": "Meet at", "place": "x" * 300, "drill": True}, self.fam).startswith("DRILL: "))
        self.assertLessEqual(len(mesh.outgoing("call", {"label": "Meet at", "place": "x" * 300}, self.fam)), 120)
        self.assertIsNone(mesh.outgoing("call", {"label": "Evacuate to", "place": "Grandma's"}, self.group))  # the group doesn't get calls
        help_ = {"status": "help", "via": "app", "person_name": "Sam", "note": "car crash", "place": ""}
        self.assertEqual(mesh.outgoing("checkin", help_, self.fam), "Sam NEEDS HELP. car crash")
        self.assertEqual(mesh.outgoing("checkin", help_, self.group), "Sam NEEDS HELP. car crash")
        self.assertIsNone(mesh.outgoing("checkin", help_ | {"via": "mesh"}, self.fam))  # no echo
        self.assertIsNone(mesh.outgoing("checkin", help_ | {"status": "safe"}, self.fam))

    def test_help_heard_on_family_goes_to_the_group(self):
        sent = []
        listener = lambda ev, data: sent.extend((c["name"], mesh.outgoing(ev, data, c)) for c in mesh.settings(self.st)["channels"])
        checkins.listeners.append(listener)
        try:
            mesh.handle(self.st, {"kind": "channel", "channel": 7, "text": "Sam: HELP flat tire on 285"}, self.s)
        finally:
            checkins.listeners.remove(listener)
        self.assertEqual(sent, [("BUNKR family", None), ("Circle", "Sam NEEDS HELP. flat tire on 285")])

    def test_group_channel(self):
        r = mesh.handle(self.st, {"kind": "channel", "channel": 2, "text": "Grandma B: SAFE home"}, self.s)
        self.assertEqual(r, "Got it: Mom is safe.")  # check-ins count from people BUNKR knows
        self.assertEqual(mesh.recent(self.st)[0]["channel"], "Circle")
        self.assertIsNone(mesh.handle(self.st, {"kind": "channel", "channel": 3, "text": "Alex: SAFE"}, self.s))  # not one of ours

    def test_settings(self):
        self.assertEqual([(c["name"], c["index"], c["kind"], c["bot"], c["shared"]) for c in self.s["channels"]],
                         [("BUNKR family", 7, "family", "full", True), ("Circle", 2, "group", "basic", False)])
        self.assertEqual(len(self.fam["secret"]), 32)
        self.assertNotEqual(self.fam["secret"], self.group["secret"])
        good = {"name": "Family", "index": 1}
        for bad in ({"connect": "rm -rf /"}, {"channels": [good | {"index": 0}]}, {"channels": [good | {"secret": "00" * 16}]},
                    {"channels": [good | {"name": "  "}]}, {"channels": [good, good | {"name": "Other"}]},
                    {"channels": [good | {"secret": "not a key"}]}, {"channels": [good] * 7}):
            with self.assertRaises(ValueError):
                mesh.save_settings(self.st, bad)
        self.assertIn("meshcore://channel/add?name=BUNKR+family&secret=" + self.fam["secret"], mesh.channel_link(self.fam))
        # a key pasted as hex, base64, or the link the MeshCore app shares (which brings the name too)
        key = "8b3387e9c5cdea6ac9e5edbaa115cd72"
        self.assertEqual(mesh.parse_secret(key.upper()), key)
        self.assertEqual(mesh.parse_secret(base64.b64encode(bytes.fromhex(key)).decode()), key)
        s = mesh.save_settings(self.st, {"channels": [{"secret": f"meshcore://channel/add?name=Circle&secret={key}", "index": 4, "kind": "group"}]})
        self.assertEqual((s["channels"][0]["name"], s["channels"][0]["secret"]), ("Circle", key))
        # a first save makes a family channel
        st2 = Store(Path(self.tmp.name) / "m2.db")
        self.assertEqual([(c["name"], c["index"]) for c in mesh.save_settings(st2, {"connect": "/dev/ttyUSB0"})["channels"]], [("Family", 1)])


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "m.db")
        self.alex = uuid.uuid4().hex
        self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at) VALUES(?,?,?,?,?)", ("Alex", "adult", 0, self.alex, checkins.now()))
        self.radio = FakeRadio()
        mesh.save_settings(self.st, {"enabled": True, "connect": f"tcp:127.0.0.1:{self.radio.port}",
                                     "channels": [{"name": "BUNKR family", "index": 7}, {"name": "Circle", "index": 2, "kind": "group"}]})
        self.link = mesh.Link(self.st, poll_every=0.5)
        checkins.listeners.append(self.link)
        self.link.start()

    def tearDown(self):
        self.link.stop()
        checkins.listeners.remove(self.link)
        self.radio.close()
        self.tmp.cleanup()

    def test_end_to_end(self):
        self.assertTrue(wait_for(lambda: self.link.status["state"] == "connected"), self.link.status)
        self.assertEqual(self.link.status["radio"]["model"], "Heltec V3")
        fam, group = mesh.settings(self.st)["channels"]
        self.assertEqual(self.radio.channels[7], ("BUNKR family", bytes.fromhex(fam["secret"])))
        self.assertEqual(self.radio.channels[2], ("Circle", bytes.fromhex(group["secret"])))
        # a check-in from the mesh, and BUNKR's answer
        self.radio.push("Alex: OMW leaving Denver")
        self.assertTrue(wait_for(lambda: checkins.board(self.st)["people"][0]["last"]))
        last = checkins.board(self.st)["people"][0]["last"]
        self.assertEqual((last["status"], last["via"], last["note"]), ("moving", "mesh", "leaving Denver"))
        self.assertTrue(wait_for(lambda: (7, "Got it: Alex is on the way.") in self.radio.sent), self.radio.sent)
        # a family call from BUNKR goes out on the channel
        checkins.start_call(self.st, "shelter", "dad")
        self.assertTrue(wait_for(lambda: any("BUNKR CALL: Shelter in place" in t for _, t in self.radio.sent)), self.radio.sent)
        self.assertFalse(any(i == 2 and "CALL" in t for i, t in self.radio.sent))  # not on the group channel

    def test_nodes_from_the_radio(self):
        self.assertTrue(wait_for(lambda: len(mesh.nodes(self.st)) == 2), mesh.nodes(self.st))
        self.assertEqual({n["name"]: n["type_name"] for n in mesh.nodes(self.st)}, {"T-Deck 7F": "companion", "Roof RAK": "repeater"})
        self.radio.advert(contact(0xC3, "Grandma B", lastmod=200, code=mc.PUSH_NEW_ADVERT))
        self.assertTrue(wait_for(lambda: any(n["name"] == "Grandma B" for n in mesh.nodes(self.st))))
        mesh.assign_node(self.st, "a1" * 32, self.alex)
        self.radio.push("T-Deck 7F: SAFE")
        self.assertTrue(wait_for(lambda: (7, "Got it: Alex is safe.") in self.radio.sent), self.radio.sent)


class MeshHttpTests(unittest.TestCase):
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

    def test_nodes_over_http(self):
        self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at) VALUES('Jordan','adult',1,'ty1',?)", (checkins.now(),))
        key = "ab" * 32
        self.assertEqual(self.req("POST", "/api/mesh/nodes", {"public_key": key}, "kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/mesh/nodes", {"public_key": "nope"})[0], 400)
        code, v = self.req("POST", "/api/mesh/nodes", {"public_key": key, "name": "T-Deck", "person_uid": "ty1"})
        self.assertEqual((code, v["nodes"][0]["person_name"]), (200, "Jordan"))
        self.assertEqual(self.req("POST", f"/api/mesh/nodes/{key}", {"person_uid": "", "label": "spare"})[1]["nodes"][0]["label"], "spare")
        self.assertEqual(self.req("POST", f"/api/mesh/nodes/{'cd' * 32}", {"person_uid": "ty1"})[0], 404)
        self.assertIn("nodes", self.req("GET", "/api/mesh")[1])
        self.assertNotIn("nodes", self.req("GET", "/api/mesh", user="kid")[1])
        self.assertEqual(self.req("DELETE", f"/api/mesh/nodes/{key}", user="kid")[0], 403)
        self.assertEqual(self.req("DELETE", f"/api/mesh/nodes/{key}")[1]["nodes"], [])

    def test_admin_sets_up_family_joins(self):
        self.assertEqual(self.req("POST", "/api/mesh", {"connect": "/dev/ttyACM0"}, "kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/mesh", {"connect": "bad"})[0], 400)
        code, v = self.req("POST", "/api/mesh", {"connect": "/dev/ttyACM0", "channels": [
            {"name": "Family", "index": 5}, {"name": "Circle", "index": 6, "kind": "group"}]})
        self.assertEqual((code, [c["index"] for c in v["channels"]]), (200, [5, 6]))
        fam = v["channels"][0]
        self.assertIn(fam["secret"], fam["link"])
        kid = self.req("GET", "/api/mesh", user="kid")[1]
        self.assertEqual([(c["name"], c["link"]) for c in kid["channels"]], [("Family", fam["link"])])  # Circle isn't shown
        self.assertNotIn("secret", kid["channels"][0])  # the key only travels inside the join link
        self.assertNotIn("log", kid)
        self.assertNotIn("settings", kid)
        self.assertEqual(self.req("POST", "/api/mesh/test")[0], 400)  # not turned on


if __name__ == "__main__":
    unittest.main()
