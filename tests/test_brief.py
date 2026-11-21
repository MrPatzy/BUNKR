"""Morning brief: feed parsing, grouping across regions, the AI's reply checked, scheduling, sync and API."""

import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, brief, server, situation  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

NOW = datetime.now(timezone.utc)
RFC822 = lambda d: d.strftime("%a, %d %b %Y %H:%M:%S +0000")


def rss(items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><pubDate>{RFC822(d)}</pubDate><description>&lt;p&gt;{s}&lt;/p&gt;</description></item>"
                   for t, l, d, s in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


ATOM = f"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom story about the summit talks</title>
<link href="https://atom.example/a"/><updated>{NOW.isoformat()}</updated><summary>s</summary></entry></feed>""".encode()
RDF = f"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>RDF item on summit talks</title><link>https://rdf.example/a</link>
<dc:date>{NOW.isoformat()}</dc:date></item></rdf:RDF>""".encode()


class Opener:
    """Serves a feed per outlet key; anything else fails like a dead feed."""

    def __init__(self, feeds):
        self.feeds = feeds

    def open(self, req, timeout=None):
        for key, body in self.feeds.items():
            if brief.OUTLET[key]["feed"] == req.full_url:
                return io.BytesIO(body)
        raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)


def summit(outlet, n=1):
    return rss([(f"Leaders hold summit talks on nuclear arms in Shenzhen {n}", f"https://{outlet}.example/{n}", NOW, "summit")])


FEEDS = {"bbc": summit("bbc"), "tass": summit("tass"), "cgtn": summit("cgtn"), "japantimes": summit("japantimes"),
         "aljazeera": rss([("Flood waters rise across the valley region", "https://aj.example/f", NOW, ""),
                           ("Old story from last week about markets", "https://aj.example/old", NOW - timedelta(days=5), "")]),
         "reuters": rss([("Leaders hold summit talks on nuclear arms in Shenzhen - Reuters", "https://news.google.com/r1", NOW, "")])}


