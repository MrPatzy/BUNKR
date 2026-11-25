"""Family check-ins over MeshCore: private channels on the mesh.

- A companion radio on the hub or a Pi (bunkr/meshcore.py) holds BUNKR's channels, one per slot. Each channel has a
  name, a 16-byte key and a kind:
    family  the household: check-ins, every bot command, family calls and "needs help" go out
    group   a small trusted circle (Circle: Jordan, Mom): check-ins from people BUNKR knows, the basic commands only
            (WHO ALERTS FLIGHT NEWS PING; no inventory, no Home Assistant), "needs help" goes out, calls don't
  Each of those is a switch per channel. Family members see the join link only for channels marked "shown to the
  family"; the rest are admin-only. People add a channel to the MeshCore app with its meshcore:// link or its key.
- Messages on that channel arrive as "Name: text". The name is matched to a person (their "name on the mesh", else
  their BUNKR name); the text becomes a check-in when it says so:
    SAFE / OK / I'M SAFE                  -> I'm safe
    OMW / ON MY WAY / MOVING / LEAVING    -> On my way
    HELP / SOS / MAYDAY / NEED HELP       -> Need help
  Anything after the word is the note ("SAFE at Grandma's"). MECP reports (Mesh-Client and other EMCOMM apps,
  "MECP/<severity>/<codes> text") count too: severity 0 or 1 (MAYDAY, URGENT) is Need help, B03 ("I am OK") is
  I'm safe; drills (D01, D02) are logged but never check anyone in.
- Going out on the channel: family calls ("BUNKR CALL: Evacuate to Grandma's. Reply SAFE, OMW or HELP"), all clear,
  and anyone who checks in "Need help" in the app. Each mesh check-in gets a short reply so the sender knows BUNKR
  heard it. Nothing a check-in from the mesh causes is sent back out again (no loops).
- Alerts go out too, per channel ("alerts"): all = situation flash and priority alerts, family flight changes and
  early news; flash = only flash alerts; off. Outside flash alerts they wait out quiet hours (22:00-06:30), the same
  one is never sent twice, and a channel gets at most ALERTS_PER_HOUR of them. Reminders never go to the mesh.
- A short daily mesh brief (weather at home, alerts, top headlines) at the time an admin picks, on family channels.
- Each radio is set up on the node it's plugged into (a Heltec on Wi-Fi can be reached by the hub over TCP).
  Check-ins it makes travel to the others with the normal record sync, marked "via MeshCore".
"""

import base64
import binascii
import json
import re
import secrets
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from . import checkins, meshbot, meshcore

import collections

DEFAULTS = {"enabled": False, "connect": "", "channels": [], "bot_off": [], "brief_at": "", "brief_user": 0}
ALERT_LEVELS = {"all": "Alerts, flights and early news", "flash": "Only flash alerts", "off": "No alerts"}
ALERTS_PER_HOUR = 6
QUIET = ("22:00", "06:30")
SEND_GAP = 2.0      # seconds between our own messages, so a 3-part answer doesn't jam the channel
KINDS = {"family": "Family (the household)", "group": "Trusted group"}
BOT_LEVELS = {"full": "Every command", "basic": "WHO ALERTS FLIGHT NEWS PING only", "off": "No commands"}
CHANNEL = {  # a new channel of each kind starts like this
    "family": {"checkins": True, "bot": "full", "send_calls": True, "send_help": True, "reply": True, "shared": True, "alerts": "all"},
    "group": {"checkins": True, "bot": "basic", "send_calls": False, "send_help": True, "reply": True, "shared": False, "alerts": "flash"},
}
MAX_CHANNELS = 6
WORDS = {
    "safe": ["i'm safe", "im safe", "i am safe", "all good", "safe"],
    "moving": ["on my way", "omw", "moving", "leaving", "en route", "heading"],
    "help": ["need help", "help", "sos", "mayday", "emergency", "911"],
}
_WORD_RE = [(st, re.compile(r"^(?:" + "|".join(re.escape(w) for w in sorted(ws, key=len, reverse=True)) + r")\b[\s:,.!-]*(.*)$", re.I | re.S))
            for st, ws in (("help", WORDS["help"]), ("moving", WORDS["moving"]), ("safe", WORDS["safe"]))]
