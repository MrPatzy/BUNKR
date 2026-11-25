"""Flights: someone checks in at an airport (or any time) with a flight number, and the family can follow it.

- Airports come from a built-in list (airports.tsv, OurAirports, public domain), so "you're at DCA" works offline.
- Live position, altitude and speed: adsb.lol, a free, open, community feed of aircraft positions (no account).
  Planes broadcast an ICAO callsign (UAL1234), not the ticket number (UA1234), so ticket codes are translated with
  AIRLINES below; FlightAware tells us the real callsign when it's set up (regional flights fly as SkyWest etc.).
- The flight's own times, delays, gates, cancellations: FlightAware AeroAPI, only when an admin has saved a key
  (flightaware.key next to the database). It's paid per lookup, so BUNKR looks a flight up when it's added and then
  every 30 minutes (every 10 around departure and arrival) until it lands, and never more than MONTHLY_CAP times a
  month in all.
- Airport-wide delays and ground stops: the FAA's airport status service (free, US airports).
- The hub checks active flights every two minutes. Landing is noticed from FlightAware's times or from the plane on
  the ground (or gone quiet low down) near the destination. The family gets one push when a flight is 30+ minutes
  late (and again if it slips another 30), when it lands, and if it's cancelled or diverted. A flight clears itself
  a few hours after landing, or when the traveller ends it.
"""

import json
import math
import re
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

USER_AGENT = "Mozilla/5.0 (compatible; BUNKR flight follow)"
ADSB = "https://api.adsb.lol/v2/"
AEROAPI = "https://aeroapi.flightaware.com/aeroapi/flights/"
FAA = "https://external-api.faa.gov/asws/api/airport/status/"
KEY_FILE = "flightaware.key"
MONTHLY_CAP = 300          # FlightAware lookups a month, all flights together
CHECK_EVERY = 120          # seconds between checks of active flights (adsb.lol is free)
DELAY_STEP = 30            # minutes: push when the arrival is this late, and again each time it slips this much more
listeners = []

# Ticket code -> (ICAO callsign prefix, name). Others can be typed as the callsign itself (e.g. "SKW5123").
AIRLINES = {
    "UA": ("UAL", "United"), "AA": ("AAL", "American"), "DL": ("DAL", "Delta"), "WN": ("SWA", "Southwest"),
    "B6": ("JBU", "JetBlue"), "AS": ("ASA", "Alaska"), "F9": ("FFT", "Frontier"), "NK": ("NKS", "Spirit"),
    "G4": ("AAY", "Allegiant"), "HA": ("HAL", "Hawaiian"), "SY": ("SCX", "Sun Country"), "MX": ("MXY", "Breeze"),
    "QX": ("QXE", "Horizon"), "OO": ("SKW", "SkyWest"), "YX": ("RPA", "Republic"), "9E": ("EDV", "Endeavor"),
    "MQ": ("ENY", "Envoy"), "OH": ("JIA", "PSA"), "YV": ("ASH", "Mesa"), "G7": ("GJS", "GoJet"), "PT": ("PDT", "Piedmont"),
    "ZW": ("AWI", "Air Wisconsin"), "C5": ("UCA", "CommutAir"), "AC": ("ACA", "Air Canada"), "WS": ("WJA", "WestJet"),
    "AM": ("AMX", "Aeromexico"), "Y4": ("VOI", "Volaris"), "VB": ("VIV", "Viva Aerobus"), "BA": ("BAW", "British Airways"),
    "VS": ("VIR", "Virgin Atlantic"), "AF": ("AFR", "Air France"), "KL": ("KLM", "KLM"), "LH": ("DLH", "Lufthansa"),
    "LX": ("SWR", "Swiss"), "OS": ("AUA", "Austrian"), "SN": ("BEL", "Brussels Airlines"), "IB": ("IBE", "Iberia"),
    "EI": ("EIN", "Aer Lingus"), "AY": ("FIN", "Finnair"), "SK": ("SAS", "SAS"), "TP": ("TAP", "TAP"), "AZ": ("ITY", "ITA"),
    "LO": ("LOT", "LOT"), "TK": ("THY", "Turkish"), "EK": ("UAE", "Emirates"), "QR": ("QTR", "Qatar Airways"),
    "EY": ("ETD", "Etihad"), "LY": ("ELY", "El Al"), "SV": ("SVA", "Saudia"), "JL": ("JAL", "Japan Airlines"),
    "NH": ("ANA", "ANA"), "KE": ("KAL", "Korean Air"), "OZ": ("AAR", "Asiana"), "CX": ("CPA", "Cathay Pacific"),
    "SQ": ("SIA", "Singapore Airlines"), "QF": ("QFA", "Qantas"), "NZ": ("ANZ", "Air New Zealand"), "CA": ("CCA", "Air China"),
    "MU": ("CES", "China Eastern"), "CZ": ("CSN", "China Southern"), "BR": ("EVA", "EVA Air"), "CI": ("CAL", "China Airlines"),
    "AI": ("AIC", "Air India"), "PR": ("PAL", "Philippine Airlines"), "LA": ("LAN", "LATAM"), "AV": ("AVA", "Avianca"),
    "CM": ("CMP", "Copa"), "FR": ("RYR", "Ryanair"), "U2": ("EZY", "easyJet"), "W6": ("WZZ", "Wizz Air"),
    "DY": ("NOZ", "Norwegian"), "ET": ("ETH", "Ethiopian"), "KQ": ("KQA", "Kenya Airways"), "SA": ("SAA", "South African"),
}
BY_ICAO = {v[0]: (k, v[1]) for k, v in AIRLINES.items()}


