"""Curated offline content (Kiwix ZIM archives) and portable tools for the USB kit.

Sizes and download URLs are looked up live from the Kiwix OPDS catalog, since
archives are republished with new dates and sizes every few months.
"""

import hashlib
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

CATALOG = "https://opds.library.kiwix.org/catalog/v2/entries"
USER_AGENT = "BUNKR/0.1 (offline preparedness kit builder)"

# Tier 1 is copied first. Items sharing a "group" are alternatives, best first:
# the planner takes the first one that fits and skips the rest.
CURATED = [
    {"id": "water", "name": "zimgit-water_en", "flavour": "", "tier": 1, "approx": 20e6,
     "why": "Water treatment and purification library"},
    {"id": "food_prep", "name": "zimgit-food-preparation_en", "flavour": "", "tier": 1, "approx": 100e6,
     "why": "Food preparation and preservation library"},
    {"id": "post_disaster", "name": "zimgit-post-disaster_en", "flavour": "", "tier": 1, "approx": 640e6,
     "why": "Post-disaster resource library"},
    {"id": "medical_lib", "name": "zimgit-medicine_en", "flavour": "", "tier": 1, "approx": 70e6,
     "why": "Medical reference library"},
    {"id": "mil_med", "name": "irp.fas.org_en_military-medicine", "flavour": "", "tier": 1, "approx": 60e6,
     "why": "Military field medicine manuals"},
    {"id": "nhs_meds", "name": "nhs.uk_en_medicines", "flavour": "", "tier": 1, "approx": 20e6,
     "why": "NHS medicines A to Z (doses, side effects)"},
    {"id": "readygov", "name": "ready.gov_en_all", "flavour": "", "tier": 1, "approx": 2.06e9,
     "why": "Ready.gov disaster guides"},
    {"id": "wiki_med", "name": "wikipedia_en_medicine", "flavour": "maxi", "group": "wiki_med", "tier": 2, "approx": 2e9,
     "why": "Wikipedia medicine articles with pictures"},
    {"id": "wiki_med_nopic", "name": "wikipedia_en_medicine", "flavour": "nopic", "group": "wiki_med", "tier": 2, "approx": 1e9,
     "why": "Wikipedia medicine articles, text only"},
    {"id": "selfreliance", "name": "lrnselfreliance_en_all", "flavour": "", "tier": 2, "approx": 3.97e9,
     "why": "Learning Self-Reliance (homesteading, skills)"},
    {"id": "ifixit", "name": "ifixit_en_all", "flavour": "", "tier": 2, "approx": 3.57e9,
     "why": "iFixit repair guides"},
    {"id": "wikivoyage", "name": "wikivoyage_en_all", "flavour": "nopic", "tier": 2, "approx": 270e6,
     "why": "Wikivoyage travel and regional info"},
    {"id": "wikivet", "name": "wikivet.net_en_all", "flavour": "", "tier": 2, "approx": 60e6,
     "why": "Veterinary reference (pets and livestock)"},
    {"id": "se_outdoors", "name": "outdoors.stackexchange.com_en_all", "flavour": "", "tier": 2, "approx": 140e6,
     "why": "Questions and answers: camping, hiking, staying safe outdoors"},
    {"id": "se_cooking", "name": "cooking.stackexchange.com_en_all", "flavour": "", "tier": 2, "approx": 240e6,
     "why": "Questions and answers: cooking, food safety, storing food"},
    {"id": "se_ham", "name": "ham.stackexchange.com_en_all", "flavour": "", "tier": 2, "approx": 80e6,
     "why": "Questions and answers: amateur radio"},
    {"id": "se_gardening", "name": "gardening.stackexchange.com_en_all", "flavour": "", "tier": 2, "approx": 930e6,
     "why": "Questions and answers: gardening and growing food"},
    {"id": "appropedia", "name": "appropedia_en_all", "flavour": "maxi", "tier": 2, "approx": 580e6,
     "why": "Appropedia: low-tech solutions (water, power, shelter, sanitation)"},
    {"id": "se_diy", "name": "diy.stackexchange.com_en_all", "flavour": "", "tier": 3, "approx": 2.1e9,
     "why": "Questions and answers: home repair, plumbing, wiring"},
    {"id": "se_electronics", "name": "electronics.stackexchange.com_en_all", "flavour": "", "tier": 3, "approx": 4.3e9,
     "why": "Questions and answers: electronics and batteries"},
    {"id": "wiktionary", "name": "wiktionary_en_all", "flavour": "nopic", "tier": 3, "approx": 14.8e9,
     "why": "English dictionary (Wiktionary), text only"},
    {"id": "wiki_full", "name": "wikipedia_en_all", "flavour": "maxi", "group": "wikipedia", "tier": 3, "approx": 127e9,
     "why": "All of English Wikipedia with pictures"},
    {"id": "wiki_nopic", "name": "wikipedia_en_all", "flavour": "nopic", "group": "wikipedia", "tier": 3, "approx": 53e9,
     "why": "All of English Wikipedia, text only"},
    {"id": "wiki_mini", "name": "wikipedia_en_all", "flavour": "mini", "group": "wikipedia", "tier": 3, "approx": 14e9,
     "why": "English Wikipedia, article intros only"},
    {"id": "wiki_simple", "name": "wikipedia_en-simple_all", "flavour": "nopic", "group": "wikipedia", "tier": 3, "approx": 1.1e9,
     "why": "Simple English Wikipedia (fallback for small drives)"},
]

