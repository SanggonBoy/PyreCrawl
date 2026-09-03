"""Nontrivial selfcheck: exercises the non-trivial logic with real network calls.
Run via:  python scripts/selfcheck.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from pyrecrawl.engines import (
    scrape_fast, scrape_smart, search_web, map_urls, process_llm
)
from pyrecrawl.server import build_server

def check(name, cond, msg=""):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {name}{' — '+msg if msg else ''}")
    if not cond:
        raise AssertionError(f"selfcheck failed: {name} — {msg}")

def main():
    print("=== PyreCrawl selfcheck ===")

    # 1. fast HTTP
    r = scrape_fast("https://example.com")
    check("scrape_fast status", r.status == 200, str(r.status))
    check("scrape_fast title", r.title == "Example Domain", r.title or "")
    check("scrape_fast markdown", len(r.markdown) > 20)

    # 2. smart ladder auto (should stay on fast path for example.com)
    r2 = scrape_smart("https://example.com", prefer="auto")
    check("scrape_smart auto", r2.method == "scrapling.fetch_fast", r2.method)

    # 3. search (fallback via lite + stealth is real)
    results = search_web("python webscraping 2026", limit=3)
    check("search >= 1 result", len(results) >= 1, str(results))
    check("search url is http(s)", all(x["url"].startswith("http") for x in results))

    # 4. map (example.com has no internal links)
    m = map_urls("https://example.com", limit=5)
    check("map internal links", isinstance(m.urls, list))

    # 5. Crawl4AI LLM path
    data = process_llm("https://example.com", fit_markdown=True)
    first = data["results"][0]
    md = (first.get("markdown") or {})
    check("crawl4ai success", bool(first.get("success")))
    check("crawl4ai markdown", isinstance(md, dict) and bool(md.get("raw_markdown")))
    check("crawl4ai title in md", "Example" in (md.get("raw_markdown") or ""))

    # 6. FastMCP server builds with all 6 tools
    srv = build_server()
    names = [t.name for t in srv._tool_manager.list_tools()]
    for expected in ("scrape", "extract", "map_site", "crawl", "search", "health"):
        check(f"tool {expected}", expected in names, str(names))

    print("\nAll checks passed.")

if __name__ == "__main__":
    main()