def now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ---------- airports ----------
_AIRPORTS = None


def airports():
    global _AIRPORTS
    if _AIRPORTS is None:
        _AIRPORTS = {}
        for line in (Path(__file__).parent / "airports.tsv").read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            iata, icao, name, city, country, lat, lon, size = line.split("\t")
            _AIRPORTS[iata] = {"iata": iata, "icao": icao, "name": name, "city": city, "country": country,
                               "lat": float(lat), "lon": float(lon), "large": size == "L"}
    return _AIRPORTS


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


def airport_near(lat, lon):
    """The airport someone is at, if any: within 4 km of a big one or 2.5 km of a smaller one."""
    if lat is None or lon is None:
        return None
    best = None
    for a in airports().values():
        if abs(a["lat"] - lat) > 0.06 or abs(a["lon"] - lon) > 0.08:
            continue
        d = km(lat, lon, a["lat"], a["lon"])
        if d <= (4 if a["large"] else 2.5) and (best is None or d < best[0]):
            best = (d, a)
    return best[1] if best else None


def airport(code):
    return airports().get((code or "").upper())


def home_airport(lat, lon, max_km=150):
    """The big airport nearest home: where a flight is assumed to be going when nobody said."""
    if lat is None or lon is None:
        return None
    best = min(((km(lat, lon, a["lat"], a["lon"]), a) for a in airports().values() if a["large"]), key=lambda x: x[0], default=None)
    return best[1] if best and best[0] <= max_km else None


# ---------- flight numbers ----------
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


# Other ways people say an airline: normalized name -> ticket code. The names in AIRLINES count too.
NAMES = {_norm(v[1]): k for k, v in AIRLINES.items()}
NAMES.update({_norm(n): c for n, c in {
    "southwest airlines": "WN", "united airlines": "UA", "american airlines": "AA", "delta air lines": "DL",
    "delta airlines": "DL", "alaska airlines": "AS", "frontier airlines": "F9", "spirit airlines": "NK",
    "allegiant air": "G4", "hawaiian airlines": "HA", "sun country airlines": "SY", "breeze airways": "MX",
    "jet blue": "B6", "jetblue airways": "B6", "skywest airlines": "OO", "horizon air": "QX", "air canada": "AC",
    "west jet": "WS", "virgin": "VS", "air france": "AF", "lufthansa german airlines": "LH", "qatar": "QR"}.items()})
# Codes people often type for an airline whose real code is different: typed -> the airlines they probably meant.
WRONG_CODES = {"SW": ["WN"], "SO": ["WN"], "JB": ["B6"], "FT": ["F9"], "FF": ["F9"], "FS": ["F9"], "DA": ["DL"],
               "AK": ["AS"], "SP": ["NK"], "UN": ["UA"], "AL": ["AS", "AA"]}
# Real codes that are often typed for another airline: always ask.
ASK_ALWAYS = {"FR": ["FR", "F9"]}


class Unsure(ValueError):
    """A flight entry BUNKR won't track until the person picks: it guessed, changed something, or found no match.
    .options are parse results to pick from (maybe none); str() is the plain question to show."""

    def __init__(self, message, options=()):
        super().__init__(message)
        self.options = list(options)


def _option(iata, num, dest=""):
    icao, name = AIRLINES[iata]
    return {"ident": iata + num, "callsign": icao + num, "airline": name, **({"dest": dest} if dest else {})}


def describe(opt):
    """'Southwest (WN) 2496'"""
    return f"{opt['airline']} ({opt['ident'][:2]}) {opt['ident'][2:]}"


def _unsure(typed, iatas, num, dest, found=False):
    opts = [_option(i, num, dest) for i in iatas]
    typed = typed.strip()[:30]
    if opts and not found:
        return Unsure("Did you mean " + " or ".join(describe(o) for o in opts) + "?", opts)
    if opts:
        return Unsure(f"Couldn't find {typed}. Did you mean " + " or ".join(describe(o) for o in opts) + "?", opts)
    return Unsure(f"Couldn't find {typed}. Type it like United 1234 or UA 1234.")


