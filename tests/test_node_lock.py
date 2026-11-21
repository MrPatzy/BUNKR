"""Node lock: the vault, the hub handing out the key and sealed plan files, and a node that starts locked, unlocks
from the hub or with the passphrase, keeps its database only in the vault, and serves nothing private while locked."""

import base64
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import auth, cli, nodes, server, sync, vault  # noqa: E402
from bunkr.db import Store  # noqa: E402
from bunkr.depot import Depot  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

PASS = "correct horse battery staple"


class VaultTests(unittest.TestCase):
    def test_seal_wrap_and_files(self):
        key = os.urandom(32)
        blob = vault.seal(key, b"plan", b"a")
        self.assertEqual(vault.unseal(key, blob, b"a"), b"plan")
        with self.assertRaises(vault.VaultError):
            vault.unseal(key, blob, b"b")
        with self.assertRaises(vault.VaultError):
            vault.unseal(os.urandom(32), blob, b"a")
        w = vault.wrap(key, PASS)
        self.assertNotIn(base64.b64encode(key).decode(), json.dumps(w))
        self.assertEqual(vault.unwrap(w, PASS), key)
        with self.assertRaises(vault.VaultError):
            vault.unwrap(w, "wrong passphrase!")
        with self.assertRaises(vault.VaultError):
            vault.wrap(key, "short")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            k = vault.hub_key(d)
            self.assertEqual((vault.hub_key(d), (d / "vault.key").stat().st_mode & 0o777), (k, 0o600))
            (d / "family-plan.html").write_text("Meet at the school")
            vault.seal_file(k, d / "family-plan.html", d / "family-plan.html.sealed")
            self.assertNotIn(b"school", (d / "family-plan.html.sealed").read_bytes())
            self.assertEqual(vault.open_sealed(k, d / "family-plan.html.sealed"), b"Meet at the school")
            (d / "medical-cards.html.sealed").write_bytes((d / "family-plan.html.sealed").read_bytes())
            with self.assertRaises(vault.VaultError):  # a sealed file can't pose as another
                vault.open_sealed(k, d / "medical-cards.html.sealed")

    def test_store_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Store(Path(tmp) / "a.db")
            plain.set_raw("x", 1)
            data, _ = plain.snapshot()
            mem = Store(Path(tmp) / "node" / "b.db", data=data)
            self.assertEqual(mem.get_raw("x"), 1)
            before = mem.changes()
            mem.set_raw("y", 2)
            self.assertGreater(mem.changes(), before)
            self.assertFalse((Path(tmp) / "node").exists())  # nothing written next to it


class HubSideTests(unittest.TestCase):
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
        self.nid = nodes.save(self.hub.store, {"name": "Pi", "role": "pi", "budget_bytes": 0, "notes": "", "profile": nodes.DEFAULT_PROFILES["pi"]})
        code, _ = nodes.new_pair_code(self.hub.store, self.nid)
        self.token = sync.pair(self.hub.store, code)["token"]

    def tearDown(self):
        self.hub.close()

    def req(self, method, path, body=None, user="dad", bearer=None):
        h = {"X-Bunkr": "1", "Content-Type": "application/json"}
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        else:
            h["Cookie"] = f"bunkr_session={self.c[user]}"
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_key_passphrase_sealed_plan_and_lost(self):
        st, r = self.req("POST", "/api/sync/vault", {"lock": "waiting"}, bearer=self.token)
        self.assertEqual((st, len(base64.b64decode(r["key"])), r["wrapped"]), (200, 32, None))
        self.assertEqual(nodes.get(self.hub.store, self.nid)["lock"], "waiting")
        self.assertEqual(self.req("POST", "/api/nodes/lock-passphrase", {"passphrase": PASS, "repeat": PASS}, user="kid")[0], 403)
        self.assertEqual(self.req("POST", "/api/nodes/lock-passphrase", {"passphrase": PASS, "repeat": "other"})[0], 400)
        self.assertEqual(self.req("POST", "/api/nodes/lock-passphrase", {"passphrase": "short", "repeat": "short"})[0], 400)
        self.assertEqual(self.req("POST", "/api/nodes/lock-passphrase", {"passphrase": PASS, "repeat": PASS})[0], 200)
        st, r = self.req("POST", "/api/sync/vault", {"lock": "on"}, bearer=self.token)
        self.assertEqual(vault.unwrap(r["wrapped"], PASS), base64.b64decode(r["key"]))
        # the plan files: plain for unlocked nodes, sealed for locked ones
        server.publish_generated()
        paths = {f["path"] for f in nodes.plan_files(server.node_plan(nodes.get(self.hub.store, self.nid)))}
        self.assertIn("plan/family-plan.html.sealed", paths)
        self.assertNotIn("plan/family-plan.html", paths)
        sealed = self.hub.depot.root / "plan/family-plan.html.sealed"
        self.assertIn(b"<html", vault.open_sealed(base64.b64decode(r["key"]), sealed).lower())
        self.req("POST", "/api/sync/vault", {"lock": "off"}, bearer=self.token)
        paths = {f["path"] for f in nodes.plan_files(server.node_plan(nodes.get(self.hub.store, self.nid)))}
        self.assertIn("plan/family-plan.html", paths)
        self.assertNotIn("plan/family-plan.html.sealed", paths)
        # lost or stolen: cut off
        self.assertEqual(self.req("POST", f"/api/nodes/{self.nid}/lost", {})[0], 200)
        self.assertEqual(self.req("POST", "/api/sync/vault", {"lock": "on"}, bearer=self.token)[0], 401)
        self.assertTrue(next(n for n in self.req("GET", "/api/nodes")[1]["nodes"] if n["id"] == self.nid)["lost"])


