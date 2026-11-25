"""LAN sync between the hub and its nodes (Pi, laptop).

Files: the node asks the hub for its plan (paths + SHA-256 from the downloads
store), downloads only what it lacks over HTTP with resume, verifies every
checksum, removes files the plan dropped, and reports its manifest back.

Records: inventory items and locations travel both ways. Each record has a uid
and an updated_at time; the newer change wins. When both sides changed the same
record since the node last synced, the newer one is kept and the other is saved
in sync_conflicts for review on the hub. Deletions travel as tombstones.

Authentication: a per-node bearer token from pairing; only its hash is on the hub.
"""

import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import brief, checkins, comms, forecasts, manifest, situation

USER_AGENT = "BUNKR-node/0.2"
# Serial numbers are not synced: they stay on the hub (and in the encrypted archive).
ITEM_SYNC_FIELDS = ("name", "category", "quantity", "unit", "pack_size", "pack_unit", "per_unit", "size_unit",
                    "essential", "expires", "notes", "subtype", "caliber", "ammo_type", "capacity", "mag_status", "fits")
LOC_SYNC_FIELDS = ("name", "label", "kind", "person", "people", "counts_home", "notes")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- hub side ----------
def pair(store, code, name=""):
    """Exchange a one-time pairing code for a node token."""
    code_h = hashlib.sha256((code or "").strip().upper().encode()).hexdigest()
    rows = store._rows("SELECT * FROM nodes WHERE pair_code_hash=? AND pair_code_hash!=''", (code_h,))
    if not rows or rows[0]["pair_expires"] < now():
        return None
    node = rows[0]
    token = secrets.token_urlsafe(32)
    store._exec("UPDATE nodes SET token_hash=?, pair_code_hash='', pair_expires='', last_seen=?, lost='' WHERE id=?",
                (token_hash(token), now(), node["id"]))
    return {"token": token, "uid": node["uid"], "name": node["name"], "role": node["role"]}


def node_for_token(store, token):
    if not token:
        return None
    rows = store._rows("SELECT * FROM nodes WHERE token_hash=? AND token_hash!=''", (token_hash(token),))
    return rows[0] if rows else None


def _location_uid_map(store):
    return {l["id"]: l["uid"] for l in store._rows("SELECT id, uid FROM locations")}


def export_records(store, since=""):
    """Locations, items and tombstones changed after `since` (ISO time)."""
    locs = store._rows("SELECT * FROM locations WHERE updated_at > ? OR received_at > ?", (since, since))
    uid_of = _location_uid_map(store)
    items = store._rows("SELECT * FROM items WHERE updated_at > ? OR received_at > ?", (since, since))
    return {
        "locations": [{"uid": l["uid"], "updated_at": l["updated_at"], "parent_uid": uid_of.get(l["parent_id"], ""),
                       **{f: l[f] for f in LOC_SYNC_FIELDS}} for l in locs],
        "items": [{"uid": i["uid"], "updated_at": i["updated_at"], "location_uid": uid_of.get(i["location_id"], ""),
                   **{f: i[f] for f in ITEM_SYNC_FIELDS}} for i in items],
        "tombstones": store._rows("SELECT kind, uid, deleted_at FROM tombstones WHERE deleted_at > ? OR received_at > ?",
                                  (since, since)),
    }


def _log_conflict(store, kind, uid, node_name, kept, lost):
    store._exec("INSERT INTO sync_conflicts(kind, uid, node, kept, lost, at) VALUES(?,?,?,?,?,?)",
                (kind, uid, node_name, json.dumps(kept), json.dumps(lost), now()))


