"""The tools an AI assistant gets through the optional BUNKR connector. Narrow on purpose; see mcp.py.

Firearms (and their serial numbers) are never visible or editable here, whatever the user's role.
Every change is written to mcp_log with enough to undo it.
"""

import json
from datetime import datetime

from . import gaps
from .mcp import ToolError

SIZE_UNITS = ["ml", "L", "fl oz", "gal"]


def _obj(props=None, required=()):
    s = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        s["required"] = list(required)
    return s


def _ro(title):
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}


def _rw(title, idempotent=False):
    return {"title": title, "readOnlyHint": False, "destructiveHint": False, "idempotentHint": idempotent,
            "openWorldHint": False}


def make_tools(srv):
    from . import brief, checkins, situation
    store = srv.store

    def items_for(user):
        return [i for i in gaps.visible(store.items(), user["role"]) if i["category"] not in gaps.SENSITIVE]

    def item_by_id(item_id, user):
        it = next((i for i in items_for(user) if i["id"] == item_id), None)
        if not it:
            raise ToolError(f"no item with id {item_id} (use search_inventory to find ids)")
        return it

    def find_location(name):
        if name in (None, ""):
            return None
        locs = store.locations()
        for loc in locs:
            if loc["name"].lower() == str(name).strip().lower():
                return loc["id"]
        close = [l["name"] for l in locs if str(name).strip().lower() in l["name"].lower()]
        raise ToolError(f"no location named '{name}'. " + (f"Did you mean: {', '.join(close)}? " if close else "") +
                        "Known locations: " + ", ".join(l["name"] for l in locs) + ". (New locations are added in BUNKR itself.)")

    def log(user, tool, args, result, undo):
        store._exec("INSERT INTO mcp_log(at, username, tool, args, result, undo) VALUES(?,?,?,?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"), user["username"], tool, json.dumps(args)[:2000],
                     result[:500], json.dumps(undo) if undo else ""))

    def fmt(i):
        bits = [f"[{i['id']}] {i['name']}", gaps.quantity_text(i), gaps.CATEGORIES.get(i["category"], i["category"])]
        if i.get("location"):
            bits.append("at " + i["location"])
        if i.get("expires"):
            bits.append("expires " + i["expires"])
        return " · ".join(bits)

    def per_unit(category, essential, size, size_unit):
        if size in (None, ""):
            return 0.0, ""
        size = float(size)
        if size < 0:
            raise ToolError("size can't be negative")
        if essential == "cash":
            return size, ""
        if essential:
            return 0.0, ""
        if category == "water":
            unit = size_unit or "gal"
            if unit not in gaps.GAL_PER:
                raise ToolError(f"size_unit must be one of {', '.join(SIZE_UNITS)}")
            return size * gaps.GAL_PER[unit], unit
        if category == "food":
            return size, ""
        return 0.0, ""

    def check_item_fields(a):
        cat = a.get("category")
        if cat in gaps.SENSITIVE:
            raise ToolError("firearms can only be added in BUNKR itself, not through the AI connector")
        if cat is not None and cat not in gaps.CATEGORIES:
            raise ToolError("category must be one of: " + ", ".join(k for k in gaps.CATEGORIES if k not in gaps.SENSITIVE))
        ess = a.get("counts_toward")
        if ess and ess not in gaps.CHECKLIST_IDS:
            raise ToolError(f"unknown counts_toward '{ess}'; see list_categories")

    # ---------------- reads ----------------
    def search_inventory(a, user):
        q = (a.get("query") or "").lower().strip()
        rows = items_for(user)
        if a.get("category"):
            rows = [i for i in rows if i["category"] == a["category"]]
        if a.get("location"):  # a room also finds what's in its sub-locations
            want = a["location"].strip().lower()
            rows = [i for i in rows if (i.get("location") or "").lower() == want
                    or (i.get("location") or "").lower().startswith(want + " > ")]
        if q:
            rows = [i for i in rows if q in f"{i['name']} {i.get('notes', '')} {i.get('location', '')}".lower()]
        if not rows:
            return "No matching items."
        lim = min(int(a.get("limit") or 50), 200)
        out = [fmt(i) for i in rows[:lim]]
        return "\n".join(out) + (f"\n…and {len(rows) - lim} more" if len(rows) > lim else "")

    def list_locations(a, user):
        locs = store.locations()
        return "\n".join(f"{l['name']} ({l['kind']}{', counts toward home supply' if l['counts_home'] else ''})"
                         for l in locs) or "No locations yet."  # names are full paths: Room > Spot > Container

    def list_categories(a, user):
        lines = ["Categories: " + ", ".join(f"{k} ({v})" for k, v in gaps.CATEGORIES.items() if k not in gaps.SENSITIVE),
                 "counts_toward (checklist items):"]
        lines += [f"  {e['id']}: {e['label']} [{e['category']}]" for e in gaps.CHECKLIST if e["category"] not in gaps.SENSITIVE]
        return "\n".join(lines)

    def readiness(a, user):
        st = srv.full_state(user)
        an = st["analysis"]
        lines = [f"Overall readiness: {an['overall']}%",
                 "By category: " + ", ".join(f"{c['label']} {c['score']}%" for c in an["categories"])]
        for l in an["lines"]:
            if l["id"] in ("water", "food"):
                lines.append(f"{l['label']}: {l['have']:g} of {l['need']:g} {l['unit']} ({l['detail']})")
        top = [g for g in an["gaps"]][:8]
        if top:
            lines.append("Biggest gaps: " + "; ".join(f"{g['label']} ({g['have']:g}/{g['need']:g} {g['unit']})".replace(" )", ")") for g in top))
        if an.get("expired"):
            lines.append("Expired: " + ", ".join(i["name"] for i in an["expired"][:10]))
        if an.get("expiring"):
            lines.append("Expiring soon: " + ", ".join(f"{i['name']} ({i['expires']})" for i in an["expiring"][:10]))
        return "\n".join(lines)

    def shopping_list(a, user):
        rows = srv.full_state(user)["shopping"][: min(int(a.get("limit") or 25), 100)]
        return "\n".join(f"{r['label']}: {r['qty']:g} {r['unit']} for {r['where']}".replace("  ", " ") for r in rows) or "Nothing to buy."

    def maintenance_tasks(a, user):
        from . import family
        rows = store._rows("SELECT * FROM tasks WHERE active=1 ORDER BY title")
        rows.sort(key=lambda t: family.next_due(t))
        return "\n".join(f"[{t['id']}] {t['title']} · due {family.next_due(t).isoformat()}"
                         + (f" · last done {t['last_done']}" if t["last_done"] else "") for t in rows) or "No tasks."

    def channel_plan(a, user):
        q = (a.get("query") or "").lower()
        rows = store._rows("SELECT * FROM channels WHERE slot IS NOT NULL AND slot < 200 ORDER BY slot")
        if q:
            rows = [c for c in rows if q in f"{c['name']} {c['system']} {c['notes']} {c['slot']}".lower()]
        from .radioplan import tone_label, tx_freq
        out = []
        for c in rows[:120]:
            tx = tx_freq(c)
            out.append(f"{c['slot']} {c['name']}: RX {c['rx_mhz']:.4f}" + (f", TX {tx:.4f}" if tx and abs(tx - c['rx_mhz']) > 1e-6 else "")
                       + (f", TX tone {tone_label(c['tx_tone'])}" if c["tx_tone"] else "")
                       + (f", RX tone {tone_label(c['rx_tone'])}" if c["rx_tone"] else "")
                       + (f" · {c['system']}" if c["system"] else "") + (f" · {c['notes']}" if c["notes"] else ""))
        return "\n".join(out) or "No matching channels."

    def recent_changes(a, user):
        rows = store._rows("SELECT * FROM mcp_log ORDER BY id DESC LIMIT 20")
        return "\n".join(f"[{r['id']}] {r['at']} {r['username']} {r['tool']}: {r['result']}"
                         + (" (undoable)" if r["undo"] else "") for r in rows) or "No changes made through the connector yet."

    # ---------------- writes ----------------
    def build_item(a):
        check_item_fields(a)
        cat, ess = a.get("category") or "other", a.get("counts_toward") or ""
        if ess and next((e["category"] for e in gaps.CHECKLIST if e["id"] == ess), cat) != cat:
            cat = next(e["category"] for e in gaps.CHECKLIST if e["id"] == ess)
        pu, su = per_unit(cat, ess, a.get("size"), a.get("size_unit"))
        data = {"name": a["name"], "category": cat, "quantity": a.get("quantity", 1), "unit": a.get("unit") or "",
                "pack_size": a.get("pack_size") or 1, "pack_unit": a.get("pack_unit") or "", "per_unit": pu,
                "size_unit": su, "essential": ess, "location_id": find_location(a.get("location")),
                "expires": a.get("expires") or "", "notes": a.get("notes") or ""}
        try:
            return srv.clean_item(data)
        except srv.ApiError as exc:
            raise ToolError(f"{a.get('name', 'item')}: {exc}")

    def add_item(a, user):
        new_id = store.save_item(build_item(a))
        msg = "Added " + fmt(item_by_id(new_id, user))
        log(user, "add_item", a, msg, {"delete_items": [new_id]})
        return msg

    def add_items(a, user):
        rows = a["items"]
        if not 1 <= len(rows) <= 50:
            raise ToolError("send 1 to 50 items at a time")
        if a.get("location"):  # default location for the whole list
            rows = [{"location": a["location"], **r} for r in rows]
        cleaned = [build_item(r) for r in rows]  # check everything before saving anything
        ids = [store.save_item(c) for c in cleaned]
        lines = [fmt(item_by_id(i, user)) for i in ids]
        msg = f"Added {len(ids)} items:\n" + "\n".join(lines)
        log(user, "add_items", a, f"Added {len(ids)} items: " + ", ".join(r["name"] for r in rows), {"delete_items": ids})
        return msg

    def update_item(a, user):
        check_item_fields(a)
        it = item_by_id(int(a["id"]), user)
        before = {k: it[k] for k in ("name", "category", "quantity", "unit", "pack_size", "pack_unit", "per_unit",
                                     "size_unit", "essential", "location_id", "expires", "notes")}
        data = dict(before)
        for k in ("name", "quantity", "unit", "pack_size", "pack_unit", "expires", "notes"):
            if k in a:
                data[k] = a[k]
        if "category" in a:
            data["category"] = a["category"]
        if "counts_toward" in a:
            data["essential"] = a["counts_toward"] or ""
        if "location" in a:
            data["location_id"] = find_location(a["location"])
        if "size" in a:
            data["per_unit"], data["size_unit"] = per_unit(data["category"], data["essential"], a["size"], a.get("size_unit"))
        try:
            clean = srv.clean_item(data)
        except srv.ApiError as exc:
            raise ToolError(str(exc))
        store.save_item(clean, it["id"])
        msg = "Updated " + fmt(item_by_id(it["id"], user))
        log(user, "update_item", a, msg, {"restore_item": it["id"], "fields": before})
        return msg

    def move_items(a, user):
        loc = find_location(a["location"])
        moved, undo = [], []
        for item_id in a["ids"][:100]:
            it = item_by_id(int(item_id), user)
            undo.append([it["id"], it["location_id"]])
            store._exec("UPDATE items SET location_id=?, updated_at=? WHERE id=?",
                        (loc, datetime.now().isoformat(timespec="seconds"), it["id"]))
            moved.append(it["name"])
        msg = f"Moved {len(moved)} item(s) to {a['location']}: {', '.join(moved)}"
        log(user, "move_items", a, msg, {"move_back": undo})
        return msg

    def add_location(a, user):
        from .db import LOCATION_KINDS
        name = (a.get("name") or "").strip()
        if not name:
            raise ToolError("a location needs a name")
        parent_id = find_location(a.get("parent")) if a.get("parent") else None
        full = (store.location(parent_id)["name"] + " > " + name) if parent_id else name
        if any(l["name"].lower() == full.lower() for l in store.locations()):
            raise ToolError(f"there's already a location called '{full}'")
        kind = a.get("kind") or "home"
        if kind not in LOCATION_KINDS:
            raise ToolError("kind must be one of: " + ", ".join(f"{k} ({v})" for k, v in LOCATION_KINDS.items()))
        data = {"name": name, "kind": kind, "person": a.get("person") or "", "people": a.get("people") or 1,
                "counts_home": "" if a.get("counts_home") is None else (1 if a["counts_home"] else 0),
                "notes": a.get("notes") or ""}
        if parent_id:
            data["parent_id"] = parent_id
        try:
            clean = srv.clean_location(data)
            loc_id = srv.save_location(clean)
        except srv.ApiError as exc:
            raise ToolError(str(exc))
        if parent_id:
            msg = f"Added '{full}' inside '{store.location(parent_id)['name']}'; it counts the same as its top-level location."
        else:
            msg = (f"Added location '{name}' ({LOCATION_KINDS[kind]}"
                   + (", counts toward home supply" if clean["counts_home"] else ", not counted as home supply") + ")"
                   + (". Vehicles and go-bags get a kit score; it shows on Locations & kits." if kind in ("vehicle", "gobag") else ""))
        log(user, "add_location", a, msg, {"delete_location": loc_id})
        return msg

    def complete_task(a, user):
        rows = store._rows("SELECT * FROM tasks WHERE id=?", (int(a["id"]),))
        if not rows:
            raise ToolError("no such task (see maintenance_tasks)")
        today = datetime.now().date().isoformat()
        store._exec("UPDATE tasks SET last_done=? WHERE id=?", (today, rows[0]["id"]))
        msg = f"Marked done today: {rows[0]['title']}"
        log(user, "complete_task", a, msg, {"task_last_done": [rows[0]["id"], rows[0]["last_done"]]})
        return msg


    # ---------- situation, family, brief ----------
    def situation_now(a, user):
        rows = situation.items(store)
        tiers = a.get("tiers") or ["flash", "priority"]
        if a.get("category"):
            rows = [r for r in rows if r["category"] == a["category"]]
        rows = [r for r in rows if r["tier"] in tiers][:int(a.get("limit") or 25)]
        st = store.get_raw("situation_sources", {}) or {}
        asof = max((v.get("at", "") for v in st.values()), default="")
        if not rows:
            return f"Nothing {'near you' if tiers == ['flash', 'priority'] else 'matching'} right now (as of {asof or 'never'})."
        out = []
        for r in rows:
            where = (f"at {r['site']}" if r["km"] == 0 else f"{r['km']} km from {r['site']}") if r["site"] and r["km"] is not None else r["where_text"]
            out.append(f"[{situation.TIERS[r['tier']]}] {r['title']}" + (f" ({where})" if where else "") +
                       f" · {situation.TRUST.get(r['trust'], r['trust'])}" + (f" · checklist: {r['scenario']}" if r["scenario"] else "")
                       + (f"\n  {r['summary'][:300]}" if a.get("details") and r["summary"] else ""))
        failed = [k for k, v in st.items() if v.get("ok") is False]
        return f"As of {asof}:\n" + "\n".join(out) + (f"\n(Couldn't refresh: {', '.join(failed)}.)" if failed else "")

    def family_board(a, user):
        b = checkins.board(store)
        lines = []
        if b["call"]:
            c = b["call"]
            lines.append(f"FAMILY CALL: {c['label']} {c['place']}".strip() + (f" ({c['address']})" if c["address"] else "")
                         + f", sent by {c['created_by']} at {c['created_at']}. Waiting on {b['waiting']}.")
        for p in b["people"]:
            last = p["last"]
            lines.append(f"{p['name']}{' (lives elsewhere)' if p['remote'] else ''}: " + (
                f"{p['status_label']} at {last['at']}" + (f", {last['note']}" if last.get("note") else "") +
                (f" (via {checkins.VIA.get(last['via'], last['via'])})" if last["via"] != "app" else "") if last else "no check-in yet")
                + (" — hasn't answered the call" if p["waiting"] else ""))
        return "\n".join(lines) or "No household members on the Family tab yet."

    def check_in(a, user):
        status = a["status"]
        me = checkins.person_for_user(store, user)
        who = a.get("person")
        if who:
            match = [p for p in checkins.people(store) if p["name"].lower() == str(who).strip().lower()]
            if not match:
                raise ToolError("no such person. People: " + ", ".join(p["name"] for p in checkins.people(store)))
            target = match[0]
            if not me or target["uid"] != me["uid"]:
                if not checkins.can_call(store, user):
                    raise ToolError("only an adult can check someone else in")
        elif me:
            target = me
        else:
            raise ToolError("your login isn't linked to a person yet (Family tab); say who to check in")
        try:
            rec = checkins.add_checkin(store, target["uid"], status, a.get("note"), a.get("place"), via="app",
                                       by_name=user["username"] if (not me or target["uid"] != me["uid"]) else "")
        except ValueError as exc:
            raise ToolError(str(exc))
        msg = f"Checked {rec['person_name']} in as '{checkins.STATUSES[status]}'" + (f" ({rec['note']})" if rec["note"] else "") + \
            ". The family board shows it" + ("; everyone who gets notifications is told." if status == "help" else ".")
        log(user, "check_in", a, msg, {"delete_checkin": rec["uid"]})
        return msg

    def send_family_call(a, user):
        if not checkins.can_call(store, user):
            raise ToolError("only an adult can send a family call")
        site = None
        if a.get("place"):
            match = [s for s in store.sites() if s["name"].lower() == str(a["place"]).strip().lower()]
            if not match:
                raise ToolError("no place with that name. Places: " + ", ".join(s["name"] for s in store.sites()))
            site = match[0]
        try:
            c = checkins.start_call(store, a["kind"], user["username"], place=site["name"] if site else "",
                                    address=site["address"] if site else "", lat=site["lat"] if site else None,
                                    lon=site["lon"] if site else None, message=a.get("message"), drill=bool(a.get("drill")))
        except ValueError as exc:
            raise ToolError(str(exc) + ". Places: " + ", ".join(s["name"] for s in store.sites()))
        msg = f"Sent to everyone: {'DRILL: ' if c['drill'] else ''}{checkins.CALL_KINDS[c['kind']]} {c['place']}".strip() + (f". Message: {c['message']}" if c["message"] else "") + \
            ". It stays on everyone's Home screen until all clear."
        log(user, "send_family_call", a, msg, None)
        return msg

    def end_family_call(a, user):
        if not checkins.can_call(store, user):
            raise ToolError("only an adult can end a family call")
        c = checkins.end_call(store, user["username"])
        if c:
            r = checkins.report(store, c["uid"])
            msg = (f"{'Drill ended' if c['drill'] else 'All clear sent'}; ended '{c['label']} {c['place']}'".strip()
                   + f". {r['answered']} of {r['total']} checked in" + (f"; missing: {', '.join(r['missing'])}" if r["missing"] else "") + ".")
        else:
            msg = "There was no family call to end."
        log(user, "end_family_call", a, msg, None)
        return msg

    def morning_brief(a, user):
        rec = brief.personalise(store, brief.latest(store, a.get("day")), user.get("id"))
        if not rec:
            return "No brief yet. It's made each morning."
        O = rec.get("outlets") or {}
        out = [f"Morning brief for {rec['day']}."]
        if rec.get("near"):
            out.append("Near you: " + "; ".join(n["title"] for n in rec["near"]))
        if rec.get("ai"):
            out.append(rec["ai"]["lead"])
            for s in rec["ai"]["stories"]:
                out.append(f"\n## {s['title']}\nAgreed: {s['agree']}")
                for d in s["differ"]:
                    out.append(f"- {d['who']}: {d['framing']}")
                if s["unconfirmed"]:
                    out.append(f"Not confirmed: {s['unconfirmed']}")
                if s["matters"]:
                    out.append(f"For us: {s['matters']}")
                if a.get("links"):
                    out.append("Sources: " + ", ".join(f"{(O.get(x['outlet']) or {}).get('name', x['outlet'])} <{x['url']}>" for x in s["sources"]))
        else:
            out.append("(No AI summary that day.) Stories covered across regions:")
            out += [f"- {g['headline']} ({len(g['regions'])} regions)" for g in rec.get("groups", [])[:8]]
        return "\n".join(out)

    def undo_change(a, user):
        rows = store._rows("SELECT * FROM mcp_log WHERE id=?", (int(a["change_id"]),))
        if not rows or not rows[0]["undo"]:
            raise ToolError("that change can't be undone (see recent_changes)")
        u = json.loads(rows[0]["undo"])
        if "delete_item" in u or "delete_items" in u:
            names = []
            for iid in u.get("delete_items") or [u["delete_item"]]:
                it = next((i for i in items_for(user) if i["id"] == iid), None)
                if it:
                    store.delete_item(it["id"])
                    names.append(it["name"])
            msg = f"Removed {len(names)} item(s) added by change {a['change_id']}: {', '.join(names) or 'already gone'}"
        elif "restore_item" in u:
            item_by_id(u["restore_item"], user)
            store.save_item(u["fields"], u["restore_item"])
            msg = f"Restored item {u['restore_item']} to how it was before change {a['change_id']}"
        elif "move_back" in u:
            for item_id, loc in u["move_back"]:
                store._exec("UPDATE items SET location_id=?, updated_at=? WHERE id=?",
                            (loc, datetime.now().isoformat(timespec="seconds"), item_id))
            msg = f"Moved {len(u['move_back'])} item(s) back"
        elif "delete_location" in u:
            lid = u["delete_location"]
            if store._rows("SELECT 1 FROM items WHERE location_id=?", (lid,)):
                raise ToolError("that location has items in it now; move them first (or remove it in BUNKR)")
            loc = store.location(lid)
            store.delete_location(lid)
            msg = f"Removed location '{loc['name'] if loc else lid}' (added through the connector by change {a['change_id']})"
        elif "delete_checkin" in u:
            store._exec("DELETE FROM checkins WHERE uid=?", (u["delete_checkin"],))
            msg = "Check-in removed"
        elif "task_last_done" in u:
            tid, prev = u["task_last_done"]
            store._exec("UPDATE tasks SET last_done=? WHERE id=?", (prev, tid))
            msg = "Task put back"
        else:
            raise ToolError("nothing to undo")
        store._exec("UPDATE mcp_log SET undo='' WHERE id=?", (rows[0]["id"],))
        log(user, "undo_change", a, msg, None)
        return msg

    item_props = {
        "name": {"type": "string", "description": "What it is, e.g. 'Bottled water' or 'AA batteries'"},
        "category": {"type": "string", "enum": [k for k in gaps.CATEGORIES if k not in gaps.SENSITIVE]},
        "quantity": {"type": "number", "minimum": 0, "description": "How many packs/units (fractions allowed: 0.5 case)"},
        "unit": {"type": "string", "description": "What one unit is called: case, jug, can, box, bills"},
        "pack_size": {"type": "number", "minimum": 0, "description": "Things in each unit: 24 for a case of 24 bottles. Default 1"},
        "pack_unit": {"type": "string", "description": "What's inside each unit: bottles, bars, rounds"},
        "size": {"type": "number", "description": "Water: volume of ONE inner item (with size_unit). Food: calories of ONE inner item. Cash: value of ONE bill in dollars"},
        "size_unit": {"type": "string", "enum": SIZE_UNITS, "description": "Water only"},
        "counts_toward": {"type": "string", "description": "Checklist item id from list_categories (e.g. batteries, first_aid_kit, cash). Leave empty for general water/food"},
        "location": {"type": "string", "description": "Exact location path from list_locations, e.g. 'Master bedroom > Under bed'"},
        "expires": {"type": "string", "description": "YYYY-MM-DD"},
        "notes": {"type": "string"},
    }
    T = {}

    def tool(name, title, desc, schema, fn, ann):
        T[name] = {"name": name, "title": title, "description": desc, "inputSchema": schema, "annotations": ann, "fn": fn}

    tool("search_inventory", "Search inventory", "Find supplies by name/notes, category or location. Returns ids for update_item.",
         _obj({"query": {"type": "string"}, "category": {"type": "string"}, "location": {"type": "string"},
               "limit": {"type": "integer", "minimum": 1, "maximum": 200}}), search_inventory, _ro("Search inventory"))
    tool("list_locations", "List locations", "Where supplies can be stored (home shelves, car kits, go-bags), as full paths like 'Master bedroom > Under bed > Gun safe'.",
         _obj(), list_locations, _ro("List locations"))
    tool("list_categories", "List categories", "Item categories and the checklist ids an item can count toward.",
         _obj(), list_categories, _ro("List categories"))
    tool("readiness", "Readiness", "Overall readiness, days of water/food, biggest gaps, expiring items.",
         _obj(), readiness, _ro("Readiness"))
    tool("shopping_list", "Shopping list", "What to buy next, most important first.",
         _obj({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}), shopping_list, _ro("Shopping list"))
    tool("maintenance_tasks", "Maintenance tasks", "Recurring chores (rotate water, test radios...) with due dates and ids.",
         _obj(), maintenance_tasks, _ro("Maintenance tasks"))
    tool("channel_plan", "Radio channel plan", "The family radio plan: slot numbers, frequencies, tones. Optional search.",
         _obj({"query": {"type": "string"}}), channel_plan, _ro("Radio channel plan"))
    tool("recent_changes", "Recent changes", "The last 20 changes made through the connector, with ids for undo_change.",
         _obj(), recent_changes, _ro("Recent changes"))
    tool("add_item", "Add supply", "Add a supply to the inventory. Use packs for cases (quantity 1, pack_size 24).",
         _obj(item_props, ["name", "category", "quantity"]), add_item, _rw("Add supply"))
    tool("add_items", "Add several supplies", "Add up to 50 supplies in one go (one approval, one undo). Optional "
         "location applies to every item that doesn't name its own. Everything is checked before anything is saved.",
         _obj({"location": {"type": "string", "description": "Default location for the list"},
               "items": {"type": "array", "minItems": 1, "maxItems": 50,
                         "items": _obj(item_props, ["name", "category", "quantity"])}}, ["items"]),
         add_items, _rw("Add several supplies"))
    tool("update_item", "Update supply", "Change an existing item (quantity, location, expiry, notes...). Get the id from search_inventory.",
         _obj({"id": {"type": "integer"}, **item_props}, ["id"]), update_item, _rw("Update supply", True))
    tool("move_items", "Move supplies", "Move items (by id) to another location.",
         _obj({"ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1}, "location": {"type": "string"}},
              ["ids", "location"]), move_items, _rw("Move supplies", True))
    tool("add_location", "Add location", "Add a storage location: a room or shelf (kind home), a vehicle, a go-bag, or an "
         "off-site stash. Vehicles and go-bags get a kit score. counts_home: whether supplies there count toward home "
         "readiness (default: yes for home and go-bags, no for vehicles and stashes).",
         _obj({"name": {"type": "string", "description": "e.g. 'Basement store room' or '4Runner'; with parent, just this level's name, e.g. 'Gun safe'"},
               "parent": {"type": "string", "description": "Optional: full path of the location this goes inside (from list_locations), "
                          "e.g. 'Master bedroom > Under bed'. Up to two levels below a top-level location. "
                          "A sub-location takes its parent's kind and counts the same."},
               "kind": {"type": "string", "enum": ["home", "vehicle", "gobag", "stash"]},
               "person": {"type": "string", "description": "Go-bags: whose bag it is"},
               "people": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Vehicles and go-bags: how many people it supplies"},
               "counts_home": {"type": "boolean"}, "notes": {"type": "string"}}, ["name", "kind"]),
         add_location, _rw("Add location"))
    tool("complete_task", "Mark task done", "Mark a maintenance task done today.",
         _obj({"id": {"type": "integer"}}, ["id"]), complete_task, _rw("Mark task done", True))
    tool("situation_now", "What's happening near us", "Alerts, fires, quakes, space weather, internet outages and cyber items, "
         "ranked by distance to the family's places. Default: only 'Act now' and 'Soon / near you'.",
         _obj({"tiers": {"type": "array", "items": {"type": "string", "enum": ["flash", "priority", "routine"]}},
               "category": {"type": "string", "enum": list(situation.CATEGORIES)}, "details": {"type": "boolean"},
               "limit": {"type": "integer", "minimum": 1, "maximum": 100}}), situation_now, _ro("What's happening near us"))
    tool("family_board", "Family check-ins", "Everyone's latest check-in (I'm safe / On my way / Need help) and any active family call.",
         _obj(), family_board, _ro("Family check-ins"))
    tool("morning_brief", "Morning brief", "Today's (or a given day's) brief: what's near us, and the top world stories with "
         "what outlets across regions agree on and how their framing differs.",
         _obj({"day": {"type": "string", "description": "YYYY-MM-DD; default the latest"}, "links": {"type": "boolean"}}),
         morning_brief, _ro("Morning brief"))
    tool("check_in", "Check in", "Tell the family how someone is: safe, moving (on my way) or help (need help). Default: you. "
         "'help' notifies everyone, even at night.",
         _obj({"status": {"type": "string", "enum": list(checkins.STATUSES)}, "person": {"type": "string", "description": "Name, if not you"},
               "note": {"type": "string"}, "place": {"type": "string"}}, ["status"]), check_in, _rw("Check in"))
    tool("send_family_call", "Send a family call", "Send an instruction to the whole family: evacuate (to a place), meet (at a place), "
         "shelter (in place) or checkin (everyone check in). It notifies everyone. Only adults.",
         _obj({"kind": {"type": "string", "enum": list(checkins.CALL_KINDS)}, "place": {"type": "string", "description": "A place name from Sites"},
               "message": {"type": "string"}, "drill": {"type": "boolean", "description": "A practice run: marked DRILL, no night notifications"}},
              ["kind"]), send_family_call, _rw("Send a family call"))
    tool("end_family_call", "All clear", "End the active family call and tell everyone all clear.", _obj(), end_family_call, _rw("All clear", True))
    tool("undo_change", "Undo a change", "Undo a change made through the connector (id from recent_changes). Removes an item only if it was added through the connector.",
         _obj({"change_id": {"type": "integer"}}, ["change_id"]), undo_change, _rw("Undo a change"))
    return T
