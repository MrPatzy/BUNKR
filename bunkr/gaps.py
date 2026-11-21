"""Readiness scoring: compares inventory against a household-sized checklist.

The checklist follows Ready.gov's "Build a Kit" basic and additional supply lists
(https://www.ready.gov/kit). Water and food are scored in days of supply; the
rest are scored by count against a per-household or per-person target.
"""

from datetime import date, timedelta

CATEGORIES = {
    "water": "Water",
    "food": "Food",
    "medical": "Medical & hygiene",
    "power": "Power & light",
    "comms": "Communication & navigation",
    "shelter": "Shelter, warmth & tools",
    "sanitation": "Sanitation",
    "documents": "Documents & cash",
    "vehicle": "Vehicle",
    "firearms": "Firearms & ammunition",
    "other": "Other",
}

# scale: "household" (fixed count) or "person" (count per person; pets excluded)
ESSENTIALS = [
    {"id": "first_aid_kit", "category": "medical", "label": "First aid kit", "qty": 1, "scale": "household"},
    {"id": "rx_meds", "category": "medical", "label": "Prescription medication (days of supply)", "qty": None, "scale": "days", "optional": True},
    {"id": "otc_meds", "category": "medical", "label": "Pain relievers, antidiarrheal, antacid", "qty": 1, "scale": "household"},
    {"id": "glasses", "category": "medical", "label": "Spare glasses / contact solution", "qty": 1, "scale": "household", "optional": True},
    {"id": "masks", "category": "medical", "label": "Dust / N95 masks", "qty": 2, "scale": "person"},
    {"id": "hygiene", "category": "medical", "label": "Soap, hand sanitizer, feminine supplies", "qty": 1, "scale": "household"},
    {"id": "flashlight", "category": "power", "label": "Flashlight", "qty": 1, "scale": "person"},
    {"id": "batteries", "category": "power", "label": "Spare battery packs (sets)", "qty": 2, "scale": "household"},
    {"id": "power_bank", "category": "power", "label": "Phone charger + backup battery", "qty": 1, "scale": "household"},
    {"id": "radio", "category": "comms", "label": "Battery / hand-crank NOAA weather radio", "qty": 1, "scale": "household"},
    {"id": "whistle", "category": "comms", "label": "Whistle to signal for help", "qty": 1, "scale": "person"},
    {"id": "paper_maps", "category": "comms", "label": "Local paper maps", "qty": 1, "scale": "household"},
    {"id": "contact_list", "category": "comms", "label": "Printed emergency contact list", "qty": 1, "scale": "household"},
    {"id": "sheeting_tape", "category": "shelter", "label": "Plastic sheeting + duct tape", "qty": 1, "scale": "household"},
    {"id": "utility_tool", "category": "shelter", "label": "Wrench / pliers to shut off utilities", "qty": 1, "scale": "household"},
    {"id": "can_opener", "category": "shelter", "label": "Manual can opener", "qty": 1, "scale": "household"},
    {"id": "fire_extinguisher", "category": "shelter", "label": "Fire extinguisher", "qty": 1, "scale": "household"},
    {"id": "matches", "category": "shelter", "label": "Matches in waterproof container", "qty": 1, "scale": "household"},
    {"id": "bedding", "category": "shelter", "label": "Sleeping bag / warm blanket", "qty": 1, "scale": "person"},
    {"id": "clothing", "category": "shelter", "label": "Change of clothes + sturdy shoes", "qty": 1, "scale": "person"},
    {"id": "wipes", "category": "sanitation", "label": "Moist towelettes", "qty": 1, "scale": "household"},
    {"id": "garbage_bags", "category": "sanitation", "label": "Garbage bags + plastic ties", "qty": 1, "scale": "household"},
    {"id": "toilet_paper", "category": "sanitation", "label": "Toilet paper (rolls)", "qty": 4, "scale": "person"},
    {"id": "bleach", "category": "sanitation", "label": "Unscented bleach (disinfect / treat water)", "qty": 1, "scale": "household"},
    {"id": "cash", "category": "documents", "label": "Cash in small bills ($1 to $20)", "qty": None, "scale": "dollars"},
    {"id": "infant_supplies", "category": "food", "label": "Infant formula, bottles, diapers", "qty": 1, "scale": "household", "optional": True},
    {"id": "pet_food", "category": "food", "label": "Pet food (days of supply)", "qty": None, "scale": "pet_days"},
]

