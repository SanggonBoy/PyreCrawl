"""PyreCrawl CLI — serve the MCP server or register it with AI agents.

    pyrecrawl serve                 # stdio MCP server (what agents launch)
    pyrecrawl setup                 # one-time: install browser engines
    pyrecrawl install [agents...]   # write MCP config into detected agents
    pyrecrawl uninstall [agents...] # remove our entry from agent configs

Zero dependencies beyond the stdlib (argparse/json/re).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

_env = os.environ.get

def _is_dev_install() -> bool:
    """True when running from a git checkout (editable install) — keep the
    launcher pointed at the local repo so changes apply immediately.
    Priority: env-var > git checkout in CWD > git anywhere in file's ancestors."""
    if _env("PYRECRAWL_DEV"):
        return True
    # User runs `pyrecrawl install` from the project dir → check CWD for .git
    if (Path.cwd() / ".git").exists():
        return True
    # Fallback: walk up from __file__ to find .git
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / ".git").exists():
            return True
    return False


def _launch_cmd() -> list[str]:
    """How agents should launch the server. Dev = local venv; release = uvx."""
    if _is_dev_install():
        # Walk up from __file__ to find the repo root (has .git)
        here = Path(__file__).resolve()
        for p in here.parents:
            if (p / ".git").exists():
                py = p / ".venv" / "Scripts" / "python.exe"
                if py.exists():
                    return [str(py), "-m", "pyrecrawl.server"]
                # fallback: use the python that is running right now
                return [sys.executable, "-m", "pyrecrawl.server"]
        return [sys.executable, "-m", "pyrecrawl.server"]
    return ["uvx", "--from", "pyrecrawl", "pyrecrawl", "serve"]

SERVER_NAME = "pyrecrawl"
# _launch_cmd() returns the right command for dev vs release.
# On install, we call _launch_cmd() to get the current entry.


def _home() -> Path:
    return Path.home()


def _claude_desktop_config() -> Path:
    if sys.platform == "win32":
        return _home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return _home() / ".config" / "Claude" / "claude_desktop_config.json"


# ---------------------------------------------------------------------------
# Agent registry: name -> (detect, config_path, writer)
# A writer receives (path, command, args) and must merge our entry in-place,
# creating parents as needed. It returns a human-readable status string.
# ---------------------------------------------------------------------------

