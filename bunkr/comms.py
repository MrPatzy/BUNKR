"""Community and comms: licenses and skills per person, neighbors, radio nets with a check-in log, ICS-213 messages.

Licenses (ham, GMRS) and certificates (CPR, first aid, CERT...) belong to a household person. One with an expiry date is
reminded about 60 days before it, then weekly once it has expired, to the person's own login and to admins (the same
way document renewals work), until someone marks it done on the maintenance list or sets the new date.

Neighbors are the people around us and what they can do: a short pick list of skills plus free text. They are family
visible, go to field nodes with sync, show as pins on the situation map when they have a position, and are kept out
of the AI connector on purpose (they never agreed to that).

Nets are weekly (or daily) radio nets. Their next time shows on the maintenance calendar. The check-in log records who
checked in to which net and how they were heard.

ICS-213 is the standard general message form. Messages are kept as a log (sent or received), print in the form's own
layout, and copy as plain ASCII text for pasting into Winlink Express or JS8Call. BUNKR never keys a radio.
"""

import html
import json
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone

LEAD_DAYS = 60
listeners = []

LICENCE_KINDS = {"ham": "Ham radio license", "gmrs": "GMRS license", "cpr": "CPR", "first_aid": "First aid",
                 "cert": "CERT", "stop_bleed": "Stop the Bleed", "wfa": "Wilderness first aid", "other": "Other"}
HAM_CLASSES = ("Technician", "General", "Extra")
SKILLS = {"medical": "Medical (doctor, nurse, EMT)", "first_aid": "First aid / CPR", "ham": "Ham radio", "gmrs": "GMRS radio",
          "electric": "Electrical", "plumbing": "Plumbing", "building": "Building / carpentry", "mechanic": "Mechanic",
          "chainsaw": "Chainsaw / trees", "power": "Generator / solar", "fire": "Firefighting", "security": "Security / police",
          "childcare": "Childcare", "animals": "Animals / vet", "food": "Garden / food", "language": "Speaks another language",
          "equipment": "Heavy equipment", "computers": "Computers / IT"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MODES = ("FM", "NFM", "DMR", "D-STAR", "C4FM", "SSB", "CW", "Digital", "Winlink", "JS8Call", "Mesh", "Other")

LICENCE_FIELDS = ("person_id", "kind", "label", "class", "callsign", "number", "expires", "notes")
NEIGHBOUR_FIELDS = ("name", "place", "lat", "lon", "phone", "callsign", "contact", "skills", "skills_other", "notes")
NET_FIELDS = ("name", "freq", "mode", "weekday", "time", "net_control", "notes", "active")
ICS_FIELDS = ("direction", "incident", "to_name", "to_position", "from_name", "from_position", "subject", "msg_date", "msg_time",
              "message", "approved_name", "approved_position", "reply", "reply_name", "reply_position", "reply_at")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _date(v):
    try:
        return date.fromisoformat(str(v or "")[:10])
    except ValueError:
        return None


def _text(v, n=200):
    return str(v if v is not None else "").strip()[:n]


def _coord(v, limit):
    if v in (None, ""):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError("latitude and longitude must be numbers")
    if not -limit <= x <= limit:
        raise ValueError("latitude or longitude out of range")
    return round(x, 6)


def _callsign(v):
    c = _text(v, 12).upper().replace(" ", "")
    if c and not all(ch.isalnum() or ch == "/" for ch in c):
        raise ValueError("a callsign is letters and numbers only")
    return c


def _insert_or_update(store, table, rec, row_id=None):
    rec = rec | {"updated_at": _now()}
    if row_id:
        if not store._rows(f"SELECT id FROM {table} WHERE id=?", (row_id,)):
            raise LookupError("not found")
        store._exec(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in rec)} WHERE id=?", (*rec.values(), row_id))
        return row_id
    rec["uid"] = uuid.uuid4().hex
    return store._exec(f"INSERT INTO {table}({', '.join(rec)}) VALUES({', '.join('?' for _ in rec)})", tuple(rec.values()))


