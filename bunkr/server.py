"""Local web server. Binds to 127.0.0.1 by default: it can read your files and write drives.

Requests must name an allowed host. --allow-host adds one, e.g. a hostname served
through a Cloudflare Tunnel; only do that with a login (Cloudflare Access) in front."""

import argparse
import base64
import html
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import urllib.error
import uuid
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import brief, flights, homeassistant, mesh, meshbot, news, checkins, chirp, family, field, notify, situation, maps as mapspack, mcp, mcp_tools, radioplan, radio, releases, scenarios
from . import __version__, audit, auth, backup, localwatch, mailer, renewals, selfupdate, homelayout, builder, dbcheck, kiwix, mfa, vault, medical, power, quickadd, drives, export, gaps, hazards, kits, library, manifest, nodes, planner, sites, sync
from .depot import Depot
from .db import COUNTS_HOME_DEFAULT, LOCATION_KINDS, SITE_KINDS, Store, guess_kind

STATIC = Path(__file__).parent / "static"
# served without a login: the login page, and the icons/manifest a phone fetches when installing the app
PUBLIC_STATIC = ("/login.html", "/style.css", "/login.js", "/passkey.js", "/favicon.ico", "/manifest.webmanifest", "/logo.jpg", "/sw.js")
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("image/x-icon", ".ico")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def host_allowed(host_header, allowed=None):
    """True if a Host header (with or without a port) names an allowed host."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host in (ALLOWED_HOSTS if allowed is None else allowed)

REQUIRE_LOGIN = False   # --require-login: every page and API call needs a session
PUBLIC_URL = ""          # --public-url: the address Claude's connector uses (default: from the request)
OPEN_NETS = []          # --open-net: clients here get the read-only field page without a login
OPEN_API = {"/api/report.html"}  # read-only pages the field page links to
ROLE = "standalone"     # --role hub | pi | laptop | standalone
HUB_URL = ""
# Areas only admins may use: they read local files, write drives or manage the system.
ADMIN_HEADS = {"audit", "audit.csv", "lock", "browse", "documents", "drives", "library", "plan", "build", "cancel", "job", "backups",
               "depot", "nodes", "users", "conflicts", "maps", "chirp", "home", "homeassistant", "mail", "localwatch"}
# what a phone may keep for when there's no connection (the service worker saves these and nothing else)
OFFLINE_HEADS = {"state", "me", "board", "family", "medical", "situation", "brief", "news", "flights", "power", "notices"}
QUIET_CHANGES = ("/api/me/home", "/api/me/news", "/api/news/read", "/api/news/rate", "/api/notices/read")
_pair_failures = []
store = None
depot = None
notifier = None
MESH = {"link": None}  # the MeshCore radio link on this machine (bunkr/mesh.py)
BRIEF = {"enabled": True, "time": "06:30", "tz": "America/Denver", "ai": True,
         "env_file": "", "claude": "claude"}    # --brief-* options; env_file holds CLAUDE_CODE_OAUTH_TOKEN
_brief_lock = threading.Lock()
scheduler = None
job = builder.Job()
depot_job = builder.Job()   # downloads-store fetches, verification, document archive
ALERT_REFRESH = 15 * 60
HAZARD_REFRESH = 30 * 24 * 3600
backup_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _num(value, kind=float, default=0):
    try:
        return kind(value)
    except (TypeError, ValueError):
        return default


def resolve_location(data):
    """location_id from the form, or a location name (CSV, older clients) found or created."""
    raw = data.get("location_id")
    if raw not in (None, "", "null"):
        loc_id = _num(raw, int, None)
        if loc_id is None or not store.location(loc_id):
            raise ApiError(400, "unknown location")
        return loc_id
    name = (data.get("location") or "").strip()
    if not name:
        return None
    for loc in store.locations():
        if loc["name"].lower() == name.lower():
            return loc["id"]
    kind = guess_kind(name)
    return save_location({"name": name, "kind": kind, "counts_home": COUNTS_HOME_DEFAULT[kind]})


def save_location(clean, row_id=None):
    """Store a cleaned location; depth and name clashes come back as plain 400 errors."""
    try:
        return store.save_location(clean, row_id)
    except ValueError as exc:
        raise ApiError(400, str(exc))
    except sqlite3.IntegrityError:
        raise ApiError(400, "there's already a location with that name there")


def clean_location(data):
    name = (data.get("name") or "").strip()
    if not name:
        raise ApiError(400, "location name is required")
    kind = data.get("kind") if data.get("kind") in LOCATION_KINDS else "home"
    counts = data.get("counts_home")
    counts = COUNTS_HOME_DEFAULT[kind] if counts in (None, "") else int(bool(counts) and counts not in ("0", "false"))
    out = {"name": name, "kind": kind, "person": (data.get("person") or "").strip(),
           "people": min(50, max(1, _num(data.get("people"), int, 1))), "counts_home": counts,
           "notes": (data.get("notes") or "").strip()}
    parent = data.get("parent_id")
    if parent not in (None, "", "null"):  # a sub-location (the parent is only chosen when it's created)
        parent = _num(parent, int, None)
        if parent is None or not store.location(parent):
            raise ApiError(400, "unknown parent location")
        out["parent_id"] = parent
    return out


def save_site(data, row_id=None):
    """Create or update a site: geocode the address (or look up the county for typed
    coordinates), then fetch its FEMA hazard profile and NWS alerts."""
    name = (data.get("name") or "").strip()
    if not name:
        raise ApiError(400, "site name is required")
    kind = data.get("kind") if data.get("kind") in SITE_KINDS else "other"
    address = (data.get("address") or "").strip()
    lat, lon = _num(data.get("lat"), float, None), _num(data.get("lon"), float, None)
    old = store.site(row_id) if row_id else None
    fields = {"name": name, "kind": kind, "address": address, "notes": (data.get("notes") or "").strip()}
    moved = not old or old["address"] != address or (lat is not None and (old["lat"], old["lon"]) != (lat, lon))
    if moved:
        try:
            if lat is not None and lon is not None:
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ApiError(400, "coordinates out of range")
                geo = {"lat": lat, "lon": lon, **sites.county_for(lat, lon)}
            elif address:
                geo = sites.geocode(address)
            else:
                raise ApiError(400, "enter an address or coordinates")
        except (LookupError, ValueError) as exc:
            raise ApiError(400, str(exc))
        except ApiError:
            raise
        except Exception as exc:
            if lat is None or lon is None:
                raise ApiError(502, f"geocoder unreachable ({exc}); enter coordinates instead")
            geo = {"lat": lat, "lon": lon, "county_fips": "", "county_name": ""}  # offline: keep coordinates
        fields.update(lat=geo["lat"], lon=geo["lon"], county_fips=geo.get("county_fips", ""),
                      county_name=geo.get("county_name", ""), state=geo.get("state", ""),
                      geocoded_at=datetime.now().isoformat(timespec="seconds"))
    site_id = store.save_site(fields, row_id)
    if moved:
        sites.refresh_site(store, store.site(site_id), hazards, with_hazards=True)
    return store.site(site_id)


def full_state(user=None):
    s = store.settings()
    role = (user or {"role": "admin"})["role"]
    items, docs, locs, site_rows = gaps.visible(store.items(), role), store.documents(), store.locations(), store.sites()
    profile = hazards.home_profile(site_rows)
    analysis = gaps.analyze(s, items, docs, hazard_profile=profile)
    kit_scores = kits.score_all(locs, items, s, profile)
    runs = store._rows("SELECT * FROM scenario_runs WHERE ended_at=''")
    for i in items:
        i["qty_text"] = gaps.quantity_text(i)
    scn = scenarios.overview(items, analysis, kit_scores, profile, runs)
    for x in scn:
        if x["id"] == "power_outage":
            x["power_plan"] = power.outage_list(store._rows("SELECT * FROM power_devices"), power.settings(store)["batteries"])
    return {"scenarios": scn, "settings": s, "items": items, "documents": docs, "locations": locs, "sites": site_rows,
            "backups": store.backup_jobs(), "analysis": analysis, "kits": kit_scores,
            "shopping": kits.shopping_list(analysis, kit_scores, s),
            "alerts": sites.all_alerts(site_rows), "hazard_profile": profile,
            "categories": gaps.CATEGORIES, "essentials": gaps.ESSENTIALS, "checklist": gaps.CHECKLIST,
            "doc_types": gaps.DOC_TYPES, "location_kinds": LOCATION_KINDS, "site_kinds": SITE_KINDS,
            "hazard_names": hazards.HAZARDS,
            "me": user or {"username": "local", "role": "admin"}, "require_login": REQUIRE_LOGIN,
            "ammo": gaps.ammo_summary(items), "magazines": gaps.magazine_summary(items), "subtypes": gaps.SUBTYPES,
            "ammo_types": gaps.AMMO_TYPES, "mag_status": gaps.MAG_STATUS,
            "node_role": ROLE, "hub_url": HUB_URL, "last_node_sync": store.get_raw("last_node_sync", ""), "version": __version__,
            "node_lock": node_lock_state() if LOCK["on"] else None,
            **home_state(user)}


def home_state(user):
    """What the Home screen needs: the family board, who I am, and whether I get the simple view."""
    me = checkins.person_for_user(store, user) if user else None
    role = (user or {"role": "admin"})["role"]
    view = (user or {}).get("view") or ("full" if role == "admin" else "simple")
    places = [{"id": s["id"], "name": s["name"], "kind": s["kind"], "address": s["address"], "lat": s["lat"], "lon": s["lon"]}
              for s in store.sites()]
    top = [i for i in situation.items(store) if i["tier"] in ("flash", "priority")][:5]
    return {"situation_top": top, "board": family_board(), "me_person": {"uid": me["uid"], "name": me["name"], "role": me["role"]} if me else None,
            "can_call": checkins.can_call(store, user) if user else True, "view": view, "places": places,
            "contacts": store._rows("SELECT name, relation, phone, out_of_area FROM contacts ORDER BY out_of_area DESC, name"),
            "news_home": news_home(user), "home_layout": homelayout.for_user(store, user), "home_tiles": [{"id": t, "title": n, "who": w} for t, n, w in homelayout.TILES],
            "last_call": last_call_report(), "med_due": med_due_for(user), "unread": notify.unread(store, (user or {}).get("id")), "backup_status": dbcheck.status(store) if role == "admin" and ROLE not in ("pi", "laptop") else None,
            "library": {"ready": kiwix_ready(), "address": store.get_raw("library_url", "") or "", "port": kiwix.PORT,
                        "home": library_link("")["url"]}}


def home_airport():
    homes = [s for s in store.sites() if s["kind"] == "home" and s["lat"] is not None] or [s for s in store.sites() if s["lat"] is not None]
    return flights.home_airport(homes[0]["lat"], homes[0]["lon"]) if homes else None


def family_board():
    """The family board plus any flights being followed."""
    return checkins.board(store) | {"flights": flights.board(store)}


def news_home(user):
    """For the Home tile: the brief in a line, and this person's top unread stories."""
    rec = brief.latest(store)
    top = news.feed(store, (user or {}).get("id"), unread=True, limit=4, with_items=False)
    if not rec and not store.get_raw("news_refreshed", ""):
        return None
    return {"day": rec.get("day") if rec else "", "lead": brief.one_line(rec) if rec else "",
            "stories": [{k: s[k] for k in ("id", "headline", "area", "place", "topic")} for s in top["stories"]],
            "unread": sum(top["unread_by_area"].values())}


def library_link(host):
    """Where phones open the library: the address an admin set (e.g. a hostname in the Cloudflare tunnel), or this
    machine's own address on port 8080. Through a public hostname with none set, the home-network address is the
    best there is, so it's marked home_only."""
    saved = store.get_raw("library_url", "") or ""
    if saved:
        u = urlparse(saved)
        return {"url": f"{u.scheme}://{u.netloc}{kiwix.ROOT}/", "home_only": False}
    name = (host or "").rsplit(":", 1)[0].strip("[]").lower() if (host or "").count(":") <= 1 else (host or "").strip("[]")
    local = bool(name) and ("." not in name or name.endswith((".local", ".lan", ".home.arpa")) or _is_ip(name))
    if local:
        return {"url": f"http://{name}:{kiwix.PORT}{kiwix.ROOT}/", "home_only": False}
    lan = next((h for h in sorted(ALLOWED_HOSTS) if _is_ip(h) and not ipaddress.ip_address(h).is_loopback), "")
    return {"url": f"http://{lan}:{kiwix.PORT}{kiwix.ROOT}/" if lan else "", "home_only": True}


def _is_ip(s):
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


_kiwix_seen = [0.0, False]


def kiwix_ready():
    """Whether the offline library answers (checked at most once a minute, so Home stays quick)."""
    import time
    if time.time() - _kiwix_seen[0] > 60:
        m = KIWIX["manager"]
        _kiwix_seen[:] = [time.time(), m.running() if m else kiwix.alive(KIWIX["url"], timeout=0.5)]
    return _kiwix_seen[1]


def med_editor(user, person_id):
    """Admins, adults (same rule as family calls) and the person themselves may change a person's medicines."""
    if not user or user.get("role") == "admin" or checkins.can_call(store, user):
        return True
    me = checkins.person_for_user(store, user)
    return bool(me and me["id"] == person_id)


def med_due_for(user):
    """What's due for this login: everything for adults and admins, only their own for others."""
    rows = medical.due(store)
    if user and not checkins.can_call(store, user):
        rows = [r for r in rows if r["user_id"] == user["id"]]
    return rows[:5]


def medical_view(user):
    people = medical.people_with_meds(store)
    for p in people:
        p["can_edit"] = med_editor(user, p["id"])
    return {"people": people, "due": medical.due(store), "lead_days": medical.REFILL_LEAD_DAYS}


def last_call_report():
    """The most recent ended call or drill from the last week, for the Home screen."""
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    rows = store._rows("SELECT uid FROM family_calls WHERE ended_at != '' AND ended_at > ? ORDER BY ended_at DESC LIMIT 1", (week,))
    return checkins.report(store, rows[0]["uid"]) if rows else None


def clean_item(data):
    category = data.get("category", "other")
    if category not in gaps.CATEGORIES:
        raise ApiError(400, "unknown category")
    essential = data.get("essential", "") or ""
    if essential and essential not in gaps.CHECKLIST_IDS:
        raise ApiError(400, "unknown checklist item")
    expires = (data.get("expires") or "").strip()
    if expires and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expires):
        raise ApiError(400, "expiry must be YYYY-MM-DD")
    name = (data.get("name") or "").strip()
    if not name:
        raise ApiError(400, "item name is required")
    return {"name": name, "category": category, "quantity": max(0, _num(data.get("quantity"))),
            "unit": (data.get("unit") or "").strip()[:40], "per_unit": max(0, _num(data.get("per_unit"))),
            "pack_size": min(100000, _num(data.get("pack_size"), float, 1)) if _num(data.get("pack_size"), float, 1) > 0 else 1,
            "pack_unit": (data.get("pack_unit") or "").strip()[:40],
            "size_unit": data.get("size_unit") if data.get("size_unit") in gaps.GAL_PER else "",
            "essential": essential, "location_id": resolve_location(data),
            "expires": expires, "notes": (data.get("notes") or "").strip(),
            "subtype": data.get("subtype") if category == "firearms" and data.get("subtype") in gaps.SUBTYPES else "",
            "caliber": (data.get("caliber") or "").strip()[:40] if category == "firearms" else "",
            "serial": (data.get("serial") or "").strip()[:80] if category == "firearms" else "",
            "ammo_type": data.get("ammo_type") if category == "firearms" and data.get("ammo_type") in gaps.AMMO_TYPES else "",
            "capacity": max(0, _num(data.get("capacity"))) if category == "firearms" else 0,
            "mag_status": data.get("mag_status") if category == "firearms" and data.get("mag_status") in gaps.MAG_STATUS else "",
            "fits": str(data.get("fits") or "")[:64] if category == "firearms" else ""}


def documents_with_sizes():
    out = []
    for d in store.documents():
        exists = os.path.exists(d["path"])
        out.append({**d, "exists": exists, "size": planner.path_size(d["path"]) if exists else 0, "renewal": renewals.status(d)})
    return out


def catalog_meta():
    every = library.entries()
    return {c["id"]: {"tier": c["tier"], "group": c.get("group"), "rank": n} for n, c in enumerate(every)}


# ---------- the offline library reader ----------
KIWIX = {"url": kiwix.URL, "manager": None, "seen": None}


def kiwix_extra():
    return [e for e in (store.get_raw("kiwix_extra", []) or []) if isinstance(e, dict) and e.get("id")]


library.extra_source = lambda: kiwix_extra()


def kiwix_status():
    m = KIWIX["manager"]
    out = m.status() if m else {"managed": False, "running": kiwix.alive(KIWIX["url"], timeout=1)}
    out["address"] = store.get_raw("library_url", "") or ""
    return out


def kiwix_watch():
    """Hub: rebuild Kiwix's book list when the ZIM files in the store change (a download finished, one was removed)."""
    m = KIWIX["manager"]
    if not m:
        return
    lib = depot.root / "library"
    seen = sorted((f.name, f.stat().st_size) for f in lib.glob("*.zim")) if lib.is_dir() else []
    if seen != KIWIX["seen"] or (seen and not m.running()):
        KIWIX["seen"] = seen
        m.refresh()


def node_plan(node):
    budget = int(node["budget_bytes"] or 0)
    if not budget and node["role"] == "hub":
        budget = 0  # the hub holds the whole store
    locked = (node.get("lock") or "") == "on"
    rows = [r for r in depot.rows() if r["kind"] != "plan" or r["path"].endswith(vault.SUFFIX) == locked]
    return nodes.plan(node, rows, catalog_meta(), budget)


# ---------- node lock (Pi, laptop) ----------
LOCK = {"on": False, "vault": None, "key": None, "locked": False, "held": False, "error": "", "fails": [],
        "token_file": "/etc/bunkr/node.token", "sealed": -1, "db": "", "lock": threading.RLock()}
UNLOCK_TRIES = 5          # passphrase attempts per 15 minutes


def node_lock_state():
    if not LOCK["on"]:
        return "off"
    return "locked" if LOCK["locked"] else "on" if LOCK["key"] else "waiting"


def lock_note(msg=""):
    v = LOCK["vault"]
    return {"error": msg or LOCK["error"], "passphrase": bool(v and v.wrapped()), "held": LOCK["held"]}


