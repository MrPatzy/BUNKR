"""Build the BUNKR .deb from a source tree (normally the public copy made by tools/publish_public.py).

    python3 tools/build_deb.py TREE [--out DIR] [--maintainer "Name <address>"]

Writes DIR/bunkr_<version>_all.deb and DIR/SHA256SUMS (the version comes from TREE/bunkr/__init__.py). Sign the
checksums afterwards with tools/release_sign.py sign DIR/SHA256SUMS, and attach all three files to the GitHub release.

What the package holds:
- the app in /usr/lib/bunkr (not /opt/bunkr: Debian packages stay out of /opt, and the home hub is a git checkout
  in /opt/bunkr; the package's preinst also refuses a machine whose BUNKR is a git checkout or from install.sh);
- bunkr.service (options in the conffile /etc/default/bunkr), bunkr-upgrade.path and bunkr-upgrade.service;
- /usr/bin/bunkr (the command line) and /usr/lib/bunkr/bunkr-upgrade (the root side of the Upgrade button).
postinst makes the bunkr system user and /var/lib/bunkr; remove keeps the data, purge deletes it.

Needs only the standard library and dpkg-deb. The build is reproducible: file times are set to SOURCE_DATE_EPOCH
(default: the newest file in the tree), owners are root, and modes are normalised.
"""

import argparse
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEPENDS = "python3 (>= 3.11), python3-cryptography, 7zip | p7zip-full, rsync"
SUGGESTS = "tesseract-ocr, kiwix-tools"
UNITS = {"deploy/deb/bunkr.service": "bunkr.service", "deploy/bunkr-upgrade.path": "bunkr-upgrade.path",
         "deploy/bunkr-upgrade.service": "bunkr-upgrade.service"}
SCRIPTS = ("preinst", "postinst", "prerm", "postrm")
SKIP = re.compile(r"(^|/)(__pycache__|\.git)(/|$)|\.py[co]$|~$")
DESCRIPTION = """family preparedness hub
 BUNKR keeps a household's supplies, plans, documents and check-ins in one
 place. It works offline, runs on a home hub, a Raspberry Pi or an old laptop,
 and keeps field nodes in sync with the hub. It is a small Python web app with
 an SQLite database and no other services.
 .
 This package installs the app in /usr/lib/bunkr, the bunkr service and the
 signed-upgrade service. Data lives in /var/lib/bunkr and is kept on removal."""


def tree_version(tree):
    m = re.search(r'^__version__ = "([^"]+)"', (tree / "bunkr/__init__.py").read_text(), re.M)
    if not m or not re.fullmatch(r"\d+\.\d+\.\d+", m.group(1)):
        raise SystemExit("bunkr/__init__.py has no MAJOR.MINOR.PATCH __version__")
    return m.group(1)


def tree_repo(tree):
    m = re.search(r'^REPO = "([^"]*)"', (tree / "bunkr/selfupdate.py").read_text(), re.M)
    return m.group(1) if m else ""


def _put(src, dest, mode):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    os.chmod(dest, mode)


