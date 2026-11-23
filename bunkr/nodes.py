"""Nodes: the hub, Pi field nodes, the storage laptop and USB kit drives.

Each node has a role, a storage budget and a profile of what it carries.
The space planner fills the budget from the downloads store in priority order
(documents, readers, survival libraries, maps and radio files, general knowledge),
and the node's manifest tells the hub what is current, behind or damaged.
"""

import json
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone

ROLES = {"hub": "Home hub", "pi": "Pi field node", "laptop": "Storage laptop", "drive": "USB kit drive"}

# "packs": offline packs (packs.py): True for all of them, False for none, or a list of pack slugs
DEFAULT_PROFILES = {
    "hub": {"documents": True, "readers": True, "tiers": [1, 2, 3], "maps": True, "firmware": True, "radio": True, "apps": True, "plan": True, "packs": True},
    "pi": {"documents": True, "readers": False, "tiers": [1, 2, 3], "maps": True, "firmware": True, "radio": True, "apps": True, "plan": True, "packs": True},
    "laptop": {"documents": True, "readers": True, "tiers": [1, 2, 3], "maps": True, "firmware": True, "radio": True, "apps": True, "plan": True, "packs": True},
    # drives carry the family plan inside the encrypted document archive instead of as a plain file
    "drive": {"documents": True, "readers": True, "tiers": [1, 2, 3], "maps": True, "firmware": False, "radio": True, "apps": False, "plan": False, "packs": True},
}

# Reminder kinds per role: (label, every N days)
REMINDERS = {
    "laptop": {"battery": ("Charge the battery to about 60% and run updates", 90)},
    "drive": {"verify": ("Plug in and verify checksums", 180)},
    "pi": {"power_test": ("Boot on battery power and check the hotspot", 90)},
}

ARCHIVE_STALE_DAYS = 30


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts):
    try:
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def profile_for(node):
    prof = dict(DEFAULT_PROFILES.get(node["role"], DEFAULT_PROFILES["drive"]))
    try:
        prof.update(json.loads(node.get("profile") or "{}"))
    except ValueError:
        pass
    sel = prof.get("packs", True)
    if not isinstance(sel, bool):
        prof["packs"] = [s for s in sel if isinstance(s, str)][:100] if isinstance(sel, list) else True
    return prof


def carries_pack(prof, row):
    """Does this profile take this offline-pack file? (item ids are "<pack slug>:<item>")"""
    sel = prof.get("packs", True)
    return sel is True or (isinstance(sel, list) and row["item_id"].split(":", 1)[0] in sel)


# storage
def list_nodes(store):
    return store._rows("SELECT * FROM nodes ORDER BY CASE role WHEN 'hub' THEN 0 WHEN 'pi' THEN 1 "
                       "WHEN 'laptop' THEN 2 ELSE 3 END, name")


def get(store, node_id=None, uid=None):
    rows = store._rows("SELECT * FROM nodes WHERE id=? OR uid=?", (node_id or -1, uid or ""))
    return rows[0] if rows else None


def save(store, data, node_id=None):
    fields = {k: data[k] for k in ("name", "role", "budget_bytes", "notes") if k in data}
    if "profile" in data:
        fields["profile"] = json.dumps(data["profile"])
    if node_id is None:
        fields.update(uid=data.get("uid") or uuid.uuid4().hex, created_at=_now())
    return store._save("nodes", tuple(fields), fields, node_id)


def delete(store, node_id):
    store._exec("DELETE FROM nodes WHERE id=?", (node_id,))


