"""The downloads store: one local copy of the latest version of every library and tool.

Nodes and kit drives fill from here instead of each downloading from the
internet. Every file is recorded with its SHA-256; libraries are checked
against the checksum Kiwix publishes, tools are hashed after download.
Default location: /var/lib/bunkr/store (or <data dir>/store), override with --store.
"""

import os
import shutil
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import library, manifest

KINDS = ("library", "tool", "map", "firmware", "radio", "app", "documents", "web", "plan")
SUBDIR = {"library": "library", "tool": "tools", "map": "maps", "firmware": "firmware",
          "radio": "radio", "app": "apps", "documents": "documents", "web": "web", "plan": "plan"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Depot:
    def __init__(self, store, root):
        self.store = store
        self.root = Path(root)

    def ensure(self):
        for d in SUBDIR.values():
            (self.root / d).mkdir(parents=True, exist_ok=True)

    # records
    def rows(self):
        return self.store._rows("SELECT * FROM depot_files ORDER BY kind, item_id, version DESC")

    def held(self, kind, item_id):
        rows = self.store._rows("SELECT * FROM depot_files WHERE kind=? AND item_id=? AND status='ok' "
                                "ORDER BY version DESC, fetched_at DESC", (kind, item_id))
        return rows[0] if rows else None

    def by_path(self, rel):
        rows = self.store._rows("SELECT * FROM depot_files WHERE path=?", (rel,))
        return rows[0] if rows else None

    def by_sha(self, sha):
        rows = self.store._rows("SELECT * FROM depot_files WHERE sha256=? AND status='ok'", (sha,))
        return rows[0] if rows else None

    def record(self, kind, item_id, rel, version, size, sha, url=""):
        now = _now()
        self.store._exec("""INSERT INTO depot_files(kind, item_id, path, version, size, sha256, source_url,
                            fetched_at, verified_at, status) VALUES(?,?,?,?,?,?,?,?,?, 'ok')
                            ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, item_id=excluded.item_id,
                            version=excluded.version, size=excluded.size, sha256=excluded.sha256,
                            source_url=excluded.source_url, fetched_at=excluded.fetched_at,
                            verified_at=excluded.verified_at, status='ok'""",
                         (kind, item_id, rel, version, size, sha, url, now, now))

    def remove(self, row):
        p = self.root / row["path"]
        if p.is_file():
            p.unlink()
        self.store._exec("DELETE FROM depot_files WHERE id=?", (row["id"],))

    def prune_older(self, kind, item_id, keep_path):
        """Drop older versions of an item once a newer one is verified."""
        for row in self.store._rows("SELECT * FROM depot_files WHERE kind=? AND item_id=? AND path!=?",
                                    (kind, item_id, keep_path)):
            self.remove(row)

    def prune_versions(self, kind, item_id, keep_version):
        """For items made of several files (firmware): drop every other version."""
        for row in self.store._rows("SELECT * FROM depot_files WHERE kind=? AND item_id=? AND version!=?",
                                    (kind, item_id, keep_version)):
            self.remove(row)

    def path(self, row):
        return self.root / row["path"]

    def usage(self):
        try:
            du = shutil.disk_usage(self.root if self.root.exists() else self.root.parent)
            free, total = du.free, du.total
        except OSError:
            free = total = 0
        held = sum(r["size"] for r in self.rows())
        return {"root": str(self.root), "held_bytes": held, "free": free, "total": total}

    # fetching
    def fetch_library(self, lib, progress=None, cancelled=lambda: False):
        """lib: a resolved curated entry (library.resolve_all). Returns the new row."""
        if not lib.get("url"):
            raise IOError(f"{lib['id']}: not available in the Kiwix catalog right now")
        size, sha, url = lib["size"], None, lib["url"]
        if lib.get("meta4"):
            size, sha, mirrors = library.parse_meta4(library._get(lib["meta4"]))
            url = mirrors[0] if mirrors else url
        rel = f"{SUBDIR['library']}/{lib['file']}"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        library.download(url, dest, size, sha, progress, cancelled)
        if not sha:
            sha = manifest.sha256_file(dest, cancelled)
        self.record("library", lib["id"], rel, lib.get("date", ""), dest.stat().st_size, sha, lib["url"])
        self.prune_older("library", lib["id"], rel)
        return self.by_path(rel)

    def tool_version(self, tool):
        """Tools use fixed 'latest' URLs; the server's Last-Modified date is the version."""
        req = urllib.request.Request(tool["url"], method="HEAD", headers={"User-Agent": library.USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            lm = resp.headers.get("Last-Modified") or ""
            size = int(resp.headers.get("Content-Length") or 0)
        try:
            version = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z").date().isoformat()
        except ValueError:
            version = lm or ""
        return version, size

    def fetch_tool(self, tool, progress=None, cancelled=lambda: False):
        version, size = self.tool_version(tool)
        rel = f"{SUBDIR['tool']}/{tool['file']}"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        old = self.by_path(rel)
        if dest.exists() and old and old["version"] == version and dest.stat().st_size == old["size"]:
            return old
        tmp = dest.with_name(dest.name + ".new")
        library.download(tool["url"], tmp, size, None, progress, cancelled)
        sha = manifest.sha256_file(tmp, cancelled)
        os.replace(tmp, dest)
        self.record("tool", tool["id"], rel, version, dest.stat().st_size, sha, tool["url"])
        return self.by_path(rel)

    def add_file(self, kind, item_id, src, version="", url="", move=False):
        """Put a local file (e.g. a document archive or a firmware file) into the store."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind}")
        src = Path(src)
        rel = f"{SUBDIR[kind]}/{src.name}"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dest.resolve():
            (shutil.move if move else shutil.copy2)(src, dest)
        self.record(kind, item_id, rel, version, dest.stat().st_size, manifest.sha256_file(dest), url)
        return self.by_path(rel)

    def verify(self, cancelled=lambda: False):
        """Re-hash every stored file; mark damaged or missing ones."""
        bad = []
        for row in self.rows():
            p = self.path(row)
            if not p.is_file():
                status = "missing"
            elif p.stat().st_size != row["size"] or manifest.sha256_file(p, cancelled) != row["sha256"]:
                status = "corrupt"
            else:
                status = "ok"
            self.store._exec("UPDATE depot_files SET status=?, verified_at=? WHERE id=?", (status, _now(), row["id"]))
            if status != "ok":
                bad.append({"path": row["path"], "status": status})
        return bad

    # catalog view
    def catalog(self, libs, tools):
        """Every library and tool BUNKR knows, with what the store holds and what's newest."""
        out = []
        for lib in libs:
            row = self.held("library", lib["id"])
            out.append({"kind": "library", "id": lib["id"], "label": lib["why"], "tier": lib["tier"],
                        "group": lib.get("group"), "latest": lib.get("date", ""), "latest_size": lib["size"],
                        "online": lib.get("online", False), "held": row,
                        "update": bool(row and lib.get("date") and lib["date"] > row["version"])})
        for t in tools:
            row = self.held("tool", t["id"])
            out.append({"kind": "tool", "id": t["id"], "label": t["why"], "tier": 0, "group": None,
                        "latest": "", "latest_size": t["size"], "online": "error" not in t, "held": row,
                        "update": False})
        return out

    def store_manifest(self):
        """Manifest of the store itself, from its records (no re-hashing)."""
        return [{"path": r["path"], "size": r["size"], "sha256": r["sha256"], "kind": r["kind"],
                 "item": r["item_id"], "version": r["version"]} for r in self.rows() if r["status"] == "ok"]
