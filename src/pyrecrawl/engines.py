"""Engine wrappers around Crawl4AI and Scrapling with a smart auto-fallback ladder.

The ladder tries each strategy in order until one succeeds:
  1. fetch_fast:    Scrapling Fetcher (curl_cffi, browser-like TLS)   ~ fast
  2. fetch_stealth: Scrapling StealthyFetcher (real Chromium, CF solve) ~ medium
  3. process_llm:   Crawl4AI (BM25 fit-markdown, deep crawl, extraction) ~ heavy
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# Heavy imports are lazy so that `engines` can be imported in a slim stdio server boot.


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    url: str
    final_url: str | None
    status: int
    html: str
    markdown: str
    title: str | None
    method: str                  # which engine produced this
    elapsed_ms: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractResult:
    url: str
    schema: dict[str, Any]
    data: dict[str, Any] | list[dict[str, Any]]
    method: str
    elapsed_ms: int


@dataclass
class MapResult:
    root: str
    urls: list[str]
    method: str
    elapsed_ms: int


@dataclass
class CrawlResult:
    root: str
    pages: list[FetchResult]
    method: str
    elapsed_ms: int


# ---------------------------------------------------------------------------
# Ladder: pick the lightest engine that succeeds
# ---------------------------------------------------------------------------

_ASYNC_RUNNER_HINT = (
    "If you hit a 'no running event loop' error in interactive shells, "
    "call .run_sync() on the returned coroutine from a thread or use the "
    "async helpers exposed by the MCP server."
)


_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.DOTALL | re.IGNORECASE)
_NOSCRIPT_RE = re.compile(r"<noscript\b[^>]*>.*?</noscript\s*>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _looks_like_cloudflare_block(html: str, status: int) -> bool:
    if status in (403, 503):
        return True
    markers = (
        "cf-mitigated",
        "cf-chl-bypass",
        "challenge-platform",
        "Just a moment...",
        "cf-challenge",
    )
    return any(m in html for m in markers)


def _looks_like_block(html: str, status: int) -> bool:
    return status >= 400 or _looks_like_cloudflare_block(html, status)


def _looks_like_js_skeleton(html: str, status: int) -> bool:
    """Heuristic: a 200 with a JS-rendered SPA shell, no real content in the body.

    Detected when status is 200 BUT the visible text is pathologically small
    relative to the HTML payload (i.e. the page is a React/Vue/Angular shell
    that only renders content client-side). Used by the smart ladder to escalate
    to the stealth browser instead of returning an empty page.
    """
    if status >= 400 or not html:
        return False
    # Strip non-visible payload before measuring: SPA shells ship their entire
    # app (and often the data blob) inside <script>/<style>, which would
    # otherwise make an empty page look text-rich.
    body = _SCRIPT_RE.sub(" ", html)
    body = _STYLE_RE.sub(" ", body)
    body = _NOSCRIPT_RE.sub(" ", body)
    body = _TAG_RE.sub(" ", body)
    text = _WS_RE.sub(" ", body).strip()
    # Healthy static pages: a few hundred to many thousands of visible chars.
    # SPA shells: 263 KB of HTML, ~90 chars of actual visible text.
    return len(html) > 5000 and len(text) < 250


# ---------------------------------------------------------------------------
# Engine 1 — fast HTTP via Scrapling Fetcher (curl_cffi)
# ---------------------------------------------------------------------------

def scrape_fast(url: str, timeout: int = 30) -> FetchResult:
    """Fast HTTP fetch with browser-like TLS. Cheap; no JS rendering."""
    from scrapling.fetchers import Fetcher

    import time
    t0 = time.perf_counter()
    resp = Fetcher.get(url, follow_redirects=True, timeout=timeout * 1000)
    html = resp.html_content or ""
    md = ""
    if hasattr(resp, "markdown"):
        try:
            md = resp.markdown() if callable(resp.markdown) else resp.markdown
        except Exception:  # noqa: BLE001
            md = ""
    if not md:
        md = _html_to_markdown(html)
    title = _extract_title(html)
    elapsed = int((time.perf_counter() - t0) * 1000)
    return FetchResult(
        url=url,
        final_url=resp.url,
        status=int(resp.status),
        html=html,
        markdown=md,
        title=title,
        method="scrapling.fetch_fast",
        elapsed_ms=elapsed,
        meta={"encoding": resp.encoding},
    )


# ---------------------------------------------------------------------------
# Engine 2 — stealth browser via Scrapling StealthyFetcher (Cloudflare solve)
# ---------------------------------------------------------------------------

def scrape_stealth(url: str, timeout: int = 60, network_idle: bool = True) -> FetchResult:
    """Open a real Chromium, optionally solve Cloudflare Turnstile, return content."""

    def _do_stealth():
        from scrapling.fetchers import StealthyFetcher
        return StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=network_idle,
            solve_cloudflare=True,
            timeout=timeout * 1000,
            block_ads=True,
            disable_resources=True,
        )

    import time
    t0 = time.perf_counter()
    resp = _run_blocking(_do_stealth)
    html = resp.html_content or ""
    md = ""
    if hasattr(resp, "markdown"):
        try:
            md = resp.markdown() if callable(resp.markdown) else resp.markdown
        except Exception:  # noqa: BLE001
            md = ""
    if not md:
        md = _html_to_markdown(html)
    title = _extract_title(html)
    elapsed = int((time.perf_counter() - t0) * 1000)
    return FetchResult(
        url=url,
        final_url=resp.url,
        status=int(resp.status),
        html=html,
        markdown=md,
        title=title,
        method="scrapling.fetch_stealth",
        elapsed_ms=elapsed,
        meta={"solved_cloudflare": True, "encoding": resp.encoding},
    )


# ---------------------------------------------------------------------------
# Engine 3 — LLM processing via Crawl4AI
# ---------------------------------------------------------------------------

async def _arun_crawl4ai(
    url: str,
    *,
    fit_markdown: bool = True,
    word_count_threshold: int = 50,
    css_selector: str | None = None,
    extraction_schema: dict[str, Any] | None = None,
    deep_crawl: bool = False,
    max_pages: int = 5,
    screenshot: bool = False,
) -> dict[str, Any]:
    from crawl4ai import (
        AsyncWebCrawler,
        BrowserConfig,
        CrawlerRunConfig,
        DefaultMarkdownGenerator,
        PruningContentFilter,
        JsonCssExtractionStrategy,
    )

    md_generator = DefaultMarkdownGenerator(
        content_filter=PruningContentFilter(threshold=0.4, threshold_type="fixed"),
        options={"citations": True},
    ) if fit_markdown else DefaultMarkdownGenerator(options={"citations": True})

    config_kwargs: dict[str, Any] = {
        "word_count_threshold": word_count_threshold,
        "markdown_generator": md_generator,
        "screenshot": screenshot,
    }
    if css_selector:
        config_kwargs["css_selector"] = css_selector

    if extraction_schema:
        config_kwargs["extraction_strategy"] = JsonCssExtractionStrategy(
            schema=extraction_schema,
        )

    if deep_crawl:
        from crawl4ai import BFSDeepCrawlStrategy
        config_kwargs["deep_crawl_strategy"] = BFSDeepCrawlStrategy(
            max_depth=2,
            include_external=False,
            max_pages=max_pages,
        )

    cfg = CrawlerRunConfig(**config_kwargs)
    browser_cfg = BrowserConfig(headless=True, verbose=False)

    # Silenced logger for clean stdio MCP transport (AsyncLogger default goes to stderr)
    from crawl4ai import AsyncLogger
    silent_logger = AsyncLogger(verbose=False)

    async with AsyncWebCrawler(config=browser_cfg, logger=silent_logger) as crawler:
        container = await crawler.arun(url=url, config=cfg)
        results = list(container)
        return {
            "results": [r.model_dump() for r in results],
            "method": "crawl4ai.llm",
        }


def _run_coro(coro):
    """Run an async coroutine from sync code RAII.

    FastMCP tools run inside an event loop; asyncio.run fails there.
    Reuse the running loop via a dedicated thread in that case, else
    asyncio.run (no loop — plain script usage).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _run_blocking(fn, *args, **kwargs):
    """Run blocking sync code (e.g. Playwright sync API) outside the event loop.

    Playwright's sync API refuses to run inside a thread with a running loop
    (FastMCP tools execute in-loop). Offload to a fresh thread with no loop.
    """
    import concurrent.futures
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kwargs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


