"""Email for reminders, through a mail server an admin sets up (Gmail, iCloud, Fastmail, your ISP...).

The server's address, port, login name and From address are a setting; the password is a file next to the
database (`smtp.password`, readable only by BUNKR), like the other keys. Mail waits in an outbox and is retried
every few minutes for up to three days, so a reminder made while the internet is down still goes out later.
"""

import smtplib
import ssl
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

PASSWORD_FILE = "smtp.password"
SECURITY = {"starttls": "STARTTLS (usually port 587)", "ssl": "SSL/TLS (usually port 465)", "none": "None (only on your own network)"}
DEFAULTS = {"host": "", "port": 587, "security": "starttls", "username": "", "from": ""}
MAX_TRIES = 30
KEEP_TRYING = timedelta(days=3)
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def settings(store):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (store.get_raw("mail", {}) or {}).items() if k in DEFAULTS})
    return s


def has_password(folder):
    p = Path(folder) / PASSWORD_FILE
    return p.exists() and p.stat().st_size > 0


def _password(folder):
    p = Path(folder) / PASSWORD_FILE
    return p.read_text().rstrip("\n") if p.exists() else ""


def ready(store, folder):
    s = settings(store)
    return bool(s["host"] and s["from"])


def view(store, folder):
    """What the Household tab shows. Never the password."""
    s = settings(store)
    waiting = store._rows("SELECT COUNT(*) AS n FROM email_outbox WHERE sent_at=''")[0]["n"]
    last = store._rows("SELECT last_error FROM email_outbox WHERE last_error!='' ORDER BY id DESC LIMIT 1")
    return s | {"has_password": has_password(folder), "ready": ready(store, folder), "security_options": SECURITY,
                "waiting": waiting, "last_error": last[0]["last_error"] if waiting and last else ""}


def save_settings(store, folder, data):
    s = settings(store)
    host = str(data.get("host", s["host"]) or "").strip()
    if host and (len(host) > 200 or any(c in host for c in " /\\@:")):
        raise ValueError("the mail server is a name like smtp.gmail.com")
    try:
        port = int(data.get("port") or s["port"])
    except (TypeError, ValueError):
        raise ValueError("the port is a number, like 587")
    if not 0 < port < 65536:
        raise ValueError("the port is a number, like 587")
    security = data.get("security") if data.get("security") in SECURITY else s["security"]
    sender = str(data.get("from", s["from"]) or "").strip()[:200]
    from .notify import EMAIL_RE
    if sender and not EMAIL_RE.match(sender):
        raise ValueError("the From address doesn't look like an email address")
    s.update(host=host, port=port, security=security, username=str(data.get("username", s["username"]) or "").strip()[:200], **{"from": sender})
    store.set_raw("mail", s)
    pw = data.get("password")
    path = Path(folder) / PASSWORD_FILE
    if data.get("forget_password"):
        path.unlink(missing_ok=True)
    elif pw:
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(str(pw).strip() + "\n")
    return view(store, folder)


def queue(store, to, subject, body):
    return store._exec("INSERT INTO email_outbox(to_addr, subject, body, created_at) VALUES(?,?,?,?)",
                       (to[:200], subject[:200], body[:5000], _now()))


def reminder(store, to, msg, base_url=""):
    """Queue one reminder email from a notification message."""
    link = (base_url.rstrip("/") + (msg.get("url") or "/")) if base_url else ""
    body = "\n\n".join(x for x in (msg.get("body") or "", f"Open BUNKR: {link}" if link else "",
                                    "You get this because your BUNKR reminders are set to email. Change it on the Household tab.") if x)
    return queue(store, to, f"BUNKR: {msg['title']}", body)


def _connect(s, folder, timeout=20):
    if s["security"] == "ssl":
        conn = smtplib.SMTP_SSL(s["host"], s["port"], timeout=timeout, context=ssl.create_default_context())
    else:
        conn = smtplib.SMTP(s["host"], s["port"], timeout=timeout)
        if s["security"] == "starttls":
            conn.starttls(context=ssl.create_default_context())
    pw = _password(folder)
    if s["username"] and pw:
        conn.login(s["username"], pw)
    return conn


def flush(store, folder, connect=None):
    """Send what's waiting. Returns (sent, failed). A failure is kept and tried again next time, for up to three days."""
    if not ready(store, folder):
        return 0, 0
    if not _lock.acquire(blocking=False):
        return 0, 0
    try:
        oldest = (datetime.now(timezone.utc) - KEEP_TRYING).isoformat(timespec="seconds")
        store._exec("DELETE FROM email_outbox WHERE sent_at='' AND (created_at < ? OR tries >= ?)", (oldest, MAX_TRIES))
        store._exec("DELETE FROM email_outbox WHERE sent_at!='' AND sent_at < ?", (oldest,))
        rows = store._rows("SELECT * FROM email_outbox WHERE sent_at='' ORDER BY id LIMIT 50")
        if not rows:
            return 0, 0
        s = settings(store)
        try:
            conn = (connect or _connect)(s, folder)
        except (OSError, smtplib.SMTPException) as exc:
            err = _short(exc)
            for r in rows:
                store._exec("UPDATE email_outbox SET tries=tries+1, last_error=? WHERE id=?", (err, r["id"]))
            return 0, len(rows)
        sent = failed = 0
        try:
            for r in rows:
                m = EmailMessage()
                m["From"], m["To"], m["Subject"] = f"BUNKR <{s['from']}>", r["to_addr"], r["subject"]
                m["Date"], m["Message-ID"] = formatdate(localtime=True), make_msgid(domain=s["from"].split("@")[-1])
                m.set_content(r["body"])
                try:
                    conn.send_message(m)
                    store._exec("UPDATE email_outbox SET sent_at=?, last_error='' WHERE id=?", (_now(), r["id"]))
                    sent += 1
                except (OSError, smtplib.SMTPException) as exc:
                    store._exec("UPDATE email_outbox SET tries=tries+1, last_error=? WHERE id=?", (_short(exc), r["id"]))
                    failed += 1
        finally:
            try:
                conn.quit()
            except (OSError, smtplib.SMTPException):
                pass
        return sent, failed
    finally:
        _lock.release()


def _short(exc):
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "the mail server didn't accept the login (for Gmail or iCloud, use an app password)"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "the mail server refused the address"
    return f"{type(exc).__name__}: {exc}"[:200]
