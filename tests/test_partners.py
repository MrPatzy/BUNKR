"""Partner hubs: share tokens (shown once, only a hash kept), what a share includes, that firearms, medical, documents,
money, inventory and whereabouts never leak, the token-checked endpoint (no login, rate limits, audit, revoke), and the
receiving side (token file mode 600, pulling, cleaning what arrives, the read-only card)."""

import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, checkins, comms, medical, partners  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

SECRETS = ("Glock", "SN-998877", "Insulin", "Penicillin", "Passport", "Wells Fargo", "Rice", "39.61234", "-105.31234",
           "Grandma's back room", "Cabin key under the mat", "AB+")


def seed(st):
    """A household with everything a share must never carry."""
    wife = st._exec("INSERT INTO people(name, role, uid, allergies, blood_type, call_sign, gmrs_call) "
                    "VALUES('Wife', 'adult', 'w', 'Penicillin', 'AB+', 'K0WIF', '')")
    st._exec("INSERT INTO people(name, role, uid, gmrs_call) VALUES('Kid', 'child', 'k', 'WRKD111')")
    medical.save(st, wife, {"name": "Insulin", "per_day": 3, "on_hand": 30})
    st.save_item({"name": "Glock 19", "category": "firearms", "quantity": 1, "serial": "SN-998877"})
    st.save_item({"name": "Rice", "category": "food", "quantity": 20})
    st.save_document({"path": "/tmp/passport.pdf", "label": "Passport", "doc_type": "id"})
    st._exec("INSERT INTO contacts(name, phone, notes) VALUES('Wells Fargo', '555', 'bank')")
    checkins.add_checkin(st, "w", "safe", note="Cabin key under the mat", place="Grandma's back room", lat=39.61234, lon=-105.31234)
    checkins.start_call(st, "meet", "dad", place="School", address="123 Main St", lat=39.7, lon=-105.2, message="Pick up at 3")
    comms.save_net(st, {"name": "ARES", "freq": "146.940", "weekday": 1, "time": "19:30", "net_control": "K0NC"})
    comms.save_neighbour(st, {"name": "Neighbour Bob", "phone": "555-0199"})
    comms.save_licence(st, wife, {"kind": "ham", "class": "General", "callsign": "K0WIF", "number": "FRN-0001", "notes": "Cabin key under the mat"})
    st._exec("INSERT INTO channels(name, rx_mhz, in_plan, service) VALUES('FAMILY', 462.5625, 1, 'gmrs')")
    st._exec("INSERT INTO channels(name, rx_mhz, in_plan, service) VALUES('SECRETCH', 151.94, 0, 'other')")


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "s.db")
        seed(self.st)
        partners._fails.clear()
        partners._uses.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def share(self, parts):
        rid, uid, token = partners.create_share(self.st, "Jordan", parts)
        return self.st._rows("SELECT * FROM partner_shares WHERE id=?", (rid,))[0], uid, token

    def test_token_only_hash_stored(self):
        row, uid, token = self.share(["board"])
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(row["token_hash"], partners.token_hash(token))
        dump = "\n".join(self.st._conn.iterdump())
        self.assertNotIn(token, dump)
        self.assertEqual(partners.check(self.st, uid, token)[0]["id"], row["id"])
        self.assertEqual(partners.check(self.st, uid, token + "x")[1][0], 401)
        self.assertEqual(partners.check(self.st, "../../etc", token)[1][0], 401)
        self.assertEqual(partners.check(self.st, uid, "")[1][0], 401)
        with self.assertRaises(ValueError):
            partners.create_share(self.st, "Jordan", ["firearms", "medical"])  # not choosable
        with self.assertRaises(ValueError):
            partners.create_share(self.st, "", ["board"])
        partners.revoke_share(self.st, row["id"])
        self.assertEqual(partners.check(self.st, uid, token)[1][0], 403)

    def test_scope_only_what_was_chosen(self):
        row, _, _ = self.share(["board"])
        p = partners.payload(self.st, row)
        self.assertEqual(p["includes"], ["board"])
        self.assertNotIn("call", p)
        self.assertNotIn("nets", p)
        self.assertNotIn("callsigns", p)
        wife = next(b for b in p["board"] if b["name"] == "Wife")
        self.assertEqual(set(wife), {"name", "status", "status_label", "at"})
        self.assertEqual(wife["status_label"], "I'm safe")
        row, _, _ = self.share(["calls", "comms", "licences"])
        p = partners.payload(self.st, row)
        self.assertNotIn("board", p)
        self.assertEqual(set(p["call"]), {"kind", "label", "place", "message", "created_at", "drill"})
        self.assertEqual([n["name"] for n in p["nets"]], ["ARES"])
        self.assertEqual([c["name"] for c in p["channels"]], ["FAMILY"])  # only the family plan's channels
        self.assertEqual({c["callsign"] for c in p["callsigns"]}, {"K0WIF", "WRKD111"})

    def test_sensitive_things_never_leak(self):
        row, _, _ = self.share(list(partners.PARTS))
        text = json.dumps(partners.payload(self.st, row))
        for s in SECRETS + ("123 Main St", "FRN-0001", "SECRETCH", "Neighbour Bob", "555-0199", "firearm", "serial", "allerg",
                            "lat", "lon", "battery"):
            self.assertNotIn(s, text, s)

    def test_rate_limits(self):
        row, uid, token = self.share(["board"])
        for _ in range(partners.FAIL_MAX):
            partners.check(self.st, uid, "wrong", ip="1.2.3.4", now_t=1000)
        self.assertEqual(partners.check(self.st, uid, token, ip="1.2.3.4", now_t=1001)[1][0], 429)  # even the right one, for a while
        self.assertIsNone(partners.check(self.st, uid, token, ip="5.6.7.8", now_t=1001)[1])
        self.assertIsNone(partners.check(self.st, uid, token, ip="1.2.3.4", now_t=1000 + partners.FAIL_WINDOW + 1)[1])
        for i in range(partners.USE_MAX):
            partners.check(self.st, uid, token, ip="9.9.9.9", now_t=5000 + i)
        self.assertEqual(partners.check(self.st, uid, token, ip="9.9.9.9", now_t=5200)[1][0], 429)
        self.assertGreater(self.st._rows("SELECT uses FROM partner_shares WHERE id=?", (row["id"],))[0]["uses"], 100)

    def test_clean_payload_from_another_hub(self):
        with self.assertRaises(ValueError):
            partners.clean_payload({"hello": 1})
        d = partners.clean_payload({"bunkr_partner": 1, "from": "T" * 500, "as_of": "2026-11-22T10:00:00+00:00",
                                    "includes": ["board", "evil"], "board": [{"name": "Jordan", "status": "safe", "x": "<script>"}, "junk"],
                                    "call": {"label": "Meet at", "drill": "yes", "lat": 1}, "channels": [{"name": "A", "rx_mhz": "1; DROP"}],
                                    "firearms": [{"name": "rifle"}]})
        self.assertEqual(len(d["from"]), 80)
        self.assertEqual(d["includes"], ["board"])
        self.assertEqual(d["board"], [{"name": "Jordan", "status": "safe", "status_label": "", "at": ""}])
        self.assertEqual(d["call"]["drill"], False)
        self.assertNotIn("lat", d["call"])
        self.assertIsNone(d["channels"][0]["rx_mhz"])
        self.assertNotIn("firearms", d)