def parse_number(text):
    """'UA 1234', 'ua1234', 'UAL1234' or 'United 1234' -> {"ident": "UA1234", "callsign": "UAL1234", "airline": "United"}.
    'UA 1234 to LAX' also gives "dest": "LAX". Raises Unsure (a ValueError) when the entry isn't clear: a wrong or
    mixed-up code ('SW 2496', 'FR 1234'), a half-typed name, or an airline we don't know."""
    raw = (text or "").strip()
    text, _, to = raw.upper().partition(" TO ")
    dest = to.strip() if airport(to.strip()) else ""
    if to.strip() and not dest:
        raise ValueError(f"no airport called {to.strip()[:10]}: use its 3-letter code, like DEN")
    typed = text.strip()
    s = re.sub(r"[\s-]+", "", text)
    extra = {"dest": dest} if dest else {}
    m = re.fullmatch(r"([A-Z]{3})(\d{1,4}[A-Z]?)", s)
    if m:  # a callsign: a known one fills in the ticket code; an unknown one is taken as typed
        code, num = m.groups()
        iata, name = BY_ICAO.get(code, ("", ""))
        if not iata and _norm(code) in NAMES:
            return _option(NAMES[_norm(code)], num, dest)
        return {"ident": (iata + num) if iata else code + num, "callsign": code + num, "airline": name, **extra}
    m = re.fullmatch(r"([A-Z][A-Z0-9]|[0-9][A-Z])(\d{1,4}[A-Z]?)", s)
    if m:
        code, num = m.groups()
        if code in ASK_ALWAYS:
            raise _unsure(typed, ASK_ALWAYS[code], num, dest)
        if code in AIRLINES:
            return _option(code, num, dest)
        if code in WRONG_CODES:
            raise _unsure(typed, WRONG_CODES[code], num, dest)
        near = [k for k, v in AIRLINES.items() if _norm(v[1]).startswith(code.lower())][:3]
        raise _unsure(typed, near, num, dest, found=True)
    m = re.fullmatch(r"(.*?[A-Z].*?)[\s-]*(\d{1,4}[A-Z]?)", text.strip())
    if not m:
        raise ValueError("type the flight like United 1234 or UA 1234")
    word, num = _norm(m.group(1)), m.group(2)
    if word in NAMES:
        return _option(NAMES[word], num, dest)
    near = list(dict.fromkeys(c for n, c in NAMES.items() if len(word) >= 2 and n.startswith(word)))[:3]
    raise _unsure(typed, near, num, dest, found=not near)


def ask(store, number, key=None, get=None, when=None):
    """None when this flight can be tracked as typed. Otherwise {"message", "options": [{"label", "number", "callsign"}]}
    for the person to pick from before anything is tracked. Looks the flight up (FlightAware, if set up) to say where it
    goes, and to catch FlightAware naming a different flight than the one typed."""
    get = get or _get_json
    when = when or now()
    try:
        n = parse_number(number)
        opts, message = [n], None
    except Unsure as u:
        opts, message = u.options, str(u)
        if not opts:
            return {"message": message, "options": []}
    out, changed = [], False
    for o in opts:
        seen = _lookup(store, o, key, get, when)
        callsign = o["callsign"]
        if seen and seen.get("atc_ident") and seen["atc_ident"] != callsign:
            callsign, changed = seen["atc_ident"], True
        label = describe(o)
        bits = []
        if seen:
            bits.append(" to ".join(_where(c) for c in (seen.get("origin"), seen.get("dest")) if c))
            t = _parse(seen.get("estimated_out") or seen.get("scheduled_out"))
            if t:
                try:
                    t = t.astimezone(ZoneInfo(seen["origin_tz"])) if seen.get("origin_tz") else t
                except Exception:
                    pass
                bits.append(t.strftime("%-I:%M %p"))
        out.append({"label": ", ".join([label] + [b for b in bits if b]), "number": o["ident"] + (f" to {o['dest']}" if o.get("dest") else ""),
                    "callsign": callsign})
    if message is None and not changed:
        return None
    if message is None:
        message = "FlightAware has this as a different flight. Did you mean " + out[0]["label"] + "?"
    return {"message": message, "options": out}


def _lookup(store, o, key, get, when):
    """FlightAware's view of one parsed flight, or {} (no key, over the cap, nothing found, or it failed)."""
    if not key or lookups_this_month(store, when) >= MONTHLY_CAP:
        return {}
    f = {"ident": o["ident"], "callsign": o["callsign"], "origin": "", "started_at": _iso(when)}
    try:
        _count_lookup(store, when)
        return flightaware(f, key, get, when)
    except Exception:
        return {}


