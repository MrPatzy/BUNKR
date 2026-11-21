"""Two-step login: authenticator codes (RFC 6238), recovery codes, passkeys checked against a software authenticator,
the login flow, the "required" policy, and admin reset."""

import base64
import hashlib
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import audit, auth, mfa  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402


def cbor(v):
    """Just enough CBOR encoding to build what a phone sends."""
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
    if v is None:
        return b"\xf6"
    if isinstance(v, bool):
        return bytes([0xF5 if v else 0xF4])
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        return head(3, len(v.encode())) + v.encode()
    if isinstance(v, list):
        return head(4, len(v)) + b"".join(cbor(x) for x in v)
    if isinstance(v, dict):
        return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())
    raise TypeError(v)


class SoftKey:
    """A passkey on a pretend phone (P-256, ES256), like an iPhone or Android phone makes."""

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0
        self.handle = ""

    def cose(self):
        n = self.key.public_key().public_numbers()
        return cbor({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def create(self, opts, origin, flags=0x45):
        self.handle = opts["user"]["id"]
        cd = json.dumps({"type": "webauthn.create", "challenge": opts["challenge"], "origin": origin}).encode()
        ad = hashlib.sha256(opts["rp"]["id"].encode()).digest() + bytes([flags]) + struct.pack(">I", 0) + bytes(16) + \
            struct.pack(">H", len(self.cred_id)) + self.cred_id + self.cose()
        return {"id": mfa.b64u(self.cred_id), "clientDataJSON": mfa.b64u(cd),
                "attestationObject": mfa.b64u(cbor({"fmt": "none", "attStmt": {}, "authData": ad}))}

    def get(self, opts, origin, flags=0x05, bump=True, rp_id=None):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        if bump:
            self.count += 1
        cd = json.dumps({"type": "webauthn.get", "challenge": opts["publicKey"]["challenge"], "origin": origin}).encode()
        ad = hashlib.sha256((rp_id or opts["publicKey"]["rpId"]).encode()).digest() + bytes([flags]) + struct.pack(">I", self.count)
        sig = self.key.sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": mfa.b64u(self.cred_id), "clientDataJSON": mfa.b64u(cd), "authenticatorData": mfa.b64u(ad),
                "signature": mfa.b64u(sig), "userHandle": self.handle, "state": opts["state"]}


class CodeTests(unittest.TestCase):
    def test_rfc6238_vectors(self):
        secret = b"12345678901234567890"
        self.assertEqual(mfa.totp(secret, 59 // 30), "287082")            # RFC 6238 appendix B, last 6 digits
        self.assertEqual(mfa.totp(secret, 1111111109 // 30), "081804")
        self.assertEqual(mfa.check_totp(secret, "287 082", now=59), 1)
        self.assertIsNone(mfa.check_totp(secret, "287082", last_counter=1, now=59))  # each code works once
        self.assertIsNone(mfa.check_totp(secret, "287082", now=59 + 120))       # too old
        uri, key = mfa.totp_uri(secret, "dad")
        self.assertEqual(base64.b32decode(key + "=" * (-len(key) % 8)), secret)
        self.assertIn("issuer=BUNKR", uri)

    def test_secrets_sealed_with_key_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(Path(tmp) / "m.db")
            sealed = mfa._seal(st, b"secret!")
            self.assertNotIn("secret", sealed)
            self.assertEqual(mfa._open(st, sealed), b"secret!")
            self.assertEqual((Path(tmp) / "mfa.key").stat().st_mode & 0o777, 0o600)
            (Path(tmp) / "mfa.key").write_bytes(os.urandom(32))  # a different key: the copy is useless
            with self.assertRaises(Exception):
                mfa._open(st, sealed)

    def test_cbor(self):
        v = {1: 2, 3: -7, "fmt": "none", "x": b"\x00" * 300, "list": [True, False, None, 70000]}
        self.assertEqual(mfa.cbor_decode(cbor(v))[0], v)


class TwoStepHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        self.st = self.hub.store
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.origin = self.hub.url
        self.told = []
        audit.listeners.append(lambda ev, d: self.told.append(d["title"]))

    def tearDown(self):
        audit.listeners.clear()
        self.hub.close()

    def post(self, path, body=None, cookie=None):
        h = {"X-Bunkr": "1", "Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = f"bunkr_session={cookie}"
        r = urllib.request.Request(self.hub.url + path, data=json.dumps(body or {}).encode(), method="POST", headers=h)
        try:
            with self.opener.open(r) as resp:
                c = resp.headers.get("Set-Cookie")
                return resp.status, json.loads(resp.read()), c.split(";")[0].split("=", 1)[1] if c else None
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), None

    def get(self, path, cookie, method="GET", body=None):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={cookie}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def login(self, user):
        return self.post("/api/login", {"username": user, "password": p3.PW})

    def turn_on_codes(self, cookie):
        st, r = self.get("/api/me/two-step/totp", cookie, "POST", {})
        secret = base64.b32decode(r["key"].replace(" ", "") + "=" * (-len(r["key"].replace(" ", "")) % 8))
        self.assertEqual(self.get("/api/me/two-step/totp/confirm", cookie, "POST", {"code": "000000"})[0], 400)
        st, r = self.get("/api/me/two-step/totp/confirm", cookie, "POST", {"code": mfa.totp(secret, int(time.time() // 30))})
        self.assertEqual((st, len(r["recovery"])), (200, 10))
        return secret, r["recovery"]

    def test_codes_and_recovery(self):
        st, r, dad = self.login("dad")
        self.assertEqual((st, r, bool(dad)), (200, {"ok": True}, True))
        secret, recovery = self.turn_on_codes(dad)
        st, r, cookie = self.login("dad")
        self.assertEqual((r["next"], r["methods"], cookie), ("two-step", ["code", "recovery"], None))  # no session yet
        self.assertEqual(self.post("/api/login/code", {"ticket": r["ticket"], "code": "123456"})[0], 401)
        st, ok, cookie = self.post("/api/login/code", {"ticket": r["ticket"], "code": mfa.totp(secret, int(time.time() // 30) + 1)})
        self.assertEqual((st, bool(cookie)), (200, True))
        self.assertEqual(self.get("/api/state", cookie)[0], 200)
        self.assertEqual(self.post("/api/login/code", {"ticket": r["ticket"], "code": "123456"})[0], 401)  # ticket used up
        st, r, _ = self.login("dad")
        self.assertEqual(self.post("/api/login/code", {"ticket": r["ticket"], "code": recovery[0].lower()})[0], 200)
        st, r, _ = self.login("dad")
        self.assertEqual(self.post("/api/login/code", {"ticket": r["ticket"], "code": recovery[0]})[0], 401)  # once only
        self.assertIn("A recovery code was used to log in", self.told)
        self.assertEqual(self.get("/api/me/two-step", cookie)[1]["recovery_left"], 9)
        stored = self.st._rows("SELECT totp_secret FROM users WHERE username='dad'")[0]["totp_secret"]
        self.assertNotIn(base64.b32encode(secret).decode().rstrip("="), stored)

    def test_wrong_codes_lock_the_login(self):
        st, r, dad = self.login("dad")
        self.turn_on_codes(dad)
        st, r, _ = self.login("dad")
        for _ in range(5):
            st, out, _ = self.post("/api/login/code", {"ticket": r["ticket"], "code": "000000"})
        self.assertIn("A login was locked after failed attempts", self.told)
        self.assertEqual(self.login("dad")[0], 401)

    def test_passkeys(self):
        st, r, dad = self.login("dad")
        phone = SoftKey()
        opts = self.get("/api/me/passkeys/options", dad, "POST", {})[1]
        self.assertEqual((opts["rp"]["id"], opts["authenticatorSelection"]["userVerification"]), ("127.0.0.1", "required"))
        st, r = self.get("/api/me/passkeys", dad, "POST", phone.create(opts, self.origin) | {"name": "Dad's iPhone"})
        self.assertEqual(st, 200, r)
        # passwordless: the passkey alone logs in
        o = self.post("/api/login/passkey-options")[1]
        st, r, cookie = self.post("/api/login/passkey", phone.get(o, self.origin))
        self.assertEqual((st, bool(cookie)), (200, True), r)
        self.assertEqual(self.get("/api/state", cookie)[0], 200)
        # a replayed counter, a different address, no Face ID check, a wrong origin: all refused
        o = self.post("/api/login/passkey-options")[1]
        self.assertIn("copied", self.post("/api/login/passkey", phone.get(o, self.origin, bump=False))[1]["error"])
        o = self.post("/api/login/passkey-options")[1]
        self.assertIn("different address", self.post("/api/login/passkey", phone.get(o, self.origin, rp_id="evil.example"))[1]["error"])
        o = self.post("/api/login/passkey-options")[1]
        self.assertIn("check it's you", self.post("/api/login/passkey", phone.get(o, self.origin, flags=0x01))[1]["error"])
        o = self.post("/api/login/passkey-options")[1]
        self.assertEqual(self.post("/api/login/passkey", phone.get(o, "https://evil.example"))[0], 401)
        # after a password, the passkey is the second step
        st, r, _ = self.login("dad")
        self.assertEqual(r["methods"], ["passkey"])
        o = self.post("/api/login/passkey-options", {"ticket": r["ticket"]})[1]
        self.assertEqual(len(o["publicKey"]["allowCredentials"]), 1)
        self.assertEqual(self.post("/api/login/passkey", phone.get(o, self.origin) | {"ticket": r["ticket"]})[0], 200)
        # someone else's passkey can't finish dad's login
        st, _, kid = self.login("kid")
        kphone = SoftKey()
        self.get("/api/me/passkeys", kid, "POST", kphone.create(self.get("/api/me/passkeys/options", kid, "POST", {})[1], self.origin))
        st, r, _ = self.login("dad")
        o = self.post("/api/login/passkey-options", {"ticket": r["ticket"]})[1]
        self.assertIn("someone else", self.post("/api/login/passkey", kphone.get(o, self.origin) | {"ticket": r["ticket"]})[1]["error"])
        pk = self.get("/api/me/two-step", dad)[1]["passkeys"][0]
        self.assertEqual(pk["name"], "Dad's iPhone")
        self.assertEqual(self.get(f"/api/me/passkeys/{pk['id']}", dad, "DELETE")[0], 200)
        self.assertIn("Two-step login was turned off", self.told)

    def test_required_policy_and_admin_reset(self):
        st, r, dad = self.login("dad")
        self.assertEqual(self.get("/api/users/policy", dad, "POST", {"policy": "everyone"})[0], 200)
        st, r, kid = self.login("kid")
        self.assertEqual((r["next"], bool(kid)), ("setup", True))
        self.assertEqual(self.get("/api/state", kid)[0], 403)  # a setup-only session
        self.assertEqual(self.get("/api/me/two-step", kid)[1]["required"], True)
        st, r = self.get("/api/me/two-step/totp", kid, "POST", {})
        secret = base64.b32decode(r["key"].replace(" ", "") + "=" * (-len(r["key"].replace(" ", "")) % 8))
        st, r = self.get("/api/me/two-step/totp/confirm", kid, "POST", {"code": mfa.totp(secret, int(time.time() // 30))})
        self.assertTrue(r["ready"])
        self.assertEqual(self.get("/api/state", kid)[0], 200)
        # can't remove the only second step while it's required
        self.assertEqual(self.get("/api/me/two-step/totp", kid, "DELETE", {"password": p3.PW})[0], 400)
        kid_id = auth.user_by_name(self.st, "kid")["id"]
        self.assertEqual(self.get(f"/api/users/{kid_id}/two-step-off", kid, "POST", {})[0], 403)
        self.assertEqual(self.get(f"/api/users/{kid_id}/two-step-off", dad, "POST", {})[0], 200)
        self.assertEqual(self.get("/api/state", kid)[0], 401)  # logged out everywhere
        users = {u["username"]: u for u in self.get("/api/users", dad)[1]}
        self.assertFalse(users["kid"]["two_step"])
        self.assertIn("Two-step login was turned off", self.told)
        self.assertEqual(self.get("/api/users/policy", dad, "POST", {"policy": "sometimes"})[0], 400)


if __name__ == "__main__":
    unittest.main()
