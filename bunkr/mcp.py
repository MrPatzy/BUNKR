"""The optional AI connector: BUNKR as an MCP server (Streamable HTTP, with its own OAuth sign-in).

Off until an admin turns it on (ai.py, Household tab). The AI app reaches /mcp from its maker's servers, so this
path (and /.well-known) bypasses Cloudflare Access; BUNKR's own sign-in protects it instead. Users sign in with
their BUNKR login on BUNKR's approval page; the app gets a short-lived access token and a refresh token.
Only AI apps the admin lists on the AI card can connect (apps()): their exact sign-in callback addresses, their web
addresses, and optionally the networks their servers connect from. A new install lists none; an install upgraded
from 1.1 keeps the apps already signed in. Nothing is wildcarded: callbacks match exactly, origins are https only.

What the assistant may do is deliberately narrow: add, edit and move inventory items; read inventory,
readiness, the shopping list, maintenance tasks and the radio channel plan; mark tasks done;
undo its own changes. Never: delete things it didn't add, firearms or serial numbers,
documents, users, the radio plan. Every change is logged with how to undo it.
"""

import base64
import hashlib
import html
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_LISTED = 20   # of each kind on the connector's app list
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=60)
CODE_TTL = timedelta(minutes=10)
MAX_CLIENTS = 50
SCOPE = "bunkr"


def _now():
    return datetime.now(timezone.utc)


def _iso(d):
    return d.isoformat(timespec="seconds")


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


class OAuthError(Exception):
    def __init__(self, error, desc="", status=400):
        super().__init__(desc or error)
        self.error, self.desc, self.status = error, desc, status


# ---------------- discovery ----------------

def protected_resource(base):
    return {"resource": base + "/mcp", "authorization_servers": [base], "bearer_methods_supported": ["header"],
            "scopes_supported": [SCOPE], "resource_name": "BUNKR"}


def auth_server(base):
    return {"issuer": base, "authorization_endpoint": base + "/mcp/oauth/authorize",
            "token_endpoint": base + "/mcp/oauth/token", "registration_endpoint": base + "/mcp/oauth/register",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE]}


# ---------------- clients (dynamic registration) ----------------

# ---------------- which AI apps may connect (admin list, AI card) ----------------

_HOST = r"[a-z0-9](?:[a-z0-9\-]{0,62}\.)*[a-z0-9\-]{1,63}(?::\d{1,5})?"


def _redirect(u):
    """An exact https callback address (no wildcards, user names or fragments), or ValueError."""
    u = str(u or "").strip()
    p = urlparse(u)
    if (len(u) > 300 or p.scheme != "https" or not re.fullmatch(_HOST, p.netloc) or p.fragment or "*" in u
            or not re.fullmatch(r"[\w\-./~%]*", p.path) or not re.fullmatch(r"[\w\-.=&%]*", p.query)):
        raise ValueError(f"a sign-in callback is a full https address, like https://app.example/oauth/callback ({u[:60]})")
    return u


def _origin(u):
    u = str(u or "").strip().rstrip("/").lower()
    p = urlparse(u)
    if p.scheme != "https" or not re.fullmatch(_HOST, p.netloc) or u != f"https://{p.netloc}":
        raise ValueError(f"an app's web address is https:// and a name, like https://app.example ({u[:60]})")
    return u


def _network(n):
    import ipaddress
    try:
        net = ipaddress.ip_network(str(n or "").strip(), strict=False)
    except ValueError:
        raise ValueError(f"a server network looks like 203.0.113.0/24 ({str(n)[:40]})") from None
    if net.prefixlen < (8 if net.version == 4 else 16):
        raise ValueError(f"{net} is too wide: list the AI app's own networks")
    return str(net)


def _clean(data, key, check):
    v = data.get(key) or []
    if isinstance(v, str):
        v = v.split()
    if not isinstance(v, list):
        raise ValueError("send a list")
    out = sorted({check(x) for x in v if str(x).strip()})
    if len(out) > MAX_LISTED:
        raise ValueError(f"list at most {MAX_LISTED}")
    return out


