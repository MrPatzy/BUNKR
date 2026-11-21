"""Maps pack: an offline map covering every saved site, cut from the Protomaps daily build.

Two layers of detail: a regional overview around all sites (default 150 km, to
zoom 11) and street-level detail around each site (default 25 km, zoom 12-15).
The result is one .pmtiles file in the downloads store, plus the map viewer and its
pinned libraries, so nodes and the hub can show it with no internet.
"""

import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import manifest, pmtiles

BUILDS = "https://build-metadata.protomaps.dev/builds.json"
BUILD_URL = "https://build.protomaps.com/{key}"
DEFAULTS = {"detail_km": 25, "detail_zoom": 15, "region_km": 150, "region_zoom": 11}
REBUILD_DAYS = 30

# The viewer and the libraries it needs, pinned by SHA-256 (same pins as deploy/install.sh).
_FONTS = "https://cdn.jsdelivr.net/gh/protomaps/basemaps-assets@028c18f713baecad011301ff7a69acc39bcc2ae7/fonts/"
VIEWER_ASSETS = [
    ("maplibre-gl.js", "https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.js",
     "45a9b07a9189ce56054c620a947ccf41e291e58c95e9b61533b740aaa65ee5cb"),
    ("maplibre-gl.css", "https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.css",
     "ab1e70d59ec40465bae7e7030da2f3ccf28133fd502e62bd598eefbadfd7a732"),
    ("pmtiles.js", "https://cdn.jsdelivr.net/npm/pmtiles@4.5.0/dist/pmtiles.js",
     "caf981bc46f6327ee7e65d5dc964d89d38a69f60edca2bd4c5c890c21b554c6c"),
    ("fonts/Noto Sans Regular/0-255.pbf", _FONTS + "Noto%20Sans%20Regular/0-255.pbf",
     "62c6d49b15fa836eb6aa45e259c7ca6762f44b011b09e47776efbe4a6db1b397"),
    ("fonts/Noto Sans Regular/256-511.pbf", _FONTS + "Noto%20Sans%20Regular/256-511.pbf",
     "2eca7561f9f566bcacfda5dd04fb5880baec1328ec0f5484678289a13994de8a"),
    ("fonts/Noto Sans Regular/8192-8447.pbf", _FONTS + "Noto%20Sans%20Regular/8192-8447.pbf",
     "8ea977a587352fe31b4159ffdbc9a40be79056f2472017c742ea1e4a931864b9"),
    ("fonts/Noto Sans Medium/0-255.pbf", _FONTS + "Noto%20Sans%20Medium/0-255.pbf",
     "ba2f0118dd024e3041b158e5f9eb49bc0a658019f53f458e9f5c0b8efcd79b91"),
    ("fonts/Noto Sans Medium/256-511.pbf", _FONTS + "Noto%20Sans%20Medium/256-511.pbf",
     "d5e801a1a5b1d409d3298c3a1e1ca76328e2314a751078833a618620e8e66e4d"),
    ("fonts/Noto Sans Medium/8192-8447.pbf", _FONTS + "Noto%20Sans%20Medium/8192-8447.pbf",
     "cc38e4956207f0edba1aaf749b61e8f6f1678ef1ad443d32ee285b21d0eb67aa"),
    ("fonts/OFL.txt", _FONTS + "OFL.txt", "7713cfc8e3c36d5ec4aa3d6cffe7500a1b3310f8a86d914b8ea09c2a9dee7c2d"),
]


def settings(store):
    return DEFAULTS | (store.get_raw("maps_settings", {}) or {})


def areas_for(sites, cfg):
    pts = [(s["lat"], s["lon"]) for s in sites if s.get("lat") is not None and s.get("lon") is not None]
    if not pts:
        return []
    out = []
    regions = [pmtiles.bbox_around(lat, lon, cfg["region_km"]) for lat, lon in pts]
    w, s_ = min(r[0] for r in regions), min(r[1] for r in regions)
    e, n = max(r[2] for r in regions), max(r[3] for r in regions)
    out.append(((w, s_, e, n), 0, cfg["region_zoom"]))
    for lat, lon in pts:
        out.append((pmtiles.bbox_around(lat, lon, cfg["detail_km"]), cfg["region_zoom"] + 1, cfg["detail_zoom"]))
    return out