# ---------- licenses and certificates ----------
def clean_licence(data):
    kind = _text(data.get("kind"), 20)
    if kind not in LICENCE_KINDS:
        raise ValueError("pick what kind of license or certificate it is")
    out = {"kind": kind, "label": _text(data.get("label"), 80), "number": _text(data.get("number"), 40),
           "notes": _text(data.get("notes"), 500), "callsign": _callsign(data.get("callsign")), "class": ""}
    if kind == "other" and not out["label"]:
        raise ValueError("say what it is")
    if kind == "ham":
        cls = _text(data.get("class"), 20).title()
        if cls and cls not in HAM_CLASSES:
            raise ValueError("ham class is Technician, General or Extra")
        out["class"] = cls
    exp = _text(data.get("expires"), 10)
    if exp and not _date(exp):
        raise ValueError("the expiry date must be a date (YYYY-MM-DD)")
    out["expires"] = exp
    return out


def save_licence(store, person_id, data, row_id=None):
    if not store._rows("SELECT id FROM people WHERE id=?", (person_id,)):
        raise ValueError("unknown person")
    rec = clean_licence(data) | {"person_id": person_id}
    if row_id:  # a new date starts the reminders again
        old = store._rows("SELECT expires FROM licences WHERE id=?", (row_id,))
        if old and old[0]["expires"] != rec["expires"]:
            rec["renewed_at"] = ""
    return _insert_or_update(store, "licences", rec, row_id)


def licence_title(lic):
    name = lic["label"] if lic["kind"] == "other" else LICENCE_KINDS.get(lic["kind"], lic["kind"])
    if lic["kind"] == "ham" and lic.get("class"):
        name = f"Ham radio license ({lic['class']})"
    return name


def licence_status(lic, today=None):
    """None (no date), or {"state": later|soon|expired|done, "days": days until it expires, "due": "YYYY-MM-DD"}."""
    due = _date(lic.get("expires"))
    if not due:
        return None
    today = today or date.today()
    days = (due - today).days
    done = (lic.get("renewed_at") or "") >= (due - timedelta(days=LEAD_DAYS)).isoformat()
    state = "done" if done else "later" if days > LEAD_DAYS else "soon" if days >= 0 else "expired"
    return {"state": state, "days": days, "due": due.isoformat()}


def licences(store, today=None):
    rows = store._rows("SELECT l.*, p.name AS person, p.user_id FROM licences l JOIN people p ON p.id = l.person_id "
                       "ORDER BY p.name, l.kind")
    for r in rows:
        r["title"] = licence_title(r)
        r["status"] = licence_status(r, today)
    return rows


def licences_due(store, today=None):
    out = [r for r in licences(store, today) if r["status"] and r["status"]["state"] in ("soon", "expired")]
    out.sort(key=lambda r: r["status"]["days"])
    return out


def _when(st):
    d = _date(st["due"])
    nice = f"{d:%b} {d.day}, {d.year}"
    if st["state"] == "expired":
        n = -st["days"]
        return f"expired {nice} ({n} day{'s' if n != 1 else ''} ago)"
    return "expires today" if st["days"] == 0 else f"expires {nice} (in {st['days']} day{'s' if st['days'] != 1 else ''})"


def licence_message(r):
    st = r["status"]
    what = f"{r['person']}'s {r['title']}" + (f" {r['callsign']}" if r.get("callsign") else "")
    title = f"Expired: {what}" if st["state"] == "expired" else f"Renew soon: {what}"
    body = f"{what} {_when(st)}. Mark it done on the maintenance list, or set the new date on the Comms tab."
    return {"title": title[:200], "body": body[:500], "tag": f"licence-{r['id']}", "url": "/#comms", "user_id": r.get("user_id")}


