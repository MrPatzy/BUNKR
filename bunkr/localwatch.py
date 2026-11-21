"""Keeping pace with what's happening where the family is: the towns on the Sites tab, watched closely.

Three layers, from most to least trustworthy:
1. Local news. Denver TV and papers are regular outlets (brief.OUTLETS). On top of that, every 10 minutes BUNKR asks
   Google News for each family town ("Nederland, CO"), so a boil-water notice that only local TV reports still arrives.
   A story there that says hack, outage, boil, evacuate and the like is raised as "Near Jordan (Nederland)".
2. Unconfirmed: what people are posting. Bluesky's public search (no account needed) and, once an admin adds a
   free Reddit app key, chosen subreddits. Only posts that name a family town (or are in that town's subreddit)
   AND mention an alarm word are kept. These are shown apart from news, labelled "Unconfirmed".
3. A heads-up from posts alone needs two different people posting about the same town and the same thing within a
   few hours. When a news outlet then reports it, the posts are marked "now reported by ...".

Nothing here posts anything anywhere; it only reads public posts. The Reddit secret is a file next to the database
(`reddit.key`, mode 600), never in the database or the API.
"""

import base64
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import brief, news

USER_AGENT = "linux:bunkr-family-watch:0.3 (self-hosted family preparedness app)"
SECRET_FILE = "reddit.key"
DEFAULTS = {"extra_places": [], "subreddits": ["Denver", "Colorado"], "bluesky": True, "reddit_id": ""}
CHATTER_HOURS = 3        # posts this recent count towards a heads-up
KEEP_DAYS = 3
MAX_PLACES = 12
listeners = []
_token = {"value": "", "until": 0.0}
_lock = threading.Lock()
STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
          "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
          "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
          "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
          "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
          "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
          "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
          "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
          "DC": "District of Columbia"}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------- settings ----------
def settings(store):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (store.get_raw("localwatch", {}) or {}).items() if k in DEFAULTS})
    return s


def has_secret(folder):
    p = Path(folder) / SECRET_FILE
    return p.exists() and p.stat().st_size > 0


def _lines(v, n, size, sep=r"[\n,]"):
    if isinstance(v, str):
        v = re.split(sep, v)
    return list(dict.fromkeys(" ".join(str(x).split())[:size] for x in (v or []) if str(x).strip()))[:n]


