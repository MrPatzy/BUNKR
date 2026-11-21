"""The master channel plan: one list of channels with fixed memory slots, built into each radio.

Rule: a slot number means the same channel on every radio. A radio that can't hold or use a
channel leaves that slot empty (nothing shifts). Each radio model has a profile: its slot range,
name length, whether it may transmit on GMRS, whether it has its own weather key, and any
fixed-slot rules (the GM-15 Pro locks slots 1-30 to GMRS frequencies, 23-54 to repeater pairs,
and makes 55 and up receive-only).

Tones are stored per direction: "" (none), "88.5" (CTCSS Hz) or "D036N" (DCS code, N/I polarity).
Outputs: CHIRP generic CSV (File > Import in CHIRP) and Yaesu ADMS-12 CSV for the FTM-300.
Radio facts below come from the CHIRP drivers (memory bounds, name length, GM-30/GM-15Pro rules)
and the FTM-300DR manuals (999 memories, single M-GRP group, SCAN skip, weather key).
"""

import csv
import io
import re

from .radio import GMRS_FRS

GMRS_FREQ = {n: f for n, f, *_ in GMRS_FRS}
GMRS_RPTR = [462.550, 462.575, 462.600, 462.625, 462.650, 462.675, 462.700, 462.725]

PROFILES = {
    "gm15pro": {"label": "Baofeng/Radioddity GM-15 Pro (GMRS)", "first": 1, "last": 250, "name_len": 6,
                "gmrs_tx": True, "ham_tx": False, "wx_key": True, "format": "chirp", "chirp": "Baofeng GM-15Pro",
                "rules": "gm30", "bands": [(136, 174), (400, 512)]},
    "uv25x4": {"label": "BTECH UV-25X4", "first": 0, "last": 199, "name_len": 8, "gmrs_tx": False, "ham_tx": True,
               "wx_key": False, "format": "chirp", "chirp": "BTECH UV-25X4 (or UV-25X4_G2)",
               "bands": [(130, 180), (200, 271), (350, 391), (400, 521)]},
    "uv5r": {"label": "Baofeng UV-5R", "first": 0, "last": 127, "name_len": 7, "gmrs_tx": False, "ham_tx": True,
             "wx_key": False, "format": "chirp", "chirp": "Baofeng UV-5R", "bands": [(130, 176), (400, 520)]},
    "qrz1": {"label": "Explorer/Radioddity QRZ-1", "first": 1, "last": 200, "name_len": 10, "gmrs_tx": False,
             "ham_tx": True, "wx_key": False, "format": "chirp", "chirp": "Explorer QRZ-1", "bands": [(136, 174), (400, 480)]},
    "ftm300": {"label": "Yaesu FTM-300DR", "first": 1, "last": 999, "name_len": 16, "gmrs_tx": False, "ham_tx": True,
               "wx_key": True, "format": "adms", "chirp": "", "bands": [(108, 174), (400, 480)]},
    "generic": {"label": "Other (CHIRP)", "first": 1, "last": 999, "name_len": 16, "gmrs_tx": False, "ham_tx": True,
                "wx_key": False, "format": "chirp", "chirp": "", "bands": [(30, 1000)]},
}

TONE_RE = re.compile(r"^(?:(\d{2,3}(?:\.\d)?)|D(\d{2,3})([NI]))$")
# The 104 standard DCS codes (octal, as radios show them)
DCS_CODES = {23, 25, 26, 31, 32, 36, 43, 47, 51, 53, 54, 65, 71, 72, 73, 74, 114, 115, 116, 122, 125, 131, 132, 134,
             143, 145, 152, 155, 156, 162, 165, 172, 174, 205, 212, 223, 225, 226, 243, 244, 245, 246, 251, 252, 255,
             261, 263, 265, 266, 271, 274, 306, 311, 315, 325, 331, 332, 343, 346, 351, 356, 364, 365, 371, 411, 412,
             413, 423, 431, 432, 445, 446, 452, 454, 455, 462, 464, 465, 466, 503, 506, 516, 523, 526, 532, 546, 565,
             606, 612, 624, 627, 631, 632, 654, 662, 664, 703, 712, 723, 731, 732, 734, 743, 754}