def set_fields(store, node_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    store._exec(f"UPDATE nodes SET {sets} WHERE id=?", (*fields.values(), node_id))


def ensure_drive_node(store, uid, name):
    """Kit drives become nodes the first time a kit is built on them."""
    node = get(store, uid=uid)
    if node:
        return node
    save(store, {"uid": uid, "name": name, "role": "drive"})
    return get(store, uid=uid)


def new_pair_code(store, node_id, minutes=15):
    """One-time code a node uses to pair; only its hash is stored."""
    import hashlib
    code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
    expires = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    set_fields(store, node_id, pair_code_hash=hashlib.sha256(code.encode()).hexdigest(), pair_expires=expires)
    return code, expires


# planning
def plan(node, depot_rows, catalog_meta, budget):
    """Choose store files for a node within budget, most important first.

    depot_rows: store records (status ok). catalog_meta: {library id: {tier, group}}.
    Returns {"files": [...rows], "skipped": [...], "used": bytes, "budget": bytes}.
    """
    prof = profile_for(node)
    rows = [r for r in depot_rows if r["status"] == "ok"]
    chosen, skipped, used = [], [], 0

    def take(r, why=""):
        nonlocal used
        if budget and used + r["size"] > budget:
            skipped.append({**r, "reason": "not enough space" + why})
            return False
        chosen.append(r)
        used += r["size"]
        return True

    if prof.get("documents"):
        docs = [r for r in rows if r["kind"] == "documents"]
        newest = max((r["version"] for r in docs), default=None)
        for r in sorted((r for r in docs if r["version"] == newest), key=lambda r: r["path"]):
            take(r)
    if prof.get("readers"):
        for r in (r for r in rows if r["kind"] == "tool"):
            take(r)
    if prof.get("packages", True):   # OS packages for installing with no internet (small); see packages.py
        for r in (r for r in rows if r["kind"] == "package"):
            take(r)
    newest_lib = {}
    for r in rows:
        if r["kind"] == "library" and r["version"] > newest_lib.get(r["item_id"], ""):
            newest_lib[r["item_id"]] = r["version"]
    libs = [r for r in rows if r["kind"] == "library" and r["version"] == newest_lib.get(r["item_id"], "") and  # not a kept older one
            catalog_meta.get(r["item_id"], {}).get("tier", 9) in prof.get("tiers", [])]
    groups_done = set()
    order = {"plan": 0, "web": 1, "map": 1, "firmware": 2, "radio": 2, "app": 3, "pack": 3}
    key = {"map": "maps", "web": "maps", "app": "apps"}
    extras = [r for r in rows if r["kind"] in order and
              (carries_pack(prof, r) if r["kind"] == "pack" else prof.get(key.get(r["kind"], r["kind"])))]
    tiered = sorted(libs, key=lambda r: (catalog_meta.get(r["item_id"], {}).get("tier", 9),
                                         catalog_meta.get(r["item_id"], {}).get("rank", 0)))
    # tier 1 libraries, then maps/radio/firmware, then the rest (general knowledge last)
    for r in [x for x in tiered if catalog_meta.get(x["item_id"], {}).get("tier") == 1] + \
             sorted(extras, key=lambda r: order[r["kind"]]) + \
             [x for x in tiered if catalog_meta.get(x["item_id"], {}).get("tier") != 1]:
        group = catalog_meta.get(r["item_id"], {}).get("group")
        if group and group in groups_done:
            continue
        if take(r, " (trying a smaller version)" if group else "") and group:
            groups_done.add(group)
    return {"files": chosen, "skipped": skipped, "used": used, "budget": budget}


def plan_files(p):
    return [{"path": r["path"], "size": r["size"], "sha256": r["sha256"], "kind": r["kind"],
             "item": r["item_id"], "version": r["version"]} for r in p["files"]]


# status
def reminders_due(node, today=None):
    today = today or date.today()
    done = json.loads(node.get("reminders") or "{}")
    out = []
    for kind, (label, every) in REMINDERS.get(node["role"], {}).items():
        last = done.get(kind) or (node.get("created_at") or "")[:10]
        try:
            last_d = date.fromisoformat(last)
        except ValueError:
            last_d = today
        due = last_d + timedelta(days=every)
        out.append({"kind": kind, "label": label, "every_days": every, "last": last,
                    "due": due.isoformat(), "overdue": due <= today})
    return out


def status(node, planned, documents_changed_at, majors_pending, today=None):
    """Dashboard row: sync freshness, files behind, archive age, reminders."""
    reported = json.loads(node["manifest"]) if node.get("manifest") else None
    have = (reported or {}).get("files", [])
    cmp = None
    if reported is not None:
        from . import manifest
        cmp = manifest.compare(planned, have)
    behind = (len(cmp["missing"]) + len(cmp["changed"])) if cmp else None
    behind_bytes = sum(f["size"] for f in planned if cmp and (f["path"] in cmp["missing"] or f["path"] in cmp["changed"]))
    archives = sorted((f for f in have if f.get("kind") == "documents" or f["path"].startswith(("documents/", "BUNKR/documents/"))),
                      key=lambda f: f.get("version") or f["path"])
    newest_archive = archives[-1] if archives else None
    archive = None
    if newest_archive:
        made = _archive_date(newest_archive)
        changed = _parse(documents_changed_at)
        age = (datetime.now(timezone.utc) - made).days if made else None
        archive = {"path": newest_archive["path"], "made": made.isoformat(timespec="minutes") if made else "",
                   "age_days": age, "stale": bool(made and changed and changed > made) or bool(age and age > 365)}
    last = _parse(node.get("last_sync") or node.get("last_seen"))
    return {"id": node["id"], "files_planned": len(planned), "behind": behind, "behind_bytes": behind_bytes,
            "extra": len(cmp["extra"]) if cmp else 0, "reported": reported is not None,
            "last_sync_days": (datetime.now(timezone.utc) - last).days if last else None,
            "archive": archive, "majors_pending": majors_pending, "reminders": reminders_due(node, today)}


def _archive_date(f):
    """documents-YYYY-MM-DD_HHMM.7z[.001] -> datetime (UTC assumed)."""
    import re
    m = re.search(r"documents-(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})", f["path"])
    if m:
        return datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}:{m.group(3)}:00+00:00")
    return _parse(f.get("version"))


def drive_report(drive_manifest, catalog, depot_rows, documents_changed_at):
    """What on a kit drive is out of date compared with the store and the catalog."""
    latest = {(c["kind"], c["id"]): c for c in catalog}
    held = {}
    for r in depot_rows:
        if r["status"] == "ok":
            key = (r["kind"], r["item_id"])
            if key not in held or r["version"] > held[key]["version"]:
                held[key] = r
    out_of_date, current = [], 0
    for f in drive_manifest.get("files", []):
        kind, item = f.get("kind"), f.get("item")
        if not kind or not item:
            continue
        if kind == "documents":
            made = _archive_date(f)
            changed = _parse(documents_changed_at)
            if made and changed and changed > made:
                out_of_date.append({**f, "reason": "documents changed since this archive was made",
                                    "latest": "rebuild with your passphrase"})
            else:
                current += 1
            continue
        h = held.get((kind, item))
        c = latest.get((kind, item))
        newest = max(filter(None, [(h or {}).get("version"), (c or {}).get("latest")]), default="")
        if kind == "tool":
            if h and h["sha256"] != f.get("sha256") and h["version"] > (f.get("version") or ""):
                out_of_date.append({**f, "reason": "newer version in the store", "latest": h["version"]})
            else:
                current += 1
        elif newest and newest > (f.get("version") or ""):
            out_of_date.append({**f, "reason": "newer version available", "latest": newest,
                                "in_store": bool(h and h["version"] == newest)})
        else:
            current += 1
    return {"out_of_date": out_of_date, "current": current, "node": drive_manifest.get("node", {}),
            "generated": drive_manifest.get("generated", "")}
