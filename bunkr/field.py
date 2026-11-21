"""The field page: what anyone on the BUNKR field Wi-Fi gets without a login.

The Wi-Fi password is the gate. Everything here is read-only and served from files
already on this node: the family plan, offline maps, the reference library (Kiwix,
port 8080), app installers, radio cards and the printable inventory. The one thing
anyone here can write is a family check-in ("I'm safe"), so it works with no internet.
"""

import html
from pathlib import Path

APP_NOTES = {
    ".apk": "Android: tap to download, then open it and allow installing from this browser",
    ".exe": "Windows", ".msi": "Windows", ".zip": "Computer", ".deb": "Linux (Pi or laptop)",
    ".appimage": "Linux (Pi or laptop)", ".dmg": "Mac",
}


def _size(n):
    return f"{n / 1e6:.0f} MB" if n >= 1e6 else f"{max(1, round(n / 1e3))} KB"


def collect(store_root):
    """What this node actually has, by looking at its store."""
    root = Path(store_root)
    have = lambda rel: (root / rel).is_file()
    apps = sorted((f for f in (root / "apps").glob("*") if f.is_file()), key=lambda f: (f.suffix.lower() != ".apk", f.name.lower())) \
        if (root / "apps").is_dir() else []
    cards = sorted((root / "radio").glob("*-card.html")) if (root / "radio").is_dir() else []
    plans = sorted((root / "radio").glob("colorado-bandplan-*.pdf")) if (root / "radio").is_dir() else []
    return {
        "plan": have("plan/family-plan.html") or have("plan/family-plan.html.sealed"),
        "maps": have("web/maps/index.html") and any((root / "maps").glob("*.pmtiles")),
        "library": any((root / "library").glob("*.zim")) if (root / "library").is_dir() else False,
        "reference": have("radio/reference.html"),
        "apps": [{"name": f.name, "size": f.stat().st_size, "note": APP_NOTES.get(f.suffix.lower(), "")} for f in apps],
        "cards": [{"name": f.name, "label": f.name.rsplit("-", 2)[0].replace("-", " ").title()} for f in cards],
        "bandplans": [{"name": f.name, "label": f.stem.rsplit("-", 1)[1]} for f in plans],
    }


def _ago(iso):
    from datetime import datetime, timezone
    try:
        secs = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return ""
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{round(secs / 60)} min ago"
    if secs < 129600:
        return f"{round(secs / 3600)} h ago"
    return f"{round(secs / 86400)} days ago"


def family_html(board, sent=""):
    """The family call (if any), a one-tap check-in form and everyone's latest check-in."""
    if not board or not board.get("people"):
        return ""
    e = html.escape
    call = board.get("call")
    banner = ""
    if call:
        where = f" {e(call['place'])}" if call.get("place") else ""
        addr = f"<br><small>{e(call['address'])}</small>" if call.get("address") else ""
        msg = f"<p>{e(call['message'])}</p>" if call.get("message") else ""
        drill = "DRILL (practice): " if call.get("drill") else ""
        banner = (f'<div class="call"><b>{drill}{e(call["label"])}{where}</b>{addr}{msg}'
                  f'<small>Sent by {e(call["created_by"])}, {_ago(call["created_at"])}</small></div>')
    opts = "".join(f'<option value="{e(p["uid"])}">{e(p["name"])}</option>' for p in board["people"])
    buttons = "".join(f'<button name="status" value="{k}" class="st-{k}">{e(v)}</button>' for k, v in board["statuses"].items())
    rows = []
    for p in board["people"]:
        last = p.get("last")
        state = (f'<span class="st-{e(last["status"])}">{e(p["status_label"])}</span> '
                 f'<small>{_ago(last["at"])}{" · " + e(last["note"]) if last.get("note") else ""}'
                 f'{" · low battery" if last.get("battery_low") else ""}</small>') if last else "<small>no check-in yet</small>"
        wait = ' <small class="wait">waiting</small>' if p.get("waiting") else ""
        rows.append(f"<li><b>{e(p['name'])}</b>{' <small>(away from home)</small>' if p.get('remote') else ''}{wait}<br>{state}</li>")
    done = '<p class="ok">Check-in sent. It reaches everyone when this kit next talks to home.</p>' if sent else ""
    return f"""<h2 id="family">Family check-in</h2>{banner}{done}
<form method="post" action="/field/checkin" class="ck"><label>Who are you?<select name="person_uid">{opts}</select></label>
<input name="note" maxlength="300" placeholder="Note (optional): where you are, what you need">
<label class="lowbat"><input type="checkbox" name="battery_low" value="1"> My phone's battery is low</label>
<div class="btns">{buttons}</div></form>
<ul class="board">{"".join(rows)}</ul>"""


def lock_html(note):
    """A locked node: what's still here, and the unlock form."""
    e = html.escape
    err = f'<p class="err">{e(note["error"])}</p>' if note.get("error") else ""
    if note.get("passphrase"):
        form = ('<form method="post" action="/unlock" class="ck"><label>Family node-lock passphrase'
                '<input type="password" name="passphrase" autocomplete="current-password" required></label>'
                '<button class="go">Unlock</button></form>')
    else:
        form = "<p>No passphrase is set up for this kit yet: bring it home and it unlocks from the hub.</p>"
    home = "" if note.get("held") else " At home it unlocks by itself within a minute."
    return f"""<div class="lock"><b>Locked</b><p>The family plan, inventory and check-ins on this kit are encrypted.{home}
Away from home, type the family's node-lock passphrase.</p>{err}{form}</div>"""