MECP_RE = re.compile(r"^MECP/([0-3])/((?:[A-Z]\d{2}[,;/ ]?)*)\s*(.*)$", re.S)
LOG_KEEP = 300
_heard = threading.local()  # the channel a check-in being handled right now came in on


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- settings (per node: the radio is plugged into this machine) ----------
def _channel(c):
    kind = c.get("kind") if c.get("kind") in KINDS else "family"
    ch = {"name": str(c.get("name") or "")[:31], "index": c.get("index"), "secret": str(c.get("secret") or ""), "kind": kind,
          **CHANNEL[kind]}
    for k in CHANNEL[kind]:
        if k in c:
            ch[k] = c[k] if k in ("bot", "alerts") else bool(c[k])
    if ch["bot"] not in BOT_LEVELS:
        ch["bot"] = CHANNEL[kind]["bot"]
    if ch["alerts"] not in ALERT_LEVELS:
        ch["alerts"] = CHANNEL[kind]["alerts"]
    return ch


def settings(store):
    raw = store.get_raw("mesh_settings", "") or ""
    try:
        s = json.loads(raw) if isinstance(raw, str) and raw else (raw if isinstance(raw, dict) else {})
    except ValueError:
        s = {}
    out = {**DEFAULTS, **{k: v for k, v in s.items() if k in DEFAULTS}}
    out["channels"] = [_channel(c) for c in out["channels"] if isinstance(c, dict)]
    out["bot_off"] = [c for c in out["bot_off"] if c in meshbot.SWITCHABLE] if isinstance(out["bot_off"], list) else []
    out["brief_at"] = out["brief_at"] if isinstance(out["brief_at"], str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", out["brief_at"]) else ""
    out["brief_user"] = out["brief_user"] if isinstance(out["brief_user"], int) and not isinstance(out["brief_user"], bool) and out["brief_user"] > 0 else 0
    return out


def channel(s, index):
    return next((c for c in s["channels"] if c["index"] == index), None)


def parse_secret(text):
    """A channel key as 32 hex characters, from hex, base64, or a meshcore:// link someone shared."""
    t = str(text or "").strip()
    if t.startswith("meshcore://"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(t).query)
        t = (q.get("secret") or [""])[0].strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", t):
        sec = t.lower()
    else:
        try:
            raw = base64.b64decode(t + "=" * (-len(t) % 4), validate=True)
        except (ValueError, binascii.Error):
            raw = b""
        if len(raw) != 16:
            raise ValueError("a channel key is 32 hex characters (or paste the meshcore:// link)")
        sec = raw.hex()
    if sec == "0" * 32:
        raise ValueError("that key is all zeros: make a new one")
    return sec


def link_name(text):
    """The channel name inside a meshcore:// link, if that's what was pasted."""
    t = str(text or "").strip()
    if not t.startswith("meshcore://"):
        return ""
    return (urllib.parse.parse_qs(urllib.parse.urlparse(t).query).get("name") or [""])[0].strip()[:31]


def _clean_channel(c, old):
    """One channel from the admin's form, checked."""
    kind = c.get("kind") if c.get("kind") in KINDS else "family"
    key_text = str(c.get("secret") or "").strip()
    name = " ".join(str(c.get("name") or link_name(key_text) or "").split())[:31]
    if not name:
        raise ValueError("give each channel a name")
    try:
        index = int(c.get("index"))
    except (TypeError, ValueError):
        raise ValueError(f"{name}: the slot is a number")
    if not 1 <= index <= 39:
        raise ValueError(f"{name}: use a slot from 1 up (slot 0 is the public channel)")
    if c.get("new_secret") or not key_text:
        secret = secrets.token_hex(16)
    else:
        try:
            secret = parse_secret(key_text)
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}")
    if c.get("bot", CHANNEL[kind]["bot"]) not in BOT_LEVELS:
        raise ValueError(f"{name}: commands are full, basic or off")
    if c.get("alerts", CHANNEL[kind]["alerts"]) not in ALERT_LEVELS:
        raise ValueError(f"{name}: alerts are all, flash or off")
    return _channel({**CHANNEL[kind], **{k: c[k] for k in CHANNEL[kind] if k in c}, "name": name, "index": index,
                     "secret": secret, "kind": kind})


