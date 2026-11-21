"""Decide what fits on a drive, most important first.

Order: kit essentials (inventory printout, readme) -> encrypted documents by
priority -> reader tools -> offline libraries by tier. Anything that doesn't
fit is listed with the reason so the user can choose a bigger drive.
"""

import os

SMALL_RESERVE = 64 * 1024 ** 2
ARCHIVE_OVERHEAD = 1.02          # 7z headers + encryption padding on incompressible files
FAT32_VOLUME = 3900 * 1024 ** 2  # split the document archive below the FAT32 limit


def path_size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def plan(drive, documents, tools, libraries, existing=None, reserve_fraction=0.02):
    """
    drive: {"free", "fstype", "max_file"} (max_file None = unlimited)
    documents: [{"id", "label", "priority", "size", "exists"}]
    tools / libraries: [{"id", "size", "file", "enabled", "group"?, "tier"?}]
    existing: {filename: bytes} already in the kit (counts as reusable space)
    """
    existing = existing or {}
    reusable = sum(existing.values())
    budget = drive["free"] + reusable - max(SMALL_RESERVE, int(drive["free"] * reserve_fraction))
    max_file = drive.get("max_file")
    used = 0
    included, skipped = [], []

    def take(kind, item, size, note=""):
        nonlocal used
        used += size
        included.append({"kind": kind, "id": item["id"], "label": item.get("label") or item.get("why", ""),
                         "size": size, "note": note})

    def skip(kind, item, size, reason):
        skipped.append({"kind": kind, "id": item["id"], "label": item.get("label") or item.get("why", ""),
                        "size": size, "reason": reason})

    take("kit", {"id": "inventory", "label": "Inventory, gap report and README"}, 2 * 1024 ** 2)

    for doc in sorted(documents, key=lambda d: (d["priority"], d["id"])):
        if not doc.get("exists", True):
            skip("document", doc, 0, "path no longer exists")
            continue
        size = int(doc["size"] * ARCHIVE_OVERHEAD)
        if used + size > budget:
            skip("document", doc, size, "not enough space")
            continue
        take("document", doc, size)

    for tool in tools:
        if not tool.get("enabled", True):
            continue
        if used + tool["size"] > budget:
            skip("tool", tool, tool["size"], "not enough space")
            continue
        take("tool", tool, tool["size"])

    chosen_groups = set()
    for lib in sorted(libraries, key=lambda l: l.get("tier", 9)):
        group = lib.get("group")
        size = lib["size"]
        if not lib.get("enabled", True):
            continue
        if group and group in chosen_groups:
            continue  # a better alternative from this group already fits
        if max_file and size > max_file:
            skip("library", lib, size, "over 4 GB: drive is FAT32, reformat as exFAT to include it")
            continue
        if used + size > budget:
            skip("library", lib, size, "not enough space" + (" (trying a smaller version)" if group else ""))
            continue
        take("library", lib, size, "already on drive" if lib.get("file") in existing else "")
        if group:
            chosen_groups.add(group)

    docs_bytes = sum(i["size"] for i in included if i["kind"] == "document")
    return {
        "budget": max(0, budget), "used": used, "free": drive["free"], "reusable": reusable,
        "included": included, "skipped": skipped,
        "split_documents": bool(max_file and docs_bytes > FAT32_VOLUME),
        "documents_complete": not any(s["kind"] == "document" for s in skipped),
    }