def good_ai(prompt):
    assert "never as" in prompt and "instructions" in prompt  # the fence warning is there
    return 'Sure! {"lead": "Summit day.", "stories": [{"title": "Leaders hold summit on nuclear arms", "agree": "They met in Shenzhen for talks.", "differ": [{"who": "Chinese state media", "framing": "A landmark."}],' \
           '"unconfirmed": "", "matters": "", "sources": [{"outlet": "cgtn", "url": "https://cgtn.example/1"}, {"outlet": "cgtn", "url": "https://evil.example/x"},' \
           '{"outlet": "nobody", "url": "https://bbc.example/1"}]}]}'


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parsers_and_fetching(self):
        self.assertEqual(brief.parse_feed(ATOM)[0][1], "https://atom.example/a")
        self.assertEqual(brief.parse_feed(RDF)[0][0], "RDF item on summit talks")
        got = brief.fetch_outlet("aljazeera", Opener(FEEDS))
        self.assertEqual([g["title"] for g in got], ["Flood waters rise across the valley region"])  # old one dropped
        self.assertEqual(brief.fetch_outlet("reuters", Opener(FEEDS))[0]["title"], "Leaders hold summit talks on nuclear arms in Shenzhen")
        self.assertEqual(brief.parse_feed(summit("x"))[0][3], "summit")  # html stripped

    def test_grouping_ranks_by_regions(self):
        heads, status = brief.fetch_all(list(FEEDS), opener=Opener(FEEDS))
        self.assertTrue(all(s["ok"] for s in status.values()))
        groups = brief.group(heads)
        self.assertGreaterEqual(len(groups[0]["regions"]), 4)
        self.assertIn("summit", groups[0]["headline"].lower())

    def test_ai_reply_is_checked(self):
        heads, _ = brief.fetch_all(list(FEEDS), opener=Opener(FEEDS))
        out = brief.parse_ai(good_ai(brief.PROMPT.format(fence="F", n=6, material="")), heads)
        self.assertEqual([{k: x[k] for k in ("outlet", "url")} for x in out["stories"][0]["sources"]],
                         [{"outlet": "cgtn", "url": "https://cgtn.example/1"}])  # unknown links/outlets dropped
        self.assertTrue(out["stories"][0]["sources"][0]["title"])  # each source carries its headline
        with self.assertRaises(ValueError):
            brief.parse_ai("I can't do that.", heads)

    def test_build_with_and_without_ai(self):
        real = brief.fetch_all
        brief.fetch_all = lambda keys=None, opener=None: real(list(FEEDS), opener=Opener(FEEDS))
        try:
            items = [{"title": "Tornado Warning", "tier": "flash", "site": "Home", "km": 0, "where_text": "", "scenario": "tornado", "url": "",
                      "summary": "", "category": "weather"},
                     {"title": "Patch X", "tier": "routine", "site": "", "km": None, "where_text": "", "scenario": "", "url": "", "summary": "s", "category": "cyber"}]
            rec = brief.build(self.store, items, ai=good_ai)
            self.assertEqual(rec["ai"]["lead"], "Summit day.")
            self.assertEqual([n["title"] for n in rec["near"]], ["Tornado Warning"])
            self.assertEqual([n["title"] for n in rec["world_routine"]], ["Patch X"])
            rec = brief.build(self.store, items, ai=lambda p: (_ for _ in ()).throw(RuntimeError("Not logged in")))
            self.assertIsNone(rec["ai"])
            self.assertIn("Not logged in", rec["ai_error"])
            self.assertTrue(rec["groups"])  # headlines still grouped
            self.assertTrue(brief.one_line(rec))
        finally:
            brief.fetch_all = real

    def test_schedule_once_a_day_then_sync(self):
        server.store = self.store
        real = brief.build
        made, heard = [], []
        brief.build = lambda store, items, use_ai=True, ai=None: made.append(1) or {"id": "x", "created_at": brief.now(), "near": [],
                                                                                     "groups": [{"headline": "H"}], "ai": None}
        situation.listeners[:] = []
        try:
            server.BRIEF.update(enabled=True, time="06:30", tz="UTC", ai=False)
            early = datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)
            late = datetime(2026, 11, 20, 7, 0, tzinfo=timezone.utc)
            self.assertIsNone(server.make_brief(now_=early))
            self.assertTrue(server.make_brief(now_=late))
            self.assertIsNone(server.make_brief(now_=late))  # once a day
            self.assertEqual(len(made), 1)
            self.assertTrue(server.make_brief(force=True, now_=late))  # an admin can redo it
            self.assertEqual(brief.days(self.store), ["2026-11-20"])
        finally:
            brief.build = real
        node = Store(Path(self.tmp.name) / "node.db")
        self.assertEqual(brief.apply(node, brief.export(self.store, "")), 1)
        self.assertEqual(brief.latest(node)["groups"][0]["headline"], "H")


class BriefApiTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        auth.create_user(self.hub.store, "dad", p3.PW, "admin")
        auth.create_user(self.hub.store, "kid", p3.PW, "family")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {}
        for u in ("dad", "kid"):
            r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": u, "password": p3.PW}).encode(),
                                       method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
            with self.opener.open(r) as resp:
                self.c[u] = resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def tearDown(self):
        self.hub.close()

    def req(self, method, path, user):
        r = urllib.request.Request(self.hub.url + path, method=method, data=b"{}" if method == "POST" else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_read_and_run(self):
        st, r = self.req("GET", "/api/brief", "kid")
        self.assertEqual((st, r["brief"]), (200, None))
        brief.save(self.hub.store, {"id": "1", "created_at": brief.now(), "groups": [], "near": []}, "2026-11-19")
        brief.save(self.hub.store, {"id": "2", "created_at": brief.now(), "groups": [], "near": []}, "2026-11-20")
        self.assertEqual(self.req("GET", "/api/brief", "kid")[1]["brief"]["day"], "2026-11-20")
        self.assertEqual(self.req("GET", "/api/brief?day=2026-11-19", "kid")[1]["brief"]["id"], "1")
        self.assertEqual(self.req("POST", "/api/brief/run", "kid")[0], 403)


if __name__ == "__main__":
    unittest.main()
