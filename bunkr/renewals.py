"""Renewal and review dates on documents: policies, passports, registrations.

30 days before a document's date the household is reminded (in the app, by email for whoever chose it, and on the
maintenance list). Once the date passes it's overdue, and the reminder repeats weekly until someone marks it done on
the maintenance list or sets the document's next date. Reminders go to admins: documents are an admin tab.
"""

from datetime import date, datetime, timedelta, timezone

AHEAD = 30
listeners = []


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(d):
    try:
        return date.fromisoformat((d or "")[:10])
    except ValueError:
        return None


def clean_date(v):
    v = str(v or "").strip()
    if v and not _parse(v):
        raise ValueError("the renewal date must be a date (YYYY-MM-DD)")
    return v[:10]


def status(doc, today=None):
    """None (no date), or {"state": later|soon|overdue|done, "days": days until the date, "due": "YYYY-MM-DD"}."""
    due = _parse(doc.get("renew_on"))
    if not due:
        return None
    today = today or date.today()
    days = (due - today).days
    done = (doc.get("renewed_at") or "") >= (due - timedelta(days=AHEAD)).isoformat()
    state = "done" if done else "later" if days > AHEAD else "soon" if days >= 0 else "overdue"
    return {"state": state, "days": days, "due": due.isoformat()}


def due(store, today=None):
    """Documents to remind about now, overdue first."""
    out = []
    for d in store._rows("SELECT * FROM documents WHERE renew_on != ''"):
        st = status(d, today)
        if st and st["state"] in ("soon", "overdue"):
            out.append(d | {"renewal": st})
    out.sort(key=lambda d: d["renewal"]["days"])
    return out


def _when(st):
    d = _parse(st["due"])
    nice = f"{d:%b} {d.day}, {d.year}"
    if st["state"] == "overdue":
        n = -st["days"]
        return f"was due {nice} ({n} day{'s' if n != 1 else ''} ago)"
    return "due today" if st["days"] == 0 else f"due {nice} (in {st['days']} day{'s' if st['days'] != 1 else ''})"


def message(d):
    st = d["renewal"]
    what = d["label"] or "A document"
    title = (f"Overdue: renew {what}" if st["state"] == "overdue" else f"Renew soon: {what}")
    ref = " · ".join(x for x in (d.get("insurer"), d.get("coverage"), f"claims {d['claim_phone']}" if d.get("claim_phone") else "") if x)
    body = f"{what} {_when(st)}." + (f" {ref}." if ref else "") + \
        " Mark it done on the maintenance list, or set the next date on the Documents tab."
    return {"title": title[:200], "body": body[:500], "tag": f"doc-{d['id']}", "url": "/#documents", "user_id": None}


def remind(store, today=None):
    """Daily: once 30 days out, then weekly once overdue, until it's done. Returns what was sent."""
    today = today or date.today()
    sent = []
    for d in due(store, today):
        st = d["renewal"]
        key = f"doc:{d['id']}:{st['due']}:" + ("soon" if st["state"] == "soon" else f"late{(-st['days'] - 1) // 7}")
        if store._rows("SELECT 1 FROM reminder_log WHERE key=?", (key,)):
            continue
        store._exec("INSERT INTO reminder_log(key, at) VALUES(?, ?)", (key, _now()))
        sent.append(d)
        for fn in list(listeners):
            try:
                fn("reminder", message(d))
            except Exception:
                pass
    return sent


def mark_done(store, doc_id, today=None):
    store._exec("UPDATE documents SET renewed_at=? WHERE id=?", ((today or date.today()).isoformat(), doc_id))


def calendar_entries(store, today=None):
    """For the maintenance list: each document coming up or overdue, with a Done button."""
    return [{"kind": "renew", "id": d["id"], "title": f"Renew: {d['label'] or 'document'}", "due": d["renewal"]["due"],
             "overdue": d["renewal"]["state"] == "overdue", "repeat": "renewal date"} for d in due(store, today)]


def details_html(docs, household="Household"):
    """For the encrypted archive: each document's renewal date and reference text (insurer, coverage, claim phone)."""
    import html
    e = html.escape
    rows = [d for d in docs if any(d.get(k) for k in ("renew_on", "insurer", "coverage", "claim_phone"))]
    if not rows:
        return ""
    body = "".join(f"<tr><td>{e(d['label'])}</td><td>{e(d.get('doc_type') or '')}</td><td>{e(d.get('renew_on') or '')}</td>"
                   f"<td>{e(d.get('insurer') or '')}</td><td>{e(d.get('coverage') or '')}</td><td>{e(d.get('claim_phone') or '')}</td></tr>"
                   for d in rows)
    return (f"<!doctype html><meta charset='utf-8'><title>{e(household)}: document details</title>"
            "<style>body{font:14px/1.4 system-ui,sans-serif;margin:16px}table{border-collapse:collapse}"
            "td,th{border:1px solid #ccc;padding:4px 8px;text-align:left;vertical-align:top}</style>"
            f"<h1>{e(household)}: document details</h1><p>Made {date.today().isoformat()}.</p>"
            "<table><tr><th>Document</th><th>Type</th><th>Renew or review by</th><th>Insurer / issuer</th><th>Coverage</th>"
            f"<th>Claim phone</th></tr>{body}</table>")