def parse_tone(t):
    """('', None) | ('ctcss', 88.5) | ('dcs', (36, 'N'))"""
    t = (t or "").strip().upper()
    if not t:
        return "", None
    m = TONE_RE.match(t)
    if not m:
        raise ValueError(f"tone must look like 88.5 or D036N (got {t!r})")
    if m.group(1):
        hz = float(m.group(1))
        if not 60 <= hz <= 260:
            raise ValueError(f"CTCSS tone out of range: {hz}")
        return "ctcss", hz
    code = int(m.group(2))
    if code not in DCS_CODES:
        raise ValueError(f"DCS {code:03d} is not a standard DCS code")
    return "dcs", (code, m.group(3))


def norm_tone(t):
    kind, v = parse_tone(t)
    return "" if not kind else (f"{v:.1f}" if kind == "ctcss" else f"D{v[0]:03d}{v[1]}")


def tone_label(t):
    kind, v = parse_tone(t)
    return "none" if not kind else (f"{v:.1f} Hz" if kind == "ctcss" else f"DCS {v[0]:03d}{v[1]}")


def tx_freq(ch):
    d = ch.get("duplex") or ""
    if d == "+":
        return round(ch["rx_mhz"] + ch["offset_mhz"], 6)
    if d == "-":
        return round(ch["rx_mhz"] - ch["offset_mhz"], 6)
    if d == "split":
        return ch["offset_mhz"]
    if d == "off":
        return None
    return ch["rx_mhz"]


def is_gmrs(f):
    return f is not None and (462.5 <= f <= 462.75 or 467.5 <= f <= 467.75)


def is_ham(f):
    return f is not None and any(lo <= f <= hi for lo, hi in ((144, 148), (219, 225), (420, 450), (902, 928)))


def short_name(ch, n):
    name = (ch.get("short") or ch["name"] or "").strip()
    return name[:n]


# ---------------- building a radio's memory list ----------------

def _gm30_check(slot, ch, rx_only):
    """GM-30 / GM-15 Pro fixed-slot rules (CHIRP radioddity_gm30 driver)."""
    f = ch["rx_mhz"]
    if 1 <= slot <= 22:
        want = GMRS_FREQ[slot]
        if abs(f - want) > 0.0001:
            return f"slot {slot} is locked to GMRS {slot} ({want:.4f}) on this radio"
        if 8 <= slot <= 14:
            return None
        if ch.get("duplex") not in ("", None):
            return f"slot {slot} is simplex-only on this radio"
    elif 23 <= slot <= 30:
        want = GMRS_RPTR[slot - 23]
        if abs(f - want) > 0.0001 or ch.get("duplex") != "+":
            return f"slot {slot} is locked to the {want:.3f} repeater pair on this radio"
    elif 31 <= slot <= 54:
        if not any(abs(f - r) < 0.0001 for r in GMRS_RPTR) or ch.get("duplex") != "+":
            return f"slots 31-54 take GMRS repeater pairs only on this radio"
    return None


