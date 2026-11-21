"""Is there a newer BUNKR? A once-a-day look at the project's latest GitHub release, and a note to admins.

BUNKR never updates itself and never sends anything about this install: the check is one anonymous request for
public release information (GitHub sees the hub's address, as with any download). Admins choose when to upgrade,
can skip a version, or turn the check off. Each new version is announced once, to admins, through the usual
channels (the bell, and a push or email by their settings), with the release notes on the Household tab.
"""

import json
import re
import urllib.request
from datetime import datetime, timezone

from . import __version__

REPO = "MrPatzy/BUNKR"          # the public repository, "owner/name"; set when the project is published
USER_AGENT = f"BUNKR/{__version__} (self-hosted; update check)"
DEFAULTS = {"enabled": True, "repo": REPO, "skip": ""}
listeners = []


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse(v):
    """ "v1.2.10" -> (1, 2, 10); a pre-release ("1.3.0-beta.1") counts as before the release. None if not a version."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-+](.+))?", str(v or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), 0 if m.group(4) else 1)


def newer(a, b):
    """Is version a newer than version b?"""
    pa, pb = parse(a), parse(b)
    return bool(pa and pb and pa > pb)


def settings(store):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (store.get_raw("selfupdate", {}) or {}).items() if k in DEFAULTS})
    s["repo"] = s["repo"] or REPO
    return s


def save_settings(store, data):
    s = settings(store)
    if "enabled" in data:
        s["enabled"] = bool(data["enabled"])
    if "repo" in data:
        repo = str(data["repo"] or "").strip().removeprefix("https://github.com/").strip("/")
        if repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("the repository is owner/name, like someone/bunkr")
        s["repo"] = repo
    if "skip" in data:
        skip = str(data["skip"] or "").strip()
        if skip and not parse(skip):
            raise ValueError("that isn't a version number")
        s["skip"] = skip
    store.set_raw("selfupdate", s)
    return view(store)


def view(store):
    s = settings(store)
    latest = store.get_raw("selfupdate_latest", {}) or {}
    avail = bool(latest.get("version") and newer(latest["version"], __version__))
    return {"current": __version__, "enabled": s["enabled"], "repo": s["repo"], "skip": s["skip"], "latest": latest,
            "available": avail, "skipped": avail and latest.get("version") == s["skip"],
            "checked_at": store.get_raw("selfupdate_checked", "") or "", "error": store.get_raw("selfupdate_error", "") or ""}


def check(store, opener=None):
    """Ask GitHub for the latest release; tell admins once about a newer one. Returns view()."""
    s = settings(store)
    if not s["enabled"] or not s["repo"]:
        return view(store)
    req = urllib.request.Request(f"https://api.github.com/repos/{s['repo']}/releases/latest",
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=20) as r:
            d = json.loads(r.read(500_000))
    except Exception as exc:
        store.set_raw("selfupdate_error", f"{type(exc).__name__}: {exc}"[:200])
        store.set_raw("selfupdate_checked", _now())
        return view(store)
    version = str(d.get("tag_name") or "").strip()
    if not parse(version) or d.get("draft") or d.get("prerelease"):
        store.set_raw("selfupdate_error", "the latest release has no version number")
        store.set_raw("selfupdate_checked", _now())
        return view(store)
    latest = {"version": version.lstrip("v"), "name": str(d.get("name") or "")[:120], "notes": str(d.get("body") or "")[:6000],
              "url": str(d.get("html_url") or "")[:300], "published_at": str(d.get("published_at") or "")[:25]}
    store.set_raw("selfupdate_latest", latest)
    store.set_raw("selfupdate_checked", _now())
    store.set_raw("selfupdate_error", "")
    if newer(latest["version"], __version__) and latest["version"] != s["skip"]:
        key = "selfupdate:" + latest["version"]
        if not store._rows("SELECT 1 FROM reminder_log WHERE key=?", (key,)):
            store._exec("INSERT INTO reminder_log(key, at) VALUES(?,?)", (key, _now()))
            for fn in list(listeners):
                try:
                    fn("reminder", {"user_id": None, "title": f"BUNKR {latest['version']} is available",
                                    "body": f"This hub runs {__version__}. See what's new and how to upgrade on the Household tab. "
                                            "Nothing changes until you upgrade.",
                                    "tag": "selfupdate", "url": "/#household"})
                except Exception:
                    pass
    return view(store)
