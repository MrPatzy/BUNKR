"""Power plan: what the house needs in an outage, and how long the batteries last.

Devices have running watts and the hours a day they'd run in an outage (a fridge's compressor runs about a third of
the time, so 8 h of 150 W), a priority (must / should / nice) and the battery they'd plug into. Each battery has a
capacity in watt-hours, a maximum output in watts and optional portable solar. BUNKR works out, per battery, the
daily energy for "must" and for "must + should", how many days that lasts, whether the load is more than the battery
can deliver at once, and how much the panels put back on an average day.

The numbers are estimates: about 85% of a battery's rated capacity is usable through its inverter, and a winter
day in Colorado gives a portable panel about 3-4 full-sun hours. The page says so.

Grid-tied rooftop solar with no battery switches off in an outage (anti-islanding, so line crews are safe). A few
inverters have a daytime emergency outlet; until the household says which inverter they have, the plan assumes the
roof gives nothing during an outage.
"""

USABLE = 0.85            # share of rated Wh that reaches the device through the inverter
SUN_HOURS = 3.5          # average full-sun hours a day for a portable panel (Colorado winter: ~3-4)
PANEL_EFF = 0.75         # real-world share of a panel's rated watts
PRIORITIES = {1: "Must have", 2: "Should have", 3: "Nice to have"}

# typical numbers; every one can be edited after adding
PRESETS = {
    "fridge": {"name": "Refrigerator", "watts": 150, "hours": 8, "surge_w": 900, "priority": 1,
               "notes": "Compressor runs about a third of the time. Keep the door shut: food stays safe ~4 h without power."},
    "freezer": {"name": "Chest freezer", "watts": 80, "hours": 8, "surge_w": 600, "priority": 2,
                "notes": "A full freezer holds about 48 h unpowered; run it a few hours a day."},
    "med_fridge": {"name": "Medicine cooler (mini fridge)", "watts": 45, "hours": 12, "surge_w": 200, "priority": 1,
                   "notes": "For medicines that need the fridge, if the main fridge won't be powered."},
    "cpap": {"name": "CPAP (no humidifier)", "watts": 30, "hours": 8, "priority": 1,
             "notes": "Humidifier and heated hose can triple the draw; turn them off on battery."},
    "router": {"name": "Modem and Wi-Fi router", "watts": 20, "hours": 24, "priority": 1,
               "notes": "Works only if the internet provider's network is up too."},
    "phones": {"name": "Phone charging (4 phones)", "watts": 40, "hours": 2, "priority": 1},
    "lights": {"name": "LED lights (3 bulbs)", "watts": 30, "hours": 5, "priority": 2},
    "hub": {"name": "BUNKR hub (Proxmox mini PC)", "watts": 40, "hours": 24, "priority": 2,
            "notes": "Measure it with a plug-in meter; mini PCs vary from 15 to 60 W."},
    "pi": {"name": "Raspberry Pi 5 field node", "watts": 8, "hours": 24, "priority": 2},
    "laptop": {"name": "Laptop", "watts": 50, "hours": 3, "priority": 3},
    "radio": {"name": "Mobile radio (Yaesu FTM-300, mostly listening)", "watts": 12, "hours": 6, "priority": 2,
              "notes": "About 0.5 A listening; transmitting at 50 W draws ~10 A while keyed."},
    "furnace": {"name": "Gas furnace blower", "watts": 500, "hours": 6, "surge_w": 1200, "priority": 2,
                "notes": "Needs a transfer switch or inlet to run safely from a battery; ask an electrician."},
    "sump": {"name": "Sump pump", "watts": 800, "hours": 1, "surge_w": 2000, "priority": 2},
    "tv": {"name": "TV", "watts": 100, "hours": 3, "priority": 3},
    "heater": {"name": "Space heater", "watts": 1500, "hours": 0, "priority": 3,
               "notes": "Drains a home battery in under an hour. Use blankets and one warm room instead."},
}

# which presets the Medical tab and the people list point to
def suggestions(store):
    out = []
    have = " ".join(r["name"].lower() for r in store._rows("SELECT name FROM power_devices"))
    cold = store._rows("SELECT m.name, p.name AS person FROM meds m JOIN people p ON p.id=m.person_id WHERE m.cold=1 AND m.active=1")
    if cold and not any(w in have for w in ("fridge", "refrigerator", "cooler")):
        out.append({"preset": "med_fridge", "why": "Needs the fridge: " + ", ".join(f"{c['person']}'s {c['name']}" for c in cold)})
    for p in store._rows("SELECT name, devices FROM people WHERE devices LIKE '%cpap%' OR devices LIKE '%CPAP%'"):
        if "cpap" not in have:
            out.append({"preset": "cpap", "why": f"{p['name']} uses a CPAP"})
            break
    return out


