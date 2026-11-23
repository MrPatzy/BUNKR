"""The Situation tab: official alerts, disasters, fires, space weather, internet outages and cyber, ranked by how
much they matter to the family's own places.

Three tiers, on purpose (calm by design, after Watch Duty and the "avoid Citizen" lesson):
  flash     a verified, official threat to life or property at one of your places. Pushes, even in quiet hours.
  priority  something to act on soon, near you or affecting things you use. Pushes outside quiet hours, capped.
  routine   worth knowing; never pushes. Goes into the morning brief.

Each source is fetched on its own; one failing never stops the others, and its last good data stays with an
"as of" time. Items carry where they came from (official / confirmed / reported / satellite) so nobody mistakes a
satellite hot spot for a confirmed fire. Nodes get the items through the normal sync and show them "as of".
"""

import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

USER_AGENT = "BUNKR/0.3 (self-hosted preparedness app)"
TIERS = {"flash": "Act now", "priority": "Soon / near you", "routine": "Good to know"}
TIER_ORDER = {"flash": 0, "priority": 1, "routine": 2}
TRUST = {"official": "Official", "confirmed": "Confirmed", "reported": "Reported", "satellite": "Satellite, unconfirmed"}
CATEGORIES = {"weather": "Weather and hazards", "fire": "Wildfire", "quake": "Earthquakes", "disaster": "Disasters worldwide",
              "space": "Space weather", "internet": "Internet outages", "cyber": "Cyber", "declaration": "Disaster declarations"}
DEFAULT_PREFS = {
    # things in the house a CISA "actively exploited" entry should be matched against
    "devices": ["Ubuntu", "Android", "iOS", "Chrome", "Windows", "Raspberry Pi"],
    # local organisations a ransomware hit would matter for (hospital, utility, school district, bank...)
    "local_orgs": ["Colorado", "Denver", "Xcel", "UCHealth", "Intermountain", "Denver Water"],
    "state": "Colorado",
}
listeners = []


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def km(a_lat, a_lon, b_lat, b_lon):
    r = math.radians
    d = math.sin(r(b_lat - a_lat) / 2) ** 2 + math.cos(r(a_lat)) * math.cos(r(b_lat)) * math.sin(r(b_lon - a_lon) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(d))


def nearest(sites, lat, lon):
    """(site name, km) of the closest site with coordinates, or ("", None)."""
    best = ("", None)
    if lat is None or lon is None:
        return best
    for s in sites:
        if s.get("lat") is None or s.get("lon") is None:
            continue
        d = km(s["lat"], s["lon"], lat, lon)
        if best[1] is None or d < best[1]:
            best = (s["name"], round(d))
    return best


# which household checklist an alert should open
SCENARIO_WORDS = [
    (r"tornado|severe thunderstorm|derecho", "tornado"),
    (r"evacuat|fire warning|wildfire", "evacuation"),
    (r"red flag|smoke|air quality", "wildfire_smoke"),
    (r"flood", "flood"),
    (r"winter storm|blizzard|ice storm|wind chill|extreme cold|freeze|winter weather", "winter_storm"),
    (r"heat", "extreme_heat"),
    (r"power outage|high wind", "power_outage"),
]


def scenario_for(text):
    t = (text or "").lower()
    for pat, sid in SCENARIO_WORDS:
        if re.search(pat, t):
            return sid
    return ""


def _item(source, uid, category, tier, title, **kw):
    base = {"uid": f"{source}:{uid}", "source": source, "category": category, "tier": tier, "title": title[:200],
            "summary": "", "where_text": "", "lat": None, "lon": None, "site": "", "km": None, "url": "",
            "starts": "", "ends": "", "scenario": "", "trust": "official", "geom": ""}
    base.update({k: v for k, v in kw.items() if k in base})
    base["summary"] = (base["summary"] or "")[:1200]
    url = str(base["url"] or "").strip()
    base["url"] = url if url.lower().startswith(("https://", "http://")) else ""  # a feed's link becomes a link in the app
    return base


