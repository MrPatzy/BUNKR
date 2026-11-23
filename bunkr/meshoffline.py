"""The offline mesh kit: everything to set up or reflash a MeshCore radio with no internet, kept in the store.

One store item, firmware `mesh-offline`, in `store/firmware/mesh-offline/` (synced to nodes with firmware, shown on
Downloads like the other firmware):
  firmware/<tag>/  MeshCore companion, repeater and room-server files for the chosen boards (Heltec V3, Heltec T114
                   and RAK4631 by default), the newest release and the one before, each checked against the SHA-256
                   GitHub publishes.
  wheels/          Python packages for meshcore-cli and esptool (plus pip) for Linux x86_64 and aarch64 on Python
                   3.11-3.13, so either tool installs with no internet. Only when pip is on this machine.
  README.txt       how to install the tools and flash a board offline.
  status.json      what the last run found.

The item's version is the newest MeshCore companion release it holds, so the usual rules apply: the 3 a.m. check
fetches a newer 1.x by itself and holds a jump to 2.x for an admin to approve. Between releases a daily run (4 a.m.)
refreshes the wheels within the same major version.

Each run builds `.mesh-offline-staging` next to the live folder and swaps it in only when every check passed, so a
failed or partial run leaves the last good copy. Nothing downloaded is ever run: pip only downloads binary wheels
(`--only-binary=:all:`, so no build step), and esptool, which may be published as source only, comes straight from
PyPI, checked against PyPI's SHA-256. Nothing is installed anywhere.
"""

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import tomllib
import urllib.request
from datetime import datetime, timezone

from . import library, manifest, releases

ITEM = "mesh-offline"
KIND = "firmware"
REL = f"firmware/{ITEM}"
REPO = "meshcore-dev/MeshCore"
PYPI = "https://pypi.org/pypi/{}/json"
FIRMWARE_KINDS = ("companion", "repeater", "room-server")
DEFAULT_BOARDS = ("heltec_v3", "heltec_t114", "rak4631")
KEEP_RELEASES = 2
PACKAGES = ["meshcore-cli", "pip"]
PY_VERSIONS = ["3.11", "3.12", "3.13"]
PLATFORMS = {
    "x86_64": ["manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64", "linux_x86_64"],
    "aarch64": ["manylinux_2_28_aarch64", "manylinux_2_17_aarch64", "manylinux2014_aarch64", "linux_aarch64"],
}
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# network and subprocess, kept in three small functions so tests can replace them
def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": releases.USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _download(url, dest, size, sha256, cancelled=lambda: False):
    library.download(url, dest, size, sha256, None, cancelled)


def _run(cmd):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")} | {"PIP_NO_INPUT": "1"}
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=1800, env=env)


# settings
def settings(store):
    s = store.get_raw("mesh_offline", {}) or {}
    boards = [b for b in s.get("boards") or DEFAULT_BOARDS if b in releases.BOARDS] or list(DEFAULT_BOARDS)
    return {"enabled": bool(s.get("enabled", True)), "boards": boards}


def save_settings(store, data):
    s = settings(store)
    if "boards" in data:
        boards = [b for b in (data.get("boards") or []) if b in releases.BOARDS]
        if not boards:
            raise ValueError("choose at least one board")
        s["boards"] = boards
    if "enabled" in data:
        s["enabled"] = bool(data["enabled"])
    store.set_raw("mesh_offline", s)
    return s


def status(store):
    return store.get_raw("mesh_offline_status", {}) or {}


# choosing firmware
def major(v):
    m = re.match(r"v?(\d+)", v or "")
    return int(m.group(1)) if m else None


def _version(kind, tag):
    return tag[len(kind) + 2:] if tag.startswith(kind + "-v") else tag.removeprefix("v")


def _releases(cache=None):
    rels = (cache or {}).get(REPO)
    if rels is None:
        rels = _get_json(f"{releases.API}/{REPO}/releases?per_page=40")
        if cache is not None:
            cache[REPO] = rels
    return [r for r in rels if not r.get("prerelease") and not r.get("draft")]


def prefixes(boards):
    return tuple(releases.BOARDS[b]["meshcore"].lower() + "_" for b in boards)


