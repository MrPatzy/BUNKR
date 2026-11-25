"""Inventory CSV import: read a file, guess which column is which, clean each row.

Pure logic with no database; server.py does the location and duplicate lookups and the saving.
Firearms and ammunition are never imported (they are admin-only and added one at a time)."""

import csv
import io
import re
from datetime import datetime

from . import gaps

# What the import fills in. The template and a BUNKR export share these names.
FIELDS = ["name", "category", "quantity", "unit", "pack_size", "pack_unit", "per_unit", "size_unit", "essential",
          "location", "expires", "notes"]
LABELS = {"name": "Item name", "category": "Category", "quantity": "Quantity", "unit": "Unit", "pack_size": "Pack size",
          "pack_unit": "Pack unit", "per_unit": "Value each", "size_unit": "Size unit", "essential": "Checklist item",
          "location": "Location", "expires": "Expires", "notes": "Notes"}
NUMBERS = ("quantity", "pack_size", "per_unit")
MAX_ROWS = 2000

EXAMPLES = [
    {"name": "Canned tomatoes", "category": "food", "quantity": "2", "unit": "cans", "location": "Pantry",
     "expires": "2027-06-30", "notes": "Keep the oldest in front"},
    {"name": "Canned tomatoes", "category": "food", "quantity": "2", "unit": "cans", "location": "Pantry",
     "expires": "2028-06-30", "notes": "Different expiry stays a separate item"},
    {"name": "Bottled water", "category": "water", "quantity": "24", "unit": "bottles", "location": "Garage"},
]

# Common header names, compared after lower-casing and dropping everything but letters and digits.
SYNONYMS = {
    "name": ["name", "item", "itemname", "product", "description", "title", "supply", "thing"],
    "category": ["category", "type", "kind", "group", "section"],
    "quantity": ["quantity", "qty", "count", "amount", "number", "have", "onhand", "stock", "units"],
    "unit": ["unit", "uom", "unitofmeasure", "measure"],
    "pack_size": ["packsize", "packagesize", "size", "perpack", "packqty"],
    "pack_unit": ["packunit", "package", "packaging", "packtype"],
    "per_unit": ["perunit", "valueeach", "each", "price", "cost", "value", "unitprice"],
    "size_unit": ["sizeunit"],
    "essential": ["essential", "checklist", "checklistitem"],
    "location": ["location", "where", "place", "room", "storage", "bin", "stored", "storedin", "shelf"],
    "expires": ["expires", "expiry", "expiration", "expirationdate", "expirydate", "expdate", "exp", "bestby",
                "bestbefore", "useby", "date"],
    "notes": ["notes", "note", "comments", "comment", "remarks", "memo", "details"],
}


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def template_csv():
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    w.writerows(EXAMPLES)
    return buf.getvalue()


def read(text):
    """(headers, rows) from CSV text; rows are lists padded to the header width. Blank lines are dropped."""
    text = (text or "").lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    table = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not table:
        return [], []
    headers = [h.strip() for h in table[0]]
    width = len(headers)
    return headers, [(r + [""] * width)[:width] for r in table[1:MAX_ROWS + 1]]


def guess(headers):
    """{field: column index or None}. Exact name first, then the common synonyms; each column is used once."""
    mapping, used = {}, set()
    normed = [_norm(h) for h in headers]
    for field in FIELDS:  # exact BUNKR names win, so a re-imported export maps perfectly
        mapping[field] = None
        if _norm(field) in normed and normed.index(_norm(field)) not in used:
            mapping[field] = normed.index(_norm(field))
            used.add(mapping[field])
    for field in FIELDS:
        if mapping[field] is not None:
            continue
        for word in SYNONYMS[field]:
            if word in normed and normed.index(word) not in used:
                mapping[field] = normed.index(word)
                used.add(mapping[field])
                break
    return mapping


def clean_mapping(mapping, width):
    out = {}
    for field in FIELDS:
        v = (mapping or {}).get(field)
        out[field] = v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < width else None
    return out


def _category(text):
    key = _norm(text)
    if not key:
        return "other"
    for cid, label in gaps.CATEGORIES.items():
        if key in (_norm(cid), _norm(label)):
            return cid
    for cid, label in gaps.CATEGORIES.items():
        if _norm(label).startswith(key) or key.startswith(_norm(cid)):
            return cid
    return None


def _date(text):
    text = text.strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _number(text):
    text = re.sub(r"[,$\s]", "", text)
    if not text:
        return ""
    try:
        v = float(text)
    except ValueError:
        return None
    return v if v >= 0 and v == v and v != float("inf") else None


def rows_for(headers, table, mapping):
    """Each file row as {n, data, location, problem}. `data` holds only the mapped fields, cleaned;
    `problem` is a plain sentence when the row can't be imported."""
    out = []
    for n, raw in enumerate(table, 2):  # line numbers as a spreadsheet shows them
        data, problem = {}, ""
        for field, col in mapping.items():
            if col is None:
                continue
            cell = raw[col].strip()
            if field == "category":
                v = _category(cell)
                if v is None:
                    problem = problem or f"unknown category “{cell}”"
                    v = "other"
            elif field == "expires":
                v = _date(cell)
                if v is None:
                    problem = problem or f"can't read the date “{cell}”"
                    v = ""
            elif field in NUMBERS:
                v = _number(cell)
                if v is None:
                    problem = problem or f"“{cell}” isn't a number"
                    v = ""
                if v == "" and field == "quantity":
                    v = 0
            elif field == "size_unit":
                v = cell if cell in gaps.GAL_PER else ""
            elif field == "essential":
                v = cell if cell in gaps.CHECKLIST_IDS else ""
            else:
                v = cell
            data[field] = v
        if not any(raw):
            continue
        if mapping.get("name") is None or not data.get("name"):
            problem = problem or "no item name"
        if data.get("category") in gaps.SENSITIVE:
            problem = "firearms and ammunition are added one at a time on the Inventory tab"
        loc = data.pop("location", "")
        out.append({"n": n, "data": data, "location": loc, "problem": problem})
    return out
