"""The BUNKR bot on the family's MeshCore channel: ask BUNKR things and tell it things from a radio, no internet needed.

Ideas from meshcore-bot (github.com/agessaman/meshcore-bot, MIT): keyword commands with aliases, per-person and
overall rate limits so a bot doesn't eat the channel's airtime, and a short command list on request. The code here is
BUNKR's own and runs inside BUNKR's radio link (bunkr/mesh.py): one companion radio can only be driven by one program.

Commands, any case. A single word on its own works as is (WHO, READY, ALERTS, WX, SUN, NEWS, PING, TRACE, ?);
anything with more to it starts with "!" (or "/"), so ordinary chat ("got it", "home in 10", "have you eaten?") never
sets the bot off. HELP is never a command: it always means "I need help". Admins can switch any command off.
  ?  / ? 2 / !commands          every command, a page at a time
  WHO  / !status                everyone's last check-in
  !where                        where everyone last checked in from
  !have water                   what we have and where (firearms never)
  !add 2 cases water @ garage   add to the inventory (adults)
  !used 1 water                 take some away (adults)
  READY                         how ready we are: days of water and food, the biggest gap
  ALERTS / !alerts 80202        what's happening near our places / weather alerts, fires and quakes near a place
  WX / !wx 80202 / !wx Denver CO 5   weather now, today and tonight; or 3, 5, 7 (or 10: NWS gives 7) days
  SUN / !sun 80202              sunrise, sunset, daylight and the moon (worked out offline)
  QUAKE / !quake 80202          earthquakes nearby in the last day
  SPACE                         space weather: radio blackouts, geomagnetic storms, Kp
  FLIGHT / !flight UA123        flights the family is following / any flight now
  OVERHEAD / !overhead 80202    aircraft near you (your node's shared position) or a place
  NEWS / !news 2 / !news more   numbered headlines / one story in 2-3 messages (or just "2" right after)
  !ask how long to boil water   a very short answer from the local AI, marked AI (only when AI is on)
  !home / !home lights / !home status   Home Assistant (bunkr/homeassistant.py), only what an admin allowed
  PING / TRACE                  is the bot there / the route your message took, by repeater name
"""

import re
import threading
import time

from . import audit, checkins, gaps, quickadd


class Later:
    """An answer that needs the internet or the AI: bunkr/mesh.py runs fn() off the radio's thread and sends what it
    returns (a line or a list of lines)."""

    def __init__(self, fn, label=""):
        self.fn, self.label = fn, label

    def run(self):
        return self.fn()

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
    "where": "where", "whereis": "where",
    "have": "have", "inv": "have", "inventory": "have", "find": "have",
    "add": "add", "got": "add", "bought": "add",
    "used": "used", "use": "used", "took": "used",
    "ready": "ready", "readiness": "ready",
    "alerts": "alerts", "alert": "alerts", "situation": "alerts",
    "wx": "wx", "weather": "wx", "forecast": "wx",
    "sun": "sun", "sunrise": "sun", "sunset": "sun", "moon": "sun",
    "quake": "quake", "quakes": "quake", "earthquake": "quake", "eq": "quake",
    "space": "space", "spacewx": "space", "solar": "space",
    "flight": "flight", "flights": "flight",
    "overhead": "overhead", "planes": "overhead", "aircraft": "overhead",
    "news": "news", "brief": "news", "more": "more",
    "ask": "ask", "ai": "ask",
    "home": "home", "ha": "home",
    "ping": "ping", "test": "ping",
    "trace": "trace", "path": "trace", "route": "trace",
}
# the full list for "?", in pages; (command, what to show)
HELP_LINES = [("who", "WHO"), ("where", "!where"), ("ready", "READY"), ("alerts", "ALERTS [zip]"), ("wx", "WX [zip] [3/5/7]"),
              ("sun", "SUN [zip]"), ("quake", "QUAKE [zip]"), ("space", "SPACE"), ("flight", "FLIGHT [UA123]"),
              ("overhead", "OVERHEAD [zip]"), ("news", "NEWS, !news 2, MORE"), ("ask", "!ask question (AI)"),
              ("have", "!have water"), ("add", "!add 2 cases water @garage"), ("used", "!used 1 water"), ("home", "!home"),
              ("ping", "PING"), ("trace", "TRACE")]
BASIC = {"menu", "ping", "trace", "who", "alerts", "wx", "sun", "quake", "space", "flight", "overhead", "news", "more"}  # a trusted group
BARE = {"menu", "who", "where", "ready", "alerts", "wx", "sun", "quake", "space", "flight", "overhead", "news", "more", "ping", "trace"}
SWITCHABLE = ["where", "ready", "alerts", "wx", "sun", "quake", "space", "flight", "overhead", "news", "ask", "have", "add", "used",
              "home", "trace"]   # admins can turn these off (WHO, PING and the menu always answer)
