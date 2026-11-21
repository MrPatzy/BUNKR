"""Kit targets for vehicles and 72-hour go-bags, and the shopping list built from gaps.

Vehicle list: Ready.gov "Car Safety" (https://www.ready.gov/car).
Go-bag list: Ready.gov "Build a Kit" basics, sized for 3 days and carried per person.
Each vehicle or go-bag location is scored on its own ("Car: 40% ready").
"""

import math
from datetime import date

from . import gaps
from .hazards import HAZARDS, active

WINTER = ["WNTW", "CWAV", "ISTM"]

# scale: "kit" = fixed count per kit, "person" = per person the kit supplies.
# hazards: only included when the home hazard profile rates one of them as relevant
# (or when there's no profile yet, since Ready.gov lists them for winter driving).
KIT_TEMPLATES = {
    "vehicle": {
        "label": "Vehicle kit", "water_days": 1, "food_days": 1,
        "items": [
            {"id": "jumper_cables", "qty": 1, "scale": "kit"},
            {"id": "flashlight", "qty": 1, "scale": "kit"},
            {"id": "first_aid_kit", "qty": 1, "scale": "kit"},
            {"id": "power_bank", "qty": 1, "scale": "kit", "label": "Phone car charger or power bank"},
            {"id": "reflective_triangle", "qty": 1, "scale": "kit"},
            {"id": "fire_extinguisher", "qty": 1, "scale": "kit", "label": "Vehicle fire extinguisher"},
            {"id": "paper_maps", "qty": 1, "scale": "kit"},
            {"id": "bedding", "qty": 1, "scale": "person", "label": "Blanket or sleeping bag"},
            {"id": "clothing", "qty": 1, "scale": "person", "label": "Warm clothes, gloves, hat, boots"},
            {"id": "traction_aid", "qty": 1, "scale": "kit", "hazards": WINTER},
            {"id": "shovel", "qty": 1, "scale": "kit", "hazards": WINTER},
            {"id": "ice_scraper", "qty": 1, "scale": "kit", "hazards": WINTER},
        ],
    },
    "gobag": {
        "label": "72-hour go-bag", "water_days": 3, "food_days": 3,
        "items": [
            {"id": "flashlight", "qty": 1, "scale": "person"},
            {"id": "first_aid_kit", "qty": 1, "scale": "kit"},
            {"id": "radio", "qty": 1, "scale": "kit", "label": "Small battery or crank radio"},
            {"id": "whistle", "qty": 1, "scale": "person"},
            {"id": "masks", "qty": 2, "scale": "person"},
            {"id": "hygiene", "qty": 1, "scale": "kit"},
            {"id": "wipes", "qty": 1, "scale": "kit"},
            {"id": "power_bank", "qty": 1, "scale": "kit"},
            {"id": "cash", "qty": 1, "scale": "kit"},
            {"id": "doc_copies", "qty": 1, "scale": "kit"},
            {"id": "paper_maps", "qty": 1, "scale": "kit"},
            {"id": "clothing", "qty": 1, "scale": "person"},
            {"id": "bedding", "qty": 1, "scale": "person", "label": "Emergency blanket or bivy"},
            {"id": "rain_poncho", "qty": 1, "scale": "person"},
            {"id": "multitool", "qty": 1, "scale": "kit"},
            {"id": "matches", "qty": 1, "scale": "kit"},
            {"id": "rx_meds", "qty": 7, "scale": "kit", "label": "Prescription medication (days)", "unit": "days"},
            {"id": "cold_bedding", "qty": 1, "scale": "person", "hazards": WINTER,
             "label": "Warm layer or cold-rated bag"},
        ],
    },
}

_LABELS = {e["id"]: e for e in gaps.CHECKLIST}


def template_lines(kind, hazard_profile=None, not_applicable=()):
    tpl = KIT_TEMPLATES.get(kind)
    if not tpl:
        return []
    codes = {c for c, _ in active(hazard_profile)}
    have_profile = bool((hazard_profile or {}).get("ratings"))
    out = []
    for t in tpl["items"]:
        if t["id"] in not_applicable:
            continue
        if t.get("hazards") and have_profile and not codes & set(t["hazards"]):
            continue
        base = _LABELS[t["id"]]
        out.append({**t, "label": t.get("label") or base["label"], "category": base["category"]})
    return out


