"""One brief per person: "Done with this", "Not for me" in the brief and on For You, and the mesh's chosen brief."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import brief, mesh, news  # noqa: E402
from bunkr.db import Store  # noqa: E402


def src(n):
    return [{"outlet": "bbc", "url": f"https://x.example/{n}/{i}", "title": f"t{n}{i}"} for i in range(2)]


def story(title, n):
    return {"title": title, "short": title[:10], "agree": "", "differ": [], "unconfirmed": "", "matters": "", "sources": src(n)}


REC = {"id": "1", "created_at": "2026-11-24T07:00:00+00:00", "near": [], "world_routine": [], "outlets": {}, "sources": {},
       "groups": [{"headline": "Thanksgiving travel", "regions": ["a", "b"], "outlets": 2,
                   "items": [{"outlet": "bbc", "title": "t", "url": u["url"], "at": ""} for u in src("tg")]}],
       "ai": {"lead": "L", "stories": [story("Thanksgiving travel", "tg"), story("Storm", "storm")]}}


class Personal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "b.db")
        brief.save(self.store, dict(REC), "2026-11-24")

    def titles(self, uid):
        return [s["title"] for s in brief.personalise(self.store, brief.latest(self.store), uid)["ai"]["stories"]]

    def add_story(self, sid, urls):
        self.store._exec("INSERT INTO news_stories(id, headline, first_at, last_at, area, topic) VALUES(?,?,?,?,?,?)",
                         (sid, "h", "2026-11-24", "2026-11-24", "us", "other"))
        for u in urls:
            self.store._exec("INSERT INTO news_items(url, outlet, title, at, fetched_at, story) VALUES(?,?,?,?,?,?)",
                             (u, "bbc", "t", "2026-11-24", "2026-11-24", sid))

    def test_done_hides_for_that_person_only(self):
        brief.finish(self.store, 1, brief.latest(self.store), "ai", 0, teach=False)
        self.assertEqual(self.titles(1), ["Storm"])
        self.assertEqual(self.titles(2), ["Thanksgiving travel", "Storm"])
        self.assertEqual(brief.personalise(self.store, brief.latest(self.store), 1)["groups"], [])

    def test_kept_stories_know_their_stored_place(self):
        brief.finish(self.store, 1, brief.latest(self.store), "ai", 0, teach=False)
        self.assertEqual(brief.personalise(self.store, brief.latest(self.store), 1)["ai"]["stories"][0]["index"], 1)

    def test_done_does_not_teach_and_a_new_story_still_shows(self):
        brief.finish(self.store, 1, brief.latest(self.store), "ai", 0, teach=False)
        self.assertEqual(self.store._rows("SELECT * FROM news_marks"), [])
        new = dict(REC, ai={"lead": "L", "stories": [story("Thanksgiving travel", "tg2")]})
        self.assertEqual(len(brief.personalise(self.store, new, 1)["ai"]["stories"]), 1)

    def test_not_for_me_in_the_brief_trains_for_you(self):
        self.add_story("s1", ["https://x.example/tg/0", "https://x.example/tg/1"])
        brief.finish(self.store, 1, brief.latest(self.store), "ai", 0, teach=True)
        self.assertEqual(self.store._rows("SELECT user_id, story, rating FROM news_marks"), [{"user_id": 1, "story": "s1", "rating": -1}])
        self.assertEqual(news.learned(self.store, 1)["topic"], {"other": -1})

    def test_not_for_me_on_for_you_hides_in_the_brief(self):
        self.add_story("s2", ["https://x.example/storm/0", "https://x.example/storm/1"])
        news.rate(self.store, 1, "s2", -1)
        self.assertEqual(self.titles(1), ["Thanksgiving travel"])
        self.assertEqual(self.titles(2), ["Thanksgiving travel", "Storm"])

    def test_bad_story_is_refused(self):
        with self.assertRaises(KeyError):
            brief.finish(self.store, 1, brief.latest(self.store), "ai", 9, teach=False)

    def test_mesh_source_defaults_to_household_and_is_checked(self):
        self.assertEqual(mesh.settings(self.store)["brief_user"], 0)
        with self.assertRaises(ValueError):
            mesh.save_settings(self.store, {"brief_user": 99})
        self.store._exec("INSERT INTO users(id, username, password_hash) VALUES(5, 'alex', 'x')")
        mesh.save_settings(self.store, {"brief_user": 5})
        self.assertEqual(mesh.settings(self.store)["brief_user"], 5)
        mesh.save_settings(self.store, {"brief_user": 0})
        self.assertEqual(mesh.settings(self.store)["brief_user"], 0)


if __name__ == "__main__":
    unittest.main()
