"""Audit log: the hash chain, hourly view rows, and what the server records (logins, changes, sensitive views, alerts)."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import audit, auth  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

DAD = {"id": 1, "username": "dad"}


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "a.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_chain_shows_edits_and_deletions(self):
        for n in range(4):
            audit.record(self.st, DAD, "change", "items", f"POST /api/items/{n}")
        self.assertEqual(audit.verify(self.st), {"ok": True, "rows": 4, "broken_at": None})
        ids = [r["id"] for r in audit.rows(self.st)][::-1]
        self.st._exec("UPDATE audit_log SET detail='nothing to see' WHERE id=?", (ids[1],))
        self.assertEqual(audit.verify(self.st)["broken_at"], ids[1])
        self.st._exec("UPDATE audit_log SET detail='POST /api/items/1' WHERE id=?", (ids[1],))
        self.assertTrue(audit.verify(self.st)["ok"])
        self.st._exec("DELETE FROM audit_log WHERE id=?", (ids[2],))
        self.assertEqual(audit.verify(self.st)["broken_at"], ids[3])

    def test_views_once_an_hour_and_prune(self):
        self.assertIsNotNone(audit.viewed(self.st, DAD, "medical"))
        self.assertIsNone(audit.viewed(self.st, DAD, "medical"))
        self.assertIsNotNone(audit.viewed(self.st, {"id": 2, "username": "mom"}, "medical"))
        self.st._exec("UPDATE audit_log SET at='2020-01-01T00:00:00+00:00'")
        self.assertIsNotNone(audit.viewed(self.st, DAD, "medical"))  # the old one is more than an hour ago
        audit.prune(self.st)
        self.assertEqual(len(audit.rows(self.st)), 1)
        self.assertTrue(audit.verify(self.st)["ok"])  # the chain starts again from the oldest kept row

    def test_nightly_tells_once(self):
        told = []
        audit.listeners.append(lambda ev, d: told.append(d["title"]))
        try:
            for n in range(3):
                audit.record(self.st, DAD, "change", "items", str(n))
            self.assertTrue(audit.nightly(self.st)["ok"])
            self.st._exec("UPDATE audit_log SET username='mom' WHERE id=2")
            audit.nightly(self.st)
            audit.nightly(self.st)
            self.assertEqual(told, ["The activity log was changed"])
        finally:
            audit.listeners.clear()


class AuditHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        self.st = self.hub.store
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.told = []
        audit.listeners.append(lambda ev, d: self.told.append(d["title"]))
        self.c = {u: self.login(u) for u in ("dad", "kid")}

    def tearDown(self):
        audit.listeners.clear()
        self.hub.close()

    def login(self, user, pw=p3.PW, cf=None):
        h = {"X-Bunkr": "1", "Content-Type": "application/json"}
        if cf:
            h["CF-Connecting-IP"] = cf
        r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": user, "password": pw}).encode(),
                                   method="POST", headers=h)
        try:
            with self.opener.open(r) as resp:
                return resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        except urllib.error.HTTPError as e:
            return e.code

    def req(self, method, path, body=None, user="dad"):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if resp.headers.get_content_type() == "application/json" else raw.decode()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def log(self):
        return audit.rows(self.st, 500)[::-1]

    def test_logins_and_lockout(self):
        self.assertEqual(self.login("kid", "wrong password!", cf="203.0.113.9"), 401)
        r = [x for x in self.log() if x["action"] == "login_failed"][-1]
        self.assertEqual((r["username"], r["ip"]), ("kid", "203.0.113.9 via 127.0.0.1"))
        for _ in range(4):
            self.login("kid", "wrong password!")
        self.assertEqual(self.told, ["A login was locked after failed attempts"])
        self.login("kid", "wrong password!")  # still locked: no second alert
        self.assertEqual(len(self.told), 1)
        self.assertEqual(self.login("nobody", "whatever12345"), 401)
        self.assertTrue(any(x["username"] == "nobody" and x["user_id"] is None for x in self.log()))
        self.assertEqual(sum(1 for x in self.log() if x["action"] == "login"), 2)
        self.assertFalse(any("wrong password" in x["detail"] for x in self.log()))  # what was typed is never kept

    def test_changes_and_sensitive_views(self):
        st, r = self.req("POST", "/api/items", {"name": "Rifle", "category": "firearms", "subtype": "rifle", "quantity": 1})
        gid = r["id"]
        self.req("POST", "/api/items", {"name": "Beans", "category": "food", "quantity": 3}, user="kid")
        self.req("DELETE", f"/api/items/{gid}")
        rows = [x for x in self.log() if x["action"] == "change"]
        self.assertEqual([(x["username"], x["what"]) for x in rows], [("dad", "firearms"), ("kid", "items"), ("dad", "firearms")])
        self.req("POST", "/api/items", {"name": "Pistol", "category": "firearms", "subtype": "handgun", "quantity": 1})
        for _ in range(3):
            self.req("GET", "/api/state")
            self.req("GET", "/api/state", user="kid")
        views = [x for x in self.log() if x["action"] == "view"]
        self.assertEqual([(x["username"], x["what"]) for x in views], [("dad", "firearms")])  # once an hour; kids never see them
        self.req("GET", "/api/report.html")
        self.req("GET", "/api/report.html")
        self.req("GET", "/api/medical-cards.html", user="kid")
        self.req("GET", "/api/medical-cards.html", user="kid")
        whats = [x["what"] for x in self.log() if x["action"] == "view"]
        self.assertEqual((whats.count("printout"), whats.count("medical")), (2, 1))

    def test_login_changes_alert_admins(self):
        st, r = self.req("POST", "/api/users", {"username": "mom", "password": p3.PW, "role": "admin"})
        kid = auth.user_by_name(self.st, "kid")["id"]
        self.req("POST", f"/api/users/{kid}", {"role": "admin"})
        self.req("POST", f"/api/users/{kid}", {"password": "a new password 123"})
        self.req("DELETE", f"/api/users/{r['id']}")
        self.assertEqual(self.told, ["A new admin login was made", "A login was made admin",
                                     "Someone's password was changed by an admin", "A login was deleted"])
        self.assertFalse(any("a new password" in x["detail"] for x in self.log()))

    def test_only_admins_read_it(self):
        self.req("GET", "/api/state")
        self.assertEqual(self.req("GET", "/api/audit", user="kid")[0], 403)
        self.assertEqual(self.req("GET", "/api/audit.csv", user="kid")[0], 403)
        st, r = self.req("GET", "/api/audit")
        self.assertEqual((st, r["check"]["ok"]), (200, True))
        self.assertIn("login", r["whats"])
        kid = auth.user_by_name(self.st, "kid")["id"]
        self.assertTrue(all(x["user_id"] == kid for x in self.req("GET", f"/api/audit?user={kid}")[1]["rows"]))
        st, csv = self.req("GET", "/api/audit.csv")
        self.assertTrue(csv.startswith("when (UTC),who,action,what,detail,from"))
        self.assertEqual(self.log()[-1]["what"], "export")  # reading the log is logged too
        denied = [x for x in self.log() if x["action"] == "denied"]
        self.assertEqual([(x["username"], x["what"]) for x in denied], [("kid", "audit"), ("kid", "audit.csv")])


if __name__ == "__main__":
    unittest.main()
