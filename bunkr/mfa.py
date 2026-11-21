"""Two-step login: passkeys (Face ID, fingerprint, a phone's screen lock) and authenticator-app codes.

Passkeys (WebAuthn) are the easy way: one tap, nothing to type, and they can't be phished. Browsers only offer them
on https (https://bunkr.example.org) or on localhost, so a passkey is tied to the address it was made on.
Authenticator codes (TOTP, RFC 6238: 6 digits, 30 seconds) work anywhere, including the Pi's Wi-Fi over plain http.
Turning on codes also gives ten one-time recovery codes for a lost phone.

A passkey asks the phone to check it's you (user verification), so it is both steps at once and can log in alone.
With a password, a person who has two-step login turned on also gives a code or uses a passkey.

Secrets: code secrets are encrypted with a key in a file next to the database (mfa.key, mode 600), so a copied
database or backup can't make codes; recovery codes are stored as hashes; passkeys are public keys only.
python3-cryptography (already on every node for phone notifications) does the AES and signature checks.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

STEP = 30
DIGITS = 6
ISSUER = "BUNKR"
CHALLENGE_SECONDS = 300
RECOVERY_COUNT = 10


class MfaError(Exception):
    pass


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    s = str(s or "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------- the key file for code secrets ----------
def _key(store):
    path = Path(store.path).parent / "mfa.key"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
    data = path.read_bytes()
    if len(data) != 32:
        raise MfaError("mfa.key is damaged")
    return data


def _seal(store, raw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return "v1:" + b64u(nonce + AESGCM(_key(store)).encrypt(nonce, raw, b"bunkr-totp"))


def _open(store, sealed):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not sealed.startswith("v1:"):
        raise MfaError("unknown secret format")
    blob = unb64u(sealed[3:])
    return AESGCM(_key(store)).decrypt(blob[:12], blob[12:], b"bunkr-totp")


# ---------- authenticator codes ----------
def totp(secret, counter):
    mac = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    return str((struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS).zfill(DIGITS)


def new_totp_secret():
    return secrets.token_bytes(20)


def totp_uri(secret, username):
    key = base64.b32encode(secret).decode().rstrip("=")
    return f"otpauth://totp/{ISSUER}:{username}?secret={key}&issuer={ISSUER}&digits={DIGITS}&period={STEP}", key


def check_totp(secret, code, last_counter=0, now=None, window=1):
    """The matching time step (to store, so a code can't be used twice), or None."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    now_c = int((now if now is not None else time.time()) // STEP)
    for c in range(now_c - window, now_c + window + 1):
        if c > last_counter and hmac.compare_digest(totp(secret, c), code):
            return c
    return None


def recovery_codes():
    """Ten codes like 4F7K-9QXM, shown once; returns (shown, stored hashes)."""
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    shown = ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(secrets.choice(alphabet) for _ in range(4))
             for _ in range(RECOVERY_COUNT)]
    return shown, [_code_hash(c) for c in shown]


def _code_hash(code):
    return hashlib.sha256(("bunkr-recovery:" + code.upper().replace("-", "").replace(" ", "")).encode()).hexdigest()


# ---------- per-user state ----------
def status(store, user_id):
    u = store._rows("SELECT totp_secret, recovery FROM users WHERE id=?", (user_id,))
    keys = store._rows("SELECT id, name, rp_id, created_at, last_used FROM passkeys WHERE user_id=? ORDER BY id", (user_id,))
    rec = json.loads(u[0]["recovery"] or "[]") if u else []
    return {"totp": bool(u and u[0]["totp_secret"]), "passkeys": keys, "recovery_left": len(rec),
            "on": bool((u and u[0]["totp_secret"]) or keys)}


def enabled(store, user_id):
    return status(store, user_id)["on"]


def start_totp(store, user_id, username):
    """A new secret to show; it only counts once confirm_totp sees a code made from it."""
    secret = new_totp_secret()
    uri, key = totp_uri(secret, username)
    PENDING.put(("totp", user_id), secret)
    return {"uri": uri, "key": " ".join(key[i:i + 4] for i in range(0, len(key), 4))}


def confirm_totp(store, user_id, code):
    secret = PENDING.take(("totp", user_id))
    if secret is None:
        raise MfaError("start again: that setup expired")
    c = check_totp(secret, code)
    if c is None:
        PENDING.put(("totp", user_id), secret)  # let them try again
        raise MfaError("that code doesn't match; check the phone's clock and try the next one")
    shown, hashes = recovery_codes()
    store._exec("UPDATE users SET totp_secret=?, totp_last=?, recovery=? WHERE id=?", (_seal(store, secret), c, json.dumps(hashes), user_id))
    return shown


