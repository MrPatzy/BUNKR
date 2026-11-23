"""Offline packs: lists of downloads the family chooses, kept in the store and synced to the nodes.

A pack has a name and items. Each item is one of:
- a web file: an https address, optionally with the SHA-256 it must have;
- a book from the Kiwix catalog (name and flavour, like the library's own books);
- an Ubuntu/Debian package (fetched with what it depends on, into an apt folder of the pack's own).

The hub fetches every pack weekly (with the OS packages) and when an admin presses "Fetch now", into
`store/packs/<slug>/`. A download goes to `store/packs/.staging/<slug>/` first and replaces the old copy only once it
is complete and its checksum matches, so a failed or tampered download never costs the copy you had. Every file is
recorded in the store (kind "pack", item "<slug>:<item id>"), so the nightly checksum check, the node planner and sync
treat them like everything else. Each pack also gets `pack.json` (the same as its export) so a node knows what it holds.

Settings (no table of their own): "offline_packs" (the lists), "packs_state" (what each item's last fetch found),
"packs_settings" (size limit per item, home-network addresses allowed) and "os_packages_extra" (OS package names
admins added to the built-in list in packages.py).

Web addresses go through safeget.py: https only, no private addresses, a size cap, never run.
"""

import hashlib
import json
import os
import re
import shutil
import threading
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import library, manifest, packages, safeget

FORMAT = 1
MAX_PACKS = 50
MAX_ITEMS = 200
MAX_OS_EXTRA = 100
MAX_IMPORT_BYTES = 256 * 1024
DEFAULT_CAP_GB = 4
RESERVED = {"pack.json", "packages"}
RESERVED_SLUGS = {"import", "settings", "fetch", "os"}   # words in the /api/depot/packs/ addresses
TYPES = ("url", "zim", "package")
ITEM_KEYS = {"url": {"type", "url", "sha256", "file", "label"}, "zim": {"type", "name", "flavour", "label"},
             "package": {"type", "package", "label"}}
IMPORT_KEYS = {"bunkr_pack", "name", "note", "items"}

_lock = threading.RLock()        # read-modify-write of the settings
_fetching = threading.Lock()     # one fetch at a time


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- settings ----------
def settings(store):
    s = store.get_raw("packs_settings", {}) or {}
    gb = s.get("max_item_gb", DEFAULT_CAP_GB)
    return {"max_item_gb": gb if isinstance(gb, (int, float)) and not isinstance(gb, bool) and gb > 0 else DEFAULT_CAP_GB,
            "allow_lan": s.get("allow_lan") is True}


def save_settings(store, data):
    try:
        gb = float(data.get("max_item_gb", DEFAULT_CAP_GB))
    except (TypeError, ValueError):
        raise ValueError("size limit must be a number of GB")
    if not 0.01 <= gb <= 500:
        raise ValueError("size limit must be between 0.01 and 500 GB")
    store.set_raw("packs_settings", {"max_item_gb": round(gb, 2), "allow_lan": data.get("allow_lan") is True})
    return settings(store)


def cap_bytes(store):
    return int(settings(store)["max_item_gb"] * 1e9)


def getter(store):
    return safeget.Getter(allow_lan=settings(store)["allow_lan"])


# ---------- OS packages admins added ----------
def os_extra(store):
    return [n for n in (store.get_raw("os_packages_extra", []) or []) if isinstance(n, str) and packages.NAME_RE.fullmatch(n)]


def os_names(store):
    """What the weekly OS package fetch keeps: the built-in list, then what admins added."""
    return list(dict.fromkeys(packages.OPTIONAL + os_extra(store)))


def check_os(store, name, run=packages._run):
    """Can this package be added? Returns the size preview; raises ValueError."""
    name = packages.valid_name(name)
    if name in os_names(store):
        raise ValueError(f"{name} is already on the list")
    if len(os_extra(store)) >= MAX_OS_EXTRA:
        raise ValueError("that's a lot of packages; remove some first")
    if not packages.known([name], run):
        raise ValueError(f"apt doesn't know a package called {name} on this system")
    return {"name": name} | packages.preview(os_names(store), [name], run)


