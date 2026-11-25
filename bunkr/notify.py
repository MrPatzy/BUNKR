"""Phone notifications through the installed web app (Web Push), with calm rules.

Delivery: standard Web Push. Every push is signed with this node's VAPID key (so nobody else can send to our
phones) and its text is encrypted end to end (RFC 8291, aes128gcm), so the relay (Google's or Apple's push
service) only sees that *something* was sent. It needs the internet; the Pi uses the same code when it has it.

Rules (calm by design):
  - "Act now" situation items, "Need help" check-ins and family calls always notify, even in quiet hours.
  - "Soon / near you" items notify only outside quiet hours, and at most `priority_cap` a day per person;
    the rest wait for the morning brief.
  - Other check-ins notify during a family call (that's when people are waiting on them), or always if the
    person chose that. Nobody is notified about their own check-in.
  - Repeats collapse: each item uses a topic, so a phone shows the latest, not a stack.

Everything a person would be notified about also lands in their BUNKR inbox (the bell), quiet hours or not, so
nothing waits only on a phone. Reminders (document renewals, medicine, security) can also go by email: each person
chooses notification, email or both on the Household tab.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PREFS = {"priority": True, "checkins": "calls", "quiet_start": "22:00", "quiet_end": "06:30", "priority_cap": 3,
                 "tz": "America/Denver", "brief": True, "reminders_by": "push", "email": ""}
REMINDER_WAYS = {"push": "Notification", "email": "Email", "both": "Both"}
INBOX_KEEP = 200        # newest notices kept per person
CHECKIN_MODES = {"help": "Only when someone needs help", "calls": "During a family call (and help)", "all": "Every check-in"}
SUBJECT = "mailto:push@bunkr.invalid"   # VAPID contact; deliberately not a real address of the family


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------- keys ----------
def load_keys(key_file):
    """This node's VAPID key pair, made on first use and kept next to the database (mode 600)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    path = Path(key_file)
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
    return key


