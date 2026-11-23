"""The optional AI connector: off by default, then discovery, registration, sign-in with PKCE, tokens, tools, limits."""

import base64
import hashlib
import json
import sys
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import ai, auth, mcp, mfa  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402

CB = "https://assistant.example/api/mcp/auth_callback"
ASSISTANT_IP = "198.51.100.10"
APPS = {"redirects": [CB], "origins": ["https://assistant.example"], "egress": ["198.51.100.0/24"]}


class McpTests(unittest.TestCase):
    def setUp(self):
        self.hub = p3.Hub()
        st = self.hub.store
        ai.save_settings(st, {"connector": True, "connector_apps": APPS})
        auth.create_user(st, "dad", p3.PW, "admin")
        self.garage = st.save_location({"name": "Garage shelf", "kind": "home", "counts_home": 1})
        st.save_item({"name": "Glock 19", "category": "firearms", "quantity": 1, "unit": "", "per_unit": 0,
                      "essential": "", "location_id": None, "expires": "", "notes": "", "subtype": "firearm", "serial": "ABC123"})
        self.opener = urllib.request.build_opener(p3.NoRedirect)

    def tearDown(self):
        self.hub.close()

    def call(self, method, path, body=None, headers=None, form=False):
        h = {"CF-Connecting-IP": ASSISTANT_IP, "X-Forwarded-Proto": "https", "Host": "bunkr.example"} | (headers or {})
        data = None
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                h["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.hub.url + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(r) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def connect(self):
        st, hd, _ = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(st, 401)
        self.assertIn('resource_metadata="https://bunkr.example/.well-known/oauth-protected-resource/mcp"', hd["WWW-Authenticate"])
        prm = json.loads(self.call("GET", "/.well-known/oauth-protected-resource/mcp")[2])
        self.assertEqual(prm["resource"], "https://bunkr.example/mcp")
        asm = json.loads(self.call("GET", "/.well-known/oauth-authorization-server")[2])
        self.assertIn("S256", asm["code_challenge_methods_supported"])
        # registration only for a listed AI app's callback
        self.assertEqual(self.call("POST", "/mcp/oauth/register", {"redirect_uris": ["https://evil.example/cb"]})[0], 400)
        st, _, b = self.call("POST", "/mcp/oauth/register", {"redirect_uris": [CB], "client_name": "Assistant",
                                                             "token_endpoint_auth_method": "none"})
        self.assertEqual(st, 201)
        cid = json.loads(b)["client_id"]
        verifier = "v" * 50
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        q = {"response_type": "code", "client_id": cid, "redirect_uri": CB, "code_challenge": challenge,
             "code_challenge_method": "S256", "state": "xyz", "resource": "https://bunkr.example/mcp", "scope": "bunkr"}
        st, _, page = self.call("GET", "/mcp/oauth/authorize?" + urllib.parse.urlencode(q), headers={"CF-Connecting-IP": "203.0.113.5"})
        self.assertEqual(st, 200)
        self.assertIn(b"Firearms or serial numbers", page)
        st, _, page = self.call("POST", "/mcp/oauth/authorize", q | {"username": "dad", "password": "wrong password!"}, form=True)
        self.assertIn(b"wrong username or password", page)
        st, hd, _ = self.call("POST", "/mcp/oauth/authorize", q | {"username": "dad", "password": p3.PW}, form=True)
        self.assertEqual(st, 303)
        loc = urllib.parse.urlparse(hd["Location"])
        self.assertEqual(f"{loc.scheme}://{loc.netloc}{loc.path}", CB)
        back = urllib.parse.parse_qs(loc.query)
        self.assertEqual(back["state"], ["xyz"])
        code = back["code"][0]
        bad = self.call("POST", "/mcp/oauth/token", {"grant_type": "authorization_code", "code": code, "client_id": cid,
                                                       "redirect_uri": CB, "code_verifier": "x" * 50}, form=True)
        self.assertEqual(json.loads(bad[2])["error"], "invalid_grant")
        # the code was burned by the failed attempt; sign in again
        st, hd, _ = self.call("POST", "/mcp/oauth/authorize", q | {"username": "dad", "password": p3.PW}, form=True)
        code = urllib.parse.parse_qs(urllib.parse.urlparse(hd["Location"]).query)["code"][0]
        st, _, b = self.call("POST", "/mcp/oauth/token", {"grant_type": "authorization_code", "code": code, "client_id": cid,
                                                           "redirect_uri": CB, "code_verifier": verifier}, form=True)
        self.assertEqual(st, 200)
        tok = json.loads(b)
        self.assertEqual(tok["token_type"], "Bearer")
        return cid, tok

    def test_sign_in_asks_for_the_second_step(self):
        import time
        uid = auth.user_by_name(self.hub.store, "dad")["id"]
        r = mfa.start_totp(self.hub.store, uid, "dad")
        key = r["key"].replace(" ", "")
        secret = base64.b32decode(key + "=" * (-len(key) % 8))
        mfa.confirm_totp(self.hub.store, uid, mfa.totp(secret, int(time.time() // 30)))
        st, _, b = self.call("POST", "/mcp/oauth/register", {"redirect_uris": [CB], "token_endpoint_auth_method": "none"})
        cid = json.loads(b)["client_id"]
        q = {"response_type": "code", "client_id": cid, "redirect_uri": CB, "code_challenge": "c" * 43,
             "code_challenge_method": "S256", "state": "s", "resource": "https://bunkr.example/mcp", "scope": "bunkr"}
        st, _, page = self.call("POST", "/mcp/oauth/authorize", q | {"username": "dad", "password": p3.PW}, form=True)
        self.assertEqual(st, 200)
        self.assertIn(b"add the code from your authenticator app", page)
        st, _, page = self.call("POST", "/mcp/oauth/authorize", q | {"username": "dad", "password": p3.PW, "code": "000000"}, form=True)
        self.assertIn(b"that code doesn&#x27;t match", page)
        code = mfa.totp(secret, int(time.time() // 30) + 1)
        st, hd, _ = self.call("POST", "/mcp/oauth/authorize", q | {"username": "dad", "password": p3.PW, "code": code}, form=True)
        self.assertEqual(st, 303)

    def rpc(self, token, method, params=None, mid=1):
        st, _, b = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}},
                             headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"})
        self.assertEqual(st, 200, b)
        return json.loads(b)

    def tool(self, token, name, args):
        r = self.rpc(token, "tools/call", {"name": name, "arguments": args})["result"]
        return r["isError"], r["content"][0]["text"]

    def test_sub_locations_through_the_connector(self):
        _, tok = self.connect()
        a = tok["access_token"]
        self.assertFalse(self.tool(a, "add_location", {"name": "Under bed", "kind": "home", "parent": "Garage shelf"})[0])
        err, text = self.tool(a, "add_location", {"name": "Safe", "kind": "home", "parent": "garage shelf > under bed"})
        self.assertFalse(err, text)
        err, text = self.tool(a, "add_location", {"name": "Too deep", "kind": "home", "parent": "Garage shelf > Under bed > Safe"})
        self.assertTrue(err)
        self.assertIn("two levels", text)
        self.assertIn("Garage shelf > Under bed > Safe (home", self.tool(a, "list_locations", {})[1])
        err, text = self.tool(a, "add_item", {"name": "Spare key", "category": "other", "quantity": 1,
                                              "location": "Garage shelf > Under bed > Safe"})
        self.assertFalse(err, text)
        self.assertIn("Spare key", self.tool(a, "search_inventory", {"location": "Garage shelf"})[1])  # a room finds what's inside
        self.assertIn("Garage shelf > Under bed > Safe", self.tool(a, "search_inventory", {"query": "spare"})[1])
        err, text = self.tool(a, "move_items", {"ids": [int(self.hub.store.items()[-1]["id"])], "location": "Garage shelf > Under bed"})
        self.assertFalse(err, text)

    def test_full_flow_and_tools(self):
        cid, tok = self.connect()
        a = tok["access_token"]
        init = self.rpc(a, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        st, _, _ = self.call("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                             headers={"Authorization": f"Bearer {a}"})
        self.assertEqual(st, 202)
        names = {t["name"]: t for t in self.rpc(a, "tools/list")["result"]["tools"]}
        self.assertIn("add_item", names)
        self.assertTrue(names["readiness"]["annotations"]["readOnlyHint"])
        self.assertNotIn("firearms", json.dumps(names["add_item"]["inputSchema"]))
        err, text = self.tool(a, "add_item", {"name": "Bottled water", "category": "water", "quantity": 2, "unit": "cases",
                                              "pack_size": 24, "pack_unit": "bottles", "size": 500, "size_unit": "ml",
                                              "location": "garage shelf"})
        self.assertFalse(err, text)
        self.assertIn("6.34 gal", text)
        err, text = self.tool(a, "add_item", {"name": "Water", "category": "water", "quantity": 1, "location": "Garge"})
        self.assertTrue(err)
        self.assertIn("Known locations: Garage shelf", text)
        err, text = self.tool(a, "add_item", {"name": "AR", "category": "firearms", "quantity": 1})
        self.assertTrue(err)
        err, text = self.tool(a, "search_inventory", {})
        self.assertNotIn("Glock", text)
        item_id = int(text.split("]")[0].strip("["))
        err, text = self.tool(a, "update_item", {"id": item_id, "quantity": 3})
        self.assertIn("3 cases", text)
        err, text = self.tool(a, "add_location", {"name": "4Runner", "kind": "vehicle", "people": 2})
        self.assertFalse(err, text)
        self.assertIn("not counted as home supply", text)
        self.assertTrue(self.tool(a, "add_location", {"name": "4runner", "kind": "vehicle"})[0])
        err, text = self.tool(a, "add_location", {"name": "Basement store room", "kind": "home"})
        self.assertIn("counts toward home supply", text)
        loc_change = int(self.tool(a, "recent_changes", {})[1].splitlines()[0].split("]")[0].strip("["))
        self.tool(a, "undo_change", {"change_id": loc_change})
        self.assertNotIn("Basement", self.tool(a, "list_locations", {})[1])
        self.assertIn("4Runner (vehicle", self.tool(a, "list_locations", {})[1])
        err, text = self.tool(a, "add_items", {"location": "4Runner", "items": [
            {"name": "Jumper cables", "category": "vehicle", "quantity": 1},
            {"name": "Water jug", "category": "water", "quantity": 2, "size": 1, "size_unit": "gal"},
            {"name": "Bad", "category": "water", "quantity": 1, "location": "Nowhere"}]})
        self.assertTrue(err)  # one bad row: nothing saved
        self.assertEqual(self.tool(a, "search_inventory", {"location": "4Runner"})[1], "No matching items.")
        err, text = self.tool(a, "add_items", {"location": "4Runner", "items": [
            {"name": "Jumper cables", "category": "vehicle", "quantity": 1},
            {"name": "Water jug", "category": "water", "quantity": 2, "size": 1, "size_unit": "gal"}]})
        self.assertFalse(err, text)
        self.assertIn("Added 2 items", text)
        bulk = int([l for l in self.tool(a, "recent_changes", {})[1].splitlines() if "add_items" in l][0].split("]")[0].strip("["))
        self.tool(a, "undo_change", {"change_id": bulk})
        self.assertEqual(self.tool(a, "search_inventory", {"location": "4Runner"})[1], "No matching items.")
        err, text = self.tool(a, "readiness", {})
        self.assertIn("Overall readiness", text)
        changes = self.tool(a, "recent_changes", {})[1]
        self.assertIn("undoable", changes)
        upd = int([l for l in changes.splitlines() if "update_item" in l][0].split("]")[0].strip("["))
        self.tool(a, "undo_change", {"change_id": upd})
        self.assertIn("2 cases", self.tool(a, "search_inventory", {"query": "bottled"})[1])
        add = int([l for l in self.tool(a, "recent_changes", {})[1].splitlines() if " add_item:" in l][0].split("]")[0].strip("["))
        self.tool(a, "undo_change", {"change_id": add})
        self.assertEqual(self.tool(a, "search_inventory", {"query": "bottled"})[1], "No matching items.")
        # refresh rotates
        st, _, b = self.call("POST", "/mcp/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                                           "client_id": cid}, form=True)
        self.assertEqual(st, 200)
        st, _, b = self.call("POST", "/mcp/oauth/token", {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                                           "client_id": cid}, form=True)
        self.assertEqual(json.loads(b)["error"], "invalid_grant")

    def test_situation_family_and_brief_tools(self):
        from bunkr import brief, checkins, situation
        import uuid
        st = self.hub.store
        dad = next(u["id"] for u in auth.users(st) if u["username"] == "dad")
        st._exec("INSERT INTO people(name, role, uid, user_id) VALUES('Alex','adult',?,?)", (uuid.uuid4().hex, dad))
        st._exec("INSERT INTO people(name, role, uid, remote) VALUES('Grandma','adult',?,1)", (uuid.uuid4().hex,))
        st._exec("INSERT INTO sites(name, kind, address) VALUES('Grandma''s', 'relative', '9 Elm St')")
        situation.save(st, "nws", [situation._item("nws", "w1", "weather", "flash", "Tornado Warning", site="Home", km=0, scenario="tornado"),
                                   situation._item("kev", "c1", "cyber", "routine", "Some flaw")])
        _, tok = self.connect()
        a = tok["access_token"]
        names = {t["name"] for t in self.rpc(a, "tools/list")["result"]["tools"]}
        self.assertTrue({"situation_now", "family_board", "check_in", "send_family_call", "end_family_call", "morning_brief"} <= names)
        err, text = self.tool(a, "situation_now", {})
        self.assertIn("[Act now] Tornado Warning (at Home)", text)
        self.assertNotIn("Some flaw", text)
        self.assertIn("Some flaw", self.tool(a, "situation_now", {"tiers": ["routine"]})[1])
        err, text = self.tool(a, "check_in", {"status": "safe", "note": "at work"})
        self.assertFalse(err, text)
        self.assertIn("Checked Alex in", text)
        self.assertTrue(self.tool(a, "check_in", {"status": "safe", "person": "Nobody"})[0])
        self.assertIn("Grandma", self.tool(a, "check_in", {"status": "help", "person": "Grandma"})[1])
        err, text = self.tool(a, "send_family_call", {"kind": "evacuate", "place": "Grandma's", "message": "Take 285"})
        self.assertFalse(err, text)
        board = self.tool(a, "family_board", {})[1]
        self.assertIn("FAMILY CALL: Evacuate to Grandma's (9 Elm St)", board)
        self.assertIn("Alex: I'm safe", board)
        self.assertTrue(self.tool(a, "send_family_call", {"kind": "meet"})[0])  # where?
        self.assertIn("All clear sent", self.tool(a, "end_family_call", {})[1])
        self.assertIn("No brief yet", self.tool(a, "morning_brief", {})[1])
        brief.save(st, {"id": "1", "created_at": brief.now(), "near": [], "groups": [], "outlets": {},
                        "ai": {"lead": "Summit day.", "stories": [{"title": "Summit", "agree": "They met.", "differ": [{"who": "Chinese state media", "framing": "A landmark."}],
                                                                   "unconfirmed": "", "matters": "", "sources": []}]}}, "2026-11-20")
        self.assertIn("- Chinese state media: A landmark.", self.tool(a, "morning_brief", {})[1])
        change = int([l for l in self.tool(a, "recent_changes", {})[1].splitlines() if " check_in:" in l][-1].split("]")[0].strip("["))
        self.tool(a, "undo_change", {"change_id": change})
        self.assertEqual(len(st._rows("SELECT * FROM checkins")), 1)

    def test_outside_listed_apps_and_bad_tokens_are_refused(self):
        _, tok = self.connect()
        st, _, _ = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                             headers={"Authorization": f"Bearer {tok['access_token']}", "Origin": "https://assistant.example"})
        self.assertEqual(st, 200)
        st, _, _ = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                             headers={"Authorization": f"Bearer {tok['access_token']}", "CF-Connecting-IP": "203.0.113.5"})
        self.assertEqual(st, 403)
        st, _, _ = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                             headers={"Authorization": f"Bearer {tok['access_token']}", "Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        st, _, _ = self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": "Bearer nope"})
        self.assertEqual(st, 401)
        self.assertEqual(self.call("GET", "/mcp")[0], 405)
        # the rest of BUNKR still needs a normal login
        self.assertEqual(self.call("GET", "/api/state", headers={"Authorization": f"Bearer {tok['access_token']}"})[0], 401)


class AppListTests(unittest.TestCase):
    """Which AI apps may connect is the admin's list: exact, https only, nothing wildcarded."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "a.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_install_lists_none(self):
        self.assertEqual(mcp.apps(self.st), {"redirects": [], "origins": [], "egress": []})
        with self.assertRaises(mcp.OAuthError):
            mcp.register(self.st, {"redirect_uris": [CB]})

    def test_an_upgraded_install_keeps_its_signed_up_apps(self):
        self.st._exec("INSERT INTO mcp_clients(client_id, name, redirect_uris, created_at) VALUES(?,?,?,?)",
                      ("c", "x", json.dumps([CB, "javascript:alert(1)"]), "2026-11-19"))
        a = mcp.apps(self.st)
        self.assertEqual(a, {"redirects": [CB], "origins": ["https://assistant.example"], "egress": []})
        self.assertTrue(mcp.from_assistant(self.st, "203.0.113.5"))       # no networks listed: not checked
        self.assertEqual(mcp.register(self.st, {"redirect_uris": [CB]})["redirect_uris"], [CB])

    def test_checks(self):
        for bad in ({"redirects": ["http://assistant.example/cb"]}, {"redirects": ["https://*.example/cb"]},
                    {"redirects": ["https://user@assistant.example/cb"]}, {"redirects": ["https://a.example/cb#x"]},
                    {"redirects": ["https://a.example/cb; script-src *"]}, {"origins": ["https://*.example"]},
                    {"origins": ["https://a.example/path"]}, {"origins": ["*"]}, {"egress": ["0.0.0.0/0"]},
                    {"egress": ["not a network"]}, {"redirects": [f"https://a{i}.example/cb" for i in range(30)]}):
            with self.assertRaises(ValueError, msg=bad):
                mcp.save_apps(self.st, bad)
        a = mcp.save_apps(self.st, {"redirects": CB + "\n", "origins": ["https://Assistant.example/"], "egress": ["198.51.100.7/24"]})
        self.assertEqual(a, {"redirects": [CB], "origins": ["https://assistant.example"], "egress": ["198.51.100.0/24"]})
        self.assertEqual(mcp.origins(self.st), ["https://assistant.example"])
        self.assertTrue(mcp.from_assistant(self.st, ""))
        self.assertTrue(mcp.from_assistant(self.st, "198.51.100.9"))
        self.assertFalse(mcp.from_assistant(self.st, "203.0.113.5"))

    def test_a_removed_app_cant_sign_in(self):
        mcp.save_apps(self.st, {"redirects": [CB]})
        cid = mcp.register(self.st, {"redirect_uris": [CB]})["client_id"]
        q = {"client_id": cid, "redirect_uri": CB, "response_type": "code", "code_challenge_method": "S256", "code_challenge": "c" * 43}
        mcp.check_authorize(self.st, q)
        mcp.save_apps(self.st, {"redirects": []})
        with self.assertRaises(mcp.OAuthError):
            mcp.check_authorize(self.st, q)


if __name__ == "__main__":
    unittest.main()
