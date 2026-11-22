"""More places to look up a scanned barcode, all free and needing no account.

Tried after Open Food Facts and Kroger, in this order:
- UPCitemdb's free trial lookup (up to 100 a day per hub, no key): names and sizes for almost anything with a
  barcode, food or not, but no calories.
- USDA FoodData Central (the US government's food database, which includes the nutrition facts brands file with
  the FDA). It has calories for many branded foods. It works with the shared "DEMO_KEY" (about 30 lookups an hour
  per hub); a free key from api.data.gov raises that. The key is a file next to the database (usda.key), never in
  the database.
- UPCitemdb's free trial lookup (up to 100 a day per hub, no key): names and sizes for almost anything with a
  barcode, food or not, but no calories.

USDA is also asked for calories when another source found the product but had none.
Only the barcode number is sent, with a generic app name. An admin can switch each source off.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, naming

USER_AGENT = f"BUNKR/{__version__} (self-hosted preparedness app)"
USDA_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
UPC_URL = "https://api.upcitemdb.com/prod/trial/lookup"
KEY_FILE = "usda.key"
SOURCES = {"usda": "USDA FoodData Central", "upcitemdb": "UPCitemdb"}
G_PER = {"g": 1.0, "kg": 1000.0, "oz": 28.3495, "lb": 453.592, "ml": 1.0, "l": 1000.0, "fl oz": 29.5735}


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(3_000_000))


# ---------- settings ----------
def settings(store):
    s = store.get_raw("food_sources", {}) or {}
    return {k: s.get(k, True) is not False for k in SOURCES}


def has_key(folder):
    p = Path(folder) / KEY_FILE
    return p.exists() and p.stat().st_size > 0


def view(store, folder):
    return {"sources": settings(store), "names": SOURCES, "usda_own_key": has_key(folder),
            "status": store.get_raw("food_sources_status", {}) or {}}


def save_settings(store, folder, data):
    s = settings(store)
    for k in SOURCES:
        if k in data:
            s[k] = bool(data[k])
    store.set_raw("food_sources", s)
    path = Path(folder) / KEY_FILE
    if data.get("forget_usda_key"):
        path.unlink(missing_ok=True)
    elif data.get("usda_key"):
        key = str(data["usda_key"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9]{20,80}", key):
            raise ValueError("that doesn't look like an api.data.gov key")
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(key + "\n")
    return view(store, folder)


def _usda_key(folder):
    try:
        return (Path(folder) / KEY_FILE).read_text().strip() or "DEMO_KEY"
    except OSError:
        return "DEMO_KEY"


def _status(store, source, ok, note=""):
    st = store.get_raw("food_sources_status", {}) or {}
    st[source] = {"ok": ok, "note": note[:160]}
    store.set_raw("food_sources_status", st)


# ---------- sizes and calories ----------
def grams(text):
    """ "16.5 oz/468 g" -> 468.0; "15 OZ" -> 425.2; "1 lb" -> 453.6; "" -> 0."""
    t = str(text or "").lower()
    found = re.findall(r"(\d+(?:\.\d+)?)\s*(fl\.? ?oz|kg|g|grams?|oz|ounces?|lbs?|pounds?|ml|l)\b", t)
    best = 0.0
    for n, u in found:
        u = {"grams": "g", "gram": "g", "ounce": "oz", "ounces": "oz", "lbs": "lb", "pound": "lb", "pounds": "lb"}.get(u, u)
        u = "fl oz" if u.startswith("fl") else u
        g = float(n) * G_PER.get(u, 0)
        if u in ("g", "ml"):
            return g               # metric on the label is the exact one
        best = best or g
    return round(best, 1)


def _same_code(a, b):
    return bool(a and b) and str(a).lstrip("0") == str(b).lstrip("0")


def _usda_food(store, code, get):
    folder = Path(store.path).parent
    q = urllib.parse.urlencode({"api_key": _usda_key(folder), "query": code, "dataType": "Branded", "pageSize": 5})
    try:
        data = get(f"{USDA_URL}?{q}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        _status(store, "usda", False, "USDA's shared key is busy: add your own free key" if exc.code == 429 else f"USDA said {exc.code}")
        raise
    _status(store, "usda", True)
    return next((f for f in data.get("foods") or [] if _same_code(f.get("gtinUpc"), code)), None)


def usda_calories(food):
    """Calories in one package from a USDA branded food: kcal per 100 g x the package weight."""
    per100 = next((n.get("value") for n in food.get("foodNutrients") or []
                   if n.get("nutrientId") in (1008, 2047, 2048) and str(n.get("unitName") or "").upper() == "KCAL"), None)
    g = grams(food.get("packageWeight"))
    try:
        return round(float(per100) * g / 100) if per100 and g else 0
    except (TypeError, ValueError):
        return 0


def _usda_draft(food):
    brand = naming.tidy(food.get("brandName") or food.get("brandOwner") or "")
    title = naming.tidy(food.get("description") or "")
    if not title:
        return None
    title = " ".join(w if len(w) <= 2 and w.isupper() and w not in ("OF", "IN") else w.capitalize() for w in title.split())
    brand = " ".join(w.capitalize() if w.isupper() else w for w in brand.split())
    size = re.sub(r"\s*/.*$", "", str(food.get("packageWeight") or "")).strip()
    parts = naming.parts(brand, title, size=size, storage="Dry", dept=str(food.get("brandedFoodCategory") or "canned"))
    label = parts["label"]
    return {"name": label[:120], "category": "food", "unit": "cans" if parts["_form"] == "Canned" else "", "per_unit": usda_calories(food),
            "size_unit": "", "parts": parts, "note": " · ".join(x for x in (label, str(food.get("packageWeight") or "")) if x)[:120]}


def usda_lookup(store, code, get=_get):
    if not settings(store)["usda"]:
        return None
    food = _usda_food(store, code, get)
    return _usda_draft(food) if food else None


def fill_calories(store, code, draft, get=_get):
    """A found product with no calories: ask USDA for them. Changes the draft; True when it did."""
    if draft.get("category") != "food" or draft.get("per_unit") or (draft.get("pack_size") or 1) > 1 or not settings(store)["usda"]:
        return False
    try:
        food = _usda_food(store, code, get)
    except Exception:
        return False
    kcal = usda_calories(food) if food else 0
    if kcal:
        draft["per_unit"] = kcal
        draft["kcal_from"] = "USDA"
    return bool(kcal)


def upcitemdb_lookup(store, code, get=_get):
    if not settings(store)["upcitemdb"]:
        return None
    from .quickadd import guess_category
    try:
        data = get(f"{UPC_URL}?{urllib.parse.urlencode({'upc': code})}")
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            return None
        _status(store, "upcitemdb", False, "UPCitemdb's free daily limit is used up" if exc.code == 429 else f"UPCitemdb said {exc.code}")
        raise
    _status(store, "upcitemdb", True)
    item = next(iter(data.get("items") or []), None)
    if not item or not item.get("title"):
        return None
    brand = naming.tidy(item.get("brand") or "")
    title = naming.tidy(item["title"])
    size = str(item.get("size") or "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(ounce|oz|fl oz|lb|pound|g|ml|l|ct|count)s?\b", size + " " + title, re.I)
    size_text = f"{m.group(1)} {m.group(2).lower().replace('ounce', 'oz').replace('pound', 'lb')}" if m else ""
    title = re.sub(r"\s+\d+(?:\.\d+)?\s*(oz|ounces?|fl oz|lbs?|g|ml|ct)\b.*$", "", title, flags=re.I).strip() or title
    if title.isupper():
        title = title.title()
    key = re.sub(r"\W", "", brand).lower()
    words = title.split()
    for n in range(1, min(4, len(words)) + 1):  # "BUSH S" for "BUSH'S Original Baked Beans": the title's spelling wins
        if key and re.sub(r"\W", "", "".join(words[:n])).lower() == key:
            brand = " ".join(words[:n])
            break
    hay = f"{title} {item.get('category') or ''}"
    cat = "food" if re.search(r"\bfood|beverage|soup|snack|grocery", str(item.get("category") or ""), re.I) else guess_category(hay)
    store_at = "Frozen" if re.search(r"frozen", hay, re.I) else "Dry"
    form = "Canned" if cat == "food" and re.search(r"\bcans?\b", size, re.I) else ""
    parts = naming.parts(brand, title, size=size_text, storage=store_at if cat in ("food", "water") else "", form=form,
                         dept=str(item.get("category") or "") + (" canned" if cat == "food" else ""))
    if cat not in ("food", "water"):
        naming.restock(parts, "")
    return {"name": parts["label"][:120], "category": cat, "unit": "cans" if parts["_form"] == "Canned" else "", "per_unit": 0,
            "size_unit": "", "parts": parts, "note": " · ".join(x for x in (parts["label"], size) if x)[:120]}
