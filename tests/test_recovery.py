"""Recovery: home inventory for insurance, the financial access sheet, the food plan and the kids' plan.

Serial numbers, receipts and the financial sheet are admin-only and never leave the hub except inside the
encrypted document archive: not by sync, not to the AI connector, not into a phone's offline copy."""

import http.client
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import audit, auth, brief, builder, checkins, mcp_tools, recovery, server, situation, sync  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 200
PDF = b"%PDF-1.4\n" + b"\0" * 200
SERIAL = "SN-ZX81-77Q"
ACCOUNT = "Bank of Tomorrow"
NOTE = "claim line opens 7am"

FOOD = {"category": "food", "pack_size": 1, "pack_unit": "", "essential": "", "counts_home": 1, "location": ""}


def food(id_, name, qty, kcal, expires="", unit="cans"):
    return {**FOOD, "id": id_, "name": name, "quantity": qty, "per_unit": kcal, "expires": expires, "unit": unit}


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "r.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_rooms_items_files(self):
        st = self.store
        room = recovery.save_room(st, {"name": "Living room"})
        with self.assertRaises(ValueError):
            recovery.save_room(st, {"name": "living room"})
        with self.assertRaises(ValueError):
            recovery.save_room(st, {"name": " "})
        tv = recovery.save_item(st, {"room_id": room, "name": "TV", "make_model": "Sony X90", "serial": SERIAL,
                                     "purchased": "2024-03-01", "price_paid": "$1,200.50", "replacement_value": 900}, admin=True)
        recovery.save_item(st, {"name": "Couch", "replacement_value": "1500"}, admin=True)
        with self.assertRaises(ValueError):
            recovery.save_item(st, {"name": "x", "price_paid": "lots"}, admin=True)
        with self.assertRaises(ValueError):
            recovery.save_item(st, {"name": "x", "purchased": "March"}, admin=True)
        with self.assertRaises(ValueError):
            recovery.save_item(st, {"name": "x", "room_id": 999}, admin=True)
        # a family edit can't wipe or change the serial number
        recovery.save_item(st, {"room_id": room, "name": "TV", "serial": "", "price_paid": 1200.5}, admin=False, row_id=tv)
        self.assertEqual(recovery.items(st, True)[1]["serial"], SERIAL)

        photo = recovery.add_file(st, tv, "photo", "../../tv.jpg", JPEG)
        receipt = recovery.add_file(st, tv, "receipt", "receipt.pdf", PDF)
        with self.assertRaises(ValueError):
            recovery.add_file(st, tv, "photo", "r.pdf", PDF)       # a photo must be an image
        with self.assertRaises(ValueError):
            recovery.add_file(st, tv, "photo", "x.svg", b"<svg onload=alert(1)>")
        with self.assertRaises(ValueError):
            recovery.add_file(st, tv, "photo", "big.jpg", JPEG + b"\0" * recovery.MAX_FILE)
        with self.assertRaises(ValueError):
            recovery.add_file(st, tv, "other", "a.jpg", JPEG)
        stored = [recovery.file_path(st, recovery.file_row(st, i)) for i in (photo, receipt)]
        self.assertTrue(all(p.is_file() and p.parent == Path(self.tmp.name) / "recovery-files" for p in stored))
        self.assertEqual(recovery.file_row(st, photo)["name"], "tv.jpg")
        self.assertEqual(stored[0].read_bytes(), JPEG)  # kept as a file, not in the database
        self.assertFalse(st._rows("SELECT 1 FROM sqlite_master WHERE sql LIKE '%BLOB%' AND name LIKE 'recovery%'"))

        fam = {r["name"]: r for r in recovery.items(st, False)}
        self.assertNotIn("serial", fam["TV"])
        self.assertEqual([f["kind"] for f in fam["TV"]["files"]], ["photo"])
        v = recovery.view(st, True)
        self.assertEqual(v["totals"], {"items": 2, "price_paid": 1200.5, "replacement_value": 2400.0})
        groups = {g["room"]: g["totals"]["replacement_value"] for g in recovery.by_room(v["items"])}
        self.assertEqual(groups, {"No room": 1500.0, "Living room": 900.0})

        recovery.delete_room(st, room)  # its items stay, with no room
        self.assertEqual({r["room"] for r in recovery.items(st, True)}, {""})
        recovery.delete_item(st, tv)
        self.assertFalse(any(p.exists() for p in stored))
        self.assertFalse(st._rows("SELECT * FROM recovery_files"))

    def test_last4(self):
        self.assertEqual(recovery.clean_last4("1234"), "1234")
        self.assertEqual(recovery.clean_last4("**** 0042"), "0042")
        self.assertEqual(recovery.clean_last4("x-77"), "77")
        self.assertEqual(recovery.clean_last4(""), "")
        for bad in ("12345", "4111 1111 1111 1111", "123456789", "12a4", "one"):
            with self.assertRaises(ValueError, msg=bad):
                recovery.clean_last4(bad)
        with self.assertRaises(ValueError):
            recovery.save_finance(self.store, {"institution": ACCOUNT, "last4": "4111111111111111"})
        with self.assertRaises(ValueError):
            recovery.save_finance(self.store, {"institution": "", "last4": "1234"})
        a = recovery.save_finance(self.store, {"institution": ACCOUNT, "kind": "bank", "last4": "1234", "notes": NOTE})
        recovery.save_finance(self.store, {"institution": "Allstate", "kind": "nope"})
        rows = recovery.finance(self.store)
        self.assertEqual([(r["institution"], r["kind"]) for r in rows], [(ACCOUNT, "bank"), ("Allstate", "other")])
        recovery.save_finance(self.store, {"institution": ACCOUNT, "kind": "bank", "last4": "9999"}, a)
        self.assertEqual(recovery.finance(self.store)[0]["last4"], "9999")
        self.assertFalse(self.store._rows("SELECT 1 FROM finance_accounts WHERE length(last4) > 4"))
        page = recovery.finance_html(recovery.finance(self.store), "Smiths")
        self.assertIn("…9999", page)
        self.assertIn("Allstate", page)