def remind(store, today=None):
    """Daily: once when it's 60 days out, then weekly once expired, until it's done or has a new date."""
    today = today or date.today()
    sent = []
    for r in licences_due(store, today):
        st = r["status"]
        key = f"lic:{r['id']}:{st['due']}:" + ("soon" if st["state"] == "soon" else f"late{(-st['days'] - 1) // 7}")
        if store._rows("SELECT 1 FROM reminder_log WHERE key=?", (key,)):
            continue
        store._exec("INSERT INTO reminder_log(key, at) VALUES(?, ?)", (key, _now()))
        sent.append(r)
        for fn in list(listeners):
            try:
                fn("reminder", licence_message(r))
            except Exception:
                pass
    return sent


def mark_renewed(store, lic_id, today=None):
    if not store._rows("SELECT id FROM licences WHERE id=?", (lic_id,)):
        raise LookupError("no such license")
    store._exec("UPDATE licences SET renewed_at=?, updated_at=? WHERE id=?", ((today or date.today()).isoformat(), _now(), lic_id))


def licence_calendar(store, today=None):
    """For the maintenance calendar: each license coming up or expired, with a Done button."""
    return [{"kind": "licence", "id": r["id"], "title": f"Renew: {r['person']}'s {r['title']}", "due": r["status"]["due"],
             "overdue": r["status"]["state"] == "expired", "repeat": "expiry date"} for r in licences_due(store, today)]


def callsigns(store):
    """Everyone's radio callsigns: from license records, plus the ones typed on the Family tab."""
    out, seen = [], set()
    for r in licences(store):
        if r["kind"] in ("ham", "gmrs") and r["callsign"]:
            out.append({"name": r["person"], "kind": r["kind"], "class": r["class"], "callsign": r["callsign"]})
            seen.add(r["callsign"])
    for p in store._rows("SELECT name, call_sign, gmrs_call FROM people ORDER BY name"):
        for kind, cs in (("ham", p["call_sign"]), ("gmrs", p["gmrs_call"])):
            cs = (cs or "").strip().upper()
            if cs and cs not in seen:
                out.append({"name": p["name"], "kind": kind, "class": "", "callsign": cs})
                seen.add(cs)
    return out


# ---------- neighbors ----------
def clean_neighbour(data):
    out = {"name": _text(data.get("name"), 120)}
    if not out["name"]:
        raise ValueError("name the neighbor")
    for f in ("place", "phone", "contact"):
        out[f] = _text(data.get(f), 200)
    out["callsign"] = _callsign(data.get("callsign"))
    out["notes"] = _text(data.get("notes"), 1000)
    out["skills_other"] = _text(data.get("skills_other"), 300)
    skills = data.get("skills") or []
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",")]
    out["skills"] = json.dumps([s for s in dict.fromkeys(skills) if s in SKILLS])
    out["lat"], out["lon"] = _coord(data.get("lat"), 90), _coord(data.get("lon"), 180)
    if (out["lat"] is None) != (out["lon"] is None):
        raise ValueError("give both latitude and longitude, or neither")
    return out


def save_neighbour(store, data, row_id=None):
    return _insert_or_update(store, "neighbours", clean_neighbour(data), row_id)


def neighbours(store):
    rows = store._rows("SELECT * FROM neighbours ORDER BY name")
    for r in rows:
        try:
            r["skills"] = [s for s in json.loads(r["skills"] or "[]") if s in SKILLS]
        except ValueError:
            r["skills"] = []
        r["skill_labels"] = [SKILLS[s] for s in r["skills"]] + ([r["skills_other"]] if r["skills_other"] else [])
    return rows


def neighbour_features(store):
    """GeoJSON points for the situation map."""
    return [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [n["lon"], n["lat"]]},
             "properties": {"kind": "neighbour", "title": n["name"], "summary": ", ".join(n["skill_labels"])[:300],
                            "place": n["place"]}}
            for n in neighbours(store) if n["lat"] is not None and n["lon"] is not None]