def process_llm(
    url: str,
    *,
    fit_markdown: bool = True,
    css_selector: str | None = None,
    extraction_schema: dict[str, Any] | None = None,
    deep_crawl: bool = False,
    max_pages: int = 5,
) -> dict[str, Any]:
    """Sync wrapper around crawl4ai. Returns dict of CrawlResult.model_dump() per page."""
    return _run_coro(_arun_crawl4ai(
        url,
        fit_markdown=fit_markdown,
        css_selector=css_selector,
        extraction_schema=extraction_schema,
        deep_crawl=deep_crawl,
        max_pages=max_pages,
    ))


# ---------------------------------------------------------------------------
# Composite: scrape smart (auto-fallback ladder)
# ---------------------------------------------------------------------------

def scrape_smart(url: str, *, prefer: str = "auto", timeout: int = 30) -> FetchResult:
    """Try fast HTTP -> stealth browser. Return the first good result.

    `prefer` can be "auto" | "fast" | "stealth" | "llm".
    """
    if prefer == "stealth":
        return scrape_stealth(url, timeout=max(timeout, 60))
    if prefer == "llm":
        data = process_llm(url, fit_markdown=True)
        first = data["results"][0] if data["results"] else {}
        html = first.get("html", "") or first.get("cleaned_html", "")
        md = (first.get("markdown") or {})
        if isinstance(md, dict):
            md_text = md.get("raw_markdown") or md.get("fit_markdown") or ""
        else:
            md_text = str(md) if md else _html_to_markdown(html)
        return FetchResult(
            url=url,
            final_url=first.get("url") or url,
            status=int(first.get("status_code") or 200),
            html=html,
            markdown=md_text,
            title=_extract_title(html),
            method="crawl4ai.llm",
            elapsed_ms=int(first.get("crawl_stats", {}).get("total", 0) * 1000) if isinstance(first.get("crawl_stats"), dict) else 0,
            meta={"screenshot": first.get("screenshot")},
        )

    # prefer == "fast" or "auto": ladder
    try:
        fast = scrape_fast(url, timeout=timeout)
        if prefer == "fast":
            return fast
        if (
            not _looks_like_block(fast.html, fast.status)
            and not _looks_like_js_skeleton(fast.html, fast.status)
            and len(fast.html) > 500
        ):
            return fast
    except Exception as e:  # noqa: BLE001 — wide net for first attempt
        fast_exc = str(e)
    else:
        fast_exc = None

    # escalate to stealth
    stealth = scrape_stealth(url, timeout=max(timeout, 60))
    if not _looks_like_block(stealth.html, stealth.status):
        if fast_exc:
            stealth.meta["fast_error"] = fast_exc
        return stealth

    # both blocked — return stealth so caller can see CF challenge HTML
    stealth.meta["fast_error"] = fast_exc
    return stealth