def build(radio, channels, extras=()):
    """Memories for one radio. `channels`: master channels (with 'slot'); `extras`: this radio's own
    channels as dicts with 'slot'. Returns (memories, issues)."""
    prof = PROFILES.get(radio.get("profile") or "generic", PROFILES["generic"])
    mems, issues, used = [], [], {}
    pool = [(c["slot"], c, "core") for c in channels if c.get("slot") is not None] + \
           [(c["slot"], c, "extra") for c in extras]
    for slot, ch, origin in sorted(pool, key=lambda x: (x[0], x[2] != "core")):
        label = f"{slot} {ch['name']}"
        if not prof["first"] <= slot <= prof["last"]:
            if origin == "extra" or slot <= 199:
                issues.append({"level": "info", "slot": slot, "text": f"{label}: beyond this radio's memory ({prof['first']}-{prof['last']}), left out"})
            continue
        if slot in used:
            issues.append({"level": "error", "slot": slot, "text": f"{label}: slot already used by {used[slot]}"})
            continue
        if ch.get("service") == "noaa" and prof["wx_key"]:
            continue  # the radio has its own weather key
        if ch.get("radios") and radio.get("profile") not in ch["radios"]:
            continue  # channel limited to some radio models
        if ch.get("status") == "hold":
            issues.append({"level": "warn", "slot": slot, "text": f"{label}: on hold until its frequencies are settled, left out"})
            continue
        if not any(lo <= ch["rx_mhz"] <= hi for lo, hi in prof["bands"]):
            continue  # outside what this radio can tune
        txf = tx_freq(ch)
        rx_only = bool(ch.get("rx_only")) or txf is None
        reason = ""
        if not rx_only:
            if is_gmrs(txf) and not prof["gmrs_tx"]:
                rx_only, reason = True, "GMRS: listen only on this radio (not GMRS-certified)"
            elif is_ham(txf) and not prof["ham_tx"]:
                rx_only, reason = True, "ham: listen only on this radio"
            elif not is_gmrs(txf) and not is_ham(txf):
                rx_only, reason = True, f"transmit {txf:.4f} is outside ham and GMRS bands"
        if prof.get("rules") == "gm30":
            if slot >= 55:
                rx_only = True
                reason = reason or "receive-only memory on this radio"
            else:
                problem = _gm30_check(slot, ch, rx_only)
                if problem:
                    issues.append({"level": "error", "slot": slot, "text": f"{label}: {problem}"})
                    continue
        name = short_name(ch, prof["name_len"])
        mems.append({"slot": slot, "name": name, "full_name": ch["name"], "rx": ch["rx_mhz"],
                     "duplex": ch.get("duplex") or "", "offset": ch.get("offset_mhz") or 0, "tx": None if rx_only else txf,
                     "tx_tone": norm_tone(ch.get("tx_tone")), "rx_tone": norm_tone(ch.get("rx_tone")),
                     "mode": ch.get("mode") or "FM", "power": ch.get("power") or "", "rx_only": rx_only,
                     "scan": bool(ch.get("scan", 1)) and ch.get("service") != "noaa", "watch": bool(ch.get("watch")),
                     "wiresx": bool(ch.get("wiresx")), "notes": ch.get("notes") or "", "why_rx_only": reason,
                     "status": ch.get("status") or "", "origin": origin})
        used[slot] = ch["name"]
    names = {}
    for m in mems:
        names.setdefault(m["name"], []).append(m["slot"])
    for n, slots in names.items():
        if len(slots) > 1 and n and slots[0] < 200:  # the statewide block repeats call signs on purpose
            issues.append({"level": "warn", "slot": slots[0],
                           "text": f"name '{n}' is shown for slots {', '.join(map(str, slots))} on this radio (names cut to {prof['name_len']} characters)"})
    for m in mems:
        if m["status"] == "confirm":
            issues.append({"level": "warn", "slot": m["slot"], "text": f"{m['slot']} {m['full_name']}: settings still to be confirmed"})
    return mems, issues


# ---------------- CHIRP ----------------

CHIRP_FIELDS = ["Location", "Name", "Frequency", "Duplex", "Offset", "Tone", "rToneFreq", "cToneFreq",
                "DtcsCode", "DtcsPolarity", "RxDtcsCode", "CrossMode", "Mode", "TStep", "Skip", "Power",
                "Comment", "URCALL", "RPT1CALL", "RPT2CALL", "DVCODE"]


