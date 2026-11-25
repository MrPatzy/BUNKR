"""Keeping pace with what's happening where the family is: the towns on the Sites tab, watched closely.

Denver TV and papers are regular outlets (brief.OUTLETS). On top of that, every 10 minutes BUNKR asks Google News for
each family town ("Nederland, CO"), so a boil-water notice that only local TV reports still arrives. A story there that
says hack, outage, boil, evacuate and the like is raised as "Near Jordan (Nederland)".

Until 1.3 this also read public posts (Bluesky, and Reddit with an app key) into an "Unconfirmed" view. Reddit closed
its API to new apps and blocks its public feeds, and the rest found almost nothing local, so that part is gone.
"""

import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import brief

DEFAULTS = {"extra_places": []}
MAX_PLACES = 12
STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
          "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
          "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
          "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
          "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
          "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
          "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
          "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
          "DC": "District of Columbia"}


def _now():
    return datetime.now(timezone.utc)


# ---------- settings ----------
def settings(store):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (store.get_raw("localwatch", {}) or {}).items() if k in DEFAULTS})
    return s


def _lines(v, n, size, sep=r"[\n,]"):
    if isinstance(v, str):
        v = re.split(sep, v)
    return list(dict.fromkeys(" ".join(str(x).split())[:size] for x in (v or []) if str(x).strip()))[:n]


def save_settings(store, data):
    s = settings(store)
    if "extra_places" in data:
        s["extra_places"] = _lines(data["extra_places"], MAX_PLACES, 60, sep=r"\n")  # "Conifer, CO" is one place
    store.set_raw("localwatch", s)
    return view(store)


def view(store):
    return settings(store) | {"places": places(store)}


# ---------- the family's places ----------
_STATE_PART = re.compile(r"^\s*([A-Z]{2}|[A-Za-z ]{4,20}?)\s*(\d{5}(-\d{4})?)?\s*$")


def city_of(address):
    """ "123 Main St, Nederland, CO 80466" -> ("Nederland", "CO"). ("", "") when it can't tell."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    for i in range(len(parts) - 1, 0, -1):
        m = _STATE_PART.match(parts[i])
        st = m.group(1).strip() if m else ""
        code = st.upper() if st.upper() in STATES else next((k for k, v in STATES.items() if v.lower() == st.lower()), "")
        if code and not re.search(r"\d", parts[i - 1]):
            return parts[i - 1], code
    return "", ""


def places(store):
    """Each town to watch, with who is there: [{"name", "state", "who": ["Jordan"], "label": "Jordan (Nederland)"}]."""
    out = {}
    home_people = [p["name"] for p in store._rows("SELECT name FROM people WHERE remote=0 ORDER BY name")]
    for s in store._rows("SELECT id, name, kind, address, state, county_name FROM sites ORDER BY id"):
        city, st = city_of(s["address"])
        if not city:
            continue
        who = [p["name"] for p in store._rows("SELECT name FROM people WHERE site_id=? ORDER BY name", (s["id"],))]
        if s["kind"] == "home":
            who = ["home"] + [n for n in who if n not in home_people]
        key = (city.lower(), st)
        e = out.setdefault(key, {"name": city, "state": st, "who": [], "sites": [], "county": (s["county_name"] or "").replace(" County", "")})
        e["who"] += [w for w in who if w not in e["who"]]
        e["sites"].append(s["name"])
    for extra in settings(store)["extra_places"]:
        city, st = city_of("x, " + extra) if "," in extra else (extra, "")
        out.setdefault(((city or extra).lower(), st), {"name": city or extra, "state": st, "who": [], "sites": [], "county": ""})
    res = []
    for e in list(out.values())[:MAX_PLACES]:
        who = ", ".join(e["who"]) if e["who"] else ""
        e["label"] = f"{who} ({e['name']})" if who else e["name"]
        res.append(e)
    return res


def place_re(p):
    return re.compile(r"(?<![\w])" + re.escape(p["name"]) + r"(?![\w])", re.I)


def local_re(p):
    """A post that's really about the town, not a product or a word that happens to share its name ("Nederland
    Biscuits recalled"): "in/near Nederland", "Nederland, CO", the state's name, or the county."""
    n = re.escape(p["name"])
    alts = [r"\b(?:in|near|around|at|across|throughout|outside|up)\s+" + n + r"\b", n + r",?\s+(?:" + re.escape(p.get("state") or "~") + r")\b"]
    if STATES.get(p.get("state")):
        alts.append(r"\b" + re.escape(STATES[p["state"]]) + r"\b")
    if p.get("county"):
        alts.append(r"\b" + re.escape(p["county"]) + r"\b")
    if p.get("state") == "CO":
        alts.append(r"\b(?:denver|front range|i-70)\b")
    return re.compile("|".join(alts), re.I)


# ---------- layer 1: a Google News search for each town ----------
def fetch_places(plist, opener=None):
    """Headlines about each family town from the last day, as outlet "local" (the publisher goes in the summary)."""
    heads, status = [], {}
    for p in plist:
        q = f'"{p["name"]}" {STATES.get(p["state"], p["state"])}'.strip() + " when:1d"
        url = brief.GN + urllib.parse.quote_plus(q)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": brief.USER_AGENT, "Accept": "application/rss+xml, */*"})
            with (opener or urllib.request.build_opener()).open(req, timeout=20) as r:
                raw = r.read(brief.MAX_FEED + 1)
            got = 0
            for title, link, when, _summary in brief.parse_feed(raw):
                if when is not None and when < _now() - timedelta(hours=30):
                    continue
                m = re.match(r"^(.*\S)\s+-\s+([^-]{2,60})$", title)
                t, pub = (m.group(1), m.group(2)) if m else (title, "")
                if not place_re(p).search(t):
                    continue  # Google matched it somewhere in the page; only keep headlines that name the town
                heads.append({"outlet": "local", "title": t, "url": link, "summary": f"Reported by {pub}." if pub else "",
                              "at": when.isoformat(timespec="seconds") if when else ""})
                got += 1
            status[p["name"]] = {"ok": True, "count": got}
        except Exception as exc:
            status[p["name"]] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160]}
    return heads, status
