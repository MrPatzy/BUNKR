"""Telling admins a newer BUNKR is out: version numbers, one note per version, skipping, and turning it off."""

import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import __version__, auth, selfupdate  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHub:
    def __init__(self, release=None, fail=False):
        self.release, self.fail, self.asked = release, fail, []

    def open(self, req, timeout=None):
        self.asked.append((req.full_url, dict(req.header_items())))
        if self.fail:
            raise urllib.error.URLError("offline")
        return Resp(json.dumps(self.release).encode())


def release(tag, **kw):
    return {"tag_name": tag, "name": f"BUNKR {tag}", "body": "- Faster news\n- Fixes", "html_url": f"https://github.com/o/r/releases/{tag}",
            "published_at": "2026-12-01T10:00:00Z", "draft": False, "prerelease": False, **kw}


def bump(v, i):
    p = [int(x) for x in v.split(".")]
    p[i] += 1
    return ".".join(map(str, p[:i + 1] + [0] * (2 - i)))


class VersionTests(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(selfupdate.newer("v1.2.10", "1.2.9"))
        self.assertTrue(selfupdate.newer("2.0", "1.9.9"))
        self.assertFalse(selfupdate.newer("1.0.0", "1.0.0"))
        self.assertTrue(selfupdate.newer("1.1.0", "1.1.0-beta.2"))     # the release is after its betas
        self.assertFalse(selfupdate.newer("nonsense", "1.0.0"))
        self.assertIsNotNone(selfupdate.parse(__version__))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "v.db")
        self.heard = []
        selfupdate.listeners[:] = [lambda ev, d: self.heard.append((ev, d))]
        selfupdate.save_settings(self.st, {"repo": "someone/bunkr"})

    def tearDown(self):
        selfupdate.listeners.clear()
        self.tmp.cleanup()

    def test_newer_once_then_skip(self):
        new = bump(__version__, 1)
        gh = FakeGitHub(release("v" + new))
        v = selfupdate.check(self.st, opener=gh)
        self.assertEqual(gh.asked[0][0], "https://api.github.com/repos/someone/bunkr/releases/latest")
        self.assertNotIn("Cookie", gh.asked[0][1])
        self.assertTrue(v["available"])
        self.assertEqual((v["latest"]["version"], v["latest"]["notes"]), (new, "- Faster news\n- Fixes"))
        self.assertEqual([(e, d["title"], d["user_id"], d["url"]) for e, d in self.heard],
                         [("reminder", f"BUNKR {new} is available", None, "/#household")])  # to admins
        selfupdate.check(self.st, opener=gh)
        self.assertEqual(len(self.heard), 1)                     # said once
        v = selfupdate.save_settings(self.st, {"skip": new})
        self.assertTrue(v["skipped"])
        newer_still = bump(new, 2)
        selfupdate.check(self.st, opener=FakeGitHub(release(newer_still)))
        self.assertEqual(self.heard[-1][1]["title"], f"BUNKR {newer_still} is available")   # a later one still tells

    def test_same_old_drafts_and_skipped_say_nothing(self):
        for tag in (__version__, "0.0.1", "not-a-version"):
            selfupdate.check(self.st, opener=FakeGitHub(release(tag)))
        selfupdate.check(self.st, opener=FakeGitHub(release("v" + bump(__version__, 0), prerelease=True)))
        self.assertEqual(self.heard, [])
        self.assertFalse(selfupdate.view(self.st)["available"])
        selfupdate.save_settings(self.st, {"skip": bump(__version__, 1)})
        selfupdate.check(self.st, opener=FakeGitHub(release(bump(__version__, 1))))
        self.assertEqual(self.heard, [])                          # skipped before it was even seen

    def test_off_offline_and_settings(self):
        selfupdate.save_settings(self.st, {"enabled": False})
        gh = FakeGitHub(release("v99.0.0"))
        selfupdate.check(self.st, opener=gh)
        self.assertEqual(gh.asked, [])                            # off means no request at all
        selfupdate.save_settings(self.st, {"enabled": True})
        v = selfupdate.check(self.st, opener=FakeGitHub(fail=True))
        self.assertIn("offline", v["error"])
        self.assertEqual(selfupdate.save_settings(self.st, {"repo": "https://github.com/a/b/"})["repo"], "a/b")
        for bad in ({"repo": "not a repo"}, {"skip": "soon"}):
            with self.assertRaises(ValueError):
                selfupdate.save_settings(self.st, bad)
        selfupdate.save_settings(self.st, {"repo": ""})
        gh = FakeGitHub(release("v99.0.0"))
        old = selfupdate.REPO
        selfupdate.REPO = ""
        try:
            selfupdate.check(self.st, opener=gh)
        finally:
            selfupdate.REPO = old
        self.assertEqual(gh.asked, [])                            # no project address, no check


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
        self.assertEqual(self.req("GET", "/api/version", user="kid")[1], {"current": __version__})
        self.assertEqual(self.req("POST", "/api/version/settings", {"enabled": False}, user="kid")[0], 403)
        code, v = self.req("POST", "/api/version/settings", {"enabled": False, "repo": "someone/bunkr"})
        self.assertEqual((code, v["enabled"], v["current"]), (200, False, __version__))
        self.assertEqual(self.req("POST", "/api/version/check")[1]["checked_at"], "")   # off: nothing fetched
        self.assertEqual(self.req("GET", "/api/state")[1]["version"], __version__)


if __name__ == "__main__":
    unittest.main()
