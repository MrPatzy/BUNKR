"""News: flagging a source that doesn't belong (and learning from it), checked brief sources, and early warning."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, brief, news, notify  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402
from tests.test_news import NOW, head  # noqa: E402

ADMIN = {"id": 1, "username": "dad", "role": "admin"}
KID = {"id": 2, "username": "kid", "role": "family"}

# two headlines that share rare-ish words but are different stories: the kind of mistake people flag
MUSK = head("nbc", "Trump and Musk trade barbs over Tesla tariffs at White House summit", "https://x.test/m1")
MUSK2 = head("cbs", "Musk fires back at Trump over Tesla tariffs", "https://x.test/m2")
ZEL = head("bbc", "Zelensky says Putin stalling as Trump summit on tariffs looms", "https://x.test/z1")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "n.db")
        self.heard = []
        news.listeners[:] = [lambda ev, d: self.heard.append((ev, d))]

    def tearDown(self):
        news.listeners.clear()
        self.tmp.cleanup()


class BriefSourceTests(unittest.TestCase):
    def test_off_topic_sources_are_dropped(self):
        heads = [MUSK, MUSK2, ZEL]
        reply = json.dumps({"lead": "x", "stories": [{"title": "Trump and Musk feud over Tesla tariffs",
                                                      "agree": "Elon Musk and President Trump argued publicly about tariffs on Tesla.",
                                                      "sources": [{"outlet": "nbc", "url": MUSK["url"]}, {"outlet": "cbs", "url": MUSK2["url"]},
                                                                  {"outlet": "bbc", "url": ZEL["url"]}, {"outlet": "bbc", "url": "https://made.up/"}]}]})
        srcs = brief.parse_ai(reply, heads)["stories"][0]["sources"]
        self.assertEqual([s["url"] for s in srcs], [MUSK["url"], MUSK2["url"]])  # Zelensky's and the made-up link are gone
        self.assertEqual(srcs[0]["title"], MUSK["title"])                       # shown, so a reader can see what each one says

    def test_outlets(self):
        self.assertTrue(set(brief.FAST) <= set(brief.OUTLET))
        for k in ("nextgov", "dtn", "therecord", "npr_us", "bno"):
            self.assertIn(k, brief.OUTLET)


class FeedFlagTests(Base):
    def setUp(self):
        super().setUp()
        news.refresh(self.st, [MUSK, MUSK2, ZEL])
        self.sid = self.st._rows("SELECT story FROM news_items WHERE url=?", (MUSK["url"],))[0]["story"]

    def story_urls(self, sid):
        return sorted(r["url"] for r in self.st._rows("SELECT url FROM news_items WHERE story=?", (sid,)))

    def test_flag_review_and_learn(self):
        self.assertEqual(self.story_urls(self.sid), sorted(x["url"] for x in (MUSK, MUSK2, ZEL)))  # the bad match
        why = json.loads(self.st._rows("SELECT match FROM news_items WHERE url=?", (ZEL["url"],))[0]["match"])
        self.assertIn("summit", why["words"])
        news.flag(self.st, KID, "feed", ZEL["url"], "this is about Ukraine", story=self.sid)
        self.assertEqual(self.story_urls(self.sid), sorted([MUSK["url"], MUSK2["url"]]))   # out at once
        self.assertEqual(self.heard[-1][0], "reminder")                                     # admins are asked to review
        f = news.flags(self.st)
        self.assertEqual((f["open"], f["flags"][0]["note"], f["flags"][0]["story_title"]), (1, "this is about Ukraine", MUSK["title"]))
        news.review(self.st, f["flags"][0]["id"], "confirm", ADMIN)
        weak = news.weak_words(self.st)
        self.assertTrue(set(why["words"]) <= set(weak))
        with self.assertRaises(ValueError):
            news.review(self.st, f["flags"][0]["id"], "confirm", ADMIN)
        # the same kind of mismatch tomorrow: the blamed words count for less, so it starts its own story
        other = Store(Path(self.tmp.name) / "o.db")
        other.set_raw("news_weak_words", json.dumps(weak))
        news.refresh(other, [MUSK, MUSK2, ZEL])
        sids = {r["url"]: r["story"] for r in other._rows("SELECT url, story FROM news_items")}
        self.assertEqual(sids[MUSK["url"]], sids[MUSK2["url"]])
        self.assertNotEqual(sids[ZEL["url"]], sids[MUSK["url"]])

    def test_dismiss_puts_it_back_and_admin_flags_count_at_once(self):
        fid = news.flag(self.st, KID, "feed", MUSK2["url"], story=self.sid)
        self.assertNotIn(MUSK2["url"], self.story_urls(self.sid))
        news.review(self.st, fid, "dismiss", ADMIN)
        self.assertIn(MUSK2["url"], self.story_urls(self.sid))
        self.assertEqual(news.weak_words(self.st), {})
        news.flag(self.st, ADMIN, "feed", ZEL["url"], story=self.sid)
        self.assertEqual(news.flags(self.st)["open"], 0)
        self.assertTrue(news.weak_words(self.st))

    def test_lead_headline_and_bad_flags(self):
        news.flag(self.st, KID, "feed", MUSK["url"], story=self.sid)  # the story's own first headline
        self.assertNotEqual(self.st._rows("SELECT headline FROM news_stories WHERE id=?", (self.sid,))[0]["headline"], MUSK["title"])
        with self.assertRaises(KeyError):
            news.flag(self.st, KID, "feed", "https://nope/", story=self.sid)
        alone = self.st._rows("SELECT story FROM news_items WHERE url=?", (MUSK["url"],))[0]["story"]
        with self.assertRaises(ValueError):
            news.flag(self.st, KID, "feed", MUSK["url"], story=alone)   # its only source
        with self.assertRaises(ValueError):
            news.flag(self.st, KID, "other", MUSK["url"])


class BriefFlagTests(Base):
    def test_brief_source_out_and_back(self):
        rec = {"id": "b1", "created_at": "x", "ai": {"lead": "x", "stories": [{"title": "Trump and Musk feud", "sources": [
            {"outlet": "nbc", "url": MUSK["url"], "title": MUSK["title"]}, {"outlet": "bbc", "url": ZEL["url"], "title": ZEL["title"]}]}]}}
        brief.save(self.st, rec, "2026-11-21")
        fid = news.flag(self.st, KID, "brief", ZEL["url"], day="2026-11-21", index=0)
        self.assertEqual([s["url"] for s in brief.latest(self.st, "2026-11-21")["ai"]["stories"][0]["sources"]], [MUSK["url"]])
        f = news.flags(self.st)["flags"][0]
        self.assertEqual((f["kind"], f["source_title"], f["story_title"]), ("brief", ZEL["title"], "Trump and Musk feud"))
        news.review(self.st, fid, "dismiss", ADMIN)
        self.assertEqual(len(brief.latest(self.st, "2026-11-21")["ai"]["stories"][0]["sources"]), 2)
        with self.assertRaises(KeyError):
            news.flag(self.st, KID, "brief", ZEL["url"], day="2026-11-21", index=5)


class EarlyWarningTests(Base):
    def test_raised_once_from_the_first_outlet(self):
        hack = head("nextgov", "USDA grapples with security breach as IT staff take multiple systems offline", "https://x.test/u1", hours_ago=0.2)
        far = head("bbc", "Power outage hits Lagos after grid collapse", "https://x.test/l1", hours_ago=0.2)
        old = head("ap", "Hackers breach US water utility", "https://x.test/w1", hours_ago=20)
        r = news.refresh(self.st, [hack, far, old])
        self.assertEqual(r["raised"], 1)
        ev, d = self.heard[0]
        self.assertEqual((ev, d["title"], d["user_ids"]), ("news", hack["title"], None))
        self.assertIn("First from Nextgov/FCW", d["body"])
        s = [x for x in news.feed(self.st, 1)["stories"] if x["headline"] == hack["title"]][0]
        self.assertEqual(s["alarm"], "breach")
        news.refresh(self.st, [head("dtn", "USDA confirms cyberthreat on loan software amid systems outages", "https://x.test/u2", hours_ago=0.1)])
        self.assertEqual(sum(1 for ev, d in self.heard if d.get("user_ids") is None and "USDA grapples" in d["title"]), 1)  # once

    def test_watch_words_are_per_person(self):
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        ids = {r["username"]: r["id"] for r in self.st._rows("SELECT id, username FROM users")}
        news.save_prefs(self.st, ids["kid"], {"areas": ["us"], "watch": ["Corner Market", "ab"]})
        self.assertEqual(news.prefs(self.st, ids["kid"])["watch"], ["Corner Market"])     # too short dropped
        news.refresh(self.st, [head("cpr", "Corner Market workers vote to strike in Denver", "https://x.test/k1", hours_ago=0.3)])
        watched = [d for ev, d in self.heard if d.get("user_ids") == [ids["kid"]]]
        self.assertEqual(len(watched), 1)
        self.assertIn('watching "Corner Market"', watched[0]["body"])

    def test_push_rules(self):
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        ids = {r["username"]: r["id"] for r in self.st._rows("SELECT id, username FROM users")}
        from datetime import datetime, timezone
        day = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
        night = datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc)
        d = {"title": "USDA breach", "body": "First from Nextgov/FCW.", "tag": "news-1", "url": "/#brief", "user_ids": None}
        self.assertEqual(sorted(u for u, _ in notify.plan(self.st, "news", d, at=day)), sorted(ids.values()))
        self.assertEqual(notify.plan(self.st, "news", d, at=night), [])
        self.assertEqual(len(notify._plan(self.st, "news", d, at=night, inbox=True)), 2)  # no push at night, but the bell has it
        self.assertEqual([u for u, _ in notify.plan(self.st, "news", d | {"user_ids": [ids["kid"]]}, at=day)], [ids["kid"]])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        self.st = self.hub.store
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {u: self.login(u) for u in ("dad", "kid")}
        news.refresh(self.st, [MUSK, MUSK2, ZEL])
        self.sid = self.st._rows("SELECT story FROM news_items WHERE url=?", (MUSK["url"],))[0]["story"]

    def tearDown(self):
        news.listeners.clear()
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

    def test_flag_and_review(self):
        code, r = self.req("POST", "/api/news/flags", {"kind": "feed", "story": self.sid, "url": ZEL["url"], "note": "Ukraine"}, user="kid")
        self.assertEqual((code, r["reviewed"]), (200, False))
        self.assertEqual((r["moved_to"], r["story_now"]), (ZEL["title"], MUSK["title"]))   # where it went, what's left
        self.assertEqual(self.req("POST", "/api/news/flags", {"kind": "feed", "story": self.sid, "url": "https://nope/"}, user="kid")[0], 404)
        self.assertEqual(self.req("GET", "/api/news/flags", user="kid")[0], 403)
        f = self.req("GET", "/api/news/flags")[1]
        self.assertEqual(f["open"], 1)
        self.assertEqual(self.req("POST", f"/api/news/flags/{f['flags'][0]['id']}", {"decision": "confirm"}, user="kid")[0], 403)
        self.assertEqual(self.req("POST", f"/api/news/flags/{f['flags'][0]['id']}", {"decision": "maybe"})[0], 400)
        self.assertEqual(self.req("POST", f"/api/news/flags/{f['flags'][0]['id']}", {"decision": "confirm"})[1]["open"], 0)
        self.assertEqual(self.req("POST", "/api/me/news", {"areas": ["us"], "watch": ["USDA"]}, user="kid")[1]["watch"], ["USDA"])


if __name__ == "__main__":
    unittest.main()


# the mismatch from the hub on 2026-11-21: everyday words ("central", "kills", "wounds") joined two places
ISRAEL = head("haaretz", "Car explosion kills woman, wounds her two children in central Israel", "https://x.test/il1")
KRYVYI = head("ap", "Ballistic missile attack on Zelenskyy's hometown in central Ukraine kills 2 and wounds scores", "https://x.test/ua1")
KRYVYI2 = head("aljazeera", "Russian strike kills two, wounds 60, in Zelenskyy's hometown", "https://x.test/ua2")
KRYVYI3 = head("dw", "Russia strikes Zelenskyy's hometown, killing 2", "https://x.test/ua3")


class MatchingTests(Base):
    def story(self, url, st=None):
        return (st or self.st)._rows("SELECT story FROM news_items WHERE url=?", (url,))[0]["story"]

    def test_places_and_everyday_words_dont_make_a_story(self):
        news.refresh(self.st, [ISRAEL, KRYVYI, KRYVYI2, KRYVYI3])
        self.assertNotEqual(self.story(ISRAEL["url"]), self.story(KRYVYI["url"]))
        self.assertEqual(len({self.story(h["url"]) for h in (KRYVYI, KRYVYI2, KRYVYI3)}), 1)
        w = lambda x: 6.0
        self.assertEqual(news.fit({"kill", "wound", "central"}, set(), {"kill", "wound", "central"}, set(), w), 0)   # everyday only
        self.assertTrue(news.fit({"zelenskyy", "hometown"}, set(), {"zelenskyy", "hometown", "kill"}, set(), w))

    def test_an_old_bad_story_is_fixed_and_titled_by_its_sources(self):
        # filed the old way: the Israel headline first, the Ukraine ones under it
        self.st._exec("INSERT INTO news_stories(id, headline, first_at, last_at) VALUES('bad', ?, ?, ?)", (ISRAEL["title"], ISRAEL["at"], ISRAEL["at"]))
        for h in (ISRAEL, KRYVYI, KRYVYI2):
            self.st._exec("INSERT INTO news_items(url, outlet, title, summary, at, fetched_at, story, match) VALUES(?,?,?,?,?,?,?,?)",
                          (h["url"], h["outlet"], h["title"], "", h["at"], h["at"], "bad", ""))
        news._update_story(self.st, "bad")
        news.tidy_recent(self.st, NOW)
        self.assertNotEqual(self.story(ISRAEL["url"]), "bad")                   # the odd one out moved
        self.assertIn("Zelenskyy", self.st._rows("SELECT headline FROM news_stories WHERE id='bad'")[0]["headline"])

    def test_a_flag_moves_the_source_where_it_fits_and_learns(self):
        news.refresh(self.st, [KRYVYI2, KRYVYI3])
        right = self.story(KRYVYI2["url"])
        self.st._exec("INSERT INTO news_stories(id, headline, first_at, last_at) VALUES('bad', ?, ?, ?)", (ISRAEL["title"], ISRAEL["at"], ISRAEL["at"]))
        for h in (ISRAEL, KRYVYI):
            self.st._exec("INSERT INTO news_items(url, outlet, title, summary, at, fetched_at, story, match) VALUES(?,?,?,?,?,?,?,?)",
                          (h["url"], h["outlet"], h["title"], "", h["at"], h["at"], "bad", json.dumps({"words": ["central", "kill", "wound"]})))
        news._update_story(self.st, "bad")
        fid = news.flag(self.st, ADMIN, "feed", KRYVYI["url"], story="bad")      # an admin's flag counts at once
        self.assertEqual(self.story(KRYVYI["url"]), right)                       # into the Zelenskyy story, not a card of its own
        pairs = news.apart_pairs(self.st)
        self.assertTrue(pairs and "ukrain" in pairs[0][0] | pairs[0][1] and "israel" in pairs[0][0] | pairs[0][1])
        self.assertTrue(news._kept_apart({"israel", "car"}, {"ukrain", "missil"}, pairs))
        self.assertFalse(news._kept_apart({"israel", "ukrain"}, {"ukrain"}, pairs))   # both sides named: no block
        # undo: the lesson is forgotten and the source goes back
        news.review(self.st, fid, "undo", ADMIN)
        self.assertEqual(news.apart_pairs(self.st), [])
        self.assertEqual(news.weak_words(self.st), {})
        self.assertEqual(self.story(KRYVYI["url"]), "bad")
        with self.assertRaises(ValueError):
            news.review(self.st, fid, "undo", ADMIN)

    def test_lead_is_the_headline_most_sources_share(self):
        items = [dict(h, at=f"2026-11-21T0{n}:00:00+00:00") for n, h in enumerate((ISRAEL, KRYVYI, KRYVYI2, KRYVYI3))]
        self.assertNotEqual(news.lead(items, lambda w: 3.0)["url"], ISRAEL["url"])