def add_os(store, name, run=packages._run):
    with _lock:
        info = check_os(store, name, run)
        store.set_raw("os_packages_extra", os_extra(store) + [info["name"]])
    return info


def remove_os(store, name):
    with _lock:
        if name not in os_extra(store):
            raise LookupError("that package isn't on the added list")
        store.set_raw("os_packages_extra", [n for n in os_extra(store) if n != name])


# ---------- the lists ----------
def all_packs(store):
    return [p for p in (store.get_raw("offline_packs", []) or []) if isinstance(p, dict) and p.get("slug")]


def get(store, slug):
    p = next((p for p in all_packs(store) if p["slug"] == slug), None)
    if not p:
        raise LookupError("no such pack")
    return p


def _save_all(store, packs):
    store.set_raw("offline_packs", packs)


def clean_name(name, what="pack name", limit=60):
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError(f"enter a {what}")
    if len(name) > limit:
        raise ValueError(f"keep the {what} under {limit} characters")
    if any(ord(c) < 32 for c in name):
        raise ValueError(f"the {what} has odd characters")
    return name


def slugify(name, taken=()):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "pack"
    slug, n = base, 2
    while slug in taken or slug in RESERVED_SLUGS:
        slug = f"{base[:36]}-{n}"
        n += 1
    return slug


def safe_file(name, fallback="download"):
    """A plain file name: letters, digits, . _ - only, no leading dot, nothing BUNKR uses itself."""
    name = urllib.parse.unquote(str(name or "")).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")[:120].rstrip("._-")
    if not name or name.lower() in RESERVED:
        name = fallback
    if name.endswith((".part", ".tmp")):
        name += ".bin"
    return name


def item_key(item):
    if item["type"] == "url":
        return "url|" + item["url"]
    if item["type"] == "zim":
        return f"zim|{item['name']}|{item['flavour']}"
    return "package|" + item["package"]


