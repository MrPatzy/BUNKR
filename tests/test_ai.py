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
        self.assertEqual((ai.view(self.st)["cli"], ai.view(self.st)["command_source"]), ("", ""))   # a new install has none
        with self.assertRaises(RuntimeError):
            ai.reply(self.st, "x")
        old = dict(ai.CLI)
        try:
            ai.CLI.update(command="tr a-z A-Z")      # --ai-cli: any command that reads stdin and prints a reply
            self.assertEqual(ai.reply(self.st, "ready\n").strip(), "READY")
            self.assertEqual(ai.view(self.st)["command_source"], "option")
            ai.CLI.update(command="no-such-ai-tool -p")
            with self.assertRaises(RuntimeError):
                ai.reply(self.st, "x")
        finally:
            ai.CLI.update(old)

    def test_command_from_the_login_file(self):
        ai.save_settings(self.st, {"summaries": True, "provider": "cli"})
        (Path(self.tmp.name) / "ai.env").write_text("# the hub's AI tool\nBUNKR_AI_CLI=\"env\"\nTOKEN=abc\n")
        v = ai.view(self.st)
        self.assertEqual((v["command"], v["command_source"], v["cli"]), ("env", "login file", "env"))
        out = ai.reply(self.st, "").splitlines()
        self.assertIn("TOKEN=abc", out)
        self.assertFalse([x for x in out if x.startswith("BUNKR_AI_CLI")])     # the command line isn't passed on to the tool
        old = dict(ai.CLI)
        try:
            ai.CLI.update(command="tr a-z A-Z")                       # --ai-cli wins over the login file
            self.assertEqual(ai.reply(self.st, "ok").strip(), "OK")
        finally:
            ai.CLI.update(old)

    def test_command_cant_be_set_from_the_web(self):
        for c in ("llm -m mistral", "", "sh -c id"):
            with self.assertRaises(ValueError):
                ai.save_settings(self.st, {"summaries": True, "command": c})
        self.assertFalse(ai.settings(self.st)["summaries"])           # nothing of a refused save is kept
        self.assertEqual(ai.command(self.st), "")


class EarlierVersionTests(unittest.TestCase):
    """A hub upgraded from 1.1 keeps doing what it did: old option, old login file and home folder, old command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.st = Store(self.dir / "a.db")
        self.old = dict(ai.CLI)

    def tearDown(self):
        ai.CLI.update(self.old)
        self.tmp.cleanup()

    def test_settings_saved_by_1_1_keep_their_command(self):
        self.st.set_raw("ai", {"summaries": True, "connector": False, "provider": "cli"})
        s = ai.settings(self.st)
        self.assertEqual((s["summaries"], s["provider"], s["command"]), (True, "cli", ai.LEGACY_CLI))
        self.assertEqual(ai.command(self.st), ai.LEGACY_CLI)
        self.st.set_raw("ai", {"summaries": False, "connector": True, "provider": "ollama"})
        self.assertEqual(ai.settings(self.st)["command"], "")

    def test_an_install_from_before_the_setting_keeps_its_command(self):
        self.st._exec("INSERT INTO briefs(uid, day, created_at, data, ai) VALUES('b', '2026-11-21', '2026-11-21T06:30', '{}', 1)")
        self.assertEqual(ai.command(self.st), ai.LEGACY_CLI)

    def test_old_option_names_the_program(self):
        ai.configure("", "", "/opt/tool/bin/thing")
        self.assertEqual(ai.CLI["command"].split()[0], "/opt/tool/bin/thing")
        self.assertEqual(ai.CLI["command"].split()[1:], ai.LEGACY_CLI.split()[1:])
        ai.configure("llm -m mistral", "/x/ai.env", "/opt/tool/bin/thing")     # --ai-cli wins
        self.assertEqual(ai.CLI, {"command": "llm -m mistral", "env_file": "/x/ai.env"})
        ai.configure()
        self.assertEqual(ai.CLI, {"command": "", "env_file": ""})

    def test_server_still_takes_the_old_options(self):
        import argparse
        from bunkr import server
        seen = {}

        def stop(args):
            seen.update(vars(args))
            raise SystemExit(0)
        with mock.patch.object(sys, "argv", ["bunkr", ai.LEGACY_FLAG, "/usr/local/bin/thing", "--brief-env-file", "/x/old.env"]), \
                mock.patch.object(argparse.ArgumentParser, "parse_args", lambda self: stop(argparse.ArgumentParser.parse_known_args(self)[0])):
            with self.assertRaises(SystemExit):
                server.main()
        self.assertEqual((seen["legacy_cli"], seen["ai_env_file"]), ("/usr/local/bin/thing", "/x/old.env"))

    def test_old_login_file_and_home_folder(self):
        self.assertEqual(ai._env_file(self.st), "")
        (self.dir / ai.LEGACY_ENV).write_text("TOKEN=old\n")
        self.assertEqual(ai._env_file(self.st), str(self.dir / ai.LEGACY_ENV))
        (self.dir / "ai.env").write_text("TOKEN=new\n")
        self.assertEqual(ai._env_file(self.st), str(self.dir / "ai.env"))      # the new name wins when both are there
        (self.dir / "ai.env").unlink()
        (self.dir / ai.LEGACY_HOME).mkdir()
        (self.dir / ai.LEGACY_ENV).write_text("TOKEN=old\nBUNKR_AI_CLI=printenv TOKEN HOME\n")
        ai.save_settings(self.st, {"summaries": True, "provider": "cli"})
        out = ai.reply(self.st, "").split()
        self.assertEqual(out, ["old", str(self.dir / ai.LEGACY_HOME)])
        self.assertFalse((self.dir / "ai-home").exists())

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
        code, v = self.req("POST", "/api/ai", {"summaries": True, "provider": "cli", "command": "id"})
        self.assertEqual(code, 400)                                          # the hub's command isn't set from the web
        self.assertIn("set on the hub", v["error"])
        self.assertEqual(ai.command(self.hub.store), "")
        self.assertFalse(ai.settings(self.hub.store)["summaries"])


if __name__ == "__main__":
    unittest.main()
