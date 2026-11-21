"""Find mounted drives and the free space / filesystem type of a target path."""

import json
import os
import shutil
import subprocess

SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/home", "/var", "/usr", "/tmp", "/opt", "/srv"}
FAT32_MAX_FILE = 4 * 1024 ** 3 - 1


def _lsblk():
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,FSTYPE,LABEL,MOUNTPOINT,RM,HOTPLUG,TRAN,MODEL"],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)["blockdevices"]


def list_drives():
    """Mounted, writable filesystems that aren't the OS itself, removable first."""
    drives = []

    def walk(dev, parent):
        for child in dev.get("children") or []:
            walk(child, dev)
        mount = dev.get("mountpoint")
        if not mount or mount in SYSTEM_MOUNTS or mount.startswith(("/snap", "/boot", "[")):
            return
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            return
        removable = bool(dev.get("rm") or dev.get("hotplug") or (parent or {}).get("rm")
                         or (parent or {}).get("hotplug") or (parent or {}).get("tran") == "usb")
        drives.append({
            "mount": mount, "device": dev.get("path"), "label": dev.get("label") or "",
            "model": ((parent or {}).get("model") or dev.get("model") or "").strip(),
            "fstype": dev.get("fstype") or "", "removable": removable,
            "total": usage.total, "free": usage.free,
            "writable": os.access(mount, os.W_OK),
        })

    for dev in _lsblk():
        walk(dev, None)
    drives.sort(key=lambda d: (not d["removable"], d["mount"]))
    return drives


def probe(path):
    """Free space and filesystem for any directory (e.g. a mount you typed in)."""
    path = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise FileNotFoundError(f"{path} is not a directory")
    usage = shutil.disk_usage(path)
    fstype = subprocess.run(["findmnt", "-n", "-o", "FSTYPE", "-T", path],
                            capture_output=True, text=True).stdout.strip()
    return {"mount": path, "fstype": fstype, "total": usage.total, "free": usage.free,
            "writable": os.access(path, os.W_OK)}


def max_file_size(fstype):
    return FAT32_MAX_FILE if fstype.lower() in ("vfat", "fat", "fat32", "msdos") else None