# ---------------------------------------------------------------------------
# Extract — schema-based structured extraction
# ---------------------------------------------------------------------------

def extract_structured(
    url: str,
    schema: dict[str, Any],
    *,
    prefer: str = "auto",
) -> ExtractResult:
    """Fetch via ladder, then extract structured data from the HTML.

    For ``prefer="auto"`` / ``"fast"`` : Scrapling's CSS parser (lxml, no browser).
    For ``"llm"`` : Crawl4AI with Playwright browser + JsonCssExtractionStrategy.
    For ``"stealth"`` : Scrapling StealthyFetcher + CSS parser.
    """
    import time
    t0 = time.perf_counter()

    if prefer == "llm":
        data = process_llm(url, fit_markdown=False, extraction_schema=schema)
        results = data["results"]
        if not results:
            return ExtractResult(url=url, schema=schema, data={}, method="crawl4ai.llm", elapsed_ms=0)
        first = results[0]
        extracted_raw = first.get("extracted_content") or "{}"
        try:
            parsed = json.loads(extracted_raw)
        except Exception:  # noqa: BLE001
            parsed = {"raw": extracted_raw}
        return ExtractResult(
            url=url, schema=schema, data=parsed,
            method="crawl4ai.llm",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

    # Fast path: Scrapling CSS parser (no browser, lxml-backed)
    if prefer in ("auto", "fast"):
        from scrapling.fetchers import Fetcher
        resp = Fetcher.get(url, follow_redirects=True, timeout=30000)
    else:
        def _do_stealth_extract():
            from scrapling.fetchers import StealthyFetcher as _SF2
            return _SF2.fetch(url, headless=True, network_idle=True, timeout=60000)
        resp = _run_blocking(_do_stealth_extract)

    parsed = _extract_css_schema(resp.html_content or "", schema)
    return ExtractResult(
        url=url, schema=schema, data=parsed,
        method=f"scrapling.{'fetch_fast' if prefer in ('auto', 'fast') else 'fetch_stealth'}",
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )


def _extract_css_schema(html: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Run a JsonCssExtractionStrategy-like schema against raw HTML using Scrapling.

    Schema format: {"name":"...","baseSelector":"div.item","fields":[
        {"name":"title","selector":"h2","type":"text"},
        {"name":"link","selector":"a","type":"attribute","attribute":"href"},
    ]}
    Returns a list of dicts (one per baseSelector match) or [].
    """
    from scrapling.parser import Selector

    root = Selector(html)
    base = schema.get("baseSelector", "body")
    fields = schema.get("fields", [])

    items: list = list(root.css(base) or []) or list(root.find_all(base) or [])
    if not items and base == "body":
        items = [root]

    out: list[dict[str, Any]] = []
    for item in items:
        row: dict[str, Any] = {}
        for f in fields:
            name = f.get("name", f.get("selector", "?"))
            sel = f.get("selector")
            try:
                # Missing/empty selector = the base element itself
                el_list = list(item.css(sel)) if sel else [item]
            except Exception:  # noqa: BLE001
                el_list = []
            el = el_list[0] if el_list else None
            if el is None:
                row[name] = None
                continue
            ftype = f.get("type", "text")
            if ftype == "attribute":
                row[name] = el.attrib.get(f.get("attribute", "href"))
            elif ftype in ("text", "html"):
                # .text is only the FIRST text node (often whitespace for
                # nested markup) — collect all descendant text instead.
                try:
                    row[name] = el.get_all_text(strip=True) if ftype == "text" else el.html_content
                except AttributeError:
                    row[name] = (el.text or "").strip() if ftype == "text" else el.html_content
            else:
                try:
                    row[name] = el.get_all_text(strip=True)
                except AttributeError:
                    row[name] = (el.text or "").strip()
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Map — enumerate URLs
# ---------------------------------------------------------------------------

def map_urls(root: str, *, include_pattern: str | None = None, limit: int = 200) -> MapResult:
    """Crawl root, harvest all internal <a href> links.

    Uses the fast HTTP path by default; falls back to stealth for CF-protected sites.
    """
    import time
    from urllib.parse import urljoin, urlparse

    t0 = time.perf_counter()
    page = scrape_smart(root, prefer="auto", timeout=30)
    if _looks_like_block(page.html, page.status):
        # try one more time with stealth
        page = scrape_stealth(root, timeout=60)

    base_host = urlparse(page.final_url or root).netloc
    pattern_re = re.compile(include_pattern) if include_pattern else None
    seen: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', page.html, flags=re.IGNORECASE):
        href = m.group(1).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urljoin(page.final_url or root, href)
        parsed = urlparse(full)
        if parsed.scheme not in ("http", "https"):
            continue
        if base_host and parsed.netloc != base_host:
            continue
        if pattern_re and not pattern_re.search(full):
            continue
        # drop fragment
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query:
            clean += f"?{parsed.query}"
        seen.add(clean)
        if len(seen) >= limit:
            break

    return MapResult(
        root=root,
        urls=sorted(seen),
        method=page.method,
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )


# ---------------------------------------------------------------------------
# Crawl — multi-page scrape with optional LLM fit-markdown
# ---------------------------------------------------------------------------

def crawl_site(
    root: str,
    *,
    max_pages: int = 5,
    css_selector: str | None = None,
    prefer: str = "auto",
) -> CrawlResult:
    """Discover URLs on `root`, then scrape each through the smart ladder.

    For deep semantic crawling (BFS/DFS with filters), prefer="llm" delegates
    to Crawl4AI's BFSDeepCrawlStrategy.
    """
    import time

    if prefer == "llm":
        t0 = time.perf_counter()
        data = process_llm(root, fit_markdown=True, deep_crawl=True, max_pages=max_pages)
        pages: list[FetchResult] = []
        for r in data["results"]:
            html = r.get("html") or r.get("cleaned_html") or ""
            md = r.get("markdown") or {}
            md_text = md.get("raw_markdown") or md.get("fit_markdown") or _html_to_markdown(html) if isinstance(md, dict) else (str(md) if md else _html_to_markdown(html))
            pages.append(FetchResult(
                url=r.get("url", root),
                final_url=r.get("redirected_url") or r.get("url"),
                status=int(r.get("status_code") or 200),
                html=html,
                markdown=md_text,
                title=_extract_title(html),
                method="crawl4ai.llm",
                elapsed_ms=0,
                meta={"screenshot": r.get("screenshot")},
            ))
        return CrawlResult(root=root, pages=pages, method="crawl4ai.llm",
                            elapsed_ms=int((time.perf_counter() - t0) * 1000))

    t0 = time.perf_counter()
    mapped = map_urls(root, limit=max_pages)
    pages = []
    for url in mapped.urls[:max_pages]:
        try:
            page = scrape_smart(url, prefer=prefer)
            pages.append(page)
        except Exception as e:  # noqa: BLE001
            pages.append(FetchResult(
                url=url, final_url=url, status=0, html="", markdown="",
                title=None, method="error", elapsed_ms=0, meta={"error": str(e)},
            ))
    return CrawlResult(
        root=root, pages=pages, method=mapped.method,
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )


# ---------------------------------------------------------------------------
# Search — web search (no API key) via DuckDuckGo HTML
# ---------------------------------------------------------------------------

def search_web(query: str, *, limit: int = 10, prefer: str = "auto") -> list[dict[str, Any]]:
    """Search DuckDuckGo and return top `limit` organic results.

    Strategy: try the fast html. endpoint first; on bot-interstitial
    (DuckDuckGo flags datacenter IPs) the smart ladder auto-escalates to
    the stealth browser against the lighter lite. endpoint, whose
    markup is also parsed.
    """
    from urllib.parse import quote_plus, unquote

    q = quote_plus(query)

    # Attempt 1: html. endpoint via ladder (fast path works on residential IPs)
    url_html = f"https://html.duckduckgo.com/html/?q={q}"
    try:
        page = scrape_smart(url_html, prefer=prefer, timeout=30)
        results = _parse_ddg_html(page.html, limit)
        if results:
            return results
    except Exception:  # noqa: BLE001
        pass

    # Attempt 2: lite. endpoint — lighter markup, usually passes with stealth
    url_lite = f"https://lite.duckduckgo.com/lite/?q={q}"
    page = scrape_smart(url_lite, prefer="stealth" if prefer == "auto" else prefer, timeout=60)
    results = _parse_ddg_lite(page.html, limit)
    if results:
        return results
    # Last resort: parse html format on lite page (same result markup sometimes)
    return _parse_ddg_html(page.html, limit)


def _resolve_ddg_href(href: str) -> str | None:
    """Unwrap DDG redirect (//duckduckgo.com/l/?uddg=<target>) to the target URL."""
    from urllib.parse import parse_qs, quote_plus, unquote, urlparse
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc:
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg")
        if uddg:
            return unquote(uddg[0])
        return None
    if href.startswith("//"):
        return "https:" + href
    return href


def _parse_ddg_lite(html: str, limit: int) -> list[dict[str, Any]]:
    """Parse lite.duckduckgo.com result tables.

    Markup per result:
      <a rel="nofollow" href="//duckduckgo.com/l/?uddg=...">Title</a>
      ... then a snippet <td class='result-snippet'>...</td>
    """
    if not html:
        return []
    snippets = re.findall(
        r"<td[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</td>",
        html, flags=re.IGNORECASE | re.DOTALL,
    )
    out: list[dict[str, Any]] = []
    for i, m in enumerate(re.finditer(
        r'<a[^>]+rel="nofollow"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html, flags=re.IGNORECASE | re.DOTALL,
    )):
        target = _resolve_ddg_href(m.group(1))
        if not target or not target.startswith(("http://", "https://")):
            continue
        title = _strip_tags(m.group(2))
        snippet = _strip_tags(snippets[i]) if i < len(snippets) else ""
        out.append({"url": target, "title": title, "snippet": snippet})
        if len(out) >= limit:
            break
    return out


def _parse_ddg_html(html: str, limit: int) -> list[dict[str, Any]]:
    if not html:
        return []
    out: list[dict[str, Any]] = []
    for m in re.finditer(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html, flags=re.IGNORECASE | re.DOTALL,
    ):
        href = _resolve_ddg_href(m.group(1)) or ""
        if not href or not href.startswith(("http://", "https://")):
            continue
        out.append({
            "url": href,
            "title": _strip_tags(m.group(2)),
            "snippet": _strip_tags(m.group(3)),
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def _extract_title(html: str) -> str | None:
    m = _TITLE_RE.search(html or "")
    if not m:
        return None
    return _WS_RE.sub(' ', _TAG_RE.sub(' ', m.group(1))).strip() or None


def _strip_tags(s: str) -> str:
    return _WS_RE.sub(' ', _TAG_RE.sub(' ', s or '')).strip()


def _html_to_markdown(html: str) -> str:
    """Cheap HTML->text fallback when engine doesn't return markdown."""
    if not html:
        return ""
    # Drop script/style
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.IGNORECASE | re.DOTALL)
    text = _TAG_RE.sub(' ', html)
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    return _WS_RE.sub(' ', text).strip()
