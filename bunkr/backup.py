"""Simple versioned backups using rsync snapshots.

Each run creates dest/<timestamp>/ as a full browsable copy. Unchanged files
are hard-linked to the previous snapshot (--link-dest), so extra snapshots
cost almost no space on Linux filesystems. On FAT/exFAT drives hard links
aren't supported and each snapshot is a full copy.
"""

import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")


def snapshots(dest):
    dest = Path(dest)
    if not dest.is_dir():
        return []
    return sorted(p for p in dest.iterdir() if p.is_dir() and SNAPSHOT_RE.match(p.name))


def run(job):
    source = Path(os.path.expanduser(job["source"]))
    dest = Path(os.path.expanduser(job["dest"]))
    if not source.exists():
        raise FileNotFoundError(f"source {source} does not exist")
    if dest.resolve().is_relative_to(source.resolve()):
        raise ValueError("destination is inside the source; that would back up the backups")
    dest.mkdir(parents=True, exist_ok=True)

    previous = snapshots(dest)
    target = dest / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    partial = target.with_name(target.name + ".partial")
    cmd = ["rsync", "-a", "--delete", "--modify-window=2"]  # FAT stores times at 2s precision
    if previous:
        cmd.append(f"--link-dest={previous[-1]}")
    src = str(source) + ("/" if source.is_dir() else "")
    cmd += [src, str(partial) + ("/" if source.is_dir() else "")]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # 23/24 = some files vanished or were unreadable; keep the snapshot but report it
    if proc.returncode not in (0, 23, 24):
        shutil.rmtree(partial, ignore_errors=True)
        raise RuntimeError(proc.stderr.strip()[-500:] or f"rsync exited {proc.returncode}")
    partial.rename(target)

    keep = max(1, int(job.get("keep") or 1))
    removed = 0
    for old in snapshots(dest)[:-keep]:
        shutil.rmtree(old)
        removed += 1
    note = " (some files could not be read)" if proc.returncode else ""
    return f"snapshot {target.name} created, {removed} old removed{note}"
