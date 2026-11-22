"""SQLite storage for household settings, inventory, locations, sites, documents and jobs.

Schema changes are applied in order by MIGRATIONS, tracked with PRAGMA user_version,
so an older database upgrades itself the first time a newer BUNKR opens it.
"""

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("BUNKR_DATA", Path.home() / ".local/share/bunkr"))

DEFAULT_SETTINGS = {
    "adults": 2,
    "children": 0,
    "pets": 0,
    "target_days": 14,          # Ready.gov: at least 3 days to evacuate, 2 weeks at home
    "water_gal_per_person": 1.0,  # Ready.gov: one gallon per person per day
    "water_gal_per_pet": 0.5,     # depends heavily on pet size; adjust per household
    "kcal_per_person": 2000,
    "cash_per_person": 100,     # dollars in small bills; no official figure, adjust to taste
    # essential ids that don't apply to this household; situational ones start
    # excluded so they don't top the gap list until the user opts in
    "not_applicable": ["infant_supplies", "glasses"],
    "kiwix_disabled": [],       # curated library ids the user opted out of
}

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 0,
    unit TEXT NOT NULL DEFAULT '',
    per_unit REAL NOT NULL DEFAULT 0,   -- gallons per unit (water) or kcal per unit (food)
    essential TEXT NOT NULL DEFAULT '', -- checklist id this item counts toward
    location TEXT NOT NULL DEFAULT '',  -- legacy free text; replaced by location_id
    expires TEXT NOT NULL DEFAULT '',   -- YYYY-MM-DD or empty
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    doc_type TEXT NOT NULL DEFAULT 'other',
    priority INTEGER NOT NULL DEFAULT 2   -- 1 critical, 2 important, 3 useful
);
CREATE TABLE IF NOT EXISTS backup_jobs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    dest TEXT NOT NULL,
    keep INTEGER NOT NULL DEFAULT 7,
    last_run TEXT NOT NULL DEFAULT '',
    last_status TEXT NOT NULL DEFAULT ''
);
"""

LOCATION_KINDS = {
    "home": "Home (room, closet, garage)",
    "vehicle": "Vehicle",
    "gobag": "Go-bag",
    "stash": "Off-site stash",
}
# Default for "counts toward home supply": home storage and go-bags kept at home yes,
# vehicles and off-site stashes no (they may not be there when you need them).
COUNTS_HOME_DEFAULT = {"home": 1, "vehicle": 0, "gobag": 1, "stash": 0}

SITE_KINDS = {"home": "Home", "meet_near": "Meeting place (near home)",
              "meet_far": "Meeting place (out of the neighborhood)", "bugout": "Bug-out site",
              "relative": "Relative / friend", "work": "Work / school", "other": "Other"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def guess_kind(name):
    n = name.lower()
    if re.search(r"\b(car|truck|van|suv|jeep|vehicle|trunk|glovebox|rv|camper)\b", n):
        return "vehicle"
    if re.search(r"go[- ]?bag|bug[- ]?out bag|\bbob\b|72|backpack|\bbag\b", n):
        return "gobag"
    if re.search(r"storage unit|off[- ]?site|cabin|stash|relative", n):
        return "stash"
    return "home"


def _migrate_1(conn):
    """Locations become records; items get a stable uid and change time (for sync)."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS locations (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        kind TEXT NOT NULL DEFAULT 'home',
        person TEXT NOT NULL DEFAULT '',      -- whose go-bag
        people INTEGER NOT NULL DEFAULT 1,    -- people this kit supplies (go-bag, vehicle)
        counts_home INTEGER NOT NULL DEFAULT 1,
        notes TEXT NOT NULL DEFAULT '',
        uid TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    );
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for col, decl in (("location_id", "INTEGER REFERENCES locations(id) ON DELETE SET NULL"),
                      ("uid", "TEXT NOT NULL DEFAULT ''"),
                      ("updated_at", "TEXT NOT NULL DEFAULT ''")):
        if col not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {decl}")
    now = _now()
    for (name,) in conn.execute("SELECT DISTINCT trim(location) FROM items WHERE trim(location) != ''").fetchall():
        kind = guess_kind(name)
        conn.execute("INSERT OR IGNORE INTO locations(name, kind, counts_home, uid, updated_at) VALUES(?,?,?,?,?)",
                     (name, kind, COUNTS_HOME_DEFAULT[kind], uuid.uuid4().hex, now))
    conn.execute("UPDATE items SET location_id=(SELECT id FROM locations WHERE locations.name=trim(items.location)) "
                 "WHERE trim(location) != '' AND location_id IS NULL")
    for (row_id,) in conn.execute("SELECT id FROM items WHERE uid=''").fetchall():
        conn.execute("UPDATE items SET uid=?, updated_at=? WHERE id=?", (uuid.uuid4().hex, now, row_id))


def _migrate_2(conn):
    """Sites (home, bug-out, relatives) with geocoding, hazard ratings and weather alerts."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'other',
        address TEXT NOT NULL DEFAULT '',
        lat REAL,
        lon REAL,
        county_fips TEXT NOT NULL DEFAULT '',
        county_name TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT '',
        geocoded_at TEXT NOT NULL DEFAULT '',
        hazards TEXT NOT NULL DEFAULT '{}',   -- FEMA NRI ratings by hazard code
        hazards_at TEXT NOT NULL DEFAULT '',
        alerts TEXT NOT NULL DEFAULT '[]',    -- active NWS alerts, as last fetched
        alerts_at TEXT NOT NULL DEFAULT '',
        alerts_error TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT ''
    );
    """)