def _seed(store):
    """An install from before the list was a setting: keep the apps already signed up (their callbacks were checked)."""
    uris = set()
    for c in store._rows("SELECT redirect_uris FROM mcp_clients"):
        for u in json.loads(c["redirect_uris"] or "[]"):
            try:
                uris.add(_redirect(u))
            except ValueError:
                pass
    return {"redirects": sorted(uris), "origins": sorted({_origin(f"https://{urlparse(u).netloc}") for u in uris}), "egress": []}


def apps(store):
    """The AI apps that may connect: {"redirects": [...], "origins": [...], "egress": [...]} (egress empty = not checked)."""
    a = store.get_raw("mcp_apps", None)
    if not isinstance(a, dict):
        a = _seed(store)
        store.set_raw("mcp_apps", a)
    return {k: [str(x) for x in a.get(k) or []] for k in ("redirects", "origins", "egress")}


def save_apps(store, data):
    if not isinstance(data, dict):
        raise ValueError("send the connector's app list")
    a = apps(store)
    for key, check in (("redirects", _redirect), ("origins", _origin), ("egress", _network)):
        if key in data:
            a[key] = _clean(data, key, check)
    store.set_raw("mcp_apps", a)
    return a


def origins(store):
    """Web addresses a listed app's pages may come from: its own, plus its callbacks' (the sign-in form posts there)."""
    a = apps(store)
    return sorted(set(a["origins"]) | {f"https://{urlparse(u).netloc}" for u in a["redirects"]})


def _redirect_ok(store, uri):
    return uri in apps(store)["redirects"]


def from_assistant(store, cf_ip):
    """True when the request didn't come through Cloudflare (LAN), no server networks are listed, or it came from one."""
    egress = apps(store)["egress"]
    if not cf_ip or not egress:
        return True
    import ipaddress
    try:
        ip = ipaddress.ip_address(cf_ip.strip())
        return any(ip in ipaddress.ip_network(n) for n in egress)
    except ValueError:
        return False


def register(store, body):
    uris = body.get("redirect_uris") or []
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and _redirect_ok(store, u) for u in uris):
        raise OAuthError("invalid_redirect_uri", "only a listed AI app's own callback addresses can register")
    method = body.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise OAuthError("invalid_client_metadata", "public clients only (token_endpoint_auth_method none)")
    # unauthenticated registration is open by design: cap it and drop old clients that never got a token
    old = _iso(_now() - timedelta(days=1))
    store._exec("""DELETE FROM mcp_clients WHERE created_at < ? AND client_id NOT IN (SELECT client_id FROM mcp_tokens)""", (old,))
    if store._rows("SELECT COUNT(*) AS n FROM mcp_clients")[0]["n"] >= MAX_CLIENTS:
        raise OAuthError("temporarily_unavailable", "too many registered clients", 503)
    cid = "bunkr-" + secrets.token_urlsafe(18)
    name = str(body.get("client_name") or "AI assistant")[:80]
    store._exec("INSERT INTO mcp_clients(client_id, name, redirect_uris, created_at) VALUES(?,?,?,?)",
                (cid, name, json.dumps(uris), _iso(_now())))
    return {"client_id": cid, "client_name": name, "redirect_uris": uris, "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "client_id_issued_at": int(_now().timestamp())}


def client(store, cid):
    rows = store._rows("SELECT * FROM mcp_clients WHERE client_id=?", (cid or "",))
    if not rows:
        return None
    c = rows[0]
    c["redirect_uris"] = json.loads(c["redirect_uris"])
    return c


# ---------------- authorize ----------------

def check_authorize(store, q):
    """Validate an authorization request. Errors here are shown on the page, never redirected."""
    c = client(store, q.get("client_id"))
    if not c:
        raise OAuthError("invalid_client", "unknown client; remove and re-add the connector in your AI app")
    ru = q.get("redirect_uri") or (c["redirect_uris"][0] if len(c["redirect_uris"]) == 1 else "")
    if ru not in c["redirect_uris"]:
        raise OAuthError("invalid_request", "redirect address doesn't match the registered one")
    if not _redirect_ok(store, ru):
        raise OAuthError("invalid_request", "this AI app isn't on the connector's list (AI card, Household tab)")
    if q.get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "response_type must be code")
    if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
        raise OAuthError("invalid_request", "PKCE (S256) is required")
    return c, ru


