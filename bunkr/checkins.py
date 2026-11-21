"""Family check-ins ("I'm safe") and family calls ("evacuate to Grandma's", "meet at the park").

A check-in is a small, append-only record: who, a status, an optional note or place, and how it arrived
(the app, the Pi's field page, MeshCore, a radio call logged by someone). A family call is one instruction to
everyone; only one is active at a time, and "all clear" ends it. Both travel between the hub and its nodes with
the normal record sync, so the Pi keeps working with the internet down and catches up later.

Anything that wants to react (phone notifications) registers in `listeners`; it is called after a change with
(event, data) and must not raise into the request.
"""

import uuid
from datetime import datetime, timezone

STATUSES = {"safe": "I'm safe", "moving": "On my way", "help": "Need help"}
CALL_KINDS = {"evacuate": "Evacuate to", "meet": "Meet at", "shelter": "Shelter in place", "checkin": "Everyone check in"}
VIA = {"app": "app", "field": "Pi Wi-Fi", "mesh": "MeshCore", "radio": "radio", "other": "other"}
NEEDS_PLACE = {"evacuate", "meet"}
LOW_BATTERY = 20   # percent: at or below this the board warns the phone may go quiet
listeners = []


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _emit(event, data):
    for fn in list(listeners):
        try:
            fn(event, data)
        except Exception:  # a broken notifier must never block a check-in
            pass


def people(store):
    return store._rows("SELECT id, uid, name, role, remote, site_id, user_id, phone FROM people ORDER BY remote, role, name")


def person_for_user(store, user):
    if not user or not user.get("id"):
        return None
    rows = store._rows("SELECT * FROM people WHERE user_id=?", (user["id"],))
    return rows[0] if rows else None


def can_call(store, user):
    """Admins and logins linked to an adult may send or end a family call."""
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    p = person_for_user(store, user)
    return bool(p and p["role"] == "adult")


def active_call(store):
    rows = store._rows("SELECT * FROM family_calls WHERE ended_at='' ORDER BY created_at DESC LIMIT 1")
    if not rows:
        return None
    c = rows[0]
    c["label"] = CALL_KINDS.get(c["kind"], c["kind"])
    c["drill"] = bool(c.get("drill"))
    return c


def battery(level, low=False):
    """(percent or None, low 0/1) from what a phone or radio said. A percent that's low counts as low."""
    try:
        level = int(round(float(level)))
        level = level if 0 <= level <= 100 else None
    except (TypeError, ValueError):
        level = None
    return level, 1 if low or (level is not None and level <= LOW_BATTERY) else 0


def add_checkin(store, person_uid, status, note="", place="", lat=None, lon=None, via="app", by_name="", at=None,
                battery_level=None, battery_low=False):
    if status not in STATUSES:
        raise ValueError("status must be one of: " + ", ".join(STATUSES))
    if via not in VIA:
        via = "other"
    rows = store._rows("SELECT uid, name FROM people WHERE uid=?", (person_uid or "",))
    if not rows:
        raise ValueError("unknown person")
    call = active_call(store)
    level, low = battery(battery_level, battery_low)
    rec = {"uid": uuid.uuid4().hex, "person_uid": rows[0]["uid"], "person_name": rows[0]["name"], "status": status,
           "note": (note or "").strip()[:300], "place": (place or "").strip()[:200],
           "lat": _coord(lat, 90), "lon": _coord(lon, 180), "call_uid": call["uid"] if call else "",
           "via": via, "by_name": (by_name or "").strip()[:80], "at": at or now(), "battery": level, "battery_low": low}
    cols = list(rec)
    store._exec(f"INSERT INTO checkins({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", tuple(rec.values()))
    _emit("checkin", rec | {"status_label": STATUSES[status]})
    return rec


def _coord(v, limit):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, 5) if -limit <= v <= limit else None


def start_call(store, kind, by, place="", address="", lat=None, lon=None, message="", drill=False):
    """Send a family call (or a drill of one). A new call replaces the one before it."""
    if kind not in CALL_KINDS:
        raise ValueError("kind must be one of: " + ", ".join(CALL_KINDS))
    place = (place or "").strip()[:120]
    if kind in NEEDS_PLACE and not place:
        raise ValueError("say where to go")
    t = now()
    for old in store._rows("SELECT uid FROM family_calls WHERE ended_at=''"):
        store._exec("UPDATE family_calls SET ended_at=?, ended_by=?, updated_at=? WHERE uid=?", (t, by, t, old["uid"]))
    rec = {"uid": uuid.uuid4().hex, "kind": kind, "place": place if kind in NEEDS_PLACE else "",
           "address": (address or "").strip()[:300] if kind in NEEDS_PLACE else "",
           "lat": _coord(lat, 90) if kind in NEEDS_PLACE else None, "lon": _coord(lon, 180) if kind in NEEDS_PLACE else None,
           "message": (message or "").strip()[:500], "created_by": by, "created_at": t, "updated_at": t, "drill": 1 if drill else 0}
    cols = list(rec)
    store._exec(f"INSERT INTO family_calls({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", tuple(rec.values()))
    _emit("call", rec | {"label": CALL_KINDS[kind], "drill": bool(drill)})
    return rec