def _migrate_3(conn):
    """Nodes, the downloads store, update log, logins and sync bookkeeping."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS nodes (
        id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'drive',      -- hub | pi | laptop | drive
        budget_bytes INTEGER NOT NULL DEFAULT 0, -- 0 = use free space
        profile TEXT NOT NULL DEFAULT '{}',      -- what it carries
        token_hash TEXT NOT NULL DEFAULT '',     -- sync token (sha256), paired nodes only
        pair_code_hash TEXT NOT NULL DEFAULT '',
        pair_expires TEXT NOT NULL DEFAULT '',
        manifest TEXT NOT NULL DEFAULT '',       -- last manifest the node reported
        last_seen TEXT NOT NULL DEFAULT '',
        last_sync TEXT NOT NULL DEFAULT '',
        last_sync_status TEXT NOT NULL DEFAULT '',
        records_cursor TEXT NOT NULL DEFAULT '', -- hub change time the node has pulled up to
        reminders TEXT NOT NULL DEFAULT '{}',    -- {kind: last done (ISO date)}
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS depot_files (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,          -- library | tool | map | firmware | radio | app
        item_id TEXT NOT NULL,       -- catalog id (e.g. curated library id)
        path TEXT NOT NULL UNIQUE,   -- relative to the store root
        version TEXT NOT NULL DEFAULT '',
        size INTEGER NOT NULL DEFAULT 0,
        sha256 TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        fetched_at TEXT NOT NULL DEFAULT '',
        verified_at TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'ok'   -- ok | corrupt | missing
    );
    CREATE INDEX IF NOT EXISTS depot_item ON depot_files(kind, item_id);
    CREATE TABLE IF NOT EXISTS updates (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        item_id TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        current TEXT NOT NULL DEFAULT '',
        latest TEXT NOT NULL DEFAULT '',
        major INTEGER NOT NULL DEFAULT 0,
        found_at TEXT NOT NULL DEFAULT '',
        applied_at TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',   -- pending | applied | failed | held
        detail TEXT NOT NULL DEFAULT '',
        UNIQUE(kind, item_id, latest)
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'family',      -- admin | family
        failed INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        last_login TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        last_seen TEXT NOT NULL DEFAULT '',
        agent TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS tombstones (
        kind TEXT NOT NULL,          -- item | location
        uid TEXT NOT NULL,
        deleted_at TEXT NOT NULL,
        PRIMARY KEY (kind, uid)
    );
    CREATE TABLE IF NOT EXISTS sync_conflicts (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        uid TEXT NOT NULL,
        node TEXT NOT NULL,
        kept TEXT NOT NULL,          -- JSON of the version kept
        lost TEXT NOT NULL,          -- JSON of the version overwritten
        at TEXT NOT NULL,
        resolved INTEGER NOT NULL DEFAULT 0
    );
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE documents SET updated_at=? WHERE updated_at=''", (_now(),))


def _migrate_4(conn):
    """When this database received a synced change (so other nodes get it next)."""
    for table in ("items", "locations", "tombstones"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "received_at" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN received_at TEXT NOT NULL DEFAULT ''")


def _migrate_5(conn):
    """Radios and channels, household members, contacts, maintenance tasks, scenario runs."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS radios (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'handheld',    -- handheld | mobile | hf
        service TEXT NOT NULL DEFAULT 'ham',      -- ham | gmrs
        chirp_driver TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        grp TEXT NOT NULL DEFAULT 'Family',
        service TEXT NOT NULL DEFAULT 'ham',      -- ham | gmrs | frs | noaa | other
        rx_mhz REAL NOT NULL,
        duplex TEXT NOT NULL DEFAULT '',          -- '' | + | - | split | off (receive only)
        offset_mhz REAL NOT NULL DEFAULT 0,       -- offset, or the transmit frequency for split
        tone_mode TEXT NOT NULL DEFAULT '',       -- '' | Tone | TSQL | DTCS
        tone_hz REAL NOT NULL DEFAULT 88.5,
        dtcs INTEGER NOT NULL DEFAULT 23,
        mode TEXT NOT NULL DEFAULT 'FM',
        power TEXT NOT NULL DEFAULT '',
        in_plan INTEGER NOT NULL DEFAULT 0,       -- shown in the family plan
        notes TEXT NOT NULL DEFAULT '',
        sort INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS radio_channels (
        radio_id INTEGER NOT NULL REFERENCES radios(id) ON DELETE CASCADE,
        channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
        slot INTEGER NOT NULL,
        PRIMARY KEY (radio_id, channel_id)
    );
    CREATE TABLE IF NOT EXISTS people (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'adult',       -- adult | child
        phone TEXT NOT NULL DEFAULT 'none',       -- android | iphone | none
        call_sign TEXT NOT NULL DEFAULT '',
        gmrs_call TEXT NOT NULL DEFAULT '',
        blood_type TEXT NOT NULL DEFAULT '',
        allergies TEXT NOT NULL DEFAULT '',
        medications TEXT NOT NULL DEFAULT '',
        conditions TEXT NOT NULL DEFAULT '',
        doctor TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        phone_checks TEXT NOT NULL DEFAULT '{}',  -- {checklist id: true}
        phone_checked_at TEXT NOT NULL DEFAULT '',
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        relation TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        address TEXT NOT NULL DEFAULT '',
        out_of_area INTEGER NOT NULL DEFAULT 0,
        notes TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL DEFAULT '',             -- built-in task id, '' for your own
        title TEXT NOT NULL,
        every_days INTEGER NOT NULL DEFAULT 0,    -- repeat interval, or 0 for a yearly date
        yearly TEXT NOT NULL DEFAULT '',          -- MM-DD for seasonal tasks
        last_done TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS scenario_runs (
        id INTEGER PRIMARY KEY,
        scenario TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL DEFAULT '',
        checked TEXT NOT NULL DEFAULT '{}',       -- {step id: who/when}
        notes TEXT NOT NULL DEFAULT ''
    );
    """)


def _migrate_6(conn):
    """Packs: 1 case of 24 x 500 ml bottles. quantity counts packs, pack_size the things in each,
    per_unit is the amount in one of those things (gallons or kcal); size_unit is how to show it."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for col, decl in (("pack_size", "REAL NOT NULL DEFAULT 1"), ("pack_unit", "TEXT NOT NULL DEFAULT ''"),
                      ("size_unit", "TEXT NOT NULL DEFAULT ''")):
        if col not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {decl}")


def _migrate_7(conn):
    """Firearms: type (firearm, ammo, magazine, gear), caliber and serial number."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for col in ("subtype", "caliber", "serial"):
        if col not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _migrate_8(conn):
    """Ammunition type (training, defensive...) and magazines: capacity, status, which firearm they fit."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for col, decl in (("ammo_type", "TEXT NOT NULL DEFAULT ''"), ("capacity", "REAL NOT NULL DEFAULT 0"),
                      ("mag_status", "TEXT NOT NULL DEFAULT ''"), ("fits", "TEXT NOT NULL DEFAULT ''")):
        if col not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {decl}")


