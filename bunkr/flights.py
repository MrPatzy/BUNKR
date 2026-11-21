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
def parse_number(text):
    """'UA 1234', 'ua1234' or 'UAL1234' -> {"ident": "UA1234", "callsign": "UAL1234", "airline": "United"}.
    'UA 1234 to LAX' also gives "dest": "LAX"."""
    text, _, to = (text or "").upper().partition(" TO ")
    dest = to.strip() if airport(to.strip()) else ""
    if to.strip() and not dest:
        raise ValueError(f"no airport called {to.strip()[:10]}: use its 3-letter code, like DEN")
    s = re.sub(r"[\s-]+", "", text)
    m = re.fullmatch(r"([A-Z]{3})(\d{1,4}[A-Z]?)", s) or re.fullmatch(r"([A-Z][A-Z0-9]|[0-9][A-Z])(\d{1,4}[A-Z]?)", s)
    if not m:
        raise ValueError("type the flight like UA 1234")
    code, num = m.groups()
    extra = {"dest": dest} if dest else {}
    if len(code) == 3:
        iata, name = BY_ICAO.get(code, ("", ""))
        return {"ident": (iata + num) if iata else code + num, "callsign": code + num, "airline": name, **extra}
    icao, name = AIRLINES.get(code, ("", ""))
    return {"ident": code + num, "callsign": (icao + num) if icao else "", "airline": name, **extra}


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


def add(store, person_uid, number, origin="", dest="", by="", home=None):
    """Start following someone's flight. Any flight they were already on is ended. With no destination given, it's
    taken to be home's airport (unless they're leaving from it); FlightAware corrects it when set up."""
    p = store._rows("SELECT uid, name FROM people WHERE uid=?", (person_uid or "",))
    if not p:
        raise ValueError("unknown person")
    n = parse_number(number)
    origin = origin.upper() if airport(origin) else ""
    dest = dest.upper() if airport(dest) else n.get("dest", "")
    if not dest and home and home["iata"] != origin:
        dest = home["iata"]
    t = _iso(now())
    for old in store._rows("SELECT uid FROM flights WHERE person_uid=? AND ended_at=''", (p[0]["uid"],)):
        end(store, old["uid"], "a new flight")
    rec = {"uid": uuid.uuid4().hex, "person_uid": p[0]["uid"], "person_name": p[0]["name"], "ident": n["ident"],
           "callsign": n["callsign"], "airline": n["airline"], "origin": origin, "dest": dest, "started_at": t,
           "started_by": (by or "")[:80], "updated_at": t, "ended_at": "", "ended_why": "", "status": "added",
           "info": "{}", "pos": "{}", "aero_at": "", "told": "{}"}
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
    tries = [("callsign", c) for c in dict.fromkeys(x for x in (info.get("atc_ident"), f["callsign"]) if x)]
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
                    "ground": alt == "ground", "gs_kt": a.get("gs"), "track": a.get("track"), "seen_at": _iso(seen)}
    return {}


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
        bits = []
        for s in data.get("Status") or []:
            kind = s.get("Type") or ""
            why = s.get("Reason") or ""
            avg = s.get("AvgDelay") or s.get("MaxDelay") or s.get("ClosureEnd") or ""
            bits.append(" ".join(x for x in (kind, avg and f"({avg})", why and f"- {why}") if x))
        out = {"delay": True, "text": "; ".join(b for b in bits if b)[:300] or "Delays reported"}
    cache[code] = out
    return out


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
                    if got.get("atc_ident") and not f["callsign"]:
                        f["callsign"] = got["atc_ident"]
            except Exception as exc:
                errors.append(f"FlightAware: {type(exc).__name__}")
                f["aero_at"] = _iso(when)
    try:
        pos = adsb(f, get, when)
        if pos:
            f["pos"] = pos
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
                             "body": f"About {late} min late into {_where(f['dest'])}." if f["dest"] else f"About {late} min late."})
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
           "callsign": info.get("atc_ident") or f["callsign"], "airline": f["airline"], "status": f["status"],
           "origin": f["origin"], "dest": f["dest"], "origin_name": o["name"] if o else "", "dest_name": d["name"] if d else "",
           "origin_city": o["city"] if o else "", "dest_city": d["city"] if d else "", "started_at": f["started_at"],
           "updated_at": f["updated_at"], "late_min": arrival_delay(info), "faa": info.get("faa") or {},
           "times": {k: info.get(k) for k in ("scheduled_out", "estimated_out", "actual_out", "scheduled_in", "estimated_in",
                                              "actual_in", "scheduled_on", "estimated_on", "actual_on") if info.get(k)},
           "gates": {k: info.get(k) for k in ("terminal_origin", "gate_origin", "terminal_destination", "gate_destination",
                                              "baggage_claim") if info.get(k)},
           "fa_status": info.get("status") or "", "aircraft": info.get("aircraft_type") or "", "has_fa": bool(f["aero_at"] and info.get("origin")),
           "pos": None}
    if pos:
        out["pos"] = pos | {"age_min": _mins(when, seen)}
        if d and not pos.get("ground"):
            left = km(pos["lat"], pos["lon"], d["lat"], d["lon"])
            out["pos"]["km_to_go"] = round(left)
    cs = out["callsign"] or f["ident"]
    out["links"] = {"FlightAware": f"https://www.flightaware.com/live/flight/{urllib.parse.quote(cs)}",
                    "Flightradar24": f"https://www.flightradar24.com/data/flights/{urllib.parse.quote(f['ident'].lower())}"}
    return out


def board(store):
    return [view(f) for f in active(store)]
