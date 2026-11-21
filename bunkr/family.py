"""Family readiness: phone checklists, the maintenance calendar and the printable family plan.

The family plan follows Ready.gov "Make a Plan": meeting places, out-of-area contacts,
medical information, plus radio channels, mesh channel names and call signs. It is
printed from BUNKR, copied into every encrypted document archive, and published to
the Pi and laptop so a phone on the hotspot can open it.
"""

import html
import json
from datetime import date, datetime, timedelta, timezone

# Only what has to live on the phone beforehand. Maps, the medical library, the family plan
# and app installers stay off phones day to day: the BUNKR field Wi-Fi serves them when needed.
PHONE_CHECKS = [
    {"id": "ice", "label": "Medical ID and emergency contacts filled in", "where": "both",
     "note": "shows on the lock screen for responders, no signal needed"},
    {"id": "wea", "label": "Wireless Emergency Alerts turned on", "where": "both", "note": "a setting, not an app"},
    {"id": "hotspot", "label": "BUNKR field Wi-Fi saved, and http://bunkr.lan:8765 opened once on it", "where": "both",
     "note": "maps, the medical library, the family plan and app installers come from here when needed"},
    {"id": "power", "label": "Charging cable and power bank in the go-bag", "where": "both"},
    {"id": "mesh_ios", "label": "Mesh app installed (MeshCore or Meshtastic), if this person carries a mesh radio",
     "where": "iphone", "optional": True,
     "note": "iPhones can't install apps without internet; Android phones can install it from BUNKR when needed"},
]

DEFAULT_TASKS = [
    {"key": "rotate_water", "title": "Rotate stored water (replace or re-treat)", "every_days": 182},
    {"key": "car_winter", "title": "Switch the car kits to winter (blanket, scraper, traction, warm layers)", "yearly": "10-15"},
    {"key": "car_summer", "title": "Switch the car kits to summer (extra water, sun protection)", "yearly": "04-15"},
    {"key": "test_radios", "title": "Radio check: charge batteries, test each radio on the family channel", "every_days": 30},
    {"key": "charge_banks", "title": "Charge power banks and spare batteries", "every_days": 90},
    {"key": "chirp_installer", "title": "Download a fresh CHIRP installer and add it on the Downloads tab", "every_days": 90},
    {"key": "alarms", "title": "Test smoke and carbon monoxide alarms", "every_days": 182},
    {"key": "review_plan", "title": "Review the family plan together; update contacts and meeting places", "every_days": 365},
    {"key": "phones", "title": "Check everyone's phone against the phone readiness list", "every_days": 90},
    {"key": "rx_rotation", "title": "Rotate prescription medication in go-bags", "every_days": 182},
]


def ensure_tasks(store):
    have = {r["key"] for r in store._rows("SELECT key FROM tasks WHERE key != ''")}
    for t in DEFAULT_TASKS:
        if t["key"] not in have:
            store._exec("INSERT INTO tasks(key, title, every_days, yearly) VALUES(?,?,?,?)",
                        (t["key"], t["title"], t.get("every_days", 0), t.get("yearly", "")))


def _parse(d):
    try:
        return date.fromisoformat((d or "")[:10])
    except ValueError:
        return None


def next_due(task, today=None):
    today = today or date.today()
    last = _parse(task["last_done"])
    if task["every_days"]:
        return (last + timedelta(days=task["every_days"])) if last else today
    if task["yearly"]:
        m, d = (int(x) for x in task["yearly"].split("-"))
        start = (last + timedelta(days=1)) if last else today
        cand = date(start.year, m, d)
        return cand if cand >= start else date(start.year + 1, m, d)
    return today


