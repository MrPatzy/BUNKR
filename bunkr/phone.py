"""The BUNKR phone app, hub side: pairing, a key for each phone, and the notification stream.

Pairing: a signed-in person asks for a one-time code (5 minutes, works once) and shows it as a QR code. The app trades
it for a phone key. Only the key's SHA-256 is stored. The key signs the app in as that person with that person's normal
access (see server `_session`); it is renewed each time it is used and stops working when removed (or after 90 days
without use).

Stream: every notification that would go out as Web Push is also kept here as an event with a number, per person who has
a paired phone, and an open connection (Server-Sent Events) sends it at once. A phone that reconnects says the last
number it got and receives what it missed (the newest BACKLOG kept, no more than BACKLOG_DAYS old). Nothing here
calls out to the internet.
"""

import hashlib
import json
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

CODE_MINUTES = 5
KEY_PREFIX = "bkp_"
KEY_DAYS = 90           # a phone key unused this long stops working
BACKLOG = 100           # events kept per person
BACKLOG_DAYS = 3
HEARTBEAT = 15          # seconds between keep-alive lines on the stream
TOUCH_SECONDS = 60      # "last seen" is written at most this often per phone
MAX_PHONES = 20         # per person

_cond = threading.Condition()
_seq = 0                # bumps when an event is added or a phone is removed, to wake the open streams
_open = {}              # phone id -> number of open streams
_touched = {}           # phone id -> time.monotonic() of the last "last seen" write


def _now():
    return datetime.now(timezone.utc)


def _iso(d):
    return d.isoformat(timespec="seconds")


