"""Logins: users with PBKDF2 password hashes, cookie sessions, lockout after failures.

Roles: "admin" can do everything (documents, drives, backups, nodes, users);
"family" can use inventory, locations, kits, shopping and sites. Only the
SHA-256 of a session token is stored, so a copied database can't log anyone in.
"""

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

ITERATIONS = 600_000
MIN_PASSWORD = 10
SESSION_DAYS = 30
MAX_FAILS = 5
COOKIE = "bunkr_session"
ROLES = ("admin", "family")


class AuthError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _iso(d):
    return d.isoformat(timespec="seconds")


def _b64(b):
    return base64.b64encode(b).decode()


def hash_password(password, iterations=ITERATIONS):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(dk)}"


def verify_password(password, stored):
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


# a fixed hash so unknown usernames take as long as wrong passwords
_DUMMY = hash_password("not a real password", ITERATIONS)


def check_password_rules(password):
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"passwords need at least {MIN_PASSWORD} characters")


VIEWS = ("", "simple", "full")


def users(store):
    return store._rows("SELECT id, username, role, view, created_at, last_login, locked_until FROM users ORDER BY username")


def user_by_name(store, username):
    rows = store._rows("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
    return rows[0] if rows else None


def create_user(store, username, password, role="family"):
    username = (username or "").strip()
    if not username or len(username) > 40 or any(c.isspace() for c in username):
        raise AuthError("usernames are 1-40 characters with no spaces")
    if role not in ROLES:
        raise AuthError("role must be admin or family")
    check_password_rules(password)
    if user_by_name(store, username):
        raise AuthError("that username is taken")
    return store._exec("INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
                       (username, hash_password(password), role, _iso(_now())))


def set_password(store, user_id, password):
    check_password_rules(password)
    store._exec("UPDATE users SET password_hash=?, failed=0, locked_until='' WHERE id=?", (hash_password(password), user_id))
    store._exec("DELETE FROM sessions WHERE user_id=?", (user_id,))  # log out everywhere


def set_role(store, user_id, role):
    if role not in ROLES:
        raise AuthError("role must be admin or family")
    if role != "admin" and _is_last_admin(store, user_id):
        raise AuthError("keep at least one admin")
    store._exec("UPDATE users SET role=? WHERE id=?", (role, user_id))


def delete_user(store, user_id):
    if _is_last_admin(store, user_id):
        raise AuthError("keep at least one admin")
    store._exec("DELETE FROM sessions WHERE user_id=?", (user_id,))
    store._exec("DELETE FROM users WHERE id=?", (user_id,))


def _is_last_admin(store, user_id):
    admins = [u["id"] for u in users(store) if u["role"] == "admin"]
    return admins == [user_id]


def check_password(store, username, password):
    """The user row if the password is right, or raise AuthError. Repeated failures lock the account for a growing
    time (1, 2, 4 ... up to 60 minutes)."""
    user = user_by_name(store, username)
    if not user:
        verify_password(password or "", _DUMMY)
        raise AuthError("wrong username or password")
    if user["locked_until"] and user["locked_until"] > _iso(_now()):
        raise AuthError("too many failed attempts; try again in a few minutes")
    if not verify_password(password or "", user["password_hash"]):
        note_failure(store, user)
        raise AuthError("wrong username or password")
    return user


def note_failure(store, user):
    """A wrong password or a wrong two-step code: count it towards the lockout."""
    failed = user["failed"] + 1
    locked = ""
    if failed >= MAX_FAILS:
        locked = _iso(_now() + timedelta(minutes=min(60, 2 ** (failed - MAX_FAILS))))
    store._exec("UPDATE users SET failed=?, locked_until=? WHERE id=?", (failed, locked, user["id"]))
    return failed


def is_locked(store, user_id):
    r = store._rows("SELECT locked_until FROM users WHERE id=?", (user_id,))
    return bool(r and r[0]["locked_until"] and r[0]["locked_until"] > _iso(_now()))


def new_session(store, user_id, agent="", scope="full"):
    """A session token. scope "setup" may only finish setting up two-step login (see server)."""
    token = secrets.token_urlsafe(32)
    now = _now()
    store._exec("UPDATE users SET failed=0, locked_until='', last_login=? WHERE id=?", (_iso(now), user_id))
    days = SESSION_DAYS if scope == "full" else 1
    store._exec("INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen, agent, scope) VALUES(?,?,?,?,?,?,?)",
                (_token_hash(token), user_id, _iso(now), _iso(now + timedelta(days=days)), _iso(now), agent[:200], scope))
    store._exec("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))
    return token


def upgrade_session(store, token):
    """Two-step login is set up: the setup-only session becomes a normal one."""
    now = _now()
    store._exec("UPDATE sessions SET scope='full', expires_at=? WHERE token_hash=?",
                (_iso(now + timedelta(days=SESSION_DAYS)), _token_hash(token)))


def login(store, username, password, agent=""):
    """Password only: a new session token, or raise AuthError. (The web login adds two-step login on top.)"""
    return new_session(store, check_password(store, username, password)["id"], agent)


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def session_user(store, token):
    """The logged-in user for a session token, extending sessions that are in use."""
    if not token:
        return None
    rows = store._rows("""SELECT s.token_hash, s.expires_at, s.last_seen, s.scope, u.id, u.username, u.role, u.view
                          FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?""", (_token_hash(token),))
    if not rows:
        return None
    s = rows[0]
    now = _now()
    if s["expires_at"] < _iso(now):
        store._exec("DELETE FROM sessions WHERE token_hash=?", (s["token_hash"],))
        return None
    if s["last_seen"] < _iso(now - timedelta(hours=1)) and s["scope"] == "full":  # sliding expiry, written at most hourly
        store._exec("UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?",
                    (_iso(now), _iso(now + timedelta(days=SESSION_DAYS)), s["token_hash"]))
    return {"id": s["id"], "username": s["username"], "role": s["role"], "view": s["view"] or "", "scope": s["scope"]}


def logout(store, token):
    if token:
        store._exec("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def cookie_header(token, secure, max_age=SESSION_DAYS * 86400):
    parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def token_from_cookie(header):
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return None
