"""Builds the kit on a drive in a background thread, reporting progress."""

import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import uuid

from . import export, library, manifest, planner

KIT_DIR = "BUNKR"


def kit_paths(mount):
    root = Path(mount) / KIT_DIR
    return {name: root / name for name in ("documents", "library", "tools", "inventory")} | {"root": root}


def existing_files(mount):
    """Library and tool files already in the kit, so rebuilding reuses them."""
    paths = kit_paths(mount)
    out = {}
    for key in ("library", "tools"):
        if paths[key].is_dir():
            for f in paths[key].iterdir():
                if f.is_file() and not f.name.endswith(".part"):
                    out[f.name] = f.stat().st_size
    return out


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"     # idle | running | done | failed | cancelled
        self.log = []
        self.step = ""
        self.bytes_done = 0
        self.bytes_total = 0
        self.started = None
        self.result = None      # optional structured result for the UI
        self._cancel = threading.Event()
        self.thread = None

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "log": self.log[-200:], "step": self.step,
                    "bytes_done": self.bytes_done, "bytes_total": self.bytes_total,
                    "started": self.started, "result": self.result}

    def say(self, msg):
        with self.lock:
            self.log.append(f"{datetime.now():%H:%M:%S}  {msg}")

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def start(self, fn, *args):
        with self.lock:
            if self.state == "running":
                raise RuntimeError("a build is already running")
            self.state, self.log, self.step = "running", [], ""
            self.bytes_done = self.bytes_total = 0
            self.result = None
            self.started = time.time()
            self._cancel.clear()

        def runner():
            try:
                fn(self, *args)
                final = "done"
            except InterruptedError:
                self.say("Cancelled. Partial downloads are kept and resume next time.")
                final = "cancelled"
            except Exception as exc:
                self.say(f"FAILED: {exc}")
                final = "failed"
            with self.lock:
                self.state = final
                self.step = ""

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()


def _stage_documents(docs, stage):
    """Symlink farm so the archive has tidy names: <Type>/<label or filename>."""
    used = set()
    for d in docs:
        src = Path(d["path"])
        folder = stage / d["doc_type"].capitalize()
        folder.mkdir(exist_ok=True)
        base = (d["label"].strip().replace("/", "-") or src.stem) + (src.suffix if src.is_file() else "")
        name, n = base, 2
        while (folder / name) in used:
            name, n = f"{base} ({n})", n + 1
        used.add(folder / name)
        os.symlink(src, folder / name)