def save_settings(store, data):
    s = settings(store)
    if "enabled" in data:
        s["enabled"] = bool(data["enabled"])
    if "connect" in data:
        c = str(data["connect"] or "").strip()
        if c and not (re.fullmatch(r"/dev/[\w./-]{1,60}", c) or re.fullmatch(r"tcp:[\w.-]{1,100}(:\d{1,5})?", c)):
            raise ValueError("connect to a serial port (/dev/ttyUSB0) or tcp:host:port (tcp:192.168.1.60:5000)")
        s["connect"] = c
    if "bot_off" in data:
        off = data["bot_off"]
        if not isinstance(off, list) or any(c not in meshbot.SWITCHABLE for c in off):
            raise ValueError("unknown bot command")
        s["bot_off"] = sorted(set(off))
    if "brief_at" in data:
        t = str(data["brief_at"] or "").strip()
        if t and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise ValueError("the daily mesh brief time is like 07:00 (or empty for none)")
        s["brief_at"] = t
    if "brief_user" in data:  # 0 or empty: the household brief; otherwise one person's
        try:
            who = int(data["brief_user"] or 0)
        except (TypeError, ValueError):
            raise ValueError("pick a person from the list, or the household brief")
        if who and not store._rows("SELECT 1 FROM users WHERE id=?", (who,)):
            raise ValueError("that person has no login")
        s["brief_user"] = max(0, who)
    if "channels" in data:
        rows = data["channels"]
        if not isinstance(rows, list) or len(rows) > MAX_CHANNELS:
            raise ValueError(f"up to {MAX_CHANNELS} channels")
        chans = [_clean_channel(c if isinstance(c, dict) else {}, s["channels"]) for c in rows]
        for attr, what in (("index", "slot"), ("name", "name"), ("secret", "key")):
            vals = [str(c[attr]).lower() for c in chans]
            if len(vals) != len(set(vals)):
                raise ValueError(f"two channels have the same {what}")
        s["channels"] = chans
    if not s["channels"]:  # a first save: start with the family channel
        s["channels"] = [_channel({"name": "Family", "index": 1, "secret": secrets.token_hex(16), "kind": "family"})]
    if s["enabled"] and not s["connect"]:
        raise ValueError("say where the radio is before turning this on")
    store.set_raw("mesh_settings", json.dumps(s))
    return s


def channel_link(ch):
    """Opens the MeshCore app on a phone and adds the channel."""
    return "meshcore://channel/add?" + urllib.parse.urlencode({"name": ch["name"], "secret": ch["secret"]})


# ---------- reading messages ----------
def split_sender(text):
    """Channel text is "Name: message"."""
    m = re.match(r"^([^:\n]{1,32}):\s?(.*)$", text or "", re.S)
    return (m.group(1).strip(), m.group(2).strip()) if m else ("", (text or "").strip())


def interpret(text):
    """What a message means: {"status", "note", "mecp", "drill"} or None if it isn't a check-in."""
    t = (text or "").strip()
    m = MECP_RE.match(t)
    if m:
        sev, codes, rest = int(m.group(1)), re.findall(r"[A-Z]\d{2}", m.group(2)), m.group(3).strip()
        drill = any(c in ("D01", "D02") for c in codes)
        status = "help" if sev <= 1 else "safe" if "B03" in codes else None
        if status is None:  # SAFETY / ROUTINE reports: "help" or "safe" words in the text still count
            w = interpret(rest)
            status = w["status"] if w else None
        label = ("MAYDAY", "URGENT", "", "")[sev]
        note = " ".join(x for x in (label, rest) if x) + (f" (MECP {' '.join(codes)})" if codes else " (MECP)")
        return {"status": status, "note": note[:300], "mecp": True, "drill": drill} if status or drill else None
    for status, rx in _WORD_RE:
        m = rx.match(t)
        if m:
            note, level, low = battery_words(m.group(1).strip())
            return {"status": status, "note": note[:300], "mecp": False, "drill": False, "battery": level, "battery_low": low}
    return None


_BATT_RE = re.compile(r"^(?:(?:low\s*(?:batt(?:ery)?)?|batt(?:ery)?\s*low)\b|(?:batt(?:ery)?\s*)?(\d{1,3})\s*%(?:\s*batt(?:ery)?)?)[\s,.;:!-]*", re.I)


def battery_words(note):
    """ "SAFE low", "SAFE 15%", "OMW batt 30% at the gas station": the battery bit, and what's left of the note."""
    m = _BATT_RE.match(note or "")
    if not m or (m.group(1) and int(m.group(1)) > 100):
        return note, None, False
    level = int(m.group(1)) if m.group(1) else None
    return note[m.end():].strip(), level, level is None or level <= checkins.LOW_BATTERY