PERMS = ["Add, edit and move inventory items", "Add storage locations, vehicles and go-bags", "Read your inventory, readiness score and shopping list",
         "Read and tick off maintenance tasks", "Read the radio channel plan",
         "Read the Situation alerts and the morning brief", "Read family check-ins, check in, and send or end a family call",
         "Undo its own changes"]
DENIED = ["Delete anything it didn't add", "Firearms or serial numbers", "Documents, logins or the radio plan settings"]


def authorize_page(q, c, error=""):
    e = html.escape
    hidden = "".join(f'<input type="hidden" name="{e(k)}" value="{e(v)}">' for k, v in q.items()
                     if k in ("client_id", "redirect_uri", "response_type", "code_challenge", "code_challenge_method",
                              "state", "scope", "resource"))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect an AI assistant to BUNKR</title><style>
:root{{--bg:#f6f5f1;--fg:#1b1b1b;--card:#fff;--line:#d9d6cc;--mut:#5d5b55;--acc:#2f6f4f;--bad:#a33}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1d1b;--fg:#ecebe6;--card:#262825;--line:#3b3d39;--mut:#a9a79f;--acc:#7cc39b;--bad:#e88}}}}
*{{box-sizing:border-box}}body{{margin:0;padding:16px;font:16px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:440px;margin:0 auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px}}
h1{{font-size:21px;margin:0 0 6px}}p,li{{color:var(--mut)}}ul{{padding-left:20px}}label{{display:block;margin:10px 0 4px;font-weight:600}}
input[type=text],input[type=password]{{width:100%;padding:10px;font-size:16px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}}
button{{margin-top:16px;width:100%;padding:12px;font-size:16px;border:0;border-radius:8px;background:var(--acc);color:#fff;font-weight:600}}
.err{{color:var(--bad);font-weight:600}}</style></head><body><main>
<h1>Connect an AI assistant to BUNKR</h1><p><b>{e(c['name'])}</b> is asking to use your BUNKR.</p>
<p>It will be able to:</p><ul>{''.join(f'<li>{e(x)}</li>' for x in PERMS)}</ul>
<p>It will not be able to touch:</p><ul>{''.join(f'<li>{e(x)}</li>' for x in DENIED)}</ul>
{f'<p class="err">{e(error)}</p>' if error else ''}
<form method="post">{hidden}<label for="u">BUNKR username</label><input id="u" type="text" name="username" autocomplete="username" autocapitalize="none" required>
<label for="p">Password</label><input id="p" type="password" name="password" autocomplete="current-password" required>
<label for="c">Code from your authenticator app (if you use two-step login)</label><input id="c" type="text" name="code" inputmode="numeric" autocomplete="one-time-code">
<button type="submit">Sign in and allow</button></form></main></body></html>"""


def issue_code(store, c, ru, user_id, q):
    code = secrets.token_urlsafe(32)
    store._exec("INSERT INTO mcp_codes(code_hash, client_id, user_id, redirect_uri, challenge, resource, expires_at) VALUES(?,?,?,?,?,?,?)",
                (_h(code), c["client_id"], user_id, ru, q["code_challenge"], q.get("resource") or "", _iso(_now() + CODE_TTL)))
    params = {"code": code}
    if q.get("state"):
        params["state"] = q["state"]
    sep = "&" if "?" in ru else "?"
    return ru + sep + urlencode(params)


def error_redirect(ru, q, error, desc):
    params = {"error": error, "error_description": desc}
    if q.get("state"):
        params["state"] = q["state"]
    return ru + ("&" if "?" in ru else "?") + urlencode(params)


# ---------------- tokens ----------------

def _tokens(store, cid, user_id):
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
    now = _now()
    store._exec("INSERT INTO mcp_tokens(token_hash, kind, client_id, user_id, expires_at, created_at) VALUES(?,?,?,?,?,?)",
                (_h(access), "access", cid, user_id, _iso(now + ACCESS_TTL), _iso(now)))
    store._exec("INSERT INTO mcp_tokens(token_hash, kind, client_id, user_id, expires_at, created_at) VALUES(?,?,?,?,?,?)",
                (_h(refresh), "refresh", cid, user_id, _iso(now + REFRESH_TTL), _iso(now)))
    store._exec("DELETE FROM mcp_tokens WHERE expires_at < ?", (_iso(now),))
    return {"access_token": access, "token_type": "Bearer", "expires_in": int(ACCESS_TTL.total_seconds()),
            "refresh_token": refresh, "scope": SCOPE}


def token(store, form):
    grant = form.get("grant_type")
    cid = form.get("client_id")
    if not client(store, cid):
        raise OAuthError("invalid_client", "unknown client", 401)
    if grant == "authorization_code":
        rows = store._rows("SELECT * FROM mcp_codes WHERE code_hash=?", (_h(form.get("code") or ""),))
        store._exec("DELETE FROM mcp_codes WHERE code_hash=? OR expires_at < ?", (_h(form.get("code") or ""), _iso(_now())))
        if not rows or rows[0]["expires_at"] < _iso(_now()) or rows[0]["client_id"] != cid:
            raise OAuthError("invalid_grant", "code expired or already used")
        row = rows[0]
        if form.get("redirect_uri") and form["redirect_uri"] != row["redirect_uri"]:
            raise OAuthError("invalid_grant", "redirect address mismatch")
        verifier = form.get("code_verifier") or ""
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not secrets.compare_digest(digest, row["challenge"]):
            raise OAuthError("invalid_grant", "PKCE check failed")
        return _tokens(store, cid, row["user_id"])
    if grant == "refresh_token":
        rows = store._rows("SELECT * FROM mcp_tokens WHERE token_hash=? AND kind='refresh'", (_h(form.get("refresh_token") or ""),))
        if not rows or rows[0]["expires_at"] < _iso(_now()) or rows[0]["client_id"] != cid:
            raise OAuthError("invalid_grant", "refresh token expired; reconnect in your AI app")
        store._exec("DELETE FROM mcp_tokens WHERE token_hash=?", (rows[0]["token_hash"],))  # rotate
        return _tokens(store, cid, rows[0]["user_id"])
    raise OAuthError("unsupported_grant_type", "use authorization_code or refresh_token")


def bearer_user(store, header):
    if not (header or "").startswith("Bearer "):
        return None
    rows = store._rows("""SELECT t.token_hash, t.expires_at, u.id, u.username, u.role FROM mcp_tokens t
                          JOIN users u ON u.id = t.user_id WHERE t.token_hash=? AND t.kind='access'""", (_h(header[7:].strip()),))
    if not rows or rows[0]["expires_at"] < _iso(_now()):
        return None
    store._exec("UPDATE mcp_tokens SET last_used=? WHERE token_hash=?", (_iso(_now()), rows[0]["token_hash"]))
    return {"id": rows[0]["id"], "username": rows[0]["username"], "role": rows[0]["role"]}


def parse_form(raw, ctype):
    if "json" in (ctype or ""):
        try:
            d = json.loads(raw or b"{}")
            return {k: str(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except ValueError:
            return {}
    return {k: v[0] for k, v in parse_qs((raw or b"").decode(errors="replace")).items()}


# ---------------- JSON-RPC ----------------

class ToolError(Exception):
    pass


def handle_rpc(msg, tools, user, run_tool):
    """One JSON-RPC message -> response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "invalid request"}}
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if mid is None:  # notification
        return None
    ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "bunkr", "title": "BUNKR", "version": "1.0"},
                   "instructions": "BUNKR is the household's preparedness inventory. Add and update supplies with "
                                   "add_item / update_item; check locations with list_locations first if unsure. "
                                   "Quantities are packs: 1 case of 24 x 500 ml bottles is quantity 1, pack_size 24, "
                                   "size 500, size_unit ml. Use undo_change to take back a mistake."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{k: v for k, v in t.items() if k != "fn"} for t in tools.values()]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in tools:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            text = run_tool(name, args, user)
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except ToolError as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