# Added to the home checklist only when the home site's FEMA National Risk Index
# rating for one of the listed hazards is Relatively Moderate or higher.
HAZARD_ESSENTIALS = [
    {"id": "smoke_filter", "category": "medical", "label": "HEPA air purifier or box-fan smoke filter", "qty": 1, "scale": "household", "hazards": ["WFIR"]},
    {"id": "cold_bedding", "category": "shelter", "label": "Cold-rated sleeping bag or extra blankets", "qty": 1, "scale": "person", "hazards": ["WNTW", "CWAV", "ISTM"]},
    {"id": "safe_heat", "category": "shelter", "label": "Safe backup heat (indoor-rated heater or wood stove) + fuel", "qty": 1, "scale": "household", "hazards": ["WNTW", "CWAV", "ISTM"]},
    {"id": "co_alarm", "category": "shelter", "label": "Battery carbon monoxide alarm", "qty": 1, "scale": "household", "hazards": ["WNTW", "CWAV", "ISTM"]},
    {"id": "cooling", "category": "power", "label": "Battery fan or cooling plan for power outages", "qty": 1, "scale": "household", "hazards": ["HWAV"]},
    {"id": "shelter_gear", "category": "shelter", "label": "Helmets and sturdy shoes kept in the shelter area", "qty": 1, "scale": "person", "hazards": ["TRND", "HRCN"]},
    {"id": "waterproof_docs", "category": "documents", "label": "Waterproof pouch for documents and cash", "qty": 1, "scale": "household", "hazards": ["IFLD", "CFLD", "HRCN"]},
    {"id": "quake_anchors", "category": "shelter", "label": "Water heater strap and furniture anchors", "qty": 1, "scale": "household", "hazards": ["ERQK"]},
    {"id": "window_protection", "category": "shelter", "label": "Window protection (shutters or pre-cut plywood)", "qty": 1, "scale": "household", "hazards": ["HRCN"]},
]

# Items that only appear in vehicle and go-bag kit targets (see kits.py).
KIT_ONLY = [
    {"id": "jumper_cables", "category": "vehicle", "label": "Jumper cables or jump starter"},
    {"id": "traction_aid", "category": "vehicle", "label": "Cat litter or sand for tire traction"},
    {"id": "shovel", "category": "vehicle", "label": "Compact shovel"},
    {"id": "ice_scraper", "category": "vehicle", "label": "Ice scraper"},
    {"id": "reflective_triangle", "category": "vehicle", "label": "Flares or reflective triangles"},
    {"id": "doc_copies", "category": "documents", "label": "Copies of key documents (or encrypted USB)"},
    {"id": "multitool", "category": "shelter", "label": "Multi-tool or knife"},
    {"id": "rain_poncho", "category": "shelter", "label": "Rain poncho"},
]

CHECKLIST = ESSENTIALS + HAZARD_ESSENTIALS + KIT_ONLY
CHECKLIST_IDS = {e["id"] for e in CHECKLIST}

# Document types the registry should cover (Ready.gov "Gather Important Documents").
DOC_TYPES = {
    "identity": "IDs, passports, driver's licenses",
    "vital": "Birth, marriage, adoption, citizenship records",
    "insurance": "Insurance policies (home, auto, health, life)",
    "property": "Deeds, titles, leases, mortgage",
    "medical": "Medical records, prescriptions, allergies",
    "financial": "Bank, retirement, tax records",
    "contacts": "Emergency contacts and account list",
    "pets": "Pet vaccination and ID records",
    "other": "Other",
}
REQUIRED_DOC_TYPES = ["identity", "vital", "insurance", "property", "medical", "financial", "contacts"]

EXPIRY_WARNING_DAYS = 60


def _parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _people(settings):
    return max(1, int(settings["adults"]) + int(settings["children"]))