# ---------- nodes: every MeshCore node the radio has heard, by public key, and whose it is ----------
KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def parse_key(text):
    """A node's public key: 64 hex characters, or a meshcore:// link with a public_key in it."""
    t = str(text or "").strip()
    if t.startswith("meshcore://"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(t).query)
        t = (q.get("public_key") or q.get("pubkey") or q.get("key") or [""])[0]
    t = t.strip().lower().replace(" ", "").replace(":", "")
    if not KEY_RE.match(t):
        raise ValueError("a node's public key is 64 hex characters (the MeshCore app shows it under the contact)")
    return t


def save_nodes(store, contacts, at=None):
    """Contacts from the radio (or one new advert): new ones are added, known ones get their name, place and hops
    updated. Who a node belongs to is never changed here."""
    at = at or now()
    n = 0
    for c in contacts:
        key = (c.get("public_key") or "").lower()
        if not KEY_RE.match(key):
            continue
        store._exec("INSERT INTO mesh_nodes(public_key, name, node_type, lat, lon, hops, source, first_seen, last_seen) "
                    "VALUES(?,?,?,?,?,?,'radio',?,?) ON CONFLICT(public_key) DO UPDATE SET name=excluded.name, "
                    "node_type=excluded.node_type, lat=COALESCE(excluded.lat, lat), lon=COALESCE(excluded.lon, lon), "
                    "hops=excluded.hops, source='radio', last_seen=excluded.last_seen",
                    (key, (c.get("name") or "")[:32], int(c.get("node_type") or 0), c.get("lat"), c.get("lon"), c.get("hops"), at, at))
        n += 1
    return n


def add_node(store, key, name="", person_uid="", label=""):
    """A node typed in by hand (its key from the MeshCore app), before the radio has heard it."""
    key = parse_key(key)
    _check_person(store, person_uid)
    store._exec("INSERT INTO mesh_nodes(public_key, name, node_type, person_uid, label, source, first_seen) VALUES(?,?,1,?,?,'manual',?) "
                "ON CONFLICT(public_key) DO UPDATE SET person_uid=excluded.person_uid, label=excluded.label, "
                "name=CASE WHEN name='' THEN excluded.name ELSE name END",
                (key, " ".join(str(name or "").split())[:32], person_uid or "", " ".join(str(label or "").split())[:40], now()))
    return key


def _check_person(store, uid):
    if uid and not store._rows("SELECT 1 FROM people WHERE uid=?", (uid,)):
        raise ValueError("no such person")


def assign_node(store, key, person_uid="", label=None):
    key = parse_key(key)
    if not store._rows("SELECT 1 FROM mesh_nodes WHERE public_key=?", (key,)):
        raise LookupError("BUNKR doesn't know that node")
    _check_person(store, person_uid)
    store._exec("UPDATE mesh_nodes SET person_uid=? WHERE public_key=?", (person_uid or "", key))
    if label is not None:
        store._exec("UPDATE mesh_nodes SET label=? WHERE public_key=?", (" ".join(str(label).split())[:40], key))


def forget_node(store, key):
    store._exec("DELETE FROM mesh_nodes WHERE public_key=?", (parse_key(key),))


def node_heard(store, name, at=None):
    if name:
        store._exec("UPDATE mesh_nodes SET last_heard=? WHERE lower(name)=?", (at or now(), name.strip().lower()))


def nodes(store):
    """Everything known, people's nodes first, then by when it was last heard from."""
    rows = store._rows("SELECT n.*, p.name AS person_name FROM mesh_nodes n LEFT JOIN people p ON p.uid = n.person_uid "
                       "ORDER BY (n.person_uid = ''), max(n.last_heard, n.last_seen) DESC")
    names = {}
    for r in rows:
        r["type_name"] = meshcore.NODE_TYPES.get(r["node_type"], "unknown")
        names.setdefault(r["name"].strip().lower(), set()).add(r["person_uid"])
    for r in rows:  # one name used by nodes of different people: a channel message can't say which one sent it
        r["name_clash"] = bool(r["name"]) and len(names.get(r["name"].strip().lower(), ())) > 1
    return rows


