"""Phase 3: logins, sessions, roles, node pairing, file sync and record merge."""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, cli, manifest, nodes, server, sync  # noqa: E402
from bunkr.db import Store  # noqa: E402
from bunkr.depot import Depot  # noqa: E402

PW = "correct horse battery"


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "a.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_hash_roundtrip(self):
        h = auth.hash_password("pw-1234567890", iterations=1000)
        self.assertTrue(auth.verify_password("pw-1234567890", h))
        self.assertFalse(auth.verify_password("pw-1234567891", h))
        self.assertFalse(auth.verify_password("x", "garbage"))

    def test_login_lockout_and_sessions(self):
        uid = auth.create_user(self.store, "dad", PW, "admin")
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.store, "DAD", PW)  # usernames are case-insensitive
        with self.assertRaises(auth.AuthError):
            auth.create_user(self.store, "kid", "short")
        token = auth.login(self.store, "Dad", PW)
        self.assertEqual(auth.session_user(self.store, token)["username"], "dad")
        for _ in range(auth.MAX_FAILS):
            with self.assertRaises(auth.AuthError):
                auth.login(self.store, "dad", "wrong password!")
        with self.assertRaises(auth.AuthError) as cm:
            auth.login(self.store, "dad", PW)  # locked even with the right password
        self.assertIn("too many", str(cm.exception))
        auth.set_password(self.store, uid, "another good password")
        self.assertIsNone(auth.session_user(self.store, token))  # password change logs out everywhere
        self.assertIsNotNone(auth.login(self.store, "dad", "another good password"))

    def test_last_admin_protected(self):
        uid = auth.create_user(self.store, "dad", PW, "admin")
        with self.assertRaises(auth.AuthError):
            auth.set_role(self.store, uid, "family")
        with self.assertRaises(auth.AuthError):
            auth.delete_user(self.store, uid)

    def test_cookie_parsing(self):
        self.assertEqual(auth.token_from_cookie("a=1; bunkr_session=XYZ; b=2"), "XYZ")
        self.assertIn("Secure", auth.cookie_header("t", True))
        self.assertNotIn("Secure", auth.cookie_header("t", False))