def essential_target(essential, settings):
    days = int(settings["target_days"])
    scale = essential["scale"]
    if scale == "person":
        return essential["qty"] * _people(settings)
    if scale == "days":
        return days
    if scale == "dollars":
        return float(settings.get("cash_per_person", 100)) * _people(settings)
    if scale == "pet_days":
        return days if int(settings["pets"]) > 0 else 0
    return essential["qty"]


def split_expiry(items, today):
    """(usable, expiring_soon, expired) with expired items removed from usable."""
    soon = today + timedelta(days=EXPIRY_WARNING_DAYS)
    expired, expiring, usable = [], [], []
    for item in items:
        exp = _parse_date(item["expires"])
        if exp and exp < today:
            expired.append(item)
            continue
        if exp and exp <= soon:
            expiring.append(item)
        usable.append(item)
    return usable, expiring, expired


def category_weights(active_hazards=()):
    """Water and food weigh most; categories a serious local hazard leans on gain weight."""
    from .hazards import CATEGORY_LINKS
    weights = {"water": 3, "food": 3, "medical": 2}
    bumped = set()
    for code, lvl in active_hazards:
        if lvl >= 4:
            for cat in CATEGORY_LINKS.get(code, []):
                if cat not in bumped:
                    weights[cat] = weights.get(cat, 1) + 1
                    bumped.add(cat)
    return weights


def analyze(settings, items, documents, today=None, hazard_profile=None):
    """Home readiness. Items in locations that don't count toward home supply
    (by default vehicles and off-site stashes) are left out."""
    from .hazards import HAZARDS, active
    today = today or date.today()
    all_items = items
    items = [i for i in items if i.get("counts_home", 1)]
    people = _people(settings)
    pets = int(settings["pets"])
    days = max(1, int(settings["target_days"]))
    na = set(settings.get("not_applicable", []))
    hazards_on = active(hazard_profile)
    hazard_codes = {c for c, _ in hazards_on}

    usable, _, _ = split_expiry(items, today)
    _, expiring, expired = split_expiry(all_items, today)  # expiry watch covers every location

    def total(pred, field="quantity"):
        return sum(count(i) * (i["per_unit"] if field == "per_unit" else 1)
                   for i in usable if pred(i))

    lines = []  # one per scored requirement

    water_gal = total(lambda i: i["category"] == "water", "per_unit")
    water_need_day = people * float(settings["water_gal_per_person"]) + pets * float(settings["water_gal_per_pet"])
    water_days = water_gal / water_need_day if water_need_day else 0
    lines.append({"id": "water", "category": "water", "label": "Drinking water",
                  "have": round(water_days, 1), "need": days, "unit": "days",
                  "detail": f"{water_gal:.1f} gal stored, household uses {water_need_day:.1f} gal/day"})

    kcal = total(lambda i: i["category"] == "food" and not i["essential"], "per_unit")
    kcal_day = people * float(settings["kcal_per_person"])
    food_days = kcal / kcal_day if kcal_day else 0
    lines.append({"id": "food", "category": "food", "label": "Non-perishable food",
                  "have": round(food_days, 1), "need": days, "unit": "days",
                  "detail": f"{kcal:,.0f} kcal stored, household needs {kcal_day:,.0f} kcal/day"})

    hazard_items = [e for e in HAZARD_ESSENTIALS if hazard_codes & set(e["hazards"])]
    for ess in ESSENTIALS + hazard_items:
        if ess["id"] in na:
            continue
        need = essential_target(ess, settings)
        if need <= 0:
            continue
        if ess["scale"] == "dollars":
            have = sum(dollars(i) for i in usable if i["essential"] == ess["id"])
        else:
            have = total(lambda i, e=ess["id"]: i["essential"] == e)
        unit = {"days": "days", "pet_days": "days", "dollars": "dollars"}.get(ess["scale"], "")
        line = {"id": ess["id"], "category": ess["category"], "label": ess["label"],
                "have": have, "need": need, "unit": unit, "detail": ""}
        if ess.get("hazards"):
            line["hazard"] = ", ".join(HAZARDS[c] for c in ess["hazards"] if c in hazard_codes)
        lines.append(line)

    have_types = {d["doc_type"] for d in documents}
    required = [t for t in REQUIRED_DOC_TYPES if f"doc_{t}" not in na]
    if pets > 0 and "doc_pets" not in na:
        required.append("pets")
    for t in required:
        lines.append({"id": f"doc_{t}", "category": "documents", "label": DOC_TYPES[t],
                      "have": 1 if t in have_types else 0, "need": 1, "unit": "",
                      "detail": "registered for the USB kit" if t in have_types else "not yet registered"})

    for line in lines:
        line["coverage"] = min(1.0, line["have"] / line["need"]) if line["need"] else 1.0

    categories = []
    for key, label in CATEGORIES.items():
        cat_lines = [l for l in lines if l["category"] == key]
        if not cat_lines:
            continue
        score = sum(l["coverage"] for l in cat_lines) / len(cat_lines)
        categories.append({"id": key, "label": label, "score": round(score * 100),
                           "missing": sum(1 for l in cat_lines if l["coverage"] < 1)})

    weights = category_weights(hazards_on)
    wsum = sum(weights.get(c["id"], 1) for c in categories)
    overall = round(sum(c["score"] * weights.get(c["id"], 1) for c in categories) / wsum) if wsum else 0

    gaps = sorted((l for l in lines if l["coverage"] < 1),
                  key=lambda l: (l["coverage"], -weights.get(l["category"], 1), not l.get("hazard")))

    return {
        "overall": overall,
        "categories": sorted(categories, key=lambda c: c["score"]),
        "gaps": gaps,
        "lines": lines,
        "expired": expired,
        "expiring": expiring,
        "water_days": round(water_days, 1),
        "food_days": round(food_days, 1),
        "target_days": days,
        "hazards": [{"code": c, "label": HAZARDS[c], "level": lvl,
                     "rating": hazard_profile["ratings"][c]} for c, lvl in hazards_on],
        "weights": weights,
    }


