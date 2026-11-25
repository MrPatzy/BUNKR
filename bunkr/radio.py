"""Radio pack: channels, CHIRP import/export, printable channel cards and reference sheets.

Channels are kept once and assigned to radios in memory-slot order. Each radio
gets a CHIRP-compatible CSV (load it in CHIRP with File > Import, then upload to the
radio with no internet), a printable channel card, and a history of programming
images (.img files saved from CHIRP) in the downloads store.

Reference data below is from the FCC rules (47 CFR 95 Subparts B and E for FRS/GMRS),
the ARRL US band chart for General class privileges, and NOAA Weather Radio.
Check the ARRL chart before relying on a band edge.
"""

import csv
import html
import io
from datetime import datetime

KINDS = {"handheld": "Handheld", "mobile": "Mobile", "base": "Base station", "hf": "HF transceiver"}
SERVICES = {"ham": "Amateur (ham)", "gmrs": "GMRS", "frs": "FRS", "noaa": "NOAA weather (receive only)",
            "other": "Other"}

# GMRS/FRS channel plan (MHz, power limits in watts): FCC 95.1763 and 95.563.
GMRS_FRS = [
    (1, 462.5625, 5, 2), (2, 462.5875, 5, 2), (3, 462.6125, 5, 2), (4, 462.6375, 5, 2), (5, 462.6625, 5, 2),
    (6, 462.6875, 5, 2), (7, 462.7125, 5, 2),
    (8, 467.5625, 0.5, 0.5), (9, 467.5875, 0.5, 0.5), (10, 467.6125, 0.5, 0.5), (11, 467.6375, 0.5, 0.5),
    (12, 467.6625, 0.5, 0.5), (13, 467.6875, 0.5, 0.5), (14, 467.7125, 0.5, 0.5),
    (15, 462.5500, 50, 2), (16, 462.5750, 50, 2), (17, 462.6000, 50, 2), (18, 462.6250, 50, 2), (19, 462.6500, 50, 2),
    (20, 462.6750, 50, 2), (21, 462.7000, 50, 2), (22, 462.7250, 50, 2),
]
# NOAA Weather Radio channels (WX1-WX7), receive only.
NOAA = [(1, 162.550), (2, 162.400), (3, 162.475), (4, 162.425), (5, 162.450), (6, 162.500), (7, 162.525)]

# General class HF privileges (US), from the ARRL band chart. Power 1,500 W PEP unless noted.
GENERAL_BANDS = [
    ("160 m", "1.800-2.000", "CW, phone, image, data", ""),
    ("80 m", "3.525-3.600 / 3.800-4.000", "CW and data / CW, phone, image", ""),
    ("60 m", "5 channels: 5.332, 5.348, 5.3585, 5.373, 5.405 (center)", "USB phone, CW, data", "100 W ERP"),
    ("40 m", "7.025-7.125 / 7.175-7.300", "CW and data / CW, phone, image", ""),
    ("30 m", "10.100-10.150", "CW and data", "200 W PEP"),
    ("20 m", "14.025-14.150 / 14.225-14.350", "CW and data / CW, phone, image", ""),
    ("17 m", "18.068-18.110 / 18.110-18.168", "CW and data / CW, phone, image", ""),
    ("15 m", "21.025-21.200 / 21.275-21.450", "CW and data / CW, phone, image", ""),
    ("12 m", "24.890-24.930 / 24.930-24.990", "CW and data / CW, phone, image", ""),
    ("10 m", "28.000-28.300 / 28.300-29.700", "CW and data / CW, phone, image", ""),
    ("6 m and up", "All amateur frequencies", "All modes", ""),
]
DIGITAL_SPOTS = [
    ("FT8", "3.573, 7.074, 14.074, 21.074, 28.074 MHz USB"),
    ("JS8Call", "3.578, 7.078, 14.078, 21.078, 28.078 MHz USB"),
    ("2 m FM calling", "146.520 MHz simplex"),
    ("70 cm FM calling", "446.000 MHz simplex"),
]

