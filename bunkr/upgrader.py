"""Upgrading a .deb install of BUNKR from a signed release, without giving the web service root.

How the pieces fit (see "Upgrading" in deploy/INSTALL.md):
- The web app runs as the unprivileged bunkr user and never installs anything. When an admin presses "Upgrade now"
  it writes a small request (version, expected SHA-256, where from) into SPOOL, a folder the bunkr user owns.
- bunkr-upgrade.path (root) sees the file and starts bunkr-upgrade.service, which runs main() as root. It reads and
  deletes the request, checks every field, fetches the release (from GitHub, or a copy in the store), checks the
  Ed25519 signature on SHA256SUMS with the release key built into the installed BUNKR and the .deb against it, backs
  up the database, installs the .deb, restarts BUNKR and checks it answers. If BUNKR doesn't come back, it reinstalls
  the previous .deb and puts the database backup back.
- Progress goes to STATUS, in a root-owned folder; the app only reads it.

Trust: the public key in the installed (root-owned) copy -> the signature over SHA256SUMS -> the .deb's SHA-256.
What the bunkr user writes (the request, files in the store) can only choose which signed, newer release to install.
"""

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, selfupdate

APP_DIR = Path("/usr/lib/bunkr")               # where the .deb puts the app (a git hub lives in /opt/bunkr instead)
DATA_DIR = Path("/var/lib/bunkr")
DB = DATA_DIR / "bunkr.db"
BACKUPS = DATA_DIR / "db-backups"
SPOOL = DATA_DIR / "upgrade"                   # bunkr-owned: the app writes REQUEST here
REQUEST = SPOOL / "request.json"
STORE_RELEASES = DATA_DIR / "store" / "bunkr-releases"   # signed releases for upgrading with no internet
WORK = Path("/var/lib/bunkr-upgrade")          # root-owned: status, log, kept .debs
STATUS = WORK / "status.json"
KEY_FILE = Path(__file__).with_name("release_key.pub")
DPKG_LIST = Path("/var/lib/dpkg/info/bunkr.list")
DEPLOY_TIMER = Path("/etc/systemd/system/bunkr-deploy.timer")
SUMS, SIG = "SHA256SUMS", "SHA256SUMS.sig"
MAX_REQUEST = 4096
MAX_SMALL = 64 * 1024
MAX_DEB = 200 * 1024 * 1024
KEEP_DEBS = 3
KEEP_BACKUPS = 10
FIELDS = {"version", "sha256", "source", "repo", "confirm_major", "requested_by", "requested_at"}
STATES = {"running", "done", "rolled_back", "failed", "refused"}
VERSION_RE = re.compile(r"(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,100}")
WHEN_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+Z-]{2,30}")
USER_AGENT = f"BUNKR/{__version__} (self-hosted; upgrade)"
BACKUP_PY = ("import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); "
             "d.close(); s.close()")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- versions