# ---- packs and sizes ----
GAL_PER = {"ml": 1 / 3785.411784, "L": 1 / 3.785411784, "fl oz": 1 / 128, "gal": 1.0}
LB_PER_GAL = 8.34


def count(item):
    """How many single things an item stands for: 2 cases of 24 bottles = 48 bottles."""
    size = item.get("pack_size")
    return float(item.get("quantity") or 0) * (float(size) if size not in (None, "") and float(size) > 0 else 1)


SENSITIVE = {"firearms"}  # admin only: not shown to family logins, field pages or plain printouts
SUBTYPES = {"firearm": "Firearm", "ammo": "Ammunition", "magazine": "Magazine", "gear": "Other gear"}
AMMO_TYPES = {"training": "Training / range (FMJ)", "defensive": "Defensive (hollow point)", "hunting": "Hunting",
              "buckshot": "Buckshot", "birdshot": "Birdshot", "slug": "Slug", "match": "Match / precision", "other": "Other"}
MAG_STATUS = {"loaded": "Loaded", "empty": "Empty", "service": "Needs service"}


def dollars(item):
    """Cash: bills x value of each, or the quantity is the dollar amount when no value is given."""
    per = float(item.get("per_unit") or 0)
    return count(item) * per if per else count(item)


def visible(items, role):
    return items if role == "admin" else [i for i in items if i.get("category") not in SENSITIVE]


def _cal(c):
    return " ".join((c or "").split()).lower()


def ammo_summary(items):
    """Rounds on hand per caliber (split by type), rounds sitting in loaded magazines, and which firearms use it."""
    out = {}
    for i in items:
        if i.get("category") != "firearms" or not (i.get("caliber") or "").strip():
            continue
        row = out.setdefault(_cal(i["caliber"]), {"caliber": " ".join(i["caliber"].split()), "rounds": 0, "by_type": {},
                                                  "in_mags": 0, "firearms": []})
        if i.get("subtype") == "ammo":
            row["rounds"] += count(i)
            label = AMMO_TYPES.get(i.get("ammo_type") or "", "Type not set")
            row["by_type"][label] = row["by_type"].get(label, 0) + count(i)
        elif i.get("subtype") == "magazine" and i.get("mag_status") == "loaded":
            row["in_mags"] += count(i) * float(i.get("capacity") or 0)
        elif i.get("subtype") == "firearm":
            row["firearms"].append(i["name"])
            row["caliber"] = " ".join(i["caliber"].split())  # the firearm's spelling wins
    return sorted(out.values(), key=lambda r: r["caliber"].lower())