def public_key_b64(key):
    from cryptography.hazmat.primitives import serialization
    return b64u(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


def vapid_header(key, endpoint, now=None, subject=SUBJECT):
    """Authorization header for one push service: an ES256 JWT for its origin plus our public key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    u = urllib.parse.urlparse(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(now or time.time()) + 12 * 3600, "sub": subject}
    signing = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "." + \
        b64u(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    jwt = signing + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={jwt}, k={public_key_b64(key)}"


# ---------- encryption (RFC 8291 / RFC 8188 aes128gcm) ----------
def _hkdf(salt, ikm, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload, p256dh, auth, salt=None, server_key=None):
    """Encrypt `payload` (bytes) for one subscription. Returns the request body."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ua_pub = unb64u(p256dh)
    auth_secret = unb64u(auth)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = as_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    record = payload + b"\x02"  # a single, last record
    cipher = AESGCM(cek).encrypt(nonce, record, None)
    return salt + struct.pack("!IB", 4096, len(as_pub)) + as_pub + cipher


# ---------- storage ----------
def subscriptions(store, user_ids=None):
    rows = store._rows("SELECT * FROM push_subs")
    return [r for r in rows if user_ids is None or r["user_id"] in user_ids]


def save_subscription(store, user_id, sub, agent=""):
    keys = sub.get("keys") or {}
    endpoint = str(sub.get("endpoint") or "")
    u = urllib.parse.urlparse(endpoint)
    if u.scheme != "https" or not u.netloc or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("not a push subscription")
    if len(unb64u(keys["p256dh"])) != 65 or len(unb64u(keys["auth"])) != 16:
        raise ValueError("bad subscription keys")
    store._exec("INSERT INTO push_subs(user_id, endpoint, p256dh, auth, agent, created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth, "
                "agent=excluded.agent, fails=0", (user_id, endpoint, keys["p256dh"], keys["auth"], agent[:200], _now()))


def remove_subscription(store, endpoint, user_id=None):
    if user_id is None:
        store._exec("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))
    else:
        store._exec("DELETE FROM push_subs WHERE endpoint=? AND user_id=?", (endpoint, user_id))


def prefs(store, user_id):
    rows = store._rows("SELECT push_prefs FROM users WHERE id=?", (user_id,))
    p = dict(DEFAULT_PREFS)
    if rows:
        try:
            p.update(json.loads(rows[0]["push_prefs"] or "{}"))
        except ValueError:
            pass
    return p


def clean_prefs(data):
    import zoneinfo
    out = {}
    if "priority" in data:
        out["priority"] = bool(data["priority"])
    if data.get("checkins") in CHECKIN_MODES:
        out["checkins"] = data["checkins"]
    for k in ("quiet_start", "quiet_end"):
        v = str(data.get(k) or "")
        if v and len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit() and int(v[:2]) < 24 and int(v[3:]) < 60:
            out[k] = v
    if "priority_cap" in data:
        try:
            out["priority_cap"] = max(0, min(20, int(data["priority_cap"])))
        except (TypeError, ValueError):
            pass
    if data.get("tz"):
        try:
            zoneinfo.ZoneInfo(str(data["tz"]))
            out["tz"] = str(data["tz"])
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            pass
    if "brief" in data:
        out["brief"] = bool(data["brief"])
    if data.get("reminders_by") in REMINDER_WAYS:
        out["reminders_by"] = data["reminders_by"]
    if "email" in data:
        addr = str(data["email"] or "").strip()
        if addr and not EMAIL_RE.match(addr):
            raise ValueError("that doesn't look like an email address")
        out["email"] = addr[:200]
    return out


EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def in_quiet_hours(p, at=None):
    import zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(p.get("tz") or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    t = (at or datetime.now(timezone.utc)).astimezone(tz).strftime("%H:%M")
    a, b = p.get("quiet_start") or "", p.get("quiet_end") or ""
    if not a or not b or a == b:
        return False
    return (a <= t < b) if a < b else (t >= a or t < b)


def sent_today(store, user_id, tier, p, at=None):
    import zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(p.get("tz") or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    local = (at or datetime.now(timezone.utc)).astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="seconds")
    return store._rows("SELECT COUNT(*) AS n FROM push_log WHERE user_id=? AND tier=? AND at>=? AND sent>0",
                       (user_id, tier, start))[0]["n"]


# ---------- rules: event -> (message, who) ----------
def plan(store, event, data, at=None):
    """Decide who gets a push for one event. Returns [(user_id, message dict)]; message has tier/title/body/tag/url/urgency."""
    return _plan(store, event, data, at)


def _plan(store, event, data, at=None, inbox=False):
    """inbox=True: who should see it in the app. Same people, but quiet hours and the daily cap don't hold it back."""
    users = store._rows("SELECT id, username FROM users")
    person_user = {r["uid"]: r["user_id"] for r in store._rows("SELECT uid, user_id FROM people WHERE user_id IS NOT NULL")}
    out = []
    calls = store._rows("SELECT drill FROM family_calls WHERE ended_at=''")
    call_active = bool(calls)
    real_call = any(not c["drill"] for c in calls)  # only a real call may wake people up
    for u in users:
        p = prefs(store, u["id"])
        quiet = False if inbox else in_quiet_hours(p, at)
        msg = None
        if event == "situation":
            if data["tier"] == "flash":
                msg = {"tier": "flash", "urgency": "high"}
            elif data["tier"] == "priority" and p["priority"] and not quiet and (inbox or sent_today(store, u["id"], "priority", p, at) < p["priority_cap"]):
                msg = {"tier": "priority", "urgency": "normal"}
            if msg:
                where = (f"at {data['site']}" if data.get("km") == 0 else f"{data['km']} km from {data['site']}") if data.get("site") and data.get("km") is not None else data.get("where_text", "")
                summary = " ".join((data.get("summary") or "").split())
                summary = summary if len(summary) <= 200 else summary[:199].rsplit(" ", 1)[0] + "…"
                msg.update(title=data["title"], body=" · ".join(x for x in (where, summary) if x), tag="sit-" + data["uid"],
                           url="/#sit-" + urllib.parse.quote(data["uid"], safe=""))
        elif event == "checkin":
            if person_user.get(data.get("person_uid")) == u["id"]:
                continue  # your own check-in
            help_ = data["status"] == "help"
            wanted = help_ or p["checkins"] == "all" or (p["checkins"] == "calls" and call_active)
            if wanted and (help_ or not quiet or real_call):
                body = " · ".join(x for x in (data.get("place"), data.get("note"),
                                              ("low battery, may go quiet" + (f" ({data['battery']}%)" if data.get("battery") is not None else ""))
                                              if data.get("battery_low") else "") if x) or "Tap to see the family board."
                msg = {"tier": "family", "urgency": "high" if help_ else "normal", "title": f"{data['person_name']}: {data['status_label']}",
                       "body": body,
                       "tag": "checkin-" + data["person_uid"], "url": "/#home"}
        elif event == "call":
            if data.get("drill"):
                if not quiet:  # a drill never wakes anyone
                    msg = {"tier": "family", "urgency": "normal", "title": f"DRILL: {data['label']} {data.get('place') or ''}".strip(),
                           "body": (data.get("message") or "Practice run. Check in as if it were real.") + " (This is a drill.)",
                           "tag": "call", "url": "/#home"}
            else:
                msg = {"tier": "family", "urgency": "high", "title": f"Family call: {data['label']} {data.get('place') or ''}".strip(),
                       "body": data.get("message") or "Open BUNKR and check in.", "tag": "call", "url": "/#home"}
        elif event == "all_clear":
            if not (data.get("drill") and quiet):
                msg = {"tier": "family", "urgency": "normal", "title": "Drill over" if data.get("drill") else "All clear",
                       "body": f"{data.get('ended_by') or 'Someone'} ended the {'drill' if data.get('drill') else 'family call'}.",
                       "tag": "call", "url": "/#home"}
        elif event == "flight":
            # someone's flight is late, landed, cancelled or diverted: everyone but the traveller, outside quiet hours
            if person_user.get(data.get("person_uid")) != u["id"] and not quiet:
                msg = {"tier": "family", "urgency": "normal", "title": data["title"], "body": data.get("body") or "",
                       "tag": "flight-" + data.get("uid", ""), "url": "/#home"}
        elif event == "brief":
            if p.get("brief") and not quiet:
                msg = {"tier": "brief", "urgency": "low", "title": data.get("title") or "Morning brief", "body": data.get("body") or "",
                       "tag": "brief", "url": "/#news-brief"}
        elif event == "news":
            # raised early (a hack, an outage, a danger in the US or Colorado) or a word this person watches for:
            # "Soon / near you" rules, so it respects quiet hours and the daily cap
            # "family": near someone's town: not held back by the daily cap or the "Soon / near you" switch
            if (data.get("user_ids") is None or u["id"] in data["user_ids"]) and (inbox or (not quiet and (data.get("family") or (
                    p["priority"] and sent_today(store, u["id"], "priority", p, at) < p["priority_cap"])))):
                msg = {"tier": "priority", "urgency": "normal", "title": data["title"], "body": data.get("body") or "",
                       "tag": data.get("tag") or "news", "url": data.get("url") or "/#brief"}
        elif event == "reminder":
            mine = u["id"] == data.get("user_id") or store._rows("SELECT 1 FROM users WHERE id=? AND role='admin'", (u["id"],))
            if mine and (not quiet or data.get("urgent")):
                msg = {"tier": "reminder", "urgency": "high" if data.get("urgent") else "low", "title": data["title"],
                       "body": data.get("body") or "", "tag": data.get("tag") or "reminder", "url": data.get("url") or "/#home"}
        elif event == "test":
            if u["id"] == data.get("user_id") and not inbox:
                msg = {"tier": "test", "urgency": "normal", "title": "BUNKR test", "body": "Notifications work on this phone.",
                       "tag": "test", "url": "/#home"}
        if msg:
            out.append((u["id"], msg))
    return out


# ---------- the inbox (the bell in the app) ----------
def record(store, user_id, msg, at=None):
    """Keep one notice. An unread one with the same tag is updated instead (a flight's latest, not five of them)."""
    t = at or _now()
    vals = (msg["title"][:200], (msg.get("body") or "")[:500], (msg.get("url") or "")[:200], msg.get("tier") or "")
    tag = (msg.get("tag") or "")[:120]
    same = store._rows("SELECT id FROM notices WHERE user_id=? AND tag=? AND tag!='' AND read_at='' ORDER BY id DESC LIMIT 1",
                       (user_id, tag)) if tag else []
    if same:
        store._exec("UPDATE notices SET title=?, body=?, url=?, tier=?, at=? WHERE id=?", (*vals, t, same[0]["id"]))
        return same[0]["id"]
    nid = store._exec("INSERT INTO notices(user_id, at, title, body, url, tier, tag) VALUES(?,?,?,?,?,?,?)", (user_id, t, *vals, tag))
    store._exec("DELETE FROM notices WHERE user_id=? AND id NOT IN (SELECT id FROM notices WHERE user_id=? ORDER BY id DESC LIMIT ?)",
                (user_id, user_id, INBOX_KEEP))
    return nid


def notices(store, user_id, limit=50):
    rows = store._rows("SELECT id, at, tier, title, body, url, tag, read_at FROM notices WHERE user_id=? ORDER BY id DESC LIMIT ?",
                       (user_id, limit))
    return {"items": rows, "unread": unread(store, user_id)}


def unread(store, user_id):
    return store._rows("SELECT COUNT(*) AS n FROM notices WHERE user_id=? AND read_at=''", (user_id,))[0]["n"] if user_id else 0


def mark_read(store, user_id, ids=None):
    """ids None: all of this person's notices."""
    t = _now()
    if ids is None:
        store._exec("UPDATE notices SET read_at=? WHERE user_id=? AND read_at=''", (t, user_id))
    else:
        for i in ids[:200]:
            store._exec("UPDATE notices SET read_at=? WHERE user_id=? AND id=? AND read_at=''", (t, user_id, int(i)))
    return unread(store, user_id)


def wants(store, user_id, way, p=None):
    """For reminders: does this person want them by 'push' or by 'email'?"""
    p = p or prefs(store, user_id)
    if way == "email":
        return p.get("reminders_by") in ("email", "both") and bool(p.get("email"))
    return p.get("reminders_by") in ("push", "both") or not p.get("email")  # no address yet: a push, not nothing


# ---------- delivery ----------
def deliver(store, key, sub, msg, opener=None, ttl=None):
    """Send one message to one subscription. Returns the HTTP status (0 on network error)."""
    body = json.dumps({k: msg.get(k, "") for k in ("title", "body", "tag", "url", "tier")}).encode()
    data = encrypt(body, sub["p256dh"], sub["auth"])
    ttl = ttl if ttl is not None else (6 * 3600 if msg.get("urgency") == "high" else 24 * 3600)
    headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl),
               "Urgency": msg.get("urgency", "normal"), "Authorization": vapid_header(key, sub["endpoint"])}
    if msg.get("tag"):
        headers["Topic"] = hashlib.sha256(msg["tag"].encode()).hexdigest()[:32]  # repeats replace each other
    req = urllib.request.Request(sub["endpoint"], data=data, method="POST", headers=headers)
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=20) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except (urllib.error.URLError, OSError):
        status = 0
    if status in (404, 410):  # the phone unsubscribed or the app was removed
        remove_subscription(store, sub["endpoint"])
    elif 200 <= status < 300:
        store._exec("UPDATE push_subs SET last_ok=?, fails=0 WHERE endpoint=?", (_now(), sub["endpoint"]))
    else:
        store._exec("UPDATE push_subs SET fails=fails+1 WHERE endpoint=?", (sub["endpoint"],))
    return status