# ---------- storage ----------
FIELDS = ("uid", "person_uid", "person_name", "ident", "callsign", "airline", "origin", "dest", "started_at", "started_by",
          "updated_at", "ended_at", "ended_why", "status", "info", "pos", "aero_at", "told")


def _row(r):
    out = dict(r)
    for k in ("info", "pos", "told"):
        try:
            out[k] = json.loads(r[k] or "{}")
        except ValueError:
            out[k] = {}
    return out


def active(store):
    return [_row(r) for r in store._rows("SELECT * FROM flights WHERE ended_at='' ORDER BY started_at")]


def get(store, uid):
    rows = store._rows("SELECT * FROM flights WHERE uid=?", (uid,))
    return _row(rows[0]) if rows else None


def add(store, person_uid, number, origin="", dest="", by="", home=None, callsign=""):
    """Start following someone's flight. Any flight they were already on is ended. With no destination given, it's
    taken to be home's airport (unless they're leaving from it), and marked as a guess until FlightAware confirms it.
    callsign is one the person picked from a "Did you mean" question; without it the callsign is only what was typed."""
    p = store._rows("SELECT uid, name FROM people WHERE uid=?", (person_uid or "",))
    if not p:
        raise ValueError("unknown person")
    n = parse_number(number)
    told = {}
    if callsign:
        callsign = callsign.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,8}", callsign):
            raise ValueError("that flight doesn't look right")
        n["callsign"] = callsign
        told["confirmed"] = True
    origin = origin.upper() if airport(origin) else ""
    dest = dest.upper() if airport(dest) else n.get("dest", "")
    if not dest and home and home["iata"] != origin:
        dest = home["iata"]
        told["dest_guess"] = True
    t = _iso(now())
    for old in store._rows("SELECT uid FROM flights WHERE person_uid=? AND ended_at=''", (p[0]["uid"],)):
        end(store, old["uid"], "a new flight")
    rec = {"uid": uuid.uuid4().hex, "person_uid": p[0]["uid"], "person_name": p[0]["name"], "ident": n["ident"],
           "callsign": n["callsign"], "airline": n["airline"], "origin": origin, "dest": dest, "started_at": t,
           "started_by": (by or "")[:80], "updated_at": t, "ended_at": "", "ended_why": "", "status": "added",
           "info": "{}", "pos": "{}", "aero_at": "", "told": json.dumps(told)}
    store._exec(f"INSERT INTO flights({', '.join(FIELDS)}) VALUES({', '.join('?' for _ in FIELDS)})", tuple(rec[f] for f in FIELDS))
    return get(store, rec["uid"])


def end(store, uid, why="ended"):
    t = _iso(now())
    store._exec("UPDATE flights SET ended_at=?, ended_why=?, updated_at=? WHERE uid=? AND ended_at=''", (t, why[:60], t, uid))


def _save(store, f):
    store._exec("UPDATE flights SET callsign=?, origin=?, dest=?, status=?, info=?, pos=?, aero_at=?, told=?, updated_at=? WHERE uid=?",
                (f["callsign"], f["origin"], f["dest"], f["status"], json.dumps(f["info"]), json.dumps(f["pos"]), f["aero_at"],
                 json.dumps(f["told"]), _iso(now()), f["uid"]))


# ---------- sources ----------
def _get_json(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(2_000_000))


def load_key(folder):
    try:
        return (Path(folder) / KEY_FILE).read_text().strip() or None
    except OSError:
        return None


def save_key(folder, value):
    """Write (or with an empty value, remove) the FlightAware key, readable only by BUNKR."""
    import os
    path = Path(folder) / KEY_FILE
    value = (value or "").strip()
    if not value:
        path.unlink(missing_ok=True)
        return False
    if not re.fullmatch(r"[A-Za-z0-9_\-.]{16,200}", value):
        raise ValueError("that doesn't look like a FlightAware key (letters and numbers)")
    path.touch(mode=0o600)
    os.chmod(path, 0o600)
    path.write_text(value + "\n")
    return True


def lookups_this_month(store, when=None):
    month = (when or now()).strftime("%Y-%m")
    raw = store.get_raw("aeroapi_usage", "") or ""
    m, _, n = raw.partition(":")
    return int(n) if m == month and n.isdigit() else 0


def _count_lookup(store, when=None):
    month = (when or now()).strftime("%Y-%m")
    store.set_raw("aeroapi_usage", f"{month}:{lookups_this_month(store, when) + 1}")