def chirp_tone(tx, rx):
    """CHIRP's Tone / rToneFreq / cToneFreq / DtcsCode / RxDtcsCode / DtcsPolarity / CrossMode."""
    tk, tv = parse_tone(tx)
    rk, rv = parse_tone(rx)
    out = {"Tone": "", "rToneFreq": "88.5", "cToneFreq": "88.5", "DtcsCode": "023", "RxDtcsCode": "023",
           "DtcsPolarity": "NN", "CrossMode": "Tone->Tone"}
    if not tk and not rk:
        return out
    if tk == "ctcss" and not rk:
        return out | {"Tone": "Tone", "rToneFreq": f"{tv:.1f}", "cToneFreq": f"{tv:.1f}"}
    if tk == "ctcss" and rk == "ctcss" and tv == rv:
        return out | {"Tone": "TSQL", "rToneFreq": f"{tv:.1f}", "cToneFreq": f"{tv:.1f}"}
    if tk == "dcs" and rk == "dcs" and tv == rv:
        return out | {"Tone": "DTCS", "DtcsCode": f"{tv[0]:03d}", "RxDtcsCode": f"{tv[0]:03d}", "DtcsPolarity": tv[1] * 2}
    cross = out | {"Tone": "Cross"}
    if tk == "ctcss":
        cross["rToneFreq"] = f"{tv:.1f}"
    if rk == "ctcss":
        cross["cToneFreq"] = f"{rv:.1f}"
    pol = [tv[1] if tk == "dcs" else "N", rv[1] if rk == "dcs" else "N"]
    if tk == "dcs":
        cross["DtcsCode"] = f"{tv[0]:03d}"
    if rk == "dcs":
        cross["RxDtcsCode"] = f"{rv[0]:03d}"
    cross["DtcsPolarity"] = "".join(pol)
    a = {"": "", "ctcss": "Tone", "dcs": "DTCS"}
    cross["CrossMode"] = f"{a[tk]}->{a[rk]}"
    return cross


def chirp_csv(mems):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CHIRP_FIELDS, lineterminator="\n")
    w.writeheader()
    for m in mems:
        if m["rx_only"]:
            duplex, offset = "off", 0.0
        else:
            duplex = m["duplex"] if m["duplex"] in ("", "+", "-", "split") else ""
            offset = m["offset"] if duplex in ("+", "-", "split") else 0.0
        row = {"Location": m["slot"], "Name": m["name"], "Frequency": f"{m['rx']:.6f}", "Duplex": duplex,
               "Offset": f"{offset:.6f}", "Mode": m["mode"], "TStep": "5.00" if m["rx"] < 300 else "12.50",
               "Skip": "" if m["scan"] else "S", "Power": "",
               "Comment": (m["full_name"] + (" | " + m["why_rx_only"] if m["why_rx_only"] else ""))[:80],
               "URCALL": "", "RPT1CALL": "", "RPT2CALL": "", "DVCODE": ""}
        row.update(chirp_tone(m["tx_tone"], m["rx_tone"]))
        w.writerow(row)
    return buf.getvalue()


# ---------------- Yaesu ADMS-12 (FTM-300) ----------------

def _mhz(x):
    return f"{x:.5f}".rstrip("0").rstrip(".")


def adms_tone(tx, rx):
    """The FTM-300 stores one tone setting per memory: (mode, ctcss, dcs)."""
    tk, tv = parse_tone(tx)
    rk, rv = parse_tone(rx)
    if tk == "ctcss" and rk == "ctcss" and tv == rv:
        return "TONE SQL", tv, 23
    if tk == "ctcss":
        return "TONE", tv, 23          # encode only: what repeater access needs
    if tk == "dcs" and rk == "dcs" and tv == rv:
        return "DCS", 100.0, tv[0]
    if rk == "dcs":
        return "DCS", 100.0, rv[0]     # listen-only GMRS: open on the repeater's output code
    if tk == "dcs":
        return "DCS", 100.0, tv[0]
    if rk == "ctcss":
        return "TONE SQL", rv, 23
    return "OFF", 100.0, 23


