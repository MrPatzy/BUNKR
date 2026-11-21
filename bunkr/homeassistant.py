"""Home Assistant: BUNKR tells HA what's happening, and family members can run what an admin allows from the mesh.

- Out to HA: BUNKR fires HA events on the HA event bus (POST /api/events/<type>), so automations in HA can react:
    bunkr_checkin   {person, status, note, place, via}       someone checked in (via app, Pi Wi-Fi, mesh...)
    bunkr_call      {kind, label, place, message, drill}     a family call went out
    bunkr_all_clear {label, drill}
    bunkr_alert     {title, tier, site, km, category}        an "act now" or "soon" alert at one of our places
    bunkr_flight    {title, body}                            a followed flight is late, landed, cancelled
  For example: a family call turns every light on and unlocks the side door; "Need help" flashes the porch light.
- From the mesh ("HOME lights"): only actions an admin listed, each one an HA entity and a service (scene or script
  turn_on, automation trigger, switch toggle...), optionally adults only. "HOME STATUS" reads listed sensors.
- HA's address and a long-lived access token (HA: your profile, Security, Long-lived access tokens). The token is kept
  in a file next to the database (homeassistant.token, mode 600), like BUNKR's other keys, never in the database.
"""

import json
import os
import re
import threading
import urllib.request
from pathlib import Path

TOKEN_FILE = "homeassistant.token"
DEFAULTS = {"url": "", "events": True, "actions": [], "sensors": []}
SERVICES = ("turn_on", "turn_off", "toggle", "trigger", "press", "open_cover", "close_cover", "lock", "unlock")
ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
WORD = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,23}$")
status = {"last": "", "error": ""}


def settings(store):
    raw = store.get_raw("ha_settings", "") or ""
    try:
        s = json.loads(raw) if raw else {}
    except ValueError:
        s = {}
    return {**DEFAULTS, **{k: v for k, v in s.items() if k in DEFAULTS}}


def parse_lines(text, kind):
    """Admin's list, one per line:
       actions:  lights = scene.outside_lights_on            sensors:  battery = sensor.inverter_soc
                 generator = script.start_generator adults            (word = entity, then an optional label)
                 garage = cover.garage_door close_cover
    Returns (rows, errors)."""
    rows, errors = [], []
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        word, eq, rest = line.partition("=")
        word, bits = word.strip().lower(), rest.split()
        if not eq or not WORD.match(word) or not bits or not ENTITY.match(bits[0]):
            errors.append(f"line {n}: write it as word = domain.entity")
            continue
        if kind == "sensors":
            rows.append({"word": word, "entity_id": bits[0], "label": " ".join(bits[1:])[:40]})
            continue
        domain = bits[0].split(".")[0]
        service = next((b for b in bits[1:] if b in SERVICES), "trigger" if domain == "automation" else "press" if domain in ("button", "input_button") else "turn_on")
        rows.append({"word": word, "entity_id": bits[0], "service": service, "adults": "adults" in bits[1:]})
    words = [r["word"] for r in rows]
    if len(words) != len(set(words)):
        errors.append("each word can only be used once")
    if kind == "actions" and "status" in words:
        errors.append('"status" is taken: HOME STATUS reads the sensors')
    return rows[:40], errors


def lines(rows, kind):
    out = []
    for r in rows:
        if kind == "sensors":
            out.append(f"{r['word']} = {r['entity_id']}" + (f" {r['label']}" if r.get("label") else ""))
        else:
            out.append(f"{r['word']} = {r['entity_id']} {r['service']}" + (" adults" if r.get("adults") else ""))
    return "\n".join(out)


def save_settings(store, data, folder):
    s = settings(store)
    if "url" in data:
        url = str(data["url"] or "").strip().rstrip("/")
        if url and not re.fullmatch(r"https?://[\w.\-\[\]:]+(:\d{1,5})?", url):
            raise ValueError("the address is like http://homeassistant.local:8123")
        s["url"] = url
    if "events" in data:
        s["events"] = bool(data["events"])
    for kind in ("actions", "sensors"):
        if kind in data:
            rows, errors = parse_lines(data[kind], kind)
            if errors:
                raise ValueError("; ".join(errors[:3]))
            s[kind] = rows
    if "token" in data:
        tok = str(data["token"] or "").strip()
        path = Path(folder) / TOKEN_FILE
        if not tok:
            path.unlink(missing_ok=True)
        elif not re.fullmatch(r"[A-Za-z0-9_\-.]{20,600}", tok):
            raise ValueError("that doesn't look like a Home Assistant token")
        else:
            path.touch(mode=0o600)
            os.chmod(path, 0o600)
            path.write_text(tok + "\n")
    store.set_raw("ha_settings", json.dumps(s))
    return s


