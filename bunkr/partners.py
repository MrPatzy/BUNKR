"""Partner hubs: a trusted neighbor who runs a BUNKR of their own sees a few chosen things from ours, read-only.

Sharing side: an admin makes a "partner share" with a name and what it includes (the family check-in board, the
family call, the comms plan, callsigns). It gets an address (/api/partner/<share uid>) and a long random token shown
once; only the token's SHA-256 is kept. The partner's hub asks with `Authorization: Bearer <token>` and gets just
those pieces as JSON (`payload`). The payload is built from a short allow-list of fields, so firearms, medical,
documents, money, inventory and where anyone is (beyond "I'm safe" and when) can never be in it. Every answer and
every wrong token goes in the audit log; wrong tokens are limited per address, answers per share. Revoking stops it.

Receiving side: an admin adds a partner (name, address, token). The token is kept in `partner-<id>.token` next to
the database (mode 600), never in the database or the API. The hub reads each partner every 15 minutes and keeps the
last good answer, cleaned again on arrival (it comes from someone else's machine), with their "as of" time.
"""

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, checkins, comms

PARTS = {"board": "Family check-in board", "calls": "Family call (evacuate, meet...)",
         "comms": "Comms plan (nets, channels, radios)", "licences": "Callsigns"}
USER_AGENT = f"BUNKR/{__version__} (partner hub)"
MAX_BYTES = 512 * 1024
FAIL_WINDOW, FAIL_MAX = 900, 10          # wrong tokens: 10 per 15 minutes from one address
USE_WINDOW, USE_MAX = 3600, 120          # right token: 120 an hour per share (the partner reads every 15 minutes)
PULL_EVERY = 900
_fails, _uses = {}, {}
_lock = threading.Lock()
SHARE_RE = re.compile(r"^[0-9a-f]{32}$")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def token_hash(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


def clean_parts(parts):
    return [p for p in PARTS if p in (parts or [])]


# ---------- sharing side ----------
def create_share(store, name, parts, by=""):
    """Returns (share row id, uid, token). The token is never stored."""
    name = str(name or "").strip()[:80]
    if not name:
        raise ValueError("name the share (who it's for)")
    parts = clean_parts(parts)
    if not parts:
        raise ValueError("pick at least one thing to share")
    token = secrets.token_urlsafe(32)
    uid = uuid.uuid4().hex
    row = store._exec("INSERT INTO partner_shares(uid, name, token_hash, parts, created_by, created_at) VALUES(?,?,?,?,?,?)",
                      (uid, name, token_hash(token), json.dumps(parts), str(by)[:60], _now()))
    return row, uid, token


def update_share(store, share_id, data):
    s = store._rows("SELECT * FROM partner_shares WHERE id=?", (share_id,))
    if not s:
        raise LookupError("no such share")
    if "parts" in data:
        parts = clean_parts(data.get("parts"))
        if not parts:
            raise ValueError("pick at least one thing to share")
        store._exec("UPDATE partner_shares SET parts=? WHERE id=?", (json.dumps(parts), share_id))
    if str(data.get("name") or "").strip():
        store._exec("UPDATE partner_shares SET name=? WHERE id=?", (str(data["name"]).strip()[:80], share_id))


def revoke_share(store, share_id):
    if not store._rows("SELECT id FROM partner_shares WHERE id=?", (share_id,)):
        raise LookupError("no such share")
    store._exec("UPDATE partner_shares SET revoked_at=? WHERE id=? AND revoked_at=''", (_now(), share_id))


def shares(store):
    rows = store._rows("SELECT id, uid, name, parts, created_by, created_at, revoked_at, last_used_at, uses "
                       "FROM partner_shares ORDER BY revoked_at != '', created_at DESC")
    for r in rows:
        r["parts"] = clean_parts(json.loads(r["parts"] or "[]"))
    return rows


def _limited(table, key, window, most, now_t):
    with _lock:
        hits = [t for t in table.get(key, []) if now_t - t < window]
        table[key] = hits
        if len(hits) >= most:
            return True
        hits.append(now_t)
        return False


def check(store, share_uid, token, ip="", now_t=None):
    """(share row, None) or (None, (status, message)). Constant-time token check; limits wrong tries per address."""
    now_t = now_t or time.time()
    ip_key = (ip or "").split(" via ")[0]
    with _lock:
        if len([t for t in _fails.get(ip_key, []) if now_t - t < FAIL_WINDOW]) >= FAIL_MAX:
            return None, (429, "too many wrong tries; wait 15 minutes")
    rows = store._rows("SELECT * FROM partner_shares WHERE uid=?", (share_uid,)) if SHARE_RE.match(share_uid or "") else []
    good = bool(rows and token and hmac.compare_digest(rows[0]["token_hash"], token_hash(token)))
    if not good or rows[0]["revoked_at"]:
        _limited(_fails, ip_key, FAIL_WINDOW, 10 ** 9, now_t)
        return None, (401 if not good else 403, "unknown share or wrong token" if not good else "this share was turned off")
    if _limited(_uses, share_uid, USE_WINDOW, USE_MAX, now_t):
        return None, (429, "asked too often; once every few minutes is plenty")
    store._exec("UPDATE partner_shares SET last_used_at=?, uses=uses+1 WHERE id=?", (_now(), rows[0]["id"]))
    return rows[0], None


def _household(store):
    return str((store.get_raw("plan_info", {}) or {}).get("household_name") or "").strip()[:80]


def payload(store, share):
    """Only the chosen pieces, each from an allow-list of fields."""
    parts = clean_parts(json.loads(share["parts"] or "[]"))
    out = {"bunkr_partner": 1, "from": _household(store) or "BUNKR", "share": share["name"], "as_of": _now(), "includes": parts}
    if "board" in parts:
        b = checkins.board(store)
        out["board"] = [{"name": p["name"], "status": p["last"]["status"] if p["last"] else "",
                         "status_label": p["status_label"], "at": p["last"]["at"] if p["last"] else ""} for p in b["people"]]
    if "calls" in parts:
        c = checkins.active_call(store)
        out["call"] = {"kind": c["kind"], "label": c["label"], "place": c["place"], "message": c["message"],
                       "created_at": c["created_at"], "drill": bool(c["drill"])} if c else None
    if "comms" in parts:
        out["nets"] = [{k: n[k] for k in ("name", "freq", "mode", "weekday", "time", "net_control", "notes", "when")}
                       for n in comms.nets(store) if n["active"]]
        out["channels"] = [{"slot": c.get("slot"), "name": c["name"], "service": c["service"], "rx_mhz": c["rx_mhz"],
                            "duplex": c["duplex"], "offset_mhz": c["offset_mhz"], "tx_tone": c.get("tx_tone") or "",
                            "rx_tone": c.get("rx_tone") or "", "mode": c["mode"]}
                           for c in store._rows("SELECT * FROM channels WHERE in_plan=1 ORDER BY slot, sort, rx_mhz")]
        out["radios"] = [{"name": r["name"], "model": r.get("model") or "", "service": r.get("service") or ""}
                         for r in store._rows("SELECT * FROM radios ORDER BY name")]
    if "licences" in parts:
        out["callsigns"] = comms.callsigns(store)
    return out


# ---------- receiving side ----------
def token_file(folder, partner_id):
    return Path(folder) / f"partner-{int(partner_id)}.token"


def _write_token(folder, partner_id, token):
    path = token_file(folder, partner_id)
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text(token + "\n")


def has_token(folder, partner_id):
    p = token_file(folder, partner_id)
    return p.exists() and p.stat().st_size > 0


def _clean_url(url):
    url = str(url or "").strip()
    if not re.fullmatch(r"https?://[^\s/]+/api/partner/[0-9a-f]{32}", url):
        raise ValueError("paste the whole share address, like https://their-bunkr/api/partner/1a2b...")
    return url


def _clean_token(token):
    token = str(token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{20,200}", token):
        raise ValueError("that doesn't look like a share token")
    return token


def add_partner(store, folder, data):
    name = str(data.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("name the partner")
    url, token = _clean_url(data.get("url")), _clean_token(data.get("token"))
    pid = store._exec("INSERT INTO partners(name, url, created_at) VALUES(?,?,?)", (name, url, _now()))
    _write_token(folder, pid, token)
    return pid


def update_partner(store, folder, pid, data):
    if not store._rows("SELECT id FROM partners WHERE id=?", (pid,)):
        raise LookupError("no such partner")
    if str(data.get("name") or "").strip():
        store._exec("UPDATE partners SET name=? WHERE id=?", (str(data["name"]).strip()[:80], pid))
    if data.get("url"):
        store._exec("UPDATE partners SET url=? WHERE id=?", (_clean_url(data["url"]), pid))
    if data.get("token"):
        _write_token(folder, pid, _clean_token(data["token"]))


def remove_partner(store, folder, pid):
    store._exec("DELETE FROM partners WHERE id=?", (pid,))
    token_file(folder, pid).unlink(missing_ok=True)


def _s(v, n=200):
    return str(v)[:n] if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def clean_payload(d):
    """What we keep from a partner's answer: known fields, short strings, nothing else."""
    if not isinstance(d, dict) or d.get("bunkr_partner") != 1:
        raise ValueError("that address didn't answer like a BUNKR partner share")
    lst = lambda k: [x for x in (d.get(k) or [])[:100] if isinstance(x, dict)] if isinstance(d.get(k), list) else None  # noqa: E731
    out = {"from": _s(d.get("from"), 80), "share": _s(d.get("share"), 80), "as_of": _s(d.get("as_of"), 40),
           "includes": [p for p in PARTS if p in (d.get("includes") or [])]}
    if lst("board") is not None:
        out["board"] = [{"name": _s(p.get("name"), 80), "status": _s(p.get("status"), 20), "status_label": _s(p.get("status_label"), 40),
                         "at": _s(p.get("at"), 40)} for p in lst("board")]
    if "call" in d:
        c = d.get("call") if isinstance(d.get("call"), dict) else None
        out["call"] = {"label": _s(c.get("label"), 40), "kind": _s(c.get("kind"), 20), "place": _s(c.get("place"), 120),
                       "message": _s(c.get("message"), 500), "created_at": _s(c.get("created_at"), 40),
                       "drill": c.get("drill") is True} if c else None
    if lst("nets") is not None:
        out["nets"] = [{k: _s(n.get(k), 120) for k in ("name", "freq", "mode", "time", "net_control", "notes", "when")} for n in lst("nets")]
    if lst("channels") is not None:
        out["channels"] = [{"slot": _num(c.get("slot")), "name": _s(c.get("name"), 30), "service": _s(c.get("service"), 10),
                            "rx_mhz": _num(c.get("rx_mhz")), "duplex": _s(c.get("duplex"), 6), "offset_mhz": _num(c.get("offset_mhz")),
                            "tx_tone": _s(c.get("tx_tone"), 10), "rx_tone": _s(c.get("rx_tone"), 10), "mode": _s(c.get("mode"), 10)}
                           for c in lst("channels")]
    if lst("radios") is not None:
        out["radios"] = [{k: _s(r.get(k), 80) for k in ("name", "model", "service")} for r in lst("radios")]
    if lst("callsigns") is not None:
        out["callsigns"] = [{k: _s(c.get(k), 40) for k in ("name", "kind", "class", "callsign")} for c in lst("callsigns")]
    return out


def pull(store, folder, partner, opener=None):
    """Read one partner now. Keeps the last good answer when this one fails."""
    try:
        token = token_file(folder, partner["id"]).read_text().strip()
    except OSError:
        token = ""
    err = ""
    try:
        if not token:
            raise ValueError("no token saved; edit the partner and paste it again")
        req = urllib.request.Request(partner["url"], headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT,
                                                              "Accept": "application/json"})
        with (opener or urllib.request.build_opener()).open(req, timeout=15) as r:
            raw = r.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("their answer was too big")
        data = clean_payload(json.loads(raw))
        store._exec("UPDATE partners SET data=?, as_of=?, fetched_at=?, last_try=?, last_error='' WHERE id=?",
                    (json.dumps(data), data["as_of"], _now(), _now(), partner["id"]))
        return True
    except urllib.error.HTTPError as exc:
        err = {401: "their hub doesn't know this token", 403: "they turned this share off",
               429: "asked too often; trying again later"}.get(exc.code, f"their hub answered {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        err = f"couldn't reach their hub ({getattr(exc, 'reason', exc)})"
    except ValueError as exc:
        err = str(exc)
    store._exec("UPDATE partners SET last_try=?, last_error=? WHERE id=?", (_now(), err[:200], partner["id"]))
    return False


def pull_all(store, folder, opener=None):
    return {p["id"]: pull(store, folder, p, opener) for p in store._rows("SELECT * FROM partners")}


def view(store, folder, admin=False):
    out = []
    for p in store._rows("SELECT * FROM partners ORDER BY name"):
        try:
            data = json.loads(p["data"] or "{}")
        except ValueError:
            data = {}
        row = {"id": p["id"], "name": p["name"], "as_of": p["as_of"], "fetched_at": p["fetched_at"], "last_try": p["last_try"],
               "last_error": p["last_error"], "data": data}
        if admin:
            row |= {"url": p["url"], "has_token": has_token(folder, p["id"])}
        out.append(row)
    return {"partners": out, "parts": PARTS}