def _migrate_9(conn):
    """Master channel plan: per-direction tones, fixed slots, system/status tags; radio model profiles."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(channels)")}
    for col, decl in (("tx_tone", "TEXT NOT NULL DEFAULT ''"), ("rx_tone", "TEXT NOT NULL DEFAULT ''"),
                      ("slot", "INTEGER"), ("system", "TEXT NOT NULL DEFAULT ''"),
                      ("region", "TEXT NOT NULL DEFAULT ''"), ("status", "TEXT NOT NULL DEFAULT ''"),
                      ("source", "TEXT NOT NULL DEFAULT ''"), ("scan", "INTEGER NOT NULL DEFAULT 1"),
                      ("watch", "INTEGER NOT NULL DEFAULT 0"), ("short", "TEXT NOT NULL DEFAULT ''"),
                      ("rx_only", "INTEGER NOT NULL DEFAULT 0"), ("wiresx", "INTEGER NOT NULL DEFAULT 0"),
                      ("radios", "TEXT NOT NULL DEFAULT ''")):
        if col not in cols:
            conn.execute(f"ALTER TABLE channels ADD COLUMN {col} {decl}")
    # carry the old single tone setting over to the per-direction fields
    for r in conn.execute("SELECT id, tone_mode, tone_hz, dtcs FROM channels").fetchall():
        tx = rx = ""
        if r[1] == "Tone":
            tx = f"{r[2]:.1f}"
        elif r[1] == "TSQL":
            tx = rx = f"{r[2]:.1f}"
        elif r[1] == "DTCS":
            tx = rx = f"D{int(r[3]):03d}N"
        conn.execute("UPDATE channels SET tx_tone=?, rx_tone=? WHERE id=? AND tx_tone='' AND rx_tone=''", (tx, rx, r[0]))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(radios)")}
    for col in ("profile", "owner"):
        if col not in cols:
            conn.execute(f"ALTER TABLE radios ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS channels_slot ON channels(slot) WHERE slot IS NOT NULL")


def _migrate_10(conn):
    """Optional AI connector (MCP): OAuth clients, codes and tokens, and a log of every change made through it."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS mcp_clients (
        client_id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        redirect_uris TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS mcp_codes (
        code_hash TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        redirect_uri TEXT NOT NULL,
        challenge TEXT NOT NULL,
        resource TEXT NOT NULL DEFAULT '',
        expires_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS mcp_tokens (
        token_hash TEXT PRIMARY KEY,
        kind TEXT NOT NULL,                 -- access | refresh
        client_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_used TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS mcp_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        username TEXT NOT NULL,
        tool TEXT NOT NULL,
        args TEXT NOT NULL DEFAULT '{}',
        result TEXT NOT NULL DEFAULT '',
        undo TEXT NOT NULL DEFAULT ''        -- JSON: how to put it back
    );
    """)


ROOM_WORDS = (r"master bedroom|guest bedroom|kids? bedroom|bedroom(?: \d+)?|living room|family room|dining room|"
              r"kitchen|pantry|garage|basement|attic|laundry room|bathroom|office|hallway|closet|mudroom|"
              r"storm cellar|store ?room")


def _migrate_11(conn):
    """Sub-locations: a location can sit inside another (two levels below the top at most). `name` stays the
    full path ("Master bedroom > Under bed"); `label` is this level's own name. Old flat room-prefixed home
    locations ("Master bedroom radio briefcase") become sub-locations under a room with their items,
    and the flat ones are removed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(locations)")}
    if "parent_id" not in cols:
        conn.execute("ALTER TABLE locations ADD COLUMN parent_id INTEGER REFERENCES locations(id) ON DELETE SET NULL")
    if "label" not in cols:
        conn.execute("ALTER TABLE locations ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE locations SET label=name WHERE label=''")
    pat = re.compile(rf"^({ROOM_WORDS})\s+(\S.*)$", re.I)
    now = _now()
    for old in conn.execute("SELECT * FROM locations WHERE kind='home' AND parent_id IS NULL ORDER BY id").fetchall():
        m = pat.match(old["name"].strip())
        if not m:
            continue
        room, leaf = m.group(1).strip(), m.group(2).strip()
        if re.fullmatch(rf"(?:{ROOM_WORDS})", leaf, re.I):  # "Basement Storeroom" is a room in its own right
            continue
        leaf = leaf[:1].upper() + leaf[1:]
        parent = conn.execute("SELECT id, name FROM locations WHERE name=? COLLATE NOCASE AND parent_id IS NULL",
                              (room,)).fetchone()
        if parent:
            parent_id, pname = parent["id"], parent["name"]
        else:
            pname = room[:1].upper() + room[1:]
            parent_id = conn.execute(
                "INSERT INTO locations(name, label, kind, counts_home, uid, updated_at) VALUES(?,?,?,?,?,?)",
                (pname, pname, "home", old["counts_home"], uuid.uuid4().hex, now)).lastrowid
        item_ids = [r[0] for r in conn.execute("SELECT id FROM items WHERE location_id=?", (old["id"],))]
        conn.execute("DELETE FROM locations WHERE id=?", (old["id"],))
        if old["uid"]:
            conn.execute("INSERT INTO tombstones(kind, uid, deleted_at) VALUES('location',?,?) "
                         "ON CONFLICT(kind, uid) DO UPDATE SET deleted_at=excluded.deleted_at", (old["uid"], now))
        new_id = conn.execute(
            "INSERT INTO locations(name, label, parent_id, kind, person, people, counts_home, notes, uid, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"{pname} > {leaf}", leaf, parent_id, old["kind"], old["person"], old["people"], old["counts_home"],
             old["notes"], uuid.uuid4().hex, now)).lastrowid
        conn.executemany("UPDATE items SET location_id=?, updated_at=? WHERE id=?", [(new_id, now, i) for i in item_ids])


def _migrate_12(conn):
    """Family check-ins and calls. People can live elsewhere (remote: Grandma in the city) with their own place
    (a site), get a uid so they can travel to nodes, and logins get a preferred view (simple or full)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(people)")}
    for name, ddl in (("remote", "INTEGER NOT NULL DEFAULT 0"), ("site_id", "INTEGER REFERENCES sites(id) ON DELETE SET NULL"),
                      ("uid", "TEXT NOT NULL DEFAULT ''"), ("updated_at", "TEXT NOT NULL DEFAULT ''")):
        if name not in cols:
            conn.execute(f"ALTER TABLE people ADD COLUMN {name} {ddl}")
    for (pid,) in conn.execute("SELECT id FROM people WHERE uid=''").fetchall():
        conn.execute("UPDATE people SET uid=?, updated_at=? WHERE id=?", (uuid.uuid4().hex, _now(), pid))
    ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "view" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN view TEXT NOT NULL DEFAULT ''")   # '' = by role | simple | full
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS checkins (
        id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL UNIQUE,
        person_uid TEXT NOT NULL,
        person_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,                  -- safe | moving | help
        note TEXT NOT NULL DEFAULT '',
        place TEXT NOT NULL DEFAULT '',
        lat REAL,
        lon REAL,
        call_uid TEXT NOT NULL DEFAULT '',     -- the family call it answers, if any
        via TEXT NOT NULL DEFAULT 'app',       -- app | field | mesh | radio | other
        by_name TEXT NOT NULL DEFAULT '',      -- who entered it, when not the person
        at TEXT NOT NULL,
        received_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS checkins_person ON checkins(person_uid, at);
    CREATE TABLE IF NOT EXISTS family_calls (
        id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,                    -- evacuate | meet | shelter | checkin
        site_uid TEXT NOT NULL DEFAULT '',
        place TEXT NOT NULL DEFAULT '',        -- the destination as text, so it reads the same on every node
        address TEXT NOT NULL DEFAULT '',
        lat REAL,
        lon REAL,
        message TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        ended_at TEXT NOT NULL DEFAULT '',
        ended_by TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        received_at TEXT NOT NULL DEFAULT ''
    );
    """)


