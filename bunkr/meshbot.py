"""The BUNKR bot on the family's MeshCore channel: ask BUNKR things and tell it things from a radio, no internet needed.

Ideas from meshcore-bot (github.com/agessaman/meshcore-bot, MIT): keyword commands with aliases, per-person and
overall rate limits so a bot doesn't eat the channel's airtime, and a short command list on request. The code here is
BUNKR's own and runs inside BUNKR's radio link (bunkr/mesh.py): one companion radio can only be driven by one program.

Commands, any case. A single word on its own works as is (WHO, READY, ALERTS, FLIGHT, NEWS, PING, ?); anything with
more to it starts with "!" (or "/"), so ordinary chat ("got it", "home in 10", "have you eaten?") never sets the bot off.
HELP is never a command: it always means "I need help".
  ?  / !commands                what the bot understands
  WHO  / !status                everyone's last check-in
  !have water                   what we have and where (firearms never)
  !add 2 cases water @ garage   add to the inventory (adults)
  !used 1 water                 take some away (adults)
  READY                         how ready we are: days of water and food, the biggest gap
  ALERTS                        what's happening near our places
  FLIGHT                        flights the family is following
  NEWS                          the morning brief in a line
  !home / !home lights / !home status   Home Assistant (bunkr/homeassistant.py), only what an admin allowed
  PING                          is the bot there (and how well it hears you)
"""

import re
import time

from . import audit, checkins, gaps, quickadd

PER_PERSON = (8, 60)       # at most 8 answers a minute to one person
OVERALL = (20, 300)        # and 20 every five minutes in all
_recent = {}               # key -> [times]


def allowed(key, limit, now=None):
    n, window = limit
    now = now if now is not None else time.monotonic()
    times = [t for t in _recent.get(key, []) if now - t < window]
    if len(times) >= n:
        _recent[key] = times
        return False
    _recent[key] = times + [now]
    return True


COMMANDS = {  # word -> command; aliases included
    "?": "menu", "commands": "menu", "menu": "menu", "cmds": "menu",
    "who": "who", "status": "who", "board": "who",
    "have": "have", "inv": "have", "inventory": "have", "find": "have", "where": "have",
    "add": "add", "got": "add", "bought": "add",
    "used": "used", "use": "used", "took": "used",
    "ready": "ready", "readiness": "ready",
    "alerts": "alerts", "alert": "alerts", "situation": "alerts",
    "flight": "flight", "flights": "flight",
    "news": "news", "brief": "news",
    "home": "home", "ha": "home",
    "ping": "ping", "test": "ping",
}
MENU = "BUNKR: SAFE/OMW/HELP check in. WHO READY ALERTS FLIGHT NEWS PING, !have water, !add 2 cases water, !used 1 water, !home"
MENU_BASIC = "BUNKR: SAFE/OMW/HELP check in. WHO ALERTS FLIGHT NEWS PING"
BASIC = {"menu", "ping", "who", "alerts", "flight", "news"}  # what a trusted-group channel may ask
BARE = {"menu", "who", "ready", "alerts", "flight", "news", "ping"}  # may be sent as one word with no "!"


def parse(text):
    """(command, the rest) or None. "!" in front for anything but a bare one-word question."""
    t = (text or "").strip()
    m = re.match(r"^([!/]?)\s*(\?|[A-Za-z]+)(?=\s|$|[?!.,])[?!.,]*\s*(.*)$", t, re.S)
    if not m:
        return None
    cmd = COMMANDS.get(m.group(2).lower())
    if not cmd:
        return None
    rest = m.group(3).strip()
    if not m.group(1) and (rest or cmd not in BARE):
        return None
    return cmd, rest


def _ago(iso):
    from datetime import datetime, timezone
    try:
        s = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return ""
    return "now" if s < 90 else f"{round(s / 60)}m" if s < 5400 else f"{round(s / 3600)}h" if s < 172800 else f"{round(s / 86400)}d"


def who(store):
    b = checkins.board(store)
    short = {"safe": "safe", "moving": "OMW", "help": "HELP"}
    bits = [f"{p['name'].split()[0]} {short[p['last']['status']]} {_ago(p['last']['at'])}{' lowbatt' if p['last'].get('battery_low') else ''}"
            if p["last"] else f"{p['name'].split()[0]} ?"
            for p in sorted(b["people"], key=lambda p: (0 if p["last"] and p["last"]["status"] == "help" else 1, p["name"]))]
    call = b["call"]
    head = f"CALL {call['label']} {call['place'] or ''}".strip() + ". " if call else ""
    return head + ", ".join(bits) if bits else "No family on BUNKR yet."


def _qty(i):
    """Short enough for a radio: "3 jugs (15 gal)"."""
    q = i["quantity"] or 0
    text = f"{q:g} {i['unit'] or ''}".strip()
    if i.get("per_unit") and i.get("category") == "water":
        text += f" ({q * i['per_unit']:g} gal)"
    return text


def _items(store):
    items = gaps.visible(store.items(), "family")  # never firearms, whoever asks
    for i in items:
        i["qty_text"] = _qty(i)
    return items