def end_call(store, by):
    call = active_call(store)
    if not call:
        return None
    t = now()
    store._exec("UPDATE family_calls SET ended_at=?, ended_by=?, updated_at=? WHERE uid=?", (t, by, t, call["uid"]))
    _emit("all_clear", {"uid": call["uid"], "label": call["label"], "place": call["place"], "ended_by": by, "drill": call["drill"]})
    return call


def report(store, call):
    """How the family answered one call: who checked in, how, and how fast (minutes from the call)."""
    if isinstance(call, str):
        rows = store._rows("SELECT * FROM family_calls WHERE uid=?", (call,))
        if not rows:
            return None
        call = rows[0]
    start, end = call["created_at"], call["ended_at"] or "9999"
    first = {}
    for c in store._rows("SELECT * FROM checkins WHERE at >= ? AND at <= ? ORDER BY at", (start, end)):
        first.setdefault(c["person_uid"], c)
    t0 = datetime.fromisoformat(start)
    rows = []
    for p in people(store):
        c = first.get(p["uid"])
        mins = round((datetime.fromisoformat(c["at"]) - t0).total_seconds() / 60, 1) if c else None
        rows.append({"name": p["name"], "remote": p["remote"], "status": c["status"] if c else "",
                     "status_label": STATUSES.get(c["status"], "") if c else "", "via": c["via"] if c else "",
                     "minutes": mins, "by_name": c["by_name"] if c else ""})
    rows.sort(key=lambda r: (r["minutes"] is None, r["minutes"] or 0))
    times = [r["minutes"] for r in rows if r["minutes"] is not None]
    lasted = None
    if call["ended_at"]:
        lasted = round((datetime.fromisoformat(call["ended_at"]) - t0).total_seconds() / 60, 1)
    return {"uid": call["uid"], "kind": call["kind"], "label": CALL_KINDS.get(call["kind"], call["kind"]), "place": call["place"],
            "drill": bool(call.get("drill")), "created_at": start, "created_by": call["created_by"], "ended_at": call["ended_at"],
            "lasted": lasted, "people": rows, "answered": len(times), "total": len(rows),
            "slowest": max(times) if times else None, "missing": [r["name"] for r in rows if r["minutes"] is None]}


def history(store, limit=10):
    """Recent calls and drills, newest first, each with its report."""
    return [report(store, c) for c in store._rows("SELECT * FROM family_calls ORDER BY created_at DESC LIMIT ?", (limit,))]


def board(store):
    """Everyone, with their latest check-in, and whether they've answered the active call."""
    call = active_call(store)
    latest = {}
    for c in store._rows("SELECT * FROM checkins ORDER BY at"):
        latest[c["person_uid"]] = c
    site_names = {s["id"]: s["name"] for s in store._rows("SELECT id, name FROM sites")}
    out = []
    for p in people(store):
        last = latest.get(p["uid"])
        answered = bool(call and last and last["at"] >= call["created_at"])
        out.append({"id": p["id"], "uid": p["uid"], "name": p["name"], "role": p["role"], "remote": p["remote"],
                    "site": site_names.get(p["site_id"], ""), "last": last,
                    "status_label": STATUSES.get(last["status"], "") if last else "",
                    "answered": answered, "waiting": bool(call) and not answered})
    return {"call": call, "people": out, "statuses": STATUSES, "call_kinds": CALL_KINDS, "via": VIA,
            "waiting": sum(1 for p in out if p["waiting"]), "help": [p["name"] for p in out
                                                                       if p["last"] and p["last"]["status"] == "help"
                                                                       and (not call or p["answered"])]}


def recent(store, limit=30):
    return store._rows("SELECT * FROM checkins ORDER BY at DESC LIMIT ?", (limit,))


# ---------- sync ----------
CHECKIN_FIELDS = ("uid", "person_uid", "person_name", "status", "note", "place", "lat", "lon", "call_uid", "via", "by_name", "at",
                  "battery", "battery_low")
CALL_FIELDS = ("uid", "kind", "site_uid", "place", "address", "lat", "lon", "message", "created_by", "created_at",
               "ended_at", "ended_by", "updated_at", "drill")
PEOPLE_SYNC_FIELDS = ("uid", "name", "role", "remote", "phone", "mesh_name", "updated_at")