# ---------- nets ----------
def clean_net(data):
    out = {"name": _text(data.get("name"), 120)}
    if not out["name"]:
        raise ValueError("name the net")
    out["freq"] = _text(data.get("freq"), 80)
    out["mode"] = _text(data.get("mode"), 20) or "FM"
    out["net_control"] = _text(data.get("net_control"), 120)
    out["notes"] = _text(data.get("notes"), 500)
    try:
        wd = int(data.get("weekday") if data.get("weekday") not in (None, "") else -1)
    except (TypeError, ValueError):
        raise ValueError("pick a day")
    if not -1 <= wd <= 6:
        raise ValueError("pick a day")
    out["weekday"] = wd
    t = _text(data.get("time"), 5)
    if t:
        try:
            t = datetime.strptime(t, "%H:%M").strftime("%H:%M")
        except ValueError:
            raise ValueError("time looks like 19:30")
    out["time"] = t
    out["active"] = 0 if data.get("active") in (False, 0, "0", "false") else 1
    return out


def save_net(store, data, row_id=None):
    return _insert_or_update(store, "nets", clean_net(data), row_id)


def net_when(n):
    day = "Every day" if n["weekday"] < 0 else f"{WEEKDAYS[n['weekday']]}s"
    return day + (f" {n['time']}" if n.get("time") else "")


def next_net(n, today=None):
    """The date of the next session (today counts)."""
    today = today or date.today()
    if n["weekday"] < 0:
        return today
    return today + timedelta(days=(n["weekday"] - today.weekday()) % 7)


def nets(store, today=None):
    rows = store._rows("SELECT * FROM nets ORDER BY active DESC, weekday, time, name")
    for n in rows:
        n["when"] = net_when(n)
        n["next"] = next_net(n, today).isoformat()
    return rows


def calendar_entries(store, today=None):
    """For the maintenance calendar: each active net's next session."""
    return [{"kind": "net", "id": n["id"], "title": f"Net: {n['name']}" + (f" ({n['freq']})" if n["freq"] else "")
             + (f" at {n['time']}" if n["time"] else ""), "due": n["next"], "overdue": False,
             "repeat": n["when"].lower() if n["weekday"] < 0 else "every " + WEEKDAYS[n["weekday"]]}
            for n in nets(store, today) if n["active"]]


def log_checkin(store, data, by_name=""):
    who = _text(data.get("who"), 120)
    if not who:
        raise ValueError("who checked in?")
    net_id = data.get("net_id")
    net = store._rows("SELECT id, name FROM nets WHERE id=?", (int(net_id),)) if str(net_id or "").isdigit() else []
    name = net[0]["name"] if net else _text(data.get("net_name"), 120)
    if not name:
        raise ValueError("which net?")
    at = _text(data.get("at"), 25)
    if at:
        try:
            at = datetime.fromisoformat(at).isoformat(timespec="minutes")
        except ValueError:
            raise ValueError("date and time look like 2026-11-22T19:30")
    else:
        at = datetime.now().isoformat(timespec="minutes")
    return store._exec("INSERT INTO net_log(net_id, net_name, at, who, signal, notes, by_name) VALUES(?,?,?,?,?,?,?)",
                       (net[0]["id"] if net else None, name, at, who, _text(data.get("signal"), 40), _text(data.get("notes"), 300),
                        _text(by_name, 60)))


def net_log(store, limit=50):
    return store._rows("SELECT * FROM net_log ORDER BY at DESC, id DESC LIMIT ?", (limit,))


# ---------- ICS-213 general message ----------
def clean_ics213(data):
    out = {f: _text(data.get(f), 4000 if f in ("message", "reply") else 120) for f in ICS_FIELDS}
    if out["direction"] not in ("sent", "received"):
        out["direction"] = "sent"
    if not out["message"]:
        raise ValueError("write the message")
    if not out["to_name"] or not out["from_name"]:
        raise ValueError("fill in To and From")
    if out["msg_date"] and not _date(out["msg_date"]):
        raise ValueError("date looks like 2026-11-22")
    now = datetime.now()
    out["msg_date"] = out["msg_date"] or now.date().isoformat()
    out["msg_time"] = (out["msg_time"] or now.strftime("%H:%M"))[:5]
    return out


