"""Weather for a place, short enough for a mesh message: now, today and tonight, or a few days; and its active alerts.

All from the U.S. National Weather Service (api.weather.gov: free, no key, U.S. only). It answers by grid point,
so a place's coordinates are first turned into its forecast office and grid (kept for a day).
"""

import json
import re
import time
import urllib.request
from datetime import datetime, timezone

API = "https://api.weather.gov/"
USER_AGENT = "BUNKR family preparedness (weather for the mesh bot)"
_points = {}   # (lat, lon) rounded -> (when, properties)

# a forecast's words, shortened: a mesh message has about 120 characters
SHORT = [(r"\bthen\b", ">"), (r"\band\b", "&"), (r"\bslight chance\b", "sl chc"), (r"\bchance\b", "chc"),
         (r"\blikely\b", "lkly"), (r"\bpartly\b", "pt"), (r"\bmostly\b", "m"), (r"\bcloudy\b", "cldy"),
         (r"\bshowers\b", "shwrs"), (r"\bthunderstorms\b", "tstms"), (r"\bpatchy\b", "ptchy"), (r"\bfreezing\b", "frz"),
         (r"\bdrizzle\b", "drzl"), (r"\bisolated\b", "isol"), (r"\bscattered\b", "sct"), (r"\bblowing\b", "blwg"),
         (r"\bareas of\b", ""), (r"\bvery\b", "v"), (r"\blight\b", "lt"), (r"\bheavy\b", "hvy"), (r"\bsnow\b", "snow"),
         (r"\bwinds?\b", "wind"), (r"\bbecoming\b", ">"), (r"\bsunny\b", "sunny"), (r"\bclear\b", "clear")]
DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(5_000_000))


def short(text):
    t = (text or "").lower()
    for pat, rep in SHORT:
        t = re.sub(pat, rep, t)
    t = " ".join(t.split())
    return t[:1].upper() + t[1:]


def point(lat, lon, get=_get, now=None):
    """The weather service's grid point for a place (its forecast links, stations and time zone)."""
    key = (round(lat, 3), round(lon, 3))
    now = now if now is not None else time.monotonic()
    hit = _points.get(key)
    if hit and now - hit[0] < 86400:
        return hit[1]
    props = get(f"{API}points/{key[0]},{key[1]}").get("properties") or {}
    if not props.get("forecast"):
        raise LookupError("the weather service has no forecast there (U.S. only)")
    _points[key] = (now, props)
    return props


