"""Fast entry: paste a shopping list or receipt to add many items, and scan barcodes.

Pasted text is turned into a preview (quantity, unit, name, a guessed category) that the person checks before
anything is saved. Nothing is guessed as a firearm: those stay a deliberate, admin-only entry.

Barcodes: BUNKR remembers each barcode it has seen (`barcodes` table: code -> product name and the item it was saved
as), so scanning the same can again adds one to that item with no internet. An unknown code is looked up on Open
Food Facts (free, open data; only the barcode number is sent, with a generic app name). For food it also gives
calories per package, so scanned food counts toward "days of food" straight away. Then Kroger's catalog (when set up),
USDA FoodData Central and UPCitemdb (foodsources.py); USDA also fills in calories another source didn't have.
"""

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import foodsources, kroger, naming
from .gaps import GAL_PER

USER_AGENT = "BUNKR/0.3 (self-hosted preparedness app)"
OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json?fields=product_name,brands,quantity,product_quantity," \
          "product_quantity_unit,nutriments,categories_tags"

VOLUME = {"gal": "gal", "gals": "gal", "gallon": "gal", "gallons": "gal", "l": "L", "liter": "L", "liters": "L", "litre": "L",
          "litres": "L", "ml": "ml", "fl oz": "fl oz", "floz": "fl oz"}
COUNT_UNITS = {"can", "cans", "case", "cases", "jug", "jugs", "bottle", "bottles", "box", "boxes", "pack", "packs", "bag", "bags",
               "roll", "rolls", "jar", "jars", "pouch", "pouches", "bar", "bars", "tube", "tubes", "pair", "pairs", "set", "sets",
               "lb", "lbs", "pound", "pounds", "kg", "oz", "dozen", "packet", "packets", "kit", "kits", "bucket", "buckets"}
CATEGORY_WORDS = [  # first match wins; order matters ("water filter" is a tool, not water)
    ("shelter", r"water filter|purif|tarp|tent|blanket|sleeping bag|rope|paracord|knife|multi-?tool|gloves|duct tape|axe|shovel|hand warmer|poncho|lighter|matches"),
    ("water", r"\bwater\b"),
    ("medical", r"bandage|band-aid|gauze|first aid|ibuprofen|acetaminophen|tylenol|advil|aspirin|antibiotic ointment|mask|n95|"
                r"sanitizer|soap|tampon|pads|thermometer|tourniquet|allergy|benadryl|imodium|pepto|vitamin|toothpaste|toothbrush"),
    ("power", r"batter(y|ies)|\baa\b|\baaa\b|\bd cells?\b|flashlight|headlamp|lantern|power bank|solar|charger|candle|propane|generator|fuel"),
    ("comms", r"radio|whistle|\bmaps?\b|compass|walkie|baofeng|antenna"),
    ("sanitation", r"toilet paper|paper towel|bleach|trash bag|garbage bag|wipes|kitty litter|bucket toilet|disinfect"),
    ("vehicle", r"jumper|jump starter|tow strap|tire|fix-a-flat|ice scraper|windshield|motor oil|antifreeze"),
    ("documents", r"\bcash\b|\$\d"),
    ("food", r"\bbeans?\b|\brice\b|soup|\bmre\b|oats|oatmeal|pasta|peanut butter|tuna|flour|sugar|\bsalt\b|honey|coffee|\btea\b|"
             r"crackers|cereal|granola|jerky|chili|spam|vegetables|corn|peas|fruit|applesauce|milk|formula|ramen|noodles|"
             r"eggs|canned|freeze[- ]dried|protein|nuts|trail mix|chocolate|meals?\b|sardines|chicken|beef|lentils|bars?\b"),
]
NUM = r"(\d+(?:\.\d+)?|\d+/\d+)"


def _num(s):
    if "/" in s:
        a, b = s.split("/", 1)
        return float(a) / float(b) if float(b) else 0.0
    return float(s)


def guess_category(name):
    n = name.lower()
    for cat, pat in CATEGORY_WORDS:
        if re.search(pat, n):
            return cat
    return "other"