class NodeTests(unittest.TestCase):
    """A Pi with --lock, served in a thread."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.db = d / "bunkr.db"
        plain = Store(self.db)
        auth.create_user(plain, "dad", p3.PW, "admin")
        plain.save_item({"name": "Beans", "category": "food", "quantity": 12, "unit": "cans", "per_unit": 0, "essential": "",
                         "location_id": None, "expires": "", "notes": ""})
        (d / "db-backups").mkdir()
        (d / "db-backups" / "bunkr-nightly-20261119.db").write_bytes(b"old plain copy")
        self.key = os.urandom(32)
        self.saved = {k: getattr(server, k) for k in ("store", "depot", "ROLE", "REQUIRE_LOGIN")}
        self.saved_lock = {k: server.LOCK[k] for k in ("on", "vault", "key", "locked", "held", "error", "token_file", "sealed", "db")}
        self.saved_nets = list(server.OPEN_NETS)
        server.store, server.ROLE, server.REQUIRE_LOGIN = plain, "pi", True
        server.depot = Depot(plain, d / "store")
        server.depot.ensure()
        server.OPEN_NETS[:] = [server.ipaddress.ip_network("127.0.0.1/32")]
        server.LOCK.update(on=True, vault=vault.NodeVault(self.db), key=None, locked=False, held=False, error="", sealed=-1, db=str(self.db))
        server.LOCK["fails"].clear()
        self.real_ask = server._ask_hub
        self.hub_says = None
        server._ask_hub = lambda state: self.hub_says
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.opener = urllib.request.build_opener(p3.NoRedirect)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server._ask_hub = self.real_ask
        for k, v in self.saved.items():
            setattr(server, k, v)
        server.LOCK.update(self.saved_lock)
        server.OPEN_NETS[:] = self.saved_nets
        self.tmp.cleanup()

    def get(self, path, cookie=None, method="GET", body=None, form=None):
        h = {"X-Bunkr": "1"}
        data = None
        if cookie:
            h["Cookie"] = f"bunkr_session={cookie}"
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        try:
            r = self.opener.open(urllib.request.Request(self.url + path, data=data, method=method, headers=h))
        except urllib.error.HTTPError as e:
            r = e
        raw = r.read()
        return r.status if hasattr(r, "status") else r.code, raw.decode("utf-8", "replace"), r.headers

    def login(self):
        st, body, h = self.get("/api/login", method="POST", body={"username": "dad", "password": p3.PW})
        self.assertEqual(st, 200, body)
        return h["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def test_first_key_moves_everything_into_the_vault(self):
        server.node_lock_tick()  # hub not reachable: keeps running unlocked from the plain file
        self.assertTrue(self.db.exists())
        self.hub_says = self.key
        server.node_lock_tick()
        self.assertFalse(self.db.exists())
        self.assertFalse((self.db.parent / "db-backups" / "bunkr-nightly-20261119.db").exists())
        self.assertNotIn(b"Beans", server.LOCK["vault"].sealed.read_bytes())
        self.assertTrue(server.store.in_memory)
        self.assertEqual(server.node_lock_state(), "on")
        cookie = self.login()
        st, body, _ = self.get("/api/state", cookie)
        self.assertIn("Beans", body)
        # a change reaches the vault on the next tick, not before
        self.get("/api/items", cookie, "POST", {"name": "Rice", "category": "food", "quantity": 5})
        self.assertNotIn(b"Rice", vault.unseal(self.key, server.LOCK["vault"].sealed.read_bytes(), b"bunkr-node-db"))
        server.node_lock_tick()
        reopened = Store(self.db, data=server.LOCK["vault"].load(self.key))
        self.assertIn("Rice", [i["name"] for i in reopened.items()])
        self.assertFalse(self.db.exists())  # still nothing plain on disk

    def lock_up(self):
        self.hub_says = self.key
        server.node_lock_tick()
        server.LOCK["vault"].save_wrapped(vault.wrap(self.key, PASS))
        sealed = server.depot.root / "plan" / "family-plan.html.sealed"
        (server.depot.root / "plan").mkdir(exist_ok=True)
        (server.depot.root / "plan" / "family-plan.html").write_text("Meet at the school")
        vault.seal_file(self.key, server.depot.root / "plan" / "family-plan.html", sealed)
        (server.depot.root / "plan" / "family-plan.html").unlink()
        server.lock_now()
        self.hub_says = None

    def test_locked_node_shows_nothing_private(self):
        self.lock_up()
        st, body, _ = self.get("/")
        self.assertEqual(st, 200)
        self.assertIn("Locked", body)
        self.assertIn('action="/unlock"', body)
        self.assertNotIn("Family plan", body)
        self.assertNotIn("Inventory", body)
        self.assertEqual(self.get("/api/state")[0], 423)
        self.assertEqual(self.get("/api/report.html")[0], 423)
        self.assertEqual(self.get("/store/plan/family-plan.html")[0], 303)
        self.assertEqual(self.get("/store/plan/family-plan.html.sealed")[0], 303)
        self.assertEqual(self.get("/api/login", method="POST", body={"username": "dad", "password": p3.PW})[0], 423)
        # locked by hand: the hub doesn't undo it
        self.hub_says = self.key
        server.node_lock_tick()
        self.assertTrue(server.LOCK["locked"])
        # the passphrase opens it
        st, body, _ = self.get("/unlock", method="POST", form={"passphrase": "not it at all"})
        self.assertIn("isn&#x27;t right", body)
        st, body, h = self.get("/unlock", method="POST", form={"passphrase": PASS})
        self.assertEqual((st, h["Location"]), (303, "/"))
        cookie = self.login()
        self.assertIn("Beans", self.get("/api/state", cookie)[1])
        st, body, _ = self.get("/store/plan/family-plan.html")
        self.assertEqual((st, body), (200, "Meet at the school"))
        self.assertEqual(self.get("/store/plan/family-plan.html.sealed")[0], 404)

    def test_restart_at_home_unlocks_and_tries_are_limited(self):
        self.lock_up()
        server.LOCK["held"] = False  # a restart: not locked by hand any more
        for _ in range(5):
            self.get("/unlock", method="POST", form={"passphrase": "wrong guess number"})
        st, body, _ = self.get("/unlock", method="POST", form={"passphrase": PASS})
        self.assertIn("Too many tries", body)
        self.hub_says = self.key
        server.node_lock_tick()
        self.assertFalse(server.LOCK["locked"])
        self.assertIn("Beans", self.get("/api/state", self.login())[1])

    def test_lock_now_from_the_app(self):
        self.lock_up()
        self.get("/unlock", method="POST", form={"passphrase": PASS})
        cookie = self.login()
        self.assertEqual(json.loads(self.get("/api/state", cookie)[1])["node_lock"], "on")
        self.assertEqual(self.get("/api/lock", cookie, "POST", {})[0], 200)
        import time
        time.sleep(1)
        self.assertTrue(server.LOCK["locked"])
        self.assertEqual(self.get("/api/state", cookie)[0], 423)

    def test_cli_with_a_sealed_vault(self):
        self.lock_up()
        calls = []
        real = sync.run
        sync.run = lambda *a, **k: calls.append(k)
        try:
            cli.sync_cmd(str(self.db), ["--token-file", "/nonexistent"])
        finally:
            sync.run = real
        self.assertEqual(calls, [{"files": True, "records": False}])
        self.assertFalse(self.db.exists())  # the CLI didn't make a new plain database
        with self.assertRaises(SystemExit):
            cli.user_cmd(str(self.db), ["add", "mom"])


if __name__ == "__main__":
    unittest.main()