def save_ics213(store, data, by="", row_id=None):
    rec = clean_ics213(data)
    if row_id:
        if not store._rows("SELECT id FROM ics213 WHERE id=?", (row_id,)):
            raise LookupError("no such message")
        store._exec(f"UPDATE ics213 SET {', '.join(f'{k}=?' for k in rec)} WHERE id=?", (*rec.values(), row_id))
        return row_id
    rec |= {"uid": uuid.uuid4().hex, "created_by": _text(by, 60), "created_at": _now()}
    return store._exec(f"INSERT INTO ics213({', '.join(rec)}) VALUES({', '.join('?' for _ in rec)})", tuple(rec.values()))


def messages(store, limit=50):
    rows = store._rows("SELECT * FROM ics213 ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
    for r in rows:
        r["text"] = ics213_text(r)
    return rows


def _ascii(s):
    """Plain ASCII for radio text modes: accents dropped, smart quotes and dashes made plain, other symbols removed."""
    s = str(s or "").replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"') \
        .replace("–", "-").replace("—", "-").replace("…", "...").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(ch for ch in s if ch == "\n" or 32 <= ord(ch) < 127)


def _who(name, position):
    return ", ".join(x for x in (name, position) if x)


def ics213_text(m):
    """The message as plain text in the form's numbered order, ready for Winlink Express or JS8Call."""
    lines = ["GENERAL MESSAGE (ICS 213)"]
    if m.get("incident"):
        lines.append(f"1. INCIDENT: {m['incident']}")
    lines += [f"2. TO: {_who(m.get('to_name'), m.get('to_position'))}",
              f"3. FROM: {_who(m.get('from_name'), m.get('from_position'))}",
              f"4. SUBJECT: {m.get('subject') or ''}",
              f"5. DATE: {m.get('msg_date') or ''}",
              f"6. TIME: {(m.get('msg_time') or '').replace(':', '')}",
              "7. MESSAGE:", (m.get("message") or "").strip()]
    if m.get("approved_name") or m.get("approved_position"):
        lines.append(f"8. APPROVED BY: {_who(m.get('approved_name'), m.get('approved_position'))}")
    if m.get("reply"):
        lines += ["REPLY:", m["reply"].strip()]
        by = _who(m.get("reply_name"), m.get("reply_position"))
        if by or m.get("reply_at"):
            lines.append("REPLIED BY: " + by + (f" DATE/TIME: {m['reply_at']}" if m.get("reply_at") else ""))
    return _ascii("\n".join(lines)).strip() + "\n"


_ICS_CSS = """body{font:13px/1.35 Arial,Helvetica,sans-serif;margin:16px;color:#000;background:#fff}
.form{max-width:780px;border:2px solid #000}.t{font-weight:700;font-size:15px;text-align:center;padding:6px;border-bottom:2px solid #000}
table{border-collapse:collapse;width:100%}td{border:1px solid #000;padding:4px 6px;vertical-align:top}
.l{font-size:11px;font-weight:700;display:block}.msg{white-space:pre-wrap;min-height:220px}.rep{white-space:pre-wrap;min-height:120px}
.foot{font-size:11px;padding:4px 6px}@media print{body{margin:8mm}.noprint{display:none}}"""


def ics213_html(m):
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    cell = lambda label, v, extra="": f"<td{extra}><span class='l'>{label}</span>{e(v)}</td>"  # noqa: E731
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ICS 213 {e(m.get('subject'))}</title><style>{_ICS_CSS}</style></head><body>
<p class="noprint"><button onclick="print()">Print</button></p>
<div class="form"><div class="t">GENERAL MESSAGE (ICS 213)</div>
<table>
<tr>{cell("1. Incident Name (Optional):", m.get("incident"), " colspan='2'")}</tr>
<tr>{cell("2. To (Name and Position):", _who(m.get("to_name"), m.get("to_position")), " colspan='2'")}</tr>
<tr>{cell("3. From (Name and Position):", _who(m.get("from_name"), m.get("from_position")), " colspan='2'")}</tr>
<tr>{cell("4. Subject:", m.get("subject"))}{cell("5. Date:", m.get("msg_date"))}</tr>
<tr><td></td>{cell("6. Time:", (m.get("msg_time") or "").replace(":", ""))}</tr>
<tr><td colspan="2"><span class="l">7. Message:</span><div class="msg">{e(m.get("message"))}</div></td></tr>
<tr>{cell("8. Approved by: Name:", m.get("approved_name"))}{cell("Position/Title:", m.get("approved_position"))}</tr>
<tr><td colspan="2"><span class="l">Signature:</span>&nbsp;</td></tr>
<tr><td colspan="2"><span class="l">9. Reply:</span><div class="rep">{e(m.get("reply"))}</div></td></tr>
<tr>{cell("10. Replied by: Name:", m.get("reply_name"))}{cell("Position/Title:", m.get("reply_position"))}</tr>
<tr><td><span class="l">Signature:</span>&nbsp;</td>{cell("Date/Time:", m.get("reply_at"))}</tr>
</table>
<div class="foot">ICS 213 · {e("Received" if m.get("direction") == "received" else "Sent")} · kept in BUNKR</div></div>
</body></html>"""


# ---------- the Comms tab ----------
def view(store, today=None):
    return {"licences": licences(store, today), "due": [r | {"message": licence_message(r)["title"]} for r in licences_due(store, today)],
            "neighbours": neighbours(store), "nets": nets(store, today), "net_log": net_log(store), "messages": messages(store),
            "people": store._rows("SELECT id, name, user_id, call_sign, gmrs_call FROM people ORDER BY remote, role, name"),
            "kinds": LICENCE_KINDS, "classes": list(HAM_CLASSES), "skills": SKILLS, "weekdays": list(WEEKDAYS),
            "modes": list(MODES), "lead_days": LEAD_DAYS}


# ---------- sync to field nodes (hub -> node only; the hub is where these are changed) ----------
SYNC = {"neighbours": NEIGHBOUR_FIELDS, "nets": NET_FIELDS}
LICENCE_SYNC = ("kind", "label", "class", "callsign", "number", "expires", "renewed_at", "notes")


def export(store):
    out = {t: [{f: r[f] for f in ("uid", "updated_at") + fields} for r in store._rows(f"SELECT * FROM {t}")]
           for t, fields in SYNC.items()}
    out["licences"] = [{f: r[f] for f in ("uid", "updated_at") + LICENCE_SYNC} | {"person_uid": r["person_uid"]}
                       for r in store._rows("SELECT l.*, p.uid AS person_uid FROM licences l JOIN people p ON p.id = l.person_id")]
    return {"comms": out}


def apply(store, incoming):
    """Replace this node's copy with the hub's. Older hubs send nothing, and nothing changes."""
    data = incoming.get("comms")
    if not isinstance(data, dict):
        return 0
    n = 0
    with store._lock:
        conn = store._conn
        for table, fields in list(SYNC.items()) + [("licences", LICENCE_SYNC)]:
            rows = data.get(table)
            if not isinstance(rows, list):
                continue
            conn.execute(f"DELETE FROM {table}")
            for r in rows:
                if not isinstance(r, dict) or not r.get("uid"):
                    continue
                vals = {f: r.get(f) for f in ("uid", "updated_at") + fields}
                if table == "licences":
                    p = conn.execute("SELECT id FROM people WHERE uid=?", (r.get("person_uid") or "",)).fetchone()
                    if not p:
                        continue
                    vals["person_id"] = p["id"]
                vals = {k: ("" if v is None and k not in ("lat", "lon") else v) for k, v in vals.items()}
                conn.execute(f"INSERT INTO {table}({', '.join(vals)}) VALUES({', '.join('?' for _ in vals)})", tuple(vals.values()))
                n += 1
        conn.commit()
    return n