def save_settings(store, folder, data):
    s = settings(store)
    if "extra_places" in data:
        s["extra_places"] = _lines(data["extra_places"], MAX_PLACES, 60, sep=r"\n")  # "Conifer, CO" is one place
    if "subreddits" in data:
        subs = [x.strip().removeprefix("r/").removeprefix("/r/") for x in _lines(data["subreddits"], 15, 30)]
        bad = [x for x in subs if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", x)]
        if bad:
            raise ValueError(f"not a subreddit name: {bad[0]}")
        s["subreddits"] = subs
    if "bluesky" in data:
        s["bluesky"] = bool(data["bluesky"])
    if "reddit_id" in data:
        rid = str(data["reddit_id"] or "").strip()
        if rid and not re.fullmatch(r"[A-Za-z0-9_\-]{10,40}", rid):
            raise ValueError("that doesn't look like a Reddit app id (the short code under the app's name)")
        s["reddit_id"] = rid
    store.set_raw("localwatch", s)
    path = Path(folder) / SECRET_FILE
    if data.get("forget_secret"):
        path.unlink(missing_ok=True)
    elif data.get("reddit_secret"):
        sec = str(data["reddit_secret"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{20,60}", sec):
            raise ValueError("that doesn't look like a Reddit app secret")
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(sec + "\n")
    _token.update(value="", until=0.0)
    return view(store, folder)


def view(store, folder):
    s = settings(store)
    return s | {"places": places(store), "reddit_ready": bool(s["reddit_id"] and has_secret(folder)), "has_secret": has_secret(folder),
                "status": store.get_raw("localwatch_status", {}) or {}}


# ---------- the family's places ----------
_STATE_PART = re.compile(r"^\s*([A-Z]{2}|[A-Za-z ]{4,20}?)\s*(\d{5}(-\d{4})?)?\s*$")


def city_of(address):
    """ "123 Main St, Nederland, CO 80466" -> ("Nederland", "CO"). ("", "") when it can't tell."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    for i in range(len(parts) - 1, 0, -1):
        m = _STATE_PART.match(parts[i])
        st = m.group(1).strip() if m else ""
        code = st.upper() if st.upper() in STATES else next((k for k, v in STATES.items() if v.lower() == st.lower()), "")
        if code and not re.search(r"\d", parts[i - 1]):
            return parts[i - 1], code
    return "", ""


def places(store):
    """Each town to watch, with who is there: [{"name", "state", "who": ["Jordan"], "label": "Jordan (Nederland)"}]."""
    out = {}
    home_people = [p["name"] for p in store._rows("SELECT name FROM people WHERE remote=0 ORDER BY name")]
    for s in store._rows("SELECT id, name, kind, address, state, county_name FROM sites ORDER BY id"):
        city, st = city_of(s["address"])
        if not city:
            continue
        who = [p["name"] for p in store._rows("SELECT name FROM people WHERE site_id=? ORDER BY name", (s["id"],))]
        if s["kind"] == "home":
            who = ["home"] + [n for n in who if n not in home_people]
        key = (city.lower(), st)
        e = out.setdefault(key, {"name": city, "state": st, "who": [], "sites": [], "county": (s["county_name"] or "").replace(" County", "")})
        e["who"] += [w for w in who if w not in e["who"]]
        e["sites"].append(s["name"])
    for extra in settings(store)["extra_places"]:
        city, st = city_of("x, " + extra) if "," in extra else (extra, "")
        out.setdefault(((city or extra).lower(), st), {"name": city or extra, "state": st, "who": [], "sites": [], "county": ""})
    res = []
    for e in list(out.values())[:MAX_PLACES]:
        who = ", ".join(e["who"]) if e["who"] else ""
        e["label"] = f"{who} ({e['name']})" if who else e["name"]
        res.append(e)
    return res


def place_re(p):
    return re.compile(r"(?<![\w])" + re.escape(p["name"]) + r"(?![\w])", re.I)


def local_re(p):
    """A post that's really about the town, not a product or a word that happens to share its name ("Nederland
    Biscuits recalled"): "in/near Nederland", "Nederland, CO", the state's name, or the county."""
    n = re.escape(p["name"])
    alts = [r"\b(?:in|near|around|at|across|throughout|outside|up)\s+" + n + r"\b", n + r",?\s+(?:" + re.escape(p.get("state") or "~") + r")\b"]
    if STATES.get(p.get("state")):
        alts.append(r"\b" + re.escape(STATES[p["state"]]) + r"\b")
    if p.get("county"):
        alts.append(r"\b" + re.escape(p["county"]) + r"\b")
    if p.get("state") == "CO":
        alts.append(r"\b(?:denver|front range|i-70)\b")
    return re.compile("|".join(alts), re.I)


# ---------- layer 1: a Google News search for each town ----------
def fetch_places(plist, opener=None):
    """Headlines about each family town from the last day, as outlet "local" (the publisher goes in the summary)."""
    heads, status = [], {}
    for p in plist:
        q = f'"{p["name"]}" {STATES.get(p["state"], p["state"])}'.strip() + " when:1d"
        url = brief.GN + urllib.parse.quote_plus(q)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": brief.USER_AGENT, "Accept": "application/rss+xml, */*"})
            with (opener or urllib.request.build_opener()).open(req, timeout=20) as r:
                raw = r.read(brief.MAX_FEED + 1)
            got = 0
            for title, link, when, _summary in brief.parse_feed(raw):
                if when is not None and when < _now() - timedelta(hours=30):
                    continue
                m = re.match(r"^(.*\S)\s+-\s+([^-]{2,60})$", title)
                t, pub = (m.group(1), m.group(2)) if m else (title, "")
                if not place_re(p).search(t):
                    continue  # Google matched it somewhere in the page; only keep headlines that name the town
                heads.append({"outlet": "local", "title": t, "url": link, "summary": f"Reported by {pub}." if pub else "",
                              "at": when.isoformat(timespec="seconds") if when else ""})
                got += 1
            status[p["name"]] = {"ok": True, "count": got}
        except Exception as exc:
            status[p["name"]] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160]}
    return heads, status


# ---------- layer 2: what people are posting (unconfirmed) ----------
CONCEPTS = [("a water problem (boil or don't drink)", r"boil|drink|water main|contaminat"), ("power outage", r"blackout|power cut|outage|offline|shut ?down"),
            ("hack", r"hack|breach|cyber|ransomware"), ("evacuation", r"evacuat|shelter in place"), ("fire", r"wildfire|explosion"),
            ("gas leak", r"gas leak|chemical"), ("shooting", r"shooter|lockdown"), ("recall", r"recall"), ("derailment", r"derail")]


def _alarm(text):
    """The kind of trouble a text is about, so "unless boiled" and "boil water notice" count as the same thing."""
    m = news.ALARM_RE.search(text or "")
    if not m:
        return ""
    w = m.group(0).lower()
    return next((name for name, rx in CONCEPTS if re.search(rx, w)), w)


def _get_json(url, headers, opener=None, data=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers}, data=data)
    with (opener or urllib.request.build_opener()).open(req, timeout=20) as r:
        return json.loads(r.read(3_000_000))


def bluesky(plist, opener=None, when=None):
    """Recent public Bluesky posts naming a family town with an alarm word."""
    when = when or _now()
    out = []
    for p in plist:
        q = urllib.parse.urlencode({"q": p["name"], "sort": "latest", "limit": 50, "since": _iso(when - timedelta(hours=CHATTER_HOURS))})
        d = _get_json("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?" + q, {}, opener)
        for post in d.get("posts") or []:
            rec, author = post.get("record") or {}, post.get("author") or {}
            text = str(rec.get("text") or "")
            alarm = _alarm(text)
            if not alarm or not place_re(p).search(text) or not local_re(p).search(text):
                continue
            rkey = str(post.get("uri") or "").rsplit("/", 1)[-1]
            out.append({"source": "bluesky", "channel": "", "place": p["name"], "author": str(author.get("handle") or "")[:80],
                        "text": text[:500], "url": f"https://bsky.app/profile/{author.get('handle')}/post/{rkey}",
                        "at": str(rec.get("createdAt") or "")[:25], "alarm": alarm})
    return out


def _reddit_token(store, folder, opener=None):
    with _lock:
        if _token["value"] and time.time() < _token["until"]:
            return _token["value"]
        s = settings(store)
        secret = (Path(folder) / SECRET_FILE).read_text().strip() if has_secret(folder) else ""
        if not (s["reddit_id"] and secret):
            return ""
        auth = base64.b64encode(f"{s['reddit_id']}:{secret}".encode()).decode()
        d = _get_json("https://www.reddit.com/api/v1/access_token", {"Authorization": "Basic " + auth,
                      "Content-Type": "application/x-www-form-urlencoded"}, opener, data=b"grant_type=client_credentials")
        if not d.get("access_token"):
            raise RuntimeError(d.get("error") or "Reddit didn't give a token")
        _token.update(value=d["access_token"], until=time.time() + int(d.get("expires_in") or 3600) - 120)
        return _token["value"]


def reddit(store, folder, plist, opener=None, when=None):
    """New posts in the chosen subreddits naming a family town (or in that town's own subreddit) with an alarm word."""
    tok = _reddit_token(store, folder, opener)
    subs = settings(store)["subreddits"]
    if not tok or not subs:
        return []
    when = when or _now()
    d = _get_json(f"https://oauth.reddit.com/r/{'+'.join(subs)}/new?limit=100&raw_json=1", {"Authorization": "Bearer " + tok}, opener)
    out = []
    for c in (d.get("data") or {}).get("children") or []:
        x = c.get("data") or {}
        at = datetime.fromtimestamp(float(x.get("created_utc") or 0), timezone.utc)
        if at < when - timedelta(hours=CHATTER_HOURS * 4):
            continue
        text = f"{x.get('title') or ''}\n{x.get('selftext') or ''}"
        alarm = _alarm(text)
        if not alarm:
            continue
        sub = str(x.get("subreddit") or "")
        # the town's own subreddit, or a regional one where the post is clearly about the town
        p = next((p for p in plist if p["name"].replace(" ", "").lower() in sub.lower()
                  or (place_re(p).search(text) and local_re(p).search(f"{text} r/{sub}"))), None)
        if not p:
            continue
        out.append({"source": "reddit", "channel": "r/" + sub, "place": p["name"], "author": str(x.get("author") or "")[:80],
                    "text": text.strip()[:500], "url": "https://www.reddit.com" + str(x.get("permalink") or ""), "at": _iso(at), "alarm": alarm})
    return out


def save_chatter(store, posts):
    stamp, n = _iso(_now()), 0
    for c in posts:
        if not c.get("url"):
            continue
        if store._rows("SELECT 1 FROM chatter WHERE url=?", (c["url"],)):
            continue
        store._exec("INSERT INTO chatter(source, channel, place, author, text, url, at, fetched_at, alarm) VALUES(?,?,?,?,?,?,?,?,?)",
                    (c["source"], c["channel"], c["place"], c["author"], c["text"], c["url"], c["at"] or stamp, stamp, c["alarm"]))
        n += 1
    store._exec("DELETE FROM chatter WHERE fetched_at < ?", (_iso(_now() - timedelta(days=KEEP_DAYS)),))
    return n


def confirm(store, when=None):
    """Posts a news outlet has since reported: link them to the story."""
    when = when or _now()
    open_ = store._rows("SELECT * FROM chatter WHERE story='' AND fetched_at >= ?", (_iso(when - timedelta(hours=24)),))
    if not open_:
        return 0
    stories = store._rows("SELECT id, headline FROM news_stories WHERE last_at >= ?", (_iso(when - timedelta(hours=36)),))
    n = 0
    for c in open_:
        rx = re.compile(r"(?<![\w])" + re.escape(c["place"]) + r"(?![\w])", re.I)
        for s in stories:
            text = " ".join(i["title"] + " " + i["summary"] for i in store._rows("SELECT title, summary FROM news_items WHERE story=?", (s["id"],)))
            if rx.search(text) and _alarm(text) == c["alarm"]:
                store._exec("UPDATE chatter SET story=? WHERE id=?", (s["id"], c["id"]))
                n += 1
                break
    return n


def corroborate(store, plist, when=None):
    """Two different people posting about the same town and the same thing within a few hours, not yet in the news:
    a heads-up to everyone's bell, once a day per town and thing."""
    when = when or _now()
    who = {p["name"].lower(): p["label"] for p in plist}
    rows = store._rows("SELECT * FROM chatter WHERE at >= ? ORDER BY at", (_iso(when - timedelta(hours=CHATTER_HOURS)),))
    groups = {}
    for c in rows:
        groups.setdefault((c["place"].lower(), c["alarm"]), []).append(c)
    raised = 0
    for (place, alarm), posts in groups.items():
        authors = {(c["source"], c["author"].lower()) for c in posts}
        if len(authors) < 2 or any(c["story"] for c in posts):
            continue
        if not news._once(store, f"chatter:{place}:{alarm}:{when.date().isoformat()}"):
            continue
        where = sorted({c["channel"] or "Bluesky" for c in posts})
        label = who.get(place, posts[0]["place"])
        for fn in list(listeners):
            try:
                fn("news", {"title": f"Unconfirmed, near {label}: people are posting about {posts[-1]['alarm']}",
                            "body": f"{len(authors)} people on {', '.join(where)} in the last {CHATTER_HOURS} hours. No news outlet has "
                                    "reported it yet: treat it as a heads-up, not a fact.",
                            "tag": f"chatter-{place}-{alarm}", "url": "/#brief", "user_ids": None, "family": True})
            except Exception:
                pass
        raised += 1
    return raised


def recent(store, limit=60):
    rows = store._rows("SELECT c.*, COALESCE(s.headline, '') AS story_headline FROM chatter c LEFT JOIN news_stories s ON s.id = c.story "
                       "ORDER BY c.at DESC LIMIT ?", (limit,))
    for r in rows:
        if r["story"]:
            first = store._rows("SELECT outlet, summary FROM news_items WHERE story=? ORDER BY at LIMIT 1", (r["story"],))
            o = first[0] if first else {"outlet": "", "summary": ""}
            r["reported_by"] = (o["summary"].removeprefix("Reported by ").rstrip(".") if o["outlet"] == "local" and o["summary"]
                                else brief.OUTLET.get(o["outlet"], {}).get("name", o["outlet"]))
    return rows


def run(store, folder, opener=None, when=None):
    """Every 10 minutes on the hub: fetch posts, link ones the news has reported, raise corroborated ones."""
    plist = places(store)
    status = {}
    posts = []
    if plist and settings(store)["bluesky"]:
        try:
            got = bluesky(plist, opener, when)
            posts += got
            status["bluesky"] = {"ok": True, "count": len(got), "at": _iso(_now())}
        except Exception as exc:
            status["bluesky"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160], "at": _iso(_now())}
    if plist and settings(store)["reddit_id"] and has_secret(folder):
        try:
            got = reddit(store, folder, plist, opener, when)
            posts += got
            status["reddit"] = {"ok": True, "count": len(got), "at": _iso(_now())}
        except Exception as exc:
            _token.update(value="", until=0.0)
            status["reddit"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160], "at": _iso(_now())}
    new = save_chatter(store, posts)
    confirm(store, when)
    raised = corroborate(store, plist, when)
    store.set_raw("localwatch_status", status | {"places": len(plist)})
    return {"new": new, "raised": raised, "status": status}
