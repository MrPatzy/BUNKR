"""Phone notifications: Web Push encryption and signing, the calm rules, delivery, and the API."""

import base64
import json
import os
import struct
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from bunkr import auth, checkins, notify, server  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402


class Phone:
    """What a browser does: a key pair and an auth secret, and decrypting what arrives (RFC 8291)."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pub = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.auth = os.urandom(16)

    def sub(self, endpoint="https://push.example.com/send/abc"):
        return {"endpoint": endpoint, "keys": {"p256dh": notify.b64u(self.pub), "auth": notify.b64u(self.auth)}}

    def decrypt(self, body):
        salt, rs, idlen = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
        as_pub = body[21:21 + idlen]
        cipher = body[21 + idlen:]
        self.assertion = (rs, idlen)
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
        ikm = notify._hkdf(self.auth, shared, b"WebPush: info\x00" + self.pub + as_pub, 32)
        cek = notify._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = notify._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        plain = AESGCM(cek).decrypt(nonce, cipher, None)
        self.assertion_delim = plain[-1]
        return plain[:-1]


class FakeOpener:
    def __init__(self, status=201):
        self.status, self.requests = status, []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if self.status >= 400:
            raise urllib.error.HTTPError(req.full_url, self.status, "gone", {}, None)

        class R:
            status = self.status

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False
        return R()


class CryptoTests(unittest.TestCase):
    def test_payload_decrypts_on_the_phone(self):
        phone = Phone()
        s = phone.sub()
        body = notify.encrypt(b'{"title":"hi"}', s["keys"]["p256dh"], s["keys"]["auth"])
        self.assertEqual(phone.decrypt(body), b'{"title":"hi"}')
        self.assertEqual(phone.assertion, (4096, 65))
        self.assertEqual(phone.assertion_delim, 2)

    def test_matches_the_rfc_8291_example(self):
        """Byte for byte the worked example in RFC 8291, section 5 and appendix A."""
        u = notify.unb64u
        as_priv = ec.derive_private_key(int.from_bytes(u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
        body = notify.encrypt(u("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
                              "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                              "BTBZMqHH6r4Tts7J_aSIgg", salt=u("DGv6ra1nlYgDCS1FRnbzlw"), server_key=as_priv)
        self.assertEqual(body, u("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8")
                         + u("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"))

    def test_vapid_jwt_is_signed_for_the_push_service(self):
        with tempfile.TemporaryDirectory() as d:
            kf = Path(d) / "vapid.pem"
            key = notify.load_keys(kf)
            self.assertEqual(oct(kf.stat().st_mode & 0o777), "0o600")
            self.assertEqual(notify.public_key_b64(notify.load_keys(kf)), notify.public_key_b64(key))  # reused
            hdr = notify.vapid_header(key, "https://fcm.googleapis.com/fcm/send/xyz", now=1_000_000)
            t = hdr.split("t=")[1].split(",")[0]
            k = hdr.split("k=")[1]
            head, claims, sig = t.split(".")
            c = json.loads(notify.unb64u(claims))
            self.assertEqual((c["aud"], c["exp"]), ("https://fcm.googleapis.com", 1_000_000 + 12 * 3600))
            self.assertTrue(c["sub"].startswith("mailto:"))
            raw = notify.unb64u(sig)
            der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
            pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), notify.unb64u(k))
            pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if wrong


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        st = self.store = Store(Path(self.tmp.name) / "n.db")
        self.dad = auth.create_user(st, "dad", p3.PW, "admin")
        self.mom = auth.create_user(st, "mom", p3.PW, "family")
        import uuid
        self.dad_uid, self.mom_uid = uuid.uuid4().hex, uuid.uuid4().hex
        st._exec("INSERT INTO people(name, role, uid, user_id) VALUES('Alex','adult',?,?)", (self.dad_uid, self.dad))
        st._exec("INSERT INTO people(name, role, uid, user_id) VALUES('Wife','adult',?,?)", (self.mom_uid, self.mom))
        for uid_ in (self.dad, self.mom):
            st._exec("UPDATE users SET push_prefs=? WHERE id=?", (json.dumps({"tz": "UTC", "quiet_start": "22:00", "quiet_end": "06:30"}), uid_))

    def tearDown(self):
        self.tmp.cleanup()

    def who(self, event, data, hour):
        at = datetime(2026, 11, 20, hour, 0, tzinfo=timezone.utc)
        return sorted(u for u, _ in notify.plan(self.store, event, data, at))

    def item(self, tier):
        return {"uid": "x:1", "tier": tier, "title": "Thing", "site": "Home", "km": 12, "where_text": "", "summary": ""}

    def test_calm_rules(self):
        both = sorted([self.dad, self.mom])
        self.assertEqual(self.who("situation", self.item("flash"), 3), both)        # act now: even at 3 am
        self.assertEqual(self.who("situation", self.item("priority"), 3), [])       # soon: not in quiet hours
        self.assertEqual(self.who("situation", self.item("priority"), 12), both)
        self.assertEqual(self.who("situation", self.item("routine"), 12), [])       # good to know: never
        msg = notify.plan(self.store, "situation", self.item("priority"), datetime(2026, 11, 20, 12, tzinfo=timezone.utc))[0][1]
        self.assertEqual((msg["body"], msg["urgency"]), ("12 km from Home", "normal"))
        # the daily cap
        for _ in range(3):
            self.store._exec("INSERT INTO push_log(user_id, at, tier, sent) VALUES(?,?,?,1)", (self.mom, "2026-11-20T08:00:00+00:00", "priority"))
        self.assertEqual(self.who("situation", self.item("priority"), 12), [self.dad])
        # check-ins: help always (not to yourself); others only during a call by default
        ck = {"person_uid": self.mom_uid, "person_name": "Wife", "status": "help", "status_label": "Need help", "note": "car"}
        self.assertEqual(self.who("checkin", ck, 3), [self.dad])
        self.assertEqual(self.who("checkin", dict(ck, status="safe", status_label="I'm safe"), 12), [])
        checkins.start_call(self.store, "shelter", "dad")
        self.assertEqual(self.who("checkin", dict(ck, status="safe", status_label="I'm safe"), 3), [self.dad])
        self.assertEqual(self.who("call", {"label": "Shelter in place", "place": ""}, 3), both)
        self.assertEqual(self.who("test", {"user_id": self.mom}, 3), [self.mom])

    def test_quiet_hours_cross_midnight_and_timezones(self):
        p = {"quiet_start": "22:00", "quiet_end": "06:30", "tz": "America/Denver"}
        self.assertTrue(notify.in_quiet_hours(p, datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)))    # 23:00 Denver
        self.assertFalse(notify.in_quiet_hours(p, datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)))  # 11:00 Denver
        self.assertFalse(notify.in_quiet_hours({"quiet_start": "", "quiet_end": ""}))
        self.assertEqual(notify.clean_prefs({"quiet_start": "25:00", "tz": "Mars/Base", "checkins": "x", "priority_cap": 99}),
                         {"priority_cap": 20})

    def test_delivery_headers_and_gone_phones(self):
        phone = Phone()
        notify.save_subscription(self.store, self.mom, phone.sub())
        key = ec.generate_private_key(ec.SECP256R1())
        op = FakeOpener(201)
        res = notify.send(self.store, key, "test", {"user_id": self.mom}, opener=op)
        self.assertEqual(res, [(self.mom, 201)])
        req = op.requests[0]
        h = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual((h["content-encoding"], h["urgency"]), ("aes128gcm", "normal"))
        self.assertTrue(h["authorization"].startswith("vapid t="))
        self.assertEqual(json.loads(phone.decrypt(req.data))["title"], "BUNKR test")
        self.assertTrue(notify.subscriptions(self.store)[0]["last_ok"])
        notify.send(self.store, key, "test", {"user_id": self.mom}, opener=FakeOpener(410))
        self.assertEqual(notify.subscriptions(self.store), [])  # the phone uninstalled: forget it
        with self.assertRaises(ValueError):
            notify.save_subscription(self.store, self.mom, {"endpoint": "http://insecure/x", "keys": phone.sub()["keys"]})


class PushApiTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        auth.create_user(self.hub.store, "mom", p3.PW, "family")
        server.notifier = notify.Notifier(self.hub.store, self.hub.dir / "vapid.pem")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": "mom", "password": p3.PW}).encode(),
                                   method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
        with self.opener.open(r) as resp:
            self.cookie = resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def tearDown(self):
        server.notifier = None
        self.hub.close()

    def req(self, method, path, body=None):
        r = urllib.request.Request(self.hub.url + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.cookie}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_subscribe_prefs_test_unsubscribe(self):
        st, k = self.req("GET", "/api/push/key")
        self.assertEqual(len(notify.unb64u(k["key"])), 65)
        self.assertEqual(k["phones"], 0)
        self.assertEqual(self.req("POST", "/api/push/test")[0], 400)  # no phone yet
        phone = Phone()
        self.assertEqual(self.req("POST", "/api/push/subscribe", {"subscription": phone.sub(), "tz": "America/Denver"})[0], 200)
        self.assertEqual(self.req("GET", "/api/push/key")[1]["prefs"]["tz"], "America/Denver")
        st, p = self.req("POST", "/api/push/prefs", {"priority": False, "checkins": "all", "quiet_start": "21:00"})
        self.assertEqual((p["priority"], p["checkins"], p["quiet_start"]), (False, "all", "21:00"))
        sent = []
        real = notify.deliver
        notify.deliver = lambda store, key, sub, msg, opener=None, ttl=None: sent.append(msg["title"]) or 201
        try:
            st, r = self.req("POST", "/api/push/test")
        finally:
            notify.deliver = real
        self.assertEqual((st, r["sent"], sent), (200, 1, ["BUNKR test"]))
        self.assertEqual(self.req("POST", "/api/push/subscribe", {"subscription": {"endpoint": "https://x/y", "keys": {}}})[0], 400)
        self.req("POST", "/api/push/unsubscribe", {"endpoint": phone.sub()["endpoint"]})
        self.assertEqual(self.req("GET", "/api/push/key")[1]["phones"], 0)
        # the service worker is public so the browser can install it
        with urllib.request.urlopen(self.hub.url + "/sw.js") as resp:
            self.assertIn(b"showNotification", resp.read())


if __name__ == "__main__":
    unittest.main()
