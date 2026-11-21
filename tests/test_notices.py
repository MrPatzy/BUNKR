"""The bell (notices kept in the app), reminders by email, document renewal dates, and battery on check-ins."""

import json
import os
import smtplib
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bunkr import auth, checkins, family, mailer, mesh, meshbot, notify, renewals  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

NIGHT = datetime(2026, 11, 21, 6, 0, tzinfo=timezone.utc)   # 23:00 in Denver: quiet hours
DAY = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.st = Store(self.dir / "t.db")
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "x", p3.PW, "family")
        rows = {r["username"]: r["id"] for r in self.st._rows("SELECT id, username FROM users")}
        self.admin, self.kid = rows["dad"], rows["x"]

    def tearDown(self):
        self.tmp.cleanup()

    def set_prefs(self, uid, **p):
        self.st._exec("UPDATE users SET push_prefs=? WHERE id=?", (json.dumps(notify.prefs(self.st, uid) | p), uid))


class InboxTests(Base):
    def test_quiet_hours_hold_the_push_not_the_inbox(self):
        data = {"user_id": None, "title": "Renew soon: Home insurance", "body": "due Dec 20", "tag": "doc-1", "url": "/#documents"}
        self.assertEqual(notify.plan(self.st, "reminder", data, at=NIGHT), [])  # no push at night
        notify.send(self.st, None, "reminder", data, at=NIGHT, push=False)
        got = notify.notices(self.st, self.admin)
        self.assertEqual((got["unread"], got["items"][0]["title"], got["items"][0]["url"]), (1, data["title"], "/#documents"))
        self.assertEqual(notify.unread(self.st, self.kid), 0)  # reminders with no owner go to admins

    def test_same_tag_updates_until_read(self):
        for body in ("15 min late", "40 min late"):
            notify.record(self.st, self.kid, {"title": "UA 1234", "body": body, "tag": "flight-x", "tier": "family"})
        items = notify.notices(self.st, self.kid)["items"]
        self.assertEqual([(i["body"]) for i in items], ["40 min late"])
        self.assertEqual(notify.mark_read(self.st, self.kid, [items[0]["id"]]), 0)
        notify.record(self.st, self.kid, {"title": "UA 1234", "body": "landed", "tag": "flight-x", "tier": "family"})
        self.assertEqual(len(notify.notices(self.st, self.kid)["items"]), 2)  # read ones are history, not overwritten
        notify.record(self.st, self.kid, {"title": "Other", "tag": ""})
        self.assertEqual(notify.mark_read(self.st, self.kid), 0)
        self.assertEqual(notify.mark_read(self.st, self.admin, [items[0]["id"]]), 0)  # someone else's: untouched
        for i in range(notify.INBOX_KEEP + 5):
            notify.record(self.st, self.kid, {"title": f"n{i}"})
        self.assertEqual(self.st._rows("SELECT COUNT(*) AS n FROM notices WHERE user_id=?", (self.kid,))[0]["n"], notify.INBOX_KEEP)

    def test_checkins_land_in_the_inbox_and_test_pushes_dont(self):
        uid = uuid.uuid4().hex
        self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at) VALUES('Jordan','adult',1,?,?)", (uid, checkins.now()))
        notify.send(self.st, None, "checkin", {"person_uid": uid, "person_name": "Jordan", "status": "help", "status_label": "Need help",
                                              "battery_low": 1, "battery": 9}, push=False)
        n = notify.notices(self.st, self.kid)["items"][0]
        self.assertEqual(n["title"], "Jordan: Need help")
        self.assertIn("low battery, may go quiet (9%)", n["body"])
        notify.send(self.st, None, "test", {"user_id": self.kid}, push=False)
        self.assertEqual(notify.unread(self.st, self.kid), 1)


