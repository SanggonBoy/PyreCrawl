"""Version checker — queries PyPI for latest release, 24 h cache.

Zero external deps: stdlib urllib + json only.

Usage from server:
    from .updater import get_latest_version, startup_log

Startup log prints a one-liner to stderr if an update is available.
health() calls get_latest_version() for a silent in-response field.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import urllib.request
from pathlib import Path

from . import __version__ as CURRENT

log = logging.getLogger("pyrecrawl")

PYPI_URL = "https://pypi.org/pypi/pyrecrawl/json"
CACHE_TTL = 86400  # 24 hours
_CACHE_DIR = Path.home() / ".pyrecrawl" / "updates"
_CACHE_FILE = _CACHE_DIR / "latest.json"


def _fetch_pypi(timeout: int = 5) -> str | None:
    """Hit PyPI JSON API. Return latest version string or None."""
    try:
        req = urllib.request.Request(
            PYPI_URL,
            headers={"Accept": "application/json", "User-Agent": f"pyrecrawl/{CURRENT}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data.get("info", {}).get("version")
    except Exception:  # noqa: BLE001
        return None


def _vtuple(v: str) -> tuple[int, ...]:
    """'0.7.2' -> (0, 7, 2) — enough for numeric-only PyPI versions."""
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)  # non-numeric (dev/post builds) → never compare as newer


def get_latest_version(*, force: bool = False) -> dict[str, str]:
    """Return {current, latest, update_available}.

    Caches the PyPI result for 24 h under ~/.pyrecrawl/updates/.
    Call with force=True to bypass cache (e.g. `pyrecrawl update`).
    """
    now = time.time()
    latest: str | None = None

    if not force and _CACHE_FILE.exists():
        try:
            cached = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if now - cached.get("ts", 0) < CACHE_TTL:
                latest = cached.get("version")
        except (json.JSONDecodeError, KeyError):  # noqa: BLE001
            pass

    if latest is None:
        latest = _fetch_pypi()
        if latest:
            try:
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                _CACHE_FILE.write_text(
                    json.dumps({"version": latest, "ts": now}) + "\n",
                    encoding="utf-8",
                )
            except OSError:  # noqa: BLE001
                pass

    newer = bool(latest and _vtuple(latest) > _vtuple(CURRENT))
    return {
        "current": CURRENT,
        "latest": latest or CURRENT,
        "update_available": newer,
    }


def startup_log() -> None:
    """Print update warning to stderr once at server start."""
    info = get_latest_version()
    if info["update_available"]:
        print(
            f"⚠  PyreCrawl {info['latest']} is available "
            f"(you have {info['current']}) — run: uv tool upgrade pyrecrawl",
            file=sys.stderr,
        )


def cmd_update() -> int:
    """CLI: run the actual upgrade via uv tool."""
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    uv = shutil.which("uv")
    if not uv:
        print("uv not found on PATH. Install it:  https://docs.astral.sh/uv/", file=sys.stderr)
        return 1
    print(f"Upgrading pyrecrawl from {CURRENT} ...")
    rc = subprocess.call([uv, "tool", "upgrade", "pyrecrawl"])
    if rc == 0:
        # Refresh the cache so the next startup log stays quiet
        get_latest_version(force=True)
        print("Done. Restart your MCP agent to pick up the new version.")
    else:
        print("Upgrade failed. Try manually:  uv tool upgrade pyrecrawl", file=sys.stderr)
    return rc