def _encrypt_documents(job, docs, passphrase, out_dir, split, extra=None):
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    final = out_dir / f"documents-{stamp}.7z"
    tmp = out_dir / f".building-{stamp}.7z"
    for leftover in out_dir.glob(".building-*"):
        leftover.unlink()
    with tempfile.TemporaryDirectory(prefix="bunkr-") as stage_dir:
        stage = Path(stage_dir)
        _stage_documents(docs, stage)
        for name, text in (extra or {}).items():  # generated files, e.g. the family plan
            (stage / name).write_text(text)
        cmd = ["7z", "a", "-t7z", "-mhe=on", "-mx=5", "-p", "-bsp1", "-bso0", str(tmp), "."]
        if split:
            cmd.insert(-2, f"-v{planner.FAT32_VOLUME // 1024 ** 2}m")
        proc = subprocess.Popen(cmd, cwd=stage, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # 7z prompts for the password and its confirmation on stdin, which keeps
        # the passphrase out of the process list.
        _out, err = proc.communicate(f"{passphrase}\n{passphrase}\n")
        if proc.returncode != 0:
            for f in out_dir.glob(tmp.name + "*"):
                f.unlink()
            raise RuntimeError(f"7z failed: {err.strip()[-300:]}")

    parts = sorted(out_dir.glob(tmp.name + "*"))
    first = parts[0]
    # For "t", a bare -p means an empty password; leaving it off makes 7z prompt.
    test = subprocess.run(["7z", "t", str(first)], input=f"{passphrase}\n",
                          capture_output=True, text=True)
    if test.returncode != 0:
        for f in parts:
            f.unlink()
        raise RuntimeError("encrypted archive failed verification; nothing was replaced")

    new_names = []
    for p in parts:
        target = out_dir / p.name.replace(tmp.name, final.name)
        p.rename(target)
        new_names.append(target.name)
    # Keep the newest previous archive as a fallback, remove older ones.
    previous = sorted({f.name.split(".7z")[0] for f in out_dir.glob("documents-*.7z*")} - {final.stem})
    for stem in previous[:-1]:
        for f in out_dir.glob(stem + ".7z*"):
            f.unlink()
    return new_names


def _copy(src, dest, progress, cancelled):
    """Copy from the downloads store with progress; atomic via a .part file."""
    part = dest.with_name(dest.name + ".part")
    done = 0
    with open(src, "rb") as fi, open(part, "wb") as fo:
        for chunk in iter(lambda: fi.read(1 << 22), b""):
            if cancelled():
                raise InterruptedError("cancelled")
            fo.write(chunk)
            done += len(chunk)
            progress(done)
    os.replace(part, dest)


def build(job, mount, the_plan, documents, tools, libraries, settings, items, passphrase,
          depot=None, on_manifest=None, drive_label="", extra_files=None):
    paths = kit_paths(mount)
    known, meta = {}, {}  # relpath -> sha256 / {kind, item, version} for the manifest
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    chosen = {(i["kind"], i["id"]) for i in the_plan["included"]}
    contents = []

    job.step = "Writing inventory and gap report"
    job.say(job.step)
    (paths["inventory"] / "inventory.csv").write_text(export.inventory_csv(items))
    (paths["inventory"] / "inventory.html").write_text(export.report_html(settings, items, documents))
    (paths["inventory"] / "bunkr-data.json").write_text(export.data_json(settings, items, documents))
    contents.append("inventory/inventory.html, inventory.csv, bunkr-data.json")

    docs = [d for d in documents if ("document", d["id"]) in chosen]
    if docs:
        job.step = f"Encrypting {len(docs)} document entries"
        job.say(job.step)
        names = _encrypt_documents(job, docs, passphrase, paths["documents"], the_plan["split_documents"], extra_files)
        job.say(f"Documents encrypted and verified: {', '.join(names)}")
        for n in names:
            meta[f"documents/{n}"] = {"kind": "documents", "item": "archive",
                                     "version": n.split(".7z")[0].removeprefix("documents-")}
        contents.append(f"documents/{names[0]}  ({len(docs)} entries)")
    for s in the_plan["skipped"]:
        if s["kind"] == "document":
            job.say(f"Skipped document '{s['label']}': {s['reason']}")

    downloads = [("tool", t, paths["tools"] / t["file"]) for t in tools if ("tool", t["id"]) in chosen]
    downloads += [("library", l, paths["library"] / l["file"]) for l in libraries
                  if ("library", l["id"]) in chosen and l.get("file")]
    with job.lock:
        job.bytes_total = sum(item["size"] for _k, item, _d in downloads)
    base = 0
    for kind, item, dest in downloads:
        if job.cancelled:
            raise InterruptedError
        job.step = f"Downloading {dest.name}"
        url, size, sha = item["url"], item["size"], None
        if kind == "library" and item.get("meta4"):
            try:
                size, sha, mirrors = library.parse_meta4(library._get(item["meta4"]))
                url = mirrors[0] if mirrors else url
            except Exception as exc:
                job.say(f"Checksum list unavailable for {dest.name} ({exc}); downloading without it")

        def progress(n, base=base):
            with job.lock:
                job.bytes_done = base + n

        rel = f"{dest.parent.name}/{dest.name}"
        version = item.get("date", "") if kind == "library" else ""
        result = None
        stored = depot.held(kind, item["id"]) if depot else None
        if stored and Path(stored["path"]).name == dest.name and depot.path(stored).is_file():
            version = stored["version"]
            if dest.exists() and dest.stat().st_size == stored["size"]:
                result = "already present"
            else:
                job.step = f"Copying {dest.name} from the downloads store"
                _copy(depot.path(stored), dest, progress, lambda: job.cancelled)
                result = "copied from the downloads store"
            known[rel] = stored["sha256"]
        elif stored and kind == "tool" and depot.path(stored).is_file():
            version = stored["version"]
            _copy(depot.path(stored), dest, progress, lambda: job.cancelled)
            result, known[rel] = "copied from the downloads store", stored["sha256"]
        elif sha:
            known[rel] = sha
        for attempt in range(3 if result is None else 0):
            try:
                result = library.download(url, dest, size, sha, progress, lambda: job.cancelled)
                break
            except InterruptedError:
                raise
            except Exception as exc:
                job.say(f"{dest.name}: attempt {attempt + 1} failed ({exc})")
                time.sleep(3)
        if result is None:
            job.say(f"Giving up on {dest.name} for now; re-run the build to resume it")
            known.pop(rel, None)
        else:
            job.say(f"{dest.name}: {result}")
            meta[rel] = {"kind": kind, "item": item["id"], "version": version}
            contents.append(f"{kind}/{dest.name}")
            if kind == "library":  # remove older dated copies of the same archive
                prefix = dest.name.rsplit("_", 1)[0] + "_"
                for old in paths["library"].glob(prefix + "*.zim"):
                    if old != dest and old.name[len(prefix):][:7].replace("-", "").isdigit():
                        old.unlink()
                        job.say(f"Removed older {old.name}")
        base += item["size"]
        with job.lock:
            job.bytes_done = base

    for s in the_plan["skipped"]:
        if s["kind"] != "document":
            job.say(f"Not included: {s['label']} ({s['reason']})")

    (paths["root"] / "README.txt").write_text(export.README.format(
        built=datetime.now().strftime("%Y-%m-%d %H:%M"),
        contents="\n".join(f"  {c}" for c in contents)))

    job.step = "Writing the drive manifest (checksums)"
    job.say(job.step)
    previous = manifest.read(paths["root"]) or {}
    node = previous.get("node") or {"uid": uuid.uuid4().hex, "role": "drive"}
    node["name"] = node.get("name") or drive_label or Path(mount).name
    files = manifest.scan(paths["root"], subdirs=["documents", "library", "tools", "inventory"],
                          previous=previous, known=known, cancelled=lambda: job.cancelled)
    manifest.annotate(files, meta.get)
    data = manifest.write(paths["root"], node, files, built=datetime.now().isoformat(timespec="seconds"))
    job.say(f"Manifest written: {len(files)} files")
    if on_manifest:
        on_manifest(data)
    job.step = "Flushing to drive"
    job.say("Flushing writes to the drive (safe to remove once this finishes)")
    os.sync()
    free = shutil.disk_usage(mount).free
    job.say(f"Kit complete. {free / 1024 ** 3:.1f} GB still free on the drive.")
