"""Sunrise, sunset, twilight and the moon's phase, worked out on the spot: no internet needed.

The sun uses NOAA's solar calculator equations (good to about a minute in the lower 48); the moon's phase uses
the mean synodic month from a known new moon (good to within a day or so, plenty for "how much light tonight").
"""

import math
from datetime import date, datetime, timedelta, timezone

SYNODIC = 29.530588853
NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
PHASES = [(1.0, "new"), (6.4, "wax cres"), (8.4, "1st qtr"), (13.8, "wax gib"), (15.8, "full"), (21.1, "wan gib"),
          (23.1, "last qtr"), (28.5, "wan cres"), (30, "new")]


def _sun_utc_minutes(day, lat, lon, zenith):
    """Minutes after UTC midnight of `day` for sunrise and sunset at this zenith angle (90.833 = the sun's rim at the
    horizon; 96 = civil twilight). (None, None) when the sun doesn't get there that day (polar day or night)."""
    jd = date.toordinal(day) + 1721424.5 + 0.5  # Julian day at noon UTC
    t = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t)) + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
         + math.sin(3 * mr) * 0.000289)
    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(app_long))))
    y = math.tan(math.radians(eps / 2)) ** 2
    l0r = math.radians(l0)
    eqtime = 4 * math.degrees(y * math.sin(2 * l0r) - 2 * e * math.sin(mr) + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
                              - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
    latr, dr = math.radians(lat), math.radians(decl)
    cos_ha = (math.cos(math.radians(zenith)) / (math.cos(latr) * math.cos(dr))) - math.tan(latr) * math.tan(dr)
    if cos_ha < -1 or cos_ha > 1:
        return None, None
    ha = math.degrees(math.acos(cos_ha))
    noon = 720 - 4 * lon - eqtime
    return noon - 4 * ha, noon + 4 * ha


def _at(day, minutes, tz):
    if minutes is None:
        return None
    return (datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(minutes=minutes)).astimezone(tz)


def sun(lat, lon, day, tz):
    """{"rise", "set", "dawn", "dusk"} as local datetimes (None in polar day or night), for `day` at the place."""
    rise, set_ = _sun_utc_minutes(day, lat, lon, 90.833)
    dawn, dusk = _sun_utc_minutes(day, lat, lon, 96.0)
    return {"rise": _at(day, rise, tz), "set": _at(day, set_, tz), "dawn": _at(day, dawn, tz), "dusk": _at(day, dusk, tz)}


def moon(when):
    """(name, percent lit) for a moment."""
    age = ((when - NEW_MOON).total_seconds() / 86400) % SYNODIC
    lit = round((1 - math.cos(2 * math.pi * age / SYNODIC)) / 2 * 100)
    name = next(n for limit, n in PHASES if age < limit)
    return name, lit


def clock(t):
    """6:58a"""
    if t is None:
        return "--"
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'a' if t.hour < 12 else 'p'}"


def line(label, lat, lon, tz, when=None):
    """'80202 Denver, CO Mon: rise 6:58a set 4:39p (9h41m). Light 6:29a-5:08p. Moon wax gib 78%'"""
    when = when or datetime.now(tz)
    local = when.astimezone(tz)
    s = sun(lat, lon, local.date(), tz)
    if s["rise"] and s["set"]:
        length = s["set"] - s["rise"]
        h, m = divmod(round(length.total_seconds() / 60), 60)
        sun_bit = f"rise {clock(s['rise'])} set {clock(s['set'])} ({h}h{m:02d}m)"
    else:
        sun_bit = "sun up all day" if abs(lat) > 60 and _sun_utc_minutes(local.date(), lat, lon, 90.833)[0] is None \
            and (lat > 0) == (4 <= local.month <= 9) else "no sunrise today"
    light = f" Light {clock(s['dawn'])}-{clock(s['dusk'])}." if s["dawn"] and s["dusk"] else ""
    name, lit = moon(when)
    return f"{label} {local.strftime('%a')}: {sun_bit}.{light} Moon {name} {lit}%"
