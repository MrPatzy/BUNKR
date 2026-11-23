"""The offline library reader: runs kiwix-serve and searches it.

Every node serves its ZIM archives with kiwix-serve at http://<node>:8080/kiwix/. On a Pi or laptop the installer
runs it as bunkr-kiwix.service. On the hub BUNKR starts it itself and rebuilds its list of books whenever a download
finishes, so there is no extra service to install. BUNKR searches every book at once for the search box on Home.

Why a port of its own and not a page inside BUNKR: books are web pages with their own scripts (the water, food and
medicine libraries are small apps; archived websites bring the site's scripts). On BUNKR's own address those
scripts could act as whoever is logged in. Sealing them off (a sandbox) breaks the apps, which need browser
storage. A different port is a different origin to the browser, so books work and can't touch BUNKR.
"""

import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path

ROOT = "/kiwix"           # kiwix-serve --urlRootLocation
PORT = 8080
URL = f"http://127.0.0.1:{PORT}"


def zim_files(lib_dir):
    """The store's books: library/*.zim, then books in offline packs (packs/<pack>/*.zim) not already in the library."""
    lib_dir = Path(lib_dir)
    own = sorted(lib_dir.glob("*.zim")) if lib_dir.is_dir() else []
    names = {z.name for z in own}
    packs = lib_dir.parent / "packs"
    extra = [z for z in sorted(packs.glob("*/*.zim")) if not z.parent.name.startswith(".")] if packs.is_dir() else []
    for z in extra:
        if z.name not in names:
            names.add(z.name)
            own.append(z)
    return own


def build_library(lib_dir, state_dir, manage="kiwix-manage"):
    """library.xml from every readable *.zim. Returns (count, skipped names). Written next to the old one, then swapped."""
    lib_dir, state_dir = Path(lib_dir), Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    new = state_dir / "library.xml.new"
    new.unlink(missing_ok=True)
    count, skipped = 0, []
    for zim in zim_files(lib_dir):
        r = subprocess.run([manage, str(new), "add", str(zim)], capture_output=True, timeout=120)
        if r.returncode == 0:
            count += 1
        else:  # still copying, or damaged
            skipped.append(zim.name)
    if count:
        new.replace(state_dir / "library.xml")
    else:
        new.unlink(missing_ok=True)
        (state_dir / "library.xml").unlink(missing_ok=True)
    return count, skipped


class Manager:
    """kiwix-serve as a child of BUNKR (the hub). Restarted when the set of books changes."""

    def __init__(self, lib_dir, state_dir, port=PORT, serve="kiwix-serve", manage="kiwix-manage", bind="0.0.0.0"):
        self.lib_dir, self.state_dir, self.port, self.bind = Path(lib_dir), Path(state_dir), port, bind
        self.serve, self.manage = serve, manage
        self.proc = None
        self.books = 0
        self.skipped = []
        self.error = ""
        self.lock = threading.Lock()

    @staticmethod
    def available(serve="kiwix-serve", manage="kiwix-manage"):
        return bool(shutil.which(serve) and shutil.which(manage))

    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def refresh(self):
        """Rebuild the book list and (re)start kiwix-serve. Safe to call after every download."""
        with self.lock:
            try:
                self.books, self.skipped = build_library(self.lib_dir, self.state_dir, self.manage)
            except (OSError, subprocess.SubprocessError) as exc:
                self.error = f"couldn't list the books ({exc})"
                return False
            self._stop()
            if not self.books:
                self.error = ""
                return False
            log = self.state_dir / "kiwix-serve.log"
            try:
                with open(log, "wb") as out:
                    self.proc = subprocess.Popen(
                        [self.serve, "--library", "--monitorLibrary", "--address", self.bind, "--port", str(self.port),
                         "--urlRootLocation", ROOT, str(self.state_dir / "library.xml")],
                        stdout=out, stderr=subprocess.STDOUT)
            except OSError as exc:
                self.error = f"couldn't start kiwix-serve ({exc})"
                return False
            for _ in range(50):  # up to 5 s for it to answer
                if self.proc.poll() is not None:
                    self.error = log.read_text("utf-8", "replace").strip()[-300:] or "kiwix-serve stopped"
                    return False
                if alive(f"http://127.0.0.1:{self.port}"):
                    self.error = ""
                    return True
                time.sleep(0.1)
            self.error = "kiwix-serve didn't answer"
            return False

    def _stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def stop(self):
        with self.lock:
            self._stop()

    def status(self):
        return {"managed": True, "running": self.running(), "books": self.books, "skipped": self.skipped, "error": self.error}


def alive(base=URL, timeout=2):
    try:
        with urllib.request.urlopen(f"{base}{ROOT}/catalog/v2/entries?count=1", timeout=timeout) as r:
            return r.status == 200
    except (OSError, urllib.error.URLError):
        return False


def books(base=URL, timeout=5):
    """What the running reader has: [{name, title, path}]."""
    ns = {"a": "http://www.w3.org/2005/Atom"}
    with urllib.request.urlopen(f"{base}{ROOT}/catalog/v2/entries?count=500", timeout=timeout) as r:
        root = ET.fromstring(r.read())
    out = []
    for e in root.findall("a:entry", ns):
        link = next((l.get("href") for l in e.findall("a:link", ns) if l.get("type") == "text/html"), "")
        out.append({"name": e.findtext("a:name", "", ns), "title": e.findtext("a:title", "", ns), "path": link})
    return out


def _text(s):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def search(q, base=URL, limit=20, timeout=10):
    """Full-text search across every book: [{title, path, book, snippet}]. Books in other languages are skipped
    if kiwix-serve refuses to mix them."""
    q = (q or "").strip()[:200]
    if not q:
        return []
    params = {"pattern": q, "format": "xml", "pageLength": max(1, min(int(limit), 50))}
    last = None
    for extra in ({}, {"books.filter.lang": "eng"}):
        url = f"{base}{ROOT}/search?" + urllib.parse.urlencode(params | extra)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                root = ET.fromstring(r.read())
            break
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code != 400:
                raise
    else:
        raise last
    out = []
    for it in root.iter("item"):
        if not (it.findtext("link", "") or "").startswith(ROOT + "/"):
            continue
        d = it.find("description")
        snippet = _text("".join(d.itertext()) if d is not None else "")
        snippet = re.sub(r"^(Search × )?(Loading… )*(No result for this search request\. )?(Loading… )*(⇡ )?", "", snippet)
        out.append({"title": _text(it.findtext("title", "")), "path": it.findtext("link", ""),
                    "book": _text(it.findtext("book/title", "")), "snippet": snippet[:300]})
    return out