def pick(rels, boards, keep=KEEP_RELEASES, ceiling=None):
    """The files to hold: newest `keep` releases of each firmware kind (not above major version `ceiling`)."""
    pre = prefixes(boards)
    out = []
    for kind in FIRMWARE_KINDS:
        mine = [r for r in rels if r["tag_name"].startswith(kind + "-v")
                and (ceiling is None or (major(_version(kind, r["tag_name"])) or 0) <= ceiling)]
        mine.sort(key=lambda r: releases.vkey(_version(kind, r["tag_name"])), reverse=True)
        for rel in mine[:keep]:
            for a in rel.get("assets", []):
                if not a["name"].lower().startswith(pre):
                    continue
                digest = (a.get("digest") or "").removeprefix("sha256:")
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise IOError(f"{a['name']}: GitHub gave no SHA-256, so it isn't kept")
                if "/" in a["name"] or a["name"].startswith("."):
                    raise IOError(f"{a['name']}: odd file name")
                out.append({"release": rel["tag_name"], "kind": kind, "version": _version(kind, rel["tag_name"]),
                            "file": a["name"], "url": a["browser_download_url"], "size": a.get("size", 0),
                            "sha256": digest, "published": (rel.get("published_at") or "")[:10]})
    return out


def bundle_version(files):
    """The kit's version: the newest companion release it holds (else the newest of anything)."""
    comp = [f["version"] for f in files if f["kind"] == "companion"] or [f["version"] for f in files]
    return max(comp, key=releases.vkey) if comp else ""


def resolve(store, cache=None):
    """For the Downloads catalog: what the newest kit would be, shaped like releases.resolve_all() rows."""
    boards = settings(store)["boards"]
    item = {"kind": KIND, "id": ITEM, "online": False, "latest": "", "size": 0,
            "label": "Offline mesh kit: MeshCore firmware for " + ", ".join(releases.BOARDS[b]["label"].split(" (")[0] for b in boards)
                     + ", meshcore-cli and esptool"}
    try:
        files = library._cached(("mesh-offline", tuple(boards)), lambda: pick(_releases(cache), boards))
        item.update(online=True, latest=bundle_version(files), size=sum(f["size"] for f in files) + 40_000_000)
    except Exception as exc:
        item["error"] = str(exc)[:200]
    return item


# wheels
def pip_command():
    """How to run pip here, or None. pip is only ever asked to download."""
    if importlib.util.find_spec("pip"):
        return [sys.executable, "-m", "pip"]
    found = shutil.which("pip3") or shutil.which("pip")
    return [found] if found else None


def _build_requires(sdist):
    """build-system.requires from an sdist's pyproject.toml, read from the archive (nothing is unpacked or run)."""
    try:
        with tarfile.open(sdist) as t:
            for m in t.getmembers():
                if m.isfile() and m.name.count("/") == 1 and m.name.endswith("/pyproject.toml"):
                    data = tomllib.load(io.BytesIO(t.extractfile(m).read(200_000)))
                    req = data.get("build-system", {}).get("requires")
                    if isinstance(req, list) and all(isinstance(r, str) for r in req):
                        return req
    except (OSError, tarfile.TarError, tomllib.TOMLDecodeError, AttributeError):
        pass
    return ["setuptools>=64", "wheel"]


def _clean_req(r):
    return isinstance(r, str) and "extra ==" not in r and "extra==" not in r and re.match(r"^[A-Za-z0-9][\w.\-]*", r)


def fetch_esptool(wheels, current):
    """esptool from PyPI: its wheel if there is one, else its source archive. Returns extra requirements."""
    info = _get_json(PYPI.format("esptool"))
    urls = info.get("urls") or []
    pick_ = next((u for u in urls if u.get("packagetype") == "bdist_wheel" and u["filename"].endswith("-py3-none-any.whl")), None) \
        or next((u for u in urls if u.get("packagetype") == "sdist"), None)
    if not pick_:
        raise IOError("esptool: PyPI lists no file to download")
    name, sha = pick_["filename"], (pick_.get("digests") or {}).get("sha256", "")
    if "/" in name or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise IOError("esptool: PyPI gave no SHA-256 for " + name)
    dest, reuse = wheels / name, current / "wheels" / name
    if reuse.is_file() and manifest.sha256_file(reuse) == sha:
        shutil.copy2(reuse, dest)
    else:
        _download(pick_["url"], dest, pick_.get("size") or 0, sha)
    if manifest.sha256_file(dest) != sha:
        raise IOError(f"{name}: checksum mismatch")
    reqs = [r for r in (info.get("info") or {}).get("requires_dist") or [] if _clean_req(r)]
    if pick_["packagetype"] == "sdist":
        reqs += _build_requires(dest)  # so `pip install --no-index` can build it later, on the machine that wants it
    return reqs


