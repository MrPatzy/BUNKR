"""The Home screen's tiles: which ones each person sees, in what order, and which are folded shut.

The family call (when there is one) and the check-in buttons are always first and can't be moved or hidden.
Everything below them is a tile. Each login keeps its own layout (users.home_layout), so it follows them to
every phone; an admin sets the layout new logins start with (settings: home_layout_default) and can reset anyone's.
A tile the code adds later shows up in its default spot for everyone, without anyone having to re-arrange.
"""

import json

# (id, title, who can have it)  "admin" = admins only, "caller" = people who can send family calls
TILES = [
    ("board", "Family", "all"),
    ("alerts", "Alerts near us", "all"),
    ("news", "Today's news", "all"),
    ("med", "Medicines", "all"),
    ("lookup", "Look it up", "all"),
    ("places", "Where we meet", "all"),
    ("contacts", "Who to call", "all"),
    ("ready", "Are we ready?", "all"),
    ("lastcall", "Last family call", "all"),
    ("sendcall", "Send a family call", "caller"),
    ("push", "Notifications on this phone", "all"),
    ("backup", "Backups", "admin"),
    ("hub", "Hub computer", "admin"),
]
TILE_IDS = [t[0] for t in TILES]
BUILT_IN = {"order": TILE_IDS, "hidden": [], "collapsed": ["push", "lastcall"]}


def clean(layout):
    """A layout as sent by a browser, reduced to known tiles. Unknown keys and ids are dropped."""
    layout = layout if isinstance(layout, dict) else {}
    out = {}
    for key in ("order", "hidden", "collapsed"):
        seen = []
        for t in layout.get(key) or []:
            if isinstance(t, str) and t in TILE_IDS and t not in seen:
                seen.append(t)
        out[key] = seen
    return out


def _parse(raw):
    try:
        return clean(json.loads(raw)) if raw else None
    except (ValueError, TypeError):
        return None


def default(store):
    return _parse(store.get_raw("home_layout_default", "")) or clean(BUILT_IN)


def complete(layout):
    """Put tiles missing from the order back where the built-in order has them."""
    order = list(layout["order"])
    for i, t in enumerate(TILE_IDS):
        if t not in order:
            before = [x for x in TILE_IDS[:i] if x in order]
            order.insert(order.index(before[-1]) + 1 if before else 0, t)
    return {**layout, "order": order}


def for_user(store, user):
    raw = None
    if user and user.get("id"):
        rows = store._rows("SELECT home_layout FROM users WHERE id=?", (user["id"],))
        raw = rows[0]["home_layout"] if rows else None
    mine = _parse(raw)
    return complete(mine or default(store)) | {"custom": bool(mine)}


def save_user(store, user_id, layout):
    lay = clean(layout)
    store._exec("UPDATE users SET home_layout=? WHERE id=?", (json.dumps(lay), user_id))
    return lay


def reset_user(store, user_id):
    store._exec("UPDATE users SET home_layout='' WHERE id=?", (user_id,))


def save_default(store, layout):
    lay = clean(layout)
    store.set_raw("home_layout_default", json.dumps(lay))
    return lay