def calendar(tasks, expiring_items, node_reminders, today=None, renewals=()):
    """Everything due, soonest first: maintenance tasks, food/medicine to use, node chores."""
    today = today or date.today()
    out = []
    for t in tasks:
        if not t["active"]:
            continue
        due = next_due(t, today)
        out.append({"kind": "task", "id": t["id"], "title": t["title"], "due": due.isoformat(),
                    "overdue": due <= today, "last_done": t["last_done"],
                    "repeat": f"every {t['every_days']} days" if t["every_days"] else f"each year on {t['yearly']}"})
    for i in expiring_items:
        out.append({"kind": "use", "id": i["id"], "title": f"Use or replace: {i['name']}" + (f" ({i['location']})" if i.get("location") else ""),
                    "due": i["expires"], "overdue": (_parse(i["expires"]) or today) <= today, "repeat": "expiry date"})
    for n in node_reminders:
        for r in n["reminders"]:
            out.append({"kind": "node", "id": n["id"], "title": f"{n['name']}: {r['label']}", "due": r["due"],
                        "overdue": r["overdue"], "repeat": f"every {r['every_days']} days", "reminder": r["kind"]})
    out += list(renewals)
    out.sort(key=lambda x: (x["due"], x["title"]))
    return out


def ics(entries, host="bunkr"):
    """Calendar file (iCalendar) so phones show the chores; one all-day event per item."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//BUNKR//Maintenance//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:BUNKR maintenance"]
    for e in entries:
        d = (_parse(e["due"]) or date.today())
        title = e["title"].replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
        lines += ["BEGIN:VEVENT", f"UID:{e['kind']}-{e['id']}-{d:%Y%m%d}@{host}", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
                  f"SUMMARY:{title[:200]}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def phone_status(person):
    checks = json.loads(person.get("phone_checks") or "{}")
    items = [c for c in PHONE_CHECKS if c["where"] == "both" or c["where"] == person["phone"]]
    done = sum(1 for c in items if checks.get(c["id"]) and not c.get("optional"))
    return {"done": done, "total": sum(1 for c in items if not c.get("optional")), "checked_at": person.get("phone_checked_at") or "",
            "items": [{**c, "ok": bool(checks.get(c["id"]))} for c in items]}


_CSS = """body{font:13.5px/1.4 system-ui,sans-serif;margin:20px;color:#111;background:#fff;max-width:900px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:16px;margin:18px 0 6px;border-bottom:2px solid #333;padding-bottom:2px}
table{border-collapse:collapse;width:100%;margin:4px 0 10px}td,th{border:1px solid #999;padding:4px 7px;text-align:left;vertical-align:top}
th{background:#eee}.meta{color:#555}.wallet{border:2px dashed #333;border-radius:8px;padding:8px 10px;width:330px;
display:inline-block;margin:0 10px 10px 0;font-size:12px;page-break-inside:avoid}.wallet b{font-size:13px}
.out{background:#fff6d6}@media print{body{margin:8mm}h2{page-break-after:avoid}}"""


def plan_html(people, contacts, sites, channels, info, household_name="Our family"):
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    meet = [s for s in sites if s["kind"] in ("meet_near", "meet_far")]
    places = [s for s in sites if s["kind"] in ("home", "bugout", "relative", "work")]
    kinds = {"meet_near": "Near home", "meet_far": "Out of the neighborhood", "home": "Home", "bugout": "Bug-out site",
             "relative": "Relative / friend", "work": "Work / school"}

    def addr(s):
        return e(s["address"] or (f"{s['lat']:.5f}, {s['lon']:.5f}" if s.get("lat") is not None else ""))

    meet_rows = "".join(f"<tr><td>{kinds[s['kind']]}</td><td>{e(s['name'])}</td><td>{addr(s)}</td><td>{e(s['notes'])}</td></tr>"
                        for s in meet) or "<tr><td colspan=4>Add meeting places on the Sites tab (types: meeting place near home / out of the neighborhood).</td></tr>"
    place_rows = "".join(f"<tr><td>{kinds.get(s['kind'], s['kind'])}</td><td>{e(s['name'])}</td><td>{addr(s)}</td></tr>" for s in places)
    contact_rows = "".join(
        f"<tr class=\"{'out' if c['out_of_area'] else ''}\"><td>{e(c['name'])}{' (out of area)' if c['out_of_area'] else ''}</td>"
        f"<td>{e(c['relation'])}</td><td>{e(c['phone'])}</td><td>{e(c['email'])}</td><td>{e(c['notes'])}</td></tr>" for c in contacts) \
        or "<tr><td colspan=5>No contacts yet.</td></tr>"
    people_rows = "".join(
        f"<tr><td><b>{e(p['name'])}</b></td><td>{e(p['blood_type'])}</td><td>{e(p['allergies'])}</td><td>{e(p['medications'])}</td>"
        f"<td>{e(p['conditions'])}</td><td>{e(p['doctor'])}</td></tr>" for p in people) or "<tr><td colspan=6>No household members yet.</td></tr>"
    calls = "".join(f"<tr><td>{e(p['name'])}</td><td>{e(p['call_sign'])}</td><td>{e(p['gmrs_call'])}</td></tr>"
                    for p in people if p["call_sign"] or p["gmrs_call"])
    chan_rows = "".join(
        f"<tr><td>{e(c['name'])}</td><td>{c['rx_mhz']:.4f}</td><td>{e(c['notes'])}</td></tr>" for c in channels if c["in_plan"]) \
        or "<tr><td colspan=3>Tick \"In family plan\" on the channels everyone should know (Comms tab).</td></tr>"
    out_area = next((c for c in contacts if c["out_of_area"]), None)
    near = next((s for s in meet if s["kind"] == "meet_near"), None)
    far = next((s for s in meet if s["kind"] == "meet_far"), None)
    fam_ch = next((c for c in channels if c["in_plan"]), None)
    wallets = "".join(
        f"<div class='wallet'><b>{e(p['name'])}</b><br>"
        f"Out-of-area contact: {e(out_area['name']) + ' ' + e(out_area['phone']) if out_area else '—'}<br>"
        f"Meet near home: {e(near['name']) if near else '—'}<br>Meet away: {e(far['name']) if far else '—'}<br>"
        f"Radio: {e(fam_ch['name']) + ' ' + format(fam_ch['rx_mhz'], '.4f') if fam_ch else '—'}<br>"
        f"{'Allergies: ' + e(p['allergies']) + '<br>' if p['allergies'] else ''}{'Blood type: ' + e(p['blood_type']) if p['blood_type'] else ''}</div>"
        for p in people)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Family emergency plan</title><style>{_CSS}</style></head><body>
<h1>{e(household_name)}: emergency plan</h1>
<p class="meta">Printed {datetime.now():%Y-%m-%d} from BUNKR. Keep a copy in every go-bag and car. If phones work, text
instead of calling; if they don't, use the radio channels below and go to the meeting place.</p>
<h2>Where we meet</h2><table><tr><th></th><th>Place</th><th>Address</th><th>Notes</th></tr>{meet_rows}</table>
{('<table><tr><th>Other places</th><th>Name</th><th>Address</th></tr>' + place_rows + '</table>') if place_rows else ''}
<h2>Who to call</h2><p class="meta">The out-of-area contact (highlighted) is the hub everyone checks in with:
long-distance calls often get through when local ones don't.</p>
<table><tr><th>Name</th><th>Relation</th><th>Phone</th><th>Email</th><th>Notes</th></tr>{contact_rows}</table>
<h2>Medical information</h2><table><tr><th>Name</th><th>Blood</th><th>Allergies</th><th>Medications</th><th>Conditions</th><th>Doctor</th></tr>{people_rows}</table>
<h2>Radio</h2><table><tr><th>Channel</th><th>MHz</th><th>Notes</th></tr>{chan_rows}</table>
{('<table><tr><th>Name</th><th>Ham call sign</th><th>GMRS call sign</th></tr>' + calls + '</table>') if calls else ''}
<h2>Mesh</h2><p>{e(info.get('mesh_channels')) or 'Add the mesh channel names on the Family tab.'}</p>
{('<h2>Notes</h2><p>' + e(info.get('notes')).replace(chr(10), '<br>') + '</p>') if info.get('notes') else ''}
<h2>Wallet cards</h2>{wallets or '<p class="meta">Add household members to print wallet cards.</p>'}
</body></html>"""
