"""Medical: each person's medications with how much is on hand, refill and expiry reminders, and emergency cards.

A medication records how many doses a day are taken and how many are on hand as of a date, so BUNKR can say how
many days are left today without anyone updating it daily. "Refilled" adds a new supply. Go-bag doses are tracked
separately with their own expiry date, since they sit unused.

Reminders (daily, once per stage per medicine): refill when 7 days are left or the refill date is near, "out" when
it runs out, and go-bag doses 30 days before they expire. They go to admins and to the person's own login, as phone
notifications (never in quiet hours) and on the Medical tab.

Emergency cards are a printable page (one card per person), also included in the family plan that the Pi and laptop
carry, so responders or family can read them with the internet down.
"""

import html
import uuid
from datetime import date, datetime, timedelta, timezone

REFILL_LEAD_DAYS = 7
BAG_LEAD_DAYS = 30
MED_FIELDS = ("person_id", "name", "dose", "how_often", "per_day", "on_hand", "on_hand_at", "bag_count", "bag_expires",
              "refill_by", "pharmacy", "rx_number", "prescriber", "critical", "cold", "notes", "active")
PERSON_MED_FIELDS = ("devices", "insurance", "pharmacy")
listeners = []


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _date(v):
    try:
        return date.fromisoformat(str(v or "")[:10])
    except ValueError:
        return None


def _num(v, default=0.0):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if x >= 0 else default


def clean(data):
    """Validate a medication from the form or API. Raises ValueError with a plain message."""
    out = {}
    out["name"] = str(data.get("name") or "").strip()[:120]
    if not out["name"]:
        raise ValueError("name the medicine")
    for f in ("dose", "how_often", "pharmacy", "rx_number", "prescriber"):
        out[f] = str(data.get(f) or "").strip()[:120]
    out["notes"] = str(data.get("notes") or "").strip()[:500]
    out["per_day"] = min(_num(data.get("per_day")), 48)
    out["on_hand"] = min(_num(data.get("on_hand")), 100000)
    out["bag_count"] = min(_num(data.get("bag_count")), 100000)
    for f in ("on_hand_at", "bag_expires", "refill_by"):
        d = _date(data.get(f))
        out[f] = d.isoformat() if d else ""
    if out["on_hand"] and not out["on_hand_at"]:
        out["on_hand_at"] = date.today().isoformat()
    for f in ("critical", "cold"):
        out[f] = 1 if data.get(f) in (True, 1, "1", "on", "true") else 0
    out["active"] = 0 if data.get("active") in (False, 0, "0", "false") else 1
    return out


def save(store, person_id, data, med_id=None):
    if not store._rows("SELECT id FROM people WHERE id=?", (person_id,)):
        raise ValueError("unknown person")
    rec = clean(data) | {"person_id": person_id, "updated_at": _now()}
    if med_id:
        sets = ", ".join(f"{k}=?" for k in rec)
        store._exec(f"UPDATE meds SET {sets} WHERE id=?", (*rec.values(), med_id))
        return med_id
    rec["uid"] = uuid.uuid4().hex
    cols = list(rec)
    return store._exec(f"INSERT INTO meds({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", tuple(rec.values()))


def refilled(store, med_id, amount, today=None):
    """A new supply arrived: today's remaining doses plus `amount`, as of today."""
    rows = store._rows("SELECT * FROM meds WHERE id=?", (med_id,))
    if not rows:
        raise ValueError("no such medicine")
    m = rows[0]
    today = today or date.today()
    left = remaining(m, today)
    new = (left if left is not None else 0) + _num(amount)
    store._exec("UPDATE meds SET on_hand=?, on_hand_at=?, refill_by='', updated_at=? WHERE id=?",
                (round(new, 1), today.isoformat(), _now(), med_id))
    return new


def remaining(m, today=None):
    """Doses left today, counting down from the last count. None when nothing was counted."""
    start = _date(m.get("on_hand_at"))
    if not start or not m.get("on_hand"):
        return None
    used = max(0, ((today or date.today()) - start).days) * (m.get("per_day") or 0)
    return max(0.0, m["on_hand"] - used)


