"""How long database copies and old store versions are kept, and how much space they use.

Database copies in `db-backups/` (next to the database):
  bunkr-nightly-YYYYMMDD.db        the 2 a.m. copy: the newest `nightly_keep` (14), plus the first copy of each month
                                   for `monthly_keep` (12) months.
  bunkr-before-deploy-<commit>-YYYYMMDD-HHMM.db
                                   made by deploy/autodeploy.sh before each deploy: the newest `deploy_keep` (10).
                                   (autodeploy.sh reads the same setting and prunes the same way.)
  bunkr-nightly-YYYYMMDD.part      an unfinished nightly copy: removed after 2 days.
  anything else                    (bunkr-pre-phase7.db, copies made by hand) is never removed here.
Never removed: the newest copy of all, and the copy the restore test last used.

Store: when every file of an item's newest version is verified, older versions of libraries, tools, maps, firmware and
apps are removed unless an admin kept that version ("Keep" on Downloads). Unfinished downloads (*.part, *.new) older
than 7 days go too, and only while no download is running. Files the store has no record of are listed, never removed.

`plan_*` only says what would happen (the dry run, shown to admins); `prune_*` does it.
"""

import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import releases

DEFAULTS = {"deploy_keep": 10, "nightly_keep": 14, "monthly_keep": 12, "clean_store": True}
LIMITS = {"deploy_keep": (1, 200), "nightly_keep": (1, 365), "monthly_keep": (0, 120)}
NIGHTLY = re.compile(r"^bunkr-nightly-(\d{8})\.db$")
DEPLOY = re.compile(r"^bunkr-before-deploy-[0-9a-f]{7,40}-(\d{8})-(\d{4})\.db$")
NIGHTLY_PART = re.compile(r"^bunkr-nightly-\d{8}\.part$")
PART_AGE = 2 * 86400
STORE_KINDS = ("library", "tool", "map", "firmware", "app")
STORE_SKIP_ITEMS = {"mesh-offline"}   # keeps its previous release inside itself on purpose
TEMP_SUFFIXES = (".part", ".new")
TEMP_AGE = 7 * 86400


def settings(store):
    s = DEFAULTS | (store.get_raw("retention", {}) or {})
    for k, (lo, hi) in LIMITS.items():
        try:
            s[k] = min(hi, max(lo, int(s[k])))
        except (TypeError, ValueError):
            s[k] = DEFAULTS[k]
    s["clean_store"] = bool(s["clean_store"])
    return {k: s[k] for k in DEFAULTS}


def save_settings(store, data):
    s = settings(store)
    for k, (lo, hi) in LIMITS.items():
        if k in data:
            try:
                v = int(data[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a whole number")
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            s[k] = v
    if "clean_store" in data:
        s["clean_store"] = bool(data["clean_store"])
    store.set_raw("retention", s)
    return s


def backups_folder(store):
    return Path(store.path).parent / "db-backups"


# ---------- database copies ----------
def classify(name):
    """'nightly', 'deploy', 'partial' or 'other', and the date it carries (YYYYMMDD[HHMM]) if any."""
    m = NIGHTLY.match(name)
    if m:
        return "nightly", m.group(1)
    m = DEPLOY.match(name)
    if m:
        return "deploy", m.group(1) + m.group(2)
    if NIGHTLY_PART.match(name):
        return "partial", ""
    return "other", ""


def _months_between(a, b):
    """Whole calendar months from YYYYMM a to YYYYMM b."""
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[4:6]) - int(a[4:6])


def plan_backups(store, folder=None, now=None):
    """Every file in db-backups with keep/delete and why. Deletes nothing."""
    s = settings(store)
    d = Path(folder) if folder else backups_folder(store)
    now = now or datetime.now()
    files = sorted((p for p in d.iterdir() if p.is_file()), key=lambda p: p.name) if d.is_dir() else []
    test = (store.get_raw("restore_test") or {}).get("backup") or ""
    dbs = [p for p in files if p.suffix == ".db"]
    newest = max(dbs, key=lambda p: p.stat().st_mtime).name if dbs else ""
    info = {p.name: (classify(p.name), p.stat()) for p in files}
    nightly = sorted((n for n, ((c, _), _) in info.items() if c == "nightly"), key=lambda n: info[n][0][1], reverse=True)
    deploy = sorted((n for n, ((c, _), _) in info.items() if c == "deploy"), key=lambda n: info[n][0][1], reverse=True)
    recent = set(nightly[:s["nightly_keep"]])
    first_of_month = {}
    for n in sorted(nightly, key=lambda n: info[n][0][1]):
        first_of_month.setdefault(info[n][0][1][:6], n)
    month_now = now.strftime("%Y%m")
    monthly = {n for ym, n in first_of_month.items() if _months_between(ym, month_now) < s["monthly_keep"]}
    keep_deploy = set(deploy[:s["deploy_keep"]])
    out = []
    for p in files:
        (cls, _date), st = info[p.name]
        row = {"name": p.name, "class": cls, "bytes": st.st_size,
               "at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")}
        if p.name == test:
            act, why = "keep", "the restore test uses it"
        elif p.name == newest:
            act, why = "keep", "the newest copy"
        elif cls == "nightly":
            if p.name in recent:
                act, why = "keep", f"one of the newest {s['nightly_keep']} nightly copies"
            elif p.name in monthly:
                act, why = "keep", f"first copy of its month (monthly copies kept {s['monthly_keep']} months)"
            else:
                act, why = "delete", "older nightly copy"
        elif cls == "deploy":
            act, why = ("keep", f"one of the newest {s['deploy_keep']} deploy copies") if p.name in keep_deploy \
                else ("delete", "older deploy copy")
        elif cls == "partial":
            act, why = ("delete", "unfinished nightly copy") if time.time() - st.st_mtime > PART_AGE \
                else ("keep", "may still be being written")
        else:
            act, why = "keep", "not made by the nightly or deploy copy, so never removed automatically"
        out.append(row | {"action": act, "why": why})
    return out