def version(v):
    """A release version is plain MAJOR.MINOR.PATCH ("1.2.0"): (1, 2, 0). Anything else is None."""
    m = VERSION_RE.fullmatch(v) if isinstance(v, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def is_major(target, current=None):
    a, b = selfupdate.parse(target), selfupdate.parse(current or __version__)
    return bool(a and b and a[0] > b[0])


def deb_name(v):
    return f"bunkr_{v}_all.deb"


def allowed_repo():
    """The only GitHub repository upgrades come from: the project's own, or BUNKR_UPGRADE_REPO from the root-owned
    /etc/default/bunkr (systemd passes it to both services)."""
    repo = (os.environ.get("BUNKR_UPGRADE_REPO") or "").strip() or selfupdate.REPO
    return repo if REPO_RE.fullmatch(repo or "") else ""


# ---------------------------------------------------------------- signatures and checksums

def load_key(path=None):
    """The release public key: one line of base64 (32 raw bytes); lines starting with # are comments. None if absent."""
    try:
        text = Path(path or KEY_FILE).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    lines = [s.strip() for s in text.splitlines() if s.strip() and not s.strip().startswith("#")]
    if len(lines) != 1:
        return None
    try:
        raw = base64.b64decode(lines[0], validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == 32 else None


def verify_signature(data, sig, key):
    """Does sig (base64 text of an Ed25519 signature) sign data with key (32 raw bytes)?"""
    if not key or not isinstance(data, bytes):
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False
    try:
        raw = base64.b64decode(sig.strip() if isinstance(sig, (bytes, str)) else b"", validate=True)
        Ed25519PublicKey.from_public_bytes(key).verify(raw, data)
        return True
    except (InvalidSignature, binascii.Error, ValueError, TypeError):
        return False


def parse_sums(text):
    """SHA256SUMS ("<64 hex>  <name>" lines, as sha256sum writes them) -> {name: sha256}."""
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([0-9a-f]{64}) [ *]([A-Za-z0-9._+~-]{1,200})", line.strip())
        if m:
            out[m.group(2)] = m.group(1)
    return out


def signed_sums(sums, sig, key):
    """{name: sha256} from SHA256SUMS bytes whose signature checks out; ValueError otherwise."""
    if not key:
        raise ValueError("this BUNKR has no release key, so it can't check upgrades")
    if not verify_signature(sums, sig, key):
        raise ValueError("the release signature doesn't check out")
    return parse_sums(sums.decode("utf-8", "replace"))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_release(folder, ver, expected, key):
    """Check SHA256SUMS(.sig) and the .deb for ver in folder. Returns the .deb's path; ValueError if anything is off."""
    folder = Path(folder)
    try:
        sums, sig = (folder / SUMS).read_bytes(), (folder / SIG).read_bytes()
    except OSError:
        raise ValueError("the release has no signed checksums")
    want = signed_sums(sums, sig, key).get(deb_name(ver))
    if not want:
        raise ValueError(f"the signed checksums don't list {deb_name(ver)}")
    if expected and want != expected:
        raise ValueError("the signed checksum isn't the one the upgrade was asked for")
    deb = folder / deb_name(ver)
    if not deb.is_file() or sha256_file(deb) != want:
        raise ValueError("the .deb doesn't match its signed checksum")
    return deb


# ---------------------------------------------------------------- which kind of install this is

def install_kind(app_root=None, app_dir=None, dpkg_list=None, deploy_timer=None):
    """"git" (a checkout updated by bunkr-deploy.timer, like the home hub), "deb" (installed from the .deb; the
    Upgrade button works) or "manual" (copied in by deploy/install.sh or by hand; upgrade with the installer)."""
    root = Path(app_root or Path(__file__).resolve().parents[1]).resolve()
    if (root / ".git").exists() or Path(deploy_timer or DEPLOY_TIMER).exists():
        return "git"
    if root == Path(app_dir or APP_DIR) and Path(dpkg_list or DPKG_LIST).exists():
        return "deb"
    return "manual"


# ---------------------------------------------------------------- the request (written by the app, checked by root)

def validate_request(raw, current=None, repo=None):
    """Check a request file's bytes field by field. Returns the request as a dict; ValueError says what's wrong."""
    current = current or __version__
    if not isinstance(raw, bytes) or len(raw) > MAX_REQUEST:
        raise ValueError("the request is too big")
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("the request isn't readable")
    if not isinstance(d, dict) or set(d) != FIELDS:
        raise ValueError("the request has the wrong fields")
    v = d["version"]
    if not version(v):
        raise ValueError("the request's version isn't a release number")
    if not selfupdate.newer(v, current):
        raise ValueError(f"{v} isn't newer than {current}")
    if not isinstance(d["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", d["sha256"]):
        raise ValueError("the request's checksum isn't a SHA-256")
    if d["source"] not in ("github", "store"):
        raise ValueError("the request's source is neither github nor store")
    allowed = repo if repo is not None else allowed_repo()
    if not isinstance(d["repo"], str) or (d["repo"] and not REPO_RE.fullmatch(d["repo"])):
        raise ValueError("the request's repository isn't owner/name")
    if d["source"] == "github" and (not allowed or d["repo"] != allowed):
        raise ValueError("upgrades only come from the configured repository")
    if not isinstance(d["confirm_major"], bool):
        raise ValueError("confirm_major is true or false")
    if is_major(v, current) and not d["confirm_major"]:
        raise ValueError(f"{v} is a new major version and needs an extra confirm")
    who = d["requested_by"]
    if not isinstance(who, str) or not 0 < len(who) <= 64 or not who.isprintable() or any(c.isspace() for c in who):
        raise ValueError("the request doesn't say who asked")
    if not isinstance(d["requested_at"], str) or not WHEN_RE.fullmatch(d["requested_at"]):
        raise ValueError("the request's time isn't a date")
    return d


def make_request(ver, sha256, source, user, confirm_major=False, current=None, repo=None):
    """The bytes of a request file; checked the same way the root side checks them."""
    repo = repo if repo is not None else allowed_repo()
    raw = json.dumps({"version": ver, "sha256": sha256, "source": source, "repo": repo if source == "github" else "",
                      "confirm_major": bool(confirm_major), "requested_by": str(user), "requested_at": _now()}).encode()
    validate_request(raw, current, repo)
    return raw


def write_request(raw, spool=None):
    """Put a request in the spool (app side, as the bunkr user). One at a time."""
    spool = Path(spool or SPOOL)
    if not spool.is_dir():
        raise ValueError("this install has no upgrade folder; install BUNKR from the .deb to upgrade from here")
    target = spool / REQUEST.name
    if target.exists():
        raise ValueError("an upgrade is already waiting to start")
    tmp = spool / f".request-{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    os.replace(tmp, target)


def take_request(path=None):
    """Root side: read and remove the request without following links. None if there's no usable file."""
    path = Path(path or REQUEST)
    try:
        if stat.S_ISLNK(os.lstat(path.parent).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        _unlink(path)                       # a link or something odd: remove it so the path unit doesn't loop
        return None
    try:
        st = os.fstat(fd)
        data = os.read(fd, MAX_REQUEST + 1) if stat.S_ISREG(st.st_mode) else None
    finally:
        os.close(fd)
        _unlink(path)
    return data


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------- status (written by root, shown by the app)

def write_status(state, ver, message, path=None, **extra):
    path = Path(path or STATUS)
    d = {"state": state, "version": ver, "from": __version__, "message": message, "at": _now(), **extra}
    tmp = path.with_name(f".status-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return d


def read_status(path=None):
    try:
        d = json.loads(Path(path or STATUS).read_text()[:20000])
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict) or d.get("state") not in STATES:
        return {}
    return {k: str(d.get(k) or "")[:500] for k in ("state", "version", "from", "message", "at")}


# ---------------------------------------------------------------- what the app offers

def store_offer(folder=None, key=None, current=None):
    """The newest signed release in the store that is newer than this one: {"version", "sha256"} or None."""
    folder = Path(folder or STORE_RELEASES)
    try:
        sums = signed_sums((folder / SUMS).read_bytes()[:MAX_SMALL], (folder / SIG).read_bytes()[:MAX_SMALL], key)
    except (OSError, ValueError):
        return None
    best = None
    for name, sha in sums.items():
        m = re.fullmatch(r"bunkr_(.+)_all\.deb", name)
        if not m or not version(m.group(1)) or not selfupdate.newer(m.group(1), current or __version__):
            continue
        if best and not selfupdate.newer(m.group(1), best["version"]):
            continue
        p = folder / name
        if p.is_file() and not p.is_symlink() and sha256_file(p) == sha:
            best = {"version": m.group(1), "sha256": sha}
    return best


def release_deb(release, ver, opener=None, key=None):
    """From a GitHub release (the API's JSON), the signed checksum of its .deb: {"name", "sha256"}, or None with no
    signed .deb attached. Only SHA256SUMS and its signature are downloaded (a few hundred bytes)."""
    assets = {a.get("name"): a.get("browser_download_url") for a in release.get("assets") or [] if isinstance(a, dict)}
    name = deb_name(ver)
    if not (name in assets and str(assets.get(SUMS) or "").startswith("https://") and str(assets.get(SIG) or "").startswith("https://")):
        return None
    sums = _get(assets[SUMS], MAX_SMALL, opener)
    sig = _get(assets[SIG], MAX_SMALL, opener)
    sha = signed_sums(sums, sig, key if key is not None else load_key()).get(name)
    return {"name": name, "sha256": sha} if sha else None


def upgrade_view(latest=None, repo=None, current=None, kind=None, key=None):
    """What the Household card shows about upgrading: the kind of install, the upgrader's status, and the release
    (if any) the Upgrade button would install."""
    current = current or __version__
    kind = kind or install_kind()
    out = {"kind": kind, "status": read_status(), "pending": "", "offer": None, "why": ""}
    try:
        raw = REQUEST.read_bytes()[:MAX_REQUEST]
        out["pending"] = str(json.loads(raw).get("version") or "?")[:20]
        out["pending_since"] = int(time.time() - REQUEST.stat().st_mtime)
    except (OSError, ValueError, AttributeError):
        pass
    if kind == "git":
        out["why"] = "This hub updates from git."
        return out
    if kind != "deb":
        out["why"] = "This BUNKR was installed from a folder, so upgrade it with the installer (steps below)."
        return out
    key = key if key is not None else load_key()
    if not key:
        out["why"] = "This BUNKR has no release key, so it can't check an upgrade. Upgrade by hand."
        return out
    offers = []
    latest = latest or {}
    deb = latest.get("deb") or {}
    if latest.get("version") and selfupdate.newer(latest["version"], current) and deb.get("sha256"):
        if repo and repo != allowed_repo():
            out["why"] = f"The Upgrade button only installs releases from {allowed_repo() or 'the project'}."
        else:
            offers.append({"version": latest["version"], "sha256": deb["sha256"], "source": "github"})
    s = store_offer(key=key, current=current)
    if s:
        offers.append({**s, "source": "store"})    # listed last: on a tie the store copy wins (no download)
    for o in offers:
        if not out["offer"] or not selfupdate.newer(out["offer"]["version"], o["version"]):
            out["offer"] = o
    if out["offer"]:
        out["offer"]["major"] = is_major(out["offer"]["version"], current)
    elif latest.get("version") and selfupdate.newer(latest["version"], current) and not out["why"]:
        out["why"] = (f"Can't upgrade from here: {latest['deb_error']}." if latest.get("deb_error")
                      else "This release has no signed .deb, so upgrade by hand.")
    return out


def request_upgrade(view, ver, user, confirm_major=False):
    """App side: the admin pressed Upgrade now for ver. view is upgrade_view(). Writes the request."""
    o = view.get("offer")
    if view.get("kind") != "deb":
        raise ValueError(view.get("why") or "this install can't upgrade itself")
    st = view.get("status") or {}
    if view.get("pending") or (st.get("state") == "running" and _age(st.get("at")) < 3600):
        raise ValueError("an upgrade is already under way")
    if not o or o["version"] != ver:
        raise ValueError("that version isn't on offer")
    if o.get("major") and not confirm_major:
        raise ValueError(f"{ver} is a new major version: confirm it first")
    write_request(make_request(ver, o["sha256"], o["source"], user, confirm_major))


def _age(iso):
    """Seconds since an ISO time; very large if it can't be read."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


# ---------------------------------------------------------------- the root side

def _get(url, limit, opener=None, dest=None):
    """Download url (https only), at most limit bytes, into dest or memory."""
    if not str(url).startswith("https://"):
        raise ValueError("downloads must be https")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with (opener or urllib.request.build_opener()).open(req, timeout=60) as r:
        if dest is None:
            data = r.read(limit + 1)
            if len(data) > limit:
                raise ValueError("a release file is too big")
            return data
        n = 0
        with open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                n += len(chunk)
                if n > limit:
                    raise ValueError("the .deb is too big")
                f.write(chunk)


def _copy_nofollow(src, dest, limit):
    """Copy a store file (bunkr-owned) as root: no links, plain files only, size-limited."""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise ValueError(f"{Path(src).name} in the store isn't a plain file of a sensible size")
        with os.fdopen(os.dup(fd), "rb") as f, open(dest, "wb") as out:
            shutil.copyfileobj(f, out)
    finally:
        os.close(fd)


def decide(installed, healthy):
    """After installing: keep it ("done") only if the install worked and BUNKR answers; otherwise "rollback"."""
    return "done" if installed and healthy else "rollback"


def rollback_plan(fallback, backup):
    """The steps that put the previous version back: stop BUNKR, restore the database copy, reinstall the previous
    .deb, start BUNKR. Steps without what they need are left out."""
    steps = [("stop",)]
    if backup:
        steps.append(("restore_db", backup))
    if fallback:
        steps.append(("install_old", fallback))
    steps.append(("start",))
    return steps


def health_target(args=None):
    """(address, port) BUNKR listens on, from BUNKR_ARGS in /etc/default/bunkr."""
    args = os.environ.get("BUNKR_ARGS", "") if args is None else args
    port = re.search(r"--port[= ](\d{1,5})", args)
    bind = re.search(r"--bind[= ](\S+)", args)
    host = bind.group(1) if bind else "127.0.0.1"
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return host, int(port.group(1)) if port else 8765


class Upgrader:
    """One upgrade run. Commands, downloads and the health check can be swapped out (tests)."""

    def __init__(self, run=None, healthy=None, opener=None, key=None, current=None, work=None, db=None, backups=None,
                 store=None, log=None):
        self.run = run or self._run
        self.healthy = healthy or self._healthy
        self.opener = opener
        self.key = key if key is not None else load_key()
        self.current = current or __version__
        self.work = Path(work or WORK)
        self.db = Path(db or DB)
        self.backups = Path(backups or BACKUPS)
        self.store = Path(store or STORE_RELEASES)
        self.log = log or self._log
        self.debs = self.work / "debs"

    # -- plumbing
    def _log(self, msg):
        line = f"{_now()} {msg}"
        print(line, flush=True)
        try:
            with open(self.work / "upgrade.log", "a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    @staticmethod
    def _run(cmd, **kw):
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        return subprocess.run(cmd, check=True, env=env, capture_output=True, text=True, timeout=1800, **kw)

    def _healthy(self):
        host, port = health_target()
        url = f"http://{'[' + host + ']' if ':' in host else host}:{port}/login.html"
        for _ in range(15):
            time.sleep(2)
            try:
                self.run(["systemctl", "is-active", "-q", "bunkr.service"])
                req = urllib.request.Request(url, headers={"Host": "127.0.0.1", "User-Agent": USER_AGENT})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as r:
                    if r.status == 200:
                        return True
            except Exception:
                continue
        return False

    def status(self, state, ver, message, **extra):
        self.log(f"{state}: {message}")
        return write_status(state, ver, message, self.work / STATUS.name, **extra)

    # -- fetching
    def fetch(self, source, repo, ver, folder):
        """Put SHA256SUMS, its signature and the .deb for ver into folder (not yet checked)."""
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        names = ((SUMS, MAX_SMALL), (SIG, MAX_SMALL), (deb_name(ver), MAX_DEB))
        if source == "store":
            for name, limit in names:
                try:
                    _copy_nofollow(self.store / name, folder / name, limit)
                except OSError:
                    raise ValueError(f"the store has no {name}")
            return
        rel = json.loads(_get(f"https://api.github.com/repos/{repo}/releases/tags/v{ver}", 2_000_000, self.opener))
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            raise ValueError(f"there's no published release v{ver}")
        assets = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets") or [] if isinstance(a, dict)}
        for name, limit in names:
            if not assets.get(name):
                raise ValueError(f"release v{ver} has no {name}")
            _get(assets[name], limit, self.opener, dest=folder / name)

    def check_control(self, deb, ver):
        out = self.run(["dpkg-deb", "-f", str(deb), "Package", "Version"]).stdout or ""
        fields = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
        if fields.get("Package") != "bunkr" or fields.get("Version") != ver:
            raise ValueError("the .deb isn't the BUNKR version it should be")

    def fallback(self, repo):
        """The current version's .deb, to go back to. Kept from the last upgrade, or fetched and checked now."""
        kept = self.debs / deb_name(self.current)
        if kept.is_file():                  # root-only folder: kept by an earlier upgrade, or put there by an admin
            try:
                self.check_control(kept, self.current)
                return kept
            except Exception as exc:
                self.log(f"ignoring {kept}: {_why(exc)}")
        if not version(self.current):
            return None
        for source in ("store", "github"):
            if source == "github" and not repo:
                continue
            folder = self.work / "previous"
            shutil.rmtree(folder, ignore_errors=True)
            try:
                self.fetch(source, repo, self.current, folder)
                deb = check_release(folder, self.current, None, self.key)
            except Exception as exc:
                self.log(f"no previous .deb from {source}: {exc}")
                continue
            self.debs.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.move(str(deb), kept)
            shutil.rmtree(folder, ignore_errors=True)
            return kept
        return None

    def backup_db(self, ver):
        if not self.db.exists():
            return None                     # a locked node keeps its database sealed; nothing plain to copy
        dest = self.backups / f"bunkr-before-upgrade-{self.current}-to-{ver}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
        self.run(["runuser", "-u", "bunkr", "--", "/usr/bin/python3", "-I", "-c", BACKUP_PY, str(self.db), str(dest)])
        olds = sorted(self.backups.glob("bunkr-before-upgrade-*.db"), key=lambda p: p.stat().st_mtime)
        for p in olds[:-KEEP_BACKUPS]:
            _unlink(p)
        return dest

    def keep(self, deb):
        self.debs.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(deb, self.debs / deb.name)
        olds = sorted(self.debs.glob("bunkr_*_all.deb"), key=lambda p: p.stat().st_mtime)
        for p in olds[:-KEEP_DEBS]:
            _unlink(p)

    def step(self, s):
        if s[0] == "stop":
            self.run(["systemctl", "stop", "bunkr.service"])
        elif s[0] == "restore_db":
            self.run(["install", "-o", "bunkr", "-g", "bunkr", "-m", "640", str(s[1]), str(self.db)])
            for suffix in ("-wal", "-shm"):
                _unlink(f"{self.db}{suffix}")
        elif s[0] == "install_old":
            self.run(["dpkg", "--force-confold", "-i", str(s[1])])
        elif s[0] == "start":
            self.run(["systemctl", "start", "bunkr.service"])

    # -- the whole run
    def process(self, raw):
        repo = allowed_repo()
        try:
            req = validate_request(raw, self.current, repo)
        except ValueError as exc:
            self.status("refused", "", f"Upgrade refused: {exc}.")
            return 1
        ver = req["version"]
        if not self.key:
            self.status("refused", ver, "Upgrade refused: this BUNKR has no release key, so it can't check the release.")
            return 1
        self.status("running", ver, f"Upgrading to {ver}…", requested_by=req["requested_by"])
        incoming = self.work / "incoming"
        shutil.rmtree(incoming, ignore_errors=True)
        try:
            self.fetch(req["source"], repo, ver, incoming)
            deb = check_release(incoming, ver, req["sha256"], self.key)
            self.check_control(deb, ver)
        except Exception as exc:
            shutil.rmtree(incoming, ignore_errors=True)
            self.status("failed", ver, f"Not upgraded: {_why(exc)}. Nothing changed.")
            return 1
        fallback = self.fallback(repo)
        if not fallback:
            self.status("failed", ver, f"Not upgraded: there's no signed copy of {self.current} to go back to if it went wrong. "
                                       "Nothing changed.")
            return 1
        try:
            backup = self.backup_db(ver)
        except Exception as exc:
            self.status("failed", ver, f"Not upgraded: the database backup failed ({_why(exc)}). Nothing changed.")
            return 1
        self.log(f"installing {deb.name}; previous {fallback.name}; backup {backup}")
        installed, healthy, reason = False, False, ""
        try:
            self.run(["apt-get", "install", "-y", "-o", "Dpkg::Options::=--force-confold", str(deb)])
            installed = True
            self.run(["systemctl", "restart", "bunkr.service"])
            healthy = self.healthy()
            reason = "" if healthy else "BUNKR didn't answer after the upgrade"
        except Exception as exc:
            reason = f"the install failed ({_why(exc)})" if not installed else f"BUNKR didn't restart ({_why(exc)})"
        if decide(installed, healthy) == "done":
            self.keep(deb)
            shutil.rmtree(incoming, ignore_errors=True)
            self.status("done", ver, f"Upgraded to {ver}.")
            return 0
        self.log(f"rolling back: {reason}")
        try:
            for s in rollback_plan(fallback, backup):
                self.step(s)
            back = self.healthy()
        except Exception as exc:
            self.log(f"rollback step failed: {_why(exc)}")
            back = False
        if back:
            self.status("rolled_back", ver, f"Rolled back to {self.current}: {reason}. The database is as it was before.")
        else:
            self.status("failed", ver, f"Upgrade to {ver} failed ({reason}) and going back didn't bring BUNKR up. "
                                       f"It needs attention: see {self.work / 'upgrade.log'}.")
        return 1


def _why(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        tail = (exc.stderr or exc.stdout or "").strip().splitlines()[-1:] or [""]
        return f"{Path(str(exc.cmd[0])).name} exit {exc.returncode}{': ' + tail[0][:160] if tail[0] else ''}"
    return str(exc)[:200] or type(exc).__name__


def main(argv=None):
    """bunkr-upgrade.service: handle a waiting request. `--status` prints the last status."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--status"]:
        print(json.dumps(read_status(), indent=1))
        return 0
    if os.geteuid() != 0:
        print("bunkr-upgrade runs as root, from bunkr-upgrade.service", file=sys.stderr)
        return 2
    WORK.mkdir(mode=0o755, parents=True, exist_ok=True)
    with open(WORK / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = take_request()
        if raw is None:
            return 0
        up = Upgrader()
        try:
            return up.process(raw)
        except Exception as exc:            # never leave the card saying "Upgrading…" forever
            up.status("failed", "", f"The upgrader stopped: {_why(exc)}. See {WORK / 'upgrade.log'}.")
            return 1