def _gz(src_bytes, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as f:
        f.write(src_bytes)
    os.chmod(dest, 0o644)


def stage(tree, root, ver, maintainer):
    """Lay out the package in root (a scratch folder)."""
    lib = root / "usr/lib/bunkr"
    for p in sorted((tree / "bunkr").rglob("*")):
        rel = p.relative_to(tree).as_posix()
        if p.is_file() and not p.is_symlink() and not SKIP.search(rel):
            _put(p, lib / rel, 0o644)
    _put(tree / "deploy/deb/bunkr-upgrade", lib / "bunkr-upgrade", 0o755)
    _put(tree / "deploy/deb/bunkr", root / "usr/bin/bunkr", 0o755)
    for src, name in UNITS.items():
        _put(tree / src, root / "usr/lib/systemd/system" / name, 0o644)
    _put(tree / "deploy/deb/default", root / "etc/default/bunkr", 0o644)
    doc = root / "usr/share/doc/bunkr"
    lic = (tree / "LICENSE").read_text() if (tree / "LICENSE").exists() else "GNU Affero General Public License, version 3.\n"
    doc.mkdir(parents=True, exist_ok=True)
    (doc / "copyright").write_text(
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        "Upstream-Name: BUNKR\n"
        f"Source: https://github.com/{tree_repo(tree)}\n\n"
        "Files: *\nCopyright: BUNKR contributors\nLicense: AGPL-3.0-or-later\n\n"
        "License: AGPL-3.0-or-later\n" + "".join(f" {line}\n" if line.strip() else " .\n" for line in lic.splitlines()))
    os.chmod(doc / "copyright", 0o644)
    if (tree / "CHANGELOG.md").exists():
        _gz((tree / "CHANGELOG.md").read_bytes(), doc / "changelog.gz")
    if (tree / "README.md").exists():
        _gz((tree / "README.md").read_bytes(), doc / "README.md.gz")
    if (tree / "deploy/INSTALL.md").exists():
        _gz((tree / "deploy/INSTALL.md").read_bytes(), doc / "INSTALL.md.gz")

    files = sorted(p for p in root.rglob("*") if p.is_file())
    size_kb = sum((p.stat().st_size + 1023) // 1024 for p in files) + len({p.parent for p in files})
    debian = root / "DEBIAN"
    debian.mkdir()
    repo = tree_repo(tree)
    control = [f"Package: bunkr", f"Version: {ver}", "Architecture: all", f"Maintainer: {maintainer}",
               f"Installed-Size: {size_kb}", f"Depends: {DEPENDS}", f"Suggests: {SUGGESTS}", "Section: misc",
               "Priority: optional"]
    if repo:
        control.append(f"Homepage: https://github.com/{repo}")
    control.append(f"Description: {DESCRIPTION}")
    (debian / "control").write_text("\n".join(control) + "\n")
    (debian / "conffiles").write_text("/etc/default/bunkr\n")
    (debian / "md5sums").write_text("".join(
        f"{hashlib.md5(p.read_bytes()).hexdigest()}  {p.relative_to(root).as_posix()}\n"
        for p in files if not p.relative_to(root).as_posix().startswith("etc/")))
    for f in ("control", "conffiles", "md5sums"):
        os.chmod(debian / f, 0o644)
    for s in SCRIPTS:
        _put(tree / "deploy/deb" / s, debian / s, 0o755)
    for d in [root, *(p for p in root.rglob("*") if p.is_dir())]:
        os.chmod(d, 0o755)


def build(tree, out, maintainer=None, epoch=None):
    """Build the .deb and SHA256SUMS into out. Returns the .deb's path."""
    tree, out = Path(tree).resolve(), Path(out).resolve()
    if not shutil.which("dpkg-deb"):
        raise SystemExit("dpkg-deb is needed (apt install dpkg)")
    ver = tree_version(tree)
    repo = tree_repo(tree)
    maintainer = maintainer or f"BUNKR project <{(repo.split('/')[0] or 'bunkr')}@users.noreply.github.com>"
    if not re.fullmatch(r"[^<>\n]+ <[^<>@\s]+@[^<>@\s]+>", maintainer):
        raise SystemExit("--maintainer is Name <address>")
    out.mkdir(parents=True, exist_ok=True)
    deb = out / f"bunkr_{ver}_all.deb"
    with tempfile.TemporaryDirectory(prefix="bunkr-deb-") as t:
        root = Path(t) / "root"
        root.mkdir()
        stage(tree, root, ver, maintainer)
        if epoch is None:
            epoch = int(os.environ.get("SOURCE_DATE_EPOCH") or max(
                p.stat().st_mtime for p in (tree / "bunkr").rglob("*") if p.is_file() and not SKIP.search(p.as_posix())))
        for p in root.rglob("*"):
            os.utime(p, (epoch, epoch), follow_symlinks=False)
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch))
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(root), str(deb)],
                       check=True, env=env, stdout=subprocess.DEVNULL)
    digest = hashlib.sha256(deb.read_bytes()).hexdigest()
    (out / "SHA256SUMS").write_text(f"{digest}  {deb.name}\n")
    return deb


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the BUNKR .deb and SHA256SUMS")
    ap.add_argument("tree", help="the source tree, e.g. the public copy from tools/publish_public.py")
    ap.add_argument("--out", default="dist", help="where to write the .deb and SHA256SUMS (default ./dist)")
    ap.add_argument("--maintainer", help='"Name <address>" (default: the GitHub no-reply address of the repository owner)')
    a = ap.parse_args(argv)
    deb = build(a.tree, a.out, a.maintainer)
    print(f"built {deb}")
    print(f"wrote {deb.parent / 'SHA256SUMS'}; now sign it: python3 tools/release_sign.py sign {deb.parent / 'SHA256SUMS'}")


if __name__ == "__main__":
    sys.exit(main())