def adms_csv(mems, total=999):
    """ADMS-12 memory CSV: no header, 22 columns, Windows line endings, one row per channel number."""
    by = {m["slot"]: m for m in mems}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    for slot in range(1, total + 1):
        m = by.get(slot)
        if not m:
            w.writerow([slot] + [""] * 20 + ["0"])
            continue
        tx = m["tx"] if m["tx"] is not None else (m["rx"] + m["offset"] if m["duplex"] == "+" else m["rx"])
        d = m["duplex"]
        direction = {"+": "+RPT", "-": "-RPT", "split": "-/+"}.get(d, "OFF")
        offset = m["offset"] if d in ("+", "-") else 0
        mode, hz, dcs = adms_tone(m["tx_tone"], m["rx_tone"])
        step = "5.0KHz" if m["rx"] < 300 else "12.5KHz"  # 12.5 kHz covers GMRS 462.5625 and 70 cm channels
        w.writerow([slot, _mhz(m["rx"]), _mhz(tx if d != "split" else m["offset"]), _mhz(offset), direction,
                    "FM", "AMS" if m["wiresx"] else "FM", m["name"], mode, f"{hz:.1f} Hz", str(dcs), "1500 Hz",
                    "RX 00", "TX 00", "LOW", "OFF", "YES" if m["scan"] else "NO", step,
                    "ON" if m["mode"] == "NFM" else "OFF", "OFF", (m["full_name"] if m["full_name"] != m["name"] else "")[:30], "0"])
    return buf.getvalue()


def radio_file(radio, mems):
    """(filename, text) for a radio's programming file."""
    prof = PROFILES.get(radio.get("profile") or "generic", PROFILES["generic"])
    slug = re.sub(r"[^a-z0-9]+", "-", radio["name"].lower()).strip("-") or "radio"
    if prof["format"] == "adms":
        return f"{slug}-adms12.csv", adms_csv(mems, prof["last"])
    return f"{slug}-chirp.csv", chirp_csv(mems)


# ---------------- printable card ----------------

def _tx_cell(m):
    if m["rx_only"]:
        return "listen only"
    return f"{m['tx']:.4f}" if m["tx"] is not None else ""


def card_html(radio, mems, issues=()):
    import html as _h
    e = _h.escape
    prof = PROFILES.get(radio.get("profile") or "generic", PROFILES["generic"])
    rows = "".join(
        f"<tr><td>{m['slot']}</td><td>{e(m['name'])}</td><td>{e(m['full_name'] if m['full_name'] != m['name'] else '')}</td>"
        f"<td>{m['rx']:.4f}</td><td>{_tx_cell(m)}</td>"
        f"<td>{e(tone_label(m['tx_tone']) if not m['rx_only'] else '')}</td><td>{e(tone_label(m['rx_tone']))}</td>"
        f"<td>{'' if m['scan'] else 'skip'}{' · watch' if m['watch'] else ''}{' · WIRES-X' if m['wiresx'] else ''}</td></tr>"
        for m in mems)
    probs = "".join(f"<li>{e(i['text'])}</li>" for i in issues if i["level"] in ("error", "warn"))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(radio['name'])} channels</title>
<style>body{{font:12px/1.3 system-ui,sans-serif;margin:16px;color:#111}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #999;padding:2px 5px;text-align:left}}th{{background:#eee}}h1{{font-size:17px;margin:0}}
@media print{{body{{margin:6mm}}}}</style></head><body>
<h1>{e(radio['name'])}</h1><p>{e(prof['label'])}{' · ' + e(radio.get('owner') or '') if radio.get('owner') else ''} · slot numbers match every radio in the plan</p>
<table><tr><th>Slot</th><th>Shown as</th><th>Channel</th><th>Receive</th><th>Transmit</th><th>TX tone</th><th>RX tone</th><th></th></tr>{rows}</table>
{f'<h2>Notes</h2><ul>{probs}</ul>' if probs else ''}</body></html>"""