def clean_item(raw, allow_lan=False):
    """One item from the form or an import, checked. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("each item must be an object")
    kind = raw.get("type")
    if kind not in TYPES:
        raise ValueError("item type must be url, zim or package")
    extra = set(raw) - ITEM_KEYS[kind]
    if extra:
        raise ValueError(f"unexpected field {sorted(extra)[0]!r}")
    for k, v in raw.items():
        if not isinstance(v, str):
            raise ValueError(f"{k} must be text")
    label = " ".join(str(raw.get("label") or "").split())[:120]
    if kind == "url":
        url = raw.get("url", "").strip()
        u, _host, _port = safeget.check_url(url, allow_lan)
        sha = (raw.get("sha256") or "").strip().lower()
        if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("a SHA-256 is 64 letters and digits (0-9, a-f)")
        file = safe_file(raw.get("file") or u.path)
        item = {"type": "url", "url": url, "sha256": sha, "file": file, "label": label}
    elif kind == "zim":
        name, flavour = (raw.get("name") or "").strip(), (raw.get("flavour") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{2,120}", name) or not re.fullmatch(r"[a-z_-]{0,20}", flavour):
            raise ValueError("that isn't a Kiwix book name")
        item = {"type": "zim", "name": name, "flavour": flavour, "label": label}
    else:
        item = {"type": "package", "package": packages.valid_name(raw.get("package")), "label": label}
    item["id"] = "i" + hashlib.sha256(item_key(item).encode()).hexdigest()[:10]
    return item


def _fit(pack, item):
    """Check an item against the rest of its pack: no duplicates, no two files with one name."""
    if any(i["id"] == item["id"] for i in pack["items"]):
        raise ValueError("that's already in the pack")
    if len(pack["items"]) >= MAX_ITEMS:
        raise ValueError(f"a pack holds up to {MAX_ITEMS} items")
    if item["type"] == "url":
        names = {i["file"] for i in pack["items"] if i["type"] == "url"}
        if item["file"] in names:
            item["file"] = f"{item['id']}-{item['file']}"[:130]
    return item


def create(store, name, note=""):
    with _lock:
        packs = all_packs(store)
        if len(packs) >= MAX_PACKS:
            raise ValueError("that's a lot of packs; remove some first")
        name = clean_name(name)
        if any(p["name"].lower() == name.lower() for p in packs):
            raise ValueError("there's already a pack with that name")
        pack = {"slug": slugify(name, {p["slug"] for p in packs}), "name": name,
                "note": " ".join(str(note or "").split())[:300], "items": [], "created_at": _now()}
        _save_all(store, packs + [pack])
        return pack


def add_item(store, slug, raw):
    with _lock:
        packs = all_packs(store)
        pack = next((p for p in packs if p["slug"] == slug), None)
        if not pack:
            raise LookupError("no such pack")
        item = _fit(pack, clean_item(raw, settings(store)["allow_lan"]))
        pack["items"].append(item)
        _save_all(store, packs)
        return item


def _forget_files(depot, slug, item_id=None):
    """Remove the store files (and records) of a pack, or of one of its items."""
    for row in depot.rows():
        s, _, i = row["item_id"].partition(":")
        if row["kind"] == "pack" and s == slug and (item_id is None or i == item_id):
            depot.remove(row)


def remove_item(store, depot, slug, item_id):
    with _lock:
        packs = all_packs(store)
        pack = next((p for p in packs if p["slug"] == slug), None)
        if not pack or not any(i["id"] == item_id for i in pack["items"]):
            raise LookupError("no such item")
        gone = next(i for i in pack["items"] if i["id"] == item_id)
        pack["items"] = [i for i in pack["items"] if i["id"] != item_id]
        _save_all(store, packs)
        _set_state(store, slug, item_id, None)
    if gone["type"] != "package":
        _forget_files(depot, slug, item_id)
    elif not any(i["type"] == "package" for i in pack["items"]):
        _forget_files(depot, slug, "packages")
        _rmtree_inside(depot, Path("packs") / slug / "packages")


def _rmtree_inside(depot, rel):
    top = (depot.root / "packs").resolve()
    target = (depot.root / rel).resolve()
    if target != top and target.is_relative_to(top) and target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)


def delete(store, depot, slug):
    with _lock:
        packs = all_packs(store)
        if not any(p["slug"] == slug for p in packs):
            raise LookupError("no such pack")
        _save_all(store, [p for p in packs if p["slug"] != slug])
        state = store.get_raw("packs_state", {}) or {}
        state.pop(slug, None)
        store.set_raw("packs_state", state)
    _forget_files(depot, slug)
    _rmtree_inside(depot, Path("packs") / slug)
    _rmtree_inside(depot, Path("packs") / ".staging" / slug)


# ---------- import and export ----------
def export(pack):
    """The pack as a small JSON-ready dict to share. No ids, no state, nothing about this hub."""
    out = {"bunkr_pack": FORMAT, "name": pack["name"], "items": []}
    if pack.get("note"):
        out["note"] = pack["note"]
    for i in pack["items"]:
        out["items"].append({k: v for k, v in i.items() if k in ITEM_KEYS[i["type"]] and v != ""})
    return out


def parse_import(data, allow_lan=False):
    """A shared pack file, checked strictly. Accepts the JSON text or the decoded object.
    Returns {"name", "note", "items"} with every item cleaned; raises ValueError naming the problem."""
    if isinstance(data, (bytes, str)):
        if len(data) > MAX_IMPORT_BYTES:
            raise ValueError("that file is too big to be a pack list")
        try:
            data = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("that file isn't a BUNKR pack (not JSON)")
    elif len(json.dumps(data)) > MAX_IMPORT_BYTES:
        raise ValueError("that file is too big to be a pack list")
    if not isinstance(data, dict) or "bunkr_pack" not in data:
        raise ValueError("that file isn't a BUNKR pack")
    if data["bunkr_pack"] != FORMAT or isinstance(data["bunkr_pack"], bool):
        raise ValueError("that pack file is from a newer BUNKR")
    extra = set(data) - IMPORT_KEYS
    if extra:
        raise ValueError(f"unexpected field {sorted(extra)[0]!r} in the pack file")
    if not isinstance(data.get("name"), str) or not isinstance(data.get("note", ""), str):
        raise ValueError("the pack needs a name")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("the pack has no items")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"a pack holds up to {MAX_ITEMS} items")
    pack = {"name": clean_name(data["name"]), "note": " ".join(data.get("note", "").split())[:300], "items": []}
    for n, raw in enumerate(items, 1):
        try:
            pack["items"].append(_fit(pack, clean_item(raw, allow_lan)))
        except ValueError as exc:
            raise ValueError(f"item {n}: {exc}")
    return pack


def import_pack(store, data):
    parsed = parse_import(data, settings(store)["allow_lan"])
    with _lock:
        packs = all_packs(store)
        if len(packs) >= MAX_PACKS:
            raise ValueError("that's a lot of packs; remove some first")
        name, n = parsed["name"], 2
        while any(p["name"].lower() == name.lower() for p in packs):
            name = f"{parsed['name'][:54]} ({n})"
            n += 1
        pack = {"slug": slugify(name, {p["slug"] for p in packs}), "name": name, "note": parsed["note"],
                "items": parsed["items"], "created_at": _now()}
        _save_all(store, packs + [pack])
        return pack


# ---------- state ----------
def _state(store):
    s = store.get_raw("packs_state", {}) or {}
    return s if isinstance(s, dict) else {}


def _set_state(store, slug, item_id, value):
    with _lock:
        s = _state(store)
        if value is None:
            s.get(slug, {}).pop(item_id, None)
        else:
            s.setdefault(slug, {})[item_id] = value
        store.set_raw("packs_state", s)


def view(store, depot):
    """Every pack with each item's last result and the size it takes in the store."""
    state, rows = _state(store), [r for r in depot.rows() if r["kind"] == "pack"]
    out = []
    for p in all_packs(store):
        mine = [r for r in rows if r["item_id"].split(":", 1)[0] == p["slug"]]
        items = []
        for i in p["items"]:
            st = state.get(p["slug"], {}).get(i["id"]) or {"status": "waiting"}
            key = f"{p['slug']}:packages" if i["type"] == "package" else f"{p['slug']}:{i['id']}"
            held = [r for r in mine if r["item_id"] == key]
            items.append(i | {"state": {k: st.get(k) for k in ("status", "error", "size", "version", "checked_at",
                                                                "fetched_at", "sha256")},
                              "held": bool(held) and all(r["status"] == "ok" for r in held)})
        out.append({k: p.get(k, "") for k in ("slug", "name", "note", "created_at")} |
                   {"items": items, "bytes": sum(r["size"] for r in mine), "files": len(mine)})
    return out