def current(props, get=_get):
    """The nearest station's latest reading: {"temp_f", "wind_mph", "wind_dir", "text"} (missing parts left out)."""
    stations = get(props["observationStations"]).get("features") or []
    for st in stations[:3]:
        sid = (st.get("properties") or {}).get("stationIdentifier")
        if not sid:
            continue
        try:
            p = get(f"{API}stations/{sid}/observations/latest").get("properties") or {}
        except Exception:
            continue
        t = (p.get("temperature") or {}).get("value")
        if t is None:
            continue
        out = {"temp_f": round(t * 9 / 5 + 32), "text": p.get("textDescription") or ""}
        w = (p.get("windSpeed") or {}).get("value")
        if w is not None:
            out["wind_mph"] = round(w * 0.621371)
        d = (p.get("windDirection") or {}).get("value")
        if d is not None:
            out["wind_dir"] = DIRS[int((d + 22.5) % 360 // 45)]
        g = (p.get("windGust") or {}).get("value")
        if g:
            out["gust_mph"] = round(g * 0.621371)
        return out
    return {}


def periods(props, get=_get):
    return (get(props["forecast"]).get("properties") or {}).get("periods") or []


def _wind(p):
    """'10 to 20 mph' + 'NW' -> 'NW10-20'."""
    nums = re.findall(r"\d+", p.get("windSpeed") or "")
    if not nums or max(int(n) for n in nums) < 10:
        return ""  # light winds aren't worth the characters
    return f"{p.get('windDirection') or ''}{'-'.join(nums)}"


def _pop(p):
    v = (p.get("probabilityOfPrecipitation") or {}).get("value")
    return f"{v}%" if v and v >= 10 else ""  # a few percent is noise


def _day(p):
    """'Mon' from the period's start (its name may be 'Thanksgiving Day')."""
    try:
        return datetime.fromisoformat(p["startTime"]).strftime("%a")
    except (KeyError, TypeError, ValueError):
        return (p.get("name") or "")[:3]


def now_and_next(label, props, get=_get):
    """'80202 Denver, CO: now 41F clear, NW12. Today: Sunny 52F. Tonight: M clear 28F 10%.'"""
    cur = {}
    try:
        cur = current(props, get)
    except Exception:
        pass
    ps = periods(props, get)[:2]
    bits = []
    if cur:
        wind = f", {cur.get('wind_dir', '')}{cur['wind_mph']}" if cur.get("wind_mph") else ""
        gust = f" g{cur['gust_mph']}" if cur.get("gust_mph") else ""
        bits.append(f"now {cur['temp_f']}F {short(cur.get('text')).lower()}{wind}{gust}".rstrip())
    for p in ps:
        extra = " ".join(x for x in (_wind(p), _pop(p)) if x)
        bits.append(f"{p.get('name')}: {short(p.get('shortForecast'))} {p.get('temperature')}F" + (f" {extra}" if extra else ""))
    return f"{label}: " + ". ".join(bits) + "."


def days(label, props, n, get=_get):
    """One line per day ('Mon 52/28 Sunny 10%'), for up to 7 days (all the weather service gives)."""
    ps = periods(props, get)
    rows, i = [], 0
    while i < len(ps) and len(rows) < n:
        p = ps[i]
        if p.get("isDaytime"):
            night = ps[i + 1] if i + 1 < len(ps) and not ps[i + 1].get("isDaytime") else None
            lo = f"/{night.get('temperature')}" if night else ""
            pop = max(((x.get("probabilityOfPrecipitation") or {}).get("value") or 0) for x in (p, night) if x)
            rows.append(f"{_day(p)} {p.get('temperature')}{lo} {short(p.get('shortForecast'))}" + (f" {pop}%" if pop >= 10 else ""))
            i += 2 if night else 1
        else:  # the forecast starts tonight
            rows.append(f"Tngt -/{p.get('temperature')} {short(p.get('shortForecast'))}" + (f" {_pop(p)}" if _pop(p) else ""))
            i += 1
    return rows


def alerts(lat, lon, get=_get, tz=None):
    """Active alerts at a point: [{"event", "until", "severity"}], most severe first."""
    feats = get(f"{API}alerts/active?point={round(lat, 4)},{round(lon, 4)}").get("features") or []
    order = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
    out, seen = [], set()
    for f in feats:
        p = f.get("properties") or {}
        ev = p.get("event") or ""
        if not ev or ev in seen:
            continue
        seen.add(ev)
        out.append({"event": ev, "until": until(p.get("ends") or p.get("expires"), tz), "severity": p.get("severity") or ""})
    out.sort(key=lambda a: order.get(a["severity"], 9))
    return out


def until(iso, tz=None):
    """'2026-11-24T20:00:00-07:00' -> 'Tue 8p' in the place's time zone."""
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    if tz:
        try:
            import zoneinfo
            t = t.astimezone(zoneinfo.ZoneInfo(tz))
        except Exception:
            pass
    h = t.hour % 12 or 12
    return f"{t.strftime('%a')} {h}{'' if not t.minute else ':%02d' % t.minute}{'a' if t.hour < 12 else 'p'}"


ABBR = [("Warning", "Wrn"), ("Watch", "Wch"), ("Advisory", "Adv"), ("Statement", "Stmt"), ("Weather", "Wx"),
        ("Winter", "Wntr"), ("Thunderstorm", "Tstm"), ("Special", "Spcl"), ("Hazardous", "Hzd")]


def alert_line(label, items):
    if not items:
        return f"{label}: no weather alerts."
    def ab(e):
        for a, b in ABBR:
            e = e.replace(a, b)
        return e
    return f"{label}: " + "; ".join(ab(a["event"]) + (f" til {a['until']}" if a["until"] else "") for a in items)


def utc_now():
    return datetime.now(timezone.utc)
