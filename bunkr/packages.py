"""OS packages kept in the store, so a node or a rebuilt machine can install them with no internet.

The hub downloads the Ubuntu or Debian packages BUNKR can use but doesn't ship (today: Tesseract, for reading
nutrition labels) with everything they need beyond a base system, once a week, into
`store/packages/<system>/` (for example `ubuntu-noble-amd64`), with a `Packages` index so apt can use the folder as
a local package source. Like the rest of the store they sync to the field nodes. deploy/install.sh adds a matching
folder as a package source, so an install with no internet still gets them.

Only packages for the hub's own system can be fetched this way (a Pi on Debian for ARM fetches its own when it's online).
"""

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

OPTIONAL = ["tesseract-ocr", "tesseract-ocr-eng"]
SKIP_PRIORITY = {"required", "important"}   # on every system already


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


def closure(names, run=_run):
    """The packages and everything they depend on, leaving out what every system has."""
    r = run(["apt-cache", "depends", "--recurse", "--no-recommends", "--no-suggests", "--no-conflicts", "--no-breaks",
             "--no-replaces", "--no-enhances", *names])
    if r.returncode:
        raise RuntimeError((r.stderr or "apt-cache failed").strip()[:200])
    pkgs = sorted({ln.strip() for ln in r.stdout.splitlines() if ln and not ln.startswith(" ") and "<" not in ln})
    out = []
    for p in pkgs:
        show = run(["apt-cache", "show", "--no-all-versions", p]).stdout
        prio = next((ln.split(":", 1)[1].strip() for ln in show.splitlines() if ln.startswith("Priority:")), "")
        if show and prio not in SKIP_PRIORITY:
            out.append(p)
    return out


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


def cache(depot, names=OPTIONAL, run=_run, log=print):
    """Download the packages into the store and record them. Returns {"system", "files", "added"}."""
    sysid = system_id(run)
    if not sysid:
        raise RuntimeError("can't tell which system this is")
    want = closure(names, run)
    folder = depot.root / "packages" / sysid
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
    keep = {}
    for deb in sorted(folder.glob("*.deb")):   # one version of each package: the one just downloaded
        pkg = deb.name.split("_", 1)[0]
        if deb.name not in fresh:
            deb.unlink()
            continue
        keep[pkg] = deb
    index(folder, run)
    recorded = set()
    for f in sorted(folder.iterdir()):
        row = depot.add_file("package", f"{sysid}/{f.name.split('_', 1)[0]}", f, version=f.name, subdir=sysid)
        recorded.add(row["path"])
    for row in depot.rows():   # forget files that were replaced
        if row["kind"] == "package" and row["path"].startswith(f"packages/{sysid}/") and row["path"] not in recorded:
            depot.remove(row)
    log(f"Packages for {sysid}: {len(keep)} kept, {added} new")
    return {"system": sysid, "files": len(keep), "added": added}