# Readers so the kit is usable on a machine with nothing installed.
TOOLS = [
    {"id": "kiwix_linux", "file": "kiwix-tools_linux-x86_64.tar.gz", "approx": 40e6,
     "url": "https://download.kiwix.org/release/kiwix-tools/kiwix-tools_linux-x86_64.tar.gz",
     "why": "Kiwix reader for Linux (kiwix-serve, opens in any browser)"},
    {"id": "kiwix_macos", "file": "kiwix-tools_macos-arm64.tar.gz", "approx": 40e6,
     "url": "https://download.kiwix.org/release/kiwix-tools/kiwix-tools_macos-arm64.tar.gz",
     "why": "Kiwix reader for Apple Silicon Macs"},
    {"id": "kiwix_windows", "file": "kiwix-desktop_windows_x64.zip", "approx": 120e6,
     "url": "https://download.kiwix.org/release/kiwix-desktop/kiwix-desktop_windows_x64.zip",
     "why": "Kiwix Desktop for Windows (portable, no install)"},
    {"id": "7zr_windows", "file": "7zr.exe", "approx": 0.6e6,
     "url": "https://www.7-zip.org/a/7zr.exe",
     "why": "7-Zip command-line extractor for Windows (opens the encrypted documents)"},
]

# Books someone added from the full Kiwix catalog. The server points this at the saved list
# (settings "kiwix_extra"); every place that reads the catalog sees them after the curated ones.
extra_source = lambda: []


def entries():
    curated = {c["id"] for c in CURATED}
    return CURATED + [e for e in extra_source() if e.get("id") not in curated]


def listed(name, flavour):
    """Already on the list? A listed entry with no flavour takes whichever the catalog has."""
    return any(e["name"] == name and e.get("flavour", "") in ("", flavour) for e in entries())


def extra_entry(name, flavour, title="", summary="", size=0):
    """A saved catalog pick in the same shape as a curated entry."""
    name = str(name or "").strip()
    flavour = str(flavour or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,120}", name) or not re.fullmatch(r"[a-z_-]{0,20}", flavour):
        raise ValueError("that isn't a Kiwix book name")
    return {"id": "x_" + re.sub(r"[^a-z0-9]+", "_", f"{name}_{flavour}".lower()).strip("_")[:80], "name": name,
            "flavour": flavour, "tier": 3, "approx": max(0, int(size or 0)), "custom": True,
            "why": (str(title or name).strip() + (f": {summary.strip()}" if summary else ""))[:160]}


def search_catalog(q, lang="eng", count=30):
    """Search the whole Kiwix catalog (a few thousand books): newest of each name and flavour, biggest first last."""
    q = str(q or "").strip()[:100]
    if len(q) < 2:
        return []
    params = {"q": q, "count": max(1, min(int(count), 100))}
    if lang:
        params["lang"] = lang
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(_get(f"{CATALOG}?{urllib.parse.urlencode(params)}"))
    best = {}
    for e in root.findall("a:entry", ns):
        link = next((l for l in e.findall("a:link", ns) if l.get("type") == "application/x-zim"), None)
        if link is None:
            continue
        name = e.findtext("a:name", "", ns)
        flavour = e.findtext("a:flavour", "", ns) or _flavour_of(name, link.get("href", ""))
        row = {"name": name, "flavour": flavour, "title": e.findtext("a:title", "", ns),
               "summary": (e.findtext("a:summary", "", ns) or "")[:200], "size": int(link.get("length") or 0),
               "date": e.findtext("a:updated", "", ns)[:10], "category": e.findtext("a:category", "", ns)}
        k = (name, flavour)
        if k not in best or row["date"] > best[k]["date"]:
            best[k] = row
    return list(best.values())


