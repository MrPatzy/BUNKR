"""Does the hub computer need a restart? Read-only: BUNKR never restarts or updates anything itself.

Ubuntu's unattended upgrades install security fixes but leave the restart to a person. When a fix needs one, Ubuntu
writes /var/run/reboot-required (and the packages that asked, one per line, in reboot-required.pkgs). If
update-notifier has left /var/lib/update-notifier/updates-available (readable without root), its count of waiting
security updates is shown too; otherwise that part is skipped.

Admins see it on Home, and get one note under the bell the first time it appears after each start-up of the
computer (keyed by the kernel's boot id, so it isn't repeated every day).
"""

import re
from datetime import datetime, timezone
from pathlib import Path

RUN_DIR = Path("/var/run")
NOTIFIER = Path("/var/lib/update-notifier/updates-available")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
listeners = []   # the notifier (set by the server)


def _read(path, limit=20_000):
    try:
        return Path(path).read_text(errors="replace")[:limit]
    except OSError:
        return None


def pending_updates(text):
    """(updates, security) from update-notifier's message, or (None, None) when it can't be read."""
    if not text:
        return None, None
    m = re.search(r"(\d+)\s+(?:updates?|packages?)\s+can be (?:applied|updated|installed)", text)
    if not m and re.search(r"\b0 updates|no updates|system is up to date", text, re.I):
        return 0, 0
    sec = re.search(r"(\d+)\s+(?:of these updates are|updates? (?:are|is))\s+(?:a\s+)?(?:standard\s+)?security updates?", text)
    sec = sec or re.search(r"(\d+)\s+(?:of these updates is a|of these updates are)\s+(?:standard\s+)?security update", text)
    return (int(m.group(1)) if m else None), (int(sec.group(1)) if sec else (0 if m else None))


def status(run_dir=None, notifier=None):
    """{"reboot": bool, "since": iso time, "packages": [...], "updates": n|None, "security": n|None}."""
    run_dir = Path(run_dir or RUN_DIR)
    flag = run_dir / "reboot-required"
    out = {"reboot": flag.is_file(), "since": "", "packages": [], "updates": None, "security": None}
    if out["reboot"]:
        try:
            out["since"] = datetime.fromtimestamp(flag.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        except OSError:
            pass
        pkgs = _read(run_dir / "reboot-required.pkgs") or ""
        seen = []
        for line in pkgs.splitlines():
            line = line.strip()
            if re.fullmatch(r"[a-z0-9][a-z0-9+.\-]{0,80}", line) and line not in seen:
                seen.append(line)
        out["packages"] = seen[:50]
    out["updates"], out["security"] = pending_updates(_read(notifier or NOTIFIER))
    out["attention"] = bool(out["reboot"] or out["security"])
    return out


def boot_id():
    return (_read(BOOT_ID, 100) or "").strip() or "unknown"


def check(store, run_dir=None, notifier=None, boot=None):
    """Hourly on the hub: remember the state for Home, and tell admins once per boot that a restart is needed."""
    st = status(run_dir, notifier)
    store.set_raw("host_status", st | {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if st["reboot"]:
        key = "reboot-needed:" + (boot or boot_id())
        if not store._rows("SELECT 1 FROM reminder_log WHERE key=?", (key,)):
            store._exec("INSERT INTO reminder_log(key, at) VALUES(?,?)", (key, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            what = ", ".join(st["packages"][:4]) + (" and more" if len(st["packages"]) > 4 else "")
            for fn in list(listeners):
                try:
                    fn("reminder", {"user_id": None, "title": "The hub needs a restart",
                                    "body": ("Security updates were installed" + (f" ({what})" if what else "")
                                             + ". Restart the hub computer when it suits you; BUNKR won't do it."),
                                    "tag": "reboot-needed", "url": "/#home"})
                except Exception:
                    pass
    return st