class EmailChoiceTests(Base):
    def test_notification_email_or_both(self):
        data = {"user_id": self.admin, "title": "Renew soon: Passport", "body": "due Dec 20", "tag": "doc-2", "url": "/#documents"}
        mails = []
        mail = lambda to, msg: mails.append((to, msg["title"]))  # noqa: E731
        self.assertTrue(notify.wants(self.st, self.admin, "push"))
        self.assertFalse(notify.wants(self.st, self.admin, "email"))
        self.set_prefs(self.admin, reminders_by="email")  # email chosen but no address yet: still a push
        self.assertTrue(notify.wants(self.st, self.admin, "push"))
        notify.send(self.st, None, "reminder", data, push=False, mail=mail, at=DAY)
        self.assertEqual(mails, [])
        self.set_prefs(self.admin, reminders_by="email", email="dad@example.com")
        self.assertFalse(notify.wants(self.st, self.admin, "push"))
        notify.send(self.st, None, "reminder", data, push=False, mail=mail, at=DAY)
        self.assertEqual(mails, [("dad@example.com", "Renew soon: Passport")])
        self.set_prefs(self.admin, reminders_by="both")
        self.assertTrue(notify.wants(self.st, self.admin, "push") and notify.wants(self.st, self.admin, "email"))
        # only reminders go by email: a family call doesn't
        notify.send(self.st, None, "call", {"label": "Meet at", "place": "park"}, push=False, mail=mail)
        self.assertEqual(len(mails), 1)

    def test_prefs_check_the_address(self):
        self.assertEqual(notify.clean_prefs({"reminders_by": "both", "email": " a@b.co "}), {"reminders_by": "both", "email": "a@b.co"})
        self.assertEqual(notify.clean_prefs({"reminders_by": "fax"}), {})
        with self.assertRaises(ValueError):
            notify.clean_prefs({"email": "not an address"})


class FakeSMTP:
    def __init__(self, fail_on=()):
        self.sent, self.fail_on, self.quit_called = [], set(fail_on), False

    def send_message(self, m):
        if m["To"] in self.fail_on:
            raise smtplib.SMTPRecipientsRefused({m["To"]: (550, b"no")})
        self.sent.append((m["To"], m["Subject"], m.get_content()))

    def quit(self):
        self.quit_called = True


