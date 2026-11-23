"""Where is "80202" or "Denver CO"? An offline answer, for the mesh bot's weather, sun and aircraft lookups.

The U.S. Census Bureau's gazetteer files (public domain) list every ZIP code area (ZCTA) and every town and city
(incorporated places and census-designated places) with the point at its middle. BUNKR fetches them once a year
into the store, boils them down to one small JSON file, and answers from that with no internet. A ZIP code is
named after the nearest town ("80202 Denver, CO"), which also gives its state.

Only the lookup is offline: weather and alerts for the place still need the internet.
"""

import io
import json
import math
import re
import urllib.request
import zipfile
from pathlib import Path

YEAR = 2025
BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{y}_Gazetteer/{y}_Gaz_{kind}_national.zip"
FILE = "places.json"
USER_AGENT = "BUNKR places"
# words the Census puts after a place's name ("Denver city", "Aspen Park CDP")
SUFFIX = re.compile(r"\s+(city and borough|consolidated government|metropolitan government|unified government|"
                    r"metro government|urban county|city|town|village|borough|CDP|municipality|plantation|comunidad|"
                    r"zona urbana|corporation)(\s*\(balance\))?$", re.I)
STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
          "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR",
          "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR", "GU", "VI", "AS", "MP"}
STATE_NAMES = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
               "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
               "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
               "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
               "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
               "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
               "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
               "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
               "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
               "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR"}
# a state's time zone when the weather service can't be asked (states split between zones get their main one)
STATE_TZ = {**{s: "America/New_York" for s in ("CT", "DE", "DC", "GA", "ME", "MD", "MA", "NH", "NJ", "NY", "NC", "OH", "PA",
                                                  "RI", "SC", "VT", "VA", "WV", "MI", "IN", "FL", "KY")},
            **{s: "America/Chicago" for s in ("AL", "AR", "IL", "IA", "LA", "MN", "MS", "MO", "OK", "WI", "TN", "TX", "KS",
                                                 "NE", "SD", "ND")},
            **{s: "America/Denver" for s in ("CO", "MT", "NM", "UT", "WY", "ID")},
            "AZ": "America/Phoenix", "CA": "America/Los_Angeles", "NV": "America/Los_Angeles", "OR": "America/Los_Angeles",
            "WA": "America/Los_Angeles", "AK": "America/Anchorage", "HI": "Pacific/Honolulu", "PR": "America/Puerto_Rico",
            "VI": "America/St_Thomas", "GU": "Pacific/Guam", "AS": "Pacific/Pago_Pago", "MP": "Pacific/Saipan"}

_cache = {}   # path -> (mtime, data)


def _rows(raw):
    """The gazetteer's text file (pipe- or tab-separated, with a header) -> list of dicts."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    sep = "|" if "|" in lines[0] else "\t"
    head = [h.strip() for h in lines[0].split(sep)]
    return [dict(zip(head, (v.strip() for v in l.split(sep)))) for l in lines[1:]]


def _unzip(blob):
    z = zipfile.ZipFile(io.BytesIO(blob))
    name = next(n for n in z.namelist() if n.endswith(".txt"))
    return z.read(name)


def build(zcta_text, place_text):
    """The two gazetteer files -> {"zip": {zip: [lat, lon]}, "place": [[name, state, lat, lon, radius km], ...]}.
    The radius is that of a circle with the town's land area, so a ZIP code in a big city's downtown is named after
    the city rather than a small town whose middle happens to be closer."""
    zips, places = {}, []
    for r in _rows(zcta_text):
        try:
            zips[r["GEOID"][:5]] = [round(float(r["INTPTLAT"]), 4), round(float(r["INTPTLONG"]), 4)]
        except (KeyError, ValueError):
            continue
    for r in _rows(place_text):
        try:
            st = r["USPS"].upper()
            name = SUFFIX.sub("", r["NAME"]).strip()
            if st in STATES and name:
                rad = math.sqrt(float(r.get("ALAND_SQMI") or 0) * 2.59 / math.pi)
                places.append([name, st, round(float(r["INTPTLAT"]), 4), round(float(r["INTPTLONG"]), 4), round(rad, 1)])
        except (KeyError, ValueError):
            continue
    return {"year": YEAR, "zip": zips, "place": places}


def _get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(50_000_000)


def refresh(folder, get=_get, year=YEAR):
    """Fetch this year's files and replace the saved copy only when they look right. Returns the counts."""
    folder = Path(folder)
    data = build(_unzip(get(BASE.format(y=year, kind="zcta"))), _unzip(get(BASE.format(y=year, kind="place"))))
    if len(data["zip"]) < 30000 or len(data["place"]) < 25000:
        raise ValueError(f"the Census files look short ({len(data['zip'])} ZIP codes, {len(data['place'])} places)")
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (FILE + ".part")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(folder / FILE)
    _cache.pop(str(folder / FILE), None)
    return {"zip": len(data["zip"]), "place": len(data["place"])}