def adsb(f, get=_get_json, when=None):
    """Where the plane is now (adsb.lol), by callsign, else by tail number. {} if it isn't being heard."""
    info = f["info"]
    tries = [("callsign", f["callsign"])] if f["callsign"] else []  # never FlightAware's ident unless the person picked it
    if info.get("registration"):
        tries.append(("reg", info["registration"]))
    for kind, value in tries:
        data = get(ADSB + kind + "/" + urllib.parse.quote(value))
        ac = [a for a in (data.get("ac") or []) if a.get("lat") is not None and a.get("lon") is not None]
        if ac:
            a = min(ac, key=lambda a: a.get("seen_pos", a.get("seen", 99)))
            alt = a.get("alt_baro")
            seen = (when or now()) - timedelta(seconds=float(a.get("seen_pos", a.get("seen", 0)) or 0))
            return {"lat": round(a["lat"], 4), "lon": round(a["lon"], 4), "alt_ft": 0 if alt == "ground" else alt,
                    "ground": alt == "ground", "gs_kt": a.get("gs"), "track": a.get("track"), "seen_at": _iso(seen),
                    "by": kind}
    return {}


def own_position(f, pos, when):
    """Is this position the flight itself? A tail-number match before FlightAware reports departure is usually the
    plane still flying its inbound leg under another flight number, unless it's moving at the origin near departure."""
    if not pos:
        return False
    if pos.get("by") != "reg":
        return True
    info = f["info"]
    if info.get("actual_out") or info.get("actual_off"):
        return True
    o = airport(f["origin"])
    dep = _parse(info.get("estimated_out")) or _parse(info.get("scheduled_out"))
    return bool(o and dep and abs(when - dep) <= timedelta(minutes=30) and (pos.get("gs_kt") or 0) >= 5
                and km(pos["lat"], pos["lon"], o["lat"], o["lon"]) < 5)


def _pick(flights, f, when):
    """FlightAware lists several days of a flight number: take the one in progress, else the next, else the last."""
    def t(x, *keys):
        for k in keys:
            v = _parse(x.get(k))
            if v:
                return v
        return None
    live = [x for x in flights if t(x, "actual_off", "actual_out") and not t(x, "actual_on", "actual_in")]
    if f["origin"]:
        live = [x for x in live if (x.get("origin") or {}).get("code_iata") == f["origin"]] or live
    if live:
        return live[0]
    upcoming = [x for x in flights if not t(x, "actual_on", "actual_in") and not x.get("cancelled")
                and (t(x, "scheduled_out", "scheduled_off") or when) >= when - timedelta(hours=6)]
    if f["origin"]:
        upcoming = [x for x in upcoming if (x.get("origin") or {}).get("code_iata") == f["origin"]] or upcoming
    if upcoming:
        return min(upcoming, key=lambda x: t(x, "scheduled_out", "scheduled_off") or when)
    started = min(_parse(f["started_at"]) or when, when)
    done =[x for x in flights if (t(x, "actual_on", "actual_in", "scheduled_on") or when) >= started - timedelta(hours=1)]
    return done[0] if done else None


def flightaware(f, key, get=_get_json, when=None):
    """The flight's own status from FlightAware AeroAPI, reduced to what the board shows."""
    when = when or now()
    data = get(AEROAPI + urllib.parse.quote(f["callsign"] or f["ident"]), headers={"x-apikey": key})
    x = _pick(data.get("flights") or [], f, when)
    if not x:
        return {}
    o, d = x.get("origin") or {}, x.get("destination") or {}
    keep = ("scheduled_out", "estimated_out", "actual_out", "scheduled_off", "estimated_off", "actual_off", "scheduled_on",
            "estimated_on", "actual_on", "scheduled_in", "estimated_in", "actual_in", "gate_origin", "gate_destination",
            "terminal_origin", "terminal_destination", "baggage_claim", "status", "progress_percent", "departure_delay",
            "arrival_delay", "cancelled", "diverted", "atc_ident", "registration", "aircraft_type")
    out = {k: x.get(k) for k in keep if x.get(k) not in (None, "")}
    out.update(origin=o.get("code_iata") or "", dest=d.get("code_iata") or "", origin_name=o.get("name") or "",
               dest_name=d.get("name") or "", dest_tz=d.get("timezone") or "", origin_tz=o.get("timezone") or "")
    return out


def faa(code, get=_get_json, cache=None):
    """Airport-wide delays and ground stops from the FAA (US airports). {} when there's nothing or it isn't covered."""
    a = airport(code)
    if not a or a["country"] != "US":
        return {}
    cache = cache if cache is not None else {}
    if code in cache:
        return cache[code]
    data = get(FAA + code)
    out = {}
    if data.get("Delay"):
        raw, plain = [], []
        for s in data.get("Status") or []:
            kind = s.get("Type") or ""
            why = s.get("Reason") or ""
            avg = s.get("AvgDelay") or s.get("MaxDelay") or s.get("ClosureEnd") or ""
            raw.append(" ".join(x for x in (kind, avg and f"({avg})", why and f"- {why}") if x))
            line = faa_line(code, s)
            if line:
                plain.append(line)
        # only entries that touch airline flights are shown; the FAA's own words always stay under "details"
        if plain:
            out = {"delay": True, "text": "; ".join(plain)[:300], "lines": plain,
                   "raw": "; ".join(b for b in raw if b)[:600]}
    cache[code] = out
    return out


