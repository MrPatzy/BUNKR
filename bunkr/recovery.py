"""Recovery: getting back on our feet after a loss.

- Home inventory for insurance: rooms, items with photos and receipts. Photos and receipts are files in
  `<db dir>/recovery-files/` (not in the database, and not in the downloads store, which nodes sync). Serial numbers
  and receipts are admin-only, like firearm serials. None of it is synced to nodes or offered to the AI connector;
  a full copy goes inside the encrypted document archive.
- Financial access sheet: who to call about each account. Admin-only everywhere, never synced, never in the offline
  copy, never in the connector. Only the last four digits of a number are kept.
- Meal plan from stored food: days of food, what to eat first, and a simple day-by-day plan.
- Kids' plan: a big-text page for children from the family plan (meeting places, who to call). Nothing sensitive.
"""

import csv
import html
import io
import math
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from . import gaps

FOLDER = "recovery-files"
MAX_FILE = 10 * 1024 * 1024        # per photo or receipt
MAX_FILES_PER_ITEM = 20
FILE_KINDS = ("photo", "receipt")
# what we accept, checked by the first bytes rather than trusting the browser
TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}
RECEIPT_ONLY = {"application/pdf"}  # a photo must be an image
FINANCE_KINDS = {"bank": "Bank", "credit_card": "Credit card", "insurance": "Insurance policy", "retirement": "Retirement",
                 "loan": "Loan", "utility": "Utility", "other": "Other"}
FINANCE_FIELDS = ("institution", "kind", "holder", "last4", "phone", "website", "notes", "contact", "paperwork")
ITEM_FIELDS = ("room_id", "name", "category", "make_model", "serial", "purchased", "price_paid", "replacement_value", "notes")
ITEM_CATEGORIES = ("Electronics", "Furniture", "Appliances", "Jewelry", "Clothing", "Tools", "Kitchen", "Art and decor",
                   "Sports and outdoor", "Toys", "Books and media", "Other")
ADMIN_ONLY_ITEM = ("serial",)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _text(v, limit=500):
    return " ".join(str(v or "").split())[:limit]


def _money(v):
    if v in (None, ""):
        return None
    try:
        n = float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        raise ValueError("price and value must be numbers")
    if n < 0 or n > 1e9 or math.isnan(n):
        raise ValueError("price and value must be between 0 and 1,000,000,000")
    return round(n, 2)


def _date(v):
    v = str(v or "").strip()
    if not v:
        return ""
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise ValueError("purchase date must be YYYY-MM-DD")


def files_dir(store):
    return Path(store.path).parent / FOLDER


# ---------- rooms ----------
def rooms(store):
    return store._rows("SELECT r.*, (SELECT COUNT(*) FROM recovery_items i WHERE i.room_id=r.id) AS item_count "
                       "FROM recovery_rooms r ORDER BY r.name COLLATE NOCASE")


def save_room(store, data, row_id=None):
    name = _text(data.get("name"), 80)
    if not name:
        raise ValueError("a room needs a name")
    clash = store._rows("SELECT id FROM recovery_rooms WHERE name=? COLLATE NOCASE AND id IS NOT ?", (name, row_id))
    if clash:
        raise ValueError("there is already a room with that name")
    notes = _text(data.get("notes"), 500)
    if row_id is None:
        return store._exec("INSERT INTO recovery_rooms(name, notes) VALUES(?, ?)", (name, notes))
    store._exec("UPDATE recovery_rooms SET name=?, notes=? WHERE id=?", (name, notes, row_id))
    return row_id


def delete_room(store, row_id):
    """Items in it stay, with no room."""
    store._exec("DELETE FROM recovery_rooms WHERE id=?", (row_id,))


# ---------- items ----------
def clean_item(store, data, admin, partial=False):
    """A new item takes every field; an edit (partial) changes only the fields it sends."""
    rec = {}
    has = lambda f: not partial or f in data  # noqa: E731
    if "room_id" in data:
        rid = data.get("room_id")
        rid = int(rid) if str(rid or "").strip().isdigit() else None
        if rid is not None and not store._rows("SELECT 1 FROM recovery_rooms WHERE id=?", (rid,)):
            raise ValueError("unknown room")
        rec["room_id"] = rid
    if has("name"):
        rec["name"] = _text(data.get("name"), 120)
        if not rec["name"]:
            raise ValueError("an item needs a name")
    for f in ("category", "make_model"):
        if has(f):
            rec[f] = _text(data.get(f), 120)
    if has("purchased"):
        rec["purchased"] = _date(data.get("purchased"))
    for f in ("price_paid", "replacement_value"):
        if has(f):
            rec[f] = _money(data.get(f))
    if has("notes"):
        rec["notes"] = _text(data.get("notes"), 1000)
    if admin and "serial" in data:  # a family login's edit never touches the serial number
        rec["serial"] = _text(data.get("serial"), 80)
    rec["updated_at"] = _now()
    return rec