CHIRP_FIELDS = ["Location", "Name", "Frequency", "Duplex", "Offset", "Tone", "rToneFreq", "cToneFreq",
                "DtcsCode", "DtcsPolarity", "RxDtcsCode", "CrossMode", "Mode", "TStep", "Skip", "Power",
                "Comment", "URCALL", "RPT1CALL", "RPT2CALL", "DVCODE"]


def standard_channels():
    """Built-in channel set: GMRS/FRS, GMRS repeaters, NOAA weather, ham calling frequencies."""
    out = []
    for n, f, gmrs_w, frs_w in GMRS_FRS:
        out.append({"name": f"GMRS {n}", "grp": "GMRS/FRS", "service": "gmrs", "rx_mhz": f, "duplex": "",
                    "offset_mhz": 0, "mode": "NFM" if 8 <= n <= 14 else "FM", "power": f"GMRS {gmrs_w} W, FRS {frs_w} W",
                    "notes": "Shared with FRS", "sort": n})
    for n in range(15, 23):
        f = GMRS_FRS[n - 1][1]
        out.append({"name": f"GMRS {n}R", "grp": "GMRS repeaters", "service": "gmrs", "rx_mhz": f, "duplex": "+",
                    "offset_mhz": 5.0, "mode": "FM", "power": "50 W", "tone_mode": "Tone",
                    "notes": "Set the repeater's access tone", "sort": 100 + n})
    for n, f in NOAA:
        out.append({"name": f"NOAA WX{n}", "grp": "NOAA weather", "service": "noaa", "rx_mhz": f, "duplex": "off",
                    "offset_mhz": 0, "mode": "FM", "notes": "Receive only", "sort": 200 + n})
    out.append({"name": "2M CALL", "grp": "Ham simplex", "service": "ham", "rx_mhz": 146.520, "duplex": "", "mode": "FM",
                "notes": "National 2 m simplex calling frequency", "sort": 300})
    out.append({"name": "70CM CALL", "grp": "Ham simplex", "service": "ham", "rx_mhz": 446.000, "duplex": "", "mode": "FM",
                "notes": "National 70 cm simplex calling frequency", "sort": 301})
    return out


def tx_mhz(ch):
    if ch["duplex"] == "+":
        return ch["rx_mhz"] + ch["offset_mhz"]
    if ch["duplex"] == "-":
        return ch["rx_mhz"] - ch["offset_mhz"]
    if ch["duplex"] == "split":
        return ch["offset_mhz"]
    if ch["duplex"] == "off":
        return None
    return ch["rx_mhz"]


def tone_text(ch):
    if ch["tone_mode"] in ("Tone", "TSQL"):
        return f"{ch['tone_hz']:.1f} Hz" + (" (both ways)" if ch["tone_mode"] == "TSQL" else "")
    if ch["tone_mode"] == "DTCS":
        return f"DCS {ch['dtcs']:03d}"
    return ""


def default_channels_for(radio, channels):
    """Sensible starting assignment: GMRS radios get GMRS + NOAA; ham radios get ham + NOAA.
    (Ham radios aren't certified for GMRS; HF radios get nothing here.)"""
    if radio["kind"] == "hf":
        return []
    want = {"gmrs": ("gmrs", "frs", "noaa"), "ham": ("ham", "noaa")}.get(radio["service"], ("noaa",))
    return [c["id"] for c in sorted(channels, key=lambda c: (c["sort"], c["rx_mhz"])) if c["service"] in want]


