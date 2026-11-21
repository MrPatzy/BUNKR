"""Local watch: family towns from the Sites tab, their own news search, "Near Jordan" alerts, and unconfirmed posts."""

import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, checkins, localwatch, news, notify  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)
RFC822 = lambda d: d.strftime("%a, %d %b %Y %H:%M:%S +0000")  # noqa: E731


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWeb:
    """Answers by URL fragment; records what was asked."""

    def __init__(self, routes):
        self.routes, self.asked = routes, []

    def open(self, req, timeout=None):
        url = req.full_url
        self.asked.append((url, dict(req.header_items()), req.data))
        for frag, body in self.routes.items():
            if frag in url:
                return Resp(body if isinstance(body, bytes) else json.dumps(body).encode())
        raise urllib.error.URLError("no route for " + url)


def gn(*titles):
    items = "".join(f"<item><title>{t}</title><link>https://news.example/{abs(hash(t))}</link><pubDate>{RFC822(NOW - timedelta(minutes=20))}</pubDate></item>"
                    for n, t in enumerate(titles))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{items}</channel></rss>'.encode()


def bsky_post(handle, text, minutes=10, rkey="abc"):
    return {"uri": f"at://did:plc:x/app.bsky.feed.post/{rkey}", "author": {"handle": handle},
            "record": {"text": text, "createdAt": (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.st = Store(self.dir / "w.db")
        home = self.st._exec("INSERT INTO sites(name, kind, address) VALUES('Home','home','100 Main St, Longmont, CO 80501')")
        ty = self.st._exec("INSERT INTO sites(name, kind, address) VALUES('Jordan''s','relative','27 Main St, Nederland, CO 80466')")
        self.st._exec("INSERT INTO sites(name, kind, address) VALUES('Somewhere','other','no city here')")
        for name, remote, site in (("Alex", 0, None), ("Jordan", 1, ty)):
            self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at, site_id) VALUES(?,?,?,?,?,?)",
                          (name, "adult", remote, uuid.uuid4().hex, checkins.now(), site))
        self.heard = []
        hear = lambda ev, d: self.heard.append((ev, d))  # noqa: E731
        news.listeners[:] = [hear]
        localwatch.listeners[:] = [hear]

    def tearDown(self):
        news.listeners.clear()
        localwatch.listeners.clear()
        self.tmp.cleanup()


class PlaceTests(Base):
    def test_city_of(self):
        self.assertEqual(localwatch.city_of("27 Main St, Nederland, CO 80466"), ("Nederland", "CO"))
        self.assertEqual(localwatch.city_of("1 Elm St, Apt 4, Fort Collins, Colorado 80521"), ("Fort Collins", "CO"))
        self.assertEqual(localwatch.city_of("12 Oak Ave, Omaha, NE"), ("Omaha", "NE"))
        self.assertEqual(localwatch.city_of("no city here"), ("", ""))
        self.assertEqual(localwatch.city_of("Denver"), ("", ""))

    def test_places_from_sites_and_people(self):
        localwatch.save_settings(self.st, self.dir, {"extra_places": "Conifer, CO\n\nConifer, CO"})
        got = {p["name"]: p["label"] for p in localwatch.places(self.st)}
        self.assertEqual(got, {"Longmont": "home (Longmont)", "Nederland": "Jordan (Nederland)", "Conifer": "Conifer"})


class LocalNewsTests(Base):
    def test_town_search_and_near_family(self):
        web = FakeWeb({"Nederland": gn("Nederland urged to not drink water unless boiled after water main break - KDVR",
                                       "Nederland forests in Oregon face beetles - Some Blog",
                                       "Longmont library reopens - Nowhere"),
                       "Longmont": gn("Longmont council meets - Longmont Press")})
        plist = localwatch.places(self.st)
        heads, status = localwatch.fetch_places(plist, opener=web)
        self.assertTrue(any("%22Nederland%22+Colorado+when%3A1d" in u for u, _, _ in web.asked))
        town = [h for h in heads if h["title"].startswith("Nederland urged")][0]
        self.assertEqual((town["outlet"], town["summary"]), ("local", "Reported by KDVR."))
        self.assertFalse(any("library" in h["title"] for h in heads))   # matched on the page, not the headline: dropped
        self.assertEqual(status["Nederland"]["ok"], True)
        r = news.refresh(self.st, heads, places=plist)
        near = [d for ev, d in self.heard if ev == "news" and d.get("family")]
        self.assertEqual(len(near), 1)
        self.assertTrue(near[0]["title"].startswith("Near Jordan (Nederland): Nederland urged"))
        self.assertIn("First from KDVR", near[0]["body"])
        self.assertEqual(r["raised"], 0)      # raised as near-family, not as a general alarm
        # the beetle story names Nederland but isn't trouble: not raised
        news.refresh(self.st, heads, places=plist)
        self.assertEqual(len([d for ev, d in self.heard if d.get("family")]), 1)   # once

    def test_near_family_pushes_skip_the_cap_but_not_quiet_hours(self):
        auth.create_user(self.st, "dad", p3.PW, "admin")
        uid = self.st._rows("SELECT id FROM users")[0]["id"]
        self.st._exec("UPDATE users SET push_prefs=? WHERE id=?", (json.dumps({"priority": False}), uid))
        d = {"title": "Near Jordan (Nederland): boil water", "body": "", "tag": "news-x", "user_ids": None, "family": True}
        day, night = datetime(2026, 11, 21, 18, tzinfo=timezone.utc), datetime(2026, 11, 21, 6, tzinfo=timezone.utc)
        self.assertEqual(len(notify.plan(self.st, "news", d, at=day)), 1)             # even with "Soon / near you" off
        self.assertEqual(notify.plan(self.st, "news", d | {"family": False}, at=day), [])
        self.assertEqual(notify.plan(self.st, "news", d, at=night), [])
        self.assertEqual(len(notify._plan(self.st, "news", d, at=night, inbox=True)), 1)


class ChatterTests(Base):
    def run_with(self, posts, reddit=None):
        routes = {"searchPosts": {"posts": posts}}
        if reddit is not None:
            routes |= {"access_token": {"access_token": "tok", "expires_in": 3600}, "oauth.reddit.com": reddit}
        web = FakeWeb(routes)
        return localwatch.run(self.st, self.dir, opener=web), web

    def test_bluesky_filters_and_two_people_raise_once(self):
        r, web = self.run_with([bsky_post("amy.bsky.social", "Boil water notice in Nederland right now, main broke on 74", rkey="1"),
                                bsky_post("bob.bsky.social", "Love the evergreen trees today", rkey="2"),
                                bsky_post("cat.bsky.social", "Huge outage downtown", rkey="3")])
        self.assertEqual(r["new"], 1)
        self.assertEqual(r["raised"], 0)                    # one person isn't enough
        rows = localwatch.recent(self.st)
        self.assertEqual((rows[0]["place"], rows[0]["alarm"], rows[0]["url"]),
                         ("Nederland", "a water problem (boil or don't drink)", "https://bsky.app/profile/amy.bsky.social/post/1"))
        r, _ = self.run_with([bsky_post("dan.bsky.social", "Nederland, CO folks: don't drink tap water until boiled", rkey="4"),
                                bsky_post("fda.bot", "Sanniu Nederland Biscuits recalled: undeclared milk", rkey="6"),
                                bsky_post("fda.bot2", "Nederland Biscuits recall notice", rkey="7")])
        self.assertEqual(r["raised"], 1)
        title = [d for ev, d in self.heard if d.get("tag", "").startswith("chatter-")][0]["title"]
        self.assertEqual(title, "Unconfirmed, near Jordan (Nederland): people are posting about a water problem (boil or don't drink)")
        self.assertFalse([p for p in localwatch.recent(self.st) if "Biscuits" in p["text"]])  # a product, not the town
        r, _ = self.run_with([bsky_post("eve.bsky.social", "Boil water still on in Nederland", rkey="5")])
        self.assertEqual(r["raised"], 0)                    # once a day

    def test_reported_by_news_and_reddit(self):
        localwatch.save_settings(self.st, self.dir, {"reddit_id": "abcDEF123456", "reddit_secret": "s" * 30, "subreddits": "Denver\nNederlandCO"})
        mode = stat.S_IMODE(os.stat(self.dir / localwatch.SECRET_FILE).st_mode)
        self.assertEqual(mode, 0o600)
        listing = {"data": {"children": [
            {"data": {"title": "Water main break, boil order?", "selftext": "Anyone else on the north side?", "author": "u1",
                      "subreddit": "NederlandCO", "permalink": "/r/NederlandCO/comments/1/x/", "created_utc": (NOW - timedelta(minutes=30)).timestamp()}},
            {"data": {"title": "Outage in Lakewood", "selftext": "", "author": "u2", "subreddit": "Denver",
                      "permalink": "/r/Denver/comments/2/y/", "created_utc": NOW.timestamp()}}]}}
        localwatch._token.update(value="", until=0.0)
        r, web = self.run_with([], reddit=listing)
        auths = [h.get("Authorization", "") for u, h, _ in web.asked if "oauth.reddit.com" in u]
        self.assertEqual(auths, ["Bearer tok"])
        self.assertTrue(any(u.endswith("/r/Denver+NederlandCO/new?limit=100&raw_json=1") for u, _, _ in web.asked))
        posts = localwatch.recent(self.st)
        self.assertEqual([(p["channel"], p["place"]) for p in posts], [("r/NederlandCO", "Nederland")])  # Lakewood isn't a family town
        # a news outlet reports it: the post says so, and posts already reported don't raise a heads-up
        news.refresh(self.st, [{"outlet": "local", "title": "Nederland urged to not drink water unless boiled", "url": "https://n/1",
                                "summary": "Reported by KDVR.", "at": NOW.isoformat()}])
        localwatch.confirm(self.st)
        p = localwatch.recent(self.st)[0]
        self.assertEqual(p["reported_by"], "KDVR")
        self.assertIn("Nederland urged", p["story_headline"])

    def test_settings_checks(self):
        v = localwatch.save_settings(self.st, self.dir, {"subreddits": "r/Denver, /r/Colorado", "bluesky": False})
        self.assertEqual((v["subreddits"], v["bluesky"], v["reddit_ready"]), (["Denver", "Colorado"], False, False))
        for bad in ({"subreddits": "not ok!"}, {"reddit_id": "x"}, {"reddit_secret": "short"}):
            with self.assertRaises(ValueError):
                localwatch.save_settings(self.st, self.dir, bad)
        r, web = self.run_with([bsky_post("a.b", "Nederland boil water", rkey="9")])
        self.assertEqual(web.asked, [])                    # Bluesky off, Reddit not set up: nothing fetched


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        self.st = self.hub.store
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {u: self.login(u) for u in ("dad", "kid")}

    def tearDown(self):
        self.hub.close()

    def login(self, user):
        r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": user, "password": p3.PW}).encode(),
                                   method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
        with self.opener.open(r) as resp:
            return resp.headers["Set-Cookie"].split("bunkr_session=")[1].split(";")[0]

    def req(self, method, path, body=None, user="dad"):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_routes(self):
        self.assertEqual(self.req("GET", "/api/localwatch", user="kid")[0], 403)
        code, v = self.req("POST", "/api/localwatch", {"extra_places": "Nederland, CO", "reddit_id": "abcDEF123456", "reddit_secret": "q" * 30})
        self.assertEqual((code, [p["name"] for p in v["places"]], v["reddit_ready"]), (200, ["Nederland"], True))
        self.assertNotIn("q" * 30, json.dumps(self.req("GET", "/api/localwatch")[1]))
        code, c = self.req("GET", "/api/news/chatter", user="kid")
        self.assertEqual((code, c["places"], c["posts"]), (200, ["Nederland"], []))


if __name__ == "__main__":
    unittest.main()
