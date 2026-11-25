"""Printable/portable copies of the inventory and gap report for the USB kit."""

import csv
import html
import io
import json
from datetime import datetime

from . import gaps

CSV_FIELDS = ["name", "category", "quantity", "unit", "pack_size", "pack_unit", "per_unit", "size_unit", "essential",
              "location", "expires", "notes", "subtype", "caliber"]


def firearms_html(items, household="Household"):
    """Serial numbers and ammunition by caliber. Goes only inside the encrypted archive."""
    e = html.escape
    guns = [i for i in items if i["category"] == "firearms" and i.get("subtype") == "firearm"]
    other = [i for i in items if i["category"] == "firearms" and i.get("subtype") != "firearm"]
    if not guns and not other:
        return ""
    rows = "".join(f"<tr><td>{e(i['name'])}</td><td>{e(i.get('caliber') or '')}</td><td><b>{e(i.get('serial') or '')}</b></td>"
                   f"<td>{e(i.get('location') or '')}</td><td>{e(i.get('notes') or '')}</td></tr>" for i in guns)
    ammo = "".join(f"<tr><td>{e(r['caliber'])}</td><td>{r['rounds']:g}</td>"
                   f"<td>{e('; '.join(f'{k}: {v:g}' for k, v in r['by_type'].items()))}</td><td>{r['in_mags']:g}</td>"
                   f"<td>{e(', '.join(r['firearms']))}</td></tr>" for r in gaps.ammo_summary(items))
    mags = "".join(f"<tr><td>{e(m['firearm'])}</td><td>{m['magazines']:g}</td><td>{', '.join(f'{c:g}' for c in m['capacity'])}</td>"
                   f"<td>{m['loaded']:g}</td><td>{m['service']:g}</td></tr>" for m in gaps.magazine_summary(items))
    gear = "".join(f"<tr><td>{e(i['name'])}</td><td>{e(gaps.SUBTYPES.get(i.get('subtype'), ''))}</td>"
                   f"<td>{e(gaps.quantity_text(i))}</td><td>{e(i.get('location') or '')}</td></tr>" for i in other)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Firearms record</title>