def refresh_wheels(stage, current, pip):
    wheels = stage / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    extra = fetch_esptool(wheels, current)
    for arch, plats in PLATFORMS.items():
        for py in PY_VERSIONS:
            cmd = [*pip, "download", "--quiet", "--disable-pip-version-check", "--no-input", "--no-cache-dir",
                   "--only-binary=:all:", "--python-version", py, "--implementation", "cp", "-d", str(wheels)]
            for p in plats:
                cmd += ["--platform", p]
            _run(cmd + PACKAGES + extra)
    names = sorted(p.name for p in wheels.iterdir() if p.is_file())
    if not any(n.startswith("meshcore_cli-") for n in names):
        raise IOError("pip did not fetch meshcore-cli")
    return names


README = """Offline mesh tools (kept current by BUNKR, in the downloads store)
=================================================================

Boards in this copy: {boards}
Firmware releases: {releases}
{wheels_note}
Install meshcore-cli and esptool with no internet (Linux x86_64 or Raspberry Pi):

    python3 -m venv ~/mesh-venv          # Debian/Pi OS: needs the python3-venv package
    ~/mesh-venv/bin/pip install --no-index --find-links ./wheels meshcore-cli esptool

Talk to a MeshCore companion radio over USB:

    ~/mesh-venv/bin/meshcli -s /dev/ttyUSB0 infos

Flash a Heltec V3 (ESP32-S3) from ./firmware/<release>/:

  Update, keeps identity, contacts and settings:
    ~/mesh-venv/bin/esptool --chip esp32s3 -p /dev/ttyUSB0 -b 460800 write-flash 0x10000 Heltec_v3_..._usb-<ver>.bin
    ~/mesh-venv/bin/esptool --chip esp32s3 -p /dev/ttyUSB0 erase-region 0xe000 0x2000
  The second line clears the boot-slot record (otadata). Without it, a board
  that was ever updated over the air may keep booting the old copy in the
  second slot (0x340000) and the update silently does nothing.
  Fresh install, wipes the board:
    ~/mesh-venv/bin/esptool --chip esp32s3 -p /dev/ttyUSB0 -b 460800 erase-flash
    ~/mesh-venv/bin/esptool --chip esp32s3 -p /dev/ttyUSB0 -b 460800 write-flash 0x0 Heltec_v3_..._usb-<ver>-merged.bin
  Back up first: meshcli "get private_key", "contacts" and "get_channels".

Flash a Heltec T114 or RAK4631 (nRF52840): double-tap reset so a USB drive
appears, then copy the matching .uf2 file onto it. esptool is not used.

"usb" builds talk over the USB cable, "ble" builds over Bluetooth. Every
firmware file matches the SHA-256 GitHub published for it (see manifest.json).
status.json says when this copy was last refreshed.
"""


# the run
def refresh(depot, store, allow_major=False, pip=None, cancelled=lambda: False, cache=None):
    """Build a fresh kit in staging, check it, swap it in, record it in the store. Returns the status dict."""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "already running", "checked_at": _now()}
    try:
        return _refresh(depot, store, allow_major, pip_command() if pip is None else pip, cancelled, cache)
    finally:
        _lock.release()