# ---------- FAA notices in plain English ----------
ABBREV = {"CLSD": "closed", "CLSD.": "closed", "ACFT": "aircraft", "EXC": "except", "PPR": "prior permission required",
          "AD": "airport", "AP": "airport", "RWY": "runway", "TWY": "taxiway", "TFC": "traffic", "NON": "non",
          "SKED": "scheduled", "UNSKED": "unscheduled", "TRANSIENT": "transient", "GA": "general aviation",
          "TO": "to", "WIP": "work in progress", "MAINT": "maintenance", "OBST": "obstruction", "U/S": "out of service",
          "UNAVBL": "unavailable", "AVBL": "available", "TEMP": "temporary", "ARR": "arrivals", "DEP": "departures",
          "WX": "weather", "TSTM": "thunderstorms", "VOL": "volume", "RWY.": "runway", "FLT": "flight", "FLTS": "flights",
          "APCH": "approach", "ILS": "instrument landing system", "NOTAM": "notice", "ARPT": "airport", "SVC": "service",
          "REQ": "required", "RSTD": "restricted", "ACTVT": "activated", "DLA": "delay", "DLAS": "delays", "EQP": "equipment"}


def expand(text):
    """Spell out common FAA / NOTAM shorthand (offline list)."""
    words = [ABBREV.get(w.upper().strip(",;"), w) for w in (text or "").split()]
    return " ".join(w for w in words if w)


def _private_only(s):
    kind = (s.get("Type") or "").upper()
    if "GROUND STOP" in kind or "DELAY" in kind:
        return False  # real delays and stops affect airlines
    blob = " ".join(str(s.get(k) or "") for k in ("Type", "Reason", "Text")).upper().replace(",", " ").replace(".", " ")
    toks = set(blob.split())
    return bool(toks & {"GA", "UNSKED", "TRANSIENT", "PRIVATE"}) or "NON SKED" in blob or "NON-SKED" in blob


def faa_line(code, s):
    """One short readable line for an FAA status entry, or '' if it doesn't affect airline flights."""
    if _private_only(s):
        return ""
    kind = (s.get("Type") or "").strip()
    k = kind.lower()
    why = expand(s.get("Reason") or "").strip().rstrip(".").lower()
    avg = (s.get("AvgDelay") or s.get("MaxDelay") or "").strip()
    end = (s.get("EndTime") or s.get("ClosureEnd") or "").strip()
    tail = f" ({why})" if why else ""
    if "ground stop" in k:
        return f"{code}: ground stop" + (f" until {end}" if end else "") + tail
    if "clos" in k:
        return f"{code}: airport closed" + (f" until {end}" if end else "") + tail
    if "arriv" in k:
        what = "arrivals"
    elif "depart" in k:
        what = "departures"
    elif "ground delay" in k:
        what = "arrivals"
    else:
        what = "flights"
    if what == "flights" and not avg and not kind:
        return ""
    return f"{code}: {what} delayed" + (f" about {_nice_delay(avg)}" if avg else "") + tail


def _nice_delay(avg):
    a = avg.lower().replace("minutes", "min").replace("minute", "min").replace("hours", "hr").replace("hour", "hr")
    return " ".join(a.split())


def _zone(name):
    import zoneinfo
    try:
        return zoneinfo.ZoneInfo(name) if name else None
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None


def labeled_time(t, viewer_tz, tz="", code=""):
    """'5:03 PM MST' in the viewer's zone, plus '(4:03 PM PST at LAX)' when the airport's zone shows a different time."""
    def fmt(z):
        lt = t.astimezone(z)
        return lt.strftime("%I:%M %p").lstrip("0") + " " + (lt.tzname() or "UTC")
    vz, az = _zone(viewer_tz), _zone(tz)
    s = fmt(vz or az or timezone.utc)
    if vz and az and fmt(vz) != fmt(az):
        s += f" ({fmt(az)}" + (f" at {code}" if code else "") + ")"
    return s


def local_time(t, tz, home=""):
    """'7:27 PM' in the destination's time zone; with the zone's name when it differs from home."""
    z, hz = _zone(tz), _zone(home)
    lt = t.astimezone(z or timezone.utc)
    s = lt.strftime("%I:%M %p").lstrip("0")
    if not (z and hz and z.utcoffset(t) == hz.utcoffset(t)):
        s += " " + (lt.tzname() or "UTC")
    return s


def landing_note(info, home=""):
    """' Now landing 7:27 PM.' from the flight's current estimate, or ''."""
    t = _parse(info.get("estimated_in")) or _parse(info.get("estimated_on")) or _parse(info.get("scheduled_in"))
    if not t:
        return ""
    return f" Now landing {labeled_time(t, HOME_TZ, info.get('dest_tz') or '', info.get('dest') or '')}."