# ---------- fetching ----------
def _version(meta):
    try:
        return parsedate_to_datetime(meta.get("last_modified") or "").date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    tag = (meta.get("etag") or "").strip('W/"')[:24]
    return tag or datetime.now(timezone.utc).date().isoformat()


def _inside(depot, rel):
    top = (depot.root / "packs").resolve()
    p = (depot.root / rel).resolve()
    if not p.is_relative_to(top) or p == top:
        raise ValueError("that path leaves the packs folder")
    return depot.root / rel


def _stage(depot, slug, file):
    d = depot.root / "packs" / ".staging" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d / (file + ".part")


def _swap(depot, stage, rel, slug, item, version, size, sha, url):
    """The verified download replaces the old copy, then the store record follows."""
    dest = _inside(depot, rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(stage, 0o644)
    os.replace(stage, dest)
    item_id = f"{slug}:{item['id']}"
    depot.record("pack", item_id, rel, version, size, sha, url)
    depot.prune_older("pack", item_id, rel)


def _own_rel(depot, slug, item, file):
    """packs/<slug>/<file>, or <item id>-<file> when another item of the pack already holds that name (a web file
    and a Kiwix book can both be called x.zim), so two items never overwrite each other."""
    rel = f"packs/{slug}/{file}"
    held = depot.by_path(rel)
    if held and held["item_id"] != f"{slug}:{item['id']}":
        rel = f"packs/{slug}/{item['id']}-{file}"[:200]
    return rel


def _fetch_url(depot, g, slug, item, old, cap, progress, cancelled):
    rel = _own_rel(depot, slug, item, item["file"])
    dest = _inside(depot, rel)
    held = depot.by_path(rel)
    have = bool(held and dest.is_file() and held["status"] == "ok")
    if have and item["sha256"] and held["sha256"] == item["sha256"]:
        return {"changed": False, "version": held["version"], "size": held["size"], "sha256": held["sha256"]}
    headers = {}
    if have and not item["sha256"]:
        if old.get("etag"):
            headers["If-None-Match"] = old["etag"]
        if old.get("last_modified"):
            headers["If-Modified-Since"] = old["last_modified"]
    stage = _stage(depot, slug, rel.rsplit("/", 1)[1])
    res = g.download(item["url"], stage, cap, item["sha256"] or None, headers, progress, cancelled)
    meta = {"etag": res.get("etag", ""), "last_modified": res.get("last_modified", "")}
    if res["status"] == 304 or (have and res.get("sha256") == held["sha256"]):
        stage.unlink(missing_ok=True)
        return {"changed": False, "version": held["version"], "size": held["size"], "sha256": held["sha256"]} | meta
    version = _version(res)
    _swap(depot, stage, rel, slug, item, version, res["size"], res["sha256"], item["url"])
    return {"changed": True, "version": version, "size": res["size"], "sha256": res["sha256"]} | meta


def _fetch_zim(depot, g, slug, item, cap, progress, cancelled, lookup):
    found = lookup({"id": f"pack:{item['name']}:{item['flavour']}", "name": item["name"], "flavour": item["flavour"]})
    if not found or not found.get("url"):
        raise IOError("not in the Kiwix catalog right now")
    file = safe_file(found.get("file") or found["url"], fallback=item["name"])
    if not file.endswith(".zim"):
        file += ".zim"
    rel = _own_rel(depot, slug, item, file)
    dest = _inside(depot, rel)
    held = depot.by_path(rel)
    if held and dest.is_file() and held["status"] == "ok" and held["version"] == found.get("date", ""):
        return {"changed": False, "version": held["version"], "size": held["size"], "sha256": held["sha256"]}
    if int(found.get("size") or 0) > cap:
        raise safeget.TooBig(f"it's {found['size'] / 1e9:.2f} GB, over the {cap / 1e9:.2f} GB limit")
    sha = None
    if found.get("meta4"):
        _size, sha, _mirrors = library.parse_meta4(g.get_small(found["meta4"]))
    stage = _stage(depot, slug, rel.rsplit("/", 1)[1])
    res = g.download(found["url"], stage, cap, sha, None, progress, cancelled)
    version = found.get("date", "") or _version(res)
    _swap(depot, stage, rel, slug, item, version, res["size"], res["sha256"], found["url"])
    return {"changed": True, "version": version, "size": res["size"], "sha256": res["sha256"]}


def _fetch_packages(depot, slug, items, run):
    sysid = packages.system_id(run)
    if not sysid:
        raise RuntimeError("can't tell which system this is")
    names = [i["package"] for i in items]
    ok = packages.known(names, run)
    if ok:
        folder = _inside(depot, f"packs/{slug}/packages/{sysid}")
        keep, _added = packages.download_into(folder, ok, run)
        recorded = set()
        for f in sorted(folder.iterdir()):
            if f.is_file():
                rel = f"packs/{slug}/packages/{sysid}/{f.name}"
                depot.record("pack", f"{slug}:packages", rel, f.name, f.stat().st_size, manifest.sha256_file(f), "")
                recorded.add(rel)
        for row in depot.rows():
            if row["kind"] == "pack" and row["item_id"] == f"{slug}:packages" and row["path"] not in recorded:
                depot.remove(row)
    else:
        keep = {}
    out = {}
    for i in items:
        deb = keep.get(i["package"])
        out[i["id"]] = ({"status": "ok", "version": deb.name.split("_")[1] if deb.name.count("_") >= 2 else deb.name,
                         "size": deb.stat().st_size} if deb else
                        {"status": "error", "error": f"apt doesn't know {i['package']} on {sysid}"})
    return out


def _write_manifest(depot, pack):
    """packs/<slug>/pack.json: the pack's list, so a node (or a person with the drive) knows what's in the folder."""
    rel = f"packs/{pack['slug']}/pack.json"
    dest = _inside(depot, rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(export(pack), indent=1, sort_keys=True) + "\n").encode()
    held = depot.by_path(rel)
    if held and dest.is_file() and held["sha256"] == hashlib.sha256(data).hexdigest():
        return
    tmp = _stage(depot, pack["slug"], "pack.json")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)
    depot.record("pack", f"{pack['slug']}:pack.json", rel, _now()[:10], len(data), hashlib.sha256(data).hexdigest())


