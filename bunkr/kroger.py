"""Barcode lookups in Kroger's product catalog (Kroger, King Soopers, Ralphs, Fred Meyer and the other Kroger stores).

Used after Open Food Facts when a scanned barcode isn't there, which is common for store brands. Optional: it does
nothing until an admin saves a free Kroger developer app's client id and secret (developer.kroger.com). The secret
is a file next to the database (`kroger.key`, mode 600), never in the database or the API. Only the barcode number
is sent, with an app-only token: no Kroger account, no store, no shopping data.
"""

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

API = "https://api.kroger.com/v1"
SECRET_FILE = "kroger.key"
USER_AGENT = f"BUNKR/{__version__} (self-hosted preparedness app)"
_token = {"value": "", "until": 0.0}
_lock = threading.Lock()

# Kroger's departments that are food (anything else is guessed from the name)
FOOD_DEPTS = re.compile(r"canned|pantry|snack|breakfast|frozen|meat|seafood|dairy|produce|bakery|deli|baking|condiment|pasta|"
                        r"rice|soup|international|beverage|coffee|candy|baby food", re.I)


def has_secret(folder):
    p = Path(folder) / SECRET_FILE
    return p.exists() and p.stat().st_size > 0


def settings(store):
    return {"client_id": str((store.get_raw("kroger", {}) or {}).get("client_id") or "")}


def ready(store, folder):
    return bool(settings(store)["client_id"] and has_secret(folder))


def view(store, folder):
    return settings(store) | {"has_secret": has_secret(folder), "ready": ready(store, folder),
                              "status": store.get_raw("kroger_status", {}) or {}}