def fingerprint(sites, cfg):
    pts = sorted((round(s["lat"], 3), round(s["lon"], 3)) for s in sites if s.get("lat") is not None)
    return hashlib.sha256(json.dumps([pts, cfg], sort_keys=True).encode()).hexdigest()[:16]


def latest_build():
    req = urllib.request.Request(BUILDS, headers={"User-Agent": pmtiles.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        builds = json.load(r)
    b = builds[-1]
    return {"key": b["key"], "url": BUILD_URL.format(key=b["key"]), "version": b["key"].split(".")[0],
            "size": b.get("size", 0)}


def estimate(sites, cfg):
    areas = areas_for(sites, cfg)
    if not areas:
        raise ValueError("add a site with an address or coordinates first")
    b = latest_build()
    est = pmtiles.estimate(pmtiles.HttpSource(b["url"]), areas)
    return {**est, "build": b["version"], "areas": len(areas)}


def ensure_viewer(depot, log=print):
    """Put the map viewer and its pinned libraries into the store (kind 'web', synced to nodes)."""
    from . import library
    base = depot.root / "web" / "maps"
    src_index = Path(__file__).resolve().parents[1] / "deploy" / "pi" / "maps" / "index.html"
    files = [("index.html", None, None)] + VIEWER_ASSETS
    for name, url, sha in files:
        dest = base / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if url is None:
            data = src_index.read_bytes()
            if not dest.exists() or dest.read_bytes() != data:
                dest.write_bytes(data)
        elif not dest.exists() or (sha and manifest.sha256_file(dest) != sha):
            library.download(url, dest, 0, sha)
            log(f"Viewer: fetched {name}")
        rel = f"web/maps/{name}"
        row = depot.by_path(rel)
        digest = manifest.sha256_file(dest)
        if not row or row["sha256"] != digest:
            depot.record("web", "maps-viewer", rel, "1", dest.stat().st_size, digest, url or "")


def build(depot, store, sites, progress=None, cancelled=lambda: False, log=print):
    """Cut the maps pack and record it in the store. Returns the new store row."""
    cfg = settings(store)
    areas = areas_for(sites, cfg)
    if not areas:
        raise ValueError("add a site with an address or coordinates first")
    ensure_viewer(depot, log)
    b = latest_build()
    log(f"Protomaps world build {b['version']}: cutting {len(areas)} area(s) "
        f"(region {cfg['region_km']} km to zoom {cfg['region_zoom']}, detail {cfg['detail_km']} km to zoom {cfg['detail_zoom']})")
    name = f"bunkr-sites-{b['version']}.pmtiles"
    dest = depot.root / "maps" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    stats = pmtiles.extract(pmtiles.HttpSource(b["url"]), areas, dest,
                            progress=progress, cancelled=cancelled)
    log(f"Map written: {stats['tiles']:,} tiles, {stats['bytes'] / 1e6:.0f} MB, {stats['requests']} requests")
    depot.record("map", "sites", f"maps/{name}", b["version"], dest.stat().st_size, manifest.sha256_file(dest), b["url"])
    depot.prune_older("map", "sites", f"maps/{name}")
    store.set_raw("maps_built", {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 "fingerprint": fingerprint(sites, cfg), "build": b["version"], "stats": stats})
    return depot.by_path(f"maps/{name}")


def needs_rebuild(store, sites):
    """True when a pack exists and is a month old, or sites/settings changed since it was cut."""
    built = store.get_raw("maps_built")
    if not built:
        return False
    if built.get("fingerprint") != fingerprint(sites, settings(store)):
        return True
    then = datetime.fromisoformat(built["at"])
    return (datetime.now(timezone.utc) - then).days >= REBUILD_DAYS
