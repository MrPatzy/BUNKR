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
  The hub's options say which command (--ai-cli; the default is Claude Code) and which login file (--ai-env-file,
  default ai.env or claude.env next to the database).

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
# the command-line tool: reads the prompt on stdin, prints the reply. The default is Claude Code with every tool off;
# another tool works the same way (--ai-cli "llm -m mistral", --ai-cli "ollama run llama3.1"). Set by server.main.
DEFAULT_CLI = "claude -p --tools '' --strict-mcp-config --no-session-persistence --output-format text"
CLI = {"command": DEFAULT_CLI, "env_file": ""}


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
        store.set_raw("ai", s)
    return {"summaries": bool(s.get("summaries")), "connector": bool(s.get("connector")),
            "provider": s.get("provider") if s.get("provider") in PROVIDERS else "ollama",
            "ollama_url": str(s.get("ollama_url") or DEFAULT_OLLAMA), "model": str(s.get("model") or "llama3.1:8b")}


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
    store.set_raw("ai", s)
    return view(store)


def _env_file(store):
    if CLI["env_file"]:
        return CLI["env_file"]
    folder = Path(store.path).parent
    for name in ("ai.env", "claude.env"):
        if (folder / name).exists():
            return str(folder / name)
    return ""


def view(store):
    s = settings(store)
    return s | {"providers": PROVIDERS, "cli_found": bool(shutil.which(shlex.split(CLI["command"])[0])), "cli": shlex.split(CLI["command"])[0],
                "cli_login": bool(_env_file(store)), "status": store.get_raw("ai_status", {}) or {}}


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
    """The command-line tool with every tool off: text in, text out."""
    home = str(Path(store.path).parent / "ai-home")
    if not os.path.isdir(home) and os.path.isdir(Path(store.path).parent / "claude-home"):
        home = str(Path(store.path).parent / "claude-home")   # where earlier versions kept it
    os.makedirs(home, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TZ")}
    env["HOME"] = home
    f = _env_file(store)
    if f:
        for line in open(f):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v
    cmd = shlex.split(CLI["command"])
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