_cache = {}
_cache_lock = threading.Lock()
CACHE_SECONDS = 6 * 3600


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _cached(key, fn):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


def _flavour_of(name, href):
    """Catalog entries don't always fill <flavour>; fall back to the file name."""
    m = re.search(re.escape(name) + r"_(maxi|nopic|mini)_", href)
    return m.group(1) if m else ""


def lookup(entry):
    """Return {url, size, date, title} for the newest catalog match, or None."""
    def fetch():
        query = urllib.parse.urlencode({"name": entry["name"], "count": 20})
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(_get(f"{CATALOG}?{query}"))
        best = None
        for e in root.findall("a:entry", ns):
            link = next((l for l in e.findall("a:link", ns) if l.get("type") == "application/x-zim"), None)
            if link is None:
                continue
            href = link.get("href", "")
            flavour = (e.findtext("a:flavour", "", ns) or _flavour_of(entry["name"], href))
            if entry["flavour"] and flavour != entry["flavour"]:
                continue
            issued = e.findtext("a:updated", "", ns)
            if best is None or issued > best["date"]:
                best = {"meta4": href, "url": href.removesuffix(".meta4"),
                        "file": href.rsplit("/", 1)[-1].removesuffix(".meta4"),
                        "size": int(link.get("length") or 0), "date": issued[:10],
                        "title": e.findtext("a:title", "", ns)}
        return best

    return _cached(("zim", entry["id"]), fetch)


def tool_size(tool):
    def fetch():
        req = urllib.request.Request(tool["url"], method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return int(resp.headers.get("Content-Length") or 0) or int(tool["approx"])
    return _cached(("tool", tool["id"]), fetch)


def resolve_all(disabled=()):
    """Curated list with live sizes. Falls back to approximate sizes when offline."""
    out = []
    for entry in entries():
        item = {**entry, "enabled": entry["id"] not in disabled, "online": False,
                "size": int(entry["approx"]), "file": None, "date": ""}
        try:
            found = lookup(entry)
        except Exception as exc:  # offline or catalog changed: plan with estimates
            item["error"] = str(exc)
            found = None
        if found:
            item.update(size=found["size"], file=found["file"], url=found["url"],
                        meta4=found["meta4"], date=found["date"], online=True)
        elif "error" not in item:
            item["error"] = "not found in the Kiwix catalog"
        out.append(item)
    tools = []
    for tool in TOOLS:
        t = dict(tool, size=int(tool["approx"]))
        try:
            t["size"] = tool_size(tool)
        except Exception as exc:
            t["error"] = str(exc)
        tools.append(t)
    return out, tools


def parse_meta4(data):
    """Return (size, sha256, [urls]) from a Metalink 4 file."""
    ns = {"m": "urn:ietf:params:xml:ns:metalink"}
    root = ET.fromstring(data)
    f = root.find("m:file", ns)
    size = int(f.findtext("m:size", "0", ns))
    sha = next((h.text for h in f.findall("m:hash", ns) if h.get("type") == "sha-256"), None)
    urls = [u.text for u in sorted(f.findall("m:url", ns), key=lambda u: int(u.get("priority", "99")))]
    return size, sha, urls


def download(url, dest, expected_size=0, sha256=None, progress=None, cancelled=lambda: False):
    """Resumable download into dest (via dest.part). Raises on mismatch or cancel."""
    part = dest.with_name(dest.name + ".part")
    # Finished files are only renamed into place after verification, so a
    # full-size final file is trusted on later runs.
    if dest.exists() and expected_size and dest.stat().st_size == expected_size:
        return "already present"
    done = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": USER_AGENT}
    if done:
        headers["Range"] = f"bytes={done}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        if done and resp.status != 206:  # server ignored the range: start over
            done = 0
        with open(part, "ab" if done else "wb") as out:
            while True:
                if cancelled():
                    raise InterruptedError("cancelled")
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done)
    if expected_size and done != expected_size:
        raise IOError(f"size mismatch: got {done} bytes, expected {expected_size}")
    if sha256:
        h = hashlib.sha256()
        with open(part, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                if cancelled():
                    raise InterruptedError("cancelled")
                h.update(chunk)
        if h.hexdigest() != sha256:
            part.unlink()
            raise IOError("checksum mismatch, partial file removed")
    part.replace(dest)
    return "downloaded"