def needs_refresh(folder, year=YEAR):
    d = load(folder)
    return not d or d.get("year", 0) < year


def load(folder):
    """The saved lookup, or {} before the first fetch."""
    p = Path(folder) / FILE
    try:
        m = p.stat().st_mtime
    except OSError:
        return {}
    hit = _cache.get(str(p))
    if hit and hit[0] == m:
        return hit[1]
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    data["_index"] = {}
    for name, st, lat, lon, *_ in data.get("place", []):
        data["_index"].setdefault(name.lower(), []).append((name, st, lat, lon))
    _cache[str(p)] = (m, data)
    return data


def km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(a)))


def nearest_town(data, lat, lon):
    """(name, state) of the town the point is most inside: distance to its middle less its radius."""
    best, bd = None, None
    for name, st, plat, plon, *rad in data.get("place", []):
        if abs(plat - lat) > 1 or abs(plon - lon) > 1.5:
            continue  # cheap box first
        d = km(lat, lon, plat, plon) - (rad[0] if rad else 0)
        if bd is None or d < bd:
            best, bd = (name, st), d
    return best or ("", "")


def _place(lat, lon, label, state, kind):
    return {"lat": lat, "lon": lon, "label": label, "state": state, "tz": STATE_TZ.get(state, ""), "kind": kind}


def find(folder, text, prefer_state=""):
    """'80202', 'Denver CO', 'Denver, Colorado' or 'Golden' -> {lat, lon, label, state, tz, kind}, or None.
    A town name alone is used when one state has a place by that name, or one of them is in prefer_state (home)."""
    data = load(folder)
    t = " ".join(str(text or "").replace(",", " ").split())
    if not t or not data:
        return None
    m = re.fullmatch(r"(\d{5})(?:-\d{4})?", t)
    if m:
        z = data.get("zip", {}).get(m.group(1))
        if not z:
            return None
        name, st = nearest_town(data, z[0], z[1])
        return _place(z[0], z[1], f"{m.group(1)} {name}, {st}" if name else m.group(1), st, "zip")
    words = t.split()
    st = ""
    for n in (2, 1):  # "New Mexico" before "Mexico"
        tail = " ".join(words[-n:]).lower() if len(words) > n else ""
        if tail.upper() in STATES and n == 1:
            st, words = tail.upper(), words[:-1]
            break
        if tail in STATE_NAMES:
            st, words = STATE_NAMES[tail], words[:-n]
            break
    hits = data["_index"].get(" ".join(words).lower(), [])
    if st:
        hits = [h for h in hits if h[1] == st]
    elif len({h[1] for h in hits}) > 1 and prefer_state:
        hits = [h for h in hits if h[1] == prefer_state] or hits
    if not hits or len({h[1] for h in hits}) > 1:
        return None
    name, st, lat, lon = hits[0]
    return _place(lat, lon, f"{name}, {st}", st, "place")


def ambiguous(folder, text):
    """The states that have a town by this name (to ask "which one?")."""
    data = load(folder)
    return sorted({h[1] for h in data.get("_index", {}).get(" ".join(str(text or "").replace(",", " ").split()).lower(), [])})
