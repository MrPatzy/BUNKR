"""Nightly database copies, and a monthly proof that the newest copy actually restores.

The copy uses SQLite's online backup, so it is consistent even while BUNKR is writing. It lands in `db-backups/`
next to the database (the same folder the deploy and schema-change copies use) as bunkr-nightly-YYYYMMDD.db. How many
are kept (the newest 14 plus one a month for 12 months, by default) is up to retention.py, run after the restore test. Secrets that live in files next to the database (the push key, the AI login file, source keys)
are not in it, on purpose.

The restore test opens a throwaway copy of the newest backup, checks it, upgrades it to this code's schema the same
way a restore would, reads the main tables back, and compares row counts. Nothing live is touched. The result is
kept in settings (`restore_test`) and shown to admins on Home; a failure is a reason to look today, not next month.
"""

import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEST_EVERY_DAYS = 30
TABLES = ("items", "locations", "people", "sites", "contacts", "channels", "checkins", "documents")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def folder(store):
    return Path(store.path).parent / "db-backups"


def nightly_copy(store, when=None):
    """Write tonight's copy (once a day). Returns the path. Old copies are pruned by retention.prune_backups."""
    d = folder(store)
    d.mkdir(parents=True, exist_ok=True)
    day = (when or datetime.now()).strftime("%Y%m%d")
    dest = d / f"bunkr-nightly-{day}.db"
    if not dest.exists():
        tmp = dest.with_suffix(".part")
        with store._lock:
            out = sqlite3.connect(tmp)
            try:
                store._conn.backup(out)
            finally:
                out.close()
        tmp.chmod(0o640)
        tmp.replace(dest)
    store.set_raw("db_backup_last", {"at": now(), "file": dest.name, "bytes": dest.stat().st_size})
    return dest


def newest(store):
    files = [p for p in folder(store).glob("*.db") if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def _counts(conn):
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES if t in have}


def restore_test(store, backup=None):
    """Prove a backup restores. Records and returns {ok, at, backup, schema_from, schema_to, rows, problem}."""
    from .db import Store, SCHEMA_VERSION
    backup = Path(backup) if backup else newest(store)
    res = {"at": now(), "ok": False, "backup": backup.name if backup else "", "problem": ""}
    if not backup:
        res["problem"] = "there is no backup to test yet"
        store.set_raw("restore_test", res)
        return res
    res["backup_at"] = datetime.fromtimestamp(backup.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory(prefix=".restore-test-", dir=Path(store.path).parent) as tmp:
        copy = Path(tmp) / "bunkr.db"
        try:
            shutil.copyfile(backup, copy)
            conn = sqlite3.connect(copy)
            try:
                check = conn.execute("PRAGMA integrity_check").fetchone()[0]
                res["schema_from"] = conn.execute("PRAGMA user_version").fetchone()[0]
                before = _counts(conn)
            finally:
                conn.close()
            if check != "ok":
                res["problem"] = f"the copy is damaged ({check[:120]})"
            else:
                restored = Store(copy)  # upgrades the copy exactly as a restore would
                try:
                    after = _counts(restored._conn)
                    restored.items()
                    restored.sites()
                    res["schema_to"] = restored._conn.execute("PRAGMA user_version").fetchone()[0]
                finally:
                    restored._conn.close()
                res["rows"] = after
                changed = [t for t in before if before[t] != after.get(t)]
                if res["schema_to"] != SCHEMA_VERSION:
                    res["problem"] = f"it upgraded to schema {res['schema_to']}, not {SCHEMA_VERSION}"
                elif changed:
                    res["problem"] = "row counts changed while upgrading: " + ", ".join(changed)
                else:
                    res["ok"] = True
        except Exception as exc:  # anything at all is a failed test, never a crash
            res["problem"] = f"{type(exc).__name__}: {exc}"[:200]
    store.set_raw("restore_test", res)
    return res


def due(store, at=None):
    last = store.get_raw("restore_test") or {}
    if not last.get("at"):
        return True
    then = datetime.fromisoformat(last["at"])
    return (at or datetime.now(timezone.utc)) - then >= timedelta(days=TEST_EVERY_DAYS if last.get("ok") else 1)


def nightly(store):
    """The scheduled job: tonight's copy, the restore test when it's due, then old copies pruned."""
    from . import retention
    nightly_copy(store)
    if due(store):
        restore_test(store)
    retention.prune_backups(store, folder(store))


def status(store, at=None):
    """For admins on Home: one line, and whether it needs attention."""
    at = at or datetime.now(timezone.utc)
    last = store.get_raw("db_backup_last") or {}
    test = store.get_raw("restore_test") or {}
    bad = []
    if not last.get("at"):
        bad.append("No nightly backup yet.")
    elif at - datetime.fromisoformat(last["at"]) > timedelta(hours=36):
        bad.append("The nightly backup hasn't run for over a day.")
    if test.get("at") and not test.get("ok"):
        bad.append(f"The restore test failed: {test.get('problem')}.")
    elif test.get("at") and at - datetime.fromisoformat(test["at"]) > timedelta(days=TEST_EVERY_DAYS + 5):
        bad.append("The restore test is overdue.")
    return {"backup": last, "test": test, "problems": bad, "ok": not bad and bool(test.get("ok"))}
