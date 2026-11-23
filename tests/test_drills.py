"""Drills (practice family calls with a timing report) and the nightly backup + restore test."""

import json
import os
import sqlite3
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

from bunkr import auth, checkins, dbcheck, field, notify, retention  # noqa: E402
from bunkr.db import SCHEMA_VERSION, Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

T0 = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
iso = lambda m: (T0 + timedelta(minutes=m)).isoformat(timespec="seconds")


class DrillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        st = self.store = Store(Path(self.tmp.name) / "d.db")
        self.dad = auth.create_user(st, "dad", p3.PW, "admin")
        self.mom = auth.create_user(st, "mom", p3.PW, "family")
        self.uid = {}
        for name, user, remote in (("Alex", self.dad, 0), ("Wife", self.mom, 0), ("Kid", None, 0), ("Grandma", None, 1)):
            self.uid[name] = uuid.uuid4().hex
            st._exec("INSERT INTO people(name, role, uid, user_id, remote) VALUES(?, 'adult', ?, ?, ?)", (name, self.uid[name], user, remote))
        for u in (self.dad, self.mom):
            st._exec("UPDATE users SET push_prefs=? WHERE id=?", (json.dumps({"tz": "UTC", "quiet_start": "22:00", "quiet_end": "06:30"}), u))

    def tearDown(self):
        checkins.listeners[:] = []
        self.tmp.cleanup()

    def call_at(self, minute, drill=True):
        c = checkins.start_call(self.store, "meet", "dad", place="Park", drill=drill)
        self.store._exec("UPDATE family_calls SET created_at=? WHERE uid=?", (iso(minute), c["uid"]))
        return c

    def test_report_times_everyone(self):
        checkins.add_checkin(self.store, self.uid["Kid"], "safe", at=iso(-30))  # before the drill: doesn't count
        c = self.call_at(0)
        self.assertTrue(checkins.active_call(self.store)["drill"])
        checkins.add_checkin(self.store, self.uid["Wife"], "safe", at=iso(2))
        checkins.add_checkin(self.store, self.uid["Wife"], "moving", at=iso(5))  # only the first answer counts
        checkins.add_checkin(self.store, self.uid["Grandma"], "safe", via="field", by_name="dad", at=iso(12.5))
        checkins.end_call(self.store, "dad")
        self.store._exec("UPDATE family_calls SET ended_at=? WHERE uid=?", (iso(20), c["uid"]))
        checkins.add_checkin(self.store, self.uid["Kid"], "safe", at=iso(25))  # after it ended: too late
        r = checkins.report(self.store, c["uid"])
        self.assertEqual((r["answered"], r["total"], r["slowest"], r["lasted"], r["drill"]), (2, 4, 12.5, 20.0, True))
        self.assertEqual(sorted(r["missing"]), ["Alex", "Kid"])
        wife = next(p for p in r["people"] if p["name"] == "Wife")
        self.assertEqual((wife["minutes"], wife["status"]), (2.0, "safe"))
        self.assertEqual(r["people"][0]["name"], "Wife")  # fastest first
        self.assertEqual([h["uid"] for h in checkins.history(self.store)], [c["uid"]])

    def test_drills_never_wake_anyone(self):
        at = lambda hour: datetime(2026, 11, 20, hour, 0, tzinfo=timezone.utc)
        users = lambda ev, d, hour: sorted(u for u, _ in notify.plan(self.store, ev, d, at(hour)))
        both = sorted([self.dad, self.mom])
        drill = {"label": "Meet at", "place": "Park", "drill": True}
        self.assertEqual(users("call", drill, 3), [])
        msg = notify.plan(self.store, "call", drill, at(12))[0][1]
        self.assertTrue(msg["title"].startswith("DRILL"))
        self.assertEqual(msg["urgency"], "normal")
        self.assertEqual(users("call", dict(drill, drill=False), 3), both)  # a real call always gets through
        self.call_at(0)
        ck = {"person_uid": self.uid["Wife"], "person_name": "Wife", "status": "safe", "status_label": "I'm safe"}
        self.assertEqual(users("checkin", ck, 3), [])            # answers to a drill wait for morning
        self.assertEqual(users("checkin", ck, 12), [self.dad])
        self.assertEqual(users("checkin", dict(ck, status="help", status_label="Need help"), 3), [self.dad])  # help always
        self.assertEqual(users("all_clear", {"label": "Meet at", "drill": True}, 3), [])
        self.assertEqual(notify.plan(self.store, "all_clear", {"label": "x", "drill": True}, at(12))[0][1]["title"], "Drill over")

    def test_drill_flag_syncs_and_shows_on_the_pi(self):
        c = self.call_at(0)
        node = Store(Path(self.tmp.name) / "node.db")
        checkins.apply(node, checkins.export(self.store, "", include_people=True), from_hub=True)
        self.assertTrue(checkins.active_call(node)["drill"])
        old = checkins.export(self.store, "")
        for x in old["calls"]:
            x.pop("drill")
            x["uid"] = "old" + x["uid"][3:]
            x["created_at"] = iso(1)
        other = Store(Path(self.tmp.name) / "other.db")
        checkins.apply(other, old)
        self.assertFalse(checkins.active_call(other)["drill"])  # an older node's call is a real one
        page = field.family_html(checkins.board(self.store))
        self.assertIn("DRILL", page)
        self.assertTrue(c)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bunkr.db")
        self.store._exec("INSERT INTO people(name, role, uid) VALUES('Alex', 'adult', 'u1')")

    def tearDown(self):
        self.tmp.cleanup()

    def test_nightly_copy_and_pruning(self):
        d = dbcheck.folder(self.store)
        for n in range(1, 18):
            dbcheck.nightly_copy(self.store, when=datetime(2026, 10, n))
        retention.prune_backups(self.store, d, now=datetime(2026, 10, 18))  # what the nightly job does after the copy
        names = sorted(p.name for p in d.glob("bunkr-nightly-*.db"))
        self.assertEqual(len(names), 14 + 1)  # the newest 14, and the month's first copy
        self.assertEqual(names[0], "bunkr-nightly-20261001.db")
        self.assertEqual(names[-1], "bunkr-nightly-20261017.db")
        (d / "bunkr-before-deploy-x.db").write_bytes(b"")  # copies it can't classify are never pruned
        dbcheck.nightly_copy(self.store, when=datetime(2026, 10, 18))
        retention.prune_backups(self.store, d, now=datetime(2026, 10, 18))
        self.assertTrue((d / "bunkr-before-deploy-x.db").exists())
        self.assertEqual(stat.S_IMODE(os.stat(d / names[-1]).st_mode), 0o640)
        c = sqlite3.connect(d / names[-1])
        self.assertEqual(c.execute("SELECT name FROM people").fetchone()[0], "Alex")
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_restore_test_passes_old_schema_and_fails_damaged(self):
        r = dbcheck.restore_test(self.store)
        self.assertEqual((r["ok"], r["problem"]), (False, "there is no backup to test yet"))
        d = dbcheck.folder(self.store)
        d.mkdir()
        old = sqlite3.connect(d / "bunkr-before-schema10.db")  # a backup from an older schema upgrades on restore
        old.executescript("CREATE TABLE people(id INTEGER PRIMARY KEY, name TEXT);")
        old.close()
        backup = dbcheck.nightly_copy(self.store)
        r = dbcheck.restore_test(self.store, backup)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["schema_to"], r["rows"]["people"]), (SCHEMA_VERSION, 1))
        self.assertEqual(self.store.get_raw("restore_test")["ok"], True)
        bad = d / "bunkr-broken.db"
        bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
        r = dbcheck.restore_test(self.store, bad)
        self.assertFalse(r["ok"])
        self.assertTrue(r["problem"])
        self.assertFalse(list(Path(self.tmp.name).glob(".restore-test-*")))  # cleaned up

    def test_status_and_schedule(self):
        s = dbcheck.status(self.store)
        self.assertEqual(s["problems"], ["No nightly backup yet."])
        self.assertTrue(dbcheck.due(self.store))
        dbcheck.nightly(self.store)
        s = dbcheck.status(self.store)
        self.assertTrue(s["ok"], s)
        self.assertFalse(dbcheck.due(self.store))
        self.assertTrue(dbcheck.due(self.store, at=datetime.now(timezone.utc) + timedelta(days=31)))
        later = dbcheck.status(self.store, at=datetime.now(timezone.utc) + timedelta(days=3))
        self.assertIn("The nightly backup hasn't run for over a day.", later["problems"])
        self.store.set_raw("restore_test", {"at": datetime.now(timezone.utc).isoformat(), "ok": False, "problem": "boom"})
        self.assertTrue(dbcheck.due(self.store, at=datetime.now(timezone.utc) + timedelta(days=1)))  # a failure retries daily
        self.assertIn("The restore test failed: boom.", dbcheck.status(self.store)["problems"])


class DrillHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        st = self.hub.store
        auth.create_user(st, "dad", p3.PW, "admin")
        kid = auth.create_user(st, "kid", p3.PW, "family")
        st._exec("INSERT INTO people(name, role, uid, user_id) VALUES('Kid', 'child', 'k1', ?)", (kid,))
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {}
        for u in ("dad", "kid"):
            r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": u, "password": p3.PW}).encode(),
                                       method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
            with self.opener.open(r) as resp:
                self.c[u] = resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def tearDown(self):
        self.hub.close()

    def req(self, method, path, body=None, user="dad"):
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"X-Bunkr": "1", "Content-Type": "application/json", "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_drill_round_trip_and_backup_check(self):
        st, c = self.req("POST", "/api/calls", {"kind": "checkin", "drill": True})
        self.assertEqual((st, c["drill"]), (200, 1))
        self.assertTrue(self.req("GET", "/api/state", user="kid")[1]["board"]["call"]["drill"])
        self.req("POST", "/api/checkin", {"person_uid": "k1", "status": "safe"}, user="kid")
        st, r = self.req("POST", "/api/calls/end", {})
        self.assertEqual((r["report"]["answered"], r["report"]["total"]), (1, 1))
        s = self.req("GET", "/api/state", user="kid")[1]
        self.assertTrue(s["last_call"]["drill"])
        self.assertIsNone(s["backup_status"])  # admins only
        self.assertEqual(len(self.req("GET", "/api/calls", user="kid")[1]["calls"]), 1)
        self.assertEqual(self.req("POST", "/api/backups-check", {}, user="kid")[0], 403)
        st, r = self.req("POST", "/api/backups-check", {})
        self.assertEqual((st, r["ok"]), (200, True))
        self.assertTrue(self.req("GET", "/api/state")[1]["backup_status"]["ok"])


if __name__ == "__main__":
    unittest.main()