def person_for(store, name):
    """The family member using this name on the mesh. Channel messages carry only the sender's node name, so: a node
    with that name assigned to someone (unless two people's nodes share it), then their mesh name, then their BUNKR name."""
    n = (name or "").strip().lower()
    if not n:
        return None
    owners = {r["person_uid"] for r in store._rows("SELECT person_uid FROM mesh_nodes WHERE lower(name)=? AND person_uid != ''", (n,))}
    if len(owners) == 1:
        rows = store._rows("SELECT uid, name, role, mesh_name FROM people WHERE uid=?", (owners.pop(),))
        if rows:
            return rows[0]
    people = store._rows("SELECT uid, name, role, mesh_name FROM people")
    for p in people:
        if (p["mesh_name"] or "").strip().lower() == n:
            return p
    for p in people:
        if p["name"].strip().lower() == n:
            return p
    first = [p for p in people if p["name"].strip().lower().split()[0] == n.split()[0]] if n.split() else []
    return first[0] if len(first) == 1 else None


def log(store, direction, text, sender="", result="", at=None, channel=""):
    store._exec("INSERT INTO mesh_log(at, direction, sender, text, result, channel) VALUES(?,?,?,?,?,?)",
                (at or now(), direction, sender[:40], text[:300], result[:120], channel[:31]))
    store._exec("DELETE FROM mesh_log WHERE id <= (SELECT id FROM mesh_log ORDER BY id DESC LIMIT 1 OFFSET ?)", (LOG_KEEP,))


def recent(store, n=30):
    return store._rows("SELECT at, direction, sender, text, result, channel FROM mesh_log ORDER BY id DESC LIMIT ?", (n,))


def node_pos(store, name, max_age_days=14):
    """(lat, lon) a node with this name last shared in its advert, if recent and only one node has the name."""
    rows = store._rows("SELECT lat, lon, last_seen FROM mesh_nodes WHERE lower(name)=? AND lat IS NOT NULL AND lon IS NOT NULL",
                       ((name or "").strip().lower(),))
    if len(rows) != 1 or (rows[0]["lat"] == 0 and rows[0]["lon"] == 0):
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(rows[0]["last_seen"])).days
    except (TypeError, ValueError):
        age = 0
    return (rows[0]["lat"], rows[0]["lon"]) if age <= max_age_days else None


def path_names(store, ids):
    """Repeater IDs from a packet's path (the start of each one's public key, as hex) -> names. Repeaters and room
    servers are looked for first; when two share an ID the most recently heard is named, with a "?"."""
    out = []
    for h in ids:
        rows = store._rows("SELECT name, node_type FROM mesh_nodes WHERE public_key LIKE ? AND name != '' "
                           "ORDER BY (node_type IN (2, 3)) DESC, max(last_heard, last_seen) DESC", (h.lower() + "%",))
        rep_rows = [r for r in rows if r["node_type"] in (2, 3)] or rows
        if not rep_rows:
            out.append(h.upper())
        else:
            out.append(rep_rows[0]["name"][:20] + ("?" if len(rep_rows) > 1 else ""))
    return out


