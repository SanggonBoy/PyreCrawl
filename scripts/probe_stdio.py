"""Probe the MCP server over stdio: verifies JSON-RPC stays clean on stdout while invoking real tools."""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

proc = subprocess.Popen(
    [str(VENV_PY), "-m", "pyrecrawl.server"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
    cwd=ROOT,
)

stderr_lines: list[str] = []


def _drain_stderr():
    for line in proc.stderr:
        stderr_lines.append(line)


threading.Thread(target=_drain_stderr, daemon=True).start()


def rpc(method, params=None, nid=0, timeout=120):
    msg = {"jsonrpc": "2.0", "id": nid, "method": method, "params": params or {}}
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"server died; stderr tail: {''.join(stderr_lines[-15:])}")
            continue
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise TimeoutError(f"no reply for {method} after {timeout}s")


def payload_of(out):
    return json.loads(out["result"]["content"][0]["text"])


def main():
    rid = 1
    print("== initialize ==")
    out = rpc("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0.1"},
    }, rid); rid += 1
    assert "result" in out, out
    print("  server:", out["result"]["serverInfo"])
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()

    print("== tools/list ==")
    out = rpc("tools/list", {}, rid); rid += 1
    names = [t["name"] for t in out["result"]["tools"]]
    print("  tools:", names)
    assert set(names) == {"scrape", "extract", "map_site", "crawl", "search", "health"}

    print("== health ==")
    p = payload_of(rpc("tools/call", {"name": "health", "arguments": {}}, rid)); rid += 1
    print("  versions:", {k: v for k, v in p.items() if not k.endswith("error")})
    assert "crawl4ai" in p and "scrapling" in p, p

    print("== scrape example.com (fast) ==")
    p = payload_of(rpc("tools/call", {
        "name": "scrape",
        "arguments": {"url": "https://example.com", "prefer": "fast", "timeout": 20},
    }, rid)); rid += 1
    print("  status:", p.get("status"), "| title:", p.get("title"), "| method:", p.get("method"))
    assert p.get("status") == 200 and p.get("title") == "Example Domain", p

    print("== extract (CSS schema) ==")
    p = payload_of(rpc("tools/call", {
        "name": "extract",
        "arguments": {
            "url": "https://example.com",
            "schema": {"name": "Page", "baseSelector": "body",
                       "fields": [{"name": "heading", "selector": "h1", "type": "text"}]},
        },
    }, rid)); rid += 1
    print("  data:", p.get("data"), "| method:", p.get("method"))
    assert "error" not in p, p

    print("== search (ladder: fast -> stealth) ==")
    p = payload_of(rpc("tools/call", {
        "name": "search",
        "arguments": {"query": "firecrawl alternative open source", "limit": 3},
    }, rid, timeout=180)); rid += 1
    print("  count:", p.get("count"))
    for x in (p.get("results") or [])[:3]:
        print("   -", (x.get("title") or "")[:60], "|", (x.get("url") or "")[:60])
    assert p.get("count", 0) >= 1, p

    print("\nALL PROBE CHECKS PASSED — stdout is pure JSON-RPC")
    proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