def page(store_root, host, board=None, sent="", locked=None):
    """Plain HTML, no scripts: works on any phone browser. locked: a locked node (see lock_html)."""
    e = html.escape
    c = collect(store_root)
    hostname = (host or "bunkr.lan").split(":")[0]
    big = []
    if c["plan"] and not locked:
        big.append(("/store/plan/family-plan.html", "Family plan", "Meeting places, contacts, medical info, radio channels"))
    if c["maps"]:
        big.append(("/store/web/maps/", "Maps", "Offline street maps of your area"))
    if c["library"]:
        big.append((f"http://{e(hostname)}:8080/kiwix/", "Library", "Medical, first aid, repair and reference books"))
    if not locked:
        big.append(("/api/report.html", "Inventory", "What supplies there are and where they are"))
    radio = [("/store/radio/reference.html", "Frequency reference")] if c["reference"] else []
    radio += [(f"/store/radio/{e(x['name'])}", f"{x['label']} channel card") for x in c["cards"]]
    radio += [(f"/store/radio/{e(x['name'])}", f"Colorado band plan, {x['label']}") for x in c["bandplans"]]
    tiles = "".join(f'<a class="tile" href="{href}"><b>{e(t)}</b><span>{e(d)}</span></a>' for href, t, d in big)
    apps = "".join(f'<li><a href="/store/apps/{e(a["name"])}" download>{e(a["name"])}</a> '
                   f'<small>{_size(a["size"])}{" · " + e(a["note"]) if a["note"] else ""}</small></li>' for a in c["apps"])
    radios = "".join(f'<li><a href="{href}">{e(t)}</a></li>' for href, t in radio)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>BUNKR field kit</title>
<link rel="icon" href="/favicon.ico" sizes="16x16 32x32 48x48">
<link rel="icon" type="image/png" sizes="192x192" href="/icons/icon-192.png">
<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">
<meta name="theme-color" content="#0a0c10">
<meta name="apple-mobile-web-app-title" content="BUNKR">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<style>
:root{{--bg:#f6f5f1;--fg:#1b1b1b;--card:#fff;--line:#d9d6cc;--mut:#5d5b55;--acc:#b0521a;--good:#2f7d4f;--bad:#b3392f;--warn:#9a6a0e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0a0c10;--fg:#ecebe8;--card:#15171c;--line:#2c3038;--mut:#a2a3a6;--acc:#ff8f3a;--good:#5fbf85;--bad:#e8675c;--warn:#e0a83a}}}}
*{{box-sizing:border-box}}body{{margin:0;padding:16px;font:17px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:640px;margin:0 auto}}h1{{font-size:24px;margin:4px 0}}h2{{font-size:17px;margin:22px 0 6px}}
p{{margin:4px 0;color:var(--mut)}}a{{color:var(--acc)}}
.tile{{display:block;padding:16px;margin:10px 0;background:var(--card);border:1px solid var(--line);border-radius:10px;text-decoration:none;color:var(--fg)}}
.tile b{{display:block;font-size:20px;color:var(--acc)}}.tile span{{color:var(--mut);font-size:15px}}
ul{{padding-left:20px;margin:4px 0}}li{{margin:8px 0;overflow-wrap:anywhere}}small{{color:var(--mut);display:block}}
footer{{margin-top:28px;font-size:14px;color:var(--mut)}}
.call{{padding:14px;margin:10px 0;border-radius:10px;border:2px solid var(--bad);background:var(--card)}}.call b{{font-size:20px}}
.ck{{display:grid;gap:8px;padding:12px;background:var(--card);border:1px solid var(--line);border-radius:10px}}
.ck select,.ck input{{font:inherit;padding:10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);width:100%}}
.btns{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}.btns button{{font:inherit;font-weight:600;padding:14px 6px;border-radius:8px;border:0;color:#fff}}
button.st-safe{{background:var(--good)}}button.st-moving{{background:var(--warn)}}button.st-help{{background:var(--bad)}}
span.st-safe{{color:var(--good);font-weight:600}}span.st-moving{{color:var(--warn);font-weight:600}}span.st-help{{color:var(--bad);font-weight:600}}
.lock{{padding:14px;margin:10px 0;border-radius:10px;border:2px solid var(--warn);background:var(--card)}}.lock b{{font-size:20px}}
.ck .lowbat{{display:flex;gap:8px;align-items:center}}.ck .lowbat input{{width:auto}}
.lock .ck{{border:0;padding:8px 0 0}}.go{{font:inherit;font-weight:600;padding:12px;border-radius:8px;border:0;color:#fff;background:var(--acc)}}
.err{{color:var(--bad)}}
.board{{list-style:none;padding:0}}.board li{{padding:8px 0;border-bottom:1px solid var(--line)}}.wait{{display:inline;color:var(--bad)}}.ok{{color:var(--good)}}
</style></head><body><main>
<h1>BUNKR field kit</h1><p>Works with no internet. Nothing to log in to; just tap.</p>
{lock_html(locked) if locked else family_html(board, sent)}
{tiles}
{f"<h2>Radios</h2><ul>{radios}</ul>" if radios else ""}
{f"<h2>Apps to install</h2><p>Only if you need them. iPhones can't install apps from here.</p><ul>{apps}</ul>" if apps else ""}
<footer>Keep this address: http://{e(hostname)}:8765{"" if locked else ' &middot; <a href="/login.html">Log in</a> to make changes'}</footer>
</main></body></html>"""