def _ask_hub(state):
    """The vault key and the passphrase-wrapped copy from the hub, or None (not paired, away from home, hub down)."""
    try:
        tok = sync.read_token(LOCK["token_file"])
        res = sync.Client(tok["hub"], tok["token"], timeout=15).json("POST", "vault", {"lock": state})
    except (OSError, SystemExit, KeyError, ValueError, urllib.error.URLError):
        return None
    if res.get("wrapped"):
        LOCK["vault"].save_wrapped(res["wrapped"])
    return base64.b64decode(res["key"]) if res.get("key") else None


def _open_vault(key):
    """Unlocked: the database comes out of the vault into memory."""
    global store
    with LOCK["lock"]:
        data = LOCK["vault"].load(key)
        store = Store(LOCK["db"], data=data)
        depot.store = store
        LOCK.update(key=key, locked=False, error="", held=False, sealed=store.changes())
        _after_open()
    print("Node lock: unlocked")


def _after_open():
    setup_notifier()
    setup_mesh()
    family.ensure_tasks(store)


def _convert_plain(key):
    """First key on a node that ran unlocked until now: move the database into the vault, remove the plain copies."""
    global store
    with LOCK["lock"]:
        data, _ = store.snapshot()
        LOCK["vault"].save(key, data)
        old = store
        store = Store(LOCK["db"], data=data)
        depot.store = store
        old._conn.close()
        LOCK["vault"].remove(LOCK["vault"].plain_files())
        LOCK.update(key=key, locked=False, error="", sealed=store.changes())
        _after_open()
    print("Node lock: the database is now kept in the vault")


def seal_now(force=False):
    """Write the in-memory database back into the vault if it changed."""
    with LOCK["lock"]:
        if not (LOCK["on"] and LOCK["key"] and store is not None and getattr(store, "in_memory", False)):
            return False
        data, changes = store.snapshot()
        if changes == LOCK["sealed"] and not force:
            return False
        LOCK["vault"].save(LOCK["key"], data)
        LOCK["sealed"] = changes
        return True


def lock_now():
    """Seal and forget the key. Stays locked until someone types the passphrase or BUNKR restarts at home."""
    global store
    with LOCK["lock"]:
        seal_now(force=True)
        LOCK.update(key=None, locked=True, held=True)
        store = None
    print("Node lock: locked by hand")


def unlock_with_passphrase(passphrase):
    """Returns "" when unlocked, or what went wrong."""
    import time
    now_t = time.time()
    LOCK["fails"][:] = [t for t in LOCK["fails"] if now_t - t < 900]
    if len(LOCK["fails"]) >= UNLOCK_TRIES:
        return "Too many tries. Wait 15 minutes."
    wrapped = LOCK["vault"].wrapped()
    if not wrapped:
        return "No passphrase has been set up for this node yet. Bring it home so it can unlock from the hub."
    try:
        key = vault.unwrap(wrapped, passphrase)
        _open_vault(key)
    except vault.VaultError as exc:
        LOCK["fails"].append(now_t)
        return str(exc)
    return ""


def node_lock_tick():
    """Every minute: unlock from the hub when at home (unless locked by hand); first-time conversion; keep the
    passphrase copy fresh; seal recent changes."""
    if not LOCK["on"]:
        return
    if LOCK["locked"]:
        if not LOCK["held"]:
            key = _ask_hub("locked")
            if key:
                try:
                    _open_vault(key)
                except vault.VaultError as exc:
                    LOCK["error"] = f"the hub's key doesn't open this vault ({exc})"
        return
    if not LOCK["key"]:  # still running from the plain database
        key = _ask_hub("waiting")
        if key:
            _convert_plain(key)
        return
    seal_now()


def node_records_sync():
    """With node lock on, record sync runs here (the database is in memory); the sync timer only moves files."""
    if not (LOCK["on"] and LOCK["key"] and store is not None):
        return
    try:
        tok = sync.read_token(LOCK["token_file"])
    except (OSError, SystemExit, ValueError):
        return
    sync.sync_records(sync.Client(tok["hub"], tok["token"]), store, log=print)
    store.set_raw("last_node_sync", manifest.now())
    _ask_hub("on")
    seal_now()


def hub_vault_key():
    """The hub's node-lock key (vault.key next to the database)."""
    return vault.hub_key(Path(store.path).parent)


def majors_pending():
    return store._rows("SELECT * FROM updates WHERE major=1 AND status IN ('pending','held')")


def nodes_view():
    changed = store.documents_changed_at()
    majors = len(majors_pending())
    out = []
    for n in nodes.list_nodes(store):
        p = node_plan(n)
        planned = nodes.plan_files(p)
        st = nodes.status(n, planned, changed, majors)
        out.append({**{k: n[k] for k in ("id", "uid", "name", "role", "budget_bytes", "notes", "last_seen",
                                         "last_sync", "last_sync_status", "created_at", "lock", "lost")},
                    "profile": nodes.profile_for(n), "paired": bool(n["token_hash"]),
                    "plan_bytes": p["used"], "plan_skipped": len(p["skipped"]), "status": st})
    return out


def release_catalog():
    """Firmware and apps, shaped like library rows for the Downloads tab."""
    held = releases.held_versions(depot)
    out = []
    for r in releases.resolve_all():
        hv = held.get(r["id"])
        row = None
        if hv:
            files = [f for f in depot.rows() if f["item_id"] == r["id"] and f["version"] == hv]
            row = {"id": files[0]["id"], "version": hv, "size": sum(f["size"] for f in files),
                   "status": "ok" if all(f["status"] == "ok" for f in files) else "damaged",
                   "path": files[0]["path"].rsplit("/", 1)[0], "files": len(files)}
        out.append({"kind": r["kind"], "id": r["id"], "label": r["label"], "tier": 4 if r["kind"] == "firmware" else 5,
                    "group": None, "latest": r["latest"], "latest_size": r["size"], "online": r["online"],
                    "held": row, "update": bool(hv and releases.newer(r["latest"], hv)), "board": r.get("board")})
    return out


def chirp_nightly():
    """Fresh CHIRP source for the Pi and laptop (daily; GitHub isn't behind a browser check)."""
    try:
        chirp.fetch_source(depot)
    except Exception as exc:
        print(f"CHIRP source check failed: {exc}")


def depot_view():
    libs, tools = library.resolve_all()
    return {"usage": depot.usage(), "catalog": depot.catalog(libs, tools) + release_catalog(), "files": depot.rows(),
            "job": depot_job.snapshot(), "updates": store._rows("SELECT * FROM updates ORDER BY found_at DESC LIMIT 50"),
            "documents_changed_at": store.documents_changed_at(), "auto_update": store.get_raw("auto_update", True),
            "reader": kiwix_status(), "extra_books": kiwix_extra()}


def fetch_into_depot(jb, wanted):
    """Job body: download chosen libraries/tools into the store."""
    libs, tools = library.resolve_all()
    by = {("library", l["id"]): l for l in libs} | {("tool", t["id"]): t for t in tools}
    by |= {(r["kind"], r["id"]): r for r in releases.resolve_all() if r["online"]}
    todo = [by[(w["kind"], w["id"])] | {"_kind": w["kind"]} for w in wanted if (w["kind"], w["id"]) in by]
    with jb.lock:
        jb.bytes_total = sum(t["size"] for t in todo)
    base = 0
    for t in todo:
        if jb.cancelled:
            raise InterruptedError
        jb.step = f"Fetching {t.get('file') or t.get('label') or t['id']}"
        jb.say(jb.step)

        def progress(n, base=base):
            with jb.lock:
                jb.bytes_done = base + n
        for attempt in range(3):
            try:
                if t["_kind"] in ("firmware", "app"):
                    v = releases.fetch(depot, t, progress, lambda: jb.cancelled)
                    jb.say(f"{t['label']}: version {v} stored, checksums verified")
                else:
                    row = (depot.fetch_library if t["_kind"] == "library" else depot.fetch_tool)(t, progress, lambda: jb.cancelled)
                    jb.say(f"{row['path']}: stored, SHA-256 {row['sha256'][:12]}…")
                break
            except InterruptedError:
                raise
            except Exception as exc:
                jb.say(f"{t['id']}: attempt {attempt + 1} failed ({exc})")
        base += t["size"]
    jb.say("Done.")


def build_archive_into_depot(jb, passphrase):
    """Job body: encrypt registered documents into the store for nodes to sync."""
    docs = [d for d in documents_with_sizes() if d["exists"]]
    if not docs:
        raise RuntimeError("no documents registered")
    out = depot.root / "documents"
    out.mkdir(parents=True, exist_ok=True)
    jb.step = f"Encrypting {len(docs)} document entries"
    jb.say(jb.step)
    names = builder._encrypt_documents(jb, docs, passphrase, out, split=False, extra=archive_extras())
    version = names[0].split(".7z")[0].removeprefix("documents-")
    for n in names:
        depot.add_file("documents", "archive", out / n, version=version)
    keep = {version}
    olds = sorted({r["version"] for r in depot.rows() if r["kind"] == "documents"} - keep)
    keep.update(olds[-1:])  # one previous archive as a fallback, like kit drives
    for r in depot.rows():
        if r["kind"] == "documents" and r["version"] not in keep:
            depot.remove(r)
    jb.say(f"Archive stored: {', '.join(names)}")


def verify_depot(jb):
    jb.step = "Verifying checksums of every stored file"
    jb.say(jb.step)
    bad = depot.verify(lambda: jb.cancelled)
    jb.say(f"{len(bad)} problem(s): " + ", ".join(f"{b['path']} {b['status']}" for b in bad) if bad else "All files match their checksums.")


def drive_check(jb, mount, verify):
    root = builder.kit_paths(mount)["root"]
    data = manifest.read(root)
    if not data:
        raise RuntimeError("no BUNKR manifest on this drive (built before manifests existed? rebuild it once)")
    libs, tools = library.resolve_all()
    rep_ = nodes.drive_report(data, depot.catalog(libs, tools), depot.rows(), store.documents_changed_at())
    jb.say(f"{rep_['node'].get('name', 'drive')}: {rep_['current']} file(s) current, {len(rep_['out_of_date'])} out of date")
    for f in rep_["out_of_date"]:
        jb.say(f"  OUT OF DATE {f['path']} ({f.get('version') or '?'} -> {f['latest']}): {f['reason']}")
    if verify:
        jb.step = "Verifying checksums on the drive"
        res = manifest.verify(root, data, lambda: jb.cancelled)
        jb.say(f"Checked {res['checked']} files: {len(res['damaged'])} damaged, {len(res['missing'])} missing")
        for pth in res["damaged"]:
            jb.say(f"  DAMAGED {pth}")
        for pth in res["missing"]:
            jb.say(f"  MISSING {pth}")
    node = nodes.ensure_drive_node(store, data["node"]["uid"], data["node"].get("name") or Path(mount).name)
    fields = {"manifest": json.dumps(data), "last_seen": manifest.now()}
    if verify:
        rem = json.loads(node["reminders"] or "{}") | {"verify": datetime.now().date().isoformat()}
        fields["reminders"] = json.dumps(rem)
    nodes.set_fields(store, node["id"], **fields)
    jb.result = rep_


def record_drive_manifest(data):
    node = nodes.ensure_drive_node(store, data["node"]["uid"], data["node"].get("name", "Kit drive"))
    nodes.set_fields(store, node["id"], manifest=json.dumps(data), last_seen=manifest.now(),
                     last_sync=manifest.now(), last_sync_status="kit built")


def is_major(current, latest):
    """Semantic versions only: 1.x -> 2.x is a major jump. Dates never are."""
    import re
    a, b = re.match(r"v?(\d+)\.", current or ""), re.match(r"v?(\d+)\.", latest or "")
    return bool(a and b and int(b.group(1)) > int(a.group(1)))


def nightly_updates():
    """Check every held library and tool for a newer version; fetch minor updates
    automatically, hold major version jumps for approval."""
    library._cache.clear()
    libs, tools = library.resolve_all()
    now = manifest.now()
    wanted = []
    for c in depot.catalog(libs, tools) + release_catalog():
        h = c["held"]
        if not h:
            continue
        latest = c["latest"]
        if c["kind"] in ("firmware", "app") and not releases.newer(latest, h["version"]):
            continue
        if c["kind"] == "tool":
            try:
                latest, _size = depot.tool_version(next(t for t in tools if t["id"] == c["id"]))
            except Exception:
                continue
        if not latest or latest <= h["version"]:
            continue
        major = is_major(h["version"], latest)
        store._exec("INSERT OR IGNORE INTO updates(kind, item_id, label, current, latest, major, found_at, status) "
                    "VALUES(?,?,?,?,?,?,?,?)", (c["kind"], c["id"], c["label"], h["version"], latest, int(major), now,
                                                "held" if major else "pending"))
        if not major and store.get_raw("auto_update", True):
            wanted.append({"kind": c["kind"], "id": c["id"]})
    if wanted and depot_job.snapshot()["state"] != "running":
        depot_job.start(fetch_into_depot, wanted)
        depot_job.thread.join()
        snap = depot_job.snapshot()
        for w in wanted:
            store._exec("UPDATE updates SET status=?, applied_at=? WHERE kind=? AND item_id=? AND status='pending'",
                        ("applied" if snap["state"] == "done" else "failed", manifest.now(), w["kind"], w["id"]))


CHANNEL_FIELDS = ("name", "grp", "service", "rx_mhz", "duplex", "offset_mhz", "tone_mode", "tone_hz", "dtcs",
                  "mode", "power", "in_plan", "notes", "sort", "tx_tone", "rx_tone", "slot", "system", "region",
                  "status", "source", "scan", "watch", "short", "rx_only", "wiresx", "radios")
RADIO_FIELDS = ("name", "model", "kind", "service", "chirp_driver", "notes", "profile", "owner")
CHANNEL_STATUS = ("", "verified", "confirm", "hold", "down")


def clean_channel(data):
    name = (data.get("name") or "").strip()
    rx = _num(data.get("rx_mhz"), float, 0)
    if not name or not 0.1 < rx < 3000:
        raise ApiError(400, "a channel needs a name and a receive frequency in MHz")
    duplex = data.get("duplex", "") if data.get("duplex", "") in ("", "+", "-", "split", "off") else ""
    tone = data.get("tone_mode", "") if data.get("tone_mode", "") in ("", "Tone", "TSQL", "DTCS") else ""
    pf = plan_fields(data, tone, data)
    legacy_tone = legacy_tone_from(pf["tx_tone"], pf["rx_tone"])
    return {"name": name[:20], "grp": (data.get("grp") or "Family").strip()[:30],
            "service": data.get("service") if data.get("service") in radio.SERVICES else "other",
            "rx_mhz": rx, "duplex": duplex, "offset_mhz": max(0.0, _num(data.get("offset_mhz"), float, 0)),
            "tone_mode": tone, "tone_hz": _num(data.get("tone_hz"), float, 88.5) or 88.5,
            "dtcs": _num(data.get("dtcs"), int, 23) or 23, "mode": (data.get("mode") or "FM")[:6],
            "power": (data.get("power") or "")[:30], "in_plan": 1 if data.get("in_plan") in (True, 1, "1", "on", "true") else 0,
            "notes": (data.get("notes") or "").strip()[:200], "sort": _num(data.get("sort"), int, 500),
            **plan_fields(data, tone, data)} | legacy_tone


def legacy_tone_from(tx, rx):
    tk, tv = radioplan.parse_tone(tx)
    rk, rv = radioplan.parse_tone(rx)
    if tk == "dcs":
        return {"tone_mode": "DTCS", "dtcs": tv[0]}
    if tk == "ctcss":
        return {"tone_mode": "TSQL" if rk == "ctcss" and rv == tv else "Tone", "tone_hz": tv}
    return {"tone_mode": "", "tone_hz": 88.5, "dtcs": 23}


def _flag(v, default=0):
    if v in (None, ""):
        return default
    return 1 if v in (True, 1, "1", "on", "true", "yes") else 0


def plan_fields(data, tone_mode, raw):
    """The master-plan fields of a channel (per-direction tones, slot, tags)."""
    try:
        tx, rx = radioplan.norm_tone(data.get("tx_tone")), radioplan.norm_tone(data.get("rx_tone"))
    except ValueError as exc:
        raise ApiError(400, str(exc))
    if not tx and not rx and tone_mode:  # older forms send the single tone setting
        hz, code = _num(raw.get("tone_hz"), float, 88.5) or 88.5, _num(raw.get("dtcs"), int, 23) or 23
        tx = f"D{code:03d}N" if tone_mode == "DTCS" else f"{hz:.1f}"
        rx = tx if tone_mode in ("TSQL", "DTCS") else ""
    slot = data.get("slot")
    slot = None if slot in (None, "") else _num(slot, int, None)
    if slot is not None and not 0 <= slot <= 999:
        raise ApiError(400, "slot must be 0-999")
    radios = data.get("radios") or ""
    if isinstance(radios, list):
        radios = ",".join(r for r in radios if r in radioplan.PROFILES)
    return {"tx_tone": tx, "rx_tone": rx, "slot": slot, "system": (data.get("system") or "").strip()[:30],
            "region": (data.get("region") or "").strip()[:30],
            "status": data.get("status") if data.get("status") in CHANNEL_STATUS else "",
            "source": (data.get("source") or "").strip()[:120], "scan": _flag(data.get("scan"), 1),
            "watch": _flag(data.get("watch")), "short": (data.get("short") or "").strip()[:16],
            "rx_only": _flag(data.get("rx_only")), "wiresx": _flag(data.get("wiresx")), "radios": radios}


def plan_channels():
    rows = store._rows("SELECT * FROM channels WHERE slot IS NOT NULL ORDER BY slot")
    for r in rows:
        r["radios"] = [x for x in (r.get("radios") or "").split(",") if x]
    return rows


def radio_extras(radio_id):
    rows = store._rows("""SELECT c.*, rc.slot AS slot FROM radio_channels rc JOIN channels c ON c.id = rc.channel_id
                          WHERE rc.radio_id=? AND c.slot IS NULL ORDER BY rc.slot""", (radio_id,))
    for r in rows:
        r["radios"] = []
    return rows


def radio_plan(r):
    return radioplan.build(r, plan_channels(), radio_extras(r["id"]))


def channels_all():
    return store._rows("SELECT * FROM channels ORDER BY sort, rx_mhz, name")


def radio_channels(radio_id):
    return store._rows("""SELECT c.*, rc.slot FROM radio_channels rc JOIN channels c ON c.id = rc.channel_id
                          WHERE rc.radio_id=? ORDER BY rc.slot""", (radio_id,))