def apply_records(store, incoming, since, source="node"):
    """Merge records from the other side. `since` = when the two sides last agreed:
    a local record changed after it, and also changed remotely, is a conflict."""
    applied, conflicts = 0, 0
    got = now()
    with store._lock:
        conn = store._conn
        for kind, table, fields in (("location", "locations", LOC_SYNC_FIELDS), ("item", "items", ITEM_SYNC_FIELDS)):
            recs = incoming.get("locations" if kind == "location" else "items", [])
            if kind == "location":  # parents before the sub-locations inside them
                recs = sorted(recs, key=lambda r: (r.get("name") or "").count(">"))
            for rec in recs:
                uid, stamp = rec.get("uid"), rec.get("updated_at") or ""
                if not uid:
                    continue
                values = {f: rec.get(f) for f in fields if f in rec}  # older nodes omit newer fields
                if kind == "location":
                    values.setdefault("label", values.get("name"))  # older nodes: flat locations
                    if "parent_uid" in rec:  # older nodes don't send one: leave the nesting as it is
                        parent = conn.execute("SELECT id FROM locations WHERE uid=?", (rec["parent_uid"] or "",)).fetchone()
                        values["parent_id"] = parent["id"] if parent else None
                if kind == "item":
                    loc = conn.execute("SELECT id FROM locations WHERE uid=?", (rec.get("location_uid") or "",)).fetchone()
                    values["location_id"] = loc["id"] if loc else None
                local = conn.execute(f"SELECT * FROM {table} WHERE uid=?", (uid,)).fetchone()
                if local is None:
                    tomb = conn.execute("SELECT deleted_at FROM tombstones WHERE kind=? AND uid=?", (kind, uid)).fetchone()
                    if tomb and tomb["deleted_at"] >= stamp:
                        continue  # deleted here after that edit
                    if kind == "location":  # a same-named location made on both sides: adopt the incoming uid
                        dup = conn.execute("SELECT id FROM locations WHERE name=?", (values["name"],)).fetchone()
                        if dup:
                            conn.execute("UPDATE locations SET uid=?, received_at=? WHERE id=?", (uid, got, dup["id"]))
                            local = conn.execute("SELECT * FROM locations WHERE id=?", (dup["id"],)).fetchone()
                    if local is None:
                        cols = ["uid", "updated_at", "received_at", *values]
                        conn.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
                                     (uid, stamp, got, *values.values()))
                        applied += 1
                        continue
                local = dict(local)
                if local["updated_at"] == stamp:
                    continue
                both_changed = local["updated_at"] > since and stamp > since
                if stamp > local["updated_at"]:
                    if both_changed:
                        _log_conflict(store, kind, uid, source, rec, {f: local.get(f) for f in fields})
                        conflicts += 1
                    sets = ", ".join(f"{c}=?" for c in values)
                    conn.execute(f"UPDATE {table} SET {sets}, updated_at=?, received_at=? WHERE uid=?",
                                 (*values.values(), stamp, got, uid))
                    applied += 1
                elif both_changed:
                    _log_conflict(store, kind, uid, source, {f: local.get(f) for f in fields}, rec)
                    conflicts += 1
        for t in incoming.get("tombstones", []):
            table = {"item": "items", "location": "locations"}.get(t.get("kind"))
            if not table:
                continue
            local = conn.execute(f"SELECT * FROM {table} WHERE uid=?", (t["uid"],)).fetchone()
            if local is not None:
                if local["updated_at"] > t["deleted_at"]:
                    _log_conflict(store, t["kind"], t["uid"], source, dict(local), {"deleted_at": t["deleted_at"]})
                    conflicts += 1
                    continue  # edited after the other side deleted it: keep the edit
                if table == "locations":
                    conn.execute("UPDATE items SET location_id=NULL WHERE location_id=?", (local["id"],))
                conn.execute(f"DELETE FROM {table} WHERE uid=?", (t["uid"],))
                applied += 1
            conn.execute("INSERT INTO tombstones(kind, uid, deleted_at, received_at) VALUES(?,?,?,?) ON CONFLICT(kind, uid) "
                         "DO UPDATE SET received_at=CASE WHEN excluded.deleted_at > deleted_at THEN excluded.received_at "
                         "ELSE received_at END, deleted_at=MAX(deleted_at, excluded.deleted_at)",
                         (t["kind"], t["uid"], t["deleted_at"], got))
        conn.commit()
    return {"applied": applied, "conflicts": conflicts}


def serve_file(handler, path, start_end=None):
    """Stream a file with HTTP Range support (resume)."""
    size = path.stat().st_size
    rng = handler.headers.get("Range", "")
    start, end, status = 0, size - 1, 200
    if rng.startswith("bytes="):
        a, _, b = rng[6:].partition("-")
        try:
            start = int(a) if a else max(0, size - int(b))
            end = min(size - 1, int(b)) if a and b else size - 1
            status = 206
        except ValueError:
            start, end, status = 0, size - 1, 200
        if start >= size:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
    length = end - start + 1
    handler.send_response(status)
    for k, v in getattr(handler, "_extra", []) or []:
        handler.send_header(k, v)
    handler._extra = []
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(length))
    if status == 206:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()
    with open(path, "rb") as f:
        f.seek(start)
        left = length
        while left > 0:
            chunk = f.read(min(1 << 20, left))
            if not chunk:
                break
            handler.wfile.write(chunk)
            left -= len(chunk)