def verify_code(store, user_id, code):
    """An authenticator code or a recovery code. Returns "totp", "recovery" or None. Each code works once."""
    u = store._rows("SELECT totp_secret, totp_last, recovery FROM users WHERE id=?", (user_id,))
    if not u:
        return None
    u = u[0]
    if u["totp_secret"]:
        c = check_totp(_open(store, u["totp_secret"]), code, u["totp_last"] or 0)
        if c is not None:
            store._exec("UPDATE users SET totp_last=? WHERE id=?", (c, user_id))
            return "totp"
    rec = json.loads(u["recovery"] or "[]")
    h = _code_hash(str(code or ""))
    if h in rec:
        rec.remove(h)
        store._exec("UPDATE users SET recovery=? WHERE id=?", (json.dumps(rec), user_id))
        return "recovery"
    return None


def remove_totp(store, user_id):
    store._exec("UPDATE users SET totp_secret='', totp_last=0, recovery='[]' WHERE id=?", (user_id,))


def remove_all(store, user_id):
    remove_totp(store, user_id)
    store._exec("DELETE FROM passkeys WHERE user_id=?", (user_id,))


# ---------- short-lived state: challenges and setups ----------
class Pending:
    def __init__(self, seconds=CHALLENGE_SECONDS):
        self.seconds, self.items, self.lock = seconds, {}, threading.Lock()

    def put(self, key, value):
        with self.lock:
            now = time.time()
            self.items = {k: v for k, v in self.items.items() if v[0] > now}
            self.items[key] = (now + self.seconds, value)

    def take(self, key):
        with self.lock:
            hit = self.items.pop(key, None)
        return hit[1] if hit and hit[0] > time.time() else None


PENDING = Pending()


# ---------- CBOR (just what WebAuthn uses) ----------
def cbor_decode(data, i=0):
    """Decode one CBOR item at data[i]. Returns (value, next index)."""
    ib = data[i]
    major, info = ib >> 5, ib & 0x1F
    i += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        n = 1 << (info - 24)
        arg = int.from_bytes(data[i:i + n], "big")
        i += n
    else:
        raise MfaError("unsupported CBOR")
    if major == 0:
        return arg, i
    if major == 1:
        return -1 - arg, i
    if major == 2:
        return bytes(data[i:i + arg]), i + arg
    if major == 3:
        return bytes(data[i:i + arg]).decode(), i + arg
    if major == 4:
        out = []
        for _ in range(arg):
            v, i = cbor_decode(data, i)
            out.append(v)
        return out, i
    if major == 5:
        out = {}
        for _ in range(arg):
            k, i = cbor_decode(data, i)
            v, i = cbor_decode(data, i)
            out[k] = v
        return out, i
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], i
    raise MfaError("unsupported CBOR")


def _public_key(cose):
    """COSE key -> (cryptography public key, alg)."""
    kty, alg = cose.get(1), cose.get(3)
    if kty == 2 and alg == -7 and cose.get(-1) == 1:
        from cryptography.hazmat.primitives.asymmetric import ec
        nums = ec.EllipticCurvePublicNumbers(int.from_bytes(cose[-2], "big"), int.from_bytes(cose[-3], "big"), ec.SECP256R1())
        return nums.public_key(), alg
    if kty == 3 and alg == -257:
        from cryptography.hazmat.primitives.asymmetric import rsa
        return rsa.RSAPublicNumbers(int.from_bytes(cose[-2], "big"), int.from_bytes(cose[-1], "big")).public_key(), alg
    if kty == 1 and alg == -8 and cose.get(-1) == 6:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        return ed25519.Ed25519PublicKey.from_public_bytes(cose[-2]), alg
    raise MfaError("this kind of passkey isn't supported")


def _verify_sig(cose_bytes, sig, signed):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    key, alg = _public_key(cbor_decode(cose_bytes)[0])
    try:
        if alg == -7:
            key.verify(sig, signed, ec.ECDSA(hashes.SHA256()))
        elif alg == -257:
            key.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
        else:
            key.verify(sig, signed)
    except InvalidSignature:
        raise MfaError("the passkey's signature doesn't check out")