def save_row(table, fields, data, row_id=None):
    values = {f: data[f] for f in fields if f in data}
    if row_id is None:
        return store._exec(f"INSERT INTO {table}({', '.join(values)}) VALUES({', '.join('?' for _ in values)})",
                           tuple(values.values()))
    store._exec(f"UPDATE {table} SET {', '.join(f'{c}=?' for c in values)} WHERE id=?", (*values.values(), row_id))
    return row_id


def radio_view():
    radios = store._rows("SELECT * FROM radios ORDER BY kind, name")
    for r in radios:
        r["channel_ids"] = [c["id"] for c in radio_channels(r["id"])]
        r["images"] = store._rows("SELECT id, path, version, size, fetched_at FROM depot_files "
                                  "WHERE kind='radio' AND item_id=? ORDER BY version DESC", (f"image-{r['id']}",))
    fw = [f for f in depot.rows() if f["kind"] == "firmware"] if depot else []
    apps = [f for f in depot.rows() if f["kind"] == "app"] if depot else []
    for r in radios:
        if r.get("profile"):
            mems, issues = radio_plan(r)
            r["plan"] = {"count": len(mems), "issues": issues}
    return {"radios": radios, "channels": channels_all(), "kinds": radio.KINDS, "services": radio.SERVICES,
            "profiles": {k: {"label": v["label"], "first": v["first"], "last": v["last"], "chirp": v["chirp"],
                             "format": v["format"]} for k, v in radioplan.PROFILES.items()},
            "boards": releases.BOARDS, "meshcore_types": {k: v[1] for k, v in releases.MESHCORE_TYPES.items()},
            "firmware": fw, "apps": apps, "flashing": radio.FLASHING}


def _slug(text):
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "radio"


def publish_generated():
    """Radio pack files (CHIRP CSVs, channel cards, reference sheet) into the store so nodes
    carry them offline. Unchanged files are left alone."""
    import hashlib
    import re
    full = {"radio/reference.html": radio.reference_html()}
    for r in store._rows("SELECT * FROM radios"):
        base = f"radio/{_slug(r['name'])}-{r['id']}"
        if r.get("profile"):
            mems, issues = radio_plan(r)
            fname, text = radioplan.radio_file(r, mems)
            full[f"{base}-{fname.rsplit('-', 1)[-1]}"] = text
            full[f"{base}-card.html"] = radioplan.card_html(r, mems, issues)
            continue
        chans = radio_channels(r["id"])
        full[f"{base}-chirp.csv"] = radio.chirp_csv(chans)
        full[f"{base}-card.html"] = radio.card_html(r, chans)
    full.update(_published_extras())
    keep = set(full) | {rel + vault.SUFFIX for rel in full if rel.startswith("plan/")}
    for rel, text in full.items():
        # "printed <date>" lines change every day; leave them out when deciding whether a file changed
        digest = hashlib.sha256(re.sub(r"[Pp]rinted \d{4}-\d{2}-\d{2}( \d\d:\d\d)?", "", text).encode()).hexdigest()
        row = depot.by_path(rel)
        sealed = rel + vault.SUFFIX if rel.startswith("plan/") else None
        if row and row["source_url"] == digest and (depot.root / rel).exists() and (not sealed or (depot.root / sealed).exists()):
            continue
        dest = depot.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        kind = "plan" if rel.startswith("plan/") else "radio"
        version = datetime.now().strftime("%Y%m%d%H%M")
        depot.record(kind, "generated", rel, version, dest.stat().st_size, manifest.sha256_file(dest), digest)
        if sealed:  # the copy locked nodes carry
            vault.seal_file(hub_vault_key(), dest, depot.root / sealed)
            depot.record(kind, "generated", sealed, version, (depot.root / sealed).stat().st_size,
                         manifest.sha256_file(depot.root / sealed), digest)
    for row in depot.rows():  # files for radios that were deleted
        if row["item_id"] == "generated" and row["path"] not in keep:
            depot.remove(row)


def _published_extras():
    """The family plan travels to the Pi and laptop as a plain page (behind the node's login)."""
    return {"plan/family-plan.html": family_plan(), "plan/medical-cards.html": medical_cards()}


def medical_cards():
    info = store.get_raw("plan_info", {}) or {}
    return medical.cards_html(medical.people_with_meds(store), store._rows("SELECT * FROM contacts ORDER BY out_of_area DESC, name"),
                              info.get("household_name") or "Our family")


PEOPLE_FIELDS = ("name", "role", "phone", "call_sign", "gmrs_call", "mesh_name", "blood_type", "allergies", "medications",
                 "conditions", "doctor", "notes", "devices", "insurance", "pharmacy")
CONTACT_FIELDS = ("name", "relation", "phone", "email", "address", "out_of_area", "notes")


def archive_extras():
    """Generated files that go inside the encrypted document archive."""
    out = {"Family plan.html": family_plan(), "Medical cards.html": medical_cards()}
    details = renewals.details_html(store.documents(), (store.get_raw("plan_info", {}) or {}).get("household_name") or "Household")
    if details:
        out["Document details.html"] = details
    record = export.firearms_html(store.items(), (store.get_raw("plan_info", {}) or {}).get("household_name") or "Household")
    if record:
        out["Firearms record.html"] = record
    return out


def family_plan():
    info = store.get_raw("plan_info", {}) or {}
    return family.plan_html(store._rows("SELECT * FROM people ORDER BY role, name"),
                            store._rows("SELECT * FROM contacts ORDER BY out_of_area DESC, name"),
                            store.sites(), channels_all(), info, info.get("household_name") or "Our family")


def node_reminders():
    return [{"id": n["id"], "name": n["name"], "reminders": nodes.reminders_due(n)} for n in nodes.list_nodes(store)]


def family_view(st=None, admin=True):
    st = st or full_state()
    people = store._rows("SELECT * FROM people ORDER BY role, name")
    for p in people:
        p["phone_status"] = family.phone_status(p)
    tasks = store._rows("SELECT * FROM tasks ORDER BY title")
    for t in tasks:
        t["due"] = family.next_due(t).isoformat()
    return {"people": people, "contacts": store._rows("SELECT * FROM contacts ORDER BY out_of_area DESC, name"),
            "tasks": tasks, "calendar": family.calendar(tasks, st["analysis"]["expiring"] + st["analysis"]["expired"], node_reminders(),
                                                        renewals=renewals.calendar_entries(store) if admin else ()),
            "plan_info": store.get_raw("plan_info", {}) or {}, "phone_checks": family.PHONE_CHECKS}


def clean_text_fields(data, fields, required="name"):
    out = {}
    for f in fields:
        v = data.get(f, "")
        out[f] = (1 if v in (True, 1, "1", "on", "true") else 0) if f == "out_of_area" else str(v or "").strip()[:500]
    if not out.get(required):
        raise ApiError(400, f"{required} is required")
    return out


VIEWER_PAGE = Path(__file__).resolve().parents[1] / "deploy" / "pi" / "maps" / "index.html"


def build_maps_job(jb):
    def prog(step, done, total):
        jb.step = step
        with jb.lock:
            jb.bytes_done, jb.bytes_total = done, total
    jb.step = "Preparing the maps pack"
    mapspack.build(depot, store, store.sites(), progress=prog, cancelled=lambda: jb.cancelled, log=jb.say)
    jb.say("Maps pack ready. Open it from Sites & alerts, Maps; nodes get it on their next sync.")


def maps_refresh():
    """Monthly, or after sites change: re-cut the maps pack if one was built before."""
    if mapspack.needs_rebuild(store, store.sites()) and depot_job.snapshot()["state"] != "running":
        depot_job.start(build_maps_job)


def kit_inputs(mount):
    info = drives.probe(mount)
    if not info["writable"]:
        raise ApiError(400, f"{info['mount']} is not writable")
    settings = store.settings()
    libs, tools = library.resolve_all(set(settings["kiwix_disabled"]))
    docs = documents_with_sizes()
    existing = builder.existing_files(info["mount"])
    the_plan = planner.plan({**info, "max_file": drives.max_file_size(info["fstype"])},
                            docs, tools, libs, existing)
    return info, the_plan, docs, tools, libs, settings


def browse(path):
    p = Path(os.path.expanduser(path or "~")).resolve()
    if not p.is_dir():
        p = p.parent
    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if child.name.startswith("."):
                continue
            try:
                entries.append({"name": child.name, "path": str(child), "dir": child.is_dir(),
                                "size": child.stat().st_size if child.is_file() else None})
            except OSError:
                continue
    except PermissionError:
        raise ApiError(403, f"no permission to read {p}")
    return {"path": str(p), "parent": str(p.parent), "entries": entries}


def run_backup(job_id):
    row = store.backup_job(job_id)
    if not row:
        raise ApiError(404, "no such backup job")
    if not backup_lock.acquire(blocking=False):
        raise ApiError(409, "another backup is running")
    try:
        status = backup.run(row)
    except Exception as exc:
        status = f"FAILED: {exc}"
    finally:
        backup_lock.release()
    store.record_backup_run(job_id, datetime.now().isoformat(timespec="minutes"), status)
    return status