def _migrate_13(conn):
    """The Situation tab: alerts, disasters, fires, space weather, outages and cyber items from public feeds."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS situation (
        uid TEXT PRIMARY KEY,                  -- source:id
        source TEXT NOT NULL,
        category TEXT NOT NULL,
        tier TEXT NOT NULL,                    -- flash | priority | routine
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        where_text TEXT NOT NULL DEFAULT '',
        lat REAL,
        lon REAL,
        site TEXT NOT NULL DEFAULT '',         -- nearest family place
        km INTEGER,                            -- distance to it
        url TEXT NOT NULL DEFAULT '',
        starts TEXT NOT NULL DEFAULT '',
        ends TEXT NOT NULL DEFAULT '',
        scenario TEXT NOT NULL DEFAULT '',     -- household checklist it opens
        trust TEXT NOT NULL DEFAULT 'official',
        first_seen TEXT NOT NULL DEFAULT '',
        last_seen TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS situation_active ON situation(active, tier);
    """)


def _migrate_14(conn):
    """Phone notifications: each phone's push subscription, what was sent (for daily caps), each login's settings."""
    if "push_prefs" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
        conn.execute("ALTER TABLE users ADD COLUMN push_prefs TEXT NOT NULL DEFAULT '{}'")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS push_subs (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        endpoint TEXT NOT NULL UNIQUE,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        agent TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        last_ok TEXT NOT NULL DEFAULT '',
        fails INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS push_log (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        at TEXT NOT NULL,
        tier TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        sent INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS push_log_user ON push_log(user_id, tier, at);
    """)


def _migrate_15(conn):
    """The morning brief, one per day."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS briefs (
        id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL DEFAULT '',
        day TEXT NOT NULL UNIQUE,              -- local date it is for
        created_at TEXT NOT NULL,
        data TEXT NOT NULL,                    -- JSON: stories, groups, near us, sources
        ai INTEGER NOT NULL DEFAULT 0
    );
    """)


def _migrate_16(conn):
    """Situation map: the outline of a warning area, as GeoJSON, when the source gives one."""
    if "geom" not in {r[1] for r in conn.execute("PRAGMA table_info(situation)")}:
        conn.execute("ALTER TABLE situation ADD COLUMN geom TEXT NOT NULL DEFAULT ''")


def _migrate_17(conn):
    """Drills: a family call can be a practice run (no night pushes, a timing report afterwards)."""
    if "drill" not in {r[1] for r in conn.execute("PRAGMA table_info(family_calls)")}:
        conn.execute("ALTER TABLE family_calls ADD COLUMN drill INTEGER NOT NULL DEFAULT 0")


def _migrate_18(conn):
    """Medical: medications with supply on hand and go-bag doses, reminders sent, and more on each person."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(people)")}
    for col in ("devices", "insurance", "pharmacy"):
        if col not in have:
            conn.execute(f"ALTER TABLE people ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS meds (
        id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL UNIQUE,
        person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        dose TEXT NOT NULL DEFAULT '',
        how_often TEXT NOT NULL DEFAULT '',
        per_day REAL NOT NULL DEFAULT 0,       -- doses a day, for counting down the supply; 0 = as needed
        on_hand REAL NOT NULL DEFAULT 0,       -- doses at home, as of on_hand_at
        on_hand_at TEXT NOT NULL DEFAULT '',
        bag_count REAL NOT NULL DEFAULT 0,     -- doses in the go-bag
        bag_expires TEXT NOT NULL DEFAULT '',
        refill_by TEXT NOT NULL DEFAULT '',    -- a date someone set by hand
        pharmacy TEXT NOT NULL DEFAULT '',
        rx_number TEXT NOT NULL DEFAULT '',
        prescriber TEXT NOT NULL DEFAULT '',
        critical INTEGER NOT NULL DEFAULT 0,   -- can't go without (insulin, seizure meds...)
        cold INTEGER NOT NULL DEFAULT 0,       -- needs the fridge: matters in a power outage
        notes TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS reminder_log (
        key TEXT PRIMARY KEY,                  -- what was reminded about, so it's said once
        at TEXT NOT NULL
    );
    """)


def _migrate_19(conn):
    """Power plan: what needs power in an outage and which battery it plugs into."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS power_devices (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        watts REAL NOT NULL DEFAULT 0,         -- while running
        hours REAL NOT NULL DEFAULT 0,         -- a day, during an outage
        surge_w REAL NOT NULL DEFAULT 0,       -- start-up surge (motors, compressors)
        priority INTEGER NOT NULL DEFAULT 2,   -- 1 must, 2 should, 3 nice
        battery TEXT NOT NULL DEFAULT '',      -- which battery it plugs into ('' = none)
        notes TEXT NOT NULL DEFAULT ''
    );
    """)


def _migrate_20(conn):
    """Fast entry: barcodes BUNKR has seen, and the item each was saved as (so a rescan works offline)."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS barcodes (
        code TEXT PRIMARY KEY,
        item_uid TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT 'other',
        per_unit REAL NOT NULL DEFAULT 0,
        size_unit TEXT NOT NULL DEFAULT '',
        unit TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    );
    """)


def _migrate_21(conn):
    """Audit log: who viewed or changed sensitive things, and every login. Each row carries a hash of the one before,
    so an edited or deleted row shows up (audit.verify)."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        user_id INTEGER,
        username TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL,
        what TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        ip TEXT NOT NULL DEFAULT '',
        prev TEXT NOT NULL DEFAULT '',
        hash TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS audit_at ON audit_log(at);
    CREATE INDEX IF NOT EXISTS audit_user ON audit_log(user_id, what, at);
    """)


def _migrate_22(conn):
    """Two-step login: authenticator-code secrets (encrypted with mfa.key), recovery-code hashes, passkeys, and
    sessions that may only finish setting up two-step login."""
    for table, col, decl in (("users", "totp_secret", "TEXT NOT NULL DEFAULT ''"), ("users", "totp_last", "INTEGER NOT NULL DEFAULT 0"),
                             ("users", "recovery", "TEXT NOT NULL DEFAULT '[]'"), ("users", "webauthn_id", "TEXT NOT NULL DEFAULT ''"),
                             ("sessions", "scope", "TEXT NOT NULL DEFAULT 'full'")):
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS passkeys (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        cred_id TEXT NOT NULL UNIQUE,
        public_key BLOB NOT NULL,
        alg INTEGER NOT NULL,
        sign_count INTEGER NOT NULL DEFAULT 0,
        name TEXT NOT NULL DEFAULT '',
        rp_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_used TEXT NOT NULL DEFAULT ''
    );
    """)


