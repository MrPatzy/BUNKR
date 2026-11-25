"""Saved forecasts, so there's still weather when the internet is down.

Every 3 hours the hub saves the weather service's 7-day forecast (14 day and night periods) and the next 72 hours,
for home and every place on the Sites tab. When a live lookup fails, the Home tile, the mesh bot's WX and the daily
mesh brief answer from the saved copy, saying how old it is and leaving out what has already passed: after a 2-day
outage there are still 5 days left. Field nodes get the saved forecasts with the rest of their sync.

Stored as one setting, `wx_saved` = {"<lat>,<lon>": {name, kind, lat, lon, at, periods: [...], hourly: [...]}}.
"""

import math
from datetime import datetime, timezone

from . import wx

EVERY = 3 * 3600          # how often the hub saves them
NEAR_KM = 15              # a saved forecast answers for anywhere this close
KEEP_FIELDS = ("name", "startTime", "endTime", "isDaytime", "temperature", "windSpeed", "windDirection", "shortForecast")
HOURS = 72


def _now():
    return datetime.now(timezone.utc)


def _key(lat, lon):
    return f"{round(lat, 2)},{round(lon, 2)}"


def _slim(p):
    out = {k: p.get(k) for k in KEEP_FIELDS if p.get(k) is not None}
    pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
    if pop is not None:
        out["probabilityOfPrecipitation"] = {"value": pop}
    return out


def saved(store):
    return store.get_raw("wx_saved", {}) or {}


def save_all(store, sites, get=wx._get, when=None):
    """Fetch and save the forecast for each place with coordinates. A place that fails keeps its last saved copy."""
    when = when or _now()
    snaps, status = saved(store), {}
    seen = set()
    for s in sites:
        if s.get("lat") is None or s.get("lon") is None:
            continue
        k = _key(s["lat"], s["lon"])
        if k in seen:
            continue
        seen.add(k)
        try:
            props = wx.point(s["lat"], s["lon"], get)
            periods = [_slim(p) for p in wx.periods(props, get)]
            hourly = []
            if props.get("forecastHourly"):
                try:
                    hourly = [_slim(p) for p in ((get(props["forecastHourly"]).get("properties") or {}).get("periods") or [])[:HOURS]]
                except Exception:
                    hourly = (snaps.get(k) or {}).get("hourly") or []
            if not periods:
                raise ValueError("no forecast periods")
            snaps[k] = {"name": s.get("name") or "", "kind": s.get("kind") or "", "lat": s["lat"], "lon": s["lon"],
                        "at": when.isoformat(timespec="seconds"), "periods": periods, "hourly": hourly}
            status[s.get("name") or k] = {"ok": True}
        except Exception as exc:
            status[s.get("name") or k] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160]}
    store.set_raw("wx_saved", snaps)
    store.set_raw("wx_saved_status", {"at": when.isoformat(timespec="seconds"), "places": status})
    return status


def _km(a_lat, a_lon, b_lat, b_lon):
    r = math.radians
    d = (math.sin(r(b_lat - a_lat) / 2) ** 2 + math.cos(r(a_lat)) * math.cos(r(b_lat)) * math.sin(r(b_lon - a_lon) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(d))


def near(store, lat, lon, max_km=NEAR_KM):
    """The saved forecast closest to a place, if one is within `max_km`."""
    best, dist = None, max_km
    for snap in saved(store).values():
        d = _km(lat, lon, snap["lat"], snap["lon"])
        if d <= dist:
            best, dist = snap, d
    return best


def remaining(periods, when=None):
    """The periods that haven't ended yet."""
    when = when or _now()
    out = []
    for p in periods:
        try:
            if datetime.fromisoformat(p["endTime"]) <= when:
                continue
        except (KeyError, TypeError, ValueError):
            pass
        out.append(p)
    return out


def age(snap, when=None):
    """'40 min ago', '9 h ago', '2 days ago'."""
    when = when or _now()
    try:
        mins = (when - datetime.fromisoformat(snap["at"])).total_seconds() / 60
    except (KeyError, TypeError, ValueError):
        return "some time ago"
    if mins < 60:
        return f"{max(1, round(mins))} min ago"
    if mins < 48 * 60:
        return f"{round(mins / 60)} h ago"
    return f"{round(mins / 1440)} days ago"


def _fake(snap, when=None):
    """Hand the saved periods to wx's own formatting, as if they came from the weather service."""
    ps = remaining(snap["periods"], when)
    get = lambda url: {"properties": {"periods": ps}} if url == "saved:forecast" else {"features": []}  # noqa: E731
    return {"forecast": "saved:forecast", "observationStations": "saved:stations"}, get, ps


def now_and_next(snap, label, when=None):
    props, get, ps = _fake(snap, when)
    if not ps:
        return ""
    return wx.now_and_next(label, props, get)[:-1] + f" (saved {age(snap, when)})."


def days(snap, label, n, when=None):
    props, get, ps = _fake(snap, when)
    return wx.days(label, props, n, get) if ps else []


def home_view(store, when=None):
    """What the Home tile shows: the saved forecast for home, whatever the internet is doing."""
    when = when or _now()
    home = next((s for s in store.sites() if s["kind"] == "home" and s.get("lat") is not None), None)
    snap = near(store, home["lat"], home["lon"]) if home else None
    if not snap:
        return None
    ps = remaining(snap["periods"], when)
    if not ps:
        return {"at": snap["at"], "age": age(snap, when), "periods": [], "days": [], "stale": True}
    return {"at": snap["at"], "age": age(snap, when), "stale": (when - datetime.fromisoformat(snap["at"])).total_seconds() > 2 * EVERY,
            "periods": [{"name": p.get("name"), "temp": p.get("temperature"), "text": p.get("shortForecast"),
                         "pop": (p.get("probabilityOfPrecipitation") or {}).get("value"), "wind": wx._wind(p)} for p in ps[:2]],
            "days": days(snap, "", 7, when)}


# ---------- field nodes get them with the rest of their sync ----------
def export(store, since=""):
    snaps = saved(store)
    return {"wx_saved": [s for s in snaps.values() if s.get("at", "") > (since or "")]}


def apply(store, incoming):
    snaps, n = saved(store), 0
    for s in incoming.get("wx_saved") or []:
        try:
            k = _key(float(s["lat"]), float(s["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if s.get("at", "") > (snaps.get(k) or {}).get("at", ""):
            snaps[k] = s
            n += 1
    if n:
        store.set_raw("wx_saved", snaps)
    return n