def _get(url, timeout=25, accept="application/json", headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _get_text(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _iso_ms(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


# ---------- sources: each returns a list of items ----------
def from_nws(sites):
    """NWS alerts already fetched per site (Sites & alerts). Warnings for life and property are flash."""
    out = {}
    for s in sites:
        for a in s.get("alerts") or []:
            ev = a.get("event") or ""
            sev = a.get("severity") or ""
            if re.search(r"warning|emergency|evacuat|shelter", ev, re.I) and sev in ("Extreme", "Severe"):
                tier = "flash"
            elif re.search(r"warning|watch", ev, re.I) or sev in ("Extreme", "Severe"):
                tier = "priority"
            else:
                tier = "routine"
            key = a.get("id") or f"{ev}-{a.get('onset')}"
            if key in out:  # the same alert covers several of our places
                out[key]["where_text"] += f", {s['name']}"
                continue
            out[key] = _item("nws", key, "weather", tier, f"{ev}", summary=(a.get("headline") or "") + (
                f"\nWhat to do: {a['instruction']}" if a.get("instruction") else ""), where_text=s["name"], site=s["name"], km=0,
                starts=a.get("onset") or "", ends=a.get("ends") or a.get("expires") or "",
                scenario=scenario_for(ev), trust="official", geom=json.dumps(a["geometry"]) if a.get("geometry") else "")
    return list(out.values())


def fetch_quakes(sites, get=_get):
    data = get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson")
    out = []
    for f in data.get("features", []):
        p, (lon, lat, *_) = f.get("properties", {}), f.get("geometry", {}).get("coordinates", [None, None])
        mag = p.get("mag") or 0
        site, d = nearest(sites, lat, lon)
        if d is not None and ((mag >= 4.0 and d <= 250) or (mag >= 5.5 and d <= 600)):
            tier = "priority"
        elif d is not None and mag >= 3.0 and d <= 60:
            tier = "routine"  # felt, not dangerous
        elif mag >= 6.5:
            tier = "routine"
        else:
            continue
        out.append(_item("usgs", f.get("id") or p.get("code"), "quake", tier, f"M{mag:.1f} earthquake, {p.get('place') or 'unknown place'}",
                         summary=("Tsunami possible. " if p.get("tsunami") else "") + (p.get("title") or ""),
                         lat=lat, lon=lon, site=site, km=d, url=p.get("url") or "", starts=_iso_ms(p.get("time")),
                         where_text=p.get("place") or ""))
    return out


def fetch_gdacs(sites, get=_get):
    data = get("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?alertlevel=Orange;Red&eventlist=EQ;TC;FL;VO;WF;DR")
    out = []
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    for f in data.get("features", []):
        p = f.get("properties", {})
        if (p.get("todate") or "") < recent:
            continue  # over: GDACS keeps finished events in this list for weeks
        lon, lat = (f.get("geometry", {}).get("coordinates") or [None, None])[:2]
        site, d = nearest(sites, lat, lon)
        tier = "priority" if d is not None and d <= 500 else "routine"
        if p.get("eventtype") == "DR" and tier == "routine":
            continue  # droughts last months; not news
        sev = (p.get("severitydata") or {}).get("severitytext") or ""
        out.append(_item("gdacs", f"{p.get('eventtype')}{p.get('eventid')}", "disaster", tier,
                         f"{p.get('alertlevel')} alert: {p.get('name') or p.get('eventname')}"[:200],
                         summary=sev, lat=lat, lon=lon, site=site, km=d, url=(p.get("url") or {}).get("report", ""),
                         starts=p.get("fromdate") or "", ends=p.get("todate") or "", where_text=p.get("country") or "",
                         trust="official"))
    return out


def fetch_fires(sites, get=_get, state="US-CO"):
    q = urllib.parse.urlencode({"where": f"POOState='{state}'", "f": "geojson",
                                "outFields": "IncidentName,IncidentSize,PercentContained,FireDiscoveryDateTime,ModifiedOnDateTime,IncidentTypeCategory"})
    data = get("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0/query?" + q)
    out = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        lon, lat = (f.get("geometry", {}).get("coordinates") or [None, None])[:2]
        site, d = nearest(sites, lat, lon)
        name = p.get("IncidentName") or "Unnamed fire"
        planned = p.get("IncidentTypeCategory") == "RX" or re.search(r"\bRX\b|prescribed|piles", name, re.I)
        if d is None:
            continue
        if d <= 15 and not planned:
            tier, scen = "priority", "evacuation"
        elif d <= 60:
            tier, scen = "routine", "wildfire_smoke"
        else:
            continue
        size = p.get("IncidentSize")
        bits = [f"{size:,.0f} acres" if size else "", f"{p['PercentContained']:.0f}% contained" if p.get("PercentContained") is not None else "",
                "prescribed burn" if planned else ""]
        out.append(_item("wfigs", name + str(p.get("FireDiscoveryDateTime")), "fire", tier, f"{name} fire, {d} km from {site}",
                         summary=", ".join(b for b in bits if b), lat=lat, lon=lon, site=site, km=d,
                         starts=_iso_ms(p.get("FireDiscoveryDateTime")), scenario=scen, trust="confirmed",
                         url="https://www.watchduty.org/"))
    return out


R_TEXT = {1: "minor HF radio fading on the day side", 2: "limited HF radio blackouts on the day side",
          3: "HF radio blackout for about an hour on the day side; GPS errors", 4: "HF radio blackout for hours; GPS errors",
          5: "complete HF radio blackout on the day side for hours"}
G_TEXT = {1: "weak power grid fluctuations; aurora far north", 2: "HF radio fades at high latitudes; aurora possible in the north",
          3: "GPS and HF radio may be degraded tonight; aurora possible in Colorado",
          4: "possible power grid voltage problems; GPS and HF degraded for hours; aurora likely",
          5: "possible widespread power grid problems; HF radio out for days; GPS degraded"}
S_TEXT = {1: "minor effects on HF in polar regions", 2: "small effects on satellites", 3: "HF radio degraded at high latitudes",
          4: "HF blackout in polar regions; satellite problems", 5: "HF out in polar regions; serious satellite problems"}


def fetch_space(sites, get=_get):
    data = get("https://services.swpc.noaa.gov/products/noaa-scales.json")
    cur = data.get("0") or {}
    out = []
    for scale, text, label in (("R", R_TEXT, "Radio blackout"), ("G", G_TEXT, "Geomagnetic storm"), ("S", S_TEXT, "Solar radiation storm")):
        try:
            level = int((cur.get(scale) or {}).get("Scale") or 0)
        except ValueError:
            level = 0
        if level < 1:
            continue
        tier = "priority" if level >= 3 else "routine"
        out.append(_item("swpc", f"{scale}{level}-{cur.get('DateStamp')}", "space", tier, f"{label} {scale}{level} now",
                         summary=text[level][:1].upper() + text[level][1:] + ".", url="https://www.swpc.noaa.gov/",
                         starts=f"{cur.get('DateStamp')}T{cur.get('TimeStamp')}+00:00", where_text="Earth"))
    return out


def fetch_kev(sites, get=_get, prefs=None, days=14):
    prefs = prefs or DEFAULT_PREFS
    data = get("https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    words = [w.strip().lower() for w in prefs.get("devices", []) if w.strip()]
    out = []
    for v in data.get("vulnerabilities", []):
        if (v.get("dateAdded") or "") < since:
            continue
        hay = f"{v.get('vendorProject')} {v.get('product')}".lower()
        mine = [w for w in words if w in hay]
        out.append(_item("kev", v.get("cveID"), "cyber", "priority" if mine else "routine",
                         f"{'Patch now: ' if mine else ''}{v.get('vendorProject')} {v.get('product')} flaw is being exploited",
                         summary=(v.get("shortDescription") or "") + (f"\nWhat to do: {v['requiredAction']}" if v.get("requiredAction") else ""),
                         url=f"https://nvd.nist.gov/vuln/detail/{v.get('cveID')}", starts=v.get("dateAdded") or "",
                         where_text="your " + ", ".join(mine) if mine else ""))
    out.sort(key=lambda x: x["starts"], reverse=True)  # newest first
    return [x for x in out if x["tier"] == "priority"] + [x for x in out if x["tier"] != "priority"][:10]


IMPORTANT_SECTORS = ("health", "public", "government", "energy", "utilit", "telecom", "water", "financ", "education")


def fetch_ransomware(sites, get=_get, prefs=None, days=3):
    prefs = prefs or DEFAULT_PREFS
    data = get("https://api.ransomware.live/v2/recentvictims")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    words = [w.strip().lower() for w in prefs.get("local_orgs", []) if w.strip()]
    out = []
    for v in data if isinstance(data, list) else []:
        if v.get("country") != "US" or (v.get("discovered") or v.get("attackdate") or "") < since[:10]:
            continue
        hay = f"{v.get('victim')} {v.get('domain')} {v.get('description')}".lower()
        local = [w for w in words if w in hay]
        sector = (v.get("activity") or "").lower()
        if local:
            tier = "priority"
        elif any(s in sector for s in IMPORTANT_SECTORS):
            tier = "routine"
        else:
            continue
        name = re.sub(r"\s+", " ", v.get("victim") or "An organisation")[:120]
        out.append(_item("ransomware", f"{v.get('group')}-{name}", "cyber", tier,
                         f"{name} listed by the {v.get('group')} ransomware group",
                         summary=f"Sector: {v.get('activity') or 'unknown'}. Listed on a leak site; the claim is the group's, not confirmed."
                         + (f" Matches your watch list: {', '.join(local)}." if local else ""),
                         starts=v.get("discovered") or v.get("attackdate") or "", trust="reported",
                         url="https://www.ransomware.live/", where_text=v.get("domain") or ""))
    return out


def fetch_internet(sites, get=_get, prefs=None):
    prefs = prefs or DEFAULT_PREFS
    until = int(datetime.now(timezone.utc).timestamp())
    base = "https://api.ioda.inetintel.cc.gatech.edu/v2/outages/alerts?"
    out, seen = [], set()
    for etype in ("region", "country"):
        data = get(base + urllib.parse.urlencode({"from": until - 6 * 3600, "until": until, "entityType": etype}))
        for a in data.get("data", []) if isinstance(data, dict) else []:
            if a.get("level") not in ("warning", "critical"):
                continue
            ent = a.get("entity") or {}
            attrs = ent.get("attrs") or {}
            cc = attrs.get("country_code") or ent.get("code")
            name = ent.get("name") or "?"
            if etype == "region" and cc != "US":
                continue
            key = f"{etype}-{ent.get('code')}"
            if key in seen:
                continue
            seen.add(key)
            ours = etype == "region" and name.lower() == (prefs.get("state") or "").lower()
            tier = "priority" if ours or (etype == "country" and cc == "US" and a["level"] == "critical") else "routine"
            if tier == "routine" and a["level"] != "critical":
                continue
            where = f"{name}, US" if etype == "region" else name
            out.append(_item("ioda", f"{key}-{datetime.now(timezone.utc).date()}", "internet", tier,
                             f"Internet outage: {where}", summary=f"{a['level'].capitalize()} drop in {a.get('datasource')} signals "
                             f"({a.get('value')} vs normal {a.get('historyValue')}).", where_text=where, trust="confirmed",
                             url=f"https://ioda.inetintel.cc.gatech.edu/{etype}/{ent.get('code')}",
                             starts=datetime.fromtimestamp(a.get("time") or until, timezone.utc).isoformat(timespec="seconds")))
    return out


def fetch_declarations(sites, get=_get, prefs=None, days=30):
    states = sorted({(s.get("state") or "").upper() for s in sites if s.get("state")}) or ["CO"]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    out = []
    for st in states[:5]:
        q = urllib.parse.urlencode({"$filter": f"state eq '{st}' and declarationDate ge '{since}'", "$orderby": "declarationDate desc", "$top": "20"})
        data = get("https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries?" + q)
        seen = set()
        for d in data.get("DisasterDeclarationsSummaries", []):
            if d.get("disasterNumber") in seen:
                continue
            seen.add(d.get("disasterNumber"))
            out.append(_item("fema", f"{d.get('disasterNumber')}", "declaration", "priority",
                             f"Federal disaster declared in {st}: {d.get('declarationTitle')}",
                             summary=f"{d.get('incidentType')} · {d.get('designatedArea')}", starts=d.get("declarationDate") or "",
                             url=f"https://www.fema.gov/disaster/{d.get('disasterNumber')}", where_text=st,
                             scenario=scenario_for(d.get("incidentType"))))
    return out


def _box(sites, km_):
    pts = [(s["lat"], s["lon"]) for s in sites if s.get("lat") is not None and s.get("lon") is not None]
    if not pts:
        return None
    dlat = km_ / 111.0
    dlon = max(km_ / (111.0 * max(math.cos(math.radians(lat)), 0.2)) for lat, _ in pts)
    return (round(min(p[1] for p in pts) - dlon, 3), round(min(p[0] for p in pts) - dlat, 3),
            round(max(p[1] for p in pts) + dlon, 3), round(max(p[0] for p in pts) + dlat, 3))


FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{sat}/{box}/{days}"
FIRMS_SATS = ("VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT")


def fetch_firms(sites, get=_get, keys=None, text=_get_text, days=3):
    """NASA FIRMS satellite heat detections near our places (needs a free map key).

    Detections within ~2 km of each other are one item, so one fire is one item. A spot that is hot on every one of the last
    few days is almost always a refinery, power plant or gas flare, so it is labelled that way and never more than
    "good to know". Satellite hot spots are never "Act now": they are unconfirmed.
    """
    key = (keys or {}).get("firms")
    box = _box(sites, 60)
    if not key or not box:
        return []
    import csv
    import io
    cells = {}
    for sat in FIRMS_SATS:
        body = text(FIRMS_URL.format(key=urllib.parse.quote(key), sat=sat, box=",".join(map(str, box)), days=days))
        if not body.lstrip().lower().startswith("latitude"):
            raise RuntimeError(body.strip().splitlines()[0][:120] if body.strip() else "empty reply")
        for r in csv.DictReader(io.StringIO(body)):
            if (r.get("confidence") or "").lower() in ("l", "low"):
                continue
            try:
                lat, lon = float(r["latitude"]), float(r["longitude"])
                frp = float(r.get("frp") or 0)
            except (KeyError, ValueError):
                continue
            k = next((k for k, c in cells.items() if km(c["lat"], c["lon"], lat, lon) <= 2.0), None)
            if k is None:  # a new ~2 km group, named after where it was first seen so it keeps its id
                k = (round(lat / 0.02), round(lon / 0.02))
                cells[k] = {"lat": lat, "lon": lon, "n": 0, "frp": 0.0, "days": set(), "last": ""}
            c = cells[k]
            c["n"] += 1
            c["frp"] = max(c["frp"], frp)
            c["days"].add(r.get("acq_date") or "")
            t = f"{r.get('acq_date')}T{(r.get('acq_time') or '0000').zfill(4)[:2]}:{(r.get('acq_time') or '0000').zfill(4)[2:]}:00+00:00"
            c["last"] = max(c["last"], t)
    out = []
    for (gy, gx), c in cells.items():
        site, d = nearest(sites, c["lat"], c["lon"])
        if d is None or d > 50:
            continue
        steady = len(c["days"]) >= days
        tier = "priority" if d <= 10 and not steady else "routine"
        title = (f"Steady heat source {d} km from {site} (likely industrial, not a fire)" if steady else
                 f"Satellite hot spot {d} km from {site}")
        out.append(_item("firms", f"{gy}_{gx}", "fire", tier, title, lat=c["lat"], lon=c["lon"], site=site, km=d,
                         summary=f"{c['n']} detection{'s' if c['n'] != 1 else ''} over {len(c['days'])} day(s), strongest {c['frp']:.0f} MW. "
                                 "Satellites see heat, not flames: check Watch Duty or local news before acting.",
                         starts=c["last"], trust="satellite", scenario="" if steady else "wildfire_smoke",
                         url=f"https://firms.modaps.eosdis.nasa.gov/map/#d:24hrs;@{c['lon']:.3f},{c['lat']:.3f},12z"))
    out.sort(key=lambda x: x["km"])
    return out[:40]


RADAR_URL = "https://api.cloudflare.com/client/v4/radar/annotations/outages?dateRange=7d&limit=100&format=json"


def fetch_radar(sites, get=_get, keys=None, prefs=None, now_=None):
    """Cloudflare Radar outage reports (needs a free API token): ones in the US or naming our state or providers."""
    token = (keys or {}).get("radar")
    if not token:
        return []
    prefs = prefs or DEFAULT_PREFS
    data = get(RADAR_URL, headers={"Authorization": f"Bearer {token}"})
    if not data.get("success", True):
        raise RuntimeError("; ".join(e.get("message", "") for e in data.get("errors", []))[:150] or "Radar refused")
    since = ((now_ or datetime.now(timezone.utc)) - timedelta(days=2)).isoformat()
    words = [w.lower() for w in [prefs.get("state") or ""] + list(prefs.get("local_orgs") or []) + list(prefs.get("devices") or []) if w]
    out = []
    for a in (data.get("result") or {}).get("annotations", []):
        ongoing = not a.get("endDate")
        if not ongoing and (a.get("endDate") or "") < since:
            continue
        locs = [x.get("code") for x in a.get("locationsDetails") or []] or list(a.get("locations") or [])
        names = ", ".join(x.get("name") or x.get("code") or "" for x in a.get("locationsDetails") or []) or ", ".join(locs)
        asns = ", ".join(x.get("name") or str(x.get("asn")) for x in a.get("asnsDetails") or [])
        hay = f"{a.get('scope')} {asns} {a.get('description')}".lower()
        mine = [w for w in words if w in hay]
        otype = (a.get("outage") or {}).get("outageType") or ""
        if "US" in locs and mine:
            tier = "priority"
        elif "US" in locs or otype == "NATIONWIDE":
            tier = "routine"
        else:
            continue
        cause = (a.get("outage") or {}).get("outageCause") or ""
        where = a.get("scope") or names
        out.append(_item("radar", str(a.get("id") or f"{where}-{a.get('startDate')}"), "internet", tier,
                         f"Internet outage: {where}" + (f" ({asns})" if asns and asns not in where else "") + ("" if ongoing else " (over)"),
                         summary=" ".join(x for x in [(a.get("description") or "").strip(), f"Cause: {cause.replace('_', ' ').lower()}." if cause else "",
                                                       f"Matches your watch list: {', '.join(mine)}." if mine else ""] if x),
                         where_text=names, starts=a.get("startDate") or "", ends=a.get("endDate") or "", trust="confirmed",
                         url=a.get("linkedUrl") or "https://radar.cloudflare.com/outage-center"))
    return out


SOURCES = {
    "usgs": ("Earthquakes (USGS)", fetch_quakes),
    "gdacs": ("Disasters worldwide (GDACS)", fetch_gdacs),
    "wfigs": ("Colorado wildfires (NIFC)", fetch_fires),
    "swpc": ("Space weather (NOAA)", fetch_space),
    "kev": ("Exploited flaws (CISA)", fetch_kev),
    "ransomware": ("Ransomware leak sites", fetch_ransomware),
    "ioda": ("Internet outages (IODA)", fetch_internet),
    "fema": ("Disaster declarations (FEMA)", fetch_declarations),
    "firms": ("Satellite fire hot spots (NASA FIRMS)", fetch_firms),
    "radar": ("Internet outages (Cloudflare Radar)", fetch_radar),
}
# sources that need a free key; the key lives in a file next to the database, never in the database or in sync
KEYED = {"firms": ("firms.key", "NASA FIRMS map key", "https://firms.modaps.eosdis.nasa.gov/api/map_key/"),
         "radar": ("cloudflare-radar.token", "Cloudflare API token with Radar read access",
                   "https://dash.cloudflare.com/profile/api-tokens")}


def load_keys(folder):
    out = {}
    for k, (name, _, _) in KEYED.items():
        try:
            v = (Path(folder) / name).read_text().strip()
        except OSError:
            continue
        if v:
            out[k] = v
    return out


def save_key(folder, source, value):
    """Write (or with an empty value, remove) a source key, readable only by BUNKR."""
    path = Path(folder) / KEYED[source][0]
    value = (value or "").strip()
    if not value:
        path.unlink(missing_ok=True)
        return False
    if not re.fullmatch(r"[A-Za-z0-9_\-.]{8,200}", value):
        raise ValueError("that doesn't look like a key (letters, numbers, - and _ only)")
    path.touch(mode=0o600)
    os.chmod(path, 0o600)
    path.write_text(value + "\n")
    return True
SOURCE_NAMES = {"nws": "Weather alerts (NWS)", **{k: v[0] for k, v in SOURCES.items()}}


# ---------- storage ----------
FIELDS = ("uid", "source", "category", "tier", "title", "summary", "where_text", "lat", "lon", "site", "km", "url",
          "starts", "ends", "scenario", "trust", "geom")


def prefs(store):
    p = dict(DEFAULT_PREFS)
    p.update(store.get_raw("situation_prefs", {}) or {})
    return p


def save(store, source, items):
    """Upsert this source's items; ones it no longer reports become inactive. Returns the newly seen items."""
    t = now()
    fresh = []
    with store._lock:
        conn = store._conn
        keep = set()
        for it in items:
            keep.add(it["uid"])
            row = conn.execute("SELECT active, tier FROM situation WHERE uid=?", (it["uid"],)).fetchone()
            vals = [it.get(f) for f in FIELDS]
            if row is None:
                conn.execute(f"INSERT INTO situation({', '.join(FIELDS)}, first_seen, last_seen, updated_at, active) "
                             f"VALUES({', '.join('?' for _ in FIELDS)}, ?, ?, ?, 1)", (*vals, t, t, t))
                fresh.append(it)
            else:
                conn.execute(f"UPDATE situation SET {', '.join(f'{f}=?' for f in FIELDS[1:])}, last_seen=?, updated_at=?, active=1 "
                             "WHERE uid=?", (*vals[1:], t, t, it["uid"]))
                if not row["active"] or TIER_ORDER.get(it["tier"], 9) < TIER_ORDER.get(row["tier"], 9):
                    fresh.append(it)  # back again, or got worse
        for row in conn.execute("SELECT uid FROM situation WHERE source=? AND active=1", (source,)).fetchall():
            if row["uid"] not in keep:
                conn.execute("UPDATE situation SET active=0, updated_at=? WHERE uid=?", (t, row["uid"]))
        conn.commit()
    return fresh


def refresh(store, sites, get=_get, only=None, keys=None):
    """Fetch every source (or `only` these), record per-source status, tell listeners about new items."""
    status = store.get_raw("situation_sources", {}) or {}
    p = prefs(store)
    new = []
    new += save(store, "nws", from_nws(sites))
    status["nws"] = {"ok": True, "at": now(), "count": sum(len(s.get("alerts") or []) for s in sites)}
    for key, (_, fn) in SOURCES.items():
        if only and key not in only:
            continue
        if key in KEYED and not (keys or {}).get(key):
            status[key] = {"ok": None, "needs_key": True}
            continue
        try:
            kw = {"prefs": p} if "prefs" in fn.__code__.co_varnames else {}
            if "keys" in fn.__code__.co_varnames:
                kw["keys"] = keys
            items = fn(sites, get=get, **kw)
            new += save(store, key, items)
            status[key] = {"ok": True, "at": now(), "count": len(items)}
        except Exception as exc:  # keep the last good items; show the error and its time
            prev = status.get(key) or {}
            status[key] = {"ok": False, "at": prev.get("at", ""), "error": f"{type(exc).__name__}: {exc}"[:200],
                           "tried": now(), "count": prev.get("count", 0)}
    store.set_raw("situation_sources", status)
    for it in new:
        for fn in list(listeners):
            try:
                fn("situation", it)
            except Exception:
                pass
    return new


def items(store, include_inactive=False):
    rows = store._rows("SELECT * FROM situation" + ("" if include_inactive else " WHERE active=1"))
    rows.sort(key=lambda r: (TIER_ORDER.get(r["tier"], 9), r["km"] if r["km"] is not None else 99999, r["starts"] or ""))
    return rows


def view(store):
    st = store.get_raw("situation_sources", {}) or {}
    return {"items": items(store), "tiers": TIERS, "trust": TRUST, "categories": CATEGORIES, "prefs": prefs(store),
            "sources": [{"key": k, "name": n, **(st.get(k) or {}), **({"key_label": KEYED[k][1], "key_url": KEYED[k][2]} if k in KEYED else {})}
                        for k, n in SOURCE_NAMES.items()]}


def geo(store, sites):
    """GeoJSON for the map: our places, then every located item (warning areas as outlines)."""
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
              "properties": {"kind": "site", "title": s["name"], "site_kind": s.get("kind") or ""}}
             for s in sites if s.get("lat") is not None and s.get("lon") is not None]
    for r in items(store):
        props = {"kind": "item", "uid": r["uid"], "title": r["title"], "tier": r["tier"], "category": r["category"],
                 "trust": TRUST.get(r["trust"], r["trust"]), "site": r["site"], "km": r["km"], "url": r["url"],
                 "summary": (r["summary"] or "")[:300], "scenario": r["scenario"], "starts": r["starts"]}
        if r.get("geom"):
            try:
                feats.append({"type": "Feature", "geometry": json.loads(r["geom"]), "properties": props})
                continue
            except ValueError:
                pass
        if r["lat"] is not None and r["lon"] is not None:
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]}, "properties": props})
    return {"type": "FeatureCollection", "features": feats}


# ---------- sync (hub -> nodes only) ----------
SYNC_FIELDS = FIELDS + ("first_seen", "last_seen", "updated_at", "active")


def export(store, since=""):
    return {"situation": [{f: r[f] for f in SYNC_FIELDS} for r in
                          store._rows("SELECT * FROM situation WHERE updated_at > ?", (since,))],
            "situation_sources": store.get_raw("situation_sources", {}) or {}}


def apply(store, incoming):
    rows = incoming.get("situation") or []
    with store._lock:
        conn = store._conn
        for r in rows:
            if not r.get("uid"):
                continue
            vals = {f: r.get(f) for f in SYNC_FIELDS}
            conn.execute(f"INSERT INTO situation({', '.join(vals)}) VALUES({', '.join('?' for _ in vals)}) "
                         f"ON CONFLICT(uid) DO UPDATE SET {', '.join(f'{f}=excluded.{f}' for f in vals if f != 'uid')}",
                         tuple(vals.values()))
        conn.commit()
    if isinstance(incoming.get("situation_sources"), dict) and incoming["situation_sources"]:
        store.set_raw("situation_sources", incoming["situation_sources"])
    return len(rows)
