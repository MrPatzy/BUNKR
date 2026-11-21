"""Scenario checklists: what to do and what to grab for specific events.

Each scenario lists steps (before, during, after) and the supplies it relies on.
Supplies are matched against the inventory, showing where each item is and
whether there is enough. Scenarios are ordered by the home site's FEMA hazard
ratings. Guidance follows Ready.gov, EPA/AirNow smoke advice and NWS safety pages.
"""

import json
from datetime import date

from . import gaps
from .hazards import HAZARDS, level

WINTER = ["WNTW", "CWAV", "ISTM"]

SCENARIOS = [
    {"id": "power_outage", "label": "Power outage", "hazards": WINTER + ["HWAV", "SWND", "TRND", "HRCN", "LTNG"], "always": True,
     "needs": ["water", "food", "flashlight", "batteries", "power_bank", "radio", "can_opener", "first_aid_kit", "rx_meds", "cash",
               {"id": "cold_bedding", "hazards": WINTER}, {"id": "safe_heat", "hazards": WINTER}, {"id": "co_alarm", "hazards": WINTER},
               {"id": "cooling", "hazards": ["HWAV"]}],
     "steps": {
         "before": ["Charge phones, power banks and radios", "Know how to open the garage door by hand",
                    "Plan for anyone whose medical equipment needs power"],
         "during": ["Use flashlights, not candles", "Keep the fridge and freezer closed (fridge about 4 hours, full freezer about 48 hours)",
                    "Unplug electronics to protect them from the surge when power returns",
                    "Generators and grills outdoors only, at least 20 feet from windows and doors",
                    "Listen to NOAA weather radio or the family radio channel for updates", "Text the out-of-area contact"],
         "after": ["Throw out food that was above 40 °F for 2 hours or more", "Restock batteries and recharge power banks"]}},
    {"id": "evacuation", "label": "Evacuation", "hazards": ["WFIR", "IFLD", "CFLD", "HRCN", "TSUN", "VLCN", "LNDS"], "always": True,
     "needs": [{"special": "gobags"}, {"special": "vehicles"}, "doc_copies", "cash", "rx_meds", "paper_maps", "pet_food", "power_bank"],
     "steps": {
         "before": ["Go-bags packed and by the door, one per person", "Car fueled (at least half a tank) and parked facing out",
                    "Know two ways out of the neighborhood", "Sign up for county emergency alerts"],
         "during": ["Grab go-bags, medications, pets and the document drive", "Text the out-of-area contact where you're going",
                    "Shut off utilities only if officials say to", "Leave a note saying where you went",
                    "Follow official routes; don't take shortcuts", "Meet at the planned meeting place if separated"],
         "after": ["Return only when officials say it's safe", "Photograph damage for insurance before cleaning up"]}},
    {"id": "winter_storm", "label": "Winter storm or extreme cold", "hazards": WINTER,
     "needs": ["water", "food", "cold_bedding", "bedding", "safe_heat", "co_alarm", "flashlight", "batteries", "radio",
               {"id": "shovel", "hazards": WINTER}, {"id": "traction_aid", "hazards": WINTER}],
     "steps": {
         "before": ["Check the car kits for winter gear", "Top up fuel for backup heat", "Let faucets drip to stop pipes freezing"],
         "during": ["Stay home if you can; avoid driving", "Heat one room and close the others off",
                    "Never use a grill, camp stove or generator indoors (carbon monoxide)",
                    "Watch for frostbite and hypothermia: numbness, shivering, confusion", "Check on neighbors by text or radio"],
         "after": ["Clear snow from vents and exhausts", "Restock the car kits"]}},
    {"id": "wildfire_smoke", "label": "Wildfire smoke", "hazards": ["WFIR"], "always": True,
     "needs": ["masks", "smoke_filter", "sheeting_tape", "water", "rx_meds", "radio", {"special": "gobags"}],
     "steps": {
         "before": ["Spare HVAC and purifier filters on hand (MERV 13 or better)", "Pick a clean room: few windows, room for everyone"],
         "during": ["Check air quality at fire.airnow.gov or on the radio", "Stay indoors with windows and doors closed",
                    "Set the HVAC to recirculate; run the HEPA purifier or box-fan filter in the clean room",
                    "Don't fry food, burn candles or vacuum (they add particles)",
                    "Wear an N95 outside; cloth and surgical masks don't stop smoke",
                    "Keep go-bags ready in case the fire comes closer"],
         "after": ["Replace filters", "Keep watching air quality for a few days"]}},
    {"id": "flood", "label": "Flood", "hazards": ["IFLD", "CFLD", "HRCN"],
     "needs": ["waterproof_docs", "water", "first_aid_kit", "radio", "flashlight", {"special": "gobags"}],
     "steps": {
         "before": ["Know if your home or routes are in a flood zone", "Documents in a waterproof pouch, up high"],
         "during": ["Go to higher ground if told to, or if water is rising", "Turn around, don't drown: 6 inches of moving water can knock you down, 1 foot can float a car",
                    "Stay off bridges over fast water", "Don't touch electrical equipment if you're wet or standing in water"],
         "after": ["Stay out of floodwater (it carries sewage and debris)", "Boil or treat water until told it's safe"]}},
    {"id": "extreme_heat", "label": "Extreme heat", "hazards": ["HWAV", "DRGT"],
     "needs": ["water", "cooling", "first_aid_kit"],
     "steps": {
         "before": ["Find the nearest cooling center", "Check that fans and the cooling plan work without grid power"],
         "during": ["Drink water before you feel thirsty", "Stay in the coolest room; limit activity in the afternoon",
                    "Watch for heat exhaustion (heavy sweat, dizziness) and heat stroke (hot dry skin, confusion: call 911)",
                    "Never leave people or pets in a parked car", "Check on older neighbors"],
         "after": ["Restock water"]}},
    {"id": "tornado", "label": "Tornado or severe storm", "hazards": ["TRND", "SWND", "HAIL"],
     "needs": ["shelter_gear", "radio", "flashlight", "whistle", "first_aid_kit", "water"],
     "steps": {
         "before": ["Know your shelter spot: basement, or a small interior room on the lowest floor",
                    "Helmets and sturdy shoes kept there"],
         "during": ["Go to the shelter at the warning, not when you see it", "Get under something sturdy; cover your head and neck",
                    "In a car with no shelter nearby: buckle up, get below the windows, cover your head"],
         "after": ["Watch for downed power lines and gas leaks", "Use the whistle if trapped", "Text the out-of-area contact"]}},
]
BY_ID = {s["id"]: s for s in SCENARIOS}