def send(store, key, event, data, opener=None, at=None, push=True, mail=None, phone=False):
    """Plan and deliver one event: into each person's inbox, by email for reminders when they chose it (mail(to, msg)),
    as pushes (push=False or no key: none) and, with phone=True, to the paired BUNKR phone apps' notification stream
    (bunkr/phone.py: same people, same rules, no internet needed). Returns [(user_id, status)] for the pushes."""
    if event != "test":
        for user_id, msg in _plan(store, event, data, at, inbox=True):
            record(store, user_id, msg)
            if mail and msg["tier"] == "reminder":
                p = prefs(store, user_id)
                if wants(store, user_id, "email", p):
                    mail(p["email"], msg)
    results = []
    pushing = push and key is not None
    if not pushing and not phone:
        return results
    for user_id, msg in plan(store, event, data, at):
        if msg["tier"] == "reminder" and not wants(store, user_id, "push"):
            continue
        if phone:
            from . import phone as phone_app
            phone_app.publish(store, user_id, event, msg)
        if not pushing:
            continue
        subs = subscriptions(store, {user_id})
        ok = 0
        for sub in subs:
            st = deliver(store, key, sub, msg, opener)
            ok += 1 if 200 <= st < 300 else 0
            results.append((user_id, st))
        store._exec("INSERT INTO push_log(user_id, at, tier, title, sent) VALUES(?,?,?,?,?)",
                    (user_id, _now(), msg["tier"], msg["title"][:200], ok))
    return results


class Notifier:
    """Listens to check-ins, calls and situation items and sends pushes in a background thread."""

    def __init__(self, store, key_file, enabled=lambda: True, mail=None):
        self.store, self.key_file, self.enabled, self.mail = store, key_file, enabled, mail
        self._key = None
        self._lock = threading.Lock()

    @property
    def key(self):
        with self._lock:
            if self._key is None:
                self._key = load_keys(self.key_file)
            return self._key

    def __call__(self, event, data):
        threading.Thread(target=self._run, args=(event, dict(data)), daemon=True).start()

    def _run(self, event, data):
        try:
            on = self.enabled()  # a node that has reached the hub lately leaves pushes and email to the hub
            push = on and bool(self.store._rows("SELECT 1 FROM push_subs LIMIT 1"))
            send(self.store, self.key if push else None, event, data, push=push, mail=self.mail if on else None, phone=on)
        except Exception:
            pass  # never let a notification problem reach a request