class MealPlanTests(unittest.TestCase):
    SETTINGS = {"adults": 2, "children": 0, "pets": 0, "target_days": 14, "kcal_per_person": 2000,
                "water_gal_per_person": 1, "water_gal_per_pet": 0.5, "not_applicable": [], "cash_per_person": 100}
    TODAY = date(2026, 11, 22)

    def items(self):
        return [food(1, "Rice", 10, 1000, "2027-05-01", "bags"), food(2, "Soup", 6, 400, "2026-12-01"),
                food(3, "Bars", 20, 250), food(4, "Old beans", 3, 300, "2026-10-01"),
                food(5, "Spices", 2, 0, "2028-01-01"), {**food(6, "Dog food", 5, 3000), "essential": "pet_food"}]

    def test_eat_first_and_days(self):
        mp = recovery.meal_plan(self.SETTINGS, self.items(), 5, self.TODAY)
        self.assertEqual([f["name"] for f in mp["eat_first"]], ["Old beans", "Soup", "Rice", "Spices"])  # by expiry
        self.assertTrue(mp["eat_first"][0]["expired"])
        self.assertEqual(mp["expired"], ["Old beans"])
        self.assertEqual(mp["no_kcal"], ["Spices"])
        self.assertEqual((mp["kcal_day"], mp["food_days"]), (4000, round((10000 + 2400 + 5000) / 4000, 1)))  # same as Readiness
        self.assertNotIn("Dog food", json.dumps(mp))

    def test_day_by_day_rotates_soonest_first(self):
        mp = recovery.meal_plan(self.SETTINGS, self.items(), 5, self.TODAY)
        day1 = mp["plan"][0]
        self.assertEqual(day1["eat"][0]["name"], "Soup")           # earliest usable expiry first
        self.assertGreaterEqual(len(day1["eat"]), 2)                # and more than one food a day
        self.assertLessEqual(day1["eat"][0]["kcal"], 2000)          # no more than half the day from one item
        self.assertEqual(day1["kcal"], 4000)
        eaten = {}
        for d in mp["plan"]:
            for x in d["eat"]:
                eaten[x["name"]] = eaten.get(x["name"], 0) + x["units"]
            self.assertNotIn("Old beans", [x["name"] for x in d["eat"]])
        self.assertEqual(eaten["Soup"], 6)                          # soup gone before anything is left over
        self.assertLessEqual(eaten["Rice"], 10)
        self.assertTrue(mp["plan"][-1]["short"] > 0)                # 17,400 kcal can't cover 5 x 4,000
        page = recovery.meal_html(mp, "Smiths")
        self.assertIn("Eat these first", page)
        self.assertIn("Day 5", page)

    def test_food_that_expires_mid_plan_is_dropped(self):
        mp = recovery.meal_plan(self.SETTINGS, [food(1, "Milk", 100, 1000, "2026-11-23"), food(2, "Bars", 100, 250)], 3, self.TODAY)
        self.assertEqual([x["name"] for x in mp["plan"][2]["eat"]], ["Bars"])
        self.assertIn("Milk", [x["name"] for x in mp["plan"][1]["eat"]])