def save_item(store, data, admin, row_id=None):
    rec = clean_item(store, data, admin, partial=row_id is not None)
    if row_id is None:
        return store._exec(f"INSERT INTO recovery_items({', '.join(rec)}) VALUES({', '.join('?' for _ in rec)})",
                           tuple(rec.values()))
    if not store._rows("SELECT 1 FROM recovery_items WHERE id=?", (row_id,)):
        raise LookupError("no such item")
    store._exec(f"UPDATE recovery_items SET {', '.join(f'{k}=?' for k in rec)} WHERE id=?", (*rec.values(), row_id))
    return row_id


def delete_item(store, row_id):
    for f in store._rows("SELECT * FROM recovery_files WHERE item_id=?", (row_id,)):
        _unlink(store, f)
    store._exec("DELETE FROM recovery_files WHERE item_id=?", (row_id,))
    store._exec("DELETE FROM recovery_items WHERE id=?", (row_id,))


def items(store, admin):
    """Every item with its room and files. Family logins get no serial numbers and no receipts."""
    rows = store._rows("SELECT i.*, COALESCE(r.name, '') AS room FROM recovery_items i "
                       "LEFT JOIN recovery_rooms r ON r.id = i.room_id ORDER BY r.name COLLATE NOCASE, i.name COLLATE NOCASE")
    files = store._rows("SELECT id, item_id, kind, name, ctype, size, added_at FROM recovery_files ORDER BY id")
    by_item = {}
    for f in files:
        if f["kind"] == "receipt" and not admin:
            continue
        by_item.setdefault(f["item_id"], []).append(f)
    for r in rows:
        r["files"] = by_item.get(r["id"], [])
        if not admin:
            for f in ADMIN_ONLY_ITEM:
                r.pop(f, None)
    return rows


def totals(rows):
    paid = sum(r["price_paid"] or 0 for r in rows)
    value = sum(r["replacement_value"] or 0 for r in rows)
    return {"items": len(rows), "price_paid": round(paid, 2), "replacement_value": round(value, 2)}


def by_room(rows):
    groups = {}
    for r in rows:
        groups.setdefault(r["room"] or "No room", []).append(r)
    return [{"room": k, "items": v, "totals": totals(v)} for k, v in groups.items()]


def view(store, admin):
    rows = items(store, admin)
    return {"rooms": rooms(store), "items": rows, "totals": totals(rows), "categories": list(ITEM_CATEGORIES),
            "limits": {"max_mb": MAX_FILE // (1024 * 1024), "per_item": MAX_FILES_PER_ITEM,
                       "types": {"photo": [t for t in TYPES if t not in RECEIPT_ONLY], "receipt": list(TYPES)}}}


# ---------- files (photos and receipts) ----------
def _sniff(head):
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def add_file(store, item_id, kind, name, data):
    if kind not in FILE_KINDS:
        raise ValueError("kind must be photo or receipt")
    if not store._rows("SELECT 1 FROM recovery_items WHERE id=?", (item_id,)):
        raise LookupError("no such item")
    if not data:
        raise ValueError("the file is empty")
    if len(data) > MAX_FILE:
        raise ValueError(f"files can be up to {MAX_FILE // (1024 * 1024)} MB")
    ctype = _sniff(data[:16])
    if not ctype or (kind == "photo" and ctype in RECEIPT_ONLY):
        raise ValueError("photos must be JPEG, PNG or WebP; receipts may also be PDF")
    n = store._rows("SELECT COUNT(*) AS n FROM recovery_files WHERE item_id=?", (item_id,))[0]["n"]
    if n >= MAX_FILES_PER_ITEM:
        raise ValueError(f"up to {MAX_FILES_PER_ITEM} files per item")
    folder = files_dir(store)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stored = secrets.token_hex(12) + TYPES[ctype]
    part = folder / (stored + ".part")
    part.write_bytes(data)
    part.chmod(0o600)
    part.replace(folder / stored)
    label = Path(str(name or "")).name[:120] or kind + TYPES[ctype]
    return store._exec("INSERT INTO recovery_files(item_id, kind, name, file, ctype, size, added_at) VALUES(?,?,?,?,?,?,?)",
                       (item_id, kind, label, stored, ctype, len(data), _now()))


