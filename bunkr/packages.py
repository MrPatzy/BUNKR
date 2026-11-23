"""OS packages kept in the store, so a node or a rebuilt machine can install them with no internet.

The hub downloads the Ubuntu or Debian packages BUNKR can use but doesn't ship (today: Tesseract, for reading
nutrition labels) with everything they need beyond a base system, once a week, into
`store/packages/<system>/` (for example `ubuntu-noble-amd64`), with a `Packages` index so apt can use the folder as
a local package source. Like the rest of the store they sync to the field nodes. deploy/install.sh adds a matching
folder as a package source, so an install with no internet still gets them.

Admins can add more package names to the list (settings "os_packages_extra", see packs.py); they are fetched the same
way, with their dependencies. Offline packs (packs.py) can hold packages too, in a folder of their own.

Only packages for the hub's own system can be fetched this way (a Pi on Debian for ARM fetches its own when it's online).
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

OPTIONAL = ["tesseract-ocr", "tesseract-ocr-eng"]
SKIP_PRIORITY = {"required", "important"}   # on every system already
# Debian policy: lower case letters, digits, + - . ; at least two characters, starting with a letter or digit
NAME_RE = re.compile(r"[a-z0-9][a-z0-9+.-]{1,99}")


def valid_name(name):
    """A Debian package name, or ValueError."""
    name = str(name or "").strip()
    if not NAME_RE.fullmatch(name):
        raise ValueError("that isn't a package name (lower case letters, digits, + - . only)")
    return name


def _run(cmd, cwd=None, timeout=600):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def system_id(run=_run):
    """ "ubuntu-noble-amd64": which system the packages are for."""
    info = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v.strip().strip('"')
    except OSError:
        return ""
    arch = run(["dpkg", "--print-architecture"]).stdout.strip()
    if not (info.get("ID") and info.get("VERSION_CODENAME") and arch):
        return ""
    return f"{info['ID']}-{info['VERSION_CODENAME']}-{arch}"


def known(names, run=_run):
    """The names apt can download (a real package, not a virtual one or a typo)."""
    out = []
    for n in names:
        show = run(["apt-cache", "show", "--no-all-versions", n])
        if show.returncode == 0 and "Package:" in (show.stdout or ""):
            out.append(n)
    return out


def closure(names, run=_run):
    """The packages and everything they depend on, leaving out what every system has."""
    return sorted(closure_sizes(names, run))


def closure_sizes(names, run=_run):
    """{package: download size in bytes} for the packages and what they depend on, leaving out what every system has."""
    if not names:
        return {}
    r = run(["apt-cache", "depends", "--recurse", "--no-recommends", "--no-suggests", "--no-conflicts", "--no-breaks",
             "--no-replaces", "--no-enhances", *names])
    if r.returncode:
        raise RuntimeError((r.stderr or "apt-cache failed").strip()[:200])
    pkgs = sorted({ln.strip() for ln in r.stdout.splitlines() if ln and not ln.startswith(" ") and "<" not in ln})
    out = {}
    for p in pkgs:
        show = run(["apt-cache", "show", "--no-all-versions", p]).stdout
        field = lambda k: next((ln.split(":", 1)[1].strip() for ln in show.splitlines() if ln.startswith(k + ":")), "")
        if show and field("Priority") not in SKIP_PRIORITY:
            out[p] = int(field("Size")) if field("Size").isdigit() else 0
    return out


def preview(current, adding, run=_run):
    """Download size of the list now and with `adding` too: {"before", "after", "new"} (new = packages pulled in)."""
    before = closure_sizes(known(current, run), run)
    after = closure_sizes(known(sorted(set(current) | set(adding)), run), run)
    return {"before": sum(before.values()), "after": sum(after.values()), "new": sorted(set(after) - set(before))}


def index(folder, run=_run):
    """Write the folder's `Packages` file (what `dpkg-scanpackages` makes), so apt can read it as a source."""
    stanzas = []
    for deb in sorted(Path(folder).glob("*.deb")):
        control = run(["dpkg-deb", "-f", str(deb)]).stdout.strip()
        if not control:
            continue
        data = deb.read_bytes()
        stanzas.append(f"{control}\nFilename: ./{deb.name}\nSize: {len(data)}\n"
                       f"MD5sum: {hashlib.md5(data).hexdigest()}\nSHA256: {hashlib.sha256(data).hexdigest()}\n")
    (Path(folder) / "Packages").write_text("\n".join(stanzas))
    return len(stanzas)


def download_into(folder, names, run=_run):
    """Fetch the packages and their dependencies into folder, keep one version of each (the new one) and write the
    index. The old files stay until apt has downloaded every new one. Returns (kept {package: path}, added count)."""
    want = closure(names, run)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    have = {p.name for p in folder.glob("*.deb")}
    added = 0
    with tempfile.TemporaryDirectory() as tmp:
        r = run(["apt-get", "download", *want], cwd=tmp)
        if r.returncode:
            raise RuntimeError((r.stderr or "apt-get download failed").strip()[-300:])
        fresh = set()
        for deb in Path(tmp).glob("*.deb"):
            if deb.name not in have:
                added += 1
            fresh.add(deb.name)
            shutil.move(str(deb), folder / deb.name)
            os.chmod(folder / deb.name, 0o644)
    keep = {}
    for deb in sorted(folder.glob("*.deb")):   # one version of each package: the one just downloaded
        pkg = deb.name.split("_", 1)[0]
        if deb.name not in fresh:
            deb.unlink()
            continue
        keep[pkg] = deb
    index(folder, run)
    return keep, added


def cache(depot, names=OPTIONAL, run=_run, log=print):
    """Download the packages into the store and record them. Returns {"system", "files", "added", "missing"}.
    Names apt doesn't know (a typo, or not on this system) are skipped and listed in "missing"."""
    sysid = system_id(run)
    if not sysid:
        raise RuntimeError("can't tell which system this is")
    names = list(dict.fromkeys(names))
    ok = known(names, run)
    if not ok:
        raise RuntimeError("apt doesn't know any of these packages")
    missing = [n for n in names if n not in ok]
    folder = depot.root / "packages" / sysid
    keep, added = download_into(folder, ok, run)
    recorded = set()
    for f in sorted(folder.iterdir()):
        row = depot.add_file("package", f"{sysid}/{f.name.split('_', 1)[0]}", f, version=f.name, subdir=sysid)
        recorded.add(row["path"])
    for row in depot.rows():   # forget files that were replaced
        if row["kind"] == "package" and row["path"].startswith(f"packages/{sysid}/") and row["path"] not in recorded:
            depot.remove(row)
    log(f"Packages for {sysid}: {len(keep)} kept, {added} new" + (f"; not found: {', '.join(missing)}" if missing else ""))
    return {"system": sysid, "files": len(keep), "added": added, "missing": missing}