class Handler(BaseHTTPRequestHandler):
    server_version = "BUNKR"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for k, v in getattr(self, "_extra", []):
            self.send_header(k, v)
        self._extra = []
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, mutating):
        # Block DNS-rebinding (foreign Host) and cross-site requests from other tabs.
        # With logins on, a rebinding page has no session cookie for its own host name,
        # so any Host is accepted (nodes on DHCP keep working when their address changes).
        if not REQUIRE_LOGIN and not host_allowed(self.headers.get("Host")):
            raise ApiError(403, "bad host")
        if mutating and self.headers.get("X-Bunkr") != "1":
            raise ApiError(403, "missing request header")

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            raise ApiError(400, "invalid JSON")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _secure(self):
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _session(self):
        if not REQUIRE_LOGIN:
            return {"id": 0, "username": "local", "role": "admin"}
        return auth.session_user(store, auth.token_from_cookie(self.headers.get("Cookie")))

    def _open_client(self):
        """On the field Wi-Fi the Wi-Fi password is the gate: read-only pages need no login."""
        if not OPEN_NETS:
            return False
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in n for n in OPEN_NETS)

    def _redirect(self, where):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method):
        url = urlparse(self.path)
        self._extra = []
        self.user = None
        try:
            if LOCK["on"] and LOCK["locked"]:
                return self._locked(method, url)
            if url.path.startswith("/api/sync/"):
                return self._sync(method, url.path[len("/api/sync/"):])
            if url.path == "/mcp" or url.path.startswith(("/mcp/", "/.well-known/")):
                return self._mcp(method, url)
            if url.path == "/field/checkin" and method == "POST":
                return self._field_checkin()
            self._guard(method != "GET")
            if url.path == "/api/login" and method == "POST":
                return self._login()
            if url.path in ("/api/login/code", "/api/login/passkey-options", "/api/login/passkey") and method == "POST":
                return self._login(url.path.rsplit("/", 1)[1])
            user = self._session()
            if user and user.get("scope") == "setup" and not (
                    url.path in ("/api/me", "/api/logout") or url.path.startswith(("/api/me/two-step", "/api/me/passkeys"))):
                if url.path.startswith("/api/"):
                    raise ApiError(403, "finish setting up two-step login first")
                if url.path not in PUBLIC_STATIC and not url.path.startswith("/icons/"):
                    return self._redirect("/login.html?setup=1")
            if not url.path.startswith("/api/"):
                if url.path in PUBLIC_STATIC or (url.path.startswith("/icons/") and ".." not in url.path):
                    return self._static(url.path)
                if url.path == "/field" or (not user and url.path == "/" and self._open_client()):
                    return self._send(200, field.page(depot.root, self.headers.get("Host"),
                                                      checkins.board(store) if (user or self._open_client()) else None,
                                                      sent=parse_qs(url.query).get("sent", [""])[0]).encode(),
                                      "text/html; charset=utf-8")
                if url.path == kiwix.ROOT or url.path.startswith(kiwix.ROOT + "/"):  # the library has a port of its own
                    link = library_link(self.headers.get("Host"))["url"]
                    return self._redirect(link + url.path[len(kiwix.ROOT) + 1:] if link else "/")
                if not user and not (method == "GET" and url.path.startswith("/store/") and self._open_client()):
                    return self._redirect("/login.html")
                if url.path.startswith("/store/"):
                    return self._store_file(url.path[len("/store/"):])
                return self._static(url.path)
            if url.path == "/api/logout" and method == "POST":
                if user:
                    audit.record(store, user, "logout", "login", "", self._client_ip())
                auth.logout(store, auth.token_from_cookie(self.headers.get("Cookie")))
                self._extra.append(("Set-Cookie", auth.cookie_header("", self._secure(), 0)))
                return self._send(200, {"ok": True})
            if not user and method == "GET" and url.path in OPEN_API and self._open_client():
                user = {"id": 0, "username": "field", "role": "family"}
            if not user:
                raise ApiError(401, "please log in")
            head = url.path.strip("/").split("/")[1] if url.path.count("/") >= 2 else ""
            self.user = user
            if head in ADMIN_HEADS and user["role"] != "admin":
                raise ApiError(403, "only an admin can do that")
            self._audit_what = None
            before = self._sensitive_item(url.path)
            result = self._api(method, url.path, parse_qs(url.query))
            self._audit(method, url.path, head, result, before)
            if method == "GET" and head in OFFLINE_HEADS and not isinstance(result, tuple) and "/two-step" not in url.path \
                    and "/passkeys" not in url.path:
                self._extra += [("X-Bunkr-Keep", "1"), ("X-Bunkr-Drop", ",".join(sorted(gaps.SENSITIVE)))]
            if isinstance(result, tuple):
                self._send(200, result[0].encode(), result[1])
            else:
                self._send(200, result)
        except ApiError as exc:
            if exc.status == 403 and getattr(self, "user", None) is not None:
                audit.record(store, self.user, "denied", url.path.strip("/").split("/")[1] if url.path.count("/") >= 2 else "",
                             f"{method} {url.path}", self._client_ip())
            self._send(exc.status, {"error": str(exc)})
        except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # keep the server alive; show the message in the UI
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _locked(self, method, url):
        """A locked node: public things (maps, library link, radio files, apps) and the unlock form. Nothing private."""
        path = url.path
        if method == "POST" and path == "/unlock":
            origin = self.headers.get("Origin") or ""
            if origin and urlparse(origin).netloc != (self.headers.get("Host") or ""):
                raise ApiError(403, "cross-site form")
            form = {k: v[0] for k, v in parse_qs(self._read_raw(2000).decode("utf-8", "replace")).items()}
            msg = unlock_with_passphrase(form.get("passphrase", ""))
            if msg:
                return self._send(200, field.page(depot.root, self.headers.get("Host"), locked=lock_note(msg)).encode(),
                                  "text/html; charset=utf-8")
            return self._redirect("/")
        if method == "GET" and (path in PUBLIC_STATIC or (path.startswith("/icons/") and ".." not in path)):
            return self._static(path)
        if method == "GET" and path.startswith("/store/") and not path.startswith("/store/plan/") and self._open_client():
            return self._store_file(path[len("/store/"):])
        if path.startswith("/api/"):
            return self._send(423, {"error": "This BUNKR is locked. Unlock it on its home page."})
        if method == "GET" and path in ("/", "/field", "/login.html"):
            return self._send(200, field.page(depot.root, self.headers.get("Host"), locked=lock_note()).encode(),
                              "text/html; charset=utf-8")
        return self._redirect("/")

    def _sensitive_item(self, path):
        """For /api/items/<id>...: whether that item is a firearm, looked up before a change can delete it."""
        m = re.match(r"/api/items/(\d+)", path)
        if not m:
            return False
        r = store._rows("SELECT category FROM items WHERE id=?", (int(m.group(1)),))
        return bool(r and r[0]["category"] in gaps.SENSITIVE)

    def _audit(self, method, path, head, result, sensitive_before):
        ip = self._client_ip()
        if method != "GET":
            if path.rstrip("/").startswith(QUIET_CHANGES):  # one person's own screen settings, not household data
                return
            what = self._audit_what or head
            if head == "items":
                new_id = result.get("id") if isinstance(result, dict) else None
                after = new_id and store._rows("SELECT category FROM items WHERE id=?", (new_id,))
                if sensitive_before or (after and after[0]["category"] in gaps.SENSITIVE):
                    what = "firearms"
            audit.record(store, self.user, "change", what, f"{method} {path}", ip)
            return
        if head == "state" and self.user["role"] == "admin" and any(i["category"] in gaps.SENSITIVE for i in store.items()):
            audit.viewed(store, self.user, "firearms", "inventory with firearms", ip)
        elif head in audit.VIEWS:
            what = audit.VIEWS[head]
            if what in ("printout", "export"):  # every printout and export, not once an hour
                audit.record(store, self.user, "view", what, path, ip)
            else:
                audit.viewed(store, self.user, what, path, ip)

    def _homeassistant(self, method, parts):
        """Admins connect Home Assistant: address, token (kept in a file), events, and what the mesh may run."""
        if self.user["role"] != "admin":
            raise ApiError(403, "only an admin can do that")
        folder = Path(store.path).parent
        sub = parts[1] if len(parts) > 1 else ""
        if method == "POST" and sub == "":
            try:
                homeassistant.save_settings(store, self._body(), folder)
            except ValueError as exc:
                raise ApiError(400, str(exc))
        elif method == "POST" and sub == "test":
            c = homeassistant.client(store, folder)
            if not c:
                raise ApiError(400, "add the address and a token first")
            try:
                msg = c.ping()
                c.fire("bunkr_test", {"from": "BUNKR"})
            except Exception as exc:
                raise ApiError(502, f"Home Assistant didn't answer: {type(exc).__name__}: {exc}"[:200])
            return {"ok": True, "message": msg or "connected"}
        elif method != "GET":
            raise ApiError(404, "not found")
        s = homeassistant.settings(store)
        return {"url": s["url"], "events": s["events"], "token": bool(homeassistant.token(folder)),
                "actions": homeassistant.lines(s["actions"], "actions"), "sensors": homeassistant.lines(s["sensors"], "sensors"),
                "status": dict(homeassistant.status)}

    def _mesh(self, method, parts):
        """The MeshCore family channel: everyone can see how to join it; admins set up the radio."""
        sub = parts[1] if len(parts) > 1 else ""
        if method == "GET" and sub == "":
            return mesh.view(store, MESH["link"], admin=self.user["role"] == "admin")
        if self.user["role"] != "admin":
            raise ApiError(403, "only an admin can change the mesh radio")
        if method == "POST" and sub == "":
            try:
                mesh.save_settings(store, self._body())
            except ValueError as exc:
                raise ApiError(400, str(exc))
            return mesh.view(store, MESH["link"])
        if sub == "nodes":
            body = self._body() if method == "POST" else {}
            key = parts[2] if len(parts) > 2 else ""
            try:
                if method == "POST" and not key:
                    mesh.add_node(store, body.get("public_key"), body.get("name"), body.get("person_uid") or "", body.get("label") or "")
                elif method == "POST":
                    mesh.assign_node(store, key, body.get("person_uid") or "", body.get("label"))
                elif method == "DELETE" and key:
                    mesh.forget_node(store, key)
                else:
                    raise ApiError(404, "not found")
            except ValueError as exc:
                raise ApiError(400, str(exc))
            except LookupError as exc:
                raise ApiError(404, str(exc))
            return {"nodes": mesh.nodes(store)}
        if method == "POST" and sub == "test":
            s = mesh.settings(store)
            if not s["enabled"]:
                raise ApiError(400, "turn the mesh radio on first")
            ch = mesh.channel(s, _num(self._body().get("index"), int, None)) or (s["channels"] or [None])[0]
            if not ch:
                raise ApiError(400, "add a channel first")
            MESH["link"].queue(ch["index"], f"BUNKR test on {ch['name']}. Reply SAFE to check in.")
            return {"queued": True, "channel": ch["name"]}
        raise ApiError(404, "not found")

    def _flights(self, method, parts):
        """Flights the family is following: add one (yours, or anyone's if you're an adult), end one, the key."""
        sub = parts[1] if len(parts) > 1 else ""
        if method == "GET" and sub == "":
            return {"flights": flights.board(store)}
        if sub == "settings":
            if self.user["role"] != "admin":
                raise ApiError(403, "only an admin can do that")
            folder = Path(store.path).parent
            if method == "POST":
                if ROLE in ("pi", "laptop"):
                    raise ApiError(400, "the FlightAware key goes on the hub")
                try:
                    flights.save_key(folder, self._body().get("key"))
                except ValueError as exc:
                    raise ApiError(400, str(exc))
            return {"key": bool(flights.load_key(folder)), "lookups": flights.lookups_this_month(store), "cap": flights.MONTHLY_CAP}
        me = checkins.person_for_user(store, self.user)
        def allowed(person_uid):
            if not ((me and me["uid"] == person_uid) or checkins.can_call(store, self.user)):
                raise ApiError(403, "only an adult can do that for someone else")
        if method == "POST" and sub == "":
            data = self._body()
            who = data.get("person_uid") or (me["uid"] if me else "")
            if not who:
                raise ApiError(400, "your login isn't linked to a person yet: link it on the Family tab")
            allowed(who)
            try:
                f = flights.add(store, who, data.get("number"), origin=data.get("origin") or "", dest=data.get("dest") or "",
                                by=self.user.get("username", ""), home=home_airport())
            except ValueError as exc:
                raise ApiError(400, str(exc))
            threading.Thread(target=check_flights, daemon=True).start()
            return flights.view(f)
        if method == "POST" and len(parts) == 3 and parts[2] == "end":
            f = flights.get(store, sub)
            if not f:
                raise ApiError(404, "no such flight")
            allowed(f["person_uid"])
            flights.end(store, f["uid"], "ended by " + self.user.get("username", "someone"))
            return {"ok": True}
        raise ApiError(404, "not found")

    def _news(self, method, parts):
        """The news feed: stories ranked for this person, the map, reading, rating, and what they follow."""
        uid = self.user.get("id")
        q = parse_qs(urlparse(self.path).query)
        arg = lambda k: (q.get(k) or [""])[0]
        if parts[0] == "me":  # /api/me/news: the areas I follow and topics I've muted
            if method == "POST":
                return news.save_prefs(store, uid, self._body())
            return news.prefs(store, uid)
        sub = parts[1] if len(parts) > 1 else ""
        if method == "GET" and sub == "":
            area, topic = arg("area"), arg("topic")
            try:
                limit, offset = max(1, min(100, int(arg("limit") or 30))), max(0, int(arg("offset") or 0))
            except ValueError:
                raise ApiError(400, "limit and offset are numbers")
            r = news.feed(store, uid, area=area if area in news.AREA_NAMES else None, topic=topic if topic in news.TOPIC_NAMES else None,
                          unread=arg("unread") == "1", limit=limit, offset=offset)
            return r | {"areas": news.AREAS, "topics": news.TOPICS, "outlets": {k: {kk: v for kk, v in o.items() if kk != "feed"}
                                                                                for k, o in brief.OUTLET.items()}}
        if method == "GET" and sub == "map":
            return {"points": news.map_points(store, uid, unread_only=arg("all") != "1")}
        if method == "GET" and sub == "stories":
            return {"stories": news.stories_by_id(store, uid, [x for x in arg("ids").split(",") if x])}
        if method == "POST" and sub == "read":
            ids = self._body().get("ids") or []
            if not isinstance(ids, list):
                raise ApiError(400, "ids must be a list")
            news.mark_read(store, uid, [str(i) for i in ids])
            return {"ok": True}
        if method == "POST" and sub == "rate":
            data = self._body()
            try:
                return {"rating": news.rate(store, uid, str(data.get("id", "")), int(data.get("rating", 0)))}
            except (KeyError, ValueError, TypeError):
                raise ApiError(404, "no such story")
        if method == "POST" and sub == "refresh":
            if self.user["role"] != "admin":
                raise ApiError(403, "only an admin can do that")
            threading.Thread(target=refresh_news, daemon=True).start()
            return {"ok": True}
        if method == "GET" and sub == "chatter":
            return {"posts": localwatch.recent(store), "places": [p["label"] for p in localwatch.places(store)],
                    "status": store.get_raw("localwatch_status", {}) or {}}
        if sub == "flags":
            # anyone can flag a source that doesn't belong; admins review
            fid = _num(parts[2], int, None) if len(parts) > 2 else None
            if method == "POST" and fid is None:
                d = self._body()
                try:
                    news.flag(store, self.user, d.get("kind"), d.get("url"), d.get("note"), story=str(d.get("story") or ""),
                              day=str(d.get("day") or ""), index=d.get("index") if isinstance(d.get("index"), int) else None)
                except KeyError as exc:
                    raise ApiError(404, str(exc.args[0]) if exc.args else "not found")
                except ValueError as exc:
                    raise ApiError(400, str(exc))
                audit.record(store, self.user, "change", "news", f"flagged a source: {str(d.get('url'))[:120]}", self._client_ip())
                return {"ok": True, "reviewed": self.user["role"] == "admin"}
            if self.user["role"] != "admin":
                raise ApiError(403, "only an admin can review flags")
            if method == "GET" and fid is None:
                return news.flags(store)
            if method == "POST" and fid is not None:
                try:
                    return news.review(store, fid, self._body().get("decision"), self.user)
                except KeyError:
                    raise ApiError(404, "no such flag")
                except ValueError as exc:
                    raise ApiError(400, str(exc))
        raise ApiError(404, "not found")

    def _field_checkin(self):
        """A check-in from the field page: anyone on the field Wi-Fi (the Wi-Fi password is the gate) or logged in."""
        origin = self.headers.get("Origin") or ""
        if origin and urlparse(origin).netloc != (self.headers.get("Host") or ""):
            raise ApiError(403, "cross-site form")
        if not (self._open_client() or self._session()):
            return self._redirect("/login.html")
        raw = self._read_raw(10_000).decode("utf-8", "replace")
        form = {k: v[0] for k, v in parse_qs(raw).items()}
        try:
            checkins.add_checkin(store, form.get("person_uid"), form.get("status"), form.get("note"), via="field",
                                 battery_low=form.get("battery_low") == "1")
        except ValueError as exc:
            raise ApiError(400, str(exc))
        audit.record(store, None, "change", "checkins", "check-in from the field page", self._client_ip())
        return self._redirect("/field?sent=1#family")

    def _client_ip(self):
        """Where a request came from. Through the Cloudflare tunnel, the visitor's address is in CF-Connecting-IP."""
        ip = self.client_address[0] if self.client_address else ""
        cf = (self.headers.get("CF-Connecting-IP") or "").strip()[:45]
        return f"{cf} via {ip}" if cf else ip

    def _rp(self):
        """The passkey address (rp id) and origin for this request. Passkeys need https, or localhost."""
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name, f"{'https' if self._secure() else 'http'}://{host}"

    def _start_session(self, user, how, scope="full"):
        token = auth.new_session(store, user["id"], self.headers.get("User-Agent", ""), scope)
        self._extra.append(("Set-Cookie", auth.cookie_header(token, self._secure())))
        audit.record(store, {"id": user["id"], "username": user["username"]}, "login", "login", how, self._client_ip())
        return token

    def _login_failed(self, name, u, msg):
        locked = bool(u and auth.is_locked(store, u["id"]) and "try again" not in msg)
        audit.record(store, {"id": u["id"] if u else None, "username": name}, "login_failed", "login",
                     "locked for a while" if locked else msg, self._client_ip(), alert="locked" if locked else None)
        raise ApiError(401, msg)

    def _login(self, step=""):
        data = self._body()
        if step == "code":
            uid = mfa.PENDING.take(("pw", str(data.get("ticket") or "")))
            u = store._rows("SELECT * FROM users WHERE id=?", (uid,))[0] if uid else None
            if not u:
                raise ApiError(401, "that took too long; log in again")
            if auth.is_locked(store, u["id"]):
                self._login_failed(u["username"], None, "too many failed attempts; try again in a few minutes")
            how = mfa.verify_code(store, u["id"], data.get("code"))
            if not how:
                auth.note_failure(store, u)
                if not auth.is_locked(store, u["id"]):
                    mfa.PENDING.put(("pw", data["ticket"]), u["id"])
                self._login_failed(u["username"], u, "that code doesn't match")
            if how == "recovery":
                audit.record(store, u, "security", "two-step", f"{u['username']} used a recovery code", self._client_ip(), alert="recovery_used")
            self._start_session(u, "password and " + ("authenticator code" if how == "totp" else "recovery code"))
            return self._send(200, {"ok": True})
        if step == "passkey-options":
            uid = None
            if data.get("ticket"):
                uid = mfa.PENDING.take(("pw", str(data["ticket"])))
                if not uid:
                    raise ApiError(401, "that took too long; log in again")
                mfa.PENDING.put(("pw", data["ticket"]), uid)
            return self._send(200, mfa.login_options(self._rp()[0], uid, store))
        if step == "passkey":
            try:
                uid = mfa.login_finish(store, data, self._rp()[1])
            except mfa.MfaError as exc:
                audit.record(store, None, "login_failed", "login", f"passkey: {exc}", self._client_ip())
                raise ApiError(401, str(exc))
            if data.get("ticket"):
                mfa.PENDING.take(("pw", str(data["ticket"])))
            u = store._rows("SELECT * FROM users WHERE id=?", (uid,))[0]
            if auth.is_locked(store, uid):
                raise ApiError(401, "too many failed attempts; try again in a few minutes")
            self._start_session(u, "passkey")
            return self._send(200, {"ok": True})
        name = str(data.get("username", ""))[:60]
        try:
            u = auth.check_password(store, data.get("username", ""), data.get("password", ""))
        except auth.AuthError as exc:
            self._login_failed(name, auth.user_by_name(store, name), str(exc))
        st = mfa.status(store, u["id"])
        if st["on"]:
            ticket = secrets.token_urlsafe(24)
            mfa.PENDING.put(("pw", ticket), u["id"])
            rp = self._rp()[0]
            methods = (["code"] if st["totp"] else []) + (["passkey"] if any(k["rp_id"] == rp for k in st["passkeys"]) else [])
            if st["recovery_left"]:
                methods.append("recovery")
            return self._send(200, {"next": "two-step", "ticket": ticket, "methods": methods})
        if two_step_required(u):
            self._start_session(u, "password (two-step login still to set up)", scope="setup")
            return self._send(200, {"ok": True, "next": "setup"})
        self._start_session(u, "password")
        self._send(200, {"ok": True})

    def _two_step(self, method, parts):
        """/api/me/two-step... and /api/me/passkeys...: a person's own two-step login. Works in a setup-only session."""
        u = self.user
        rp, origin = self._rp()
        data = self._body() if method in ("POST", "DELETE") else {}
        sub = parts[2] if len(parts) > 2 else ""
        ip = self._client_ip()

        def done(extra):
            token = auth.token_from_cookie(self.headers.get("Cookie"))
            if u.get("scope") == "setup" and mfa.enabled(store, u["id"]):
                auth.upgrade_session(store, token)
                extra["ready"] = True
            return extra

        def need_password():
            row = store._rows("SELECT password_hash FROM users WHERE id=?", (u["id"],))
            if not row or not auth.verify_password(str(data.get("password") or ""), row[0]["password_hash"]):
                raise ApiError(400, "enter your password to change two-step login")

        def keep_one(removing):
            st = mfa.status(store, u["id"])
            left = (1 if st["totp"] and removing != "totp" else 0) + len([k for k in st["passkeys"] if k["id"] != removing])
            if not left and two_step_required(u):
                raise ApiError(400, "two-step login is required here; add another way before removing this one")
            return left
        try:
            if parts[1] == "two-step" and method == "GET":
                return {**mfa.status(store, u["id"]), "rp_id": rp, "required": two_step_required(u),
                        "policy": store.get_raw("two_step_policy", "none") or "none"}
            if parts[1] == "two-step" and sub == "totp" and method == "POST" and parts[3:4] == ["confirm"]:
                codes = mfa.confirm_totp(store, u["id"], data.get("code"))
                audit.record(store, u, "security", "two-step", "turned on authenticator codes", ip)
                return done({"recovery": codes})
            if parts[1] == "two-step" and sub == "totp" and method == "POST":
                return mfa.start_totp(store, u["id"], u["username"])
            if parts[1] == "two-step" and sub == "totp" and method == "DELETE":
                need_password()
                keep_one("totp")
                mfa.remove_totp(store, u["id"])
                audit.record(store, u, "security", "two-step", "turned off authenticator codes", ip,
                             alert=None if mfa.enabled(store, u["id"]) else "mfa_off")
                return {"ok": True}
            if parts[1] == "two-step" and sub == "recovery" and method == "POST":
                need_password()
                if not mfa.status(store, u["id"])["totp"]:
                    raise ApiError(400, "recovery codes come with authenticator codes")
                shown, hashes = mfa.recovery_codes()
                store._exec("UPDATE users SET recovery=? WHERE id=?", (json.dumps(hashes), u["id"]))
                audit.record(store, u, "security", "two-step", "made new recovery codes", ip)
                return {"recovery": shown}
            if parts[1] == "passkeys" and sub == "options" and method == "POST":
                return mfa.register_options(store, u, rp)
            if parts[1] == "passkeys" and not sub and method == "POST":
                pid = mfa.register_finish(store, u, data, origin, data.get("name"))
                audit.record(store, u, "security", "two-step", f"added a passkey ({str(data.get('name') or 'Passkey')[:40]})", ip)
                return done({"id": pid})
            if parts[1] == "passkeys" and sub and method == "DELETE":
                pk = _num(sub, int, None)
                if not store._rows("SELECT 1 FROM passkeys WHERE id=? AND user_id=?", (pk, u["id"])):
                    raise ApiError(404, "no such passkey")
                keep_one(pk)
                mfa.remove_passkey(store, u["id"], pk)
                audit.record(store, u, "security", "two-step", "removed a passkey", ip,
                             alert=None if mfa.enabled(store, u["id"]) else "mfa_off")
                return {"ok": True}
        except mfa.MfaError as exc:
            raise ApiError(400, str(exc))
        raise ApiError(404, "not found")

    def _bulk_items(self, data):
        """Add several items from a checked preview. Rows that don't validate are reported, not saved."""
        rows = data.get("items") or []
        if not isinstance(rows, list) or not rows:
            raise ApiError(400, "nothing to add")
        added, problems = [], []
        for n, r in enumerate(rows[:200], 1):
            if not isinstance(r, dict):
                continue
            if r.get("category") in gaps.SENSITIVE:
                problems.append(f"line {n}: add firearms and ammunition one at a time on the Inventory tab")
                continue
            try:
                added.append(store.save_item(clean_item(r | {"location_id": data.get("location_id"), "location": data.get("location")})))
            except ApiError as exc:
                problems.append(f"line {n}: {exc}")
        return {"added": len(added), "ids": added, "problems": problems}

    def _power(self, method, row_id, parts):
        sub = parts[1] if len(parts) > 1 else ""
        if method == "GET":
            devs = store._rows("SELECT * FROM power_devices ORDER BY priority, name")
            cfg = power.settings(store)
            return {"devices": devs, "settings": cfg, "plan": power.plan(devs, cfg["batteries"]), "presets": power.PRESETS,
                    "priorities": power.PRIORITIES, "suggestions": power.suggestions(store),
                    "assumptions": {"usable": power.USABLE, "sun_hours": power.SUN_HOURS, "panel_eff": power.PANEL_EFF}}
        if self.user["role"] != "admin" and not checkins.can_call(store, self.user):
            raise ApiError(403, "only an adult can change the power plan")
        data = self._body() if method == "POST" else {}
        dev_id = _num(parts[2], int, None) if sub == "devices" and len(parts) > 2 else None
        try:
            if (method, sub) == ("POST", "settings"):
                store.set_raw("power", {"batteries": power.clean_batteries(data.get("batteries")),
                                        "inverter": str(data.get("inverter") or "").strip()[:80]})
                return power.settings(store)
            if (method, sub) == ("POST", "preset"):
                if data.get("key") not in power.PRESETS:
                    raise ValueError("unknown preset")
                rec = power.clean_device(power.PRESETS[data["key"]] | {"battery": data.get("battery") or ""})
            elif (method, sub) == ("POST", "devices"):
                rec = power.clean_device(data)
            elif (method, sub) == ("DELETE", "devices") and dev_id:
                store._exec("DELETE FROM power_devices WHERE id=?", (dev_id,))
                return {"ok": True}
            else:
                raise ApiError(404, "not found")
        except ValueError as exc:
            raise ApiError(400, str(exc))
        if dev_id:
            store._exec(f"UPDATE power_devices SET {', '.join(f'{k}=?' for k in rec)} WHERE id=?", (*rec.values(), dev_id))
            return {"id": dev_id}
        return {"id": store._exec(f"INSERT INTO power_devices({', '.join(rec)}) VALUES({', '.join('?' for _ in rec)})", tuple(rec.values()))}

    def _meds(self, method, row_id, parts):
        sub = parts[2] if len(parts) > 2 else ""
        found = store._rows("SELECT * FROM meds WHERE id=?", (row_id,)) if row_id else []
        if row_id and not found:
            raise ApiError(404, "no such medicine")
        med = found[0] if found else None
        data = self._body() if method == "POST" else {}
        person_id = med["person_id"] if med else _num(data.get("person_id"), int, 0)
        if not med_editor(self.user, person_id):
            raise ApiError(403, "only an adult or the person themselves can change this")
        try:
            if method == "POST" and med and sub == "refilled":
                return {"on_hand": medical.refilled(store, row_id, data.get("amount"))}
            if method == "POST" and not sub:
                return {"id": medical.save(store, person_id, data, row_id)}
        except ValueError as exc:
            raise ApiError(400, str(exc))
        if method == "DELETE" and med:
            store._exec("DELETE FROM meds WHERE id=?", (row_id,))
            return {"ok": True}
        raise ApiError(404, "not found")

    def _store_file(self, rel):
        """Maps and the map viewer from the store: GET only, Range supported (pmtiles needs it)."""
        rel = unquote(rel)
        if not rel.startswith(("maps/", "web/", "firmware/", "apps/", "radio/", "plan/")):
            raise ApiError(404, "not found")
        target = (depot.root / rel).resolve()
        if not target.is_relative_to(depot.root.resolve()):
            raise ApiError(404, "not found")
        if rel == "maps/" or rel == "maps":
            files = sorted(depot.root.joinpath("maps").glob("*.pmtiles")) if depot.root.joinpath("maps").is_dir() else []
            return self._send(200, [{"name": f.name, "size": f.stat().st_size} for f in files])
        if target.is_dir():
            target = target / "index.html"
        if rel.rstrip("/") in ("web/maps", "web/maps/index.html"):
            target = VIEWER_PAGE  # always this code's viewer, so a new release reaches the map without a rebuild
        if rel.startswith("plan/") and LOCK["on"]:  # a locked node only has the sealed copy
            sealed = target.with_name(target.name + vault.SUFFIX)
            if rel.endswith(vault.SUFFIX) or not sealed.is_file() or not LOCK["key"]:
                raise ApiError(404, "not found")
            return self._send(200, vault.open_sealed(LOCK["key"], sealed), "text/html; charset=utf-8")
        if not target.is_file():
            raise ApiError(404, "not found")
        if target.suffix == ".pmtiles":
            return sync.serve_file(self, target)
        if rel.startswith(("firmware/", "apps/", "radio/")) and target.suffix not in (".html", ".csv", ".txt"):
            self._extra.append(("Content-Disposition", f'attachment; filename="{target.name}"'))
            return sync.serve_file(self, target)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    # ---------------- Claude connector (MCP) ----------------
    def _base(self):
        if PUBLIC_URL:
            return PUBLIC_URL.rstrip("/")
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip().lower()
        return f"{'https' if proto == 'https' else 'http'}://{(self.headers.get('Host') or '127.0.0.1').strip()}"

    def _raw(self, status, data=b"", ctype="application/json", headers=()):
        self.send_response(status)
        for k, v in list(headers) + getattr(self, "_extra", []):
            self.send_header(k, v)
        self._extra = []
        if data:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _read_raw(self, limit=1_000_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ApiError(413, "request too large")
        return self.rfile.read(n) if n else b""

    def _mcp(self, method, url):
        base, p = self._base(), url.path
        js = lambda obj, status=200, headers=(): self._raw(status, json.dumps(obj).encode(), "application/json", headers)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if p.startswith("/.well-known/"):
            if method != "GET":
                return self._raw(405, headers=[("Allow", "GET")])
            if p.startswith("/.well-known/oauth-protected-resource"):
                return js(mcp.protected_resource(base))
            if p.startswith(("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration")):
                return js(mcp.auth_server(base))
            return js({"error": "not found"}, 404)
        claude_ok = mcp.from_claude(self.headers.get("CF-Connecting-IP"))
        try:
            if p == "/mcp/oauth/register" and method == "POST":
                if not claude_ok:
                    return js({"error": "access_denied"}, 403)
                try:
                    body = json.loads(self._read_raw() or b"{}")
                except ValueError:
                    raise mcp.OAuthError("invalid_client_metadata", "send JSON")
                return js(mcp.register(store, body if isinstance(body, dict) else {}), 201)
            if p == "/mcp/oauth/token" and method == "POST":
                if not claude_ok:
                    return js({"error": "access_denied"}, 403)
                return js(mcp.token(store, mcp.parse_form(self._read_raw(), self.headers.get("Content-Type"))))
            if p == "/mcp/oauth/authorize":
                if method == "POST":
                    form = mcp.parse_form(self._read_raw(), self.headers.get("Content-Type"))
                else:
                    form = q
                try:
                    c, ru = mcp.check_authorize(store, form)
                    if form.get("resource") and form["resource"].rstrip("/") != base + "/mcp":
                        raise mcp.OAuthError("invalid_target", "this sign-in is for a different server")
                except mcp.OAuthError as exc:
                    page = f"<!doctype html><meta name=viewport content='width=device-width'><p>Can't connect: {html.escape(exc.desc)}</p>"
                    return self._raw(400, page.encode(), "text/html; charset=utf-8")
                if method == "GET":
                    return self._raw(200, mcp.authorize_page(form, c).encode(), "text/html; charset=utf-8",
                                     [("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://claude.ai https://claude.com")])
                try:
                    u = auth.check_password(store, form.get("username", ""), form.get("password", ""))
                    if mfa.enabled(store, u["id"]):
                        if not mfa.status(store, u["id"])["totp"]:
                            raise auth.AuthError("this login uses passkeys only: add authenticator codes on the Household tab "
                                                 "to connect Claude")
                        if not mfa.verify_code(store, u["id"], form.get("code")):
                            auth.note_failure(store, u)
                            raise auth.AuthError("add the code from your authenticator app" if not form.get("code") else "that code doesn't match")
                    user = {"id": u["id"], "username": u["username"]}
                    audit.record(store, user, "login", "claude", "connector sign-in", self._client_ip())
                except auth.AuthError as exc:
                    return self._raw(200, mcp.authorize_page(form, c, str(exc)).encode(), "text/html; charset=utf-8")
                return self._redirect(mcp.issue_code(store, c, ru, user["id"], form))
            if p == "/mcp":
                if method != "POST":
                    return self._raw(405, headers=[("Allow", "POST")])
                origin = self.headers.get("Origin")
                if (origin and origin.rstrip("/") not in ("https://claude.ai", "https://claude.com", base)) or not claude_ok:
                    return js({"error": "forbidden"}, 403)
                user = mcp.bearer_user(store, self.headers.get("Authorization"))
                if not user:
                    return js({"error": "invalid_token"}, 401, [("WWW-Authenticate",
                              f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp", scope="{mcp.SCOPE}"')])
                try:
                    msg = json.loads(self._read_raw() or b"null")
                except ValueError:
                    return js({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
                tools = mcp_tools.make_tools(sys.modules[__name__])

                def run(name, args, u):
                    audit.record(store, u, "connector", "claude", name, self._client_ip())
                    try:
                        return tools[name]["fn"](args, u)
                    except mcp.ToolError:
                        raise
                    except (KeyError, TypeError, ValueError) as exc:
                        raise mcp.ToolError(f"bad or missing argument: {exc}")
                    except ApiError as exc:
                        raise mcp.ToolError(str(exc))
                msgs = msg if isinstance(msg, list) else [msg]
                out = [r for r in (mcp.handle_rpc(m, tools, user, run) for m in msgs) if r is not None]
                if not out:
                    return self._raw(202)
                return js(out if isinstance(msg, list) else out[0])
            return js({"error": "not found"}, 404)
        except mcp.OAuthError as exc:
            return js({"error": exc.error, "error_description": exc.desc}, exc.status)

    def _bearer(self):
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.startswith("Bearer ") else ""

    def _sync(self, method, sub):
        """Node-to-hub API. Authenticated by the node's token, not a browser session."""
        import time
        try:
            if (method, sub) == ("POST", "pair"):
                now_t = time.time()
                _pair_failures[:] = [t for t in _pair_failures if now_t - t < 900]
                if len(_pair_failures) >= 10:
                    raise ApiError(429, "too many pairing attempts; wait 15 minutes")
                res = sync.pair(store, self._body().get("code", ""))
                if not res:
                    _pair_failures.append(now_t)
                    raise ApiError(403, "pairing code wrong or expired")
                return self._send(200, res)
            node = sync.node_for_token(store, self._bearer())
            if not node:
                raise ApiError(401, "unknown node token; pair again")
            nodes.set_fields(store, node["id"], last_seen=manifest.now())
            if (method, sub) == ("GET", "plan"):
                try:
                    publish_generated()
                except Exception as exc:  # never block a sync on this
                    print(f"publish_generated failed: {exc}")
                p = node_plan(node)
                return self._send(200, {"files": nodes.plan_files(p), "used": p["used"], "budget": p["budget"],
                                        "skipped": [{k: x[k] for k in ("path", "size", "reason")} for x in p["skipped"]]})
            if method == "GET" and sub.startswith("file/"):
                rel = unquote(sub[5:])
                if rel not in {f["path"] for f in nodes.plan_files(node_plan(node))}:
                    raise ApiError(403, "that file isn't planned for this node")
                target = (depot.root / rel).resolve()
                if not target.is_relative_to(depot.root.resolve()) or not target.is_file():
                    raise ApiError(404, "missing from the store")
                return sync.serve_file(self, target)
            if (method, sub) == ("POST", "vault"):
                data = self._body()
                lock = "on" if data.get("lock") == "on" else "waiting" if data.get("lock") == "waiting" else "off"
                nodes.set_fields(store, node["id"], lock=lock)
                return self._send(200, {"key": base64.b64encode(hub_vault_key()).decode(),
                                        "wrapped": store.get_raw("vault_wrapped", None)})
            if (method, sub) == ("POST", "report"):
                data = self._body()
                nodes.set_fields(store, node["id"], manifest=json.dumps(data.get("manifest") or {}),
                                 last_sync=manifest.now(), last_sync_status=str(data.get("status", ""))[:200])
                return self._send(200, {"ok": True})
            if (method, sub) == ("POST", "records"):
                data = self._body()
                since = str(data.get("since") or "")
                # A little overlap so changes stamped in the same second are never skipped;
                # re-sent records with an unchanged updated_at are ignored on arrival.
                cursor = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(timespec="seconds")
                res = sync.apply_records(store, {k: data.get(k, []) for k in ("locations", "items", "tombstones")},
                                         since, source=node["name"])
                checkins.apply(store, {k: data.get(k, []) for k in ("checkins", "calls")})
                changes = (sync.export_records(store, since) | checkins.export(store, since, include_people=True)
                           | situation.export(store, since) | brief.export(store, since))
                return self._send(200, {"changes": changes, "cursor": cursor,
                                        "applied": res["applied"], "conflicts": res["conflicts"]})
            raise ApiError(404, "unknown sync endpoint")
        except ApiError as exc:
            self._send(exc.status, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _static(self, path):
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            raise ApiError(404, "not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def _api(self, method, path, query):
        parts = path.strip("/").split("/")[1:]
        head = parts[0] if parts else ""
        row_id = _num(parts[1], int, None) if len(parts) > 1 else None

        if (method, head) == ("GET", "state"):
            st = full_state(self.user)
            if self.user["role"] != "admin":  # family logins don't see file paths or backup jobs
                st["documents"] = [{k: d[k] for k in ("id", "label", "doc_type", "priority")} for d in st["documents"]]
                st["backups"] = []
            return st
        if (method, head) == ("GET", "me") and len(parts) == 1:
            return {**self.user, "require_login": REQUIRE_LOGIN, "node_role": ROLE, "node_lock": LOCK["on"]}
        if (method, head) == ("POST", "lock"):
            if not LOCK["on"]:
                raise ApiError(400, "node lock isn't on here")
            audit.record(store, self.user, "security", "node lock", "locked this node", self._client_ip())
            threading.Timer(0.5, lock_now).start()  # after this answer goes out
            return {"ok": True}
        if head == "me" and parts[1:2] in (["two-step"], ["passkeys"]):
            return self._two_step(method, parts)
        if (method, head) == ("POST", "me") and len(parts) == 2 and parts[1] == "password":
            data = self._body()
            u = store._rows("SELECT * FROM users WHERE id=?", (self.user["id"],))
            if not u or not auth.verify_password(data.get("current", ""), u[0]["password_hash"]):
                raise ApiError(400, "current password is wrong")
            try:
                auth.set_password(store, self.user["id"], data.get("new", ""))
            except auth.AuthError as exc:
                raise ApiError(400, str(exc))
            return {"ok": True, "note": "password changed; log in again"}

        if head == "me" and parts[1:2] == ["home"] and len(parts) == 2 and method in ("POST", "DELETE"):
            if not self.user.get("id"):  # no logins on this machine: the one layout is the default
                if method == "DELETE":
                    store.set_raw("home_layout_default", "")
                else:
                    homelayout.save_default(store, self._body().get("layout"))
            elif method == "DELETE":
                homelayout.reset_user(store, self.user["id"])
            else:
                homelayout.save_user(store, self.user["id"], self._body().get("layout"))
            return homelayout.for_user(store, self.user)
        if head == "news" or (head == "me" and parts[1:2] == ["news"]):
            return self._news(method, parts)
        if head == "home" and parts[1:2] == ["default"] and method == "POST":
            return homelayout.save_default(store, self._body().get("layout"))
        if head == "users" and row_id and parts[2:3] == ["home"] and method == "DELETE":
            if not store._rows("SELECT id FROM users WHERE id=?", (row_id,)):
                raise ApiError(404, "no such login")
            homelayout.reset_user(store, row_id)
            return {"ok": True}
        if head == "users" and parts[1:2] == ["policy"] and method == "POST":
            pol = self._body().get("policy")
            if pol not in TWO_STEP_POLICIES:
                raise ApiError(400, "choose none, admins or everyone")
            store.set_raw("two_step_policy", pol)
            audit.record(store, self.user, "security", "two-step", f"two-step login required for: {pol}", self._client_ip())
            return {"policy": pol}
        if head == "users" and row_id and parts[2:3] == ["two-step-off"] and method == "POST":
            target = store._rows("SELECT username FROM users WHERE id=?", (row_id,))
            if not target:
                raise ApiError(404, "no such login")
            mfa.remove_all(store, row_id)
            store._exec("DELETE FROM sessions WHERE user_id=?", (row_id,))
            audit.record(store, self.user, "security", "two-step", f"turned off two-step login for {target[0]['username']}",
                         self._client_ip(), alert="mfa_off")
            return {"ok": True}
        if head == "users":
            try:
                if method == "GET":
                    return [u | {"two_step": mfa.enabled(store, u["id"])} for u in auth.users(store)]
                data = self._body() if method == "POST" else {}
                ip = self._client_ip()
                if method == "POST" and not row_id:
                    new = auth.create_user(store, data.get("username"), data.get("password"), data.get("role", "family"))
                    if data.get("role") == "admin":
                        audit.record(store, self.user, "security", "logins", f"new admin login {data.get('username')}", ip, alert="admin_added")
                    return {"id": new}
                if method == "POST" and row_id:
                    target = store._rows("SELECT username, role FROM users WHERE id=?", (row_id,))
                    tname = target[0]["username"] if target else str(row_id)
                    if data.get("password"):
                        auth.set_password(store, row_id, data["password"])
                        if row_id != self.user["id"]:
                            audit.record(store, self.user, "security", "logins", f"changed the password of {tname}", ip, alert="password_reset")
                    if data.get("role"):
                        auth.set_role(store, row_id, data["role"])
                        if data["role"] == "admin" and target and target[0]["role"] != "admin":
                            audit.record(store, self.user, "security", "logins", f"made {tname} an admin", ip, alert="made_admin")
                    if "view" in data and data["view"] in auth.VIEWS:
                        store._exec("UPDATE users SET view=? WHERE id=?", (data["view"], row_id))
                    return {"ok": True}
                if method == "DELETE" and row_id:
                    if row_id == self.user["id"]:
                        raise ApiError(400, "you can't delete your own login")
                    target = store._rows("SELECT username FROM users WHERE id=?", (row_id,))
                    auth.delete_user(store, row_id)
                    audit.record(store, self.user, "security", "logins", f"deleted the login {target[0]['username'] if target else row_id}",
                                 self._client_ip(), alert="user_deleted")
                    return {"ok": True}
            except auth.AuthError as exc:
                raise ApiError(400, str(exc))

        if head == "conflicts":
            if method == "GET":
                return store._rows("SELECT * FROM sync_conflicts WHERE resolved=0 ORDER BY at DESC LIMIT 100")
            if method == "POST" and row_id:
                store._exec("UPDATE sync_conflicts SET resolved=1 WHERE id=?", (row_id,))
                return {"ok": True}
        if (method, head) == ("POST", "settings"):
            return store.update_settings(self._body())

        if head == "items":
            if method == "POST" and len(parts) == 2 and parts[1] == "move":
                data = self._body()
                ids = [_num(i, int, None) for i in data.get("ids", [])]
                if not ids or None in ids:
                    raise ApiError(400, "choose items to move")
                loc = resolve_location(data)
                store.move_items(ids, loc)
                return {"ok": True, "moved": len(ids)}
            if (method, parts[1:2]) == ("POST", ["parse"]):
                return {"items": quickadd.parse(str(self._body().get("text") or "")[:20000])}
            if (method, parts[1:2]) == ("POST", ["bulk"]):
                return self._bulk_items(self._body())
            if method == "POST" and row_id and parts[2:3] == ["bump"]:
                rows = store._rows("SELECT * FROM items WHERE id=?", (row_id,))
                if not rows or (rows[0]["category"] in gaps.SENSITIVE and self.user["role"] != "admin"):
                    raise ApiError(404, "no such item")
                amount = _num(self._body().get("amount"), float, 1)
                store._exec("UPDATE items SET quantity=MAX(0, quantity + ?), updated_at=? WHERE id=?",
                            (amount, datetime.now(timezone.utc).isoformat(timespec="seconds"), row_id))
                return {"quantity": store._rows("SELECT quantity FROM items WHERE id=?", (row_id,))[0]["quantity"]}
            if method == "POST":
                body = self._body()
                saved = store.save_item(clean_item(body), row_id)
                if body.get("barcode"):
                    try:
                        quickadd.remember(store, body["barcode"], store._rows("SELECT * FROM items WHERE id=?", (saved,))[0])
                    except ValueError as exc:
                        raise ApiError(400, str(exc))
                return {"id": saved}
            if method == "DELETE" and row_id:
                store.delete_item(row_id)
                return {"ok": True}
        if head == "barcode" and method == "POST":
            try:
                r = quickadd.lookup(store, self._body().get("code"))
            except ValueError as exc:
                raise ApiError(400, str(exc))
            if r.get("item") and r["item"]["category"] in gaps.SENSITIVE and self.user["role"] != "admin":
                r = {"code": r["code"], "source": "none"}
            return r

        if head == "locations":
            if method == "POST":
                return {"id": save_location(clean_location(self._body()), row_id)}
            if method == "DELETE" and row_id:
                try:
                    store.delete_location(row_id)
                except ValueError as exc:
                    raise ApiError(400, str(exc))
                return {"ok": True}

        if head == "sites":
            if method == "POST" and len(parts) == 3 and parts[2] == "refresh":
                site = store.site(row_id)
                if not site:
                    raise ApiError(404, "no such site")
                sites.refresh_site(store, site, hazards, with_hazards=True)
                return store.site(row_id)
            if method == "POST":
                return save_site(self._body(), row_id)
            if method == "DELETE" and row_id:
                store.delete_site(row_id)
                return {"ok": True}

        if (method, head) == ("GET", "shopping.html"):
            st = full_state(self.user)
            return export.shopping_html(st["shopping"]), "text/html; charset=utf-8"
        if (method, head) == ("GET", "shopping.csv"):
            return export.shopping_csv(full_state()["shopping"]), "text/csv; charset=utf-8"
        if method == "GET" and head == "packing" and row_id:
            loc = store.location(row_id)
            if not loc:
                raise ApiError(404, "no such location")
            st = full_state(self.user)
            score = next((k for k in st["kits"] if k["location_id"] == row_id), None)
            inside = {row_id}
            for _ in range(2):  # sub-locations and the ones inside them
                inside |= {l["id"] for l in st["locations"] if l.get("parent_id") in inside}
            here = [i for i in st["items"] if i["location_id"] in inside]
            return export.packing_html(loc, here, score, st["categories"]), "text/html; charset=utf-8"

        if head == "chirp":
            sub = parts[1] if len(parts) > 1 else ""
            if method == "GET" and not sub:
                return chirp.status(depot)
            if (method, sub) == ("POST", "fetch"):
                try:
                    _row, changed = chirp.fetch_source(depot)
                except Exception as exc:
                    raise ApiError(502, f"couldn't reach GitHub ({exc})")
                return {**chirp.status(depot), "changed": changed}
            if (method, sub) == ("POST", "upload"):
                try:
                    chirp.add_installer(depot, query.get("name", [""])[0], self.rfile,
                                        int(self.headers.get("Content-Length") or 0))
                except ValueError as exc:
                    raise ApiError(400, str(exc))
                store._exec("UPDATE tasks SET last_done=? WHERE key='chirp_installer'",
                            (datetime.now().date().isoformat(),))
                return chirp.status(depot)
        if head == "depot":
            sub = parts[1] if len(parts) > 1 else ""
            if method == "GET" and not sub:
                return depot_view()
            if method == "GET" and sub == "job":
                return depot_job.snapshot()
            if method == "POST" and sub in ("fetch", "verify", "archive", "drive-check"):
                data = self._body()
                if sub == "fetch":
                    wanted = [w for w in data.get("items", []) if w.get("kind") in ("library", "tool", "firmware", "app")]
                    if not wanted:
                        raise ApiError(400, "choose something to fetch")
                    args = (fetch_into_depot, wanted)
                elif sub == "verify":
                    args = (verify_depot,)
                elif sub == "archive":
                    if len(data.get("passphrase") or "") < 10:
                        raise ApiError(400, "use a passphrase of at least 10 characters")
                    args = (build_archive_into_depot, data["passphrase"])
                else:
                    args = (drive_check, data.get("mount", ""), bool(data.get("verify")))
                try:
                    depot_job.start(*args)
                except RuntimeError as exc:
                    raise ApiError(409, "the downloads store is busy: " + str(exc))
                return depot_job.snapshot()
            if method == "POST" and sub == "cancel":
                depot_job.cancel()
                return depot_job.snapshot()
            if method == "POST" and sub == "settings":
                store.set_raw("auto_update", bool(self._body().get("auto_update")))
                return {"auto_update": store.get_raw("auto_update", True)}
            if method == "POST" and sub == "updates" and len(parts) == 4 and parts[3] == "approve":
                upd = store._rows("SELECT * FROM updates WHERE id=?", (_num(parts[2], int, -1),))
                if not upd:
                    raise ApiError(404, "no such update")
                store._exec("UPDATE updates SET status='pending' WHERE id=?", (upd[0]["id"],))
                depot_job.start(fetch_into_depot, [{"kind": upd[0]["kind"], "id": upd[0]["item_id"]}])
                return depot_job.snapshot()
            if method == "DELETE" and len(parts) == 3 and sub == "files":
                rows = store._rows("SELECT * FROM depot_files WHERE id=?", (_num(parts[2], int, -1),))
                if not rows:
                    raise ApiError(404, "no such file")
                depot.remove(rows[0])
                return {"ok": True}

        if head == "radio" and method == "GET":
            sub = parts[1] if len(parts) > 1 else ""
            if not sub:
                return radio_view()
            if sub == "reference.html":
                return radio.reference_html(), "text/html; charset=utf-8"
            if sub == "cards.html":
                pages = [radio.card_html(r, radio_channels(r["id"])) for r in store._rows("SELECT * FROM radios ORDER BY name")]
                body = "".join(p.split("<body>")[1].split("</body>")[0] for p in pages) or "<p>No radios yet.</p>"
                return pages[0].split("<body>")[0] + "<body>" + body + "</body></html>" if pages else body, "text/html; charset=utf-8"
        if head == "radios":
            sub = parts[2] if len(parts) > 2 else ""
            r = store._rows("SELECT * FROM radios WHERE id=?", (row_id,))[0] if row_id and store._rows(
                "SELECT id FROM radios WHERE id=?", (row_id,)) else None
            if row_id and not r:
                raise ApiError(404, "no such radio")
            if r and r.get("profile") and method == "GET" and sub in ("chirp.csv", "file", "card.html", "plan"):
                mems, issues = radio_plan(r)
                if sub == "plan":
                    return {"memories": mems, "issues": issues, "profile": radioplan.PROFILES[r["profile"]]}
                if sub == "card.html":
                    return radioplan.card_html(r, mems, issues), "text/html; charset=utf-8"
                fname, text = radioplan.radio_file(r, mems)
                self._extra.append(("Content-Disposition", f'attachment; filename="{fname}"'))
                return text, "text/csv; charset=utf-8"
            if method == "GET" and sub == "chirp.csv":
                return radio.chirp_csv(radio_channels(row_id)), "text/csv; charset=utf-8"
            if method == "GET" and sub == "card.html":
                return radio.card_html(r, radio_channels(row_id)), "text/html; charset=utf-8"
            if method == "POST" and sub == "extras":  # this radio's own channels, at fixed slots
                pairs = self._body().get("extras", [])
                rows = [(_num(p.get("channel_id"), int, None), _num(p.get("slot"), int, None)) for p in pairs]
                valid = {c["id"] for c in channels_all() if c["slot"] is None}
                if any(c not in valid or s is None or not 0 <= s <= 999 for c, s in rows) or \
                        len({s for _, s in rows}) != len(rows):
                    raise ApiError(400, "extras need plan-free channels and unique slots")
                with store._lock:
                    store._conn.execute("DELETE FROM radio_channels WHERE radio_id=?", (row_id,))
                    store._conn.executemany("INSERT INTO radio_channels(radio_id, channel_id, slot) VALUES(?,?,?)",
                                            [(row_id, c, s) for c, s in rows])
                    store._conn.commit()
                return {"ok": True, "count": len(rows)}
            if method == "POST" and sub == "channels":
                ids = [_num(i, int, None) for i in self._body().get("channel_ids", [])]
                valid = {c["id"] for c in channels_all()}
                if None in ids or not set(ids) <= valid or len(set(ids)) != len(ids):
                    raise ApiError(400, "unknown or repeated channel")
                with store._lock:
                    store._conn.execute("DELETE FROM radio_channels WHERE radio_id=?", (row_id,))
                    store._conn.executemany("INSERT INTO radio_channels(radio_id, channel_id, slot) VALUES(?,?,?)",
                                            [(row_id, c, n + 1) for n, c in enumerate(ids)])
                    store._conn.commit()
                return {"ok": True, "count": len(ids)}
            if method == "POST" and sub == "image":
                if self.user["role"] != "admin":
                    raise ApiError(403, "only an admin can do that")
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 8 * 1024 * 1024:
                    raise ApiError(400, "choose a programming file (up to 8 MB)")
                data = self.rfile.read(length)
                import tempfile
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                orig = Path(unquote(self.headers.get("X-Filename", "image.img"))).name
                ext = Path(orig).suffix[:6] or ".img"
                with tempfile.TemporaryDirectory() as tmp:
                    src = Path(tmp) / f"{_slug(r['name'])}-{row_id}-{stamp}{ext}"
                    src.write_bytes(data)
                    row = depot.add_file("radio", f"image-{row_id}", src, version=stamp)
                return {"ok": True, "path": row["path"]}
            if method == "POST" and not sub:
                data = self._body()
                if not (data.get("name") or "").strip():
                    raise ApiError(400, "a radio needs a name")
                fields = {"name": data["name"].strip()[:40], "model": (data.get("model") or "").strip()[:60],
                          "kind": data.get("kind") if data.get("kind") in radio.KINDS else "handheld",
                          "service": data.get("service") if data.get("service") in ("ham", "gmrs") else "ham",
                          "chirp_driver": (data.get("chirp_driver") or "").strip()[:60],
                          "notes": (data.get("notes") or "").strip()[:300],
                          "profile": data.get("profile") if data.get("profile") in radioplan.PROFILES else "",
                          "owner": (data.get("owner") or "").strip()[:60]}
                if fields["profile"] == "gm15pro":
                    fields["service"] = "gmrs"
                new_id = save_row("radios", RADIO_FIELDS, fields, row_id)
                if not row_id and not fields["profile"]:  # start with the sensible default channels for its service
                    ids = radio.default_channels_for(fields, channels_all())
                    store._conn.executemany("INSERT INTO radio_channels(radio_id, channel_id, slot) VALUES(?,?,?)",
                                            [(new_id, c, n + 1) for n, c in enumerate(ids)])
                    store._conn.commit()
                return {"id": new_id}
            if method == "DELETE" and row_id:
                store._exec("DELETE FROM radios WHERE id=?", (row_id,))
                return {"ok": True}
        if head == "channels":
            sub = parts[1] if len(parts) > 1 else ""
            if method == "POST" and sub == "standard":
                have = {c["name"] for c in channels_all()}
                added = 0
                for ch in radio.standard_channels():
                    if ch["name"] not in have:
                        save_row("channels", CHANNEL_FIELDS, clean_channel(ch))
                        added += 1
                return {"added": added}
            if method == "POST" and sub == "import":
                data = self._body()
                found = radio.parse_import(data.get("csv") or "", (data.get("group") or "Repeaters").strip()[:30])
                if not found:
                    raise ApiError(400, "no channels found; use a CHIRP CSV or a RepeaterBook CSV export")
                for ch in found:
                    save_row("channels", CHANNEL_FIELDS, clean_channel(ch))
                return {"added": len(found)}
            if method == "POST":
                return {"id": save_row("channels", CHANNEL_FIELDS, clean_channel(self._body()), row_id)}
            if method == "DELETE" and row_id:
                store._exec("DELETE FROM channels WHERE id=?", (row_id,))
                return {"ok": True}

        if head == "family" and method == "GET":
            return family_view(full_state(self.user), admin=self.user["role"] == "admin")
        if head == "situation":
            sub_ = parts[1] if len(parts) > 1 else ""
            if method == "GET" and sub_ == "geo":
                return situation.geo(store, store.sites())
            if method == "GET":
                maps_dir = depot.root / "maps"
                return situation.view(store) | {"map": {
                    "viewer": (depot.root / "web" / "maps" / "maplibre-gl.js").exists(),
                    "pack": maps_dir.is_dir() and any(maps_dir.glob("*.pmtiles"))}}
            if self.user["role"] != "admin":
                raise ApiError(403, "only an admin can change that")
            if (method, sub_) == ("POST", "prefs"):
                data = self._body()
                clean = {k: [str(x).strip()[:60] for x in (data.get(k) or []) if str(x).strip()][:60] for k in ("devices", "local_orgs")}
                clean["state"] = str(data.get("state") or situation.DEFAULT_PREFS["state"]).strip()[:40]
                store.set_raw("situation_prefs", clean)
                return situation.prefs(store)
            if (method, sub_) == ("POST", "key"):
                data = self._body()
                if data.get("source") not in situation.KEYED:
                    raise ApiError(400, "unknown source")
                if ROLE in ("pi", "laptop"):
                    raise ApiError(400, "keys go on the hub; nodes get the results by sync")
                try:
                    have = situation.save_key(Path(store.path).parent, data["source"], data.get("value"))
                except ValueError as exc:
                    raise ApiError(400, str(exc))
                if have:
                    threading.Thread(target=refresh_situation, daemon=True).start()
                return {"saved": have}
            if (method, sub_) == ("POST", "refresh"):
                if ROLE in ("pi", "laptop"):
                    raise ApiError(400, "nodes get the situation from the hub when they sync")
                threading.Thread(target=refresh_situation, daemon=True).start()
                return {"started": True}
        if head == "brief":
            sub_ = parts[1] if len(parts) > 1 else ""
            if method == "GET":
                rec = brief.latest(store, query.get("day", [None])[0])
                return {"brief": rec, "days": brief.days(store), "settings": {k: BRIEF[k] for k in ("enabled", "time", "tz", "ai")},
                        "running": _brief_lock.locked()}
            if (method, sub_) == ("POST", "run"):
                if self.user["role"] != "admin":
                    raise ApiError(403, "only an admin can do that")
                if ROLE in ("pi", "laptop"):
                    raise ApiError(400, "nodes get the brief from the hub when they sync")
                if _brief_lock.locked():
                    return {"started": False, "running": True}
                threading.Thread(target=make_brief, kwargs={"force": True}, daemon=True).start()
                return {"started": True}
        if head == "push":
            sub_ = parts[1] if len(parts) > 1 else ""
            uid_ = self.user.get("id") or 0
            if (method, sub_) == ("GET", "key"):
                return {"key": notify.public_key_b64(notifier.key) if notifier else "", "prefs": notify.prefs(store, uid_),
                        "modes": notify.CHECKIN_MODES, "reminder_ways": notify.REMINDER_WAYS,
                        "email_ready": mailer.ready(store, Path(store.path).parent), "phones": len(notify.subscriptions(store, {uid_}))}
            if not uid_:
                raise ApiError(400, "notifications need a login")
            if (method, sub_) == ("POST", "subscribe"):
                data = self._body()
                try:
                    notify.save_subscription(store, uid_, data.get("subscription") or {}, self.headers.get("User-Agent", ""))
                except ValueError as exc:
                    raise ApiError(400, str(exc))
                if data.get("tz"):
                    p = notify.prefs(store, uid_) | notify.clean_prefs({"tz": data["tz"]})
                    store._exec("UPDATE users SET push_prefs=? WHERE id=?", (json.dumps(p), uid_))
                return {"ok": True}
            if (method, sub_) == ("POST", "unsubscribe"):
                notify.remove_subscription(store, str(self._body().get("endpoint") or ""), uid_)
                return {"ok": True}
            if (method, sub_) == ("POST", "prefs"):
                try:
                    p = notify.prefs(store, uid_) | notify.clean_prefs(self._body())
                except ValueError as exc:
                    raise ApiError(400, str(exc))
                store._exec("UPDATE users SET push_prefs=? WHERE id=?", (json.dumps(p), uid_))
                return p
            if (method, sub_) == ("POST", "test"):
                if not notifier:
                    raise ApiError(400, "notifications are off on this node")
                res = notify.send(store, notifier.key, "test", {"user_id": uid_})
                if not res:
                    raise ApiError(400, "this login has no phone set up for notifications yet")
                return {"sent": sum(1 for _, st in res if 200 <= st < 300), "tried": len(res)}
        if head == "notices":
            uid_ = self.user.get("id") or 0
            if method == "GET":
                return notify.notices(store, uid_)
            if method == "POST" and len(parts) > 1 and parts[1] == "read":
                ids = self._body().get("ids")
                if ids is not None and not (isinstance(ids, list) and all(isinstance(i, int) for i in ids)):
                    raise ApiError(400, "ids is a list of numbers")
                return {"unread": notify.mark_read(store, uid_, ids)}
        if head == "version":
            # which BUNKR this is; admins also see whether a newer one is out, and choose what to do about it
            if method == "GET" and len(parts) == 1:
                return selfupdate.view(store) if self.user["role"] == "admin" else {"current": __version__}
            if self.user["role"] != "admin":
                raise ApiError(403, "only an admin can do that")
            if method == "POST" and parts[1:] == ["check"]:
                if ROLE in ("pi", "laptop"):
                    raise ApiError(400, "the hub checks for new versions")
                return selfupdate.check(store)
            if method == "POST" and parts[1:] == ["settings"]:
                try:
                    return selfupdate.save_settings(store, self._body())
                except ValueError as exc:
                    raise ApiError(400, str(exc))
        if head == "localwatch":
            folder = Path(store.path).parent
            if method == "GET":
                return localwatch.view(store, folder) | {"news": store.get_raw("localwatch_news", {}) or {}}
            if method == "POST" and parts[1:] == ["run"]:
                threading.Thread(target=lambda: (refresh_news(brief.FAST), localwatch.run(store, folder)), daemon=True).start()
                return {"ok": True}
            if method == "POST" and len(parts) == 1:
                try:
                    return localwatch.save_settings(store, folder, self._body())
                except ValueError as exc:
                    raise ApiError(400, str(exc))
        if head == "mail":
            folder = Path(store.path).parent
            if method == "GET":
                return mailer.view(store, folder)
            if method == "POST" and len(parts) == 1:
                try:
                    return mailer.save_settings(store, folder, self._body())
                except ValueError as exc:
                    raise ApiError(400, str(exc))
            if method == "POST" and parts[1:] == ["test"]:
                to = str(self._body().get("to") or notify.prefs(store, self.user.get("id") or 0).get("email") or "").strip()
                if not notify.EMAIL_RE.match(to):
                    raise ApiError(400, "put your email address in your notification settings first, or type one here")
                if not mailer.ready(store, folder):
                    raise ApiError(400, "set the mail server and From address first")
                mailer.queue(store, to, "BUNKR test", "Email from BUNKR works. Reminders you set to email will come like this.")
                sent, failed = mailer.flush(store, folder)
                if not sent:
                    raise ApiError(400, "it didn't send: " + (mailer.view(store, folder)["last_error"] or "the mail server didn't answer") +
                                   ". BUNKR keeps trying for three days.")
                return {"sent": sent}
        if (method, head) == ("GET", "board"):
            return family_board()
        if head == "flights":
            return self._flights(method, parts)
        if head == "mesh":
            return self._mesh(method, parts)
        if head == "homeassistant":
            return self._homeassistant(method, parts)
        if (method, head) == ("POST", "checkin"):
            data = self._body()
            me = checkins.person_for_user(store, self.user)
            who = data.get("person_uid") or (me["uid"] if me else "")
            if not who:
                raise ApiError(400, "your login isn't linked to a person yet: link it on the Family tab")
            by = ""
            if not me or who != me["uid"]:
                if not checkins.can_call(store, self.user):
                    raise ApiError(403, "only an adult can check someone else in")
                by = self.user["username"]
            try:
                lat, lon = checkins._coord(data.get("lat"), 90), checkins._coord(data.get("lon"), 180)
                at_airport = flights.airport_near(lat, lon)
                if data.get("flight"):
                    flights.parse_number(data["flight"])  # refuse a bad flight number before saving anything
                place = data.get("place") or (f"{at_airport['iata']} airport" if at_airport else "")
                rec = checkins.add_checkin(store, who, data.get("status"), data.get("note"), place, lat, lon,
                                           data.get("via") or "app", by_name=by,
                                           battery_level=None if by else data.get("battery"),  # someone else's phone isn't theirs
                                           battery_low=bool(data.get("battery_low")))
                if data.get("flight"):
                    rec["flight"] = flights.add(store, who, data["flight"], origin=at_airport["iata"] if at_airport else "",
                                                by=self.user.get("username", ""), home=home_airport())["uid"]
                    threading.Thread(target=check_flights, daemon=True).start()
                elif at_airport and not any(f["person_uid"] == who for f in flights.active(store)):
                    rec["airport"] = {k: at_airport[k] for k in ("iata", "name", "city")}
                    home = home_airport()
                    if home and home["iata"] != at_airport["iata"]:
                        rec["airport"]["home"] = home["iata"]
                return rec
            except ValueError as exc:
                raise ApiError(400, str(exc))
        if head == "calls" and method == "POST":
            if not checkins.can_call(store, self.user):
                raise ApiError(403, "only an adult can send a family call")
            if path.rstrip("/").endswith("/end"):
                ended = checkins.end_call(store, self.user["username"])
                return {"ended": ended, "report": checkins.report(store, ended["uid"]) if ended else None}
            data = self._body()
            site = None
            if data.get("site_id"):
                rows = store._rows("SELECT * FROM sites WHERE id=?", (_num(data.get("site_id"), int, 0),))
                if not rows:
                    raise ApiError(400, "unknown place")
                site = rows[0]
            try:
                return checkins.start_call(store, data.get("kind"), self.user["username"],
                                           place=site["name"] if site else data.get("place"),
                                           address=site["address"] if site else data.get("address"),
                                           lat=site["lat"] if site else None, lon=site["lon"] if site else None,
                                           message=data.get("message"), drill=bool(data.get("drill")))
            except ValueError as exc:
                raise ApiError(400, str(exc))
        if head == "power":
            return self._power(method, row_id, parts)
        if head == "medical" and method == "GET":
            return medical_view(self.user)
        if head == "meds":
            return self._meds(method, row_id, parts)
        if head == "calls" and method == "GET":
            return {"calls": checkins.history(store, 10)}
        if head == "backups-check" and method == "POST":
            if self.user["role"] != "admin":
                raise ApiError(403, "only an admin can do that")
            dbcheck.nightly_copy(store)
            return dbcheck.restore_test(store)
        if (method, path.rstrip("/")) == ("POST", "/api/me/view"):
            view = self._body().get("view", "")
            if view not in auth.VIEWS:
                raise ApiError(400, "view must be simple or full")
            if self.user.get("id"):
                store._exec("UPDATE users SET view=? WHERE id=?", (view, self.user["id"]))
            return {"view": view}
        if (method, head) == ("GET", "family-plan.html"):
            return family_plan(), "text/html; charset=utf-8"
        if (method, head) == ("GET", "medical-cards.html"):
            return medical_cards(), "text/html; charset=utf-8"
        if (method, head) == ("GET", "calendar.ics"):
            st = full_state(self.user)
            return family.ics(family_view(st, admin=self.user["role"] == "admin")["calendar"]), "text/calendar; charset=utf-8"
        if (method, head) == ("POST", "plan-info"):
            data = self._body()
            info = {k: str(data.get(k) or "").strip()[:2000] for k in ("household_name", "mesh_channels", "notes")}
            store.set_raw("plan_info", info)
            return info
        if head == "people":
            sub = parts[2] if len(parts) > 2 else ""
            if method == "POST" and row_id and sub == "phone":
                data = self._body()
                person = store._rows("SELECT * FROM people WHERE id=?", (row_id,))
                if not person:
                    raise ApiError(404, "no such person")
                valid = {c["id"] for c in family.PHONE_CHECKS}
                checks = {k: True for k, v in (data.get("checks") or {}).items() if v and k in valid}
                fields = {"phone_checks": json.dumps(checks)}
                if data.get("mark_checked"):
                    fields["phone_checked_at"] = datetime.now().date().isoformat()
                store._exec(f"UPDATE people SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), row_id))
                return {"ok": True}
            if method == "POST":
                body = self._body()
                data = clean_text_fields(body, PEOPLE_FIELDS)
                data["role"] = data["role"] if data["role"] in ("adult", "child") else "adult"
                data["phone"] = data["phone"] if data["phone"] in ("android", "iphone", "none") else "none"
                data["remote"] = 1 if body.get("remote") in (True, 1, "1", "on", "true") else 0
                site = _num(body.get("site_id"), int, None)
                data["site_id"] = site if site and store._rows("SELECT id FROM sites WHERE id=?", (site,)) else None
                if self.user["role"] == "admin" and "user_id" in body:
                    uid_ = _num(body.get("user_id"), int, None)
                    if uid_ and not store._rows("SELECT id FROM users WHERE id=?", (uid_,)):
                        raise ApiError(400, "unknown login")
                    if uid_:  # a login belongs to one person
                        store._exec("UPDATE people SET user_id=NULL WHERE user_id=? AND id!=?", (uid_, row_id or 0))
                    data["user_id"] = uid_
                data["updated_at"] = checkins.now()
                if row_id is None:
                    data["uid"] = uuid.uuid4().hex
                return {"id": save_row("people", PEOPLE_FIELDS + ("remote", "site_id", "user_id", "updated_at", "uid"), data, row_id)}
            if method == "DELETE" and row_id:
                store._exec("DELETE FROM people WHERE id=?", (row_id,))
                return {"ok": True}
        if head == "contacts":
            if method == "POST":
                return {"id": save_row("contacts", CONTACT_FIELDS, clean_text_fields(self._body(), CONTACT_FIELDS), row_id)}
            if method == "DELETE" and row_id:
                store._exec("DELETE FROM contacts WHERE id=?", (row_id,))
                return {"ok": True}
        if head == "tasks":
            sub = parts[2] if len(parts) > 2 else ""
            task = store._rows("SELECT * FROM tasks WHERE id=?", (row_id,))[0] if row_id and store._rows(
                "SELECT id FROM tasks WHERE id=?", (row_id,)) else None
            if row_id and not task:
                raise ApiError(404, "no such task")
            if method == "POST" and sub == "done":
                store._exec("UPDATE tasks SET last_done=? WHERE id=?", (datetime.now().date().isoformat(), row_id))
                return {"ok": True}
            if method == "POST" and sub == "active":
                store._exec("UPDATE tasks SET active=? WHERE id=?", (1 if self._body().get("active") else 0, row_id))
                return {"ok": True}
            if method == "POST":
                data = self._body()
                title = (data.get("title") or "").strip()
                every = max(0, _num(data.get("every_days"), int, 0))
                yearly = (data.get("yearly") or "").strip()
                import re as _re
                if yearly and not _re.fullmatch(r"(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])", yearly):
                    raise ApiError(400, "yearly date must be MM-DD")
                if not title or not (every or yearly):
                    raise ApiError(400, "a task needs a title and either a repeat in days or a yearly date")
                fields = {"title": title[:200], "every_days": every if not yearly else 0, "yearly": yearly,
                          "notes": (data.get("notes") or "").strip()[:300]}
                if task and task["key"]:  # built-in tasks keep their identity; only timing and notes change
                    fields.pop("title")
                return {"id": save_row("tasks", tuple(fields), fields, row_id)}
            if method == "DELETE" and row_id:
                if task["key"]:
                    raise ApiError(400, "built-in tasks can be turned off but not deleted")
                store._exec("DELETE FROM tasks WHERE id=?", (row_id,))
                return {"ok": True}
        if head == "scenarios" and method == "POST" and len(parts) == 3:
            scn_id, action = parts[1], parts[2]
            if scn_id not in scenarios.BY_ID:
                raise ApiError(404, "no such scenario")
            run = store._rows("SELECT * FROM scenario_runs WHERE scenario=? AND ended_at=''", (scn_id,))
            now_s = datetime.now().isoformat(timespec="minutes")
            if action == "start":
                if not run:
                    store._exec("INSERT INTO scenario_runs(scenario, started_at) VALUES(?,?)", (scn_id, now_s))
                return {"ok": True}
            if not run:
                raise ApiError(400, "start this checklist first")
            if action == "check":
                data = self._body()
                checked = json.loads(run[0]["checked"] or "{}")
                step = str(data.get("step", ""))[:40]
                if data.get("done"):
                    checked[step] = {"by": self.user["username"], "at": now_s}
                else:
                    checked.pop(step, None)
                store._exec("UPDATE scenario_runs SET checked=? WHERE id=?", (json.dumps(checked), run[0]["id"]))
                return {"ok": True}
            if action == "end":
                store._exec("UPDATE scenario_runs SET ended_at=? WHERE id=?", (now_s, run[0]["id"]))
                return {"ok": True}

        if head == "maps":
            sub = parts[1] if len(parts) > 1 else ""
            if method == "GET" and not sub:
                held = [r for r in depot.rows() if r["kind"] == "map"]
                return {"settings": mapspack.settings(store), "defaults": mapspack.DEFAULTS, "built": store.get_raw("maps_built"),
                        "files": held, "stale": mapspack.needs_rebuild(store, store.sites()),
                        "viewer": (depot.root / "web" / "maps" / "maplibre-gl.js").exists()}
            if method == "GET" and sub == "estimate":
                try:
                    return mapspack.estimate(store.sites(), mapspack.settings(store))
                except ValueError as exc:
                    raise ApiError(400, str(exc))
            if method == "POST" and sub == "settings":
                data = self._body()
                cfg = {"detail_km": min(150, max(1, _num(data.get("detail_km"), int, 25))),
                       "detail_zoom": min(15, max(10, _num(data.get("detail_zoom"), int, 15))),
                       "region_km": min(800, max(10, _num(data.get("region_km"), int, 150))),
                       "region_zoom": min(12, max(5, _num(data.get("region_zoom"), int, 11)))}
                if cfg["region_zoom"] >= cfg["detail_zoom"]:
                    raise ApiError(400, "the overview zoom must be below the detail zoom")
                store.set_raw("maps_settings", cfg)
                return mapspack.settings(store)
            if method == "POST" and sub == "build":
                if not any(x.get("lat") is not None for x in store.sites()):
                    raise ApiError(400, "add a site with an address or coordinates first")
                try:
                    depot_job.start(build_maps_job)
                except RuntimeError as exc:
                    raise ApiError(409, "the downloads store is busy: " + str(exc))
                return depot_job.snapshot()

        if head == "nodes":
            if method == "GET" and not row_id:
                return {"nodes": nodes_view(), "roles": nodes.ROLES, "defaults": nodes.DEFAULT_PROFILES,
                        "lock_passphrase_set": store.get_raw("vault_wrapped_at", "") or ""}
            if method == "POST" and parts[1:2] == ["lock-passphrase"]:
                data = self._body()
                if data.get("passphrase") != data.get("repeat"):
                    raise ApiError(400, "the two passphrases don't match")
                try:
                    store.set_raw("vault_wrapped", vault.wrap(hub_vault_key(), data.get("passphrase") or ""))
                except vault.VaultError as exc:
                    raise ApiError(400, str(exc))
                store.set_raw("vault_wrapped_at", manifest.now())
                audit.record(store, self.user, "security", "node lock", "set the node-lock passphrase", self._client_ip())
                return {"ok": True}
            if method == "POST" and row_id and parts[2:3] == ["lost"]:
                n = nodes.get(store, row_id)
                if not n:
                    raise ApiError(404, "no such node")
                nodes.set_fields(store, row_id, token_hash="", lost=manifest.now())
                audit.record(store, self.user, "security", "node lock", f"marked {n['name']} lost or stolen", self._client_ip(),
                             alert="node_lost")
                return {"ok": True}
            if method == "GET" and row_id and len(parts) == 3 and parts[2] == "plan":
                n = nodes.get(store, row_id)
                if not n:
                    raise ApiError(404, "no such node")
                p = node_plan(n)
                return {"files": nodes.plan_files(p), "skipped": [{k: s[k] for k in ("path", "size", "reason")} for s in p["skipped"]],
                        "used": p["used"], "budget": p["budget"]}
            if method == "POST" and row_id and len(parts) == 3 and parts[2] == "pair":
                if not nodes.get(store, row_id):
                    raise ApiError(404, "no such node")
                code, expires = nodes.new_pair_code(store, row_id)
                return {"code": code, "expires": expires}
            if method == "POST" and row_id and len(parts) == 3 and parts[2] == "reminder":
                n = nodes.get(store, row_id)
                kind = self._body().get("kind", "")
                if not n or kind not in nodes.REMINDERS.get(n["role"], {}):
                    raise ApiError(400, "unknown reminder")
                rem = json.loads(n["reminders"] or "{}") | {kind: datetime.now().date().isoformat()}
                nodes.set_fields(store, row_id, reminders=json.dumps(rem))
                return {"ok": True}
            if method == "POST":
                data = self._body()
                name = (data.get("name") or "").strip()
                if not name:
                    raise ApiError(400, "node name is required")
                role = data.get("role") if data.get("role") in nodes.ROLES else "drive"
                prof = data.get("profile") if isinstance(data.get("profile"), dict) else nodes.DEFAULT_PROFILES[role]
                prof = {k: prof[k] for k in nodes.DEFAULT_PROFILES[role] if k in prof}
                fields = {"name": name, "role": role, "notes": (data.get("notes") or "").strip(), "profile": prof,
                          "budget_bytes": int(max(0, _num(data.get("budget_gb"), float, 0)) * 1e9)}
                return {"id": nodes.save(store, fields, row_id)}
            if method == "DELETE" and row_id:
                nodes.delete(store, row_id)
                return {"ok": True}

        if head == "documents":
            if method == "GET":
                return documents_with_sizes()
            if method == "POST" and row_id and len(parts) == 3 and parts[2] == "renewed":
                if not store._rows("SELECT 1 FROM documents WHERE id=?", (row_id,)):
                    raise ApiError(404, "no such document")
                renewals.mark_done(store, row_id)
                return {"ok": True}
            if method == "POST":
                data = self._body()
                try:
                    renew_on = renewals.clean_date(data.get("renew_on"))
                except ValueError as exc:
                    raise ApiError(400, str(exc))
                doc = {"label": (data.get("label") or "").strip(),
                       "doc_type": data.get("doc_type") if data.get("doc_type") in gaps.DOC_TYPES else "other",
                       "priority": min(3, max(1, _num(data.get("priority"), int, 2))), "renew_on": renew_on,
                       "insurer": str(data.get("insurer") or "").strip()[:120], "coverage": str(data.get("coverage") or "").strip()[:300],
                       "claim_phone": str(data.get("claim_phone") or "").strip()[:40]}
                if row_id is None:
                    p = os.path.realpath(os.path.expanduser(data.get("path") or ""))
                    if not os.path.exists(p):
                        raise ApiError(400, f"{p} does not exist")
                    doc["path"] = p
                    doc["label"] = doc["label"] or Path(p).stem
                return {"id": store.save_document(doc, row_id)}
            if method == "DELETE" and row_id:
                store.delete_document(row_id)
                return {"ok": True}

        if (method, head) == ("GET", "browse"):
            return browse(query.get("path", [""])[0])
        if (method, head) == ("GET", "drives"):
            return drives.list_drives()
        if (method, head) == ("GET", "library") and len(parts) == 1:
            libs, tools = library.resolve_all(set(store.settings()["kiwix_disabled"]))
            return {"libraries": libs, "tools": tools, "reader": kiwix_status()}
        if head == "library" and parts[1:2] == ["catalog"] and method == "GET":
            try:
                found = library.search_catalog(query.get("q", [""])[0], lang=query.get("lang", ["eng"])[0][:8])
            except Exception as exc:
                raise ApiError(502, f"couldn't reach the Kiwix catalog ({type(exc).__name__})")
            return {"results": [r | {"added": library.listed(r["name"], r["flavour"])} for r in found]}
        if (method, head) == ("POST", "library") and parts[1:2] == ["address"]:
            u = str(self._body().get("url") or "").strip()
            if u and not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(/[\w./~-]*)?", u):
                raise ApiError(400, "that doesn't look like a web address (https://library.example.org/kiwix/)")
            store.set_raw("library_url", u)
            return {"url": u}
        if head == "library" and parts[1:2] == ["custom"]:
            extra = kiwix_extra()
            if method == "POST":
                data = self._body()
                try:
                    e = library.extra_entry(data.get("name"), data.get("flavour"), data.get("title"), data.get("summary"), data.get("size"))
                except ValueError as exc:
                    raise ApiError(400, str(exc))
                if library.listed(e["name"], e["flavour"]):
                    raise ApiError(400, "that book is already on the list")
                if len(extra) >= 100:
                    raise ApiError(400, "that's a lot of books; remove some first")
                store.set_raw("kiwix_extra", extra + [e])
                return {"id": e["id"]}
            if method == "DELETE" and len(parts) > 2:
                store.set_raw("kiwix_extra", [e for e in extra if e["id"] != parts[2]])
                return {"ok": True}
        if (method, head) == ("GET", "kiwix") and parts[1:2] == ["search"]:
            try:
                return {"results": kiwix.search(query.get("q", [""])[0], KIWIX["url"])}
            except (OSError, ET.ParseError) as exc:
                raise ApiError(503, "the offline library isn't running here" if isinstance(exc, OSError) else "the library gave an answer BUNKR can't read")

        if (method, head) == ("POST", "plan"):
            info, the_plan, *_ = kit_inputs(self._body().get("mount", ""))
            return {"drive": info, "plan": the_plan}
        if (method, head) == ("POST", "build"):
            data = self._body()
            passphrase = data.get("passphrase") or ""
            info, the_plan, docs, tools, libs, settings = kit_inputs(data.get("mount", ""))
            if any(i["kind"] == "document" for i in the_plan["included"]) and len(passphrase) < 10:
                raise ApiError(400, "use a passphrase of at least 10 characters")
            try:
                job.start(builder.build, info["mount"], the_plan, docs, tools, libs,
                          settings, gaps.visible(store.items(), "family"), passphrase, depot, record_drive_manifest,
                          info.get("label") or info.get("model") or "", archive_extras())
            except RuntimeError as exc:
                raise ApiError(409, str(exc))
            return job.snapshot()
        if (method, head) == ("GET", "job"):
            return job.snapshot()
        if (method, head) == ("POST", "cancel"):
            job.cancel()
            return job.snapshot()

        if head == "backups":
            if method == "POST" and len(parts) == 3 and parts[2] == "run":
                return {"status": run_backup(row_id)}
            if method == "POST":
                data = self._body()
                fields = {"name": (data.get("name") or "").strip() or "Backup",
                          "source": (data.get("source") or "").strip(),
                          "dest": (data.get("dest") or "").strip(),
                          "keep": min(365, max(1, _num(data.get("keep"), int, 7)))}
                if not fields["source"] or not fields["dest"]:
                    raise ApiError(400, "source and destination are required")
                return {"id": store.save_backup_job(fields, row_id)}
            if method == "DELETE" and row_id:
                store.delete_backup_job(row_id)
                return {"ok": True}

        if (method, head) == ("GET", "audit"):
            uid = _num(query.get("user", [""])[0], int, None)
            what = (query.get("what", [""])[0] or "")[:40] or None
            return {"rows": audit.rows(store, 300, uid, what, _num(query.get("before", [""])[0], int, None)),
                    "check": audit.verify(store), "users": [{"id": u["id"], "username": u["username"]} for u in auth.users(store)],
                    "whats": [r["what"] for r in store._rows("SELECT DISTINCT what FROM audit_log ORDER BY what")]}
        if (method, head) == ("GET", "audit.csv"):
            return export_audit_csv(), "text/csv; charset=utf-8"

        if (method, head) == ("GET", "export.csv"):
            return export.inventory_csv(gaps.visible(store.items(), self.user["role"])), "text/csv; charset=utf-8"
        if (method, head) == ("GET", "report.html"):
            s = store.settings()
            return export.report_html(s, gaps.visible(store.items(), self.user["role"]), store.documents()), "text/html; charset=utf-8"

        raise ApiError(404, "unknown endpoint")


TWO_STEP_POLICIES = ("none", "admins", "everyone")


def two_step_required(user):
    pol = store.get_raw("two_step_policy", "none") or "none"
    return pol == "everyone" or (pol == "admins" and user["role"] == "admin")


def export_audit_csv():
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["when (UTC)", "who", "action", "what", "detail", "from"])
    for r in audit.rows(store, 1000):
        w.writerow([r["at"], r["username"], r["action"], r["what"], r["detail"], r["ip"]])
    return out.getvalue()


def _stale(stamp, seconds):
    if not stamp:
        return True
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    now = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
    return (now - then).total_seconds() > seconds


def refresh_alerts():
    """Alerts for every site with coordinates; hazard profiles once a month."""
    for site in store.sites():
        if _stale(site["alerts_at"], ALERT_REFRESH - 60) or site["alerts_error"]:
            sites.refresh_site(store, site, hazards,
                               with_hazards=_stale(site["hazards_at"], HAZARD_REFRESH))


def bot_context():
    """What the mesh bot can look up (bunkr/meshbot.py). Each answer is one short line."""
    def ready(rest, adult):
        a = gaps.analyze(store.settings(), gaps.visible(store.items(), "family"), store.documents(),
                         hazard_profile=hazards.home_profile(store.sites()))
        gap = (a.get("gaps") or [{}])[0]
        gap = gap.get("label") or gap.get("name") or "" if isinstance(gap, dict) else str(gap)
        n = lambda v: f"{v:g}" if isinstance(v, (int, float)) else str(v)
        return f"{a['overall']}% ready. Water {n(a['water_days'])} of {n(a['target_days'])} days, food {n(a['food_days'])} days." + (f" Gap: {gap}" if gap else "")

    def alerts(rest, adult):
        top = [i for i in situation.items(store) if i["tier"] in ("flash", "priority")][:3]
        if not top:
            return "Nothing near us right now."
        return "; ".join(i["title"] + (f" ({i['km']} km)" if i.get("km") not in (None, 0) else " (here)" if i.get("km") == 0 else "") for i in top)

    def flight(rest, adult):
        fl = flights.board(store)
        if not fl:
            return "No flights being followed."
        out = []
        for f in fl[:2]:
            bit = f"{f['person_name'].split()[0]} {f['ident']} {flights.FLIGHT_WORDS.get(f['status'], f['status'])}"
            if f["late_min"] and f["late_min"] >= 15:
                bit += f" {f['late_min']}m late"
            if f["pos"] and not f["pos"].get("ground") and f["pos"].get("km_to_go") is not None:
                bit += f", {round(f['pos']['km_to_go'] * 0.621)} mi to {f['dest'] or 'go'}"
            out.append(bit)
        return "; ".join(out)

    def news_(rest, adult):
        rec = brief.latest(store)
        return brief.one_line(rec) if rec else "No brief yet."

    def home(rest, adult):
        return homeassistant.home_command(store, Path(store.path).parent, rest, adult)

    return {"ready": ready, "alerts": alerts, "flight": flight, "news": news_, "home": home}


def setup_mesh():
    """One background link per machine; it idles until an admin points it at a radio. Home Assistant events too."""
    if MESH["link"] is None:
        MESH["link"] = mesh.Link(lambda: store, ctx=bot_context)
        MESH["link"].start()
    if MESH["link"] not in checkins.listeners:
        checkins.listeners.append(MESH["link"])
    if MESH.get("ha") is None:
        MESH["ha"] = homeassistant.Events(lambda: store, lambda: Path(store.path).parent)
    for listeners in (checkins.listeners, situation.listeners, flights.listeners):
        if MESH["ha"] not in listeners:
            listeners.append(MESH["ha"])


def setup_notifier():
    """Pushes go out from the hub. A node (Pi, laptop) only sends its own when it hasn't reached the hub lately."""
    global notifier

    def enabled():
        if ROLE not in ("pi", "laptop"):
            return True
        last = store.get_raw("last_node_sync", "") or ""
        return not last or last < (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds")

    folder = Path(store.path).parent

    def mail(to, msg):
        mailer.reminder(store, to, msg, PUBLIC_URL)
        threading.Thread(target=mailer.flush, args=(store, folder), daemon=True).start()

    notifier = notify.Notifier(store, folder / "vapid.pem", enabled, mail=mail)
    for listeners in (checkins.listeners, situation.listeners, medical.listeners, audit.listeners, flights.listeners,
                      renewals.listeners, news.listeners, localwatch.listeners, selfupdate.listeners):
        if notifier not in listeners:
            listeners[:] = [l for l in listeners if not isinstance(l, notify.Notifier)] + [notifier]  # others (mesh) stay


def _brief_day(now_=None):
    import zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(BRIEF["tz"])
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    local = (now_ or datetime.now(timezone.utc)).astimezone(tz)
    return local.date().isoformat(), local.strftime("%H:%M")


_flights_lock = threading.Lock()


def check_flights():
    """Bring followed flights up to date (every two minutes on the hub, and right after one is added)."""
    if ROLE in ("pi", "laptop") or not flights.active(store) or not _flights_lock.acquire(blocking=False):
        return None
    try:
        return flights.check_all(store, key=flights.load_key(Path(store.path).parent))
    finally:
        _flights_lock.release()


_news_lock = threading.Lock()


def refresh_news(keys=None):
    """Fetch headlines, file them into stories, tag new ones (with Claude when the brief uses it). Hourly on the hub,
    and the breaking-news and specialist feeds (brief.FAST) every 10 minutes."""
    if not _news_lock.acquire(blocking=False):
        return None
    try:
        heads, _status = brief.fetch_all(keys)
        places = localwatch.places(store)
        if places:  # each family town's own news search, every 10 minutes with the fast feeds
            local, st = localwatch.fetch_places(places)
            heads += local
            store.set_raw("localwatch_news", st)
        ai = (lambda p: brief.run_claude(p, env_file=BRIEF["env_file"] or None, home=str(Path(store.path).parent / "claude-home"),
                                         claude=BRIEF["claude"], timeout=300)) if BRIEF["ai"] and BRIEF["enabled"] else None
        return news.refresh(store, heads, ai=ai, places=places)
    finally:
        _news_lock.release()


def make_brief(force=False, now_=None):
    """Build today's brief once its time has come (or now, when forced), then notify. Safe to call often."""
    if not BRIEF["enabled"] and not force:
        return None
    day, hhmm = _brief_day(now_)
    if not force and (hhmm < BRIEF["time"] or brief.latest(store, day)):
        return None
    if not _brief_lock.acquire(blocking=False):
        return None
    try:
        ai = (lambda p: brief.run_claude(p, env_file=BRIEF["env_file"] or None,
                                         home=str(Path(store.path).parent / "claude-home"), claude=BRIEF["claude"])) if BRIEF["ai"] else None
        rec = brief.build(store, situation.items(store), use_ai=BRIEF["ai"], ai=ai)
        brief.save(store, rec, day)
        for fn in list(situation.listeners):
            if isinstance(fn, notify.Notifier):
                fn("brief", {"title": "Morning brief", "body": brief.one_line(rec)[:180]})
        return rec
    finally:
        _brief_lock.release()


def refresh_situation():
    """Public feeds, ranked against our places. The hub (or a standalone install) fetches; nodes get it by sync."""
    situation.refresh(store, store.sites(), keys=situation.load_keys(Path(store.path).parent))


def _open_only(fn):
    """Background jobs need the database: skip them while a node is locked."""
    def run():
        if store is not None and not (LOCK["on"] and LOCK["locked"]):
            return fn()
    return run


def start_background():
    global scheduler
    from .scheduler import Scheduler
    scheduler = Scheduler()
    _every = scheduler.every
    scheduler.every = lambda name, seconds, fn, **kw: _every(name, seconds, _open_only(fn), **kw)
    if LOCK["on"]:
        _every("node-lock", 60, node_lock_tick, first_delay=2)
        scheduler.every("node-sync", 3600, node_records_sync, first_delay=90)
    scheduler.every("alerts", ALERT_REFRESH, refresh_alerts, first_delay=10)
    scheduler.every("updates", 3600, nightly_updates, at_hour=3)
    scheduler.every("maps", 3600, maps_refresh, at_hour=4)
    scheduler.every("chirp", 3600, chirp_nightly, at_hour=3)
    scheduler.every("publish", 3600, publish_generated, first_delay=30)
    if not LOCK["on"]:  # a locked node's database lives only in its vault; no plain copies
        scheduler.every("db-backup", 3600, lambda: dbcheck.nightly(store), at_hour=2)
    scheduler.every("audit", 3600, lambda: audit.nightly(store), at_hour=2)
    scheduler.every("medical", 3600, lambda: medical.remind(store), at_hour=8)
    scheduler.every("renewals", 3600, lambda: renewals.remind(store), at_hour=8)
    scheduler.every("mail", 300, lambda: mailer.flush(store, Path(store.path).parent), first_delay=60)
    if KIWIX["manager"]:
        scheduler.every("kiwix", 60, kiwix_watch, first_delay=5)
    if ROLE not in ("pi", "laptop"):
        scheduler.every("situation", 600, refresh_situation, first_delay=40)
        scheduler.every("brief", 600, make_brief, first_delay=90)
        scheduler.every("news", 3600, refresh_news, first_delay=150)
        scheduler.every("news-fast", 600, lambda: refresh_news(brief.FAST), first_delay=400)
        scheduler.every("chatter", 600, lambda: localwatch.run(store, Path(store.path).parent), first_delay=460)
        scheduler.every("flights", flights.CHECK_EVERY, check_flights, first_delay=20)
        scheduler.every("bunkr-version", 24 * 3600, lambda: selfupdate.check(store), first_delay=600)
    scheduler.start()


def main():
    global store
    ap = argparse.ArgumentParser(description="BUNKR preparedness tracker")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", help="database file (default ~/.local/share/bunkr/bunkr.db)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--bind", default="127.0.0.1", metavar="ADDR",
                    help="address to listen on (default 127.0.0.1); firewall the port if you change it")
    ap.add_argument("--require-login", action="store_true",
                    help="require a BUNKR login for every page (needed whenever other devices can reach it)")
    ap.add_argument("--role", choices=["standalone", "hub", "pi", "laptop"], default="standalone")
    ap.add_argument("--hub-url", default="", help="the hub's address (field nodes)")
    ap.add_argument("--store", metavar="DIR", help="downloads store (default: 'store' next to the database)")
    ap.add_argument("--brief-time", default="06:30", metavar="HH:MM", help="when the morning brief is made (local time, --brief-tz)")
    ap.add_argument("--brief-tz", default="America/Denver", help="time zone for the brief")
    ap.add_argument("--no-brief", action="store_true", help="don't make a morning brief")
    ap.add_argument("--no-brief-ai", action="store_true", help="headlines only, no AI summary")
    ap.add_argument("--brief-env-file", default="", metavar="FILE",
                    help="file with CLAUDE_CODE_OAUTH_TOKEN=... for the AI summary (default: claude.env next to the database)")
    ap.add_argument("--claude", default="claude", metavar="PATH", help="the claude command")
    ap.add_argument("--public-url", default="", metavar="URL",
                    help="public address of this BUNKR for the Claude connector, e.g. https://bunkr.example.org")
    ap.add_argument("--open-net", action="append", default=[], metavar="CIDR",
                    help="clients on this network get the read-only field page without a login, e.g. the "
                         "Pi's hotspot 10.42.0.0/24 (repeatable; the Wi-Fi password is the gate)")
    ap.add_argument("--lock", action="store_true",
                    help="node lock (Pi, laptop): keep the database encrypted; unlock from the hub or with the family passphrase")
    ap.add_argument("--token-file", default="/etc/bunkr/node.token", help="the node's sync token (node lock asks the hub with it)")
    ap.add_argument("--kiwix", choices=["auto", "off"], default="auto",
                    help="hub: run the offline library reader (kiwix-serve) on 127.0.0.1:8080 unless one already answers there")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="also accept requests for this hostname (repeatable)")
    args = ap.parse_args()
    ALLOWED_HOSTS.update(h.strip().lower() for h in args.allow_host if h.strip())
    global REQUIRE_LOGIN, ROLE, HUB_URL
    REQUIRE_LOGIN, ROLE, HUB_URL = args.require_login, args.role, args.hub_url
    OPEN_NETS[:] = [ipaddress.ip_network(n, strict=False) for n in args.open_net]
    global PUBLIC_URL
    PUBLIC_URL = args.public_url.strip()
    BRIEF.update(enabled=not args.no_brief, ai=not args.no_brief_ai, tz=args.brief_tz, claude=args.claude,
                 time=args.brief_time if re.fullmatch(r"\d\d:\d\d", args.brief_time) else "06:30",
                 env_file=args.brief_env_file or str(Path(args.db).parent / "claude.env"))
    if args.bind not in ("127.0.0.1", "localhost", "::1") and not args.require_login:
        print("WARNING: listening beyond this computer without --require-login; firewall the port.")
    global depot
    from .db import DATA_DIR
    db_path = args.db or str(DATA_DIR / "bunkr.db")
    if args.lock and args.role not in ("pi", "laptop"):
        print("Node lock is for a Pi or laptop node (--role pi or laptop); ignoring --lock.")
        args.lock = False
    if args.lock:
        LOCK.update(on=True, vault=vault.NodeVault(db_path), token_file=args.token_file, db=db_path)
    if LOCK["on"] and LOCK["vault"].exists():
        store = None
        LOCK["locked"] = True
        print("Node lock: locked. It unlocks from the hub at home, or with the family passphrase on its page.")
    else:
        store = Store(args.db)
        if LOCK["on"]:
            print("Node lock: waiting for the key from the hub; until then the database is not locked.")
    depot = Depot(store, Path(args.store) if args.store else Path(db_path).parent / "store")
    if store is not None:
        setup_notifier()
        setup_mesh()
    depot.ensure()
    if ROLE == "hub" and args.kiwix == "auto" and not kiwix.alive() and kiwix.Manager.available():
        KIWIX["manager"] = kiwix.Manager(depot.root / "library", Path(store.path).parent / "kiwix")
    if store is not None:
        family.ensure_tasks(store)
    if LOCK["on"]:
        import atexit
        import signal
        atexit.register(seal_now)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if KIWIX["manager"]:
        import atexit
        import signal
        atexit.register(KIWIX["manager"].stop)  # don't leave kiwix-serve running when BUNKR stops
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        threading.Thread(target=kiwix_watch, daemon=True, name="kiwix-start").start()
    start_background()
    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"BUNKR running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
