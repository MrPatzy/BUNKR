"""Sites (home, bug-out sites, relatives): geocoding and National Weather Service alerts.

Geocoding uses the U.S. Census Bureau geocoder, which also returns the county,
so the FEMA hazard profile can be looked up without a second service.
Alerts come from api.weather.gov and are cached, so offline nodes show them "as of".
"""

import json
import urllib.parse
import urllib.request

CENSUS = "https://geocoding.geo.census.gov/geocoder/geographies"
NWS_ALERTS = "https://api.weather.gov/alerts/active"
# NWS asks for an identifying User-Agent; a generic app name keeps the household anonymous.
USER_AGENT = "BUNKR/0.2 (self-hosted preparedness app)"
BENCH = {"benchmark": "Public_AR_Current", "vintage": "Current_Current", "layers": "Counties", "format": "json"}

SEVERITY_ORDER = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3, "Unknown": 4}


def _get_json(url, timeout=30, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _county(geos):
    c = (geos.get("Counties") or [{}])[0]
    return {"county_fips": c.get("GEOID", ""), "county_name": c.get("NAME", ""),
            "state_fips": c.get("STATE", "")}


def geocode(address):
    """One-line U.S. address -> {lat, lon, matched, county_fips, county_name}."""
    q = urllib.parse.urlencode({"address": address, **BENCH})
    data = _get_json(f"{CENSUS}/onelineaddress?{q}")
    matches = data.get("result", {}).get("addressMatches") or []
    if not matches:
        raise LookupError("the Census geocoder found no match; check the address or enter coordinates")
    m = matches[0]
    return {"lat": m["coordinates"]["y"], "lon": m["coordinates"]["x"], "matched": m["matchedAddress"],
            "state": m.get("addressComponents", {}).get("state", ""), **_county(m.get("geographies", {}))}


def county_for(lat, lon):
    q = urllib.parse.urlencode({"x": lon, "y": lat, **BENCH})
    data = _get_json(f"{CENSUS}/coordinates?{q}")
    out = _county(data.get("result", {}).get("geographies", {}))
    if not out["county_fips"]:
        raise LookupError("no U.S. county at those coordinates")
    return out


def simplify(geom, max_points=120):
    """A warning area's outline, rounded and thinned so it stays small in the database and in sync. None if absent."""
    if not isinstance(geom, dict) or geom.get("type") not in ("Polygon", "MultiPolygon"):
        return None

    def ring(r):
        step = max(1, len(r) // max_points)
        pts = [[round(x, 3), round(y, 3)] for x, y, *_ in r[::step]]
        if pts and pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts if len(pts) >= 4 else None

    def poly(rings):
        out = [x for x in (ring(r) for r in rings[:1]) if x]  # the outer edge is enough for a map
        return out or None

    if geom["type"] == "Polygon":
        c = poly(geom.get("coordinates") or [])
        return {"type": "Polygon", "coordinates": c} if c else None
    polys = [x for x in (poly(pp) for pp in (geom.get("coordinates") or [])[:20]) if x]
    return {"type": "MultiPolygon", "coordinates": polys} if polys else None


def fetch_alerts(lat, lon):
    """Active NWS alerts for a point, most severe first, trimmed to what the UI shows."""
    q = urllib.parse.urlencode({"point": f"{lat:.4f},{lon:.4f}"})
    data = _get_json(f"{NWS_ALERTS}?{q}", accept="application/geo+json")
    out = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        out.append({k: p.get(k) or "" for k in ("id", "event", "severity", "urgency", "certainty", "headline",
                                                "areaDesc", "onset", "ends", "expires", "instruction",
                                                "senderName")}
                   | {"description": (p.get("description") or "")[:1500], "geometry": simplify(f.get("geometry"))})
    out.sort(key=lambda a: (SEVERITY_ORDER.get(a["severity"], 9), a["onset"]))
    return out


def refresh_site(store, site, hazards_mod, with_hazards=False):
    """Fetch alerts (and optionally hazards) for one site, storing errors instead of raising."""
    if site.get("lat") is None or site.get("lon") is None:
        return
    try:
        store.set_site_alerts(site["id"], fetch_alerts(site["lat"], site["lon"]))
    except Exception as exc:
        store.set_site_alerts(site["id"], None, error=f"{type(exc).__name__}: {exc}"[:300])
    if with_hazards and site.get("county_fips"):
        try:
            store.set_site_hazards(site["id"], hazards_mod.fetch_county(site["county_fips"]))
        except Exception:
            pass  # keep the previous profile; the UI shows its date


def all_alerts(sites):
    """Alerts across sites for the dashboard, each tagged with its site."""
    out = []
    for s in sites:
        for a in s.get("alerts", []):
            out.append({**a, "site": s["name"], "site_id": s["id"], "fetched": s.get("alerts_at", "")})
    out.sort(key=lambda a: (SEVERITY_ORDER.get(a["severity"], 9), a["site"]))
    return out