def clean_device(data):
    name = str(data.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("name the device")

    def num(k, hi, default=0.0):
        try:
            v = float(data.get(k) if data.get(k) not in (None, "") else default)
        except (TypeError, ValueError):
            raise ValueError(f"{k.replace('_', ' ')} must be a number")
        if not 0 <= v <= hi:
            raise ValueError(f"{k.replace('_', ' ')} must be between 0 and {hi}")
        return v
    try:
        pri = int(data.get("priority") or 2)
    except (TypeError, ValueError):
        pri = 2
    return {"name": name, "watts": num("watts", 20000), "hours": num("hours", 24), "surge_w": num("surge_w", 50000),
            "priority": pri if pri in PRIORITIES else 2, "battery": str(data.get("battery") or "").strip()[:60],
            "notes": str(data.get("notes") or "").strip()[:300]}


def clean_batteries(rows):
    out, seen = [], set()
    for r in (rows or [])[:12]:
        name = str(r.get("name") or "").strip()[:60]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())

        def num(k, hi):
            try:
                v = float(r.get(k) or 0)
            except (TypeError, ValueError):
                v = 0
            return max(0.0, min(v, hi))
        out.append({"name": name, "wh": num("wh", 200000), "max_w": num("max_w", 50000), "solar_w": num("solar_w", 20000),
                    "notes": str(r.get("notes") or "").strip()[:200]})
    return out


def settings(store):
    s = store.get_raw("power", {}) or {}
    return {"batteries": s.get("batteries") or [], "inverter": s.get("inverter") or ""}


def plan(devices, batteries):
    """Per battery: daily Wh for must / must+should / everything, days each lasts, peak load vs max output, solar back."""
    out = []
    names = {b["name"] for b in batteries}
    for b in batteries:
        mine = [d for d in devices if d["battery"] == b["name"]]
        usable = b["wh"] * USABLE
        solar = b["solar_w"] * PANEL_EFF * SUN_HOURS
        levels = []
        for top, label in ((1, "Must have"), (2, "Must + should"), (3, "Everything")):
            ds = [d for d in mine if d["priority"] <= top]
            wh = sum(d["watts"] * d["hours"] for d in ds)
            net = wh - solar
            days = None if not wh else (float("inf") if net <= 0 else usable / net)
            levels.append({"label": label, "wh_day": round(wh), "days": None if days is None else ("forever" if days == float("inf") else round(days, 1)),
                           "hours": None if days in (None, float("inf")) else round(days * 24)})
        on_at_once = sum(d["watts"] for d in mine if d["hours"] > 0)
        surge = max([d["surge_w"] for d in mine] or [0])
        warn = []
        if b["max_w"] and on_at_once > b["max_w"]:
            warn.append(f"Everything on at once is {on_at_once:.0f} W, more than its {b['max_w']:.0f} W output: stagger them.")
        if b["max_w"] and surge and surge > b["max_w"] * 2:
            warn.append(f"A {surge:.0f} W start-up surge may trip it.")
        if not b["wh"]:
            warn.append("Add its capacity in Wh (on the label or in the manual).")
        out.append({"battery": b["name"], "wh": b["wh"], "usable_wh": round(usable), "solar_wh_day": round(solar), "levels": levels,
                    "devices": sorted(mine, key=lambda d: (d["priority"], -d["watts"] * d["hours"])), "warnings": warn})
    loose = [d for d in devices if d["battery"] not in names]
    return {"batteries": out, "unpowered": sorted(loose, key=lambda d: d["priority"])}


def outage_list(devices, batteries):
    """The short list for the power_outage checklist: what to plug into what, most important first."""
    p = plan(devices, batteries)
    steps = []
    for b in p["batteries"]:
        musts = [d["name"] for d in b["devices"] if d["priority"] == 1]
        if musts:
            lvl = b["levels"][0]
            last = f" (lasts about {lvl['days']} days)" if isinstance(lvl["days"], float) and lvl["days"] >= 1 else \
                f" (lasts about {lvl['hours']} hours)" if lvl["hours"] else " (solar keeps up)" if lvl["days"] == "forever" else ""
            steps.append(f"Plug into the {b['battery']}: {', '.join(musts)}{last}.")
    if any(d["priority"] == 1 for d in p["unpowered"]):
        steps.append("No battery planned for: " + ", ".join(d["name"] for d in p["unpowered"] if d["priority"] == 1) + ".")
    return steps