def relevance(scn, profile):
    ratings = (profile or {}).get("ratings", {})
    worst = max((level(ratings.get(h)) for h in scn["hazards"]), default=0)
    return max(worst, 3 if scn.get("always") else 0), [HAZARDS[h] for h in scn["hazards"] if level(ratings.get(h)) >= 3]


def _need_applies(need, profile):
    if isinstance(need, str) or "special" in need:
        return True
    if not (profile or {}).get("ratings"):
        return True
    return any(level(profile["ratings"].get(h)) >= 3 for h in need["hazards"])


def evaluate(scn, items, analysis, kits_scores, profile, today=None):
    """Supplies for one scenario, matched against the inventory."""
    today = today or date.today()
    usable, _, _ = gaps.split_expiry(items, today)
    lines_by_id = {l["id"]: l for l in analysis["lines"]}
    labels = {e["id"]: e["label"] for e in gaps.CHECKLIST}
    out = []
    for need in scn["needs"]:
        if not _need_applies(need, profile):
            continue
        if isinstance(need, dict) and need.get("special") == "gobags":
            bags = [k for k in kits_scores if k["kind"] == "gobag"]
            out.append({"id": "gobags", "label": "Go-bags", "items": [{"name": k["name"], "location": f"{k['score']}% ready"} for k in bags],
                        "status": "ok" if bags and all(k["score"] >= 80 for k in bags) else ("short" if bags else "missing"),
                        "detail": f"{len(bags)} go-bag(s)" if bags else "no go-bags set up (Locations & kits)"})
            continue
        if isinstance(need, dict) and need.get("special") == "vehicles":
            cars = [k for k in kits_scores if k["kind"] == "vehicle"]
            out.append({"id": "vehicles", "label": "Vehicle kits", "items": [{"name": k["name"], "location": f"{k['score']}% ready"} for k in cars],
                        "status": "ok" if cars and all(k["score"] >= 80 for k in cars) else ("short" if cars else "missing"),
                        "detail": f"{len(cars)} vehicle(s)" if cars else "no vehicles set up"})
            continue
        nid = need if isinstance(need, str) else need["id"]
        if nid in ("water", "food"):
            matched = [i for i in usable if i["category"] == nid and not i["essential"]]
        else:
            matched = [i for i in usable if i["essential"] == nid]
        line = lines_by_id.get(nid)
        if line:
            status = "ok" if line["coverage"] >= 1 else ("short" if line["have"] else "missing")
            detail = f"{line['have']:g} of {line['need']:g} {line['unit']}".strip()
        else:
            status = "ok" if matched else "missing"
            detail = f"{sum(gaps.count(i) for i in matched):g} on hand" if matched else "none logged"
        out.append({"id": nid, "label": labels.get(nid, {"water": "Drinking water", "food": "Food"}.get(nid, nid)),
                    "status": status, "detail": detail,
                    "items": [{"name": i["name"], "quantity": gaps.count(i), "unit": i["unit"], "text": gaps.quantity_text(i),
                               "location": i.get("location") or "no location",
                               "expires": i["expires"]} for i in matched]})
    return out


def overview(items, analysis, kits_scores, profile, runs):
    active = {r["scenario"]: r for r in runs if not r["ended_at"]}
    out = []
    for scn in SCENARIOS:
        lvl, why = relevance(scn, profile)
        supplies = evaluate(scn, items, analysis, kits_scores, profile)
        run = active.get(scn["id"])
        out.append({"id": scn["id"], "label": scn["label"], "level": lvl, "why": why, "steps": scn["steps"],
                    "supplies": supplies, "ready": sum(1 for s in supplies if s["status"] == "ok"), "total": len(supplies),
                    "run": {**run, "checked": json.loads(run["checked"] or "{}")} if run else None})
    out.sort(key=lambda s: (s["run"] is None, -s["level"], s["label"]))
    return out
