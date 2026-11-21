"""Hazard profile from the FEMA National Risk Index (county level).

Each saved site's county is looked up once in FEMA's public NRI county layer.
The home site's ratings decide which hazard-specific checklist items appear
and which categories weigh more in the readiness score.
"""

import json
import urllib.parse
import urllib.request

NRI_URL = ("https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
           "National_Risk_Index_Counties/FeatureServer/0/query")
USER_AGENT = "BUNKR/0.2 (self-hosted preparedness app)"

HAZARDS = {
    "WFIR": "Wildfire", "WNTW": "Winter weather", "CWAV": "Cold wave", "ISTM": "Ice storm",
    "HWAV": "Heat wave", "DRGT": "Drought", "TRND": "Tornado", "SWND": "Strong wind",
    "HAIL": "Hail", "LTNG": "Lightning", "IFLD": "Inland flooding", "CFLD": "Coastal flooding",
    "HRCN": "Hurricane", "ERQK": "Earthquake", "LNDS": "Landslide", "AVLN": "Avalanche",
    "VLCN": "Volcanic activity", "TSUN": "Tsunami",
}

# NRI ratings, mapped to a level. "Relatively Moderate" (3) and above count as relevant.
LEVELS = {"Very High": 5, "Relatively High": 4, "Relatively Moderate": 3,
          "Relatively Low": 2, "Very Low": 1}
RELEVANT = 3

# Which readiness categories a hazard leans on (extra weight in the overall score).
CATEGORY_LINKS = {
    "WFIR": ["medical", "shelter"], "WNTW": ["shelter", "power"], "CWAV": ["shelter", "power"],
    "ISTM": ["power", "shelter"], "HWAV": ["water", "power"], "DRGT": ["water"],
    "TRND": ["comms", "shelter"], "SWND": ["power"], "IFLD": ["documents", "sanitation"],
    "CFLD": ["documents", "sanitation"], "HRCN": ["shelter", "water", "power"],
    "ERQK": ["shelter", "medical"],
}


def level(rating):
    return LEVELS.get(rating or "", 0)


def fetch_county(fips, timeout=30):
    """Return {code: rating} plus overall risk for a 5-digit county FIPS code."""
    if not (len(fips) == 5 and fips.isdigit()):
        raise ValueError("county FIPS must be 5 digits")
    fields = ",".join(["STCOFIPS", "COUNTY", "STATEABBRV", "RISK_RATNG", "NRI_VER"] +
                      [f"{c}_RISKR" for c in HAZARDS])
    query = urllib.parse.urlencode({"where": f"STCOFIPS='{fips}'", "outFields": fields,
                                    "returnGeometry": "false", "f": "json"})
    req = urllib.request.Request(f"{NRI_URL}?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    if data.get("error"):
        raise IOError(data["error"].get("message", "NRI query failed"))
    feats = data.get("features") or []
    if not feats:
        raise LookupError(f"county {fips} not found in the National Risk Index")
    a = feats[0]["attributes"]
    return {"ratings": {c: a.get(f"{c}_RISKR") or "" for c in HAZARDS},
            "overall": a.get("RISK_RATNG") or "", "county": a.get("COUNTY") or "",
            "state": a.get("STATEABBRV") or "", "version": a.get("NRI_VER") or ""}


def active(profile):
    """Hazard codes rated Relatively Moderate or higher, worst first: [(code, level)]."""
    ratings = (profile or {}).get("ratings", {})
    out = [(c, level(r)) for c, r in ratings.items() if level(r) >= RELEVANT]
    return sorted(out, key=lambda x: -x[1])


def home_profile(sites):
    """Hazard profile of the first home site that has one."""
    for s in sites:
        if s["kind"] == "home" and s.get("hazards", {}).get("ratings"):
            return s["hazards"]
    return {}