def _write_json_servers(path: Path, command: list[str], args: list[str],
                        root_key: str, entry_key: str) -> str:
    """Generic JSON config writer: {root_key: {entry_key: {command, args}}}."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            return f"REFUSED — {path} is not valid JSON ({e}); fix it manually"
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    servers = data.setdefault(root_key, {})
    if not isinstance(servers, dict):
        return f"REFUSED — '{root_key}' in {path} is not an object"
    entry: dict[str, Any] = {"command": command[0], "args": args}
    servers[SERVER_NAME] = entry
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"wrote {path}"


def _write_vscode(path: Path, command: list[str], args: list[str]) -> str:
    data: dict[str, Any] = {"command": command[0], "args": args, "type": "stdio"}
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            return f"REFUSED — {path} is not valid JSON ({e})"
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    doc.setdefault("servers", {})[SERVER_NAME] = data
    path.write_text(json.dumps(doc, indent=4) + "\n", encoding="utf-8")
    return f"wrote {path}"


def _write_codex(path: Path, command: list[str], args: list[str]) -> str:
    """Codex uses ~/.codex/config.toml — append/replace a [mcp_servers.pyrecrawl] block."""
    block = (
        f"[mcp_servers.{SERVER_NAME}]\n"
        f'command = "{command[0]}"\n'
        f"args = [{', '.join(json.dumps(a) for a in args)}]\n"
    )
    if path.parent.exists() is False:
        path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text:
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    # drop any previous block of ours
    pattern = re.compile(
        rf"\[mcp_servers\.{SERVER_NAME}\][^\[]*", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub("", text).rstrip() + "\n\n"
    path.write_text((text.rstrip() + "\n\n" if text.strip() else "") + block, encoding="utf-8")
    return f"wrote {path}"


def _write_hermes(path: Path, command: list[str], args: list[str]) -> str:
    """Hermes uses config.yaml with an mcp_servers: map — append a stdio entry."""
    yaml_entry = (
        f"  {SERVER_NAME}:\n"
        f"    command: {command[0]}\n"
        f"    args:\n" + "".join(f"      - {a}\n" for a in args) +
        "    enabled: true\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if text:
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        # remove previous entry of ours (2-space indented under mcp_servers)
        text = re.sub(rf"\n  {SERVER_NAME}:\n(?:    .*\n|      .*\n)*", "\n", text)
        if "mcp_servers:" in text:
            # insert right after the mcp_servers: line
            text = text.replace("mcp_servers:", "mcp_servers:\n" + yaml_entry.rstrip("\n"), 1)
        else:
            text = text.rstrip("\n") + "\n\nmcp_servers:\n" + yaml_entry
    else:
        text = "mcp_servers:\n" + yaml_entry
    path.write_text(text, encoding="utf-8")
    return f"wrote {path}"


def _hermes_config_path() -> Path:
    """Pick the Hermes config: prefer one with mcp_servers block, then any existing, else default."""
    candidates = [
        _home() / "AppData" / "Local" / "hermes" / "config.yaml",
        _home() / ".hermes" / "config.yaml",
    ]
    for p in candidates:
        if p.exists() and "mcp_servers" in p.read_text(encoding="utf-8", errors="ignore"):
            return p
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def _write_claude_code(cwd: Path, command: list[str], args: list[str]) -> str:
    """Project-scoped .mcp.json in the current directory."""
    return _write_json_mcp(cwd / ".mcp.json", command, args)


def _write_json_mcp(path: Path, command: list[str], args: list[str]) -> str:
    return _write_json_servers(path, command, args, root_key="mcpServers", entry_key=SERVER_NAME)


AGENTS: dict[str, dict[str, Any]] = {
    "claude-desktop": {
        "detect": lambda: _claude_desktop_config().parent.exists(),
        "path": _claude_desktop_config,
        "write": lambda p, cmd, args: _write_json_mcp(p, cmd, args),
        "hint": "restart Claude Desktop fully (tray → Quit)",
    },
    "claude-code": {
        "detect": lambda: shutil.which("claude") is not None,
        "path": lambda: Path.cwd() / ".mcp.json",
        "write": lambda p, cmd, args: _write_json_mcp(p, cmd, args),
        "hint": "project-scoped; restart the claude session",
    },
    "cursor": {
        "detect": lambda: (_home() / ".cursor").exists(),
        "path": lambda: _home() / ".cursor" / "mcp.json",
        "write": lambda p, cmd, args: _write_json_mcp(p, cmd, args),
        "hint": "Cursor → Settings → MCP to verify",
    },
    "vscode": {
        "detect": lambda: shutil.which("code") is not None,
        "path": lambda: Path.cwd() / ".vscode" / "mcp.json",
        "write": _write_vscode,
        "hint": "project-scoped; Copilot Chat → Install MCP Server",
    },
    "codex": {
        "detect": lambda: (_home() / ".codex").exists() or shutil.which("codex") is not None,
        "path": lambda: _home() / ".codex" / "config.toml",
        "write": _write_codex,
        "hint": "restart codex CLI",
    },
    "opencode": {
        "detect": lambda: (_home() / ".config" / "opencode").exists() or shutil.which("opencode") is not None,
        "path": lambda: _home() / ".config" / "opencode" / "opencode.json",
        "write": lambda p, cmd, args: _write_opencode(p, cmd, args),
        "hint": "restart opencode",
    },
    "hermes": {
        "detect": lambda: (_home() / ".hermes").exists() or (_home() / "AppData" / "Local" / "hermes").exists(),
        "path": _hermes_config_path,
        "write": _write_hermes,
        "hint": "start a NEW Hermes session to pick up the tools",
    },
}


def _write_opencode(path: Path, command: list[str], args: list[str]) -> str:
    entry = {"type": "local", "command": [command[0], *args], "enabled": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            return f"REFUSED — {path} is not valid JSON ({e})"
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    doc.setdefault("mcp", {})[SERVER_NAME] = entry
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return f"wrote {path}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_serve(_: argparse.Namespace) -> int:
    from .server import main as serve_main
    serve_main()
    return 0


def cmd_setup(_: argparse.Namespace) -> int:
    """One-time browser engine install (playwright chromium + scrapling)."""
    steps = [
        [sys.executable, "-m", "playwright", "install", "chromium"],
        ["scrapling", "install"],
    ]
    for s in steps:
        print(f"$ {' '.join(s)}")
        rc = subprocess.call(s)
        if rc != 0:
            print(f"  step failed (exit {rc}) — fix and re-run `pyrecrawl setup`", file=sys.stderr)
            return rc
    print("Engines ready.")
    return 0


def _resolve_targets(names: list[str]) -> list[str]:
    if not names or names == ["all"]:
        detected = [n for n, a in AGENTS.items() if a["detect"]()]
        return detected or list(AGENTS)
    unknown = [n for n in names if n not in AGENTS]
    if unknown:
        print(f"Unknown agent(s): {unknown}\nAvailable: {', '.join(AGENTS)}", file=sys.stderr)
        sys.exit(2)
    return names


def cmd_install(ns: argparse.Namespace) -> int:
    targets = _resolve_targets(ns.agents)
    if not targets:
        print("No agents detected. Pass names explicitly, e.g. `pyrecrawl install claude-desktop`.")
        return 1
    print(f"Registering '{SERVER_NAME}' with: {', '.join(targets)}")
    rc = 0
    cmd_args = _launch_cmd()
    for name in targets:
        agent = AGENTS[name]
        path: Path = agent["path"]()
        if ns.dry_run:
            print(f"  [{name}] would write -> {path}  (launch: {cmd_args[0]} ...)")
            continue
        status = agent["write"](path, [cmd_args[0]], cmd_args[1:])
        print(f"  [{name}] {status}")
        print(f"           {agent['hint']}")
    return rc


def cmd_uninstall(ns: argparse.Namespace) -> int:
    targets = _resolve_targets(ns.agents)
    for name in targets:
        path: Path = AGENTS[name]["path"]()
        if not path.exists():
            print(f"  [{name}] nothing to remove ({path} missing)")
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            try:
                doc = json.loads(text)
            except json.JSONDecodeError:
                print(f"  [{name}] REFUSED — invalid JSON at {path}")
                continue
            changed = False
            for key in ("mcpServers", "servers", "mcp"):
                if isinstance(doc.get(key), dict) and SERVER_NAME in doc[key]:
                    del doc[key][SERVER_NAME]
                    changed = True
            if changed:
                path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
                print(f"  [{name}] removed from {path}")
            else:
                print(f"  [{name}] not present in {path}")
        elif path.suffix == ".toml":
            new = re.sub(rf"\[mcp_servers\.{SERVER_NAME}\][^\[]*", "", text)
            path.write_text(new, encoding="utf-8")
            print(f"  [{name}] removed from {path}")
        elif path.suffix in (".yaml", ".yml"):
            new = re.sub(rf"\n  {SERVER_NAME}:\n(?:    .*\n|      .*\n)*", "\n", text)
            path.write_text(new, encoding="utf-8")
            print(f"  [{name}] removed from {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    from . import __version__
    p = argparse.ArgumentParser(prog="pyrecrawl", description="PyreCrawl MCP server")
    p.add_argument("--version", action="version", version=f"pyrecrawl {__version__}")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("serve", help="run the stdio MCP server (default)")
    sp.set_defaults(fn=cmd_serve)

    st = sub.add_parser("setup", help="one-time browser engine install (playwright + scrapling)")
    st.set_defaults(fn=cmd_setup)

    ins = sub.add_parser("install", help="register the MCP server with your AI agents")
    ins.add_argument("agents", nargs="*", help=f"{'all'} or any of: {', '.join(AGENTS)}")
    ins.add_argument("--dry-run", action="store_true", help="show what would be written")
    ins.set_defaults(fn=cmd_install)

    un = sub.add_parser("uninstall", help="remove our entry from agent configs")
    un.add_argument("agents", nargs="*", help="same names as `install`")
    un.set_defaults(fn=cmd_uninstall)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        argv = ["serve", *argv]  # bare `pyrecrawl` = serve (MCP clients may call it directly)
    ns = parser.parse_args(argv)
    if getattr(ns, "fn", None) is None:
        ns = parser.parse_args(["serve", *argv])
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main())