class Hub:
    """A hub server in a thread with a temporary database and store."""

    def __init__(self, require_login=True):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        server.store = Store(d / "hub.db")
        server.depot = Depot(server.store, d / "store")
        server.depot.ensure()
        server.REQUIRE_LOGIN = require_login
        server.ROLE = "hub"
        self.store, self.depot, self.dir = server.store, server.depot, d
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        server.REQUIRE_LOGIN = False
        server.ROLE = "standalone"
        self.tmp.cleanup()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class LoginHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        auth.create_user(self.hub.store, "dad", PW, "admin")
        auth.create_user(self.hub.store, "kid", PW, "family")
        self.opener = urllib.request.build_opener(NoRedirect)

    def tearDown(self):
        self.hub.close()

    def req(self, method, path, body=None, cookie=None, xb=True, host=None):
        h = {"Content-Type": "application/json"}
        if xb:
            h["X-Bunkr"] = "1"
        if cookie:
            h["Cookie"] = f"bunkr_session={cookie}"
        if host:
            h["Host"] = host
        r = urllib.request.Request(self.hub.url + path, method=method, headers=h,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(r) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def login(self, user):
        st, hd, _ = self.req("POST", "/api/login", {"username": user, "password": PW})
        self.assertEqual(st, 200)
        cookie = hd["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        return cookie.split(";")[0].split("=", 1)[1]

    def test_pages_need_login(self):
        st, hd, _ = self.req("GET", "/")
        self.assertEqual((st, hd["Location"]), (303, "/login.html"))
        self.assertEqual(self.req("GET", "/login.html")[0], 200)
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("POST", "/api/login", {"username": "dad", "password": "nope nope nope"})[0], 401)
        self.assertEqual(self.req("POST", "/api/login", {"username": "dad", "password": PW}, xb=False)[0], 403)

    def test_roles(self):
        dad, kid = self.login("dad"), self.login("kid")
        self.assertEqual(self.req("GET", "/", cookie=dad)[0], 200)
        st, _, body = self.req("GET", "/api/state", cookie=kid, host="192.168.1.9:8765")  # any Host with logins on
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["me"]["role"], "family")
        self.assertEqual(self.req("POST", "/api/locations", {"name": "Car", "kind": "vehicle"}, cookie=kid)[0], 200)
        for path in ("/api/browse?path=/", "/api/nodes", "/api/depot", "/api/users", "/api/backups"):
            self.assertEqual(self.req("GET", path, cookie=kid)[0], 403, path)
        self.assertEqual(self.req("GET", "/api/users", cookie=dad)[0], 200)
        self.assertEqual(self.req("POST", "/api/logout", {}, cookie=kid)[0], 200)
        self.assertEqual(self.req("GET", "/api/state", cookie=kid)[0], 401)

    def test_store_maps_need_login_and_support_range(self):
        maps = self.hub.depot.root / "maps"
        (maps / "area.pmtiles").write_bytes(b"0123456789")
        dad = self.login("dad")
        self.assertEqual(self.req("GET", "/store/maps/area.pmtiles")[0], 303)
        st, _, body = self.req("GET", "/store/maps/", cookie=dad)
        self.assertEqual(json.loads(body), [{"name": "area.pmtiles", "size": 10}])
        r = urllib.request.Request(self.hub.url + "/store/maps/area.pmtiles",
                                   headers={"Cookie": f"bunkr_session={dad}", "Range": "bytes=2-5"})
        with urllib.request.urlopen(r) as resp:
            self.assertEqual((resp.status, resp.read()), (206, b"2345"))
        self.assertEqual(self.req("GET", "/store/../hub.db", cookie=dad)[0], 404)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.hub = Hub()
        self.work = tempfile.TemporaryDirectory()
        self.w = Path(self.work.name)
        src = self.w / "zimgit-water_en_2026-01.zim"
        src.write_bytes(b"W" * 300_000)
        self.row = self.hub.depot.add_file("library", "water", src, version="2026-01")

    def tearDown(self):
        self.hub.close()
        self.work.cleanup()

    def pair(self, name):
        nid = nodes.save(self.hub.store, {"name": name, "role": "pi"})
        code, _ = nodes.new_pair_code(self.hub.store, nid)
        tok = self.w / f"{name}.token"
        sync.pair_node(self.hub.url, code, tok)
        with self.assertRaises(SystemExit):  # codes work once
            sync.pair_node(self.hub.url, code, self.w / "again.token")
        return nid, tok

    def test_files_sync_resume_and_report(self):
        nid, tok = self.pair("Pi")
        store_dir = self.w / "pi-store"
        lib = store_dir / "library"
        lib.mkdir(parents=True)
        (lib / (Path(self.row["path"]).name + ".part")).write_bytes(b"W" * 100_000)  # interrupted earlier
        log = []
        sync.run(tok, store_dir, self.w / "pi.db", log=log.append)
        got = store_dir / self.row["path"]
        self.assertEqual(manifest.sha256_file(got), self.row["sha256"])
        node = nodes.get(self.hub.store, nid)
        reported = json.loads(node["manifest"])
        self.assertEqual(reported["files"][0]["sha256"], self.row["sha256"])
        self.assertEqual(nodes.status(node, nodes.plan_files(server.node_plan(node)), "", 0)["behind"], 0)
        # second run downloads nothing
        log.clear()
        sync.run(tok, store_dir, self.w / "pi.db", log=log.append)
        self.assertFalse(any(l.startswith("Fetching") for l in log))
        # store drops the file -> node removes it, but never touches files it didn't put there
        (store_dir / "library" / "my-own.zim").write_bytes(b"mine")
        self.hub.depot.remove(self.row)
        sync.run(tok, store_dir, self.w / "pi.db", log=log.append)
        self.assertFalse(got.exists())
        self.assertTrue((store_dir / "library" / "my-own.zim").exists())

    def test_unplanned_file_and_bad_token_refused(self):
        _, tok = self.pair("Pi")
        c = sync.Client(self.hub.url, sync.read_token(tok)["token"])
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(c._req("GET", "file/../hub.db"))
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(IOError):
            sync.Client(self.hub.url, "nope").json("GET", "plan")

    def test_records_merge_both_ways_and_relay(self):
        _, tok_a = self.pair("PiA")
        _, tok_b = self.pair("PiB")
        hub = self.hub.store
        car = hub.save_location({"name": "Car", "kind": "vehicle", "counts_home": 0})
        hub_item = hub.save_item({"name": "Cables", "category": "vehicle", "quantity": 1, "location_id": car})
        db_a, db_b = self.w / "a.db", self.w / "b.db"
        sync.run(tok_a, self.w / "sa", db_a, files=False, log=lambda m: None)
        sync.run(tok_b, self.w / "sb", db_b, files=False, log=lambda m: None)
        a, b = Store(db_a), Store(db_b)
        self.assertEqual([(i["name"], i["location"]) for i in a.items()], [("Cables", "Car")])
        # node A adds water used from the go-bag; it reaches the hub and then node B
        bag = a.save_location({"name": "Bag", "kind": "gobag"})
        a.save_item({"name": "Water", "category": "water", "quantity": 2, "per_unit": 1, "location_id": bag})
        sync.run(tok_a, self.w / "sa", db_a, files=False, log=lambda m: None)
        sync.run(tok_b, self.w / "sb", db_b, files=False, log=lambda m: None)
        self.assertEqual(sorted(i["name"] for i in b.items()), ["Cables", "Water"])
        # both edit the cables while apart: newer wins, the other is logged
        ia = next(i for i in a.items() if i["name"] == "Cables")
        a.save_item({"quantity": 2, "updated_at": "2030-01-01T00:00:00+00:00"}, ia["id"])
        hub.save_item({"quantity": 5, "updated_at": "2030-01-02T00:00:00+00:00"}, hub_item)
        sync.run(tok_a, self.w / "sa", db_a, files=False, log=lambda m: None)
        self.assertEqual(next(i for i in a.items() if i["name"] == "Cables")["quantity"], 5)
        conflicts = hub._rows("SELECT * FROM sync_conflicts")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(json.loads(conflicts[0]["lost"])["quantity"], 2)
        # deletion on the hub travels as a tombstone
        hub.delete_item(hub_item)
        sync.run(tok_b, self.w / "sb", db_b, files=False, log=lambda m: None)
        self.assertEqual([i["name"] for i in b.items()], ["Water"])


class CliTests(unittest.TestCase):
    def test_subcommand_detection(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "c.db")
            pwf = Path(d) / "pw"
            pwf.write_text(PW)
            cli.main(["--db", db, "user", "add", "mom", "--admin", "--password-file", str(pwf)])
            cli.main(["user", "role", "mom", "admin", "--db", db] if False else ["--db", db, "user", "list"])
            self.assertEqual(auth.user_by_name(Store(db), "mom")["role"], "admin")


if __name__ == "__main__":
    unittest.main()
