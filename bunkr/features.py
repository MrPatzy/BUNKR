"""Features that are built but not released yet. They are off unless named in BUNKR_PREVIEW (comma separated).

Held back from 1.3.0: "weather_station" (bunkr/station.py), "csv_import" (bunkr/invimport.py) and "phone_app"
(bunkr/phone.py, the Android app's pairing and notification stream). Off means the routes answer 404 and the
screens don't show them; nothing is deleted, so the hub's own copy turns them on with BUNKR_PREVIEW=weather_station,csv_import,phone_app.
"""

import os

HELD_BACK = ("weather_station", "csv_import", "phone_app")


def enabled(name):
    return name in {p.strip() for p in os.environ.get("BUNKR_PREVIEW", "").split(",")}


def on():
    """The held-back features that are switched on (the screens use this to show or hide them)."""
    return [n for n in HELD_BACK if enabled(n)]
