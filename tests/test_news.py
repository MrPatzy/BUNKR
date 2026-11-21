"""The news feed: places and topics, filing headlines into stories, each person's ranking, ratings, reads and the map."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, news  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def head(outlet, title, url, hours_ago=1, summary=""):
    return {"outlet": outlet, "title": title, "url": url, "summary": summary, "at": (NOW - timedelta(hours=hours_ago)).isoformat()}


HEADS = [
    head("reuters", "Russian drone strike hits Kyiv apartment block", "https://x.test/1"),
    head("tass", "Russian forces strike Kyiv energy targets, ministry says", "https://x.test/2"),
    head("aljazeera", "Drone strike on Kyiv apartment block kills five", "https://x.test/3"),
    head("cpr", "Wildfire near Boulder forces evacuations", "https://x.test/4"),
    head("coloradosun", "Boulder wildfire evacuations expand overnight", "https://x.test/5"),
    head("coloradosun", "School board approves new calendar", "https://x.test/6"),
    head("npr", "Fed holds interest rates steady as prices climb", "https://x.test/7", hours_ago=5),
    head("japantimes", "Japan earthquake shakes Tokyo, no tsunami warning", "https://x.test/8"),
]


class TagTests(unittest.TestCase):
    def test_places_and_topics(self):
        self.assertEqual(news.tag(["Wildfire forces evacuations near Boulder"])["place"], "Boulder")
        t = news.tag(["Russian drone strike hits Kyiv apartment block"])
        self.assertEqual((t["area"], t["place"], t["topic"]), ("russia", "Ukraine", "conflict"))  # where it happened, not who did it
        self.assertEqual(news.tag(["Measles outbreak spreads in Texas"])["topic"], "health")
        self.assertEqual(news.tag(["Apple unveils new chips"])["area"], "world")
        self.assertEqual(news.tag(["School board approves new calendar"], outlets=["coloradosun"])["area"], "colorado")
        self.assertEqual(news.tag(["Let us know what you think"])["area"], "world")  # "us" only as "US"
        self.assertEqual(news.tag(["US Fed holds rates"])["area"], "us")

    def test_prefs_are_cleaned(self):
        self.assertEqual(news.clean_prefs({"areas": ["colorado", "mars", "colorado"], "muted_topics": ["politics", "x"]}),
                         {"areas": ["colorado"], "muted_topics": ["politics"], "watch": []})
        self.assertEqual(news.clean_prefs(None)["areas"], [a for a, _ in news.AREAS])


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "n.db")
        self.r = news.refresh(self.st, HEADS)

    def tearDown(self):
        self.tmp.cleanup()

    def stories(self, uid=1, **kw):
        return news.feed(self.st, uid, **kw)["stories"]

    def test_headlines_group_into_stable_stories(self):
        self.assertEqual(self.r["stories"], 5)
        kyiv = [s for s in self.stories() if s["place"] == "Ukraine"]
        self.assertEqual(len(kyiv), 1)
        self.assertEqual(kyiv[0]["outlets"], 3)
        self.assertEqual(len(kyiv[0]["regions"]), 3)
        self.assertEqual(len(kyiv[0]["items"]), 3)
        # a later headline joins the same story; the id holds; repeats are ignored
        r = news.refresh(self.st, [head("bbc", "Kyiv apartment block hit in overnight drone strike", "https://x.test/9"), HEADS[0]])
        self.assertEqual(r["new"], 0)
        again = [s for s in self.stories() if s["place"] == "Ukraine"][0]
        self.assertEqual((again["id"], again["outlets"]), (kyiv[0]["id"], 4))

    def test_wide_coverage_ranks_first_and_prefs_filter(self):
        self.assertEqual(self.stories()[0]["place"], "Ukraine")
        news.save_prefs(self.st, None, {"areas": ["colorado"], "muted_topics": []})  # no logins: the shared reader
        self.assertEqual({s["area"] for s in self.stories(None)}, {"colorado"})
        news.save_prefs(self.st, None, {"areas": ["colorado"], "muted_topics": ["safety"]})
        self.assertEqual([s["headline"] for s in self.stories(None)], ["School board approves new calendar"])
        self.assertEqual(len(self.stories(None, area="russia")), 1)  # choosing a place shows it even if not followed

    def test_ratings_teach_one_person_only(self):
        quake = [s for s in self.stories() if s["area"] == "eastasia"][0]
        before = [s["id"] for s in self.stories(2)]
        for s in self.stories():
            if s["area"] == "colorado":
                news.rate(self.st, 1, s["id"], 1)
        news.rate(self.st, 1, quake["id"], -1)
        mine = self.stories()
        self.assertEqual(mine[0]["area"], "colorado")
        self.assertNotIn(quake["id"], [s["id"] for s in mine])  # "Not for me" hides it
        self.assertEqual(news.learned(self.st, 1)["area"]["colorado"], 2)
        self.assertEqual([s["id"] for s in self.stories(2)], before)  # someone else's feed is untouched
        with self.assertRaises(KeyError):
            news.rate(self.st, 1, "nope", 1)

    def test_reads_unread_and_map(self):
        ids = [s["id"] for s in self.stories()]
        pts = news.map_points(self.st, 1)
        self.assertIn("Boulder", {p["place"] for p in pts})
        self.assertNotIn("", {p["place"] for p in pts})  # stories with no place aren't pinned
        news.mark_read(self.st, 1, ids[:2])
        self.assertEqual(len(self.stories(unread=True)), len(ids) - 2)
        self.assertEqual(sum(p["unread"] for p in news.map_points(self.st, 1, unread_only=False)),
                         sum(1 for s in self.stories(unread=True) if s["lat"] is not None))
        self.assertEqual(len(self.stories(2, unread=True)), len(ids))

    def test_ai_tags_are_checked(self):
        multi = [s["id"] for s in self.stories() if s["outlets"] >= 2]
        reply = {multi[0]: {"area": "europe", "place": "Brussels", "lat": 50.85, "lon": 4.35, "topic": "politics"},
                 multi[1]: {"area": "mars", "place": "x", "lat": 0, "lon": 0, "topic": "politics"}}
        n = news.ai_tag(self.st, multi, lambda prompt: "Sure: " + json.dumps(reply))
        self.assertEqual(n, 1)
        got = {s["id"]: s for s in self.stories()}
        self.assertEqual(got[multi[0]]["place"], "Brussels")
        self.assertNotEqual(got[multi[1]]["area"], "mars")

    def test_old_stories_are_pruned(self):
        news.prune(self.st, NOW + timedelta(days=news.KEEP_DAYS + 1))
        self.assertEqual(self.st._rows("SELECT COUNT(*) AS n FROM news_items")[0]["n"], 0)


class NewsHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        self.st = self.hub.store
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        news.refresh(self.st, HEADS)
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {u: self.login(u) for u in ("dad", "kid")}

    def tearDown(self):
        self.hub.close()

    def login(self, user):
        r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": user, "password": p3.PW}).encode(),
                                   method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
        with self.opener.open(r) as resp:
            return resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def req(self, method, path, body=None, user="kid"):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_feed_prefs_rate_read_map(self):
        code, r = self.req("GET", "/api/news")
        self.assertEqual(code, 200)
        self.assertEqual(len(r["stories"]), 5)
        self.assertIn("reuters", r["outlets"])
        self.assertNotIn("feed", r["outlets"]["reuters"])
        self.assertEqual(self.req("POST", "/api/me/news", {"areas": ["colorado", "us"], "muted_topics": []})[1]["areas"], ["colorado", "us"])
        r = self.req("GET", "/api/news")[1]
        self.assertEqual({s["area"] for s in r["stories"]}, {"colorado"})  # the Fed story names no place: "Worldwide"
        sid = r["stories"][0]["id"]
        self.assertEqual(self.req("POST", "/api/news/rate", {"id": sid, "rating": 1})[1], {"rating": 1})
        self.assertEqual(self.req("POST", "/api/news/rate", {"id": "nope", "rating": 1})[0], 404)
        self.req("POST", "/api/news/read", {"ids": [s["id"] for s in r["stories"]]})
        self.assertEqual(self.req("GET", "/api/news?unread=1")[1]["stories"], [])
        self.assertEqual(self.req("GET", "/api/news/map")[1]["points"], [])
        self.assertTrue(self.req("GET", "/api/news/map?all=1")[1]["points"])
        got = self.req("GET", "/api/news/stories?ids=" + sid)[1]["stories"]
        self.assertEqual((got[0]["id"], got[0]["rating"], got[0]["read"]), (sid, 1, True))
        # dad's feed doesn't change
        self.assertEqual(len(self.req("GET", "/api/news", user="dad")[1]["stories"]), 5)
        self.assertEqual(self.req("POST", "/api/news/refresh")[0], 403)

    def test_home_tile_shows_my_unread(self):
        st = self.req("GET", "/api/state")[1]["news_home"]
        self.assertEqual(st["unread"], 5)
        self.assertEqual(len(st["stories"]), 4)
        ids = [s["id"] for s in self.req("GET", "/api/news")[1]["stories"]]
        self.req("POST", "/api/news/read", {"ids": ids})
        self.assertEqual(self.req("GET", "/api/state")[1]["news_home"]["stories"], [])


if __name__ == "__main__":
    unittest.main()
