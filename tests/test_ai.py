"""Optional AI: off on a new install, kept on for an install that used it, one place to turn it on, providers."""

import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import ai, auth, brief  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = Store(Path(self.tmp.name) / "a.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_install_is_off(self):
        s = ai.settings(self.st)
        self.assertEqual((s["summaries"], s["connector"], s["provider"]), (False, False, "ollama"))
        self.assertIsNone(ai.summarizer(self.st))
        with self.assertRaises(ai.AIOff):
            ai.reply(self.st, "hi")

    def test_an_install_that_used_ai_keeps_it(self):
        self.st._exec("INSERT INTO briefs(uid, day, created_at, data, ai) VALUES('b', '2026-11-21', '2026-11-21T06:30', '{}', 1)")
        self.st._exec("INSERT INTO mcp_clients(client_id, name, redirect_uris, created_at) VALUES('c', 'x', '[]', '2026-11-19')")
        s = ai.settings(self.st)
        self.assertEqual((s["summaries"], s["connector"], s["provider"]), (True, True, "cli"))
        ai.save_settings(self.st, {"connector": False})
        self.assertFalse(ai.settings(self.st)["connector"])    # once saved, the choice sticks

    def test_checks(self):
        for bad in ({"provider": "skynet"}, {"ollama_url": "file:///etc/passwd"}, {"model": "a b; rm"}):
            with self.assertRaises(ValueError):
                ai.save_settings(self.st, bad)
        v = ai.save_settings(self.st, {"summaries": True, "provider": "ollama", "ollama_url": "http://192.168.1.9:11434/", "model": "qwen2.5:7b"})
        self.assertEqual((v["ollama_url"], v["model"]), ("http://192.168.1.9:11434", "qwen2.5:7b"))

    def test_ollama(self):
        ai.save_settings(self.st, {"summaries": True, "provider": "ollama"})
        sent = []

        def fake(req, timeout=None):
            sent.append(json.loads(req.data))
            return io.BytesIO(json.dumps({"response": "ready"}).encode())
        with mock.patch.object(urllib.request, "urlopen", fake):
            self.assertEqual(ai.summarizer(self.st)("say ready"), "ready")
        self.assertEqual((sent[0]["model"], sent[0]["prompt"], sent[0]["stream"]), ("llama3.1:8b", "say ready", False))

        def down(req, timeout=None):
            raise urllib.error.URLError("refused")
        with mock.patch.object(urllib.request, "urlopen", down):
            with self.assertRaises(RuntimeError):
                ai.reply(self.st, "x")
        self.assertIn("couldn't reach Ollama", ai.view(self.st)["status"]["error"])

    def test_cli(self):
        ai.save_settings(self.st, {"summaries": True, "provider": "cli"})
        old = dict(ai.CLI)
        try:
            ai.CLI.update(command="tr a-z A-Z")      # any command that reads stdin and prints a reply
            self.assertEqual(ai.reply(self.st, "ready\n").strip(), "READY")
            ai.CLI.update(command="no-such-ai-tool -p")
            with self.assertRaises(RuntimeError):
                ai.reply(self.st, "x")
        finally:
            ai.CLI.update(old)

    def test_brief_without_ai(self):
        rec = brief.build(self.st, [], ai=None, opener=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
        self.assertIsNone(rec["ai"])


class HttpTests(unittest.TestCase):
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

    def tearDown(self):
        self.hub.close()

    def req(self, method, path, body=None, user="dad", raw=False):
        h = {"X-Bunkr": "1", "Content-Type": "application/json"}
        if user:
            h["Cookie"] = f"bunkr_session={self.c[user]}"
        r = urllib.request.Request(self.hub.url + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with self.opener.open(r) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_admin_only_and_connector_off_by_default(self):
        self.assertEqual(self.req("GET", "/api/ai", user="kid")[0], 403)
        self.assertEqual(self.req("GET", "/.well-known/oauth-authorization-server", user=None)[0], 404)   # connector off: not there
        self.assertEqual(self.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, user=None)[0], 404)
        code, v = self.req("POST", "/api/ai", {"connector": True})
        self.assertEqual((code, v["connector"]), (200, True))
        self.assertEqual(self.req("GET", "/.well-known/oauth-authorization-server", user=None)[0], 200)
        self.assertEqual(self.req("POST", "/api/ai/test")[0], 400)          # summaries off: the test says so


if __name__ == "__main__":
    unittest.main()
