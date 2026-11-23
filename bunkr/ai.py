"""Optional AI. Everything in BUNKR works without it, and a new install has it off.

An admin turns it on in one place, the "AI (optional)" card on the Household tab. Two things can use it, each
switched on separately:

- Summaries: the morning brief's story write-ups (where outlets agree and differ) and sorting new stories by area
  and topic. Without AI the brief still groups and ranks headlines, offline included.
- Connector (MCP): lets an AI assistant read your inventory and add or move items, after you sign in and approve
  it. Narrow on purpose (see mcp.py and mcp_tools.py); firearms never go through it.

Where the AI runs (the "provider"):
- "ollama": a model on your own computer through Ollama (free, nothing leaves your network).
- "cli": a command-line AI tool on the hub that reads a prompt and prints a reply, with its tools switched off.
  The command is set on the hub, never from the web: the --ai-cli option, or a BUNKR_AI_CLI= line in the AI login
  file (--ai-env-file, by default ai.env next to the database). None is set on a new install.

The prompt and the reply are plain text. Headlines are other people's words, fenced in the prompt, never
instructions; replies are parsed and checked, never run.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

PROVIDERS = {"ollama": "A local model (Ollama)", "cli": "A command-line AI tool on the hub"}
DEFAULT_OLLAMA = "http://127.0.0.1:11434"
# the command-line tool: reads the prompt on stdin, prints the reply, e.g. "llm -m mistral" or "ollama run llama3.1".
# None by default. Only the hub sets it (an admin web login must not pick a program the hub runs): --ai-cli, else
# BUNKR_AI_CLI= in the login file. CLI holds the hub's options (server.main).
DEFAULT_CLI = ""
CLI = {"command": DEFAULT_CLI, "env_file": ""}
# Names from earlier versions (up to 1.1), kept so an upgraded hub carries on as it was: its old option for the
# tool's program, its login file and home folder, and the command it ran when none was set. New installs never use them.
LEGACY_FLAG = "--claude"
LEGACY_ENV = "claude.env"
LEGACY_HOME = "claude-home"
LEGACY_CLI = "claude -p --tools '' --strict-mcp-config --no-session-persistence --output-format text"


def configure(command="", env_file="", legacy_program=""):
    """The hub's options: --ai-cli COMMAND, --ai-env-file FILE, and the old option that named only the program."""
    if not command and legacy_program:
        command = shlex.join([legacy_program] + shlex.split(LEGACY_CLI)[1:])
    CLI.update(command=command or DEFAULT_CLI, env_file=env_file or "")


class AIOff(RuntimeError):
    pass


def _used_before(store):
    """An install from before AI was a setting: keep what it was doing (AI summaries made, a connector signed in)."""
    try:
        briefs = store._rows("SELECT 1 FROM briefs WHERE ai=1 LIMIT 1")
        clients = store._rows("SELECT 1 FROM mcp_clients LIMIT 1")
    except Exception:
        return False, False
    return bool(briefs), bool(clients)


def settings(store):
    s = store.get_raw("ai", None)
    if not isinstance(s, dict):
        summaries, connector = _used_before(store)
        s = {"summaries": summaries, "connector": connector, "provider": "cli" if summaries else "ollama"}
    if "command" not in s:
        # saved by an earlier version, which ran its own default command: a hub that ran the tool keeps the one it ran.
        # Only ever set here; the API can't change it.
        s["command"] = LEGACY_CLI if s.get("provider") == "cli" else ""
        store.set_raw("ai", s)
    return {"summaries": bool(s.get("summaries")), "connector": bool(s.get("connector")),
            "provider": s.get("provider") if s.get("provider") in PROVIDERS else "ollama",
            "ollama_url": str(s.get("ollama_url") or DEFAULT_OLLAMA), "model": str(s.get("model") or "llama3.1:8b"),
            "command": str(s.get("command") or "")}


ENV_KEY = "BUNKR_AI_CLI"   # the line in the login file that names the command (not passed on to the tool)


def _login(store):
    """The login file's lines as {name: value} ({} when there's none)."""
    f = _env_file(store)
    out = {}
    if f:
        for line in Path(f).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                out[k.strip()] = v.strip()
    return out


def command_source(store):
    """(command, where it was set: "option", "login file", "earlier version" or "") - all on the hub side."""
    if CLI["command"]:
        return CLI["command"], "option"
    c = _login(store).get(ENV_KEY, "")
    if len(c) > 1 and c[0] == c[-1] and c[0] in "'\"":
        c = c[1:-1]          # BUNKR_AI_CLI="llm -m mistral"
    if c:
        return c, "login file"
    c = settings(store)["command"]
    return c, "earlier version" if c else ""


