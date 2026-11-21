"""CHIRP, the radio programming tool, kept fresh in the store.

Linux (Pi, laptop): CHIRP's source from GitHub (kk7ds/chirp), fetched nightly and pinned to
the commit. Nodes install it from the store with Debian's own packages for its dependencies.
Windows: CHIRP's daily installers sit behind a browser check on chirpmyradio.com, so they
can't be fetched automatically. You download one in your browser and add it here; BUNKR
checksums it, carries it to nodes and kit drives, and reminds you every 90 days.
"""

import hashlib
import json
import re
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path

API = "https://api.github.com/repos/kk7ds/chirp/commits/master"
ZIP = "https://codeload.github.com/kk7ds/chirp/zip/{sha}"
DOWNLOAD_PAGE = "https://chirpmyradio.com/projects/chirp/wiki/Download"
SRC_ID, WIN_ID = "chirp_linux_src", "chirp_windows"
SUBDIR = "radio/software"
REFRESH_DAYS = 90
MAX_SRC = 50_000_000
MAX_INSTALLER = 300_000_000
UA = {"User-Agent": "BUNKR (offline preparedness store)"}
NAME_RE = re.compile(r"^chirp-next-(\d{8})[\w.-]*\.(exe|msi|zip|whl|dmg)$", re.I)


def latest_commit(opener=urllib.request.urlopen):
    with opener(urllib.request.Request(API, headers=UA), timeout=30) as r:
        d = json.load(r)
    sha = d["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("unexpected commit id from GitHub")
    when = d["commit"]["committer"]["date"][:10].replace("-", "")
    return sha, when


def check_source_zip(path, sha):
    """A real CHIRP source tree for that commit, and an intact zip."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        top = f"chirp-{sha}/"
        for need in ("pyproject.toml", "chirp/__init__.py", "chirp/drivers/"):
            if not any(n == top + need or n.startswith(top + need) for n in names):
                raise ValueError(f"not a CHIRP source archive (missing {need})")
        if any(n.startswith("/") or ".." in n.split("/") for n in names):
            raise ValueError("archive has unsafe paths")
        bad = z.testzip()
        if bad:
            raise ValueError(f"damaged archive ({bad})")


def fetch_source(depot, opener=urllib.request.urlopen):
    """Store the newest CHIRP source if it changed. Returns (row, changed)."""
    sha, when = latest_commit(opener)
    version = f"{when}-{sha[:7]}"
    held = depot.held("radio", SRC_ID)
    if held and held["version"] == version and (depot.root / held["path"]).is_file():
        return held, False
    rel = f"{SUBDIR}/chirp-src-{version}.zip"
    dest = depot.root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".zip.part")
    h, size = hashlib.sha256(), 0
    url = ZIP.format(sha=sha)
    with opener(urllib.request.Request(url, headers=UA), timeout=60) as r, open(part, "wb") as out:
        while chunk := r.read(1 << 16):
            size += len(chunk)
            if size > MAX_SRC:
                raise ValueError("CHIRP source is unexpectedly large")
            h.update(chunk)
            out.write(chunk)
    try:
        check_source_zip(part, sha)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    depot.record("radio", SRC_ID, rel, version, size, h.hexdigest(), url)
    depot.prune_older("radio", SRC_ID, rel)
    return depot.held("radio", SRC_ID), True


def add_installer(depot, name, stream, length):
    """Save an installer you downloaded in your browser. `stream` is read up to `length` bytes."""
    name = Path(name or "").name
    m = NAME_RE.match(name)
    if not m:
        raise ValueError("expected a CHIRP download named like chirp-next-20261118-installer.exe")
    if not 0 < length <= MAX_INSTALLER:
        raise ValueError("file is empty or too large")
    rel = f"{SUBDIR}/{name}"
    dest = depot.root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h, got, first = hashlib.sha256(), 0, b""
    with open(part, "wb") as out:
        while got < length:
            chunk = stream.read(min(1 << 16, length - got))
            if not chunk:
                break
            if not first:
                first = chunk[:4]
            got += len(chunk)
            h.update(chunk)
            out.write(chunk)
    if got != length:
        part.unlink(missing_ok=True)
        raise ValueError("upload was cut short; try again")
    ext = m.group(2).lower()
    if (ext in ("exe", "msi") and not first.startswith(b"MZ") and not first.startswith(b"\xd0\xcf")) or \
            (ext in ("zip", "whl") and not first.startswith(b"PK")):
        part.unlink(missing_ok=True)
        raise ValueError("that file doesn't look like a CHIRP installer")
    part.replace(dest)
    version = m.group(1)
    depot.record("radio", WIN_ID, rel, version, got, h.hexdigest(), DOWNLOAD_PAGE)
    depot.prune_older("radio", WIN_ID, rel)
    return depot.held("radio", WIN_ID)


def _age_days(row, today):
    try:
        built = datetime.strptime(row["version"][:8], "%Y%m%d").date()
    except ValueError:
        built = date.fromisoformat((row.get("fetched_at") or today.isoformat())[:10])
    return (today - built).days


def status(depot, today=None):
    today = today or date.today()
    src, win = depot.held("radio", SRC_ID), depot.held("radio", WIN_ID)
    out = {"download_page": DOWNLOAD_PAGE, "refresh_days": REFRESH_DAYS, "linux": None, "windows": None}
    if src:
        out["linux"] = {"version": src["version"], "path": src["path"], "size": src["size"],
                        "fetched_at": src["fetched_at"], "sha256": src["sha256"]}
    if win:
        age = _age_days(win, today)
        out["windows"] = {"version": win["version"], "file": Path(win["path"]).name, "path": win["path"],
                          "size": win["size"], "sha256": win["sha256"], "age_days": age, "due": age >= REFRESH_DAYS}
    return out