def parse_line(line):
    """One line of a list or receipt -> an item draft, or None for blank/price-only lines."""
    raw = re.sub(r"\s+", " ", line).strip(" -*•\t")
    raw = re.sub(r"\$\s*\d+[.,]\d{2}\s*$", "", raw).strip()   # a price at the end of a receipt line
    raw = re.sub(r"^\[[ x]\]\s*", "", raw, flags=re.I)          # checklist boxes
    if not raw or not re.search(r"[A-Za-z]", raw):
        return None
    qty, unit, size, size_unit, name = 1.0, "", 0.0, "", raw
    vol = "|".join(sorted((re.escape(k) for k in VOLUME), key=len, reverse=True))
    m = re.match(rf"^{NUM}\s*(?:x|×|\*)?\s*{NUM}[\s-]*({vol})\b\.?\s*(.*)$", raw, re.I)  # "3 x 5 gal water", "3 5-gallon jugs"
    if m:
        qty, size, size_unit, name = _num(m.group(1)), _num(m.group(2)), VOLUME[m.group(3).lower()], m.group(4)
        w = name.split(" ", 1)
        if len(w) > 1 and w[0].lower() in COUNT_UNITS:  # "jugs of water"
            unit, name = w[0].lower(), w[1]
    else:
        m = re.match(rf"^{NUM}\s*(?:x|×|\*)?\s*(.*)$", raw, re.I)  # "12 cans black beans", "2 x flashlight"
        if m:
            qty, name = _num(m.group(1)), m.group(2)
        else:
            m = re.match(rf"^(.*?)[\s,:-]+(?:x|×|\*)?\s*{NUM}\s*$", raw, re.I)  # "AA batteries x 24"
            if m and m.group(1):
                name, qty = m.group(1), _num(m.group(2))
        w = name.split(" ", 2)
        if len(w) > 1 and w[0].lower() in VOLUME:  # "2 gal water": two one-gallon units
            size, size_unit, name = 1.0, VOLUME[w[0].lower()], " ".join(w[1:])
        elif len(w) > 1 and " ".join(w[:2]).lower() in VOLUME:
            size, size_unit, name = 1.0, VOLUME[" ".join(w[:2]).lower()], " ".join(w[2:])
        elif len(w) > 1 and w[0].lower().rstrip(".") in COUNT_UNITS:
            unit, name = w[0].lower().rstrip("."), " ".join(w[1:])
    name = re.sub(r"^(of|x)\s+", "", name.strip(), flags=re.I).strip(" ,.-") or raw
    if unit == "dozen":
        qty, unit = qty * 12, ""
    cat = guess_category(name)
    out = {"name": name[:120], "quantity": round(qty, 3), "unit": unit, "category": cat, "per_unit": 0, "size_unit": ""}
    if size and size_unit:
        out["per_unit"] = round(size * GAL_PER[size_unit], 4)  # stored in gallons, shown in its own unit
        out["size_unit"] = size_unit
        if not unit:
            out["unit"] = "jugs" if size_unit == "gal" and size >= 1 else "bottles"
    return out


def parse(text, limit=200):
    out = []
    for line in (text or "").splitlines()[:limit]:
        it = parse_line(line)
        if it:
            out.append(it)
    return out


# ---------- barcodes ----------
def clean_code(code):
    c = re.sub(r"\D", "", str(code or ""))
    if not 6 <= len(c) <= 14:
        raise ValueError("a barcode is 6 to 14 digits")
    return c


def remember(store, code, item):
    """Link a barcode to the item it was saved as (and keep what we know about the product)."""
    code = clean_code(code)
    store._exec("INSERT INTO barcodes(code, item_uid, name, category, per_unit, size_unit, unit, updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET item_uid=excluded.item_uid, name=excluded.name, category=excluded.category, "
                "per_unit=excluded.per_unit, size_unit=excluded.size_unit, unit=excluded.unit, updated_at=excluded.updated_at",
                (code, item.get("uid") or "", item.get("name") or "", item.get("category") or "other", item.get("per_unit") or 0,
                 item.get("size_unit") or "", item.get("unit") or "", datetime.now(timezone.utc).isoformat(timespec="seconds")))


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)