def file_row(store, file_id):
    r = store._rows("SELECT * FROM recovery_files WHERE id=?", (file_id,))
    return r[0] if r else None


def file_path(store, row):
    p = (files_dir(store) / row["file"]).resolve()
    if not p.is_relative_to(files_dir(store).resolve()):
        raise LookupError("bad file")
    return p


def _unlink(store, row):
    try:
        file_path(store, row).unlink(missing_ok=True)
    except (LookupError, OSError):
        pass


def delete_file(store, file_id):
    row = file_row(store, file_id)
    if row:
        _unlink(store, row)
        store._exec("DELETE FROM recovery_files WHERE id=?", (file_id,))


# ---------- printouts ----------
CSS = """body{font:13.5px/1.4 system-ui,sans-serif;margin:20px;color:#111;background:#fff;max-width:1000px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:16px;margin:18px 0 6px;border-bottom:2px solid #333;padding-bottom:2px}
table{border-collapse:collapse;width:100%;margin:4px 0 10px}td,th{border:1px solid #999;padding:4px 7px;text-align:left;vertical-align:top}
th{background:#eee}.meta{color:#555}.num{text-align:right;white-space:nowrap}.thumbs img{height:72px;margin:0 4px 4px 0;border:1px solid #ccc}
tr.total td{font-weight:bold;background:#f6f6f6}@media print{body{margin:8mm}h2{page-break-after:avoid}tr{page-break-inside:avoid}}"""


def _usd(v):
    return "" if v is None else f"${v:,.2f}"


def insurance_html(rows, admin, household="Household", src=None):
    """The insurance list, grouped by room. `src(file)` gives each photo's address (the API, or a path inside the
    archive). Serial numbers only for admins."""
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    src = src or (lambda f: f"/api/recovery/files/{f['id']}")
    cols = 7 + (1 if admin else 0)
    out = []
    for g in by_room(rows):
        body = []
        for r in g["items"]:
            photos = [f for f in r["files"] if f["kind"] == "photo"]
            receipts = [f for f in r["files"] if f["kind"] == "receipt"]
            thumbs = "".join(f'<img src="{e(src(f))}" alt="">' for f in photos)
            body.append(f"<tr><td><b>{e(r['name'])}</b>{'<br><span class=meta>' + e(r['notes']) + '</span>' if r['notes'] else ''}</td>"
                        f"<td>{e(r['category'])}</td><td>{e(r['make_model'])}</td>"
                        + (f"<td>{e(r.get('serial'))}</td>" if admin else "")
                        + f"<td>{e(r['purchased'])}</td><td class=num>{_usd(r['price_paid'])}</td>"
                        f"<td class=num>{_usd(r['replacement_value'])}</td>"
                        f"<td class=thumbs>{thumbs}{'<br>' if thumbs and receipts else ''}"
                        f"{f'{len(receipts)} receipt(s)' if receipts and admin else ''}</td></tr>")
        t = g["totals"]
        body.append(f"<tr class=total><td colspan={cols - 3}>{e(g['room'])}: {t['items']} item(s)</td>"
                    f"<td class=num>{_usd(t['price_paid'])}</td><td class=num>{_usd(t['replacement_value'])}</td><td></td></tr>")
        head = ("<tr><th>Item</th><th>Category</th><th>Make and model</th>" + ("<th>Serial number</th>" if admin else "")
                + "<th>Bought</th><th>Paid</th><th>To replace</th><th>Photos</th></tr>")
        out.append(f"<h2>{e(g['room'])}</h2><table>{head}{''.join(body)}</table>")
    t = totals(rows)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Home inventory for insurance</title><style>{CSS}</style></head><body>