def command(store):
    """The command-line tool to run ("" when none is set on the hub)."""
    return command_source(store)[0]


def save_settings(store, data):
    s = settings(store)
    for k in ("summaries", "connector"):
        if k in data:
            s[k] = bool(data[k])
    if "provider" in data:
        if data["provider"] not in PROVIDERS:
            raise ValueError("pick where the AI runs from the list")
        s["provider"] = data["provider"]
    if "ollama_url" in data:
        u = str(data["ollama_url"] or "").strip().rstrip("/") or DEFAULT_OLLAMA
        if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(:\d+)?(/[\w\-./]*)?", u):
            raise ValueError("the Ollama address looks like http://127.0.0.1:11434")
        s["ollama_url"] = u
    if "model" in data:
        m = str(data["model"] or "").strip()
        if not re.fullmatch(r"[\w.\-:/]{1,80}", m):
            raise ValueError("the model name looks like llama3.1:8b")
        s["model"] = m
    if "command" in data:
        raise ValueError(f"the AI command is set on the hub (--ai-cli, or {ENV_KEY}= in ai.env), not here")
    if "connector_apps" in data:
        from . import mcp
        mcp.save_apps(store, data["connector_apps"])
    store.set_raw("ai", s)
    return view(store)


def _env_file(store):
    if CLI["env_file"]:
        return CLI["env_file"]
    folder = Path(store.path).parent
    for name in ("ai.env", LEGACY_ENV):   # the old name when there's no new one
        if (folder / name).exists():
            return str(folder / name)
    return ""


def view(store):
    from . import mcp
    s = settings(store)
    cmd, source = command_source(store)
    try:
        prog = shlex.split(cmd)[0] if cmd else ""
    except ValueError:
        prog = ""
    return s | {"providers": PROVIDERS, "command": cmd, "command_source": source, "command_hint": f"--ai-cli, or {ENV_KEY}= in ai.env",
                "cli_found": bool(prog and shutil.which(prog)), "cli": prog, "cli_login": bool(_env_file(store)),
                "connector_apps": mcp.apps(store), "status": store.get_raw("ai_status", {}) or {}}


def summaries_on(store):
    return settings(store)["summaries"]


def connector_on(store):
    return settings(store)["connector"]


def _ollama(s, prompt, timeout):
    body = json.dumps({"model": s["model"], "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(s["ollama_url"] + "/api/generate", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return str(json.loads(r.read(4_000_000)).get("response") or "")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Ollama said {exc.code}: is the model '{s['model']}' pulled?") from None
    except OSError as exc:
        raise RuntimeError(f"couldn't reach Ollama at {s['ollama_url']} ({type(exc).__name__})") from None


def _cli(store, prompt, timeout):
    """The command-line tool: text in, text out (the command should switch the tool's own tools off)."""
    home = str(Path(store.path).parent / "ai-home")
    if not os.path.isdir(home) and os.path.isdir(Path(store.path).parent / LEGACY_HOME):
        home = str(Path(store.path).parent / LEGACY_HOME)   # where earlier versions kept it
    try:
        cmd = shlex.split(command(store))     # run as a word list, never through a shell
    except ValueError:
        raise RuntimeError("the AI command set on the hub has an unmatched quote") from None
    if not cmd:
        raise RuntimeError(f"no AI command is set on the hub (--ai-cli, or {ENV_KEY}= in ai.env); or pick Ollama")
    os.makedirs(home, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TZ")}
    env["HOME"] = home
    env.update({k: v for k, v in _login(store).items() if k != ENV_KEY})
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, env=env, cwd=home)
    except FileNotFoundError:
        raise RuntimeError(f"the AI command '{cmd[0]}' isn't installed on the hub") from None
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "the AI command failed").strip()[:300])
    return r.stdout


def reply(store, prompt, timeout=600):
    """The AI's reply to a prompt. Raises AIOff when summaries are off."""
    s = settings(store)
    if not s["summaries"]:
        raise AIOff("AI summaries are off")
    try:
        text = _ollama(s, prompt, timeout) if s["provider"] == "ollama" else _cli(store, prompt, timeout)
    except Exception as exc:
        store.set_raw("ai_status", {"ok": False, "error": str(exc)[:200]})
        raise
    store.set_raw("ai_status", {"ok": True})
    return text


def summarizer(store, timeout=600):
    """A prompt -> reply function when AI summaries are on, else None (the no-AI path)."""
    return (lambda prompt: reply(store, prompt, timeout)) if summaries_on(store) else None