def _refresh(depot, store, allow_major, pip, cancelled, cache):
    s = settings(store)
    live = depot.root / REL
    stage = depot.root / "firmware" / f".{ITEM}-staging"
    old = depot.root / "firmware" / f".{ITEM}-old"
    held = depot.held(KIND, ITEM)
    ceiling = None if allow_major or not held else major(held["version"])
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    prev = status(store)
    st = {"checked_at": _now(), "boards": s["boards"]}
    try:
        files = pick(_releases(cache), s["boards"], KEEP_RELEASES, ceiling)
        if not files:
            raise IOError("no firmware files matched; asset names may have changed")
        for f in files:
            if cancelled():
                raise InterruptedError("cancelled")
            dest = stage / "firmware" / f["release"] / f["file"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            reuse = live / "firmware" / f["release"] / f["file"]
            if reuse.is_file() and manifest.sha256_file(reuse) == f["sha256"]:
                shutil.copy2(reuse, dest)
            else:
                _download(f["url"], dest, f["size"], f["sha256"], cancelled)
            got = manifest.sha256_file(dest)
            if got != f["sha256"]:
                raise IOError(f"{f['file']}: checksum mismatch (expected {f['sha256']}, got {got})")
        if pip:
            wheels = refresh_wheels(stage, live, pip)
            st["wheels_note"] = ""
        elif (live / "wheels").is_dir():  # no pip here: keep the wheels we had
            shutil.copytree(live / "wheels", stage / "wheels")
            wheels = sorted(p.name for p in (stage / "wheels").iterdir() if p.is_file())
            st["wheels_note"] = "pip isn't installed on the hub, so these wheels weren't refreshed this time."
        else:
            wheels = []
            st["wheels_note"] = "pip isn't installed on the hub, so this copy has no Python wheels yet (install python3-pip)."
        version = bundle_version(files)
        rels = sorted({f["release"] for f in files})
        (stage / "README.txt").write_text(README.format(
            boards=", ".join(releases.BOARDS[b]["label"] for b in s["boards"]), releases=", ".join(rels),
            wheels_note=("\nNote: " + st["wheels_note"] + "\n") if st["wheels_note"] else ""))
        (stage / "manifest.json").write_text(json.dumps({"version": version, "firmware": files, "wheels": wheels}, indent=2))
        # swap: the live copy is replaced only now, after every check passed
        shutil.rmtree(old, ignore_errors=True)
        if live.exists():
            live.rename(old)
        stage.rename(live)
        shutil.rmtree(old, ignore_errors=True)
        urls = {f"{REL}/firmware/{f['release']}/{f['file']}": (f["sha256"], f["url"]) for f in files}
        st |= {"ok": True, "version": version, "releases": rels, "firmware_files": len(files), "wheels": len(wheels)}
        _write_status(depot, live, st, prev)
        keep = set()
        for p in sorted(live.rglob("*")):
            if p.is_file():
                rel = p.relative_to(depot.root).as_posix()
                sha, url = urls.get(rel) or (manifest.sha256_file(p), "")
                depot.record(KIND, ITEM, rel, version, p.stat().st_size, sha, url)
                keep.add(rel)
        for row in depot.store._rows("SELECT * FROM depot_files WHERE kind=? AND item_id=?", (KIND, ITEM)):
            if row["path"] not in keep:
                depot.remove(row)
        st["size_bytes"] = sum(r["size"] for r in depot.store._rows(
            "SELECT size FROM depot_files WHERE kind=? AND item_id=?", (KIND, ITEM)))
    except Exception as exc:  # keep the last good copy, record why this run failed
        shutil.rmtree(stage, ignore_errors=True)
        if isinstance(exc, subprocess.CalledProcessError):
            msg = f"pip download failed: {(exc.stderr or '').strip()[-300:]}"
        else:
            msg = f"{type(exc).__name__}: {exc}"
        st |= {"ok": False, "error": msg[:400]}
        if live.is_dir():
            _write_status(depot, live, st, prev)
    if st["ok"]:
        st["last_good"] = st["checked_at"]
    else:
        st["last_good"] = prev.get("last_good")
    store.set_raw("mesh_offline_status", st)
    return st


def _write_status(depot, live, st, prev):
    out = dict(st, last_good=st["checked_at"] if st.get("ok") else prev.get("last_good"))
    p = live / "status.json"
    p.write_text(json.dumps(out, indent=2))
    row = depot.by_path(p.relative_to(depot.root).as_posix())
    if row:  # keep its record matching, so the nightly checksum check doesn't flag it
        depot.record(row["kind"], row["item_id"], row["path"], row["version"], p.stat().st_size, manifest.sha256_file(p), "")
