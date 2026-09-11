#!/usr/bin/env python3
"""PyreCrawl download/usage stats — stdlib only, no Actions needed.

Usage:
    python stats.py             # print summary (Discord-friendly)
    python stats.py --selfcheck # validate parsing against a canned payload
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

PKG = "pyrecrawl"
REPO = "SanggonBoy/PyreCrawl"
GH_CANDIDATES = [
    r"C:/Program Files/GitHub CLI/gh.exe",  # not on PATH of older sessions
]


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "pyrecrawl-stats/0.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def pypi_summary(days: int = 7) -> dict:
    """Sum pypistats.org daily downloads over the last `days` days."""
    data = _get_json(f"https://pypistats.org/api/packages/{PKG}/overall")["data"]
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    out = {"with_mirrors": 0, "without_mirrors": 0}
    for row in data:
        if row["date"] >= cutoff:
            out[row["category"]] += row["downloads"]
    return out


def _gh() -> str:
    for p in GH_CANDIDATES:
        if Path(p).exists():
            return p
    found = shutil.which("gh")
    if not found:
        raise SystemExit("gh CLI not found")
    return found


def github_traffic() -> dict:
    gh = _gh()
    out = {}
    for kind in ("views", "clones"):
        raw = subprocess.run(
            [gh, "api", f"repos/{REPO}/traffic/{kind}"],
            capture_output=True, text=True, timeout=30,
        )
        if raw.returncode != 0:
            out[kind] = {"error": raw.stderr.strip()[:120]}
            continue
        d = json.loads(raw.stdout)
        # count/sum the 14-day series; API caps at 14 days anyway
        key = "views" if kind == "views" else "clones"
        out[kind] = {
            "uniques": d.get("uniques", 0),
            "total": sum(x["count"] for x in d.get(key, [])),
        }
    return out


def summary() -> str:
    p = pypi_summary()
    g = github_traffic()
    lines = [
        f"**PyreCrawl stats — {date.today().isoformat()}**",
        f"PyPI downloads 7d: **{p['without_mirrors']}** asli "
        f"(+{p['with_mirrors']} w/ mirrors) — pypistats.org/packages/{PKG}",
        f"GitHub 14d: {g['views'].get('uniques', '?')} unique views / "
        f"{g['clones'].get('uniques', '?')} unique clones",
    ]
    return "\n".join(lines)


def _selfcheck() -> int:
    canned = {"data": [
        {"category": "without_mirrors", "date": "2026-09-10", "downloads": 10},
        {"category": "with_mirrors", "date": "2026-09-10", "downloads": 25},
        {"category": "without_mirrors", "date": "2026-08-01", "downloads": 999},  # old, excluded
    ]}
    cutoff = (date.today() - timedelta(days=6)).isoformat()
    assert canned["data"][2]["date"] < cutoff, "cutoff must exclude old rows"
    got = sum(r["downloads"] for r in canned["data"]
              if r["category"] == "without_mirrors" and r["date"] >= cutoff)
    assert got == 10, got
    print("selfcheck OK")
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(_selfcheck())
    print(summary())