HOME_TZ = ""


# ---------- following a flight ----------
def _mins(a, b):
    return round((a - b).total_seconds() / 60) if a and b else None


def arrival_delay(info):
    """Minutes late at the gate (or runway), from FlightAware's times."""
    sched = _parse(info.get("scheduled_in")) or _parse(info.get("scheduled_on"))
    est = (_parse(info.get("actual_in")) or _parse(info.get("estimated_in")) or _parse(info.get("actual_on"))
           or _parse(info.get("estimated_on")))
    m = _mins(est, sched)
    if m is None and isinstance(info.get("arrival_delay"), (int, float)):
        m = round(info["arrival_delay"] / 60)
    return m


def phase(f, when=None):
    """added / scheduled / taxiing / airborne / landed / cancelled / diverted, from what we know."""
    when = when or now()
    info, pos = f["info"], f["pos"]
    if not own_position(f, pos, when):
        pos = {}
    if info.get("cancelled"):
        return "cancelled"
    if info.get("diverted"):
        return "diverted"
    if info.get("actual_on") or info.get("actual_in"):
        return "landed"
    dest = airport(f["dest"])
    seen = _parse(pos.get("seen_at"))
    if pos and dest:
        d = km(pos["lat"], pos["lon"], dest["lat"], dest["lon"])
        if f["told"].get("airborne") and pos.get("ground") and d < 10:
            return "landed"
        if f["told"].get("airborne") and seen and when - seen > timedelta(minutes=15) and d < 40 and (pos.get("alt_ft") or 0) < 6000:
            return "landed"  # gone quiet low down near the destination: on the ground, out of receivers' sight
    if pos and seen and when - seen < timedelta(minutes=10):
        return "taxiing" if pos.get("ground") else "airborne"
    if info.get("actual_off"):
        return "airborne"
    if info.get("actual_out"):
        return "taxiing"
    if info:
        return "scheduled"
    return "added"


def _tell(event, data):
    for fn in list(listeners):
        try:
            fn(event, data)
        except Exception:
            pass


def _where(code):
    a = airport(code)
    return a["city"] or a["name"] if a else code


def check(store, f, key=None, get=_get_json, when=None, faa_cache=None):
    """Bring one flight up to date and tell the family about what changed. Returns the flight."""
    when = when or now()
    errors = []
    started = _parse(f["started_at"]) or when
    # FlightAware: on adding, then every 30 min (10 around departure and arrival), until landed; within the cap
    if key and f["status"] not in ("landed", "cancelled") and lookups_this_month(store, when) < MONTHLY_CAP:
        last = _parse(f["aero_at"])
        info = f["info"]
        near = any(abs((t - when).total_seconds()) < 2700 for t in
                   (_parse(info.get(k)) for k in ("estimated_out", "scheduled_out", "estimated_on", "scheduled_on")) if t)
        if last is None or when - last >= timedelta(minutes=10 if near else 30):
            try:
                _count_lookup(store, when)
                got = flightaware(f, key, get, when)
                f["aero_at"] = _iso(when)
                if got:
                    f["info"] = got
                    f["origin"] = got.get("origin") or f["origin"]
                    f["dest"] = got.get("dest") or f["dest"]
                    if got.get("dest"):
                        f["told"].pop("dest_guess", None)  # FlightAware knows where it's going
            except Exception as exc:
                errors.append(f"FlightAware: {type(exc).__name__}")
                f["aero_at"] = _iso(when)
    inbound = None
    try:
        pos = adsb(f, get, when)
        if pos and own_position(f, pos, when):
            f["pos"] = pos
        elif pos:
            inbound = pos  # the tail number's current flight, not hers yet
    except Exception as exc:
        errors.append(f"adsb.lol: {type(exc).__name__}")
    faa_now = {}
    for code in dict.fromkeys(c for c in (f["origin"], f["dest"]) if c):
        try:
            got = faa(code, get, faa_cache)
            if got:
                faa_now[code] = got
        except Exception:
            pass
    f["info"]["faa"] = faa_now
    f["info"]["errors"] = errors
    f["info"].pop("inbound", None)
    if inbound:
        f["info"]["inbound"] = inbound
    old = f["status"]
    f["status"] = phase(f, when)
    if f["status"] == "airborne":
        f["told"]["airborne"] = True
    # what the family hears about
    who, what = f["person_name"], f["ident"]
    late = arrival_delay(f["info"])
    if f["status"] in ("scheduled", "taxiing", "airborne") and late is not None and late >= DELAY_STEP:
        step = late // DELAY_STEP * DELAY_STEP
        if step > f["told"].get("delay", 0):
            f["told"]["delay"] = step
            _tell("flight", {"person_uid": f["person_uid"], "uid": f["uid"], "title": f"{who}'s flight {what} is running late",
                             "body": (f"About {late} min late into {_where(f['dest'])}." if f["dest"] else f"About {late} min late.")
                             + landing_note(f["info"])})
    if f["status"] != old and f["status"] in ("landed", "cancelled", "diverted") and not f["told"].get(f["status"]):
        f["told"][f["status"]] = _iso(when)
        title = {"landed": f"{who} landed" + (f" in {_where(f['dest'])}" if f["dest"] else ""),
                 "cancelled": f"{who}'s flight {what} was cancelled", "diverted": f"{who}'s flight {what} was diverted"}[f["status"]]
        _tell("flight", {"person_uid": f["person_uid"], "uid": f["uid"], "title": title,
                         "body": f"{what} {f['origin']} to {f['dest']}".strip() if f["origin"] or f["dest"] else what})
    _save(store, f)
    # clear it: a few hours after landing, or when nothing has been heard of it for a day
    landed_at = _parse(f["told"].get("landed") or f["info"].get("actual_in") or f["info"].get("actual_on"))
    if f["status"] == "landed" and landed_at and when - landed_at > timedelta(hours=3):
        end(store, f["uid"], "landed")
    elif f["status"] in ("cancelled", "diverted") and when - started > timedelta(hours=12):
        end(store, f["uid"], f["status"])
    elif when - started > timedelta(hours=30):
        end(store, f["uid"], "timed out")
    return f


