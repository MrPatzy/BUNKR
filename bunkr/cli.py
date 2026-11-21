"""Command line: `python3 -m bunkr` runs the server; subcommands manage logins and sync.

  python3 -m bunkr [--db FILE] [--port N] [--require-login] ...      run the web app
  python3 -m bunkr [--db FILE] user add NAME [--admin] [--password-file F]
  python3 -m bunkr [--db FILE] user list | passwd NAME | role NAME admin|family | del NAME
  python3 -m bunkr pair --hub URL --code CODE [--token-file F]     pair a node with the hub
  python3 -m bunkr [--db FILE] sync [--token-file F] [--store DIR]   sync a node now
"""

import argparse
import getpass
import sys
from pathlib import Path

SUBCOMMANDS = ("user", "pair", "sync")
DEFAULT_TOKEN = "/etc/bunkr/node.token"


def _password(args):
    if getattr(args, "password_file", None):
        return Path(args.password_file).read_text().strip()
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    pw = getpass.getpass("New password (10+ characters): ")
    if pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords don't match.")
    return pw


def user_cmd(db, argv):
    from .vault import NodeVault
    if db and NodeVault(db).exists():
        sys.exit("Node lock is on: manage logins on this node's page while it's unlocked (Household tab).")
    return _user_cmd(db, argv)


def _user_cmd(db, argv):
    from . import auth
    from .db import Store
    ap = argparse.ArgumentParser(prog="bunkr user")
    sub = ap.add_subparsers(dest="action", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--admin", action="store_true")
    a.add_argument("--password-file")
    sub.add_parser("list")
    pw = sub.add_parser("passwd")
    pw.add_argument("name")
    pw.add_argument("--password-file")
    r = sub.add_parser("role")
    r.add_argument("name")
    r.add_argument("role", choices=auth.ROLES)
    d = sub.add_parser("del")
    d.add_argument("name")
    args = ap.parse_args(argv)
    store = Store(db)
    try:
        if args.action == "list":
            for u in auth.users(store):
                print(f"{u['username']:20} {u['role']:7} last login {u['last_login'] or 'never'}")
            return
        if args.action == "add":
            auth.create_user(store, args.name, _password(args), "admin" if args.admin else "family")
            print(f"Created {'admin' if args.admin else 'family'} login '{args.name}'.")
            return
        user = auth.user_by_name(store, args.name)
        if not user:
            sys.exit(f"No login named '{args.name}'.")
        if args.action == "passwd":
            auth.set_password(store, user["id"], _password(args))
            print("Password changed; existing sessions were logged out.")
        elif args.action == "role":
            auth.set_role(store, user["id"], args.role)
            print(f"'{args.name}' is now {args.role}.")
        elif args.action == "del":
            auth.delete_user(store, user["id"])
            print(f"Deleted '{args.name}'.")
    except auth.AuthError as exc:
        sys.exit(str(exc))


def pair_cmd(argv):
    from . import sync
    ap = argparse.ArgumentParser(prog="bunkr pair")
    ap.add_argument("--hub", required=True, help="hub address, e.g. http://192.168.1.40:8765")
    ap.add_argument("--code", help="pairing code shown on the hub's Nodes tab")
    ap.add_argument("--token-file", default=DEFAULT_TOKEN)
    args = ap.parse_args(argv)
    code = args.code or input("Pairing code from the hub's Nodes tab: ")
    res = sync.pair_node(args.hub, code, args.token_file)
    print(f"Paired as '{res['name']}' ({res['role']}). Token saved to {args.token_file}.")
    print("Run a first sync now with: python3 -m bunkr sync")


def sync_cmd(db, argv):
    from . import sync
    from .db import Store
    ap = argparse.ArgumentParser(prog="bunkr sync")
    ap.add_argument("--token-file", default=DEFAULT_TOKEN)
    ap.add_argument("--store", default="/var/lib/bunkr/store")
    ap.add_argument("--hub", help="override the hub address saved at pairing")
    ap.add_argument("--no-files", action="store_true")
    ap.add_argument("--no-records", action="store_true")
    args = ap.parse_args(argv)
    if args.hub:
        import json
        tok = sync.read_token(args.token_file)
        if tok.get("hub") != args.hub.rstrip("/"):
            tok["hub"] = args.hub.rstrip("/")
            Path(args.token_file).write_text(json.dumps(tok))
    from .vault import NodeVault
    if db and NodeVault(db).exists():
        # node lock: the database is sealed; BUNKR itself syncs records while unlocked. Files only here.
        sync.run(args.token_file, args.store, db, files=not args.no_files, records=False)
        return
    sync.run(args.token_file, args.store, db, files=not args.no_files, records=not args.no_records)
    from .manifest import now
    Store(db).set_raw("last_node_sync", now())


def default_db():
    """Nodes installed by deploy/install.sh keep data in /var/lib/bunkr."""
    p = Path("/var/lib/bunkr/bunkr.db")
    return str(p) if p.exists() else None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--db")
    known, rest = pre.parse_known_args(argv)
    cmd = rest[0] if rest and rest[0] in SUBCOMMANDS else None
    if cmd is None:
        from .server import main as serve
        sys.argv = [sys.argv[0], *argv]
        return serve()
    db = known.db or default_db()
    if cmd == "user":
        return user_cmd(db, rest[1:])
    if cmd == "pair":
        return pair_cmd(rest[1:])
    return sync_cmd(db, rest[1:])