def from_off(product):
    """An Open Food Facts product -> an item draft (calories per package for food, gallons for water)."""
    name = " ".join(x for x in ((product.get("brands") or "").split(",")[0].strip(), product.get("product_name") or "") if x).strip()
    if not name:
        return None
    tags = " ".join(product.get("categories_tags") or [])
    qty_num = product.get("product_quantity")
    try:
        qty_num = float(qty_num) if qty_num not in (None, "") else None
    except (TypeError, ValueError):
        qty_num = None
    unit = (product.get("product_quantity_unit") or "").lower()
    qty_text = re.sub(r"\s*\(.*?\)", "", str(product.get("quantity") or "")).strip()
    form = "Frozen" if "en:frozen" in tags else "Canned" if "en:canned" in tags else "Dried" if "en:dried" in tags else ""
    store_at = "Frozen" if form == "Frozen" else "Cold" if re.search(r"en:(dairies|refrigerated|fresh|chilled)", tags) else "Dry"
    parts = naming.parts((product.get("brands") or "").split(",")[0].strip(), product.get("product_name") or name,
                         size=qty_text, storage=store_at, form=form)
    if "en:waters" in tags or re.search(r"\bwater\b", name, re.I) and not re.search(r"coconut|flavou?r", name, re.I):
        parts["_form"] = parts["_form"] or "Bottled"
        naming.restock(parts, store_at)
        draft = {"name": name[:120], "category": "water", "unit": "bottles", "per_unit": 0, "size_unit": "", "parts": parts}
        if qty_num and unit in ("ml", ""):
            draft.update(per_unit=round(qty_num * GAL_PER["ml"], 4), size_unit="L" if qty_num >= 1000 else "ml")
        return draft
    kcal100 = (product.get("nutriments") or {}).get("energy-kcal_100g")
    per = 0
    try:
        if kcal100 and qty_num and unit in ("g", "ml", ""):
            per = round(float(kcal100) * qty_num / 100)
    except (TypeError, ValueError):
        per = 0
    cat = "food" if per or "en:foods" in tags or "en:plant-based-foods" in tags else guess_category(name)
    if cat not in ("food", "water"):
        naming.restock(parts, "")
    return {"name": name[:120], "category": cat, "unit": "cans" if parts["_form"] == "Canned" else "", "per_unit": per if cat == "food" else 0,
            "size_unit": "", "parts": parts, "note": " · ".join(x for x in (parts["label"], product.get("quantity") or "") if x)[:120]}


def _named(store, draft):
    """Name a looked-up product in the household's style (naming.py); the store's own name stays in the note."""
    parts = draft.pop("parts", None)
    if parts:
        draft["name"] = (naming.name(store, parts) or draft["name"])[:120]
    return draft


def lookup(store, code, get=_get, online=True):
    """What BUNKR knows about a barcode: the item it's saved as, or a product draft, or nothing."""
    code = clean_code(code)
    rows = store._rows("SELECT * FROM barcodes WHERE code=?", (code,))
    if rows:
        b = rows[0]
        item = store._rows("SELECT * FROM items WHERE uid=? AND uid != ''", (b["item_uid"],)) if b["item_uid"] else []
        if item:
            return {"code": code, "source": "saved", "item": item[0]}
        return {"code": code, "source": "saved", "draft": {k: b[k] for k in ("name", "category", "per_unit", "size_unit", "unit")}}
    if not online:
        return {"code": code, "source": "none"}
    error = ""
    draft = None
    try:
        data = get(OFF_URL.format(code=code))
        draft = from_off(data.get("product") or {}) if data.get("status") == 1 else None
    except urllib.error.HTTPError as exc:
        if exc.code != 404:  # Open Food Facts answers "not found" for products it doesn't have (store brands, often)
            error = f"couldn't reach Open Food Facts ({type(exc).__name__})"
    except Exception as exc:
        error = f"couldn't reach Open Food Facts ({type(exc).__name__})"
    source = "openfoodfacts" if draft else ""
    if not draft:
        try:  # then Kroger's catalog, when an admin has set it up
            draft = kroger.lookup(store, code)
            source = "kroger" if draft else ""
        except Exception:
            error = error or "couldn't reach Kroger"
    for name, fn in (("upcitemdb", foodsources.upcitemdb_lookup), ("usda", foodsources.usda_lookup)):  # the better name first
        if draft:
            break
        try:
            draft = fn(store, code, get=get)
            source = name if draft else ""
        except Exception:
            error = error or f"couldn't reach {foodsources.SOURCES[name]}"
    if draft:
        if not draft.get("per_unit") and source != "usda":
            foodsources.fill_calories(store, code, draft, get=get)
        return {"code": code, "source": source, "draft": _named(store, draft)}
    return {"code": code, "source": "none"} | ({"error": error} if error else {})