class PartnerHttpTests(unittest.TestCase):
    def setUp(self):
        partners._fails.clear()
        partners._uses.clear()
        self.hub = p3.Hub()
        st = self.hub.store
        seed(st)
        auth.create_user(st, "dad", p3.PW, "admin")
        kid = auth.create_user(st, "kid", p3.PW, "family")
        st._exec("UPDATE people SET user_id=? WHERE uid='k'", (kid,))
        self.opener = urllib.request.build_opener(p3.NoRedirect)
        self.c = {}
        for u in ("dad", "kid"):
            r = urllib.request.Request(self.hub.url + "/api/login", data=json.dumps({"username": u, "password": p3.PW}).encode(),
                                       method="POST", headers={"X-Bunkr": "1", "Content-Type": "application/json"})
            with self.opener.open(r) as resp:
                self.c[u] = resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def tearDown(self):
        self.hub.close()

    def req(self, method, path, body=None, user="dad", headers=None):
        h = {"X-Bunkr": "1", "Content-Type": "application/json"}
        if user:
            h["Cookie"] = f"bunkr_session={self.c[user]}"
        r = urllib.request.Request(path if path.startswith("http") else self.hub.url + path, method=method,
                                   data=json.dumps(body).encode() if body is not None else None, headers=h | (headers or {}))
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_share_endpoint_and_receiving(self):
        self.assertEqual(self.req("GET", "/api/partner-shares", user="kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/partner-shares", {"name": "Jordan", "parts": ["board"]}, user="kid")[0], 403)
        st, r = self.req("POST", "/api/partner-shares", {"name": "Jordan", "parts": ["board", "calls", "comms", "licences"]})
        self.assertEqual(st, 200)
        url, token = r["url"], r["token"]
        self.assertRegex(url, r"/api/partner/[0-9a-f]{32}$")
        listed = self.req("GET", "/api/partner-shares")[1]
        self.assertNotIn(token, json.dumps(listed))
        self.assertNotIn("token_hash", json.dumps(listed))

        # the partner endpoint: no login, its own token
        self.assertEqual(self.req("GET", url, user=None)[0], 401)
        self.assertEqual(self.req("GET", url, user=None, headers={"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(self.req("GET", url, user="dad")[0], 401)  # a session isn't a share token
        st, data = self.req("GET", url, user=None, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(st, 200)
        self.assertEqual(data["includes"], ["board", "calls", "comms", "licences"])
        text = json.dumps(data)
        for s in SECRETS + ("123 Main St", "Neighbour Bob"):
            self.assertNotIn(s, text, s)
        self.assertEqual(self.req("POST", url, {}, user=None, headers={"Authorization": f"Bearer {token}"})[0], 405)
        log = self.hub.store._rows("SELECT * FROM audit_log WHERE what='partner share'")
        self.assertIn("view", {r["action"] for r in log})
        self.assertIn("denied", {r["action"] for r in log})
        self.assertTrue(any(r["username"] == "partner:Jordan" for r in log))

        # the receiving side, pointed at this same hub
        self.assertEqual(self.req("POST", "/api/partners", {"name": "Jordan", "url": url, "token": token}, user="kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/partners", {"name": "Jordan", "url": "file:///etc/passwd", "token": token})[0], 400)
        st, r = self.req("POST", "/api/partners", {"name": "Jordan's house", "url": url, "token": token})
        self.assertEqual(st, 200)
        pid = r["id"]
        tf = partners.token_file(self.hub.dir, pid)
        self.assertEqual(stat.S_IMODE(os.stat(tf).st_mode), 0o600)
        self.assertNotIn(token, "\n".join(self.hub.store._conn.iterdump()))
        st, v = self.req("POST", f"/api/partners/{pid}/pull")
        p = v["partners"][0]
        self.assertEqual((p["last_error"], p["has_token"]), ("", True))
        self.assertEqual(p["data"]["call"]["label"], "Meet at")
        self.assertTrue(p["as_of"])
        st, v = self.req("GET", "/api/partners", user="kid")  # family reads the card, never the address or token
        self.assertEqual(v["partners"][0]["data"]["board"][0]["name"] in ("Wife", "Kid"), True)
        self.assertNotIn("url", v["partners"][0])
        self.assertNotIn(token, json.dumps(v))

        # turned off: the partner keeps its last copy and says why it can't update
        share_id = listed["shares"][0]["id"]
        self.assertEqual(self.req("DELETE", f"/api/partner-shares/{share_id}")[0], 200)
        self.assertEqual(self.req("GET", url, user=None, headers={"Authorization": f"Bearer {token}"})[0], 403)
        p = self.req("POST", f"/api/partners/{pid}/pull")[1]["partners"][0]
        self.assertEqual(p["last_error"], "they turned this share off")
        self.assertEqual(p["data"]["call"]["label"], "Meet at")
        self.assertEqual(self.req("DELETE", f"/api/partners/{pid}")[0], 200)
        self.assertFalse(tf.exists())


if __name__ == "__main__":
    unittest.main()