def check_all(store, key=None, get=_get_json, when=None):
    cache = {}
    return [check(store, f, key, get, when, cache) for f in active(store)]


FLIGHT_WORDS = {"added": "added", "scheduled": "scheduled", "taxiing": "on the ground", "airborne": "in the air",
                "landed": "landed", "cancelled": "CANCELLED", "diverted": "DIVERTED"}


def view(f, when=None):
    """What the board shows for one flight."""
    when = when or now()
    info, pos = f["info"], f["pos"]
    seen = _parse(pos.get("seen_at")) if pos else None
    o, d = airport(f["origin"]), airport(f["dest"])
    out = {"uid": f["uid"], "person_uid": f["person_uid"], "person_name": f["person_name"], "ident": f["ident"],
           "callsign": f["callsign"], "airline": f["airline"], "status": f["status"],
           "origin": f["origin"], "dest": f["dest"], "dest_guess": bool(f["dest"] and f["told"].get("dest_guess")), "origin_name": o["name"] if o else "", "dest_name": d["name"] if d else "",
           "origin_city": o["city"] if o else "", "dest_city": d["city"] if d else "", "started_at": f["started_at"],
           "updated_at": f["updated_at"], "late_min": arrival_delay(info), "faa": info.get("faa") or {},
           "times": {k: info.get(k) for k in ("scheduled_out", "estimated_out", "actual_out", "scheduled_in", "estimated_in",
                                              "actual_in", "scheduled_on", "estimated_on", "actual_on") if info.get(k)},
           "gates": {k: info.get(k) for k in ("terminal_origin", "gate_origin", "terminal_destination", "gate_destination",
                                              "baggage_claim") if info.get(k)},
           "fa_status": info.get("status") or "", "aircraft": info.get("aircraft_type") or "", "has_fa": bool(f["aero_at"] and info.get("origin")),
           "origin_tz": info.get("origin_tz") or "", "dest_tz": info.get("dest_tz") or "", "pos": None, "inbound": None}
    ib = info.get("inbound")
    ib_seen = _parse(ib.get("seen_at")) if ib else None
    if ib and o and ib_seen and when - ib_seen < timedelta(minutes=10) and not (info.get("actual_out") or info.get("actual_off")):
        left = km(ib["lat"], ib["lon"], o["lat"], o["lon"])
        at = bool(ib.get("ground")) and left < 10
        gs = ib.get("gs_kt") or 0
        out["inbound"] = {"at_origin": at, "eta_min": None if at or gs < 50 or ib.get("ground") else round(left / (gs * 1.852) * 60),
                          "km": round(left)}
    if pos:
        out["pos"] = pos | {"age_min": _mins(when, seen)}
        if d and not pos.get("ground"):
            left = km(pos["lat"], pos["lon"], d["lat"], d["lon"])
            out["pos"]["km_to_go"] = round(left)
    cs = out["callsign"] or f["ident"]
    fr = (cs if f["told"].get("confirmed") else f["ident"]).lower()  # a picked callsign, else the ticket number
    out["links"] = {"FlightAware": f"https://www.flightaware.com/live/flight/{urllib.parse.quote(cs)}",
                    "Flightradar24": f"https://www.flightradar24.com/data/flights/{urllib.parse.quote(fr)}"}
    return out


def board(store):
    return [view(f) for f in active(store)]