def refresh(store, depot, log=print, progress=None, cancelled=lambda: False, only=None, g=None,
            run=packages._run, lookup=library.lookup):
    """Fetch every pack (or only the one with slug `only`). Returns {"ok", "failed", "changed"} counts.
    One failing item doesn't stop the others; its old copy stays."""
    if not _fetching.acquire(blocking=False):
        raise RuntimeError("packs are already being fetched")
    try:
        g = g or getter(store)
        cap = cap_bytes(store)
        counts = {"ok": 0, "failed": 0, "changed": 0}
        if not only:   # files of packs that were deleted while a fetch ran
            slugs = {p["slug"] for p in all_packs(store)}
            for row in depot.rows():
                if row["kind"] == "pack" and row["item_id"].split(":", 1)[0] not in slugs:
                    depot.remove(row)
        for pack in all_packs(store):
            if only and pack["slug"] != only:
                continue
            slug = pack["slug"]
            _write_manifest(depot, pack)
            wanted = {f"{slug}:{i['id']}" for i in pack["items"]} | {f"{slug}:pack.json", f"{slug}:packages"}
            for row in depot.rows():   # files of items removed from the list
                if row["kind"] == "pack" and row["item_id"].split(":", 1)[0] == slug and row["item_id"] not in wanted:
                    depot.remove(row)
            debs = [i for i in pack["items"] if i["type"] == "package"]
            for item in pack["items"]:
                if item["type"] == "package":
                    continue
                if cancelled():
                    raise InterruptedError("cancelled")
                if not any(p["slug"] == slug and any(i["id"] == item["id"] for i in p["items"]) for p in all_packs(store)):
                    continue   # removed while this fetch ran
                old = _state(store).get(slug, {}).get(item["id"]) or {}
                what = item.get("label") or item.get("file") or item.get("name")
                log(f"{pack['name']}: {what}")
                try:
                    if item["type"] == "url":
                        r = _fetch_url(depot, g, slug, item, old, cap, progress, cancelled)
                    else:
                        r = _fetch_zim(depot, g, slug, item, cap, progress, cancelled, lookup)
                except InterruptedError:
                    raise
                except Exception as exc:
                    counts["failed"] += 1
                    msg = str(exc)[:200] or type(exc).__name__
                    log(f"  failed: {msg} (the old copy, if any, is kept)")
                    _set_state(store, slug, item["id"], {k: old.get(k) for k in ("size", "version", "fetched_at", "sha256",
                                                                                "etag", "last_modified")} |
                               {"status": "error", "error": msg, "checked_at": _now()})
                    continue
                counts["ok"] += 1
                counts["changed"] += r["changed"]
                log(f"  {'new copy stored, checked' if r['changed'] else 'no change'} ({r['size'] / 1e6:.1f} MB)")
                _set_state(store, slug, item["id"], {"status": "ok", "error": "", "checked_at": _now(),
                                                     "fetched_at": _now() if r["changed"] else old.get("fetched_at"),
                                                     **{k: r.get(k, old.get(k)) for k in ("size", "version", "sha256",
                                                                                        "etag", "last_modified")}})
            if debs:
                log(f"{pack['name']}: {len(debs)} OS package(s)")
                try:
                    res = _fetch_packages(depot, slug, debs, run)
                except Exception as exc:
                    msg = str(exc)[:200]
                    log(f"  failed: {msg}")
                    res = {i["id"]: {"status": "error", "error": msg} for i in debs}
                for i in debs:
                    old = _state(store).get(slug, {}).get(i["id"]) or {}
                    st = res[i["id"]]
                    counts["ok" if st["status"] == "ok" else "failed"] += 1
                    _set_state(store, slug, i["id"], old | st | {"checked_at": _now()} |
                               ({"error": ""} if st["status"] == "ok" else {}))
        _rmtree_inside(depot, Path("packs") / ".staging")
        return counts
    finally:
        _fetching.release()