def _migrate_23(conn):
    """Node lock: whether a node keeps its data locked (as it last told the hub), and nodes marked lost or stolen."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "lock" not in have:
        conn.execute("ALTER TABLE nodes ADD COLUMN lock TEXT NOT NULL DEFAULT ''")
    if "lost" not in have:
        conn.execute("ALTER TABLE nodes ADD COLUMN lost TEXT NOT NULL DEFAULT ''")


def _migrate_24(conn):
    """Each login's own Home layout: tile order, hidden and folded tiles (JSON; empty = the family's default)."""
    if "home_layout" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
        conn.execute("ALTER TABLE users ADD COLUMN home_layout TEXT NOT NULL DEFAULT ''")


def _migrate_25(conn):
    """The news feed: headlines filed into stories (tagged with area, place and topic), and what each person read and rated."""
    if "news_prefs" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:
        conn.execute("ALTER TABLE users ADD COLUMN news_prefs TEXT NOT NULL DEFAULT ''")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS news_stories (
        id TEXT PRIMARY KEY,
        headline TEXT NOT NULL,
        first_at TEXT NOT NULL,
        last_at TEXT NOT NULL,
        area TEXT,
        place TEXT NOT NULL DEFAULT '',
        lat REAL,
        lon REAL,
        topic TEXT NOT NULL DEFAULT 'other',
        outlets INTEGER NOT NULL DEFAULT 0,
        regions TEXT NOT NULL DEFAULT '[]',
        ai INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS news_stories_last ON news_stories(last_at);
    CREATE TABLE IF NOT EXISTS news_items (
        id INTEGER PRIMARY KEY,
        url TEXT NOT NULL UNIQUE,
        outlet TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        story TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS news_items_story ON news_items(story);
    CREATE TABLE IF NOT EXISTS news_marks (
        user_id INTEGER NOT NULL,
        story TEXT NOT NULL,
        read_at TEXT NOT NULL DEFAULT '',
        rating INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, story)
    );
    """)


def _migrate_26(conn):
    """Flights the family is following: whose, which, and what FlightAware, adsb.lol and the FAA last said."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS flights (
        uid TEXT PRIMARY KEY,
        person_uid TEXT NOT NULL,
        person_name TEXT NOT NULL,
        ident TEXT NOT NULL,
        callsign TEXT NOT NULL DEFAULT '',
        airline TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT '',
        dest TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        started_by TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        ended_at TEXT NOT NULL DEFAULT '',
        ended_why TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'added',
        info TEXT NOT NULL DEFAULT '{}',
        pos TEXT NOT NULL DEFAULT '{}',
        aero_at TEXT NOT NULL DEFAULT '',
        told TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS flights_active ON flights(ended_at);
    """)