def score_kit(location, items, settings, hazard_profile=None, today=None):
    """Score one vehicle or go-bag location against its template."""
    today = today or date.today()
    tpl = KIT_TEMPLATES.get(location["kind"])
    if not tpl:
        return None
    people = max(1, int(location.get("people") or 1))
    # items in a sub-location count as if they were in the top-level location
    here = [i for i in items if i.get("top_location_id", i.get("location_id")) == location["id"]]
    usable, expiring, expired = gaps.split_expiry(here, today)
    na = set(settings.get("not_applicable", []))

    def total(pred, per_unit=False):
        return sum(gaps.count(i) * (i["per_unit"] if per_unit else 1) for i in usable if pred(i))

    lines = []
    gal_day = people * float(settings["water_gal_per_person"])
    gal = total(lambda i: i["category"] == "water" and not i["essential"], True)
    lines.append({"id": "water", "category": "water", "label": "Drinking water",
                  "have": round(gal / gal_day, 1) if gal_day else 0, "need": tpl["water_days"], "unit": "days",
                  "detail": f"{gal:.1f} gal for {people} {'person' if people == 1 else 'people'}",
                  "buy_unit": "gal", "per_day": gal_day})
    kcal_day = people * float(settings["kcal_per_person"])
    kcal = total(lambda i: i["category"] == "food" and not i["essential"], True)
    lines.append({"id": "food", "category": "food", "label": "Food (ready to eat)",
                  "have": round(kcal / kcal_day, 1) if kcal_day else 0, "need": tpl["food_days"], "unit": "days",
                  "detail": f"{kcal:,.0f} kcal", "buy_unit": "kcal", "per_day": kcal_day})
    for t in template_lines(location["kind"], hazard_profile, na):
        need = t["qty"] * (people if t["scale"] == "person" else 1)
        have = total(lambda i, e=t["id"]: i["essential"] == e)
        line = {"id": t["id"], "category": t["category"], "label": t["label"], "have": have,
                "need": need, "unit": t.get("unit", ""), "detail": ""}
        if t.get("hazards"):
            line["hazard"] = ", ".join(HAZARDS[c] for c in t["hazards"])
        lines.append(line)
    for line in lines:
        line["coverage"] = min(1.0, line["have"] / line["need"]) if line["need"] else 1.0
    weight = {"water": 2, "food": 2}
    wsum = sum(weight.get(l["id"], 1) for l in lines)
    score = round(100 * sum(l["coverage"] * weight.get(l["id"], 1) for l in lines) / wsum) if wsum else 0
    return {"location_id": location["id"], "name": location["name"], "kind": location["kind"],
            "template": tpl["label"], "people": people, "score": score, "lines": lines,
            "gaps": sorted((l for l in lines if l["coverage"] < 1), key=lambda l: l["coverage"]),
            "expired": expired, "expiring": expiring, "item_count": len(here)}


def score_all(locations, items, settings, hazard_profile=None, today=None):
    return [k for k in (score_kit(l, items, settings, hazard_profile, today) for l in locations if not l.get("parent_id")) if k]


def _buy(line, home_settings=None):
    """How much to buy to close a gap, in shopping units."""
    short = max(0.0, line["need"] - line["have"])
    if line["id"] == "water":
        per_day = line.get("per_day") or (home_settings or {}).get("_water_day", 1)
        return math.ceil(short * per_day * 10) / 10, "gal"
    if line["id"] == "food":
        per_day = line.get("per_day") or (home_settings or {}).get("_kcal_day", 2000)
        return int(math.ceil(short * per_day / 100) * 100), "kcal"
    if line["unit"] in ("days", "dollars"):
        return math.ceil(short), line["unit"]
    return math.ceil(short), ""


def shopping_list(analysis, kit_scores, settings):
    """Gaps from home and every kit as one buy list, most important first."""
    people = max(1, int(settings["adults"]) + int(settings["children"]))
    pets = int(settings["pets"])
    home_ctx = {"_water_day": people * float(settings["water_gal_per_person"]) + pets * float(settings["water_gal_per_pet"]),
                "_kcal_day": people * float(settings["kcal_per_person"])}
    weights = analysis.get("weights", {})
    rows = []
    for g in analysis["gaps"]:
        if g["id"].startswith("doc_"):
            continue  # documents are registered, not bought
        qty, unit = _buy(g, home_ctx)
        if qty <= 0:
            continue
        importance = (1 - g["coverage"]) * weights.get(g["category"], 1) * (1.5 if g.get("hazard") else 1)
        rows.append({"where": "Home", "location_id": None, "essential": "" if g["id"] in ("water", "food") else g["id"],
                     "label": g["label"], "category": g["category"], "qty": qty, "unit": unit,
                     "hazard": g.get("hazard", ""), "importance": round(importance, 3)})
    for k in kit_scores:
        for g in k["gaps"]:
            qty, unit = _buy(g)
            if qty <= 0:
                continue
            importance = (1 - g["coverage"]) * (2 if g["id"] in ("water", "food") else 1) * 0.8
            rows.append({"where": k["name"], "location_id": k["location_id"],
                         "essential": "" if g["id"] in ("water", "food") else g["id"],
                         "label": g["label"], "category": g["category"], "qty": qty, "unit": unit,
                         "hazard": g.get("hazard", ""), "importance": round(importance, 3)})
    rows.sort(key=lambda r: (-r["importance"], r["where"] != "Home", r["label"]))
    return rows