<h1>{e(household)}: home inventory</h1>
<p class="meta">Printed {datetime.now():%Y-%m-%d} from BUNKR. For an insurance claim after a fire, flood, storm or theft.
{t['items']} item(s). Paid in total {_usd(t['price_paid'])}; to replace {_usd(t['replacement_value'])}.</p>
{''.join(out) or '<p>No items yet. Add them on the Recovery tab.</p>'}
</body></html>"""


CSV_FIELDS = ["room", "name", "category", "make_model", "serial", "purchased", "price_paid", "replacement_value", "notes",
              "photos", "receipts"]


def insurance_csv(rows, admin):
    fields = [f for f in CSV_FIELDS if admin or f not in ("serial", "receipts")]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({**r, "photos": sum(1 for f in r["files"] if f["kind"] == "photo"),
                    "receipts": sum(1 for f in r["files"] if f["kind"] == "receipt")})
    return buf.getvalue()


# ---------- financial access sheet (admin only) ----------
def clean_last4(v):
    """Only the last four digits. Spaces, dashes and masking (****, xx, •) are allowed; more than four digits isn't."""
    s = re.sub(r"[\s\-*xX•.#]", "", str(v or ""))
    if not s:
        return ""
    if not s.isdigit():
        raise ValueError("last 4 digits: numbers only")
    if len(s) > 4:
        raise ValueError("only the last 4 digits, never the whole number")
    return s


def clean_finance(data):
    rec = {f: _text(data.get(f), 1000 if f in ("notes", "paperwork") else 200) for f in FINANCE_FIELDS}
    if not rec["institution"]:
        raise ValueError("enter the bank, company or insurer")
    if rec["kind"] not in FINANCE_KINDS:
        rec["kind"] = "other"
    rec["last4"] = clean_last4(data.get("last4"))
    rec["updated_at"] = _now()
    return rec


def finance(store):
    order = " ".join(f"WHEN '{k}' THEN {n}" for n, k in enumerate(FINANCE_KINDS))
    return store._rows(f"SELECT * FROM finance_accounts ORDER BY CASE kind {order} ELSE 99 END, institution COLLATE NOCASE")


def save_finance(store, data, row_id=None):
    rec = clean_finance(data)
    if row_id is None:
        return store._exec(f"INSERT INTO finance_accounts({', '.join(rec)}) VALUES({', '.join('?' for _ in rec)})",
                           tuple(rec.values()))
    if not store._rows("SELECT 1 FROM finance_accounts WHERE id=?", (row_id,)):
        raise LookupError("no such account")
    store._exec(f"UPDATE finance_accounts SET {', '.join(f'{k}=?' for k in rec)} WHERE id=?", (*rec.values(), row_id))
    return row_id


def delete_finance(store, row_id):
    store._exec("DELETE FROM finance_accounts WHERE id=?", (row_id,))


def finance_html(rows, household="Household"):
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    body = "".join(
        f"<tr><td><b>{e(r['institution'])}</b><br><span class=meta>{e(FINANCE_KINDS.get(r['kind'], 'Other'))}</span></td>"
        f"<td>{e(r['holder'])}</td><td>{'…' + e(r['last4']) if r['last4'] else ''}</td>"
        f"<td>{e(r['phone'])}<br>{e(r['website'])}</td><td>{e(r['contact'])}</td><td>{e(r['paperwork'])}</td>"
        f"<td>{e(r['notes'])}</td></tr>" for r in rows)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Financial access sheet</title><style>{CSS}</style></head><body>
