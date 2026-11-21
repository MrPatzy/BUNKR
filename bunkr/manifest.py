"""Manifests: what files a node or drive holds, with sizes and SHA-256 checksums.

Every kit drive and synced node keeps BUNKR/MANIFEST.json (drives) or
<store>/MANIFEST.json (nodes). The hub compares manifests with its downloads
store to tell which files are out of date, missing or damaged.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

FORMAT = 1
NAME = "MANIFEST.json"
SKIP_SUFFIXES = (".part",)
SKIP_NAMES = {NAME, NAME + ".tmp"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path, cancelled=lambda: False, progress=None):
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            if cancelled():
                raise InterruptedError("cancelled")
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done)
    return h.hexdigest()


def scan(root, subdirs=None, previous=None, known=None, verify=False, cancelled=lambda: False):
    """List files under root (optionally only some subdirs) with checksums.

    Checksums are reused from `known` ({relpath: sha}) or from the previous
    manifest when size and mtime are unchanged, unless verify=True.
    """
    root = Path(root)
    prev = {f["path"]: f for f in (previous or {}).get("files", [])}
    known = known or {}
    out = []
    tops = [root / d for d in subdirs] if subdirs else [root]
    for top in tops:
        if not top.exists():
            continue
        walker = [(str(top.parent), [], [top.name])] if top.is_file() else os.walk(top)
        for dirpath, dirnames, filenames in walker:
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith(".") or name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
                    continue
                full = Path(dirpath) / name
                rel = full.relative_to(root).as_posix()
                st = full.stat()
                mtime = int(st.st_mtime)
                old = prev.get(rel)
                if not verify and rel in known:
                    sha = known[rel]
                elif not verify and old and old["size"] == st.st_size and old.get("mtime") == mtime and old.get("sha256"):
                    sha = old["sha256"]
                else:
                    sha = sha256_file(full, cancelled)
                entry = {"path": rel, "size": st.st_size, "mtime": mtime, "sha256": sha}
                if old:
                    for k in ("item", "kind", "version"):
                        if k in old:
                            entry[k] = old[k]
                out.append(entry)
    return out


def read(root):
    p = Path(root) / NAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("format") == FORMAT else None


def write(root, node, files, **extra):
    """Atomically write the manifest (temp file, then rename)."""
    root = Path(root)
    data = {"format": FORMAT, "generated": now(), "node": node, "files": files, **extra}
    tmp = root / (NAME + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, root / NAME)
    return data


def annotate(files, lookup):
    """Attach kind/item/version from lookup(path) -> dict or None."""
    for f in files:
        meta = lookup(f["path"])
        if meta:
            f.update({k: v for k, v in meta.items() if k in ("kind", "item", "version")})
    return files


def compare(expected, actual):
    """expected/actual: [{path, sha256, size}] -> {missing, changed, extra, ok} (paths)."""
    have = {f["path"]: f for f in actual}
    want = {f["path"]: f for f in expected}
    missing = [p for p in want if p not in have]
    changed = [p for p in want if p in have and have[p].get("sha256") != want[p].get("sha256")]
    extra = [p for p in have if p not in want]
    ok = [p for p in want if p in have and p not in changed]
    return {"missing": missing, "changed": changed, "extra": extra, "ok": ok}


def verify(root, manifest_data, cancelled=lambda: False):
    """Re-hash every file listed; return {damaged: [...], missing: [...], checked: n}."""
    root = Path(root)
    damaged, missing = [], []
    for f in manifest_data.get("files", []):
        p = root / f["path"]
        if not p.is_file():
            missing.append(f["path"])
        elif p.stat().st_size != f["size"] or sha256_file(p, cancelled) != f["sha256"]:
            damaged.append(f["path"])
    return {"damaged": damaged, "missing": missing, "checked": len(manifest_data.get("files", []))}