def _match(items, words):
    ws = [w for w in re.findall(r"[a-z0-9]+", words.lower()) if len(w) > 1]
    if not ws:
        return []
    def hit(i):
        name = i["name"].lower()
        return all(w in name or (w.endswith("s") and w[:-1] in name) for w in ws)
    return [i for i in items if hit(i)]


def have(store, q):
    if not q:
        return "HAVE what? e.g. HAVE water"
    found = _match(_items(store), q)
    if not found:
        return f"Nothing called {q[:30]} in the inventory."
    parts = [f"{i['name']} {i['qty_text']}" + (f" @{i['location']}" if i.get("location") else "") for i in found[:4]]
    more = f" +{len(found) - 4} more" if len(found) > 4 else ""
    return "; ".join(parts) + more


def _location(store, name):
    if not name:
        return None
    n = name.strip().lower()
    locs = store.locations()
    exact = [l for l in locs if l["name"].lower() == n or (l.get("label") or "").lower() == n]
    part = [l for l in locs if n in l["name"].lower()]
    pick = exact or (part if len(part) == 1 else [])
    return pick[0] if pick else None


def add(store, person, rest):
    line, _, where = rest.partition("@")
    draft = quickadd.parse_line(line)
    if not draft:
        return "ADD what? e.g. ADD 2 cases water @ garage"
    loc = _location(store, where)
    if where.strip() and not loc:
        return f"No place called {where.strip()[:30]}. Try without @, or a place from Locations."
    same = [i for i in _items(store) if i["name"].lower() == draft["name"].lower()
            and (loc is None or i["location_id"] == loc["id"])]
    who_ = {"id": None, "username": f"mesh:{person['name']}"[:60]}
    if len(same) == 1:
        i = same[0]
        store.save_item({"quantity": round((i["quantity"] or 0) + draft["quantity"], 3)}, i["id"])
        audit.record(store, who_, "change", "items", f"mesh ADD {draft['quantity']:g} to {i['name']}", "mesh")
        return f"Now {round((i['quantity'] or 0) + draft['quantity'], 3):g} {i['unit'] or ''} {i['name']}".replace("  ", " ")
    data = {"name": draft["name"], "category": draft["category"], "quantity": draft["quantity"], "unit": draft["unit"],
            "per_unit": draft["per_unit"], "size_unit": draft["size_unit"], "location_id": loc["id"] if loc else None}
    store.save_item(data)
    audit.record(store, who_, "change", "items", f"mesh ADD new {draft['name']}", "mesh")
    return f"Added {draft['quantity']:g} {draft['unit']} {draft['name']} ({draft['category']})".replace("  ", " ") + (f" @{loc['name']}" if loc else "")


def used(store, person, rest):
    m = re.match(r"^(\d+(?:\.\d+)?)\s+(.*)$", rest)
    qty, name = (float(m.group(1)), m.group(2)) if m else (1.0, rest)
    found = _match(_items(store), name)
    if not found:
        return f"Nothing called {name[:30]} in the inventory."
    if len(found) > 1:
        exact = [i for i in found if i["name"].lower() == name.strip().lower()]
        if len(exact) != 1:
            return "Which one? " + "; ".join(f"{i['name']}" + (f" @{i['location']}" if i.get("location") else "") for i in found[:4])
        found = exact
    i = found[0]
    left = max(0.0, round((i["quantity"] or 0) - qty, 3))
    store.save_item({"quantity": left}, i["id"])
    audit.record(store, {"id": None, "username": f"mesh:{person['name']}"[:60]}, "change", "items", f"mesh USED {qty:g} of {i['name']}", "mesh")
    return f"{i['name']}: {left:g} left" + (" (add it to Shopping?)" if left == 0 else "")


def answer(store, person, text, ctx):
    """The reply to one command from a family member on the channel, or None if it isn't one."""
    p = parse(text)
    if not p:
        return None
    cmd, rest = p
    basic = ctx.get("level") == "basic"
    if basic and cmd not in BASIC:
        return None  # inventory and Home Assistant stay on the family channel
    key = person["uid"] if person else "unknown"
    if not allowed(("p", key), PER_PERSON) or not allowed(("all",), OVERALL):
        return None  # quietly: answering "slow down" costs airtime too
    adult = bool(person and person.get("role") == "adult")
    if cmd == "menu":
        return MENU_BASIC if basic else MENU
    if cmd == "ping":
        return "pong" + (f" (SNR {ctx['snr']:g}, {ctx['hops']} hops)" if ctx.get("snr") is not None else "")
    if not person:
        return None  # strangers on the channel get nothing about the family
    if cmd == "who":
        return who(store)
    if cmd == "have":
        return have(store, rest)
    if cmd in ("add", "used"):
        if not adult:
            return "Only an adult can change the inventory from the mesh."
        return add(store, person, rest) if cmd == "add" else used(store, person, rest)
    if cmd in ("ready", "alerts", "flight", "news", "home"):
        fn = ctx.get(cmd)
        return fn(rest, adult) if fn else "Not available on this BUNKR."
    return None
