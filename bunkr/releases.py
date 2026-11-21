"""Mesh firmware and offline apps from GitHub releases, into the downloads store.

Each catalog entry names a repository, how to pick the release (latest stable,
optionally by tag prefix), and which assets to keep. GitHub publishes a SHA-256
digest for every asset; downloads are checked against it. Meshtastic ships one
zip per chip family, so only your boards' files are kept from it.
"""

import io
import json
import re
import urllib.request
import zipfile

from . import library, manifest

API = "https://api.github.com/repos"
USER_AGENT = "BUNKR/0.3 (self-hosted preparedness app)"

# Your boards: MeshCore asset prefix, Meshtastic board name, how to flash.
BOARDS = {
    "heltec_v3": {"label": "Heltec V3 (ESP32-S3)", "meshcore": "Heltec_v3", "meshtastic": "heltec-v3",
                  "platform": "esp32s3", "flash": "esptool"},
    "heltec_t114": {"label": "Heltec T114 (nRF52840)", "meshcore": "Heltec_t114", "meshtastic": "heltec-mesh-node-t114",
                    "platform": "nrf52840", "flash": "uf2"},
    "rak4631": {"label": "RAK4631 (nRF52840)", "meshcore": "RAK_4631", "meshtastic": "rak4631",
                "platform": "nrf52840", "flash": "uf2"},
}
MESHCORE_TYPES = {
    "companion_radio_ble": ("companion", "Companion, Bluetooth (pairs with the phone app)"),
    "companion_radio_usb": ("companion", "Companion, USB (for a computer or the Pi)"),
    "repeater": ("repeater", "Repeater"),
    "room_server": ("room-server", "Room server (message board)"),
}


def _catalog():
    out = []
    for b, info in BOARDS.items():
        for t, (tag, label) in MESHCORE_TYPES.items():
            # V3: the -merged.bin is for a fresh flash at 0x0, the plain .bin updates an existing install.
            # nRF: .uf2 for drag-and-drop, .zip for Bluetooth DFU updates.
            pat = rf"^{info['meshcore']}_{t}-v[\d.]+-\w+(-merged\.bin|\.bin|\.uf2|\.zip)$"
            out.append({"kind": "firmware", "id": f"meshcore-{t}-{b}", "repo": "meshcore-dev/MeshCore",
                        "tag_prefix": f"{tag}-v", "assets": [pat], "board": b,
                        "label": f"MeshCore {label}: {info['label']}"})
        out.append({"kind": "firmware", "id": f"meshtastic-{b}", "repo": "meshtastic/firmware",
                    "assets": [rf"^firmware-{info['platform']}-[\d.]+\w*\.zip$"], "board": b,
                    "zip_members": [rf"(^|/)firmware-{re.escape(info['meshtastic'])}-[\d.]+",
                                    rf"(^|/)littlefs\w*-{re.escape(info['meshtastic'])}-[\d.]+",
                                    r"(^|/)bleota[\w-]*\.bin$" if info["platform"].startswith("esp32") else r"(^|/)\w*[Ee]rase\w*\.uf2$",
                                    r"(^|/)device-(install|update)\.(sh|bat)$"],
                    "label": f"Meshtastic: {info['label']}"})
    out += [
        {"kind": "app", "id": "meshtastic-android", "repo": "meshtastic/Meshtastic-Android",
         "assets": [r"^androidApp-fdroid-universal-release\.apk$"], "label": "Meshtastic for Android (APK)"},
        {"kind": "app", "id": "organic-maps-android", "repo": "organicmaps/organicmaps", "tag_suffix": "-android",
         "assets": [r"^OrganicMaps-\d+-web-release\.apk$"], "label": "Organic Maps for Android (APK)"},
        {"kind": "app", "id": "mesh-client", "repo": "Colorado-Mesh/mesh-client",
         "assets": [r"^mesh-client_[\d.]+_(amd64|arm64)\.deb$"], "label": "Mesh-Client for Linux (Pi and laptop)"},
        {"kind": "app", "id": "pat", "repo": "la5nta/pat",
         "assets": [r"^pat_[\d.]+_linux_(amd64|arm64|armhf)\.deb$"], "label": "Pat Winlink client for Linux"},
    ]
    return out


CATALOG = _catalog()
BY_ID = {c["id"]: c for c in CATALOG}


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def version_of(entry, tag):
    """Version from a tag: companion-v1.18.0 -> 1.18.0, v2.7.26.abc -> 2.7.26.abc."""
    tag = tag[len(entry.get("tag_prefix", "")):] if tag.startswith(entry.get("tag_prefix", "")) else tag
    return tag.removeprefix("v").removesuffix(entry.get("tag_suffix", ""))