def chirp_csv(channels):
    """CHIRP 'generic CSV': File > Import in CHIRP, then Radio > Upload."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CHIRP_FIELDS, lineterminator="\n")
    w.writeheader()
    for i, ch in enumerate(channels):
        duplex = {"": "", "+": "+", "-": "-", "split": "split", "off": "off"}[ch["duplex"]]
        offset = ch["offset_mhz"] if duplex in ("+", "-", "split") else 0
        w.writerow({"Location": ch.get("slot", i + 1), "Name": ch["name"][:16], "Frequency": f"{ch['rx_mhz']:.6f}",
                    "Duplex": duplex, "Offset": f"{offset:.6f}", "Tone": ch["tone_mode"],
                    "rToneFreq": f"{ch['tone_hz']:.1f}", "cToneFreq": f"{ch['tone_hz']:.1f}",
                    "DtcsCode": f"{ch['dtcs']:03d}", "DtcsPolarity": "NN", "RxDtcsCode": f"{ch['dtcs']:03d}",
                    "CrossMode": "Tone->Tone", "Mode": ch["mode"] or "FM", "TStep": "5.00", "Skip": "",
                    "Power": "", "Comment": ch["notes"][:60], "URCALL": "", "RPT1CALL": "", "RPT2CALL": "", "DVCODE": ""})
    return buf.getvalue()


def _f(row, *names, default=""):
    for n in names:
        for k, v in row.items():
            if k and k.strip().lower() == n.lower() and v not in (None, ""):
                return v.strip()
    return default


def parse_import(text, group="Repeaters"):
    """Channels from a CHIRP CSV or a RepeaterBook CSV export. Unknown rows are skipped."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    out = []
    for i, r in enumerate(rows):
        freq = _f(r, "Frequency", "Output Freq", "Downlink")
        try:
            rx = float(freq)
        except ValueError:
            continue
        duplex = _f(r, "Duplex")
        offset = _f(r, "Offset")
        if not duplex:  # RepeaterBook: input frequency instead of duplex/offset
            inp = _f(r, "Input Freq", "Uplink")
            try:
                tx = float(inp)
                diff = round(tx - rx, 4)
                duplex, offset = ("", 0) if diff == 0 else ("+" if diff > 0 else "-", abs(diff))
            except ValueError:
                duplex, offset = "", 0
        tone_mode = _f(r, "Tone")
        tone = _f(r, "rToneFreq", "Uplink Tone", "PL", "CTCSS")
        try:
            tone_hz = float(tone)
            tone_mode = tone_mode or "Tone"
        except ValueError:
            tone_hz = 88.5
            tone_mode = tone_mode if tone_mode in ("Tone", "TSQL", "DTCS") else ""
        name = _f(r, "Name", "Call", "Callsign", "Location", default=f"{rx:.4f}")
        place = _f(r, "Location Nearest City", "Nearest City", "City")
        try:
            off = float(offset)
        except (TypeError, ValueError):
            off = 0.0
        out.append({"name": name[:20], "grp": group, "service": "gmrs" if 462 <= rx <= 467.8 else "ham",
                    "rx_mhz": rx, "duplex": duplex if duplex in ("", "+", "-", "split", "off") else "",
                    "offset_mhz": off, "tone_mode": tone_mode if tone_mode in ("", "Tone", "TSQL", "DTCS") else "",
                    "tone_hz": tone_hz, "mode": (_f(r, "Mode", default="FM") or "FM")[:6],
                    "notes": ", ".join(x for x in (place, _f(r, "Comment", "Notes")) if x)[:120], "sort": 400 + i})
    return out


_CSS = """body{font:13px/1.35 system-ui,sans-serif;margin:18px;color:#111;background:#fff}
table{border-collapse:collapse;width:100%;margin:6px 0 18px}td,th{border:1px solid #999;padding:3px 6px;text-align:left}
th{background:#eee}h1{font-size:18px;margin:0 0 4px}h2{font-size:15px;margin:14px 0 4px}.meta{color:#555}
.card{border:2px solid #333;border-radius:8px;padding:8px 10px;width:340px;display:inline-block;vertical-align:top;margin:0 10px 12px 0;page-break-inside:avoid}
.card table{font-size:11.5px}@media print{body{margin:6mm}}"""


def card_html(radio, channels):
    """Printable channel card for one radio (cut out and keep with it)."""
    e = html.escape
    rows = "".join(
        f"<tr><td>{ch.get('slot', i + 1)}</td><td>{e(ch['name'])}</td><td>{ch['rx_mhz']:.4f}</td>"
        f"<td>{'RX only' if tx_mhz(ch) is None else ('' if tx_mhz(ch) == ch['rx_mhz'] else f'{tx_mhz(ch):.4f}')}</td>"
        f"<td>{e(tone_text(ch))}</td></tr>" for i, ch in enumerate(channels))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Channel card: {e(radio['name'])}</title>