class HttpTests(unittest.TestCase):
    """Over HTTP with logins: an admin, an adult with a family login, and a child's family login."""

    def setUp(self):
        self.hub = p3.Hub()
        st = self.hub.store
        auth.create_user(st, "dad", p3.PW, "admin")
        auth.create_user(st, "mom", p3.PW, "family")
        auth.create_user(st, "kid", p3.PW, "family")
        mom = next(u for u in auth.users(st) if u["username"] == "mom")
        st._exec("INSERT INTO people(name, role, uid, user_id, allergies, blood_type) VALUES('Mom', 'adult', 'm', ?, 'penicillin', 'O-')", (mom["id"],))
        st._exec("INSERT INTO contacts(name, relation, phone, out_of_area) VALUES('Grandma', 'grandmother', '555-0100', 1)")
        st.save_site({"name": "Big oak tree", "kind": "meet_near", "address": "end of the street"})
        st.save_site({"name": "Library", "kind": "meet_far", "address": "1 Main St"})
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {}
        for u in ("dad", "mom", "kid"):
            r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": u, "password": p3.PW}).encode(),
                                       method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
            with self.opener.open(r) as resp:
                self.c[u] = resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def tearDown(self):
        self.hub.close()

    def req(self, method, path, body=None, user="dad", raw=None, ctype="application/json"):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.hub.url + path, method=method, data=data,
                                   headers={"X-Bunkr": "1", "Content-Type": ctype, "Cookie": f"bunkr_session={self.c[user]}"})
        try:
            with self.opener.open(r) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def js(self, *a, **k):
        st, _hd, body = self.req(*a, **k)
        return st, json.loads(body or b"{}")

    def fill(self):
        room = self.js("POST", "/api/recovery/rooms", {"name": "Office"})[1]["id"]
        item = self.js("POST", "/api/recovery/items", {"room_id": room, "name": "Laptop", "serial": SERIAL,
                                                       "price_paid": 1500, "replacement_value": 1300})[1]["id"]
        st, r = self.js("POST", f"/api/recovery/items/{item}/files?kind=photo&name=laptop.jpg", raw=JPEG, ctype="image/jpeg")
        self.assertEqual(st, 200, r)
        st, r = self.js("POST", f"/api/recovery/items/{item}/files?kind=receipt&name=r.pdf", raw=PDF, ctype="application/pdf")
        self.assertEqual(st, 200, r)
        receipt = r["id"]
        st, r = self.js("POST", "/api/finance", {"institution": ACCOUNT, "kind": "bank", "last4": "4321", "notes": NOTE})
        self.assertEqual(st, 200, r)
        return item, receipt, r["id"]

    def test_finance_is_admin_only(self):
        _item, _receipt, acct = self.fill()
        for who in ("mom", "kid"):
            self.assertEqual(self.req("GET", "/api/finance", user=who)[0], 403)
            self.assertEqual(self.req("GET", "/api/finance-sheet.html", user=who)[0], 403)
            self.assertEqual(self.req("POST", "/api/finance", {"institution": "x"}, user=who)[0], 403)
            self.assertEqual(self.req("DELETE", f"/api/finance/{acct}", user=who)[0], 403)
        self.assertEqual(self.req("POST", "/api/finance", {"institution": "x", "last4": "123456"})[0], 400)
        st, r = self.js("GET", "/api/finance")
        self.assertEqual([a["last4"] for a in r["accounts"]], ["4321"])
        st, hd, page = self.req("GET", "/api/finance-sheet.html")
        self.assertEqual(st, 200)
        self.assertIn(ACCOUNT.encode(), page)
        self.assertIsNone(hd.get("X-Bunkr-Keep"))
        log = audit.rows(self.hub.store, 100)
        self.assertTrue(any(r["what"] == "financial sheet" and r["action"] == "view" for r in log))
        self.assertTrue(any(r["what"] == "printout" and r["detail"] == "/api/finance-sheet.html" for r in log))
        self.assertTrue(any(r["what"] == "denied" or r["action"] == "denied" for r in log))
        self.assertEqual(self.req("POST", f"/api/finance/{acct}", {"institution": ACCOUNT, "last4": "1"})[0], 200)
        self.assertEqual(self.req("DELETE", f"/api/finance/{acct}")[0], 200)

    def test_serials_and_receipts_are_admin_only(self):
        item, receipt, _acct = self.fill()
        st, fam = self.js("GET", "/api/recovery", user="kid")
        self.assertEqual(st, 200)
        self.assertNotIn(SERIAL, json.dumps(fam))
        self.assertEqual([f["kind"] for f in fam["items"][0]["files"]], ["photo"])
        photo = fam["items"][0]["files"][0]["id"]
        st, hd, body = self.req("GET", f"/api/recovery/files/{photo}", user="kid")
        self.assertEqual((st, hd["Content-Type"], body), (200, "image/jpeg", JPEG))
        self.assertEqual(self.req("GET", f"/api/recovery/files/{receipt}", user="kid")[0], 403)
        self.assertEqual(self.req("GET", f"/api/recovery/files/{receipt}")[2], PDF)
        for path in ("/api/insurance-list.html", "/api/insurance-list.csv"):
            body = self.req("GET", path, user="kid")[2]
            self.assertIn(b"Laptop", body)
            self.assertNotIn(SERIAL.encode(), body)
            self.assertIn(SERIAL.encode(), self.req("GET", path)[2])
        # the child can look but not change; the adult can change but not touch serials or receipts
        self.assertEqual(self.req("POST", "/api/recovery/items", {"name": "Bike"}, user="kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/recovery/rooms", {"name": "Garage"}, user="kid")[0], 403)
        self.assertEqual(self.req("DELETE", f"/api/recovery/items/{item}", user="kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/recovery/items", {"name": "Bike"}, user="mom")[0], 200)
        self.assertEqual(self.req("POST", f"/api/recovery/items/{item}", {"name": "Laptop", "serial": "HACKED"}, user="mom")[0], 200)
        self.assertEqual(next(i for i in recovery.items(self.hub.store, True) if i["name"] == "Laptop")["serial"], SERIAL)
        self.assertEqual(self.req("POST", f"/api/recovery/items/{item}/files?kind=receipt", raw=PDF, ctype="application/pdf", user="mom")[0], 403)
        self.assertEqual(self.req("DELETE", f"/api/recovery/files/{receipt}", user="mom")[0], 403)
        self.assertEqual(self.req("POST", f"/api/recovery/items/{item}/files?kind=photo", raw=b"GIF89a....", ctype="image/gif")[0], 400)
        # too big: refused from the length alone, before the body is read
        conn = http.client.HTTPConnection(self.hub.url.split("//")[1], timeout=10)
        conn.putrequest("POST", f"/api/recovery/items/{item}/files?kind=photo")
        for k, v in (("X-Bunkr", "1"), ("Content-Type", "image/jpeg"), ("Content-Length", str(recovery.MAX_FILE + 1)),
                     ("Cookie", f"bunkr_session={self.c['dad']}")):
            conn.putheader(k, v)
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()
        self.assertIsNone(self.req("GET", "/api/recovery")[1].get("X-Bunkr-Keep"))
        printed = self.req("GET", "/api/insurance-list.html")[2].decode()
        self.assertIn(f"/api/recovery/files/{photo}", printed)       # thumbnails
        self.assertIn("$1,500.00", printed)                          # totals
        self.assertIn("Office: 1 item(s)", printed)

    def test_nothing_sensitive_leaves_the_hub(self):
        self.fill()
        st = self.hub.store
        secrets = (SERIAL, ACCOUNT, NOTE, "4321")
        # sync payloads to field nodes
        payload = json.dumps(sync.export_records(st) | checkins.export(st, "", include_people=True)
                             | situation.export(st) | brief.export(st))
        # what nodes get as plain plan files
        published = json.dumps(server._published_extras())
        # what a phone may keep offline: only OFFLINE_HEADS, and none of those carry it
        self.assertFalse({"recovery", "finance", "finance-sheet.html", "insurance-list.html"} & server.OFFLINE_HEADS)
        offline = ""
        for head in sorted(server.OFFLINE_HEADS):
            for who in ("dad", "kid"):
                s, hd, body = self.req("GET", f"/api/{head}", user=who)
                if s == 200 and hd.get("X-Bunkr-Keep"):
                    offline += body.decode()
        self.assertIn("Laptop", "x" + recovery.insurance_csv(recovery.items(st, True), True))
        # the AI connector: no tool for it, and nothing it says mentions it
        tools = mcp_tools.make_tools(server)
        self.assertFalse([n for n in tools if any(w in n for w in ("finance", "recovery", "insurance", "account"))])
        admin = {"id": 1, "username": "dad", "role": "admin"}
        said = ""
        for name, t in tools.items():
            if t["annotations"].get("readOnlyHint"):
                try:
                    said += json.dumps(t["fn"]({}, admin))
                except Exception as exc:  # a tool that needs arguments
                    said += str(exc)
        for where, text in (("sync", payload), ("published", published), ("offline", offline), ("connector", said)):
            for s in secrets:
                self.assertNotIn(s, text, f"{s} leaked into {where}")
        self.assertNotIn("Laptop", payload + said)  # the home inventory isn't synced or offered to the AI connector at all

    def test_archive_has_everything(self):
        self.fill()
        extras = server.archive_extras()
        self.assertIn(SERIAL, extras["Home inventory/Home inventory.html"])
        self.assertIn(SERIAL, extras["Home inventory/Home inventory.csv"])
        self.assertIn(ACCOUNT, extras["Financial access sheet.html"])
        files = [k for k, v in extras.items() if isinstance(v, Path)]
        self.assertEqual(len(files), 2)
        self.assertIn("files/1-1-photo.jpg", extras["Home inventory/Home inventory.html"])
        self.assertIn("Kids plan.html", extras)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            names = builder._encrypt_documents(builder.Job(), [], p3.PW, out, split=False, extra=extras)
            listing = subprocess.run(["7z", "l", f"-p{p3.PW}", str(out / names[0])], capture_output=True, text=True).stdout
            self.assertIn("Financial access sheet.html", listing)
            self.assertIn("1-1-photo.jpg", listing)
            self.assertIn("1-2-receipt.pdf", listing)

    def test_kids_plan_and_meal_plan_print(self):
        st, hd, page = self.req("GET", "/api/kids-plan.html", user="kid")
        page = page.decode()
        self.assertEqual(st, 200)
        for want in ("911", "Grandma", "555-0100", "Big oak tree", "Library", "Earthquake", "Tornado", "Fire", "Lost"):
            self.assertIn(want, page)
        for never in ("penicillin", "O-", ACCOUNT):
            self.assertNotIn(never, page)
        self.assertIn("kids-plan.html", " ".join(server._published_extras()))
        self.hub.store.save_item({"name": "Chili", "category": "food", "quantity": 4, "unit": "cans", "pack_size": 1, "per_unit": 500,
                                  "essential": "", "expires": "2027-01-01"})
        st, mp = self.js("GET", "/api/meal-plan?days=3", user="kid")
        self.assertEqual((st, mp["days"], mp["eat_first"][0]["name"]), (200, 3, "Chili"))
        st, hd, page = self.req("GET", "/api/meal-plan.html?days=2", user="kid")
        self.assertEqual(st, 200)
        self.assertIn(b"Chili", page)
        self.assertIn(b"Day 2", page)


if __name__ == "__main__":
    unittest.main()