def save_settings(store, folder, data):
    s = settings(store)
    if "client_id" in data:
        cid = str(data["client_id"] or "").strip()
        if cid and not re.fullmatch(r"[A-Za-z0-9_.\-]{8,120}", cid):
            raise ValueError("that doesn't look like a Kroger client id")
        s["client_id"] = cid
    store.set_raw("kroger", s)
    path = Path(folder) / SECRET_FILE
    if data.get("forget_secret"):
        path.unlink(missing_ok=True)
    elif data.get("client_secret"):
        sec = str(data["client_secret"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{16,200}", sec):
            raise ValueError("that doesn't look like a Kroger client secret")
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(sec + "\n")
    _token.update(value="", until=0.0)
    store.set_raw("kroger_status", {})
    return view(store, folder)


def _json(req, opener=None):
    with (opener or urllib.request.build_opener()).open(req, timeout=12) as r:
        return json.loads(r.read(2_000_000))


def _get_token(store, folder, opener=None):
    with _lock:
        if _token["value"] and time.time() < _token["until"]:
            return _token["value"]
        cid = settings(store)["client_id"]
        secret = (Path(folder) / SECRET_FILE).read_text().strip() if has_secret(folder) else ""
        if not (cid and secret):
            return ""
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        req = urllib.request.Request(API + "/connect/oauth2/token", method="POST",
                                     data=b"grant_type=client_credentials&scope=product.compact",
                                     headers={"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            d = _json(req, opener)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401):
                raise RuntimeError("Kroger didn't accept the client id and secret") from None
            raise
        if not d.get("access_token"):
            raise RuntimeError("Kroger didn't give a token")
        _token.update(value=d["access_token"], until=time.time() + int(d.get("expires_in") or 1800) - 60)
        return _token["value"]


def product_ids(code):
    """Kroger's product id is the 13-digit UPC without its check digit. Try that first, then the number as scanned."""
    out = []
    if len(code) in (12, 13):
        out.append(code[:-1].zfill(13))
    if len(code) <= 13 and code.zfill(13) not in out:
        out.append(code.zfill(13))
    return out


def _size(size):
    """ "1 gal" / "16.9 fl oz" / "1.5 L" -> (amount, unit) for water, else None."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(gal|fl oz|l|ml)\b", (size or "").lower())
    if not m:
        return None
    return float(m.group(1)), {"gal": "gal", "fl oz": "fl oz", "l": "L", "ml": "ml"}[m.group(2)]


def split_size(size):
    """ "4 ct / 15.25 oz" -> (4, "15.25 oz"); "15 oz" -> (1, "15 oz")."""
    size = re.sub(r"\s+", " ", str(size or "")).strip()
    m = re.match(r"(\d+)\s*(?:ct|pk|pack|count|cans?|bottles?)\b\s*(?:/|x)\s*(.+)$", size, re.I)
    if m and int(m.group(1)) > 1:
        return int(m.group(1)), m.group(2).strip()
    return 1, size


def storage(p):
    t = str((p.get("temperature") or {}).get("indicator") or "").lower()
    return "Frozen" if "froz" in t else "Cold" if "refrig" in t or "chill" in t or "cold" in t else "Dry" if t else ""


def calories(p):
    """(kcal per serving, servings per package) from Kroger's nutrition facts, or (0, 0)."""
    for ni in p.get("nutritionInformation") or []:
        kcal = next((n.get("quantity") for n in ni.get("nutrients") or [] if n.get("code") == "ENER-"
                     or str(n.get("displayName") or "").lower() == "calories"), None)
        servings = (ni.get("servingsPerPackage") or {}).get("value")
        try:
            if kcal and servings:
                return float(kcal), float(servings)
        except (TypeError, ValueError):
            pass
    return 0.0, 0.0


THINGS = {"Canned": "cans", "Bottled": "bottles", "Jarred": "jars"}


def to_draft(p):
    """A Kroger product -> an item draft: naming parts, calories per can (or gallons per bottle), packs."""
    from . import naming
    from .gaps import GAL_PER
    from .quickadd import guess_category
    desc = naming.tidy(p.get("description"))
    if not desc:
        return None
    brand = naming.tidy(p.get("brand"))
    raw_size = str(((p.get("items") or [{}])[0] or {}).get("size") or "").strip()
    count, each = split_size(raw_size)
    depts = " ".join(str(c) for c in p.get("categories") or [])
    store_at = storage(p)
    organic = "organic" in str(p.get("organicClaimName") or "").lower() and "no" != str(p.get("organicClaimName") or "").strip().lower()
    parts = naming.parts(brand, desc, size=each, storage=store_at, organic=organic, dept=depts)
    label = parts["label"]
    cat = guess_category(label)
    if re.search(r"\bwater\b", label, re.I) and re.search(r"beverage|water", depts, re.I) and not re.search(r"coconut|flavou?r", label, re.I):
        cat = "water"  # drinking water ("Purified Water" isn't a purifier)
        parts["_form"] = parts["_form"] or "Bottled"
    elif cat == "other" and FOOD_DEPTS.search(depts):
        cat = "food"
    naming.restock(parts, "" if cat not in ("food", "water") else "Frozen" if "Frozen" in (parts["_form"], store_at) else store_at)
    thing = THINGS.get(parts["_form"], "")
    draft = {"name": label[:120], "category": cat, "unit": "packs" if count > 1 else thing, "pack_size": count,
             "pack_unit": thing if count > 1 else "", "per_unit": 0, "size_unit": "", "parts": parts,
             "note": " · ".join(x for x in (label, raw_size) if x)[:120]}
    if cat == "water":
        sz = _size(each)
        if sz:
            draft.update(per_unit=round(sz[0] * GAL_PER[sz[1]], 4), size_unit=sz[1])
    elif cat == "food":
        kcal, servings = calories(p)
        if kcal and servings:
            draft["per_unit"] = round(kcal * servings / count)  # servings are for the whole package
    return draft


def lookup(store, code, opener=None):
    """A draft for this barcode from Kroger's catalog, or None. Raises if Kroger can't be reached or refuses the key."""
    folder = Path(store.path).parent
    if not ready(store, folder):
        return None
    try:
        tok = _get_token(store, folder, opener)
        found = None
        for pid in product_ids(code):
            req = urllib.request.Request(f"{API}/products/{urllib.parse.quote(pid)}",
                                         headers={"Authorization": "Bearer " + tok, "Accept": "application/json", "User-Agent": USER_AGENT})
            try:
                found = to_draft(_json(req, opener).get("data") or {})
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 404):
                    continue
                if exc.code == 401:
                    _token.update(value="", until=0.0)
                raise
            if found:
                break
    except Exception as exc:
        store.set_raw("kroger_status", {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160], "at": _now()})
        raise
    store.set_raw("kroger_status", {"ok": True, "found": bool(found), "at": _now()})
    return found


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