def latest(entry, releases_cache=None):
    """Newest stable release matching the entry: {tag, version, published, assets:[{name,url,size,sha256}]}."""
    key = entry["repo"]
    rels = (releases_cache or {}).get(key)
    if rels is None:
        rels = _get(f"{API}/{entry['repo']}/releases?per_page=40")
        if releases_cache is not None:
            releases_cache[key] = rels
    for rel in rels:
        tag = rel["tag_name"]
        if rel.get("prerelease") or rel.get("draft"):
            continue
        if entry.get("tag_prefix") and not tag.startswith(entry["tag_prefix"]):
            continue
        if entry.get("tag_suffix") and not tag.endswith(entry["tag_suffix"]):
            continue
        assets = [{"name": a["name"], "url": a["browser_download_url"], "size": a["size"],
                   "sha256": (a.get("digest") or "").removeprefix("sha256:")}
                  for a in rel["assets"] if any(re.search(p, a["name"]) for p in entry["assets"])]
        if assets:
            return {"tag": tag, "version": version_of(entry, tag), "published": rel.get("published_at", "")[:10],
                    "assets": assets}
    return None


def resolve_all(releases_cache=None):
    """Catalog entries with their latest release (or an error when offline)."""
    cache = {} if releases_cache is None else releases_cache
    out = []
    for e in CATALOG:
        item = {**e, "online": False, "latest": "", "size": 0}
        try:
            rel = library._cached(("release", e["id"]), lambda e=e: latest(e, cache))
            if rel:
                est = sum(a["size"] for a in rel["assets"])
                if e.get("zip_members"):
                    est = int(est * 0.06)  # only one board's files are kept from the chip-family zip
                item.update(online=True, latest=rel["version"], size=est, release=rel)
            else:
                item["error"] = "no matching release"
        except Exception as exc:
            item["error"] = str(exc)
        out.append(item)
    return out


def fetch(depot, entry, progress=None, cancelled=lambda: False):
    """Download one entry's assets into the store, verify digests, prune older versions."""
    rel = entry.get("release") or latest(entry)
    if not rel:
        raise IOError(f"{entry['id']}: no release found")
    sub = f"{'firmware' if entry['kind'] == 'firmware' else 'apps'}/{entry['id']}/{rel['version']}"
    folder = depot.root / sub
    folder.mkdir(parents=True, exist_ok=True)
    done = 0
    for a in rel["assets"]:
        dest = folder / a["name"]

        def prog(n, base=done):
            if progress:
                progress(base + n)
        library.download(a["url"], dest, a["size"], a["sha256"] or None, prog, cancelled)
        sha = a["sha256"] or manifest.sha256_file(dest, cancelled)
        if entry.get("zip_members"):
            _extract_members(depot, entry, rel, dest, folder, sub)
            dest.unlink()  # the chip-family zip is large; keep only this board's files
        else:
            depot.record(entry["kind"], entry["id"], f"{sub}/{a['name']}", rel["version"], dest.stat().st_size,
                         sha, a["url"])
        done += a["size"]
    depot.prune_versions(entry["kind"], entry["id"], rel["version"])
    _remove_empty_dirs(depot.root / sub.rsplit("/", 1)[0])
    return rel["version"]


def _extract_members(depot, entry, rel, zpath, folder, sub):
    kept = 0
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir() or not any(re.search(p, info.filename) for p in entry["zip_members"]):
                continue
            name = info.filename.rsplit("/", 1)[-1]
            data = z.read(info)
            (folder / name).write_bytes(data)
            depot.record(entry["kind"], entry["id"], f"{sub}/{name}", rel["version"], len(data),
                         manifest.sha256_file(folder / name), rel["assets"][0]["url"])
            kept += 1
    if not kept:
        raise IOError(f"{entry['id']}: none of this board's files were in {zpath.name}")


def _remove_empty_dirs(top):
    if not top.is_dir():
        return
    for d in sorted((p for p in top.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()


def held_versions(depot):
    """{item id: version} for firmware and apps in the store."""
    out = {}
    for r in depot.rows():
        if r["kind"] in ("firmware", "app") and r["status"] == "ok":
            out[r["item_id"]] = max(out.get(r["item_id"], ""), r["version"])
    return out


def vkey(v):
    """Sort key for versions like 1.18.0 or 2.7.26.54e0d8d or 2026.11.11-39 (numbers compare as numbers)."""
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.\-_]", v or "")]


def newer(latest_v, held_v):
    return bool(latest_v) and vkey(latest_v) > vkey(held_v or "")