def export(store, since="", include_people=False):
    out = {"checkins": [{f: r[f] for f in CHECKIN_FIELDS} for r in
                        store._rows("SELECT * FROM checkins WHERE at > ? OR received_at > ?", (since, since))],
           "calls": [{f: r[f] for f in CALL_FIELDS} for r in
                     store._rows("SELECT * FROM family_calls WHERE updated_at > ? OR received_at > ?", (since, since))]}
    if include_people:  # hub -> node only: the roster, so the Pi's board and field page know everyone
        out["people"] = [{f: r[f] for f in PEOPLE_SYNC_FIELDS} for r in store._rows("SELECT * FROM people")]
    return out


def apply(store, incoming, from_hub=False):
    """Merge check-ins (append-only by uid) and calls (newer updated_at wins). Returns how many changed."""
    got, n = now(), 0
    events = []
    with store._lock:
        conn = store._conn
        if from_hub and isinstance(incoming.get("people"), list):
            for p in incoming["people"]:
                if not p.get("uid") or not p.get("name"):
                    continue
                row = conn.execute("SELECT id, updated_at FROM people WHERE uid=?", (p["uid"],)).fetchone()
                vals = (str(p["name"])[:120], p.get("role") if p.get("role") in ("adult", "child") else "adult",
                        1 if p.get("remote") else 0, p.get("phone") or "none", str(p.get("mesh_name") or "")[:40],
                        p.get("updated_at") or got)
                if row is None:
                    conn.execute("INSERT INTO people(uid, name, role, remote, phone, mesh_name, updated_at) VALUES(?,?,?,?,?,?,?)",
                                 (p["uid"], *vals))
                    n += 1
                elif (p.get("updated_at") or "") > (row["updated_at"] or ""):
                    conn.execute("UPDATE people SET name=?, role=?, remote=?, phone=?, mesh_name=?, updated_at=? WHERE uid=?",
                                 (*vals, p["uid"]))
                    n += 1
        for c in incoming.get("checkins") or []:
            if not c.get("uid") or c.get("status") not in STATUSES or not c.get("person_uid") or not c.get("at"):
                continue
            if conn.execute("SELECT 1 FROM checkins WHERE uid=?", (c["uid"],)).fetchone():
                continue
            vals = {f: c.get(f) for f in CHECKIN_FIELDS}
            vals["via"] = vals["via"] if vals["via"] in VIA else "other"
            vals["battery"], vals["battery_low"] = battery(c.get("battery"), c.get("battery_low"))  # older nodes don't send it
            for f in ("note", "place", "person_name", "by_name", "call_uid"):
                vals[f] = str(vals.get(f) or "")[:300]
            cols = [*vals, "received_at"]
            conn.execute(f"INSERT INTO checkins({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
                         (*vals.values(), got))
            events.append(("checkin", vals | {"status_label": STATUSES[vals["status"]]}))
            n += 1
        for c in incoming.get("calls") or []:
            if not c.get("uid") or c.get("kind") not in CALL_KINDS or not c.get("updated_at"):
                continue
            row = conn.execute("SELECT updated_at, ended_at FROM family_calls WHERE uid=?", (c["uid"],)).fetchone()
            vals = {f: c.get(f) if c.get(f) is not None else ("" if f not in ("lat", "lon") else None) for f in CALL_FIELDS}
            vals["drill"] = 1 if c.get("drill") else 0  # older nodes don't send it
            if row is None:
                cols = [*vals, "received_at"]
                conn.execute(f"INSERT INTO family_calls({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
                             (*vals.values(), got))
                if not vals["ended_at"]:
                    events.append(("call", vals | {"label": CALL_KINDS[vals["kind"]], "drill": bool(vals["drill"])}))
                n += 1
            elif vals["updated_at"] > row["updated_at"] or (vals["updated_at"] == row["updated_at"]
                                                             and vals["ended_at"] and not row["ended_at"]):
                sets = ", ".join(f"{f}=?" for f in vals if f != "uid")
                conn.execute(f"UPDATE family_calls SET {sets}, received_at=? WHERE uid=?",
                             (*(v for f, v in vals.items() if f != "uid"), got, c["uid"]))
                if vals["ended_at"] and not row["ended_at"]:
                    events.append(("all_clear", {"uid": vals["uid"], "label": CALL_KINDS[vals["kind"]], "place": vals["place"],
                                                 "ended_by": vals["ended_by"], "drill": bool(vals["drill"])}))
                n += 1
        # only one call stays active: the newest
        active = conn.execute("SELECT uid FROM family_calls WHERE ended_at='' ORDER BY created_at DESC").fetchall()
        for old in active[1:]:
            conn.execute("UPDATE family_calls SET ended_at=?, ended_by='newer call', updated_at=? WHERE uid=?", (got, got, old["uid"]))
        conn.commit()
    for ev in events:
        _emit(*ev)
    return n
