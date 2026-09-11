"""Regression: crawl() css_selector + max_depth wiring (issue #2, 2026-09-11).

Offline (fake scrape_smart/process_llm) + real lxml via project venv.
Run: python scripts/test_scope_selector.py  (exit 0 = PASS)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pyrecrawl.engines as E

failures = []
def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}  {detail if not cond else ''}")
    if not cond: failures.append(name)

HTML = ("<html><head><title>T</title></head><body>"
        "<nav><a href='/a'>a</a><a href='/b'>b</a></nav>"
        "<article><h1>Hello</h1><p>body text</p></article>"
        "<footer>foot</footer></body></html>")

r = E.FetchResult(url="u", final_url="u", status=200, html=HTML,
                  markdown="full", title="T", method="m", elapsed_ms=1)

# 1. basic scoping
s = E._scope_to_selector(r, "article")
check("scope: markdown from article", "Hello" in (s.markdown or ""), repr(s.markdown))
check("scope: footer/nav stripped", "foot" not in (s.markdown or "") and "foot" not in (s.html or ""), repr(s.html)[:120])
check("scope: meta records selector", s.meta.get("css_selector") == "article", str(s.meta))
check("scope: title/method preserved", s.title == "T" and s.method == "m")

# 2. no-match -> unchanged
s2 = E._scope_to_selector(r, "div.nope")
check("scope: no match keeps full", s2.markdown == "full", repr(s2.markdown))
check("scope: no match adds no meta", "css_selector" not in s2.meta)

# 3/4. None/empty/bad passthrough
check("scope: None passthrough", E._scope_to_selector(r, None).markdown == "full")
r_empty = E.FetchResult(url="u", final_url="u", status=200, html="", markdown="", title=None, method="m", elapsed_ms=1)
check("scope: empty html passthrough", E._scope_to_selector(r_empty, "article").html == "")
check("scope: bad selector passthrough", E._scope_to_selector(r, "[[[").markdown == "full")

# 5. process_llm forwards css_selector + max_depth
captured = {}
real_coro = E._arun_crawl4ai
async def fake(url, **kw):
    captured.update(kw); return {"results": [], "method": "crawl4ai.llm"}
E._arun_crawl4ai = fake
E.process_llm("http://x", deep_crawl=True, max_pages=3, css_selector="article", max_depth=4)
E._arun_crawl4ai = real_coro
check("llm: css_selector forwarded", captured.get("css_selector") == "article", str(captured))
check("llm: max_depth forwarded", captured.get("max_depth") == 4, str(captured))

# 6. crawl_site llm branch wiring
orig_llm = E.process_llm
calls = {}
def fake_llm(root, **kw):
    calls.update(kw); return {"results": [], "method": "crawl4ai.llm"}
E.process_llm = fake_llm
E.crawl_site("http://root", prefer="llm", max_pages=7, css_selector="main", max_depth=3)
check("crawl llm: css_selector reaches llm", calls.get("css_selector") == "main", str(calls))
check("crawl llm: max_depth reaches llm", calls.get("max_depth") == 3, str(calls))
calls.clear()
E.crawl_site("http://root", prefer="llm", css_selector="main")
E.process_llm = orig_llm
check("crawl llm: max_depth 0 maps to 1", calls.get("max_depth") == 1, str(calls))

# 7. crawl_site non-llm flat: pages scoped
def fake_smart(u, prefer="auto", timeout=30):
    return E.FetchResult(url=u, final_url=u, status=200, html=HTML,
                         markdown="FULL", title="T", method="scrapling.fetch_fast", elapsed_ms=1)
orig_smart, orig_map = E.scrape_smart, E.map_urls
E.scrape_smart = fake_smart
E.map_urls = lambda root, limit=200: E.MapResult(root=root, urls=["http://root/p1", "http://root/p2"], method="x", elapsed_ms=1)
out = E.crawl_site("http://root", max_pages=2, css_selector="article", prefer="fast")
check("crawl fast: 2 pages", len(out.pages) == 2)
check("crawl fast: pages scoped", all("foot" not in (p.markdown or "") for p in out.pages), [p.markdown[:60] for p in out.pages])
check("crawl fast: selector meta set", all(p.meta.get("css_selector") == "article" for p in out.pages))

# 8. BFS: link discovery on FULL html even when selector strips nav
def fake_smart2(u, prefer="auto", timeout=30):
    body = f"<html><head><title>T</title></head><body><nav><a href='{u}/sub'>sub</a></nav><article><h1>z</h1></article></body></html>"
    return E.FetchResult(url=u, final_url=u, status=200, html=body, markdown="x", title="T", method="m", elapsed_ms=1)
E.scrape_smart = fake_smart2
out2 = E.crawl_site("http://root", max_pages=3, max_depth=1, css_selector="article", prefer="fast")
E.scrape_smart, E.map_urls = orig_smart, orig_map
urls = [p.url for p in out2.pages]
check("bfs: /sub discovered despite nav-stripping selector", any("/sub" in u for u in urls), str(urls))

print("\nRESULT:", "PASS" if not failures else f"FAIL -> {failures}")
sys.exit(1 if failures else 0)