def handle(store, msg, s, ctx=None):
    """One message from the radio. Returns the reply to send on the same channel (a line, a list of lines or a
    meshbot.Later), or None. ctx: what the bot can look up (bunkr/meshbot.py), or None for check-ins only."""
    ch = channel(s, msg.get("channel")) if msg.get("kind") == "channel" else None
    if not ch:
        return None  # the public channel, others' channels and direct messages: not ours
    name = ch["name"]
    sender, body = split_sender(msg.get("text", ""))
    node_heard(store, sender)
    meaning = interpret(body) if ch["checkins"] else None
    if not meaning:
        if ch["bot"] != "off" and ctx is not None:
            hops = meshcore.hops(msg.get("path_len"))
            extra = {"snr": msg.get("snr"), "hops": hops, "level": ch["bot"], "off": s.get("bot_off") or [],
                     "sender_pos": node_pos(store, sender) if sender else None}
            heard = msg.get("heard")  # the raw packet, when the radio's log caught it (bunkr.mesh.Link)
            me = (ctx.get("me") or "BUNKR")
            if heard is not None or hops is not None:
                extra["trace"] = lambda: meshinfo_trace(store, heard, sender, me, msg.get("snr"), hops)
            reply = meshbot.answer(store, person_for(store, sender), body, {**ctx, **extra})
            if reply:
                log(store, "in", body, sender, "command", channel=name)
                if isinstance(reply, meshbot.Later):
                    return reply
                return [meshcore.fit(r, 120) for r in reply] if isinstance(reply, list) else meshcore.fit(reply, 120)
        log(store, "in", body, sender, "chat", channel=name)
        return None
    if meaning["drill"] and not meaning["status"]:
        log(store, "in", body, sender, "MECP drill (logged only)", channel=name)
        return None
    p = person_for(store, sender)
    if not p:
        log(store, "in", body, sender, f"unknown sender {sender!r}", channel=name)
        return f"BUNKR doesn't know {sender} yet. Set their name on the mesh on the Family tab." if sender and ch["reply"] else None
    if meaning["drill"]:
        log(store, "in", body, sender, "MECP drill (logged only)", channel=name)
        return None
    _heard.channel = ch["index"]
    try:  # listeners run inside this: "needs help" goes on to the other channels, never back to this one
        checkins.add_checkin(store, p["uid"], meaning["status"], meaning["note"], via="mesh", by_name="",
                             battery_level=meaning.get("battery"), battery_low=meaning.get("battery_low"))
    finally:
        _heard.channel = None
    log(store, "in", body, sender, f"checked in {p['name']}: {checkins.STATUSES[meaning['status']]}", channel=name)
    said = {"safe": "is safe", "moving": "is on the way", "help": "needs help. The family has been told"}[meaning["status"]]
    batt = " Low battery noted." if meaning.get("battery_low") else ""
    return f"Got it: {p['name']} {said}.{batt}" if ch["reply"] else None


def meshinfo_trace(store, heard, sender, me, snr, hops):
    from . import meshinfo
    names = path_names(store, heard["path"]) if heard else []
    rssi = heard.get("rssi") if heard else None
    return meshinfo.trace(names, sender, me, snr=heard.get("snr", snr) if heard else snr, rssi=rssi,
                          hops=len(heard["path"]) if heard else hops)


# ---------- going out ----------
def _quiet(at=None):
    t = (at or datetime.now()).strftime("%H:%M")
    start, end = QUIET
    return t >= start or t < end


def alert_text(event, data):
    """(level, text) for an alert-type event: level is "flash" or "all", or None when it isn't one for the mesh."""
    if event == "situation" and data.get("tier") in ("flash", "priority"):
        where = (f" at {data['site']}" if data.get("km") == 0 else f" {round(data['km'] * 0.621)}mi from {data['site']}") \
            if data.get("site") and data.get("km") is not None else ""
        tag = "FLASH" if data["tier"] == "flash" else "ALERT"
        return ("flash" if data["tier"] == "flash" else "all"), meshcore.fit(f"BUNKR {tag}: {data.get('title', '')}{where}", 120)
    if event == "flight" and data.get("title"):
        body = f" {data['body']}" if data.get("body") else ""
        return "all", meshcore.fit(f"BUNKR FLIGHT: {data['title']}.{body}", 120)
    if event == "news" and data.get("user_ids") is None and data.get("title"):
        return "all", meshcore.fit(f"BUNKR NEWS: {data['title']}", 120)
    return None


def outgoing(event, data, s):
    """What to put on a channel for a BUNKR event, or None. s: that channel's settings."""
    if event == "call" and s["send_calls"]:
        what = f"{data.get('label', '')} {data.get('place') or ''}".strip()
        drill = "DRILL: " if data.get("drill") else ""
        msg = f" {data['message']}" if data.get("message") else ""
        return meshcore.fit(f"{drill}BUNKR CALL: {what}.{msg} Reply SAFE, OMW or HELP", 120)
    if event == "all_clear" and s["send_calls"]:
        return "DRILL over." if data.get("drill") else "BUNKR: all clear."
    if event == "checkin" and s["send_help"] and data.get("status") == "help":
        if data.get("via") == "mesh" and getattr(_heard, "channel", None) in (None, s.get("index")):
            return None  # heard on this channel (or on another BUNKR's radio): don't echo it
        bits = " ".join(x for x in (data.get("place"), data.get("note")) if x)
        return meshcore.fit(f"{data['person_name']} NEEDS HELP. {bits}".strip(), 120)
    return None