<h1>{e(household)}: money and accounts</h1>
<p class="meta">Printed {datetime.now():%Y-%m-%d} from BUNKR. Private: keep it with the insurance papers, not in a go-bag.
Who to call to reach money, file a claim or pause bills. Only the last 4 digits of any number are kept here.</p>
<table><tr><th>Institution</th><th>Whose</th><th>Ends in</th><th>Phone and website</th><th>Who to call</th><th>Paperwork is</th><th>Policy and claim notes</th></tr>
{body or '<tr><td colspan=7>No accounts yet.</td></tr>'}</table>
</body></html>"""


# ---------- archive copy ----------
def archive_files(store, household="Household"):
    """For the encrypted document archive: the full insurance list (serials, receipts) with its photo and receipt
    files, and the financial sheet. Values are text or a Path to copy."""
    out = {}
    rows = items(store, admin=True)
    if rows:
        out["Home inventory/Home inventory.html"] = insurance_html(rows, True, household, src=lambda f: f"files/{_archive_name(f)}")
        out["Home inventory/Home inventory.csv"] = insurance_csv(rows, True)
        for f in store._rows("SELECT * FROM recovery_files"):
            p = file_path(store, f)
            if p.is_file():
                out[f"Home inventory/files/{_archive_name(f)}"] = p
    accts = finance(store)
    if accts:
        out["Financial access sheet.html"] = finance_html(accts, household)
    return out


def _archive_name(f):
    return f"{f['item_id']}-{f['id']}-{f['kind']}{TYPES.get(f['ctype'], '')}"


# ---------- meal plan from stored food ----------
def _food(items, today):
    """Food that counts toward days of food (not pet food or formula), with how many units and their calories."""
    out = []
    for i in items:
        if i.get("category") != "food" or i.get("essential") or not i.get("counts_home", 1):
            continue
        units = gaps.count(i)
        if units <= 0:
            continue
        exp = gaps._parse_date(i.get("expires"))
        out.append({"id": i["id"], "name": i["name"], "location": i.get("location") or "", "expires": i.get("expires") or "",
                    "exp": exp, "units": units, "kcal_each": float(i.get("per_unit") or 0),
                    "unit": i.get("pack_unit") or i.get("unit") or "", "expired": bool(exp and exp < today)})
    out.sort(key=lambda f: (f["exp"] is None, f["exp"] or date.max, f["name"].lower()))
    return out


def meal_plan(settings, items, days=7, today=None):
    """Days of food (the same count as Readiness), the eat-first list (soonest expiry first) and a day-by-day plan.

    Each day takes food soonest-to-expire first, but no more than half the day's calories from one item while
    others are left, so meals rotate. Food that will have expired by a day isn't planned for that day."""
    today = today or date.today()
    days = max(1, min(int(days or 7), 90))
    people = gaps._people(settings)
    kcal_day = people * float(settings["kcal_per_person"])
    analysis = gaps.analyze(settings, items, [], today=today)
    food = _food(items, today)
    eat_first = [{k: f[k] for k in ("id", "name", "location", "expires", "units", "unit", "kcal_each", "expired")}
                 for f in food if f["exp"]]
    no_kcal = [f["name"] for f in food if not f["expired"] and not f["kcal_each"]]
    left = {f["id"]: f["units"] for f in food}
    plan = []
    for n in range(days):
        day = today + timedelta(days=n)
        usable = [f for f in food if not f["expired"] and f["kcal_each"] > 0 and left[f["id"]] > 0
                  and not (f["exp"] and f["exp"] < day)]
        need, meals = kcal_day, {}
        for capped in (True, False):
            for f in usable:
                if need <= 0:
                    break
                have = left[f["id"]]
                if have <= 0:
                    continue
                if capped and len(usable) > 1:  # whole units that fit under half the day, so meals rotate
                    take = min(have, math.floor(min(need, kcal_day / 2) / f["kcal_each"] + 1e-9))
                else:  # then top up, rounding up to a whole unit
                    take = min(have, max(1, math.ceil(need / f["kcal_each"] - 1e-9)))
                if take <= 0:
                    continue
                left[f["id"]] -= take
                meals[f["id"]] = meals.get(f["id"], 0) + take
                need -= take * f["kcal_each"]
        by_id = {f["id"]: f for f in food}
        got = kcal_day - need
        plan.append({"day": n + 1, "date": day.isoformat(), "kcal": round(got), "short": round(max(0, need)),
                     "eat": [{"id": i, "name": by_id[i]["name"], "units": round(u, 2), "unit": by_id[i]["unit"],
                              "kcal": round(u * by_id[i]["kcal_each"]), "expires": by_id[i]["expires"]}
                             for i, u in meals.items()]})
    return {"people": people, "kcal_day": round(kcal_day), "food_days": analysis["food_days"], "days": days,
            "eat_first": eat_first, "expired": [f["name"] for f in food if f["expired"]], "no_kcal": no_kcal, "plan": plan}


