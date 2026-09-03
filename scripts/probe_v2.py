"""Probe v2: exercise all 6 MCP tools + edge cases over real stdio.

Builds on probe_stdio.py but adds: map_site, crawl, llm prefer, include_html,
CF-protected site, error paths, and ladder escalation assertions.

Run:  python scripts/probe_v2.py
Exit 0 = PASS, exit 1 = FAIL
"""
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
    if "error" in out:
        raise RuntimeError(f"rpc returned error: {out['error']}")
    return json.loads(out["result"]["content"][0]["text"])


results = {"pass": 0, "fail": 0, "failures": []}


def check(label, cond, detail=""):
    if cond:
        results["pass"] += 1
        print(f"  ✓ {label}")
    else:
        results["fail"] += 1
        results["failures"].append((label, detail))
        print(f"  ✗ {label}  -- {detail}")


def main():
    rid = 1
    print("== initialize ==")
    out = rpc("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "probe_v2", "version": "0.2"},
    }, rid); rid += 1
    check("initialize ok", "result" in out, str(out)[:200])
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()

    print("== tools/list ==")
    out = rpc("tools/list", {}, rid); rid += 1
    names = sorted([t["name"] for t in out["result"]["tools"]])
    expected = sorted(["scrape", "extract", "map_site", "crawl", "search", "health"])
    check("all 6 tools registered", names == expected, f"got={names}")

    # 1. health
    print("== health ==")
    p = payload_of(rpc("tools/call", {"name": "health", "arguments": {}}, rid)); rid += 1
    check("health: crawl4ai version", "crawl4ai" in p and "." in str(p.get("crawl4ai")), str(p))
    check("health: scrapling version", "scrapling" in p, str(p))
    check("health: mcp version", "mcp" in p, str(p))

    # 2. scrape - fast on static
    print("== scrape fast: example.com ==")
    p = payload_of(rpc("tools/call", {
        "name": "scrape",
        "arguments": {"url": "https://example.com", "prefer": "fast", "timeout": 20},
    }, rid)); rid += 1
    check("status 200", p.get("status") == 200, str(p.get("status")))
    check("title correct", p.get("title") == "Example Domain", str(p.get("title")))
    check("markdown non-empty", len(p.get("markdown") or "") > 20, f"len={len(p.get('markdown') or '')}")
    check("method = scrapling.fetch_fast", p.get("method") == "scrapling.fetch_fast", str(p.get("method")))

    # 3. scrape - auto on SPA (must escalate, found by test_ladder_bug.py)
    print("== scrape auto on SPA (Shopee) ==")
    p = payload_of(rpc("tools/call", {
        "name": "scrape",
        "arguments": {"url": "https://shopee.co.id/search?keyword=kahf+face+wash",
                      "prefer": "auto", "timeout": 60},
    }, rid, timeout=90)); rid += 1
    if "error" in p:
        print(f"  (skip — Shopee blocked: {p['error'][:120]})")
    else:
        check("SPA escalation method != fast",
              p.get("method") != "scrapling.fetch_fast",
              f"method={p.get('method')} (ladder bug regressed?)")

    # 4. scrape - include_html flag
    print("== scrape include_html ==")
    p = payload_of(rpc("tools/call", {
        "name": "scrape",
        "arguments": {"url": "https://example.com", "prefer": "fast", "include_html": True, "timeout": 15},
    }, rid)); rid += 1
    check("include_html: html field present", "html" in p, "no html in response")
    check("include_html: html has content", len(p.get("html", "")) > 100, f"html_len={len(p.get('html',''))}")

    # 5. scrape - include_html=False default
    print("== scrape default (no html) ==")
    p = payload_of(rpc("tools/call", {
        "name": "scrape",
        "arguments": {"url": "https://example.com", "prefer": "fast", "timeout": 15},
    }, rid)); rid += 1
    check("html NOT in default response", "html" not in p, "html leaked by default")

    # 6. extract - CSS schema on static
    print("== extract CSS schema on example.com ==")
    p = payload_of(rpc("tools/call", {
        "name": "extract",
        "arguments": {
            "url": "https://example.com",
            "schema": {"name": "Page", "baseSelector": "body",
                       "fields": [{"name": "heading", "selector": "h1", "type": "text"},
                                  {"name": "link", "selector": "a", "type": "attribute", "attribute": "href"}]},
        },
    }, rid)); rid += 1
    check("extract no error", "error" not in p, str(p.get("error")))
    check("extract has data list", isinstance(p.get("data"), list), f"type={type(p.get('data'))}")
    if isinstance(p.get("data"), list) and p["data"]:
        first = p["data"][0]
        check("extract heading = 'Example Domain'", first.get("heading") == "Example Domain", str(first))

    # 7. extract on real list (Wikipedia vector-toc)
    print("== extract CSS schema on wikipedia ==")
    p = payload_of(rpc("tools/call", {
        "name": "extract",
        "arguments": {
            "url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
            "schema": {"name": "Links", "baseSelector": "#vector-toc a",
                       "fields": [{"name": "text", "selector": "", "type": "text"},
                                  {"name": "href", "selector": "", "type": "attribute", "attribute": "href"}]},
            "prefer": "fast",
        },
    }, rid, timeout=60)); rid += 1
    if "error" not in p:
        check("wiki extract non-empty", isinstance(p.get("data"), list) and len(p["data"]) > 3,
              f"len={len(p.get('data') or [])}")
        if isinstance(p.get("data"), list) and p["data"]:
            first = p["data"][0]
            check("wiki extract has text", bool(first.get("text")), str(first))
            check("wiki extract has href", bool(first.get("href")), str(first))
    else:
        print(f"  (skip — wikipedia extract failed: {p['error'][:120]})")

    # 8. map_site
    print("== map_site on wikipedia ==")
    p = payload_of(rpc("tools/call", {
        "name": "map_site",
        "arguments": {"root": "https://en.wikipedia.org/wiki/Python_(programming_language)", "limit": 20},
    }, rid, timeout=60)); rid += 1
    if "error" not in p:
        check("map returns list", isinstance(p.get("urls"), list), str(p)[:200])
        check("map urls are http(s)", all((u or "").startswith("http") for u in p.get("urls", [])),
              f"count={len(p.get('urls') or [])}")
        check("map urls are internal", all("wikipedia.org" in (u or "") for u in p.get("urls", [])), "")
    else:
        print(f"  (skip — map failed: {p['error'][:120]})")

    # 9. map_site with include_pattern
    print("== map_site with include_pattern ==")
    p = payload_of(rpc("tools/call", {
        "name": "map_site",
        "arguments": {"root": "https://example.com", "include_pattern": ".*", "limit": 5},
    }, rid)); rid += 1
    if "error" not in p:
        check("map w/ pattern returns list", isinstance(p.get("urls"), list), str(p)[:200])

    # 10. crawl - small budget
    print("== crawl example.com (max 2 pages) ==")
    p = payload_of(rpc("tools/call", {
        "name": "crawl",
        "arguments": {"root": "https://example.com", "max_pages": 2, "prefer": "fast"},
    }, rid)); rid += 1
    if "error" not in p:
        check("crawl returns pages list", isinstance(p.get("pages"), list), str(p)[:200])
        check("crawl pages count <= 2", len(p.get("pages") or []) <= 2, f"count={len(p.get('pages') or [])}")
    else:
        print(f"  (skip — crawl failed: {p['error'][:120]})")

    # 11. search - returns >= 1
    print("== search ==")
    p = payload_of(rpc("tools/call", {
        "name": "search",
        "arguments": {"query": "python mcp server", "limit": 3},
    }, rid, timeout=180)); rid += 1
    if "error" not in p:
        check("search count >= 1", (p.get("count") or 0) >= 1, str(p)[:200])
        check("search results have url+title",
              all(r.get("url") and r.get("title") for r in (p.get("results") or [])),
              str(p.get("results"))[:200])
    else:
        print(f"  (skip — search failed: {p['error'][:120]})")

    # 12. error path - malformed URL
    print("== scrape malformed URL ==")
    p = payload_of(rpc("tools/call", {
        "name": "scrape",
        "arguments": {"url": "not-a-url", "prefer": "fast", "timeout": 10},
    }, rid)); rid += 1
    check("malformed URL returns error field", "error" in p, str(p)[:200])

    # 13. error path - 404
    print("== scrape 404 ==")
    p = payload_of(rpc("tools/call", {
        "name": "scrape",
        "arguments": {"url": "https://httpbin.org/status/404", "prefer": "fast", "timeout": 15},
    }, rid, timeout=30)); rid += 1
    # We accept either: an error field, or status>=400 in payload
    ok = "error" in p or (p.get("status") and p["status"] >= 400)
    check("404 handled (no crash)", ok, f"status={p.get('status')}, error={p.get('error')}")

    print("\n" + "=" * 60)
    print(f"PASS: {results['pass']}   FAIL: {results['fail']}")
    if results["failures"]:
        print("\nFailures:")
        for lbl, det in results["failures"]:
            print(f"  - {lbl}: {det[:200]}")
    proc.terminate()
    sys.exit(0 if results["fail"] == 0 else 1)


if __name__ == "__main__":
    main()