# ---------- the link: one background thread per radio ----------
class Link:
    """Keeps a connection to the radio: sets up the family channel, collects messages, sends what's queued."""

    def __init__(self, store, connect=meshcore.Connection, poll_every=15, ctx=None):
        self._store, self.connect, self.poll_every, self.ctx = store, connect, poll_every, ctx
        self.outbox = []
        self.lock = threading.Lock()
        self.rx = collections.deque(maxlen=40)        # (monotonic, packet) for group messages the radio heard
        self.alerts_sent = {}                         # channel index -> [(monotonic, tag)]
        self.status = {"state": "off", "error": "", "radio": {}, "since": "", "heard": ""}
        self.stop_flag = threading.Event()
        self.thread = None

    @property
    def store(self):
        return self._store() if callable(self._store) else self._store

    def __call__(self, event, data):
        """Listener for check-ins and calls, and for alerts (situation, flights, early news)."""
        s = settings(self.store)
        if not s["enabled"]:
            return
        alert = alert_text(event, data) if event in ("situation", "flight", "news") else None
        for ch in s["channels"]:
            if alert:
                self.send_alert(ch, event, data, *alert)
                continue
            text = outgoing(event, data, ch)
            if text:
                self.queue(ch["index"], text)

    def send_alert(self, ch, event, data, level, text, at=None, mono=None):
        if ch.get("alerts", "off") == "off" or (ch["alerts"] == "flash" and level != "flash"):
            return False
        if level != "flash" and _quiet(at):
            return False
        mono = mono if mono is not None else time.monotonic()
        tag = data.get("uid") or data.get("tag") or text
        sent = [(t, g) for t, g in self.alerts_sent.get(ch["index"], []) if mono - t < 86400]
        if any(g == (event, tag) for _, g in sent) or sum(1 for t, _ in sent if mono - t < 3600) >= ALERTS_PER_HOUR:
            self.alerts_sent[ch["index"]] = sent
            return False
        self.alerts_sent[ch["index"]] = sent + [(mono, (event, tag))]
        self.queue(ch["index"], text)
        return True

    def queue_reply(self, index, reply):
        """A reply from handle(): a line, lines, or a Later run on its own thread."""
        if isinstance(reply, meshbot.Later):
            def run():
                try:
                    got = reply.run()
                except Exception as exc:
                    got = f"Couldn't get that ({type(exc).__name__})."
                for line in ([got] if isinstance(got, str) else list(got or [])):
                    self.queue(index, meshcore.fit(line, 120))
            threading.Thread(target=run, name="mesh-answer", daemon=True).start()
            return
        for line in ([reply] if isinstance(reply, str) else reply):
            self.queue(index, line)

    def queue(self, index, text):
        with self.lock:
            self.outbox.append((index, text))
            del self.outbox[:-20]

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self.run, name="mesh", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()

    def run(self):
        backoff = 5
        while not self.stop_flag.is_set():
            try:
                s = settings(self.store)
            except Exception:  # a locked node: no database yet
                self.stop_flag.wait(10)
                continue
            if not s["enabled"] or not s["connect"]:
                self.status.update(state="off", error="")
                self.stop_flag.wait(10)
                continue
            conn = None
            try:
                self.status.update(state="connecting", error="")
                conn = self.connect(s["connect"])
                radio = meshcore.Radio(conn)
                info = radio.start()
                for ch in s["channels"]:
                    radio.ensure_channel(ch["index"], ch["name"], bytes.fromhex(ch["secret"]))
                self.status.update(state="connected", radio=info, since=now(), error="")
                backoff = 5
                self.session(radio, s)
            except Exception as exc:  # radio unplugged, Wi-Fi down, wrong address...
                self.status.update(state="error", error=f"{type(exc).__name__}: {exc}"[:200])
                self.stop_flag.wait(backoff)
                backoff = min(backoff * 2, 300)
            finally:
                if conn:
                    conn.close()

    def heard_packet(self, s, m, now_=None):
        """The raw packet the radio heard for this channel message: same channel and number of hops, in the last
        30 seconds, newest first. None when the log didn't catch it."""
        ch = channel(s, m.get("channel"))
        if not ch:
            return None
        try:
            want = meshcore.channel_hash(bytes.fromhex(ch["secret"]))
        except ValueError:
            return None
        hops = meshcore.hops(m.get("path_len"))
        now_ = now_ if now_ is not None else time.monotonic()
        for t, pk in reversed(self.rx):
            if now_ - t > 30:
                break
            if pk["payload"][0] == want and (hops is None or len(pk["path"]) == hops):
                return pk
        return None

    def refresh_nodes(self, radio):
        """Read the radio's contact list (only what changed since last time) into BUNKR's nodes."""
        try:
            found, self.since = radio.contacts(self.since)
            save_nodes(self.store, found)
            self.status["nodes_at"] = now()
        except meshcore.RadioError as exc:  # older firmware, or the list didn't finish: try again later
            self.status["nodes_error"] = str(exc)[:120]
        self.last_nodes = time.monotonic()
        self.nodes_due = False

    def session(self, radio, s):
        last_poll = 0.0
        self.since, self.nodes_due = 0, True
        self.last_nodes = 0.0
        while not self.stop_flag.is_set():
            now_s = settings(self.store)
            if now_s != s:  # settings changed: reconnect and set the channel up again
                return
            pushed = radio.conn.poll(1.0)
            for p in pushed:
                if p["type"] == meshcore.LOG_RX and p.get("raw"):
                    pk = meshcore.packet(p["raw"])
                    if pk and pk["type"] == meshcore.GRP_TXT and pk["payload"]:
                        self.rx.append((time.monotonic(), {**pk, "snr": p.get("snr"), "rssi": p.get("rssi")}))
            new = [p for p in pushed if p["type"] == meshcore.PUSH_NEW_ADVERT and not p.get("bad")]
            if new:
                save_nodes(self.store, new)
            if any(p["type"] in (meshcore.PUSH_ADVERT, meshcore.PUSH_PATH_UPDATED) for p in pushed):
                self.nodes_due = True
            if (self.nodes_due and time.monotonic() - self.last_nodes > 30) or time.monotonic() - self.last_nodes > 900:
                self.refresh_nodes(radio)
            waiting = any(p["type"] == meshcore.MESSAGES_WAITING for p in pushed)
            msgs = [p for p in pushed if p.get("text") is not None]
            if waiting or time.monotonic() - last_poll > self.poll_every:
                msgs += radio.messages()
                last_poll = time.monotonic()
            for m in msgs:
                self.status["heard"] = now()
                if m.get("kind") == "channel":
                    m["heard"] = self.heard_packet(s, m)
                try:
                    ctx = self.ctx() if self.ctx else None
                    if ctx is not None:
                        ctx["me"] = (self.status.get("radio") or {}).get("name") or "BUNKR"
                    reply = handle(self.store, m, s, ctx)
                except Exception as exc:
                    reply = None
                    log(self.store, "in", m.get("text", ""), "", f"couldn't use it: {type(exc).__name__}")
                if reply:
                    self.queue_reply(m.get("channel"), reply)
            with self.lock:
                out, self.outbox = self.outbox, []
            names = {c["index"]: c["name"] for c in s["channels"]}
            for i, (index, text) in enumerate(out):
                if index not in names:
                    continue  # that channel was removed
                try:
                    if i:
                        self.stop_flag.wait(SEND_GAP)
                    radio.send_text(index, text)
                    log(self.store, "out", text, "", "sent", channel=names[index])
                except Exception:
                    with self.lock:
                        self.outbox = out[i:] + self.outbox
                    raise


def view(store, link, admin=True):
    """The mesh card. Family members see how to join the channels shown to them, and nothing else."""
    s = settings(store)
    chans = [{**c, "link": channel_link(c)} for c in s["channels"] if admin or c["shared"]]
    if not admin:
        return {"channels": [{k: c[k] for k in ("name", "link", "kind", "bot")} for c in chans], "bot_off": s["bot_off"],
                "status": {"state": (dict(link.status) if link else {}).get("state", "off")}, "words": {k: v[:4] for k, v in WORDS.items()}}
    return {"settings": {k: v for k, v in s.items() if k != "channels"}, "channels": chans, "kinds": KINDS, "bot_levels": BOT_LEVELS,
            "alert_levels": ALERT_LEVELS, "brief_people": store._rows("SELECT id, username FROM users ORDER BY username"), "switchable": meshbot.SWITCHABLE, "bot_off": s["bot_off"],
            "defaults": CHANNEL, "status": dict(link.status) if link else {"state": "off"}, "nodes": nodes(store),
            "log": recent(store), "words": {k: v[:4] for k, v in WORDS.items()}}