def _migrate_27(conn):
    """MeshCore check-ins: the name each person uses on the mesh, and a short log of what the radio heard and sent."""
    if "mesh_name" not in {r[1] for r in conn.execute("PRAGMA table_info(people)")}:
        conn.execute("ALTER TABLE people ADD COLUMN mesh_name TEXT NOT NULL DEFAULT ''")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS mesh_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        direction TEXT NOT NULL,
        sender TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL,
        result TEXT NOT NULL DEFAULT ''
    );
    """)


def _migrate_28(conn):
    """MeshCore: more than one channel (Family, a trusted group), so the log says which one."""
    if "channel" not in {r[1] for r in conn.execute("PRAGMA table_info(mesh_log)")}:
        conn.execute("ALTER TABLE mesh_log ADD COLUMN channel TEXT NOT NULL DEFAULT ''")


def _migrate_29(conn):
    """MeshCore nodes BUNKR knows, by public key (a name can change or be shared; a key can't), and whose each one is."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS mesh_nodes (
        public_key TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        node_type INTEGER NOT NULL DEFAULT 0,
        person_uid TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT '',
        lat REAL,
        lon REAL,
        hops INTEGER,
        source TEXT NOT NULL DEFAULT 'radio',
        first_seen TEXT NOT NULL DEFAULT '',
        last_seen TEXT NOT NULL DEFAULT '',
        last_heard TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS mesh_nodes_person ON mesh_nodes(person_uid);
    """)


def _migrate_30(conn):
    """Notifications kept in the app (the bell), email for reminders, document renewal dates, battery on check-ins."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS notices (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        tier TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL DEFAULT '',
        tag TEXT NOT NULL DEFAULT '',
        read_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS notices_user ON notices(user_id, read_at, at);
    CREATE TABLE IF NOT EXISTS email_outbox (
        id INTEGER PRIMARY KEY,
        to_addr TEXT NOT NULL,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        tries INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        sent_at TEXT NOT NULL DEFAULT ''
    );
    """)
    have = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
    for col in ("renew_on", "renewed_at", "insurer", "coverage", "claim_phone"):
        if col not in have:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    have = {r[1] for r in conn.execute("PRAGMA table_info(checkins)")}
    if "battery" not in have:
        conn.execute("ALTER TABLE checkins ADD COLUMN battery INTEGER")            # percent, when the phone says
    if "battery_low" not in have:
        conn.execute("ALTER TABLE checkins ADD COLUMN battery_low INTEGER NOT NULL DEFAULT 0")


def _migrate_31(conn):
    """News: flags on sources that don't belong to a story (reviewed by an admin), why a headline joined its story,
    and stories BUNKR raised early ("developing")."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS news_flags (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,                    -- feed | brief
        story TEXT NOT NULL DEFAULT '',        -- feed: the story id it was in
        day TEXT NOT NULL DEFAULT '',          -- brief: the brief's day
        story_title TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL,
        outlet TEXT NOT NULL DEFAULT '',
        source_title TEXT NOT NULL DEFAULT '',
        words TEXT NOT NULL DEFAULT '[]',      -- feed: the shared words that joined it to the story
        note TEXT NOT NULL DEFAULT '',
        user_id INTEGER,
        username TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',   -- open | confirmed | dismissed
        reviewed_by TEXT NOT NULL DEFAULT '',
        reviewed_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS news_flags_status ON news_flags(status, at);
    """)
    if "match" not in {r[1] for r in conn.execute("PRAGMA table_info(news_items)")}:
        conn.execute("ALTER TABLE news_items ADD COLUMN match TEXT NOT NULL DEFAULT ''")  # JSON: shared words and score
    if "alarm" not in {r[1] for r in conn.execute("PRAGMA table_info(news_stories)")}:
        conn.execute("ALTER TABLE news_stories ADD COLUMN alarm TEXT NOT NULL DEFAULT ''")  # why it was raised early



def _migrate_32(conn):
    """Unconfirmed: public posts (Bluesky, Reddit) about the family's towns that mention an alarm word."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS chatter (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,                  -- bluesky | reddit
        channel TEXT NOT NULL DEFAULT '',      -- r/Denver
        place TEXT NOT NULL,                   -- the family town it names
        author TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        at TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        alarm TEXT NOT NULL DEFAULT '',
        story TEXT NOT NULL DEFAULT ''         -- the news story that later reported it
    );
    CREATE INDEX IF NOT EXISTS chatter_at ON chatter(at);
    """)


def _migrate_33(conn):
    """Community and comms: licences and skills per person, neighbours, radio nets and a check-in log, ICS-213 messages,
    and partner hubs (a read-only share for a neighbour's BUNKR, and the partners this hub reads)."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS licences (
        id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL UNIQUE,
        person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,                    -- ham | gmrs | cpr | first_aid | cert | other
        label TEXT NOT NULL DEFAULT '',        -- what it is, for "other" (Wilderness First Responder...)
        class TEXT NOT NULL DEFAULT '',        -- ham: Technician | General | Extra
        callsign TEXT NOT NULL DEFAULT '',
        number TEXT NOT NULL DEFAULT '',       -- certificate or FRN number
        expires TEXT NOT NULL DEFAULT '',      -- YYYY-MM-DD or empty
        renewed_at TEXT NOT NULL DEFAULT '',   -- "done" on the maintenance list, until a new date is set
        notes TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS neighbours (
        id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        place TEXT NOT NULL DEFAULT '',        -- address or "two doors down"
        lat REAL,
        lon REAL,
        phone TEXT NOT NULL DEFAULT '',
        callsign TEXT NOT NULL DEFAULT '',
        contact TEXT NOT NULL DEFAULT '',      -- email or anything else
        skills TEXT NOT NULL DEFAULT '[]',     -- JSON list of pick-list ids
        skills_other TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS nets (
        id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        freq TEXT NOT NULL DEFAULT '',         -- "146.520" or "GMRS 20" or a repeater name
        mode TEXT NOT NULL DEFAULT 'FM',
        weekday INTEGER NOT NULL DEFAULT -1,   -- 0 Monday .. 6 Sunday, -1 every day
        time TEXT NOT NULL DEFAULT '',         -- HH:MM local
        net_control TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS net_log (
        id INTEGER PRIMARY KEY,
        net_id INTEGER REFERENCES nets(id) ON DELETE SET NULL,
        net_name TEXT NOT NULL DEFAULT '',     -- kept when the net is removed
        at TEXT NOT NULL,
        who TEXT NOT NULL,
        signal TEXT NOT NULL DEFAULT '',       -- 59, "5 by 9", "weak"
        notes TEXT NOT NULL DEFAULT '',
        by_name TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS net_log_at ON net_log(at);
    CREATE TABLE IF NOT EXISTS ics213 (
        id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL UNIQUE,
        direction TEXT NOT NULL DEFAULT 'sent', -- sent | received
        incident TEXT NOT NULL DEFAULT '',
        to_name TEXT NOT NULL DEFAULT '',
        to_position TEXT NOT NULL DEFAULT '',
        from_name TEXT NOT NULL DEFAULT '',
        from_position TEXT NOT NULL DEFAULT '',
        subject TEXT NOT NULL DEFAULT '',
        msg_date TEXT NOT NULL DEFAULT '',
        msg_time TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT '',
        approved_name TEXT NOT NULL DEFAULT '',
        approved_position TEXT NOT NULL DEFAULT '',
        reply TEXT NOT NULL DEFAULT '',
        reply_name TEXT NOT NULL DEFAULT '',
        reply_position TEXT NOT NULL DEFAULT '',
        reply_at TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS partner_shares (
        id INTEGER PRIMARY KEY,
        uid TEXT NOT NULL UNIQUE,              -- in the share's address; not a secret
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL,              -- SHA-256 of the token; the token itself is shown once
        parts TEXT NOT NULL DEFAULT '[]',      -- JSON: board | calls | comms | licences
        created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        revoked_at TEXT NOT NULL DEFAULT '',
        last_used_at TEXT NOT NULL DEFAULT '',
        uses INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS partners (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        url TEXT NOT NULL,                     -- the share address; its token is in partner-<id>.token next to the DB
        data TEXT NOT NULL DEFAULT '{}',       -- the last good answer, cleaned
        as_of TEXT NOT NULL DEFAULT '',        -- when their hub made it
        fetched_at TEXT NOT NULL DEFAULT '',
        last_try TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    """)


def _migrate_34(conn):
    """Recovery: a home inventory for insurance (rooms, items, photos and receipts kept as files next to the
    database) and the admin-only financial access sheet. None of it is synced to nodes."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS recovery_rooms (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        notes TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS recovery_items (
        id INTEGER PRIMARY KEY,
        room_id INTEGER REFERENCES recovery_rooms(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        make_model TEXT NOT NULL DEFAULT '',
        serial TEXT NOT NULL DEFAULT '',        -- admin only, like firearm serials
        purchased TEXT NOT NULL DEFAULT '',     -- YYYY-MM-DD
        price_paid REAL,
        replacement_value REAL,
        notes TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS recovery_items_room ON recovery_items(room_id);
    CREATE TABLE IF NOT EXISTS recovery_files (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES recovery_items(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,                     -- photo | receipt (receipts are admin only)
        name TEXT NOT NULL DEFAULT '',          -- the name it was uploaded with
        file TEXT NOT NULL UNIQUE,              -- stored name in <db dir>/recovery-files/
        ctype TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        added_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS recovery_files_item ON recovery_files(item_id);
    CREATE TABLE IF NOT EXISTS finance_accounts (
        id INTEGER PRIMARY KEY,
        institution TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'other',     -- bank | credit_card | insurance | retirement | loan | utility | other
        holder TEXT NOT NULL DEFAULT '',
        last4 TEXT NOT NULL DEFAULT '',         -- never more than the last four digits
        phone TEXT NOT NULL DEFAULT '',
        website TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',         -- policy and claim notes
        contact TEXT NOT NULL DEFAULT '',       -- who to call
        paperwork TEXT NOT NULL DEFAULT '',     -- where the paperwork is
        updated_at TEXT NOT NULL DEFAULT ''
    );
    """)

MIGRATIONS = [_migrate_1, _migrate_2, _migrate_3, _migrate_4, _migrate_5, _migrate_6, _migrate_7, _migrate_8, _migrate_9,
              _migrate_10, _migrate_11, _migrate_12, _migrate_13, _migrate_14, _migrate_15, _migrate_16, _migrate_17, _migrate_18, _migrate_19, _migrate_20,
              _migrate_21, _migrate_22, _migrate_23, _migrate_24, _migrate_25, _migrate_26, _migrate_27, _migrate_28, _migrate_29, _migrate_30, _migrate_31, _migrate_32,
              _migrate_33, _migrate_34]
SCHEMA_VERSION = len(MIGRATIONS)

ITEM_FIELDS = ("name", "category", "quantity", "unit", "pack_size", "pack_unit", "per_unit", "size_unit",
               "essential", "location_id", "expires", "notes", "subtype", "caliber", "serial",
               "ammo_type", "capacity", "mag_status", "fits")
LOCATION_FIELDS = ("name", "label", "parent_id", "kind", "person", "people", "counts_home", "notes")
MAX_DEPTH = 2          # levels below a top-level location
PATH_SEP = " > "
SITE_FIELDS = ("name", "kind", "address", "lat", "lon", "county_fips", "county_name", "state",
               "geocoded_at", "notes")
DOC_FIELDS = ("path", "label", "doc_type", "priority", "updated_at", "renew_on", "renewed_at", "insurer", "coverage", "claim_phone")
BACKUP_FIELDS = ("name", "source", "dest", "keep")


class Store:
    def __init__(self, path=None, data=None):
        """data: a whole database as bytes (a node's unlocked vault). It is opened in memory only; `path` then just
        names the folder its key files live in, and nothing is written there."""
        if path is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = DATA_DIR / "bunkr.db"
        self.path = str(path)
        self._lock = threading.RLock()
        self.in_memory = data is not None
        if self.in_memory:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            if data:
                self._conn.deserialize(data)
        else:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(BASE_SCHEMA)
        self._migrate()

    def snapshot(self):
        """The whole database as bytes, and a change counter to tell whether it moved since last time."""
        with self._lock:
            return self._conn.serialize(), self._conn.total_changes

    def changes(self):
        with self._lock:
            return self._conn.total_changes

    def _migrate(self):
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for n, step in enumerate(MIGRATIONS[version:], start=version + 1):
                with self._conn:  # one transaction per step
                    step(self._conn)
                    self._conn.execute(f"PRAGMA user_version={n}")

    def _rows(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _exec(self, sql, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.lastrowid

    # settings
    def settings(self):
        out = dict(DEFAULT_SETTINGS)
        for row in self._rows("SELECT key, value FROM settings"):
            if row["key"] in DEFAULT_SETTINGS:
                out[row["key"]] = json.loads(row["value"])
        # Once household members are entered on the Family tab, they set the head count.
        counts = self._rows("SELECT role, COUNT(*) AS n FROM people WHERE remote=0 GROUP BY role")
        if counts:
            by = {r["role"]: r["n"] for r in counts}
            out["adults"], out["children"] = by.get("adult", 0), by.get("child", 0)
            out["household_from_people"] = True
        return out

    def update_settings(self, values):
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                continue
            default = DEFAULT_SETTINGS[key]
            if isinstance(default, list):
                value = [str(v) for v in value]
            elif isinstance(default, (int, float)):
                value = max(0, type(default)(value))
            self.set_raw(key, value)
        return self.settings()

    def get_raw(self, key, default=None):
        rows = self._rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_raw(self, key, value):
        self._exec("INSERT INTO settings(key, value) VALUES(?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    # generic CRUD
    def _save(self, table, fields, data, row_id=None, stamp=False):
        values = {f: data[f] for f in fields if f in data}
        if stamp:
            values["updated_at"] = data.get("updated_at") or _now()
            if row_id is None:
                values["uid"] = data.get("uid") or uuid.uuid4().hex
        if row_id is None:
            cols = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            return self._exec(f"INSERT INTO {table}({cols}) VALUES({marks})", tuple(values.values()))
        if values:
            sets = ", ".join(f"{c}=?" for c in values)
            self._exec(f"UPDATE {table} SET {sets} WHERE id=?", (*values.values(), row_id))
        return row_id

    # items
    def items(self):
        rows = self._rows("""
            SELECT i.*, COALESCE(l.name, '') AS location_name, COALESCE(t.kind, '') AS location_kind,
                   COALESCE(t.counts_home, 1) AS counts_home, t.id AS top_location_id
            FROM items i LEFT JOIN locations l ON l.id = i.location_id
            LEFT JOIN locations p ON p.id = l.parent_id
            LEFT JOIN locations t ON t.id = COALESCE(p.parent_id, p.id, l.id)
            ORDER BY i.category, i.name""")
        for r in rows:  # the legacy free-text column is superseded by the location record
            r["location"] = r.pop("location_name")
        return rows

    def save_item(self, data, row_id=None):
        return self._save("items", ITEM_FIELDS, data, row_id, stamp=True)

    def move_items(self, ids, location_id):
        now = _now()
        with self._lock:
            self._conn.executemany("UPDATE items SET location_id=?, updated_at=? WHERE id=?",
                                   [(location_id, now, i) for i in ids])
            self._conn.commit()

    def delete_item(self, row_id):
        with self._lock:
            row = self._conn.execute("SELECT uid FROM items WHERE id=?", (row_id,)).fetchone()
            if row and row["uid"]:
                self._tombstone("item", row["uid"])
            self._conn.execute("DELETE FROM items WHERE id=?", (row_id,))
            self._conn.commit()

    def _tombstone(self, kind, uid):
        self._conn.execute("INSERT INTO tombstones(kind, uid, deleted_at) VALUES(?,?,?) "
                           "ON CONFLICT(kind, uid) DO UPDATE SET deleted_at=excluded.deleted_at", (kind, uid, _now()))

    # locations
    def locations(self):
        """Every location. A sub-location takes its type, people and home-supply setting from its top-level one."""
        rows = self._rows("""
            SELECT l.*, (SELECT COUNT(*) FROM items i WHERE i.location_id = l.id) AS item_count
            FROM locations l""")
        by_id = {r["id"]: r for r in rows}
        for r in rows:
            depth, top = 0, r
            while top["parent_id"] in by_id and depth < MAX_DEPTH:
                top, depth = by_id[top["parent_id"]], depth + 1
            r["depth"], r["top_id"] = depth, top["id"]
        for r in rows:
            top = by_id[r["top_id"]]
            if top is not r:
                for f in ("kind", "person", "people", "counts_home"):
                    r[f] = top[f]
        order = {"home": 0, "gobag": 1, "vehicle": 2}
        rows.sort(key=lambda r: (order.get(by_id[r["top_id"]]["kind"], 3), by_id[r["top_id"]]["name"].lower(),
                                 r["name"].lower()))
        return rows

    def location(self, row_id):
        rows = self._rows("SELECT * FROM locations WHERE id=?", (row_id,))
        return rows[0] if rows else None

    def _location_depth(self, row_id):
        depth = 0
        while depth <= MAX_DEPTH + 1:
            row = self._conn.execute("SELECT parent_id FROM locations WHERE id=?", (row_id,)).fetchone()
            if row is None or row["parent_id"] is None:
                break
            row_id, depth = row["parent_id"], depth + 1
        return depth

    def save_location(self, data, row_id=None):
        """`data["name"]` is this level's own name; the stored name is the full path. The parent is chosen
        when a location is created. Raises ValueError for a third level down."""
        data = dict(data)
        label = (data.get("name") or "").strip()
        with self._lock:
            if row_id is None:
                parent_id = data.get("parent_id")
            else:
                cur = self.location(row_id)
                parent_id = cur["parent_id"] if cur else None
                label = label or (cur["label"] if cur else "")
            parent = None
            if parent_id is not None:
                parent = self.location(parent_id)
                if parent is None:
                    raise ValueError("unknown parent location")
                if self._location_depth(parent_id) >= MAX_DEPTH:
                    raise ValueError("locations go two levels below a room, vehicle or bag, no deeper")
                top = next(l for l in self.locations() if l["id"] == parent_id)  # parent already carries its top's settings
                data.update({f: top[f] for f in ("kind", "person", "people", "counts_home")})
            if PATH_SEP.strip() in label:
                raise ValueError(f"a location name can't contain '{PATH_SEP.strip()}'")
            data.update(label=label, parent_id=parent_id, name=(parent["name"] + PATH_SEP + label) if parent else label)
            new_id = self._save("locations", LOCATION_FIELDS, data, row_id, stamp=True)
            if row_id is not None:
                self._refresh_location_tree(row_id)
            return new_id

    def _refresh_location_tree(self, row_id):
        """After a rename or type change: rebuild the paths and copy the top-level settings below it."""
        row = self._conn.execute("SELECT * FROM locations WHERE id=?", (row_id,)).fetchone()
        for kid in self._conn.execute("SELECT id, label FROM locations WHERE parent_id=?", (row_id,)).fetchall():
            self._conn.execute("UPDATE locations SET name=?, kind=?, person=?, people=?, counts_home=?, updated_at=? "
                               "WHERE id=?", (row["name"] + PATH_SEP + kid["label"], row["kind"], row["person"],
                                              row["people"], row["counts_home"], _now(), kid["id"]))
            self._refresh_location_tree(kid["id"])
        self._conn.commit()

    def delete_location(self, row_id):
        """Items in it are kept, with no location. A location with sub-locations can't be removed."""
        with self._lock:
            if self._conn.execute("SELECT 1 FROM locations WHERE parent_id=?", (row_id,)).fetchone():
                raise ValueError("remove its sub-locations first")
            self._conn.execute("UPDATE items SET location_id=NULL, updated_at=? WHERE location_id=?",
                               (_now(), row_id))
            row = self._conn.execute("SELECT uid FROM locations WHERE id=?", (row_id,)).fetchone()
            if row and row["uid"]:
                self._tombstone("location", row["uid"])
            self._conn.execute("DELETE FROM locations WHERE id=?", (row_id,))
            self._conn.commit()

    # sites
    def sites(self):
        rows = self._rows("SELECT * FROM sites ORDER BY CASE kind WHEN 'home' THEN 0 ELSE 1 END, name")
        for r in rows:
            r["hazards"] = json.loads(r["hazards"] or "{}")
            r["alerts"] = json.loads(r["alerts"] or "[]")
        return rows

    def site(self, row_id):
        rows = [s for s in self.sites() if s["id"] == row_id]
        return rows[0] if rows else None

    def save_site(self, data, row_id=None):
        return self._save("sites", SITE_FIELDS, data, row_id)

    def set_site_hazards(self, row_id, hazards):
        self._exec("UPDATE sites SET hazards=?, hazards_at=? WHERE id=?", (json.dumps(hazards), _now(), row_id))

    def set_site_alerts(self, row_id, alerts, error=""):
        if error:  # keep the last good alerts, flagged with when they were fetched
            self._exec("UPDATE sites SET alerts_error=? WHERE id=?", (error, row_id))
        else:
            self._exec("UPDATE sites SET alerts=?, alerts_at=?, alerts_error='' WHERE id=?",
                       (json.dumps(alerts), _now(), row_id))

    def delete_site(self, row_id):
        self._exec("DELETE FROM sites WHERE id=?", (row_id,))

    # documents
    def documents(self):
        return self._rows("SELECT * FROM documents ORDER BY priority, doc_type, label")

    def save_document(self, data, row_id=None):
        return self._save("documents", DOC_FIELDS, {**data, "updated_at": _now()}, row_id)

    def delete_document(self, row_id):
        self._exec("DELETE FROM documents WHERE id=?", (row_id,))
        self.set_raw("documents_changed_at", _now())

    def documents_changed_at(self):
        """Newest change to the document list (stale-archive check on nodes)."""
        rows = self._rows("SELECT MAX(updated_at) AS t FROM documents")
        return max(rows[0]["t"] or "", self.get_raw("documents_changed_at", "") or "")

    # backups
    def backup_jobs(self):
        return self._rows("SELECT * FROM backup_jobs ORDER BY name")

    def backup_job(self, row_id):
        rows = self._rows("SELECT * FROM backup_jobs WHERE id=?", (row_id,))
        return rows[0] if rows else None

    def save_backup_job(self, data, row_id=None):
        return self._save("backup_jobs", BACKUP_FIELDS, data, row_id)

    def record_backup_run(self, row_id, when, status):
        self._exec("UPDATE backup_jobs SET last_run=?, last_status=? WHERE id=?",
                   (when, status, row_id))

    def delete_backup_job(self, row_id):
        self._exec("DELETE FROM backup_jobs WHERE id=?", (row_id,))
