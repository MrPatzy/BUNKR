"""A home weather station: readings in, the Home weather tile, and warnings that need no internet.

A station sends readings to /station/<key> (the key is made on Sites & maps, "Weather station"; it's the only thing
that lets a reading in, since a station can't log in). Three ways in, all on the same address:

- BUNKR's own JSON, for a station you build: {"temp_f": 41.2, "humidity": 63, "pressure_hpa": 1012.4, "wind_mph": 8,
  "gust_mph": 15, "wind_dir": 315, "rain_day_in": 0.12, "rain_rate_in": 0.0}  (or temp_c, pressure_inhg)
- Weather Underground's upload format (tempf, humidity, baromin, windspeedmph, windgustmph, winddir, dailyrainin,
  rainin), as a query string or a form: most stations can send this to a custom server.
- Ecowitt's "customized" upload (tempf, baromrelin, rainratein...), the Ecowitt protocol posting a form.

From the readings BUNKR raises its own warnings, as Situation items (so they reach the bell, phones and the mesh like
any alert, and clear themselves when the reading does): pressure falling fast, strong gusts, heavy rain, extreme heat;
and a freeze as "good to know". Readings are kept a week, at most one a minute (table station_readings).
"""

import json
import secrets
from datetime import datetime, timedelta, timezone

from . import situation

KEEP_DAYS = 8
FRESH_MIN = 30            # a reading older than this isn't "now"
FALL_HPA = 3.0            # in 3 hours: the classic "a storm is coming" drop
FALL_FAST_HPA = 6.0
GUST_MPH = 45
RAIN_RATE_IN = 1.0        # inches an hour
RAIN_DAY_IN = 2.0
HEAT_F = 100
FREEZE_F = 32
HPA_PER_INHG = 33.8639

# incoming name -> (our name, how to convert)
FIELDS = {
    "temp_f": ("temp_f", float), "tempf": ("temp_f", float),
    "temp_c": ("temp_f", lambda v: float(v) * 9 / 5 + 32), "tempc": ("temp_f", lambda v: float(v) * 9 / 5 + 32),
    "humidity": ("humidity", float),
    "pressure_hpa": ("pressure_hpa", float), "pressure_mb": ("pressure_hpa", float),
    "pressure_inhg": ("pressure_hpa", lambda v: float(v) * HPA_PER_INHG), "baromin": ("pressure_hpa", lambda v: float(v) * HPA_PER_INHG),
    "baromrelin": ("pressure_hpa", lambda v: float(v) * HPA_PER_INHG),
    "wind_mph": ("wind_mph", float), "windspeedmph": ("wind_mph", float),
    "gust_mph": ("gust_mph", float), "windgustmph": ("gust_mph", float),
    "wind_dir": ("wind_dir", float), "winddir": ("wind_dir", float),
    "rain_day_in": ("rain_day_in", float), "dailyrainin": ("rain_day_in", float),
    "rain_rate_in": ("rain_rate_in", float), "rainratein": ("rain_rate_in", float), "rainin": ("rain_rate_in", float),
}
RANGES = {"temp_f": (-80, 150), "humidity": (0, 100), "pressure_hpa": (850, 1090), "wind_mph": (0, 250), "gust_mph": (0, 300),
          "wind_dir": (0, 360), "rain_day_in": (0, 60), "rain_rate_in": (0, 30)}
DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _now():
    return datetime.now(timezone.utc)


def _iso(t):
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------- setup ----------
def settings(store):
    return store.get_raw("station", {}) or {}


def new_key(store, name="Home weather station"):
    s = {"key": secrets.token_urlsafe(18), "name": str(name or "Home weather station")[:60], "since": _iso(_now())}
    store.set_raw("station", s)
    return s


def forget(store):
    store.set_raw("station", {})


def key_ok(store, key):
    k = settings(store).get("key") or ""
    return bool(k) and secrets.compare_digest(k, str(key or ""))


# ---------- readings ----------
def normalize(params, when=None):
    """A reading from any of the three formats, converted, range-checked; unknown or bad values are left out."""
    out = {}
    for k, v in (params or {}).items():
        k = str(k).lower()
        if k not in FIELDS or v in (None, "", "-9999", -9999):
            continue
        name, conv = FIELDS[k]
        try:
            x = conv(v[0] if isinstance(v, list) else v)
        except (TypeError, ValueError):
            continue
        lo, hi = RANGES[name]
        if lo <= x <= hi and name not in out:
            out[name] = round(x, 2)
    if not out:
        raise ValueError("no readings BUNKR knows (temp_f, humidity, pressure_hpa, wind_mph, ...)")
    out["at"] = _iso(when or _now())
    return out


def record(store, reading):
    """Keep it (one a minute at most) and drop readings older than a week."""
    minute = reading["at"][:16]
    store._exec("INSERT INTO station_readings(at, data) VALUES(?, ?) ON CONFLICT(at) DO UPDATE SET data=excluded.data",
                (minute, json.dumps(reading)))
    cutoff = _iso(datetime.fromisoformat(reading["at"]) - timedelta(days=KEEP_DAYS))[:16]
    store._exec("DELETE FROM station_readings WHERE at < ?", (cutoff,))