# ---------- node side ----------
class Client:
    def __init__(self, hub, token, timeout=60):
        self.hub = hub.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method, path, body=None, headers=None):
        h = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {self.token}"} | (headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        return urllib.request.Request(f"{self.hub}/api/sync/{path}", data=data, method=method, headers=h)

    def json(self, method, path, body=None):
        try:
            with urllib.request.urlopen(self._req(method, path, body), timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                msg = json.load(e).get("error", "")
            except ValueError:
                msg = ""
            raise IOError(f"hub said {e.code} {msg}".strip()) from None

    def download(self, rel, dest, size, sha, log=print):
        part = dest.with_name(dest.name + ".part")
        done = part.stat().st_size if part.exists() else 0
        if done > size:
            part.unlink()
            done = 0
        headers = {"Range": f"bytes={done}-"} if done else {}
        req = self._req("GET", "file/" + urllib.request.quote(rel), headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            if done and r.status != 206:
                done = 0
            with open(part, "ab" if done else "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        if part.stat().st_size != size:
            raise IOError(f"{rel}: incomplete ({part.stat().st_size} of {size} bytes), will resume next time")
        if manifest.sha256_file(part) != sha:
            part.unlink()
            raise IOError(f"{rel}: checksum mismatch, discarded")
        os.replace(part, dest)


def pair_node(hub, code, token_file, name=""):
    req = urllib.request.Request(f"{hub.rstrip('/')}/api/sync/pair", method="POST",
                                 data=json.dumps({"code": code, "name": name}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Pairing failed: hub said {e.code}. Codes work once and expire after 15 minutes.")
    token_file = Path(token_file)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"hub": hub.rstrip("/"), "token": res["token"], "uid": res["uid"],
                            "name": res["name"], "role": res["role"]}))
    return res


def read_token(token_file):
    data = json.loads(Path(token_file).read_text())
    return data


def sync_files(client, store_dir, node_info, log=print):
    """Bring store_dir in line with the hub's plan for this node."""
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    plan = client.json("GET", "plan")
    previous = manifest.read(store_dir) or {}
    have = {f["path"]: f for f in manifest.scan(store_dir, previous=previous)}
    got, failed = 0, []
    for f in plan["files"]:
        dest = (store_dir / f["path"]).resolve()
        if not dest.is_relative_to(store_dir.resolve()):
            failed.append(f["path"])
            continue
        if have.get(f["path"], {}).get("sha256") == f["sha256"]:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        log(f"Fetching {f['path']} ({f['size'] / 1e6:.0f} MB)")
        try:
            client.download(f["path"], dest, f["size"], f["sha256"], log)
            got += 1
        except Exception as exc:
            log(f"  failed: {exc}")
            failed.append(f["path"])
    wanted = {f["path"] for f in plan["files"]}
    managed = {f["path"] for f in previous.get("files", [])}  # only remove files BUNKR put here
    removed = 0
    for rel in sorted(managed - wanted):
        p = store_dir / rel
        if p.is_file():
            p.unlink()
            removed += 1
            log(f"Removed {rel} (no longer planned for this node)")
    meta = {f["path"]: f for f in plan["files"]}
    files = manifest.scan(store_dir, previous=previous)
    manifest.annotate(files, meta.get)
    data = manifest.write(store_dir, node_info, files)
    return {"fetched": got, "failed": failed, "removed": removed, "manifest": data}


def sync_records(client, store, log=print):
    since = store.get_raw("sync_cursor", "") or ""
    out = export_records(store, since) | checkins.export(store, since)
    res = client.json("POST", "records", {"since": since, **out})
    result = apply_records(store, res["changes"], since, source="hub")
    result["family"] = checkins.apply(store, res["changes"], from_hub=True)
    result["situation"] = situation.apply(store, res["changes"])
    result["comms"] = comms.apply(store, res["changes"])
    result["briefs"] = brief.apply(store, res["changes"])
    result["forecasts"] = forecasts.apply(store, res["changes"])
    store.set_raw("sync_cursor", res["cursor"])
    log(f"Records: sent {len(out['items'])} items / {len(out['locations'])} locations, "
        f"received {result['applied']} change(s), {res.get('conflicts', 0) + result['conflicts']} conflict(s)")
    return result


def run(token_file, store_dir, db_path=None, log=print, files=True, records=True):
    from .db import Store
    tok = read_token(token_file)
    client = Client(tok["hub"], tok["token"])
    status = "ok"
    summary = {}
    try:
        if records:
            summary["records"] = sync_records(client, Store(db_path), log)
        if files:
            node_info = {"uid": tok["uid"], "name": tok["name"], "role": tok["role"]}
            r = sync_files(client, store_dir, node_info, log)
            summary["files"] = {k: r[k] for k in ("fetched", "failed", "removed")}
            if r["failed"]:
                status = f"{len(r['failed'])} file(s) failed"
            client.json("POST", "report", {"manifest": r["manifest"], "status": status})
    except (OSError, urllib.error.URLError) as exc:
        log(f"Sync failed: {exc}")
        raise SystemExit(1)
    log(f"Sync finished: {status}")
    return summary


if __name__ == "__main__":
    sys.exit("use: python3 -m bunkr sync ...")
