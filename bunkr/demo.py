"""A made-up household, so BUNKR can be tried (and photographed) without anyone's real data.

    python3 -m bunkr --demo           # a fresh demo in a temporary folder, at http://127.0.0.1:8765
    python3 -m bunkr --demo --db X    # the same, kept in X (filled only if X is new)

The demo runs no background jobs: it fetches nothing, sends nothing and never opens a radio. Everything in it is
invented: the Rivera family, their address, their supplies, the news. Times are relative to when it's made, so the
board always reads "2 h ago" and the flight is always in the air.
"""

import json
import uuid
from datetime import date, datetime, timedelta, timezone

import secrets

from . import auth, brief, checkins, comms, features, flights, medical, news, recovery, station

HOME = {"name": "Home", "kind": "home", "address": "742 Aspen Ct, Longmont, CO 80501", "lat": 40.1672, "lon": -105.1019,
        "county_fips": "08013", "county_name": "Boulder County", "state": "CO"}
PLACES = [
    HOME,
    {"name": "Library parking lot", "kind": "meet_near", "address": "Kimbark St, Longmont, CO", "lat": 40.1690, "lon": -105.1043,
     "state": "CO"},
    {"name": "Aunt Lena's", "kind": "meet_far", "address": "Fort Collins, CO", "lat": 40.5853, "lon": -105.0844, "state": "CO"},
    {"name": "Grandma Rosa's", "kind": "relative", "address": "Pueblo, CO", "lat": 38.2544, "lon": -104.6091, "state": "CO"},
]
PEOPLE = [  # name, role, phone, ham/gmrs, blood, allergies, mesh name, lives elsewhere
    ("Alex", "adult", "android", "KF0ABC", "WRAB123", "O+", "Penicillin", "Alex T114", 0),
    ("Sam", "adult", "iphone", "", "WRAB123", "A-", "", "", 0),
    ("Jordan", "child", "iphone", "", "", "B+", "Peanuts", "", 0),
    ("Mia", "child", "none", "", "", "O+", "", "", 0),
    ("Grandma Rosa", "adult", "iphone", "", "", "", "Sulfa drugs", "", 1),
]
LOCATIONS = [  # name, kind, parent, person, people
    ("Basement", "home", None, "", 1), ("Basement > Shelf A", "home", "Basement", "", 1),
    ("Basement > Shelf B", "home", "Basement", "", 1), ("Garage", "home", None, "", 1),
    ("Kitchen pantry", "home", None, "", 1), ("Hall closet", "home", None, "", 1),
    ("Hall closet > Power outage kit", "home", "Hall closet", "", 1),
    ("Alex's go-bag", "gobag", None, "Alex", 1), ("Sam's go-bag", "gobag", None, "Sam", 1),
    ("Kids' go-bag", "gobag", None, "Jordan, Mia", 2), ("Minivan", "vehicle", None, "", 4),
]
ITEMS = [  # name, category, essential, qty, unit, pack, per_unit (gal or kcal), size_unit, where, expires in days
    ("Water, 5-gallon jugs", "water", "water", 6, "jugs", 1, 5, "gal", "Basement > Shelf A", 400),
    ("Bottled water, case of 24", "water", "water", 4, "cases", 24, 0.132, "ml", "Garage", 500),
    ("Rice, 20 lb bag", "food", "", 2, "bags", 1, 32000, "", "Basement > Shelf B", 700),
    ("Canned black beans", "food", "", 24, "cans", 1, 385, "", "Kitchen pantry", 600),
    ("Freeze-dried meals", "food", "", 18, "pouches", 1, 560, "", "Basement > Shelf B", 3000),
    ("Peanut butter", "food", "", 3, "jars", 1, 2660, "", "Kitchen pantry", 25),
    ("Granola bars", "food", "", 2, "boxes", 12, 190, "", "Kids' go-bag", 120),
    ("First aid kit", "medical", "first_aid_kit", 1, "kit", 1, 0, "", "Hall closet", None),
    ("Ibuprofen and antacids", "medical", "otc_meds", 1, "bag", 1, 0, "", "Hall closet", 300),
    ("N95 masks", "medical", "masks", 2, "boxes", 10, 0, "", "Hall closet > Power outage kit", None),
    ("Headlamps", "power", "flashlight", 4, "headlamps", 1, 0, "", "Hall closet > Power outage kit", None),
    ("AA batteries", "power", "batteries", 2, "packs", 24, 0, "", "Hall closet > Power outage kit", 1500),
    ("Power bank, 20,000 mAh", "power", "power_bank", 2, "power banks", 1, 0, "", "Hall closet > Power outage kit", None),
    ("Hand-crank NOAA weather radio", "comms", "radio", 1, "radio", 1, 0, "", "Hall closet > Power outage kit", None),
    ("GMRS handheld radios", "comms", "", 4, "radios", 1, 0, "", "Garage", None),
    ("Whistles", "comms", "whistle", 4, "whistles", 1, 0, "", "Kids' go-bag", None),
    ("Road atlas, Colorado", "comms", "paper_maps", 1, "atlas", 1, 0, "", "Minivan", None),
    ("Duct tape and plastic sheeting", "shelter", "sheeting_tape", 1, "kit", 1, 0, "", "Garage", None),
    ("Wool blankets", "shelter", "bedding", 5, "blankets", 1, 0, "", "Basement", None),
    ("Fire extinguisher", "shelter", "fire_extinguisher", 2, "extinguishers", 1, 0, "", "Kitchen pantry", 900),
    ("Toilet paper", "sanitation", "toilet_paper", 3, "packs", 12, 0, "", "Basement", None),
    ("Unscented bleach", "sanitation", "bleach", 1, "bottle", 1, 0, "", "Garage", 200),
    ("Cash, small bills", "documents", "cash", 300, "dollars", 1, 0, "", "Hall closet", None),
    ("Jumper cables", "vehicle", "jumper_cables", 1, "set", 1, 0, "", "Minivan", None),
    ("Water pouches", "water", "water", 12, "pouches", 1, 0.033, "ml", "Alex's go-bag", 1800),
    ("Water pouches", "water", "water", 6, "pouches", 1, 0.033, "ml", "Sam's go-bag", 1800),
]
NEWS = [  # outlet, title, hours ago (several outlets on one event make one story)
    ("ap", "Winter storm watch issued for the Front Range ahead of weekend snow", 2),
    ("npr", "Front Range braces for heavy weekend snow as winter storm watch is issued", 3),
    ("reuters", "Heavy snow expected on Colorado's Front Range this weekend, forecasters say", 4),
    ("bbc", "Global shipping delays ease as port backlogs clear", 5),
    ("reuters", "Port backlogs clear and global shipping delays ease, data shows", 6),
    ("dw", "Shipping delays ease worldwide as ports catch up on backlogs", 7),
    ("guardian", "Scientists map new earthquake fault line under coastal city", 8),
    ("ap", "New fault line mapped beneath coastal city, scientists say", 9),
    ("npr", "Power companies test grid backups ahead of winter demand", 3),
    ("ap", "Utilities test grid backup systems before winter demand peaks", 5),
    ("france24", "Flood defences hold as river crests after week of rain", 10),
    ("bbc", "River crests after a week of rain and flood defences hold", 11),
]
SITUATION = [  # source, category, tier, title, summary, where, lat, lon, site, km, scenario
    ("nws", "weather", "priority", "Winter Storm Watch", "8 to 14 inches of snow possible Saturday into Sunday. Travel could be very difficult.",
     "Boulder County", 40.10, -105.20, "Home", 0, "winter_storm"),
    ("usgs", "quake", "routine", "M 2.9, 12 km SW of Trinidad, CO", "Light earthquake, no damage expected.", "Trinidad, CO",
     37.10, -104.60, "Grandma Rosa's", 130, ""),
    ("ioda", "internet", "routine", "Internet outage: regional provider in Nebraska", "Partial outage, about 40 minutes.",
     "Nebraska", 41.10, -100.00, "", None, ""),
]