def _hash(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _wake():
    global _seq
    with _cond:
        _seq += 1
        _cond.notify_all()


# ---------- pairing ----------
def new_code(store, user_id):
    """A one-time code for this person (a new one replaces the last). Returns (code, expires_at)."""
    code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
    expires = _iso(_now() + timedelta(minutes=CODE_MINUTES))
    store._exec("INSERT INTO phone_codes(user_id, code_hash, expires_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET code_hash=excluded.code_hash, expires_at=excluded.expires_at",
                (user_id, _hash(code), expires))
    return code, expires


def pair_link(code, hubs):
    """What the QR code holds: bunkr://pair?code=...&hub=...&hub=... (as many hub addresses as fit a QR code)."""
    from urllib.parse import urlencode
    link = ""
    for n in range(len(hubs), -1, -1):
        link = "bunkr://pair?" + urlencode([("code", code)] + [("hub", h) for h in hubs[:n]])
        if len(link.encode()) <= 200:
            break
    return link


def exchange(store, code, name=""):
    """Trade a code for a phone key. Returns {"key", "id", "username", "role"}, or None (wrong, used or expired)."""
    code = (code or "").strip().upper()
    if not code:
        return None
    with store._lock:
        rows = store._rows("SELECT user_id, expires_at FROM phone_codes WHERE code_hash=?", (_hash(code),))
        if not rows:
            return None
        store._exec("DELETE FROM phone_codes WHERE code_hash=?", (_hash(code),))   # it works once, even if it had expired
        if rows[0]["expires_at"] < _iso(_now()):
            return None
        uid = rows[0]["user_id"]
        user = store._rows("SELECT username, role FROM users WHERE id=?", (uid,))
        if not user:
            return None
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        t = _iso(_now())
        pid = store._exec("INSERT INTO phone_keys(user_id, key_hash, name, created_at, last_seen) VALUES(?,?,?,?,?)",
                          (uid, _hash(key), " ".join(str(name or "").split())[:60] or "Phone", t, t))
        old = store._rows("SELECT id FROM phone_keys WHERE user_id=? ORDER BY id DESC LIMIT -1 OFFSET ?", (uid, MAX_PHONES))
        for r in old:
            remove(store, r["id"])
    return {"key": key, "id": pid, "username": user[0]["username"], "role": user[0]["role"]}


# ---------- keys ----------
def is_key(token):
    return bool(token) and token.startswith(KEY_PREFIX)


def user_for_key(store, token):
    """The person a phone key signs in as (with their normal access), or None. Using it renews it."""
    if not is_key(token):
        return None
    rows = store._rows("""SELECT k.id, k.last_seen, u.id AS uid, u.username, u.role, u.view
                          FROM phone_keys k JOIN users u ON u.id = k.user_id WHERE k.key_hash=?""", (_hash(token),))
    if not rows:
        return None
    k = rows[0]
    if k["last_seen"] < _iso(_now() - timedelta(days=KEY_DAYS)):
        remove(store, k["id"])
        return None
    touch(store, k["id"])
    return {"id": k["uid"], "username": k["username"], "role": k["role"], "view": k["view"] or "", "scope": "full",
            "phone_id": k["id"]}


def touch(store, phone_id):
    import time
    t = time.monotonic()
    if t - _touched.get(phone_id, -1e9) >= TOUCH_SECONDS:
        _touched[phone_id] = t
        store._exec("UPDATE phone_keys SET last_seen=? WHERE id=?", (_iso(_now()), phone_id))


def _alive(store, phone_id):
    rows = store._rows("SELECT last_seen FROM phone_keys WHERE id=?", (phone_id,))
    return bool(rows) and rows[0]["last_seen"] >= _iso(_now() - timedelta(days=KEY_DAYS))


def phones(store, user_id=None):
    """Paired phones: one person's, or everyone's (user_id None). Never the key itself."""
    rows = store._rows("""SELECT k.id, k.user_id, u.username, k.name, k.created_at, k.last_seen
                          FROM phone_keys k JOIN users u ON u.id = k.user_id
                          WHERE (? IS NULL OR k.user_id=?) ORDER BY k.id""", (user_id, user_id))
    for r in rows:
        r["connected"] = _open.get(r["id"], 0) > 0
    return rows


def phone_owner(store, phone_id):
    rows = store._rows("SELECT user_id FROM phone_keys WHERE id=?", (phone_id,))
    return rows[0]["user_id"] if rows else None


def remove(store, phone_id):
    """Cut a phone off: its key stops working and any open stream ends at once."""
    store._exec("DELETE FROM phone_keys WHERE id=?", (phone_id,))
    _touched.pop(phone_id, None)
    _wake()


def has_phones(store, user_id):
    return bool(store._rows("SELECT 1 FROM phone_keys WHERE user_id=? LIMIT 1", (user_id,)))


# ---------- the app itself ----------
APK_ID = "bunkr-android"        # the downloads-store entry (bunkr/releases.py): apps/bunkr-android/<number>/bunkr-<number>.apk
_APK = re.compile(r"^bunkr-(\d+)\.apk$")


def latest_apk(root):
    """The newest app file the hub holds: {"version": N, "path": Path, "size": bytes}, or None. N only goes up
    (it is the build number, which is also the app's Android version code)."""
    best = None
    for p in (Path(root) / "apps" / APK_ID).glob("*/bunkr-*.apk"):
        m = _APK.match(p.name)
        if m and p.is_file() and (best is None or int(m.group(1)) > best["version"]):
            best = {"version": int(m.group(1)), "path": p, "size": p.stat().st_size}
    return best


def apk_info(root):
    """What the app and the web page need to offer an update. {"available": False} when the hub has no app file."""
    apk = latest_apk(root)
    if not apk:
        return {"available": False}
    h = hashlib.sha256()
    with open(apk["path"], "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"available": True, "version": apk["version"], "size": apk["size"], "sha256": h.hexdigest(),
            "url": "/app/bunkr.apk"}


# ---------- events ----------
def screen_of(url):
    """The screen to open: "/#home" -> "home", "/#sit-ab12" -> "sit-ab12"."""
    return (url or "").partition("#")[2]


def _event(r):
    return {"id": r["id"], "at": r["at"], "event": r["event"], "tier": r["tier"], "title": r["title"], "body": r["body"],
            "tag": r["tag"], "url": r["url"], "screen": screen_of(r["url"])}


def publish(store, user_id, event, msg):
    """Keep one notification for this person's phones and wake their open streams. Returns its number (None: no phones)."""
    if not has_phones(store, user_id):
        return None
    t = _now()
    n = store._exec("INSERT INTO app_events(user_id, at, event, tier, title, body, tag, url) VALUES(?,?,?,?,?,?,?,?)",
                    (user_id, _iso(t), event, msg.get("tier") or "", (msg.get("title") or "")[:200], (msg.get("body") or "")[:500],
                     (msg.get("tag") or "")[:120], (msg.get("url") or "")[:200]))
    store._exec("DELETE FROM app_events WHERE user_id=? AND (at < ? OR id NOT IN "
                "(SELECT id FROM app_events WHERE user_id=? ORDER BY id DESC LIMIT ?))",
                (user_id, _iso(t - timedelta(days=BACKLOG_DAYS)), user_id, BACKLOG))
    _wake()
    return n


def latest_id(store, user_id):
    return store._rows("SELECT COALESCE(MAX(id), 0) AS n FROM app_events WHERE user_id=?", (user_id,))[0]["n"]


def events_after(store, user_id, last_id, limit=BACKLOG):
    return [_event(r) for r in store._rows("SELECT * FROM app_events WHERE user_id=? AND id>? ORDER BY id LIMIT ?",
                                           (user_id, last_id, limit))]


def sse(ev):
    return f"id: {ev['id']}\nevent: notice\ndata: {json.dumps(ev, separators=(',', ':'))}\n\n".encode()


def follow(store, phone_id, last_id=None, beat=HEARTBEAT):
    """The stream for one phone, as chunks of bytes to write. Ends when the phone is removed (or its key lapses).
    last_id: the last event number the app got; what came after is sent first. None: only what happens from now on."""
    user_id = phone_owner(store, phone_id)
    if user_id is None:
        return
    newest = latest_id(store, user_id)
    last_id = newest if last_id is None or last_id > newest else max(0, last_id)   # a number from the future: start over
    _open[phone_id] = _open.get(phone_id, 0) + 1
    try:
        yield f"retry: 5000\nevent: ready\ndata: {json.dumps({'last_id': last_id})}\n\n".encode()
        while True:
            with _cond:
                seen = _seq
            if not _alive(store, phone_id):
                return
            evs = events_after(store, user_id, last_id)
            for ev in evs:
                last_id = ev["id"]
                yield sse(ev)
            if evs:
                continue
            with _cond:
                _cond.wait_for(lambda: _seq != seen, timeout=beat)
                woken = _seq != seen
            if not woken:
                touch(store, phone_id)
                yield b": heartbeat\n\n"
    finally:
        _open[phone_id] = max(0, _open.get(phone_id, 1) - 1)