class MailerTests(Base):
    def test_settings_and_password_file(self):
        v = mailer.save_settings(self.st, self.dir, {"host": "smtp.gmail.com", "port": "587", "security": "starttls",
                                                     "username": "dad@example.com", "password": "app pass", "from": "dad@example.com"})
        self.assertTrue(v["ready"] and v["has_password"])
        self.assertNotIn("password", v)
        self.assertNotIn("app pass", json.dumps(v))
        self.assertNotIn("app pass", json.dumps(self.st.get_raw("mail")))  # the password isn't in the database
        mode = stat.S_IMODE(os.stat(self.dir / mailer.PASSWORD_FILE).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertTrue(mailer.save_settings(self.st, self.dir, {"host": "smtp.gmail.com"})["has_password"])  # empty keeps it
        self.assertFalse(mailer.save_settings(self.st, self.dir, {"forget_password": True})["has_password"])
        for bad in ({"host": "smtp gmail"}, {"port": "x"}, {"from": "nope"}):
            with self.assertRaises(ValueError):
                mailer.save_settings(self.st, self.dir, bad)

    def test_outbox_retries_until_it_goes(self):
        self.assertEqual(mailer.flush(self.st, self.dir), (0, 0))  # not set up: nothing happens
        mailer.save_settings(self.st, self.dir, {"host": "smtp.example.com", "from": "bunkr@example.com"})
        mailer.reminder(self.st, "dad@example.com", {"title": "Renew soon: Passport", "body": "due Dec 20", "url": "/#documents"},
                        "https://bunkr.example.org")

        def offline(s, folder):
            raise OSError("no route to host")
        self.assertEqual(mailer.flush(self.st, self.dir, connect=offline), (0, 1))
        self.assertIn("no route", mailer.view(self.st, self.dir)["last_error"])
        fake = FakeSMTP()
        self.assertEqual(mailer.flush(self.st, self.dir, connect=lambda s, f: fake), (1, 0))
        to, subject, body = fake.sent[0]
        self.assertEqual((to, subject), ("dad@example.com", "BUNKR: Renew soon: Passport"))
        self.assertIn("https://bunkr.example.org/#documents", body)
        self.assertTrue(fake.quit_called)
        self.assertEqual(mailer.view(self.st, self.dir)["waiting"], 0)
        mailer.queue(self.st, "bad@example.com", "x", "y")
        self.assertEqual(mailer.flush(self.st, self.dir, connect=lambda s, f: FakeSMTP({"bad@example.com"})), (0, 1))
        self.assertIn("refused", mailer.view(self.st, self.dir)["last_error"])


class RenewalTests(Base):
    def doc(self, renew_on, **kw):
        return self.st.save_document({"path": f"/tmp/doc-{uuid.uuid4().hex}", "label": kw.pop("label", "Home insurance"), "doc_type": "insurance",
                                      "priority": 1, "renew_on": renew_on, **kw})

    def test_status(self):
        t = date(2026, 11, 21)
        self.assertIsNone(renewals.status({"renew_on": ""}, t))
        self.assertEqual(renewals.status({"renew_on": "2027-03-01"}, t)["state"], "later")
        self.assertEqual(renewals.status({"renew_on": "2026-12-21"}, t)["state"], "soon")      # 30 days out
        self.assertEqual(renewals.status({"renew_on": "2026-11-21"}, t)["state"], "soon")      # due today
        self.assertEqual(renewals.status({"renew_on": "2026-11-20"}, t)["state"], "overdue")
        self.assertEqual(renewals.status({"renew_on": "2026-11-20", "renewed_at": "2026-11-01"}, t)["state"], "done")
        self.assertEqual(renewals.status({"renew_on": "2026-11-20", "renewed_at": "2025-11-01"}, t)["state"], "overdue")  # last year's
        with self.assertRaises(ValueError):
            renewals.clean_date("next week")

    def test_reminders_once_then_weekly_until_done(self):
        heard = []
        renewals.listeners[:] = [lambda ev, d: heard.append((ev, d["title"]))]
        try:
            did = self.doc("2026-12-15", insurer="State Farm", claim_phone="800-555-0100")
            self.doc("2027-06-01", label="Later one")
            self.assertEqual(len(renewals.remind(self.st, date(2026, 11, 14))), 0)          # 31 days out
            self.assertEqual(len(renewals.remind(self.st, date(2026, 11, 15))), 1)          # 30 days out
            self.assertEqual(len(renewals.remind(self.st, date(2026, 11, 16))), 0)          # said once
            msg = renewals.message(renewals.due(self.st, date(2026, 11, 16))[0])
            self.assertIn("State Farm", msg["body"])
            self.assertIn("claims 800-555-0100", msg["body"])
            due = date(2026, 12, 15)
            overdue = [n for n in range(0, 24) if renewals.remind(self.st, due + timedelta(days=n))]
            self.assertEqual(overdue, [1, 8, 15, 22])                                       # the day after, then weekly
            self.assertTrue(heard[-1][1].startswith("Overdue: renew Home insurance"))
            cal = renewals.calendar_entries(self.st, date(2027, 1, 7))
            self.assertEqual([(c["kind"], c["id"], c["overdue"]) for c in cal], [("renew", did, True)])
            renewals.mark_done(self.st, did, date(2027, 1, 7))
            self.assertEqual(renewals.calendar_entries(self.st, date(2027, 1, 7)), [])
            self.assertEqual(renewals.remind(self.st, date(2027, 1, 14)), [])
            # next year's date: reminders start again
            self.st.save_document({"renew_on": "2027-12-15"}, did)
            self.assertEqual([d["id"] for d in renewals.remind(self.st, date(2027, 11, 20))
                              if d["renewal"]["state"] == "soon"], [did])
        finally:
            renewals.listeners.clear()

    def test_calendar_and_archive(self):
        self.doc("2026-11-25", coverage="Home, $400k <dwelling>")
        tasks = []
        cal = family.calendar(tasks, [], [], today=date(2026, 11, 21), renewals=renewals.calendar_entries(self.st, date(2026, 11, 21)))
        self.assertEqual(cal[0]["title"], "Renew: Home insurance")
        out = renewals.details_html(self.st.documents())
        self.assertIn("2026-11-25", out)
        self.assertIn("&lt;dwelling&gt;", out)
        self.assertEqual(renewals.details_html([{"label": "x", "renew_on": "", "insurer": "", "coverage": "", "claim_phone": ""}]), "")


class BatteryTests(Base):
    def setUp(self):
        super().setUp()
        self.uid = uuid.uuid4().hex
        self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at, mesh_name) VALUES('Jordan','adult',1,?,?,'Ty')",
                      (self.uid, checkins.now()))

    def test_levels(self):
        self.assertEqual(checkins.battery(54), (54, 0))
        self.assertEqual(checkins.battery("15"), (15, 1))
        self.assertEqual(checkins.battery(None, True), (None, 1))
        self.assertEqual(checkins.battery(140), (None, 0))
        self.assertEqual(checkins.battery("x"), (None, 0))

    def test_stored_shown_and_synced(self):
        checkins.add_checkin(self.st, self.uid, "safe", battery_level=12)
        last = [p for p in checkins.board(self.st)["people"] if p["uid"] == self.uid][0]["last"]
        self.assertEqual((last["battery"], last["battery_low"]), (12, 1))
        self.assertIn("Jordan", meshbot.who(self.st))
        self.assertIn("lowbatt", meshbot.who(self.st))
        exp = checkins.export(self.st)["checkins"][0]
        self.assertEqual((exp["battery"], exp["battery_low"]), (12, 1))
        other = Store(self.dir / "pi.db")
        old = {k: v for k, v in exp.items() if k not in ("battery", "battery_low")} | {"uid": uuid.uuid4().hex}  # an older node
        self.assertEqual(checkins.apply(other, {"checkins": [exp, old]}), 2)
        got = {r["uid"]: (r["battery"], r["battery_low"]) for r in other._rows("SELECT * FROM checkins")}
        self.assertEqual(got, {exp["uid"]: (12, 1), old["uid"]: (None, 0)})

    def test_over_the_mesh(self):
        s = mesh.save_settings(self.st, {"connect": "/dev/ttyUSB0"})
        r = mesh.handle(self.st, {"kind": "channel", "channel": 1, "text": "Ty: SAFE low at the cabin"}, s)
        self.assertEqual(r, "Got it: Jordan is safe. Low battery noted.")
        c = checkins.recent(self.st, 1)[0]
        self.assertEqual((c["note"], c["battery_low"], c["via"]), ("at the cabin", 1, "mesh"))
        mesh.handle(self.st, {"kind": "channel", "channel": 1, "text": "Ty: OMW 64%"}, s)
        c = self.st._rows("SELECT * FROM checkins ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((c["status"], c["battery"], c["battery_low"], c["note"]), ("moving", 64, 0, ""))
        self.assertEqual(mesh.interpret("SAFE lower deck")["battery_low"], False)
        self.assertEqual(mesh.interpret("SAFE 150%")["note"], "150%")


class HttpTests(unittest.TestCase):
    def setUp(self):
        from bunkr import server
        self.addCleanup(setattr, server, "notifier", server.notifier)
        server.notifier = None  # another test's notifier may point at a folder that's gone
        self.hub = p3.Hub()
        self.st = self.hub.store
        auth.create_user(self.st, "dad", p3.PW, "admin")
        auth.create_user(self.st, "kid", p3.PW, "family")
        self.ids = {r["username"]: r["id"] for r in self.st._rows("SELECT id, username FROM users")}
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

    def test_bell(self):
        notify.record(self.st, self.ids["kid"], {"title": "Jordan: I'm safe", "url": "/#home"})
        notify.record(self.st, self.ids["kid"], {"title": "Morning brief", "url": "/#brief", "tag": "brief"})
        self.assertEqual(self.req("GET", "/api/state", user="kid")[1]["unread"], 2)
        code, r = self.req("GET", "/api/notices", user="kid")
        self.assertEqual((code, r["unread"], [i["title"] for i in r["items"]]), (200, 2, ["Morning brief", "Jordan: I'm safe"]))
        self.assertEqual(self.req("GET", "/api/notices")[1]["items"], [])                    # each person's own
        self.assertEqual(self.req("POST", "/api/notices/read", {"ids": [r["items"][0]["id"]]}, user="kid")[1]["unread"], 2 - 1)
        self.assertEqual(self.req("POST", "/api/notices/read", {"ids": "all"}, user="kid")[0], 400)
        self.assertEqual(self.req("POST", "/api/notices/read", {}, user="kid")[1]["unread"], 0)

    def test_documents_and_mail_settings(self):
        path = self.hub.dir / "policy.pdf"
        path.write_text("x")
        code, r = self.req("POST", "/api/documents", {"path": str(path), "label": "Home insurance", "doc_type": "insurance",
                                                      "renew_on": "2026-12-01", "insurer": "State Farm", "claim_phone": "800"})
        self.assertEqual(code, 200)
        did = r["id"]
        self.assertEqual(self.req("POST", "/api/documents", {"path": str(path), "renew_on": "soon"})[0], 400)
        docs = self.req("GET", "/api/documents")[1]
        self.assertEqual((docs[0]["insurer"], docs[0]["renewal"]["due"]), ("State Farm", "2026-12-01"))
        self.assertEqual(self.req("POST", f"/api/documents/{did}/renewed", user="kid")[0], 403)
        self.assertEqual(self.req("POST", f"/api/documents/{did}/renewed")[0], 200)
        self.assertEqual(self.req("POST", "/api/documents/999/renewed")[0], 404)
        self.assertTrue(self.st.documents()[0]["renewed_at"])
        # mail settings: admins only, and the password never comes back
        self.assertEqual(self.req("GET", "/api/mail", user="kid")[0], 403)
        code, v = self.req("POST", "/api/mail", {"host": "smtp.example.com", "from": "b@example.com", "password": "s3cret-app"})
        self.assertEqual((code, v["has_password"]), (200, True))
        self.assertNotIn("s3cret-app", json.dumps(self.req("GET", "/api/mail")[1]))
        self.assertEqual(self.req("POST", "/api/mail/test", {"to": "nope"})[0], 400)
        # each person's choice
        code, p = self.req("POST", "/api/push/prefs", {"reminders_by": "both", "email": "kid@example.com"}, user="kid")
        self.assertEqual((code, p["reminders_by"], p["email"]), (200, "both", "kid@example.com"))
        self.assertEqual(self.req("POST", "/api/push/prefs", {"email": "nope"}, user="kid")[0], 400)
        self.assertTrue(self.req("GET", "/api/push/key", user="kid")[1]["email_ready"])

    def test_checkin_with_battery(self):
        uid = uuid.uuid4().hex
        self.st._exec("INSERT INTO people(name, role, remote, uid, updated_at, user_id) VALUES('Kid','child',0,?,?,?)",
                      (uid, checkins.now(), self.ids["kid"]))
        self.assertEqual(self.req("POST", "/api/checkin", {"status": "safe", "battery": 18}, user="kid")[0], 200)
        self.assertEqual(self.req("POST", "/api/checkin", {"status": "safe", "person_uid": uid, "battery": 90}, user="dad")[0], 200)
        rows = self.st._rows("SELECT battery, battery_low, by_name FROM checkins ORDER BY id")
        self.assertEqual([(r["battery"], r["battery_low"]) for r in rows], [(18, 1), (None, 0)])  # dad's phone isn't the kid's


if __name__ == "__main__":
    unittest.main()