def meal_html(mp, household="Household"):
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    first = "".join(f"<tr><td>{e(f['name'])}</td><td>{e(f['expires'])}{' (expired)' if f['expired'] else ''}</td>"
                    f"<td>{f['units']:g} {e(f['unit'])}</td><td>{e(f['location'])}</td></tr>" for f in mp["eat_first"])
    days = "".join(
        f"<tr><td><b>Day {d['day']}</b><br><span class=meta>{e(d['date'])}</span></td><td>"
        + ("<br>".join(f"{x['units']:g} {e(x['unit'])} {e(x['name'])} <span class=meta>({x['kcal']:,} kcal)</span>" for x in d["eat"])
           or "Nothing left")
        + f"</td><td class=num>{d['kcal']:,}{'<br><b>short ' + format(d['short'], ',') + '</b>' if d['short'] else ''}</td></tr>"
        for d in mp["plan"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Food plan</title><style>{CSS}</style></head><body>
<h1>{e(household)}: food plan</h1>
<p class="meta">Printed {datetime.now():%Y-%m-%d} from BUNKR. {mp['people']} people need about {mp['kcal_day']:,} kcal a day.
Stored food lasts about <b>{mp['food_days']:g} days</b>. Eat what expires first; drink water with every meal.</p>
<h2>Eat these first</h2><table><tr><th>Food</th><th>Expires</th><th>How much</th><th>Where</th></tr>{first or '<tr><td colspan=4>No expiry dates set.</td></tr>'}</table>
<h2>Day by day ({mp['days']} days)</h2><table><tr><th>Day</th><th>Eat</th><th>kcal</th></tr>{days}</table>
{('<p class=meta>No calories set, so not planned: ' + e(', '.join(mp['no_kcal'])) + '</p>') if mp['no_kcal'] else ''}
{('<p class=meta>Expired, check before eating: ' + e(', '.join(mp['expired'])) + '</p>') if mp['expired'] else ''}
</body></html>"""


# ---------- kids' plan ----------
KID_STEPS = [
    ("Fire", ["Get out fast. Don't stop for toys or pets.", "Stay low under smoke.",
              "Feel a door first. If it is hot, use another way out.", "Go to our meeting spot outside: {near}.",
              "Never go back inside."]),
    ("Earthquake", ["Drop to your hands and knees.", "Cover your head under a table or desk.", "Hold on until the shaking stops.",
                    "Stay away from windows.", "Then find a grown-up."]),
    ("Tornado", ["Go to the lowest floor, in a room with no windows.", "Get under something strong.",
                 "Cover your head and neck with your arms.", "Stay there until a grown-up says it is safe."]),
    ("Lost", ["Stop and stay where you are.", "Look for a police officer, a store worker or a mom with kids.",
              "Tell them your name and our phone number.", "If you can't find us, go to: {far}."]),
]


def kids_html(contacts, sites, household="Our family"):
    """Big text, one page. Only names, phone numbers and meeting places: no medical or money details."""
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    home = next((s for s in sites if s["kind"] == "home"), None)
    near = next((s for s in sites if s["kind"] == "meet_near"), None)
    far = next((s for s in sites if s["kind"] == "meet_far"), None)
    calls = [c for c in contacts if c.get("phone")]
    calls.sort(key=lambda c: (not c.get("out_of_area"), c["name"].lower()))
    who = "".join(f"<li><b>{e(c['name'])}</b>{' (' + e(c['relation']) + ')' if c.get('relation') else ''}: "
                  f"<span class=big>{e(c['phone'])}</span></li>" for c in calls[:4])
    spot = lambda s, fallback: e(s["name"]) if s else fallback  # noqa: E731
    near_t, far_t = spot(near, "the spot a grown-up showed you"), spot(far, "the place a grown-up showed you")
    boxes = "".join(f"<div class=box><h2>{title}</h2><ol>{''.join('<li>' + e(s).replace('{near}', near_t).replace('{far}', far_t) + '</li>' for s in steps)}</ol></div>"
                    for title, steps in KID_STEPS)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Our family plan for kids</title>
<style>body{{font:20px/1.35 system-ui,sans-serif;margin:16px;color:#111;background:#fff;max-width:900px}}
h1{{font-size:32px;margin:0 0 8px}}h2{{font-size:24px;margin:0 0 6px}}.big{{font-size:26px;font-weight:bold;white-space:nowrap}}
.top{{border:4px solid #c00;border-radius:12px;padding:10px 14px;margin-bottom:12px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
.box{{border:3px solid #333;border-radius:12px;padding:8px 12px}}ol,ul{{margin:4px 0;padding-left:26px}}li{{margin:2px 0}}
@media print{{body{{margin:8mm;font-size:17px}}.big{{font-size:22px}}}}</style></head><body>
<h1>{e(household)}: what to do</h1>
<div class=top><h2>Who to call</h2><ul><li>Emergency: <span class=big>911</span></li>{who}</ul>
<p>Where we meet: near home <b>{near_t}</b>{'. Far away: <b>' + far_t + '</b>' if far else ''}.</p>
{'<p>Our address: <b>' + e(home['address']) + '</b></p>' if home and home.get('address') else ''}</div>
<div class=grid>{boxes}</div>
<p>Practice this with a grown-up. It's OK to be scared. Stay calm and help each other.</p>
</body></html>"""