def prune_backups(store, folder=None, now=None):
    plan = plan_backups(store, folder, now)
    d = Path(folder) if folder else backups_folder(store)
    for r in plan:
        if r["action"] == "delete":
            (d / r["name"]).unlink(missing_ok=True)
    return plan


# ---------- the downloads store ----------
def pin_key(row):
    return f"{row['kind']}/{row['item_id']}/{row['version']}"


def pins(store):
    return set(store.get_raw("store_pinned", []) or [])


def set_pin(store, kind, item_id, version, on):
    p = pins(store)
    key = f"{kind}/{item_id}/{version}"
    (p.add if on else p.discard)(key)
    store.set_raw("store_pinned", sorted(p))
    return sorted(p)


def plan_store(depot, job_running=False):
    """What a store clean-up would remove: {"delete": [...], "unknown": [...]} (dry run)."""
    rows = depot.rows()
    pinned = pins(depot.store)
    delete, unknown = [], []
    groups = {}
    for r in rows:
        if r["kind"] in STORE_KINDS and r["item_id"] not in STORE_SKIP_ITEMS:
            groups.setdefault((r["kind"], r["item_id"]), []).append(r)
    for (kind, item), rs in groups.items():
        versions = sorted({r["version"] for r in rs}, key=releases.vkey)
        if len(versions) < 2:
            continue
        newest = versions[-1]
        if not all(r["status"] == "ok" and depot.path(r).is_file() for r in rs if r["version"] == newest):
            continue  # the new one isn't verified yet: keep the old one
        for r in rs:
            if r["version"] != newest and pin_key(r) not in pinned:
                delete.append({"path": r["path"], "bytes": r["size"], "id": r["id"],
                               "why": f"superseded by version {newest}"})
    known = {r["path"] for r in rows}
    root = depot.root
    now = time.time()
    if root.is_dir():
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel in known or any(part.startswith(".mesh-offline-") for part in p.relative_to(root).parts):
                continue
            st = p.stat()
            if p.name.endswith(TEMP_SUFFIXES):
                if not job_running and now - st.st_mtime > TEMP_AGE:
                    delete.append({"path": rel, "bytes": st.st_size, "why": "unfinished download, untouched for a week"})
                continue
            unknown.append({"path": rel, "bytes": st.st_size, "why": "not a file the store has a record of"})
    return {"delete": delete, "unknown": unknown}


def prune_store(depot, job_running=False):
    plan = plan_store(depot, job_running)
    by_id = {r["id"]: r for r in depot.rows()}
    for d in plan["delete"]:
        if d.get("id") in by_id:
            depot.remove(by_id[d["id"]])
        else:
            (depot.root / d["path"]).unlink(missing_ok=True)
    return plan


# ---------- space used ----------
def _du(path):
    total = 0
    if path.is_dir():
        for p in path.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                pass
    return total


def space(store, depot):
    """For admins: what the database, its copies and the store take up, and what's free."""
    from .depot import SUBDIR
    db = Path(store.path)
    folder = backups_folder(store)
    by_class = {}
    for p in folder.iterdir() if folder.is_dir() else []:
        if p.is_file():
            c = by_class.setdefault(classify(p.name)[0], {"files": 0, "bytes": 0})
            c["files"] += 1
            c["bytes"] += p.stat().st_size
    parts = {name: _du(depot.root / sub) for name, sub in SUBDIR.items()}
    try:
        du = shutil.disk_usage(depot.root if depot.root.exists() else db.parent)
        free, total = du.free, du.total
    except OSError:
        free = total = 0
    db_bytes = sum(p.stat().st_size for p in (db, db.with_name(db.name + "-wal")) if p.is_file())
    return {"database": db_bytes, "backups": by_class, "backups_bytes": sum(c["bytes"] for c in by_class.values()),
            "store": parts, "store_bytes": sum(parts.values()), "free": free, "total": total}