def history(store, hours=24, when=None):
    since = _iso((when or _now()) - timedelta(hours=hours))[:16]
    return [json.loads(r["data"]) for r in store._rows("SELECT data FROM station_readings WHERE at >= ? ORDER BY at", (since,))]


def latest(store):
    rows = store._rows("SELECT data FROM station_readings ORDER BY at DESC LIMIT 1")
    return json.loads(rows[0]["data"]) if rows else None


def _ago_min(r, when):
    return (when - datetime.fromisoformat(r["at"])).total_seconds() / 60


def pressure_change(store, when=None, hours=3):
    """hPa over the last `hours` (negative = falling), or None without readings near both ends."""
    when = when or _now()
    hist = [r for r in history(store, hours + 1, when) if "pressure_hpa" in r]
    if not hist or _ago_min(hist[-1], when) > FRESH_MIN:
        return None
    target = when - timedelta(hours=hours)
    old = min(hist, key=lambda r: abs((datetime.fromisoformat(r["at"]) - target).total_seconds()))
    if abs((datetime.fromisoformat(old["at"]) - target).total_seconds()) > 40 * 60:
        return None
    return round(hist[-1]["pressure_hpa"] - old["pressure_hpa"], 1)


def now_view(store, when=None):
    """The latest reading for the Home tile, if it's fresh: values plus the pressure trend."""
    when = when or _now()
    r = latest(store)
    if not r or _ago_min(r, when) > FRESH_MIN:
        return None
    out = dict(r)
    if "wind_dir" in r:
        out["wind_from"] = DIRS[int((r["wind_dir"] + 22.5) % 360 // 45)]
    ch = pressure_change(store, when)
    if ch is not None:
        out["pressure_3h"] = ch
        out["pressure_trend"] = "falling fast" if ch <= -FALL_HPA else "falling" if ch <= -1 else "rising" if ch >= 1 else "steady"
    out["name"] = settings(store).get("name") or "Home weather station"
    return out


# ---------- warnings from the station itself ----------
def warnings(store, home=None, when=None):
    """Situation items for what the readings say right now ([] when all is calm or the station is quiet)."""
    when = when or _now()
    r = latest(store)
    if not r or _ago_min(r, when) > FRESH_MIN:
        return []
    home = home or {}
    day = when.date().isoformat()
    kw = {"site": home.get("name") or "Home", "km": 0, "lat": home.get("lat"), "lon": home.get("lon"),
          "where_text": "at home", "trust": "own station", "starts": r["at"]}
    out = []
    ch = pressure_change(store, when)
    if ch is not None and ch <= -FALL_HPA:
        out.append(situation._item("station", f"pressure:{day}", "weather", "priority",
                                   f"Pressure falling {'very ' if ch <= -FALL_FAST_HPA else ''}fast at home ({ch} hPa in 3 h)",
                                   summary="A drop like this usually means a storm or strong wind is on the way, even before any "
                                           "forecast says so. From the home weather station.", **kw))
    if r.get("gust_mph", 0) >= GUST_MPH:
        out.append(situation._item("station", f"gust:{day}", "weather", "priority", f"Gusts of {round(r['gust_mph'])} mph at home",
                                   summary="Secure loose things outside; watch for falling branches and power lines.", **kw))
    if r.get("rain_rate_in", 0) >= RAIN_RATE_IN or r.get("rain_day_in", 0) >= RAIN_DAY_IN:
        what = (f"{r['rain_rate_in']:g} in an hour" if r.get("rain_rate_in", 0) >= RAIN_RATE_IN else f"{r['rain_day_in']:g} in today")
        out.append(situation._item("station", f"rain:{day}", "weather", "priority", f"Heavy rain at home ({what})",
                                   summary="Watch low spots, basements and window wells; don't drive through water.",
                                   scenario="flood", **kw))
    if r.get("temp_f", -999) >= HEAT_F:
        out.append(situation._item("station", f"heat:{day}", "weather", "priority", f"{round(r['temp_f'])}°F at home",
                                   summary="Drink water, check on anyone without air conditioning, keep pets inside.",
                                   scenario="extreme_heat", **kw))
    if r.get("temp_f", 999) <= FREEZE_F:
        out.append(situation._item("station", f"freeze:{day}", "weather", "routine", f"Freezing at home ({round(r['temp_f'])}°F)",
                                   summary="Pets and plants in; drip faucets on outside walls in a hard freeze.", **kw))
    return out


def publish(store, home=None, when=None):
    """Save the warnings as Situation items and tell listeners about new ones (bell, phones, mesh)."""
    fresh = situation.save(store, "station", warnings(store, home, when))
    for it in fresh:
        for fn in list(situation.listeners):
            try:
                fn("situation", it)
            except Exception:
                pass
    return fresh
