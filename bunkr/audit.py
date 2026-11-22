"""Audit log: who looked at or changed sensitive things, and every login.

Recorded:
- logins (worked, failed, locked out), anything someone was refused (not allowed for their login), log-outs and every change to logins (new, deleted, role, password);
- every change anyone makes through BUNKR (method and path only, never what was typed, so no passwords end up here);
- looking at sensitive things: the inventory with firearms (admins), documents, medical details and cards, people's
  details, the family plan, printouts and exports, the log itself, and what the optional AI connector reads.

A view is written at most once an hour per person and thing, so opening the app twenty times isn't twenty rows.
Each row stores a hash of the row before it, so an edited or deleted row breaks the chain and `verify` says where.
Rows older than two years are dropped (the chain starts again from the oldest kept row).

Some events also tell the admins straight away (a "reminder" push): a login locked out after failed attempts, a new
admin, someone made admin, a login deleted, another person's password changed, two-step login turned off, a
recovery code used.
"""

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone

KEEP_DAYS = 730
VIEW_EVERY = timedelta(hours=1)
FIELDS = ("at", "user_id", "username", "action", "what", "detail", "ip")
listeners = []        # fn(event, data) -> the notifier ("reminder" events to admins)
_lock = threading.Lock()

# GET routes worth a "view" row: head -> what
VIEWS = {"documents": "documents", "medical": "medical", "medical-cards.html": "medical", "people": "people",
         "family-plan.html": "family plan", "report.html": "printout", "export.csv": "export", "users": "logins",
         "audit": "audit log", "audit.csv": "export", "backups": "backups", "nodes": "nodes", "calendar.ics": "calendar",
         "recovery": "home inventory", "insurance-list.html": "printout", "insurance-list.csv": "export",
         "finance": "financial sheet", "finance-sheet.html": "printout", "kids-plan.html": "family plan"}
ALERTS = {"locked": "A login was locked after failed attempts", "admin_added": "A new admin login was made",
          "made_admin": "A login was made admin", "user_deleted": "A login was deleted",
          "password_reset": "Someone's password was changed by an admin", "mfa_off": "Two-step login was turned off",
          "recovery_used": "A recovery code was used to log in", "node_lost": "A node was marked lost or stolen",
          "chain_broken": "The activity log was changed"}


def _now():
    return datetime.now(timezone.utc)


def _hash(prev, row):
    return hashlib.sha256((prev + json.dumps([row.get(k) for k in FIELDS], separators=(",", ":"))).encode()).hexdigest()


def record(store, user, action, what="", detail="", ip="", alert=None):
    """Write one row. user: a session user dict, or None for someone not logged in."""
    row = {"at": _now().isoformat(timespec="seconds"), "user_id": (user or {}).get("id"),
           "username": str((user or {}).get("username") or "")[:60], "action": action[:20], "what": str(what)[:40],
           "detail": str(detail)[:300], "ip": str(ip)[:80]}
    with _lock:
        last = store._rows("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
        prev = last[0]["hash"] if last else ""
        store._exec("INSERT INTO audit_log(at, user_id, username, action, what, detail, ip, prev, hash) VALUES(?,?,?,?,?,?,?,?,?)",
                    (*[row[k] for k in FIELDS], prev, _hash(prev, row)))
    if alert:
        tell(alert, f"{row['username'] or 'Someone'}: {detail}" if detail else row["username"])
    return row


def tell(kind, body=""):
    for fn in list(listeners):
        try:
            fn("reminder", {"user_id": None, "title": ALERTS.get(kind, "Security"), "body": body[:200],
                            "tag": f"security-{kind}", "url": "/#household"})
        except Exception as exc:  # never let a push failure stop the action
            print(f"audit alert failed: {exc}")


def viewed(store, user, what, detail="", ip=""):
    """A view, at most once an hour per person and thing."""
    since = (_now() - VIEW_EVERY).isoformat(timespec="seconds")
    if store._rows("SELECT 1 FROM audit_log WHERE action='view' AND user_id IS ? AND what=? AND at > ? LIMIT 1",
                   ((user or {}).get("id"), what, since)):
        return None
    return record(store, user, "view", what, detail, ip)


def rows(store, limit=200, user_id=None, what=None, before_id=None):
    sql, args = "SELECT id, at, user_id, username, action, what, detail, ip FROM audit_log WHERE 1=1", []
    if user_id is not None:
        sql += " AND user_id=?"
        args.append(user_id)
    if what:
        sql += " AND what=?"
        args.append(what)
    if before_id:
        sql += " AND id < ?"
        args.append(before_id)
    return store._rows(sql + " ORDER BY id DESC LIMIT ?", (*args, max(1, min(int(limit), 1000))))


def verify(store):
    """Walk the chain. Returns {"ok", "rows", "broken_at"} (broken_at: the first row that doesn't match)."""
    prev, n = None, 0
    for r in store._rows("SELECT * FROM audit_log ORDER BY id"):
        n += 1
        if prev is not None and r["prev"] != prev:
            return {"ok": False, "rows": n, "broken_at": r["id"]}
        if _hash(r["prev"], r) != r["hash"]:
            return {"ok": False, "rows": n, "broken_at": r["id"]}
        prev = r["hash"]
    return {"ok": True, "rows": n, "broken_at": None}


def prune(store, days=KEEP_DAYS):
    cut = (_now() - timedelta(days=days)).isoformat(timespec="seconds")
    with _lock:
        store._exec("DELETE FROM audit_log WHERE at < ?", (cut,))


def nightly(store):
    """Drop old rows; tell the admins once if the chain no longer checks out."""
    prune(store)
    v = verify(store)
    seen = store.get_raw("audit_broken_at", None)
    if not v["ok"] and v["broken_at"] != seen:
        tell("chain_broken", f"Row {v['broken_at']} doesn't match the one before it.")
    store.set_raw("audit_broken_at", v["broken_at"])
    return v