def _auth_data(ad, rp_id):
    if len(ad) < 37:
        raise MfaError("bad authenticator data")
    if ad[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise MfaError("this passkey was made for a different address")
    flags = ad[32]
    if not flags & 0x01:
        raise MfaError("the passkey wasn't confirmed by a touch")
    if not flags & 0x04:
        raise MfaError("the phone didn't check it's you (Face ID, fingerprint or screen lock)")
    return flags, struct.unpack(">I", ad[33:37])[0]


def _client_data(raw, kind, challenge, origin):
    try:
        cd = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise MfaError("bad client data")
    if cd.get("type") != kind:
        raise MfaError("wrong kind of passkey request")
    if not hmac.compare_digest(str(cd.get("challenge", "")), b64u(challenge)):
        raise MfaError("that passkey request expired; try again")
    if cd.get("origin") != origin:
        raise MfaError(f"passkey made on {cd.get('origin')}, not {origin}")
    return cd


# ---------- passkeys ----------
def user_handle(store, user_id):
    r = store._rows("SELECT webauthn_id FROM users WHERE id=?", (user_id,))
    if r and r[0]["webauthn_id"]:
        return r[0]["webauthn_id"]
    h = b64u(secrets.token_bytes(16))
    store._exec("UPDATE users SET webauthn_id=? WHERE id=?", (h, user_id))
    return h


def register_options(store, user, rp_id):
    challenge = secrets.token_bytes(32)
    PENDING.put(("reg", user["id"]), (challenge, rp_id))
    existing = store._rows("SELECT cred_id FROM passkeys WHERE user_id=? AND rp_id=?", (user["id"], rp_id))
    return {"challenge": b64u(challenge), "rp": {"id": rp_id, "name": ISSUER},
            "user": {"id": user_handle(store, user["id"]), "name": user["username"], "displayName": user["username"]},
            "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in (-7, -8, -257)],
            "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True, "userVerification": "required"},
            "excludeCredentials": [{"type": "public-key", "id": r["cred_id"]} for r in existing],
            "attestation": "none", "timeout": CHALLENGE_SECONDS * 1000}


def register_finish(store, user, data, origin, name=""):
    pend = PENDING.take(("reg", user["id"]))
    if not pend:
        raise MfaError("that passkey setup expired; try again")
    challenge, rp_id = pend
    _client_data(unb64u(data.get("clientDataJSON")), "webauthn.create", challenge, origin)
    att, _ = cbor_decode(unb64u(data.get("attestationObject")))
    ad = att.get("authData") or b""
    flags, count = _auth_data(ad, rp_id)
    if not flags & 0x40:
        raise MfaError("the passkey didn't include its key")
    n = struct.unpack(">H", ad[53:55])[0]
    cred_id = ad[55:55 + n]
    cose, _end = cbor_decode(ad, 55 + n)
    _public_key(cose)  # a kind we can check later
    cose_bytes = ad[55 + n:_end]
    if b64u(cred_id) != data.get("id"):
        raise MfaError("passkey id mismatch")
    if store._rows("SELECT 1 FROM passkeys WHERE cred_id=?", (b64u(cred_id),)):
        raise MfaError("that passkey is already saved")
    label = str(name or "").strip()[:60] or "Passkey"
    return store._exec("INSERT INTO passkeys(user_id, cred_id, public_key, alg, sign_count, name, rp_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (user["id"], b64u(cred_id), cose_bytes, cose.get(3), count, label, rp_id, _now_iso()))


def login_options(rp_id, user_id=None, store=None):
    """A challenge for a passkey login. With user_id (after a password), only that person's passkeys count."""
    challenge = secrets.token_bytes(32)
    state = secrets.token_urlsafe(16)
    PENDING.put(("login", state), (challenge, rp_id, user_id))
    allow = [{"type": "public-key", "id": r["cred_id"]} for r in
             store._rows("SELECT cred_id FROM passkeys WHERE user_id=? AND rp_id=?", (user_id, rp_id))] if user_id and store else []
    return {"state": state, "publicKey": {"challenge": b64u(challenge), "rpId": rp_id, "userVerification": "required",
                                          "allowCredentials": allow, "timeout": CHALLENGE_SECONDS * 1000}}


def login_finish(store, data, origin):
    """Check a passkey login. Returns the user id."""
    pend = PENDING.take(("login", str(data.get("state") or "")))
    if not pend:
        raise MfaError("that passkey request expired; try again")
    challenge, rp_id, want_user = pend
    rows = store._rows("SELECT * FROM passkeys WHERE cred_id=?", (str(data.get("id") or ""),))
    if not rows or rows[0]["rp_id"] != rp_id:
        raise MfaError("BUNKR doesn't know this passkey (it may have been removed)")
    pk = rows[0]
    if want_user and pk["user_id"] != want_user:
        raise MfaError("that passkey belongs to someone else")
    if data.get("userHandle") and data["userHandle"] != user_handle(store, pk["user_id"]):
        raise MfaError("passkey user mismatch")
    raw_cd = unb64u(data.get("clientDataJSON"))
    _client_data(raw_cd, "webauthn.get", challenge, origin)
    ad = unb64u(data.get("authenticatorData"))
    _flags, count = _auth_data(ad, rp_id)
    if pk["sign_count"] and count <= pk["sign_count"]:
        raise MfaError("this passkey looks copied (its counter went backwards)")
    _verify_sig(bytes(pk["public_key"]), unb64u(data.get("signature")), ad + hashlib.sha256(raw_cd).digest())
    store._exec("UPDATE passkeys SET sign_count=?, last_used=? WHERE id=?", (count, _now_iso(), pk["id"]))
    return pk["user_id"]


def remove_passkey(store, user_id, pk_id):
    return store._exec("DELETE FROM passkeys WHERE id=? AND user_id=?", (pk_id, user_id))