def magazine_summary(items):
    """Per firearm: magazines that fit it, how many are loaded, and any needing service."""
    guns = [i for i in items if i.get("category") == "firearms" and i.get("subtype") == "firearm"]
    mags = [i for i in items if i.get("category") == "firearms" and i.get("subtype") == "magazine"]
    rows = []
    for g in guns:
        mine = [m for m in mags if m.get("fits") and m["fits"] == g.get("uid")]
        rows.append({"firearm": g["name"], "caliber": g.get("caliber") or "", "magazines": sum(count(m) for m in mine),
                     "loaded": sum(count(m) for m in mine if m.get("mag_status") == "loaded"),
                     "service": sum(count(m) for m in mine if m.get("mag_status") == "service"),
                     "capacity": sorted({float(m.get("capacity") or 0) for m in mine if m.get("capacity")})})
    known = {g.get("uid") for g in guns}
    loose = [m for m in mags if not m.get("fits") or m["fits"] not in known]
    if loose:
        rows.append({"firearm": "Not matched to a firearm", "caliber": "", "magazines": sum(count(m) for m in loose),
                     "loaded": sum(count(m) for m in loose if m.get("mag_status") == "loaded"),
                     "service": sum(count(m) for m in loose if m.get("mag_status") == "service"),
                     "capacity": sorted({float(m.get("capacity") or 0) for m in loose if m.get("capacity")})})
    return rows


def size_text(item):
    """'500 ml', '1 gal' or '400 kcal' for one of the things in the pack ('' when not tracked)."""
    per = float(item.get("per_unit") or 0)
    if item.get("essential") == "cash":
        return f"${per:g}" if per else ""
    if not per or item.get("essential"):
        return ""
    if item.get("category") == "food":
        return f"{per:,.0f} kcal"
    if item.get("category") == "water":
        unit = item.get("size_unit") or "gal"
        return f"{per / GAL_PER.get(unit, 1):.4g} {unit}"
    return ""


def quantity_text(item):
    """'1 case of 24 × 500 ml bottles = 3.17 gal, 26 lb each' for lists and printouts."""
    q = float(item.get("quantity") or 0)
    if item.get("essential") == "cash":
        per = float(item.get("per_unit") or 0)
        if not per:
            return f"${q:,.2f}".replace(".00", "")
        return f"{q:g} {(item.get('unit') or 'bills').strip()} × ${per:g} = ${dollars(item):,.0f}"
    if item.get("subtype") == "magazine" and item.get("category") == "firearms":
        cap = float(item.get("capacity") or 0)
        out = f"{q:g} {(item.get('unit') or ('magazine' if q == 1 else 'magazines')).strip()}"
        out += f", {cap:g} rounds each" if cap else ""
        return out + (f", {MAG_STATUS[item['mag_status']].lower()}" if item.get("mag_status") in MAG_STATUS else "")
    out = f"{q:g} {(item.get('unit') or '').strip()}".strip()
    size, inner, each = float(item.get("pack_size") or 1), (item.get("pack_unit") or "").strip(), size_text(item)
    is_pack = size != 1 or bool(inner)
    if is_pack:
        out += f" of {size:g}" + (f" × {each}" if each else "") + (f" {inner}" if inner else "")
    elif each:
        out += f" × {each}"
    per = float(item.get("per_unit") or 0) if each else 0
    if per and item.get("category") == "water":
        out += f" = {count(item) * per:.3g} gal"
        out += f", {size * per * LB_PER_GAL:.0f} lb" + (" each" if q != 1 else "")  # what one person carries
    elif per:
        out += f" = {count(item) * per:,.0f} kcal"
    elif is_pack:
        out += f" = {count(item):g}" + (f" {inner}" if inner else "")
    return out
