"""Regression: internal-link harvest must only yield real pages (issue #3, 2026-09-11).

Old regex `href=["']...` also matched <link rel=stylesheet|canonical|icon>,
RSS <link>, etc. — junk burned the limit quota in map_urls AND became fake
"pages" through crawl's map+scrape path.

Offline: _iter_internal_links tested directly + map_urls via stubbed
scrape_smart. Run: python scripts/test_link_harvest.py (exit 0 = PASS)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pyrecrawl.engines as E

failures = []
def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}  {detail if not cond else ''}")
    if not cond: failures.append(name)

HTML = """<html><head>
<link rel="stylesheet" href="https://ex.com/style.css">
<link rel="icon" href="/favicon.ico">
<link rel="canonical" href="https://ex.com/">
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
</head><body>
<A HREF="/About">upper</A>
<a href='/blog/post-1'>single quotes</a>
<a href="/doc.pdf?v=2">pdf ok</a>
<a href="/app.js?v=9">js junk</a>
<a href="/img/logo.WEBP">image junk (case)</a>
<a href="#top">frag</a>
<a href="javascript:void(0)">js href</a>
<a href="mailto:x@y.z">mail</a>
<a href="tel:+62">tel</a>
<a href="https://other.com/page">external</a>
<a href="/shop/item?id=5">query kept</a>
<a href="/clean#frag">frag dropped</a>
<a href="/data.zip">archive</a>
<a href="relative/page.html">relative resolved</a>
</body></html>"""

links = list(E._iter_internal_links(HTML, "https://ex.com/", "ex.com"))

check("anchor-only: stylesheet css gone", "https://ex.com/style.css" not in links and "/style.css" not in "".join(links), str(links))
check("anchor-only: favicon gone", not any("favicon" in l for l in links), str(links))
check("anchor-only: canonical gone", sum(l == "https://ex.com/" for l in links) == 0, str(links))
check("anchor-only: feed.xml gone", not any("feed.xml" in l for l in links))
check("keep: /About", "https://ex.com/About" in links)
check("keep: single-quote /blog/post-1", "https://ex.com/blog/post-1" in links)
check("keep: pdf with query", "https://ex.com/doc.pdf?v=2" in links)
check("drop: /app.js", not any("/app.js" in l for l in links))
check("drop: /img/logo.WEBP case-insensitive", not any("logo" in l for l in links))
check("drop: /data.zip", not any("data.zip" in l for l in links))
check("drop: external other.com", not any("other.com" in l for l in links))
check("keep query: /shop/item?id=5", "https://ex.com/shop/item?id=5" in links)
check("frag stripped: /clean", "https://ex.com/clean" in links and not any("#" in l for l in links))
check("relative resolved to ex.com", "https://ex.com/relative/page.html" in links)
check("no junk schemes", not any(l.startswith(("javascript", "mailto", "tel")) for l in links))

# dedupe/limit contract of map_urls still holds: stub scrape_smart offline
def fake_smart(u, prefer="auto", timeout=30):
    return E.FetchResult(url=u, final_url="https://ex.com/", status=200, html=HTML,
                         markdown="x", title="T", method="m", elapsed_ms=1)
orig = E.scrape_smart
E.scrape_smart = fake_smart
m = E.map_urls("https://ex.com/", limit=3)
E.scrape_smart = orig
check("map_urls: limit honored, page-only", len(m.urls) == 3 and not any(".css" in u or ".ico" in u for u in m.urls), str(m.urls))

# old-behavior guard: what the OLD regex produced on this same HTML
import re
old = re.findall(r'href=["\']([^"\']+)["\']', HTML, flags=re.IGNORECASE)
junk = [h for h in old if any(h.endswith(e) or e in h for e in (".css", ".ico", "/feed.xml", "style.css"))]
check("control: old regex DID catch junk (test is meaningful)", len(junk) >= 3, str(old[:6]))

print("\nRESULT:", "PASS" if not failures else f"FAIL -> {failures}")
sys.exit(1 if failures else 0)