def status(m, today=None):
    """Days left, when to refill, and what (if anything) needs doing, for one medicine."""
    today = today or date.today()
    left = remaining(m, today)
    days = int(left // m["per_day"]) if left is not None and m.get("per_day") else None
    manual = _date(m.get("refill_by"))
    refill = manual
    if days is not None:
        by_supply = today + timedelta(days=max(0, days - REFILL_LEAD_DAYS))
        refill = min(manual, by_supply) if manual else by_supply
    bag_exp = _date(m.get("bag_expires"))
    todo = []
    if days is not None and days <= 0:
        todo.append(("out", f"{m['name']} has run out" if left == 0 else f"{m['name']}: less than a day left"))
    elif days is not None and days <= REFILL_LEAD_DAYS:
        todo.append(("refill", f"Refill {m['name']}: {days} day{'s' if days != 1 else ''} left"))
    elif manual and manual <= today + timedelta(days=REFILL_LEAD_DAYS):
        todo.append(("refill", f"Refill {m['name']} by {manual:%a %b %-d}"))
    if bag_exp and bag_exp <= today + timedelta(days=BAG_LEAD_DAYS):
        todo.append(("bag", f"Go-bag {m['name']} " + ("expired" if bag_exp < today else f"expires {bag_exp:%b %-d}")))
    if m.get("bag_count") == 0 and m.get("critical"):
        todo.append(("bag_missing", f"No {m['name']} in the go-bag"))
    return {"left": None if left is None else round(left, 1), "days_left": days,
            "refill_by": refill.isoformat() if refill else "", "todo": [{"kind": k, "text": t} for k, t in todo]}


def people_with_meds(store, today=None):
    people = store._rows("SELECT id, uid, name, role, remote, user_id, blood_type, allergies, conditions, doctor, devices, "
                         "insurance, pharmacy, medications FROM people ORDER BY remote, role, name")
    meds = store._rows("SELECT * FROM meds WHERE active=1 ORDER BY critical DESC, name")
    for p in people:
        p["meds"] = [m | {"status": status(m, today)} for m in meds if m["person_id"] == p["id"]]
    return people


def due(store, today=None):
    """Everything that needs doing, worst first, each with who it's for."""
    out = []
    order = {"out": 0, "refill": 1, "bag_missing": 2, "bag": 3}
    for p in people_with_meds(store, today):
        for m in p["meds"]:
            for t in m["status"]["todo"]:
                out.append({"key": f"{m['uid']}:{t['kind']}", "kind": t["kind"], "text": f"{p['name']}: {t['text']}",
                            "person": p["name"], "user_id": p["user_id"], "med_id": m["id"], "critical": bool(m["critical"])})
    out.sort(key=lambda x: (order.get(x["kind"], 9), not x["critical"]))
    return out


def remind(store, today=None):
    """Daily: tell listeners about each due thing once (a new kind for a medicine counts as new). Returns what was sent."""
    today = today or date.today()
    sent = []
    now_due = due(store, today)
    keys = {d["key"] for d in now_due}
    with store._lock:
        # a refilled medicine can remind again next time it runs low
        for r in store._conn.execute("SELECT key FROM reminder_log WHERE key LIKE 'med:%'").fetchall():
            if r["key"][4:] not in keys:
                store._conn.execute("DELETE FROM reminder_log WHERE key=?", (r["key"],))
        store._conn.commit()
    for d in now_due:
        if store._rows("SELECT 1 FROM reminder_log WHERE key=?", ("med:" + d["key"],)):
            continue
        store._exec("INSERT INTO reminder_log(key, at) VALUES(?, ?)", ("med:" + d["key"], _now()))
        sent.append(d)
        for fn in list(listeners):
            try:
                fn("reminder", {"title": d["text"], "body": "Open the Medical tab to mark it done." if d["kind"] != "bag"
                                else "Swap in fresh doses and update the date on the Medical tab.",
                                "tag": "med-" + d["key"], "url": "/#medical", "user_id": d["user_id"], "urgent": d["kind"] == "out" and d["critical"]})
            except Exception:
                pass
    return sent


# ---------- printable emergency cards ----------
_CSS = """body{font:13px/1.35 system-ui,sans-serif;margin:16px;color:#111;background:#fff}
h1{font-size:20px;margin:0 0 4px}.meta{color:#555;margin:0 0 10px}
.card{border:2px solid #b00;border-radius:10px;padding:10px 12px;width:330px;display:inline-block;vertical-align:top;
margin:0 10px 10px 0;page-break-inside:avoid}.card h2{font-size:16px;margin:0 0 4px;color:#b00}
.card table{border-collapse:collapse;width:100%}.card td{padding:2px 4px;vertical-align:top;border-bottom:1px solid #ddd}
.card td:first-child{color:#555;width:34%}.crit{font-weight:700}@media print{body{margin:6mm}}"""


def cards_html(people, contacts, household="Our family"):
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    out_area = next((c for c in contacts if c.get("out_of_area")), None)
    cards = []
    for p in people:
        meds = p.get("meds") or []
        med_rows = "".join(f"<div class='{'crit' if m['critical'] else ''}'>{e(m['name'])} {e(m['dose'])} {e(m['how_often'])}"
                           f"{' (keep cold)' if m['cold'] else ''}{' — essential' if m['critical'] else ''}</div>" for m in meds) \
            or e(p.get("medications")) or "None listed"
        rows = [("Blood type", e(p.get("blood_type")) or "—"), ("Allergies", e(p.get("allergies")) or "None known"),
                ("Conditions", e(p.get("conditions")) or "—"), ("Medicines", med_rows), ("Devices", e(p.get("devices")) or "—"),
                ("Doctor", e(p.get("doctor")) or "—"), ("Pharmacy", e(p.get("pharmacy")) or "—"),
                ("Insurance", e(p.get("insurance")) or "—")]
        if out_area:
            rows.append(("Emergency contact", f"{e(out_area['name'])} {e(out_area['phone'])}"))
        cards.append(f"<div class='card'><h2>{e(p['name'])}</h2><table>" +
                     "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows) + "</table></div>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Medical cards</title><style>{_CSS}</style></head><body>
<h1>{e(household)}: medical cards</h1>
<p class="meta">Printed {datetime.now():%Y-%m-%d} from BUNKR. Keep one in each go-bag and wallet. Phones: also fill in Medical ID.</p>
{''.join(cards) or '<p>Add people on the Family tab.</p>'}
</body></html>"""