def _iso(t):
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def fill(store):
    """Fill an empty database with the Rivera household. Safe to call once; does nothing if people already exist."""
    if store._rows("SELECT 1 FROM people LIMIT 1"):
        return False
    now = datetime.now(timezone.utc)
    today = date.today()
    store.set_raw("demo", True)
    store.set_raw("plan_info", {"household_name": "The Rivera family", "mesh_channels": "MeshCore: #rivera (private)",
                                "notes": "Meet at the library lot if we can't get home. Gas shut-off is left of the water heater."})

    site_ids = {}
    for s in PLACES:
        site_ids[s["name"]] = store.save_site(dict(s, geocoded_at=_iso(now)))
    store.set_site_alerts(site_ids["Home"], [{"event": "Winter Storm Watch", "headline": "Winter Storm Watch issued for the Front Range",
                                              "severity": "Moderate", "urgency": "Expected", "description": "8 to 14 inches of snow possible.",
                                              "instruction": "Plan for difficult travel.", "senderName": "NWS Boulder CO",
                                              "fetched": _iso(now)}])

    uids = {}
    for name, role, phone, ham, gmrs, blood, allergies, mesh_name, remote in PEOPLE:
        uid = uuid.uuid4().hex
        store._exec("INSERT INTO people(name, role, phone, call_sign, gmrs_call, blood_type, allergies, mesh_name, remote, site_id, "
                    "uid, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (name, role, phone, ham, gmrs, blood, allergies, mesh_name, remote,
                     site_ids["Grandma Rosa's"] if remote else None, uid, _iso(now)))
        uids[name] = uid
    pid = {r["name"]: r["id"] for r in store._rows("SELECT id, name FROM people")}
    # the demo is used as Alex: a login (with a password nobody knows) linked to Alex
    auth.create_user(store, "alex", secrets.token_urlsafe(24), "admin")
    store._exec("UPDATE people SET user_id=? WHERE id=?", (auth.user_by_name(store, "alex")["id"], pid["Alex"]))

    for name, relation, phone, out_of_area in (("Aunt Lena", "Sam's sister", "(970) 555-0142", 1),
                                               ("Dr. Patel", "Family doctor", "(303) 555-0188", 0),
                                               ("Neighbors, the Kims", "Next door", "(303) 555-0117", 0)):
        store._exec("INSERT INTO contacts(name, relation, phone, out_of_area) VALUES(?,?,?,?)", (name, relation, phone, out_of_area))

    loc_ids = {}
    for name, kind, parent, person, people in LOCATIONS:
        loc_ids[name] = store.save_location({"name": name.split(" > ")[-1], "parent_id": loc_ids.get(parent), "kind": kind,
                                             "person": person, "people": people,
                                             "counts_home": 0 if kind == "vehicle" else 1, "notes": ""})
    for name, cat, ess, qty, unit, pack, per_unit, size_unit, where, exp in ITEMS:
        store.save_item({"name": name, "category": cat, "essential": ess, "quantity": qty, "unit": unit, "pack_size": pack,
                         "pack_unit": "bottles" if pack > 1 and cat == "water" else "", "per_unit": per_unit,
                         "size_unit": size_unit or ("gal" if cat == "water" else ""), "location_id": loc_ids[where],
                         "expires": (today + timedelta(days=exp)).isoformat() if exp else "", "notes": ""})

    medical.save(store, pid["Alex"], {"name": "Lisinopril", "dose": "10 mg", "how_often": "once a day", "per_day": 1,
                                      "on_hand": 24, "bag_count": 7, "bag_expires": (today + timedelta(days=200)).isoformat(),
                                      "pharmacy": "Main St Pharmacy", "critical": 1})
    medical.save(store, pid["Jordan"], {"name": "EpiPen", "dose": "0.3 mg", "how_often": "as needed", "per_day": 0,
                                        "on_hand": 2, "bag_count": 1, "bag_expires": (today + timedelta(days=21)).isoformat(),
                                        "critical": 1})
    medical.save(store, pid["Grandma Rosa"], {"name": "Metformin", "dose": "500 mg", "how_often": "twice a day", "per_day": 2,
                                              "on_hand": 18})

    # the family board: who checked in when, one on a flight home
    for name, status, hours, place, note in (("Alex", "safe", 2, "Home", ""), ("Sam", "moving", 0.4, "", "Leaving work, home by 6"),
                                             ("Jordan", "safe", 3, "School", ""), ("Grandma Rosa", "safe", 20, "Grandma Rosa's", "")):
        checkins.add_checkin(store, uids[name], status, note=note, place=place, at=_iso(now - timedelta(hours=hours)))
    f = flights.add(store, uids["Grandma Rosa"], "WN 1234", origin="PHX", dest="DEN", by="alex")
    f["status"] = "airborne"
    f["info"] = {"scheduled_out": _iso(now - timedelta(minutes=70)), "actual_out": _iso(now - timedelta(minutes=62)),
                 "scheduled_in": _iso(now + timedelta(minutes=35)), "estimated_in": _iso(now + timedelta(minutes=41)),
                 "gate_destination": "C34", "baggage_claim": "7"}
    f["pos"] = {"lat": 38.9, "lon": -106.3, "alt_ft": 36000, "gs_kt": 452, "ground": False, "seen_at": _iso(now - timedelta(minutes=1))}
    flights._save(store, f)

    comms.save_net(store, {"name": "County ARES weekly net", "freq": "146.940 MHz (-0.6, 100.0)", "mode": "FM", "weekday": 2,
                           "time": "19:30", "net_control": "County EC"})
    comms.save_neighbour(store, {"name": "The Kims", "place": "Next door", "phone": "(303) 555-0117",
                                 "skills": ["medical", "chainsaw"], "notes": "Nurse; has a generator."})
    comms.save_licence(store, pid["Alex"], {"kind": "ham", "class": "General", "callsign": "KF0ABC",
                                            "expires": (today + timedelta(days=2900)).isoformat()})
    comms.save_licence(store, pid["Sam"], {"kind": "cpr", "label": "CPR and First Aid",
                                           "expires": (today + timedelta(days=45)).isoformat()})

    room = recovery.save_room(store, {"name": "Living room"})
    for name, cat, value in (("65-inch TV", "Electronics", 900), ("Sectional sofa", "Furniture", 2200)):
        recovery.save_item(store, {"room_id": room, "name": name, "category": cat, "replacement_value": value}, True)

    # news: a few made-up stories, filed the way real headlines are, and a morning brief from them
    heads = [{"outlet": o, "title": t, "url": f"https://example.org/demo/{n}", "summary": "",
              "at": _iso(now - timedelta(hours=h))} for n, (o, t, h) in enumerate(NEWS)]
    news.file_headlines(store, heads, now)
    for src, cat, tier, title, summary, where, lat, lon, site, km, scenario in SITUATION:
        store._exec("INSERT INTO situation(uid, source, category, tier, title, summary, where_text, lat, lon, site, km, scenario, "
                    "first_seen, last_seen, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{src}:demo-{uuid.uuid4().hex[:8]}", src, cat, tier, title, summary, where, lat, lon, site, km, scenario,
                     _iso(now), _iso(now), _iso(now)))
    rec = {"id": uuid.uuid4().hex, "created_at": _iso(now), "near": [], "world_routine": [],
           "groups": brief.group([dict(h) for h in heads], min_regions=1), "outlets": {}, "sources": {},
           "headline_count": len(heads), "ai": None, "ai_error": ""}
    brief.save(store, rec, today.isoformat())

    # weather: a saved forecast with the weekend storm in it, and the family's own station reporting now
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    sky = ["Sunny", "Mostly clear", "Partly sunny", "Partly cloudy", "Increasing clouds", "Snow likely", "Heavy snow",
           "Snow", "Snow showers then partly sunny", "Mostly cloudy", "Sunny", "Clear", "Sunny", "Mostly clear"]
    temps = [48, 27, 45, 25, 38, 22, 29, 15, 33, 14, 41, 20, 47, 24]
    pops = [0, 0, 10, 10, 30, 70, 90, 80, 40, 10, 0, 0, 0, 0]
    periods = []
    for i in range(14):
        t = start + timedelta(hours=12 * i)
        day = i % 2 == 0
        name = "Today" if i == 0 else "Tonight" if i == 1 else t.strftime("%A") + ("" if day else " Night")
        periods.append({"name": name, "startTime": t.isoformat(), "endTime": (t + timedelta(hours=12)).isoformat(), "isDaytime": day,
                        "temperature": temps[i], "windSpeed": "10 to 20 mph" if 5 <= i <= 7 else "5 mph", "windDirection": "NE" if 5 <= i <= 7 else "W",
                        "shortForecast": sky[i], "probabilityOfPrecipitation": {"value": pops[i]}})
    store.set_raw("wx_saved", {f"{round(HOME['lat'], 2)},{round(HOME['lon'], 2)}": {
        "name": "Home", "kind": "home", "lat": HOME["lat"], "lon": HOME["lon"], "at": _iso(now - timedelta(minutes=50)),
        "periods": periods, "hourly": []}})
    if features.enabled("weather_station"):   # held back from 1.3.0: the demo shows none of it unless switched on
        station.new_key(store, "Backyard station")
        for minutes, temp, hpa in ((180, 49, 1018.2), (120, 48, 1017.1), (60, 47, 1016.0), (0, 46, 1015.1)):
            station.record(store, station.normalize({"temp_f": temp, "humidity": 41, "pressure_hpa": hpa, "wind_mph": 7, "gust_mph": 14,
                                                     "wind_dir": 290, "rain_day_in": 0}, now - timedelta(minutes=minutes)))
    return True