def token(folder):
    try:
        return (Path(folder) / TOKEN_FILE).read_text().strip() or None
    except OSError:
        return None


class Client:
    def __init__(self, url, tok, timeout=8):
        self.url, self.tok, self.timeout = url.rstrip("/"), tok, timeout

    def _call(self, method, path, body=None):
        req = urllib.request.Request(self.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.tok}", "Content-Type": "application/json",
                                              "User-Agent": "BUNKR"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read(500_000)
        return json.loads(raw) if raw else None

    def ping(self):
        return (self._call("GET", "/api/") or {}).get("message", "")

    def fire(self, event, data):
        return self._call("POST", f"/api/events/{event}", data)

    def service(self, entity_id, service):
        return self._call("POST", f"/api/services/{entity_id.split('.')[0]}/{service}", {"entity_id": entity_id})

    def state(self, entity_id):
        return self._call("GET", f"/api/states/{entity_id}")


def client(store, folder):
    s, tok = settings(store), token(folder)
    return Client(s["url"], tok) if s["url"] and tok else None


# ---------- BUNKR -> HA events ----------
def event_for(event, data):
    """(HA event type, data) for a BUNKR event, or None."""
    if event == "checkin":
        return "bunkr_checkin", {"person": data.get("person_name"), "status": data.get("status"), "note": data.get("note") or "",
                                 "place": data.get("place") or "", "via": data.get("via") or "app"}
    if event == "call":
        return "bunkr_call", {"kind": data.get("kind"), "label": data.get("label"), "place": data.get("place") or "",
                              "message": data.get("message") or "", "drill": bool(data.get("drill"))}
    if event == "all_clear":
        return "bunkr_all_clear", {"label": data.get("label"), "drill": bool(data.get("drill"))}
    if event == "situation" and data.get("tier") in ("flash", "priority"):
        return "bunkr_alert", {"title": data.get("title"), "tier": data.get("tier"), "site": data.get("site") or "",
                               "km": data.get("km"), "category": data.get("category") or ""}
    if event == "flight":
        return "bunkr_flight", {"title": data.get("title"), "body": data.get("body") or ""}
    return None


class Events:
    """A listener for BUNKR's events that passes them on to Home Assistant in the background."""

    def __init__(self, get_store, folder_fn, make_client=None):
        self.get_store, self.folder_fn = get_store, folder_fn
        self.make_client = make_client or (lambda: client(self.get_store(), self.folder_fn()))

    def __call__(self, event, data):
        try:
            if not settings(self.get_store())["events"]:
                return
        except Exception:
            return
        ev = event_for(event, data)
        if ev:
            threading.Thread(target=self.send, args=ev, daemon=True).start()

    def send(self, kind, data):
        try:
            c = self.make_client()
            if c:
                c.fire(kind, data)
                status.update(last=kind, error="")
        except Exception as exc:  # HA down: BUNKR carries on
            status.update(error=f"{kind}: {type(exc).__name__}: {exc}"[:200])


# ---------- the mesh's HOME command ----------
def home_command(store, folder, rest, adult, make_client=None):
    s = settings(store)
    c = (make_client or (lambda: client(store, folder)))()
    if not c:
        return "Home Assistant isn't set up on this BUNKR."
    word = rest.strip().lower()
    if not word:
        names = [a["word"] for a in s["actions"]]
        return ("!home " + ", ".join(names) + (", status" if s["sensors"] else "")) if names or s["sensors"] else "No Home Assistant actions are set up."
    try:
        if word == "status":
            if not s["sensors"]:
                return "No Home Assistant sensors are set up."
            parts = []
            for r in s["sensors"][:6]:
                st = c.state(r["entity_id"]) or {}
                unit = (st.get("attributes") or {}).get("unit_of_measurement") or ""
                parts.append(f"{r['label'] or r['word']} {st.get('state', '?')}{unit}")
            return "; ".join(parts)
        act = next((a for a in s["actions"] if a["word"] == word), None)
        if not act:
            return f"No !home {word[:20]}. Send !home for the list."
        if act["adults"] and not adult:
            return f"!home {word} is for adults."
        c.service(act["entity_id"], act["service"])
        return f"Done: {word} ({act['service'].replace('_', ' ')})."
    except Exception as exc:
        return f"Home Assistant didn't answer ({type(exc).__name__})."