<style>{_CSS}</style></head><body>
<div class="card"><h1>{e(radio['name'])}</h1><div class="meta">{e(radio['model'])} · {SERVICES.get(radio['service'], '')} · printed {datetime.now():%Y-%m-%d}</div>
<table><tr><th>#</th><th>Name</th><th>RX MHz</th><th>TX MHz</th><th>Tone</th></tr>{rows or '<tr><td colspan=5>No channels assigned</td></tr>'}</table></div>
</body></html>"""


def reference_html():
    """FRS/GMRS chart, NOAA weather channels and the General class band plan on one printable page."""
    gm = "".join(f"<tr><td>{n}</td><td>{f:.4f}</td><td>{g:g} W</td><td>{fr:g} W</td>"
                 f"<td>{f'{f + 5:.4f} input' if n >= 15 else ''}</td></tr>" for n, f, g, fr in GMRS_FRS)
    wx = "".join(f"<tr><td>WX{n}</td><td>{f:.3f}</td></tr>" for n, f in NOAA)
    bands = "".join(f"<tr><td>{b}</td><td>{r}</td><td>{m}</td><td>{p}</td></tr>" for b, r, m, p in GENERAL_BANDS)
    digi = "".join(f"<tr><td>{n}</td><td>{f}</td></tr>" for n, f in DIGITAL_SPOTS)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Radio reference</title><style>{_CSS}</style></head><body>
<h1>Radio reference</h1><p class="meta">Printed {datetime.now():%Y-%m-%d} by BUNKR. Sources: FCC 47 CFR 95 (FRS/GMRS),
ARRL US band chart (General class), NOAA Weather Radio. Check the current ARRL chart before relying on a band edge.</p>
<h2>FRS / GMRS channels</h2><p class="meta">GMRS needs an FCC license (no test) and covers the whole family. Channels 15-22
can use repeaters: the radio listens on the channel and transmits 5 MHz higher. Channels 8-14 are narrow FM.</p>
<table><tr><th>Ch</th><th>MHz</th><th>GMRS max</th><th>FRS max</th><th>Repeater</th></tr>{gm}</table>
<h2>NOAA Weather Radio (receive only)</h2><table><tr><th>Channel</th><th>MHz</th></tr>{wx}</table>
<h2>General class HF privileges (USA)</h2><table><tr><th>Band</th><th>MHz</th><th>Modes</th><th>Limit</th></tr>{bands}</table>
<h2>Common digital and calling frequencies</h2><table><tr><th>Mode</th><th>Where</th></tr>{digi}</table>
</body></html>"""


FLASHING = {
    "esptool": [
        "Plug the board into a computer that has esptool (installed on the Pi and laptop by BUNKR).",
        "Fresh install or switching firmware: erase first: esptool --chip esp32s3 erase_flash",
        "MeshCore: esptool --chip esp32s3 write_flash 0x0 <file>-merged.bin",
        "MeshCore update (keeps settings): esptool --chip esp32s3 write_flash 0x10000 <file>.bin",
        "Meshtastic: run ./device-install.sh -f firmware-heltec-v3-<version>.factory.bin from the firmware folder "
        "(it calls esptool), or esptool --chip esp32s3 write_flash 0x0 <...>.factory.bin",
        "If the port isn't found, hold BOOT (PRG) while plugging in. Newer esptool spells it write-flash; both work.",
    ],
    "uf2": [
        "Double-tap the reset button: a USB drive appears on the computer (or phone with a USB-C adapter).",
        "Copy the .uf2 file onto that drive. The board reboots by itself when the copy finishes.",
        "Switching between MeshCore and Meshtastic: if the board misbehaves afterwards, flash the "
        "matching erase .uf2 (shipped with Meshtastic as *erase*.uf2) and then the firmware again.",
        "The .zip files are for over-the-air (Bluetooth DFU) updates from the phone apps.",
    ],
}