NET = {"wx", "quake", "space", "overhead", "ask"}   # always need the internet (or the AI)


def menu(page, allowed_cmds, basic):
    """'BUNKR 1/2: SAFE/OMW/HELP check in. WHO, !where, ...'"""
    lines = [label for cmd, label in HELP_LINES if cmd in allowed_cmds and (not basic or cmd in BASIC)]
    pages, cur = [], ""
    for l in lines:  # page 1 also carries "SAFE/OMW/HELP check in."; each has "BUNKR n/m: " and "(? n next)"
        if cur and len(cur) + 2 + len(l) > (70 if not pages else 94):
            pages.append(cur)
            cur = l
        else:
            cur = f"{cur}, {l}" if cur else l
    if cur:
        pages.append(cur)
    pages = pages or [""]
    page = min(max(page, 1), len(pages))
    head = f"BUNKR {page}/{len(pages)}: " + ("SAFE/OMW/HELP check in. " if page == 1 else "")
    tail = f" (? {page + 1} next)" if page < len(pages) else ""
    return head + pages[page - 1] + tail


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
    if not m.group(1) and cmd == "menu" and rest.isdigit():
        return cmd, rest  # "? 2": the next page
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


def where(store):
    b = checkins.board(store)
    bits = []
    for p in sorted(b["people"], key=lambda p: p["name"]):
        last = p["last"]
        if not last:
            continue
        place = last.get("place") or (p.get("site") or "")
        bits.append(f"{p['name'].split()[0]} {place[:24] or '?'} {_ago(last['at'])}".strip())
    return "; ".join(bits) if bits else "No check-ins yet."


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
    """The reply to one command from a family member on the channel: a line, a list of lines, a Later (slow: the
    radio thread runs it in the background), or None if it isn't one."""
    from . import meshinfo
    key = person["uid"] if person else "unknown"
    p = parse(text)
    if not p and person and ctx.get("news_rec") is not None:
        n = meshinfo.news_number(key, text)  # a bare "2" right after NEWS
        if n is not None:
            p = ("news", str(n))
    if not p:
        return None
    cmd, rest = p
    if cmd == "more":
        cmd, rest = "news", "more"
    basic = ctx.get("level") == "basic"
    if basic and cmd not in BASIC:
        return None  # inventory and Home Assistant stay on the family channel
    off = set(ctx.get("off") or ())
    if cmd in off:
        return None
    if not allowed(("p", key), PER_PERSON) or not allowed(("all",), OVERALL):
        return None  # quietly: answering "slow down" costs airtime too
    adult = bool(person and person.get("role") == "adult")
    if cmd == "menu":
        page = int(rest) if rest.isdigit() else 1
        return menu(page, {c for c, _ in HELP_LINES if c not in off and (c != "ask" or ctx.get("ai"))}, basic)
    if cmd == "ping":
        return "pong" + (f" (SNR {ctx['snr']:g}, {ctx['hops']} hops)" if ctx.get("snr") is not None and ctx.get("hops") is not None
                         else f" (SNR {ctx['snr']:g})" if ctx.get("snr") is not None else "")
    if cmd == "trace":
        t = ctx.get("trace")
        return t() if t else "Trace isn't available on this BUNKR."
    if not person:
        return None  # strangers on the channel get nothing about the family
    if cmd == "who":
        return who(store)
    if cmd == "where":
        if rest:
            return have(store, rest)  # "!where water": the older meaning
        return where(store)
    if cmd == "have":
        return have(store, rest)
    if cmd in ("add", "used"):
        if not adult:
            return "Only an adult can change the inventory from the mesh."
        return add(store, person, rest) if cmd == "add" else used(store, person, rest)
    if cmd == "news":
        fn = ctx.get("news")
        return fn(rest, key) if fn else "Not available on this BUNKR."
    if cmd == "ask" and not ctx.get("ai"):
        return "No AI is set up on this BUNKR."
    if cmd == "alerts" and not rest:
        cmd = "alerts_here"
    if cmd == "flight" and not rest:
        cmd = "flight_board"
    fn = ctx.get(cmd)
    if not fn:
        return "Not available on this BUNKR."
    if cmd == "overhead":
        pos = ctx.get("sender_pos")
        return Later(lambda: fn(rest, adult, pos), cmd)
    if cmd in NET or cmd in ("alerts", "flight"):
        return Later(lambda: fn(rest, adult), cmd)
    return fn(rest, adult)