<style>body{{font:14px/1.4 system-ui,sans-serif;margin:24px;color:#111;max-width:900px}}table{{border-collapse:collapse;width:100%;margin:6px 0 18px}}
td,th{{border:1px solid #999;padding:4px 6px;text-align:left}}th{{background:#eee}}</style></head><body>
<h1>Firearms record: {e(household)}</h1><p>Printed {datetime.now():%Y-%m-%d}. Keep with insurance papers; for police reports after loss or theft.</p>
<h2>Firearms</h2><table><tr><th>Make and model</th><th>Caliber</th><th>Serial number</th><th>Location</th><th>Notes</th></tr>{rows or '<tr><td colspan=5>None</td></tr>'}</table>
<h2>Ammunition by caliber</h2><table><tr><th>Caliber</th><th>Rounds</th><th>By type</th><th>In loaded magazines</th><th>Used by</th></tr>{ammo or '<tr><td colspan=5>None</td></tr>'}</table>
<h2>Magazines</h2><table><tr><th>Firearm</th><th>Magazines</th><th>Capacity</th><th>Loaded</th><th>Needs service</th></tr>{mags or '<tr><td colspan=5>None</td></tr>'}</table>
<h2>Magazines and gear</h2><table><tr><th>Item</th><th>Type</th><th>Quantity</th><th>Location</th></tr>{gear or '<tr><td colspan=4>None</td></tr>'}</table>
</body></html>"""


def inventory_csv(items):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    w.writeheader()
    w.writerows(items)
    return buf.getvalue()


def data_json(settings, items, documents):
    docs = [{k: d[k] for k in ("label", "doc_type", "priority")} for d in documents]
    return json.dumps({"exported": datetime.now().isoformat(timespec="seconds"),
                       "household": settings, "items": items, "documents": docs}, indent=2)


def report_html(settings, items, documents):
    a = gaps.analyze(settings, items, documents)
    e = html.escape
    rows = "".join(
        f"<tr><td>{e(i['name'])}</td><td>{e(gaps.CATEGORIES.get(i['category'], i['category']))}</td>"
        f"<td>{e(gaps.quantity_text(i))}</td><td>{e(i['location'])}</td><td>{e(i['expires'])}</td></tr>"
        for i in items)
    gap_rows = "".join(
        f"<tr><td>{e(g['label'])}</td><td>{g['have']:g} / {g['need']:g} {e(g['unit'])}</td>"
        f"<td>{round(g['coverage'] * 100)}%</td></tr>" for g in a["gaps"])
    cats = "".join(f"<li>{e(c['label'])}: {c['score']}%</li>" for c in a["categories"])
    people = int(settings["adults"]) + int(settings["children"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Preparedness inventory</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#111;background:#fff}}
table{{border-collapse:collapse;width:100%;margin:8px 0 24px}}td,th{{border:1px solid #bbb;padding:4px 8px;text-align:left}}
th{{background:#eee}}h1{{margin:0 0 4px}}.meta{{color:#555}}</style></head><body>
<h1>Preparedness inventory</h1>
<p class="meta">Printed {datetime.now():%Y-%m-%d %H:%M}. Household: {people} people, {settings['pets']} pets.
Target: {a['target_days']} days. Overall readiness {a['overall']}%.
Water {a['water_days']} days, food {a['food_days']} days.</p>
<h2>Readiness by category</h2><ul>{cats}</ul>
<h2>Gaps, most urgent first</h2><table><tr><th>Need</th><th>Have / target</th><th>Coverage</th></tr>{gap_rows or '<tr><td colspan=3>No gaps</td></tr>'}</table>
<h2>Inventory</h2><table><tr><th>Item</th><th>Category</th><th>Quantity</th><th>Location</th><th>Expires</th></tr>{rows}</table>
</body></html>"""


PRINT_CSS = """body{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#111;background:#fff}
table{border-collapse:collapse;width:100%;margin:8px 0 24px}td,th{border:1px solid #bbb;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#eee}h1{margin:0 0 4px}.meta{color:#555}.box{width:18px}"""

SHOPPING_FIELDS = ["where", "label", "qty", "unit", "category", "hazard"]


def _qty(qty, unit):
    if unit == "kcal":
        return f"{qty:,} kcal"
    return f"{qty:g} {unit}".strip()


def shopping_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=SHOPPING_FIELDS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def shopping_html(rows):
    e = html.escape
    body = "".join(
        f"<tr><td class=box>&#9744;</td><td>{e(r['label'])}{' <em>(' + e(r['hazard']) + ')</em>' if r['hazard'] else ''}</td>"
        f"<td>{e(_qty(r['qty'], r['unit']))}</td><td>{e(r['where'])}</td></tr>" for r in rows)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Shopping list</title>
<style>{PRINT_CSS}</style></head><body><h1>Shopping list</h1>
<p class="meta">Printed {datetime.now():%Y-%m-%d %H:%M}. Most important first. Items in italics are for a local hazard.</p>
<table><tr><th></th><th>Buy</th><th>How much</th><th>For</th></tr>{body or '<tr><td colspan=4>Nothing to buy: every target is met.</td></tr>'}</table>
</body></html>"""


def labels_html(locations, items, base):
    """A sheet of bin labels: each a QR code (the link opens that place in BUNKR), its name and what's in it now.
    The same link can go on an NFC sticker. `base` is BUNKR's address as phones reach it."""
    from . import qr
    e = html.escape
    today = datetime.now().date().isoformat()
    cards = []
    for l in locations:
        here = [i for i in items if i["location_id"] == l["id"] or (i.get("location") or "").startswith(l["name"] + " > ")]
        soon = sorted(i["expires"] for i in here if i.get("expires") and i["expires"] >= today)
        link = f"{base.rstrip('/')}/#bin-{l['uid']}"
        cards.append(f"""<div class=label><div class=qr>{qr.svg(link, module=3)}</div><div class=txt>
<div class=name>{e(l['name'].split(' > ')[-1])}</div>{f"<div class=path>{e(' > '.join(l['name'].split(' > ')[:-1]))}</div>" if ' > ' in l['name'] else ''}
<div class=what>{len(here)} item{'' if len(here) == 1 else 's'}{f' · next expiry {e(soon[0])}' if soon else ''}</div>
<div class=how>Scan or tap for what's inside now</div></div></div>""")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Bin labels</title>
<style>{PRINT_CSS}
.sheet {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(3.4in, 1fr)); gap: .15in; }}
.label {{ display: flex; gap: .12in; align-items: center; border: 1px dashed #999; border-radius: 6px; padding: .1in; break-inside: avoid; }}
.label .qr svg {{ width: 1.25in; height: 1.25in; display: block; }}
.label .name {{ font-size: 18pt; font-weight: 700; line-height: 1.1; }}
.label .path {{ font-size: 10pt; color: #444; }}
.label .what {{ font-size: 10pt; margin-top: .05in; }}
.label .how {{ font-size: 8pt; color: #666; margin-top: .04in; }}
@media print {{ .meta {{ display: none; }} }}
</style></head><body><h1>Bin labels</h1>
<p class="meta">Print, cut along the dashed lines and stick one on each bin. The item count is as of today; scanning shows what's
there now. For an NFC sticker, write the same link (Android: "NFC tag" on Locations; on an iPhone, an app like NFC Tools).</p>
<div class=sheet>{''.join(cards) or '<p>No locations yet.</p>'}</div></body></html>"""


def packing_html(location, items, score, categories):
    """Packing list for one location: what's in it, and (for kits) what's still missing."""
    e = html.escape
    rows = "".join(
        f"<tr><td class=box>&#9744;</td><td>{e(i['name'])}</td><td>{e(gaps.quantity_text(i))}</td>"
        f"<td>{e(categories.get(i['category'], i['category']))}</td><td>{e(i['expires'])}</td></tr>"
        for i in sorted(items, key=lambda i: (i["category"], i["name"])))
    missing = ""
    if score:
        missing = "".join(f"<tr><td class=box>&#9744;</td><td>{e(g['label'])}</td>"
                          f"<td>{g['have']:g} of {g['need']:g} {e(g['unit'])}</td></tr>" for g in score["gaps"])
        missing = (f"<h2>Still missing ({score['template']}, {score['score']}% ready)</h2>"
                   f"<table><tr><th></th><th>Item</th><th>Have</th></tr>{missing or '<tr><td colspan=3>Nothing missing.</td></tr>'}</table>")
    who = f" for {e(location['person'])}" if location.get("person") else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Packing list: {e(location['name'])}</title>
<style>{PRINT_CSS}</style></head><body><h1>{e(location['name'])}</h1>
<p class="meta">Packing list{who}. Printed {datetime.now():%Y-%m-%d %H:%M}. {len(items)} items.</p>
<table><tr><th></th><th>Item</th><th>Quantity</th><th>Category</th><th>Expires</th></tr>{rows or '<tr><td colspan=5>Nothing stored here yet.</td></tr>'}</table>
{missing}</body></html>"""


README = """PREPAREDNESS KIT
================
Built {built} by BUNKR.

documents/   Encrypted 7-Zip archive of your important documents.
             Open with 7-Zip (Windows: tools/7zr.exe x documents\\<file>.7z)
             or on Linux/Mac: 7z x documents/<file>.7z
             It asks for the passphrase you chose when building the kit.
             Split archives (.7z.001, .002...) open from the .001 file.
inventory/   inventory.html (print this), inventory.csv, bunkr-data.json
library/     Offline reference libraries (.zim files) for Kiwix.
tools/       Kiwix readers and 7-Zip.

READING THE LIBRARY OFFLINE
  Windows: unzip tools/kiwix-desktop_windows_x64.zip, run kiwix-desktop.exe,
           then File > Open and pick a .zim file from library/.
  Linux:   tar xf tools/kiwix-tools_linux-x86_64.tar.gz
           ./kiwix-tools_*/kiwix-serve --port 8080 library/*.zim
           then open http://localhost:8080 in a browser.
  Mac:     same as Linux with the macos-arm64 archive.
  Phones:  install the Kiwix app and copy .zim files over.

CONTENTS
{contents}
"""
