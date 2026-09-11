"""Anonymous usage telemetry — opt-out transparent ping, once per 24 h.

Sends ONE lightweight POST to the stats endpoint on each server start.
Collected: hashed machine fingerprint, pyrecrawl version, python version,
platform (linux/mac/win). No IP logged, no URLs scraped, no content sent.

Disable:  export PYRECRAWL_NO_TELEMETRY=1
           — or —
          export DO_NOT_TRACK=1
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import __version__ as CURRENT

log = logging.getLogger("pyrecrawl.telemetry")

# --- Config ---
ENDPOINT = "https://pyrecrawl-stats.fajarnugraha90543.workers.dev/ping"
OPT_OUT_VARS = ("PYRECRAWL_NO_TELEMETRY", "DO_NOT_TRACK")
CACHE_DIR = Path.home() / ".pyrecrawl"
LAST_PING_FILE = CACHE_DIR / ".telemetry_ping"
COOLDOWN_SECONDS = 86400  # 24 h


def _fingerprint() -> str:
    """Stable anonymous fingerprint: SHA-256(hostname + MAC)."""
    host = socket.gethostname()
    mac = uuid.getnode()  # 48-bit MAC as int (random fallback if hidden)
    raw = f"{host}:{mac}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]  # 16 chars = 64-bit collision-safe


def _opted_out() -> bool:
    return any(os.environ.get(v) == "1" for v in OPT_OUT_VARS)


def _on_cooldown() -> bool:
    if not LAST_PING_FILE.exists():
        return False
    try:
        last = float(LAST_PING_FILE.read_text().strip())
        return (time.time() - last) < COOLDOWN_SECONDS
    except (ValueError, OSError):
        return False


def _record_ping() -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_PING_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def startup_ping() -> None:
    """Spawn the ping on a daemon thread so server startup is never blocked."""
    threading.Thread(target=ping, name="pyrecrawl-telemetry", daemon=True).start()


def ping() -> None:
    """Send one anonymous ping. Called once at startup. Never raises."""
    if _opted_out() or _on_cooldown():
        return
    log.info(
        "anonymous usage ping: sends hashed id + version + os only — "
        "disable with PYRECRAWL_NO_TELEMETRY=1 or DO_NOT_TRACK=1"
    )
    payload = json.dumps({
        "id": _fingerprint(),
        "version": CURRENT,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": platform.system().lower(),
        "ts": int(time.time()),
    }).encode()
    try:
        req = urllib.request.Request(
            ENDPOINT,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": f"pyrecrawl/{CURRENT}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status < 300:
                _record_ping()
    except Exception:  # noqa: BLE001
        pass  # telemetry must never break the server
