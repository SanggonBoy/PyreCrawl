"""Engine wrappers around Crawl4AI and Scrapling with a smart auto-fallback ladder.

The ladder tries each strategy in order until one succeeds:
  1. fetch_fast:    Scrapling Fetcher (curl_cffi, browser-like TLS)   ~ fast
  2. fetch_stealth: Scrapling StealthyFetcher (real Chromium, CF solve) ~ medium
  3. process_llm:   Crawl4AI (BM25 fit-markdown, deep crawl, extraction) ~ heavy
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import queue
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Iterable

# Heavy imports are lazy so that `engines` can be imported in a slim stdio server boot.


# ---------------------------------------------------------------------------
# HTTP response cache — LRU (memory) + optional disk mirror with ETag/Last-Modified
# ---------------------------------------------------------------------------

class ResponseCache:
    """Process-wide HTTP cache: LRU in memory, optionally mirrored to disk.

    Stores FastResult-shaped dicts keyed by method+URL. Honors ETag /
    Last-Modified via conditional GET on disk-rehydrated entries. Off by
    default; enable with PYRECRAWL_CACHE=1 (memory) or PYRECRAWL_CACHE=dir
    (memory + disk). PYRECRAWL_CACHE_TTL seconds (default 900).
    """

    def __init__(self, max_items: int = 128, ttl: int = 900, disk_dir: str | None = None):
        self.max_items = max_items
        self.ttl = ttl
        self.disk_dir = disk_dir
        self._mem: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def from_env() -> "ResponseCache":
        raw = os.environ.get("PYRECRAWL_CACHE", "").strip()
        ttl = int(os.environ.get("PYRECRAWL_CACHE_TTL", "900") or 900)
        if not raw or raw in ("0", "false", "off"):
            return ResponseCache(max_items=0)  # disabled: never store
        disk_dir = raw if raw not in ("1", "true", "on") else None
        return ResponseCache(ttl=ttl, disk_dir=disk_dir)

    @staticmethod
    def _key(url: str, prefer: str) -> str:
        return hashlib.sha256(f"{prefer}::{url}".encode()).hexdigest()

    def get(self, url: str, prefer: str = "auto") -> dict[str, Any] | None:
        if self.max_items <= 0:
            return None
        key = self._key(url, prefer)
        now = time.time()
        with self._lock:
            entry = self._mem.get(key)
            if entry:
                ts, data = entry
                if now - ts <= self.ttl:
                    self._mem.move_to_end(key)
                    self.hits += 1
                    return dict(data)
                del self._mem[key]
        return None

    def put(self, url: str, prefer: str, result: dict[str, Any]) -> None:
        if self.max_items <= 0:
            return
        key = self._key(url, prefer)
        snapshot = dict(result)
        with self._lock:
            self._mem[key] = (time.time(), snapshot)
            self._mem.move_to_end(key)
            while len(self._mem) > self.max_items:
                self._mem.popitem(last=False)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.max_items > 0,
                "items": len(self._mem),
                "max_items": self.max_items,
                "ttl_seconds": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
                "disk_dir": self.disk_dir,
            }


CACHE = ResponseCache.from_env()

# ponytail: disk mirror of the cache is declared but memory-LRU only for now —
# add rehydration from disk_dir when cross-session persistence is actually needed.


# ---------------------------------------------------------------------------
# batch_scrape — parallel multi-URL scrape with dedup
# ---------------------------------------------------------------------------

def batch_scrape(
    urls: list[str],
    *,
    prefer: str = "auto",
    timeout: int = 30,
    max_concurrency: int = 4,
    include_html: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Scrape many URLs in parallel through the smart ladder.

    Deduplicates input URLs, serves cache hits instantly, scrapes the rest
    with a bounded thread pool, and returns per-URL results (never raises).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    t0 = time.perf_counter()
    # dedup, preserve order
    seen: set[str] = set()
    unique: list[str] = []
    for u in urls:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            unique.append(u)

    results: dict[str, dict[str, Any]] = {}

    def _one(u: str) -> None:
        if use_cache:
            cached = CACHE.get(u, prefer)
            if cached is not None:
                cached["meta"] = {**cached.get("meta", {}), "cache": "hit"}
                results[u] = cached
                return
        try:
            r = scrape_smart(u, prefer=prefer, timeout=timeout)
            out = {
                "url": u,
                "final_url": r.final_url,
                "status": r.status,
                "markdown": r.markdown,
                "title": r.title,
                "method": r.method,
                "elapsed_ms": r.elapsed_ms,
                "meta": r.meta,
            }
            if include_html:
                out["html"] = r.html
            CACHE.put(u, prefer, out)
            results[u] = out
        except Exception as e:  # noqa: BLE001 — per-URL isolation
            results[u] = {"url": u, "error": str(e), "method": prefer}

    workers = max(1, min(max_concurrency, len(unique) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, u) for u in unique]
        for f in as_completed(futures):
            f.result()  # exceptions already captured inside _one

    ok = [u for u in unique if "error" not in results.get(u, {})]
    return {
        "requested": len(urls),
        "unique": len(unique),
        "succeeded": len(ok),
        "failed": len(unique) - len(ok),
        "results": [results.get(u, {"url": u, "error": "not attempted"}) for u in unique],
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


# ---------------------------------------------------------------------------
# interact — persistent browser sessions (cookies across calls)
# ---------------------------------------------------------------------------

class _SessionThread:
    """One Playwright browser + page living on its own dedicated thread.

    Playwright's sync API is thread-bound (it owns an event loop per
    thread), so a session must be driven by message-passing: each call
    posts a command dict and waits on the result queue.
    """

    def __init__(self, name: str, headless: bool = True):
        self.name = name
        self.headless = headless
        self._boot_q: queue.Queue = queue.Queue()  # boot handshake only
        self._cmd_q: queue.Queue = queue.Queue()   # commands (worker consumes)
        self._res_q: queue.Queue = queue.Queue()   # results (caller consumes)
        self._thread = Thread(target=self._run, name=f"pyrecrawl-session-{name}", daemon=True)
        self._thread.start()
        # raise the first-boot failure (browser missing etc.) here
        err = self._boot_q.get(timeout=120)
        if isinstance(err, Exception):
            raise err

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            self._boot_q.put(e)
            return
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=self.headless)
            ctx = browser.new_context()
            page = ctx.new_page()
        except Exception as e:  # noqa: BLE001 — browser/chromium missing at boot
            self._boot_q.put(e)
            return
        self._boot_q.put("ready")
        while True:
            cmd = self._cmd_q.get()
            if cmd is None:  # close signal
                try:
                    ctx.close()
                    browser.close()
                    pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._res_q.put({"closed": True})
                return
            act = cmd.get("action")
            try:
                result = self._do(page, ctx, act, cmd)
                self._res_q.put({"ok": result})
            except Exception as e:  # noqa: BLE001
                self._res_q.put({"error": str(e)})

    @staticmethod
    def _do(page, ctx, act: str, cmd: dict[str, Any]) -> dict[str, Any]:
        if act == "open" or act == "goto":
            resp = page.goto(cmd["url"], wait_until="domcontentloaded", timeout=cmd.get("timeout_ms", 15000))
            page.wait_for_load_state("networkidle", timeout=10000)
            return {"url": page.url, "title": page.title(), "status": resp.status if resp else 0}
        if act == "click":
            page.click(cmd["selector"], timeout=cmd.get("timeout_ms", 10000))
            page.wait_for_load_state("networkidle", timeout=8000)
            return {"url": page.url, "clicked": cmd["selector"]}
        if act == "fill":
            page.fill(cmd["selector"], cmd.get("value", cmd.get("text", "")), timeout=cmd.get("timeout_ms", 10000))
            return {"url": page.url, "filled": cmd["selector"]}
        if act == "type":
            page.press(cmd.get("selector") or "body", cmd["key"], timeout=cmd.get("timeout_ms", 10000))
            return {"url": page.url, "key": cmd["key"]}
        if act == "eval":
            value = page.evaluate(f"() => ({cmd['js']})")
            try:
                json.dumps(value)  # must be JSON-serializable back to the MCP client
            except (TypeError, ValueError):
                value = str(value)
            return {"url": page.url, "result": value}
        if act == "wait":
            sel = cmd.get("selector")
            if sel:
                page.wait_for_selector(sel, timeout=cmd.get("timeout_ms", 15000))
                return {"url": page.url, "waited_for": sel}
            page.wait_for_timeout(cmd.get("timeout_ms", 5000))
            return {"url": page.url, "waited_ms": cmd.get("timeout_ms", 5000)}
        if act == "content":
            html = page.content()
            body_text = page.evaluate("() => document.body ? document.body.innerText : ''")
            return {"url": page.url, "title": page.title(),
                    "text": body_text, "html_chars": len(html)}
        if act == "screenshot":
            import base64
            if not page.viewport_size:
                page.set_viewport_size({"width": 1280, "height": 720})
            last_err = None
            for attempt in range(3):  # transient capture failures are common after idle
                try:
                    shot = page.screenshot(full_page=cmd.get("full_page", False))
                    data = base64.b64encode(shot).decode("ascii")
                    return {"url": page.url, "png_base64": data, "bytes": len(shot)}
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    page.wait_for_timeout(400)
            raise last_err  # type: ignore[misc]
        if act == "cookies":
            return {"cookies": ctx.cookies()}
        raise ValueError(f"unknown session action: {act}")

    def call(self, timeout: int = 125, **cmd) -> dict[str, Any]:
        self._cmd_q.put(cmd)
        out = self._res_q.get(timeout=timeout)
        if isinstance(out, dict) and "ok" in out:
            return out["ok"]
        if isinstance(out, dict) and "error" in out:
            raise RuntimeError(out["error"])
        raise RuntimeError(f"session {self.name!r} closed unexpectedly")

    def close(self):
        self._cmd_q.put(None)
        self._res_q.get(timeout=60)


class SessionManager:
    """Registry of named persistent browser sessions.

    One MCP `session` tool call = one action against a named session.
    Sessions survive across tool calls in the same server process, so
    login flows (fill user/pass → click login → navigate) keep cookies.
    """

    def __init__(self, ttl_seconds: int = 1800):
        self.ttl = ttl_seconds
        self._sessions: dict[str, _SessionThread] = {}
        self._touched: dict[str, float] = {}
        self._lock = Lock()

    def _gc(self):
        now = time.time()
        for name, ts in list(self._touched.items()):
            if now - ts > self.ttl:
                try:
                    self._sessions[name].close()
                except Exception:  # noqa: BLE001
                    pass
                self._sessions.pop(name, None)
                self._touched.pop(name, None)

    def get_or_open(self, name: str, headless: bool = True) -> _SessionThread:
        with self._lock:
            self._gc()
            s = self._sessions.get(name)
            if s is None or not s._thread.is_alive():
                if s is not None:
                    self._sessions.pop(name, None)
                s = _SessionThread(name, headless=headless)
                self._sessions[name] = s
            self._touched[name] = time.time()
            return s

    def close_session(self, name: str) -> bool:
        with self._lock:
            s = self._sessions.pop(name, None)
            self._touched.pop(name, None)
        if s is None:
            return False
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
        return True

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            self._gc()
            now = time.time()
            return [
                {"name": n, "alive": s._thread.is_alive(),
                 "idle_seconds": int(now - self._touched.get(n, now))}
                for n, s in self._sessions.items()
            ]


SESSIONS = SessionManager()


def session_action(
    session: str,
    action: str,
    *,
    url: str | None = None,
    selector: str | None = None,
    text: str | None = None,
    key: str | None = None,
    js: str | None = None,
    timeout_ms: int = 30000,
    full_page: bool = False,
    headless: bool = True,
) -> dict[str, Any]:
    """Drive a named persistent browser session (cookies kept across calls).

    Args:
        session: session name (created on first use).
        action: one of:
            "open"       — (re)open URL in the session, return url/title/status.
            "click"      — click `selector`.
            "fill"       — fill `selector` with `text`.
            "type"       — press `key` (e.g. "Enter") into `selector` (default body).
            "eval"       — run JS expression `js`, return the value.
            "wait"       — wait for `selector` (or sleep `timeout_ms`).
            "content"    — return url/title/visible text of the current page.
            "screenshot" — return PNG bytes (base64, ``full_page`` optional).
            "cookies"    — return the session's cookies.
            "close"      — destroy the session.
        url/selector/text/key/js/timeout_ms/full_page/headless: per-action args.
    """
    t0 = time.perf_counter()
    if action == "close":
        ok = SESSIONS.close_session(session)
        return {"session": session, "action": action, "closed": ok,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
    if action == "list":
        return {"sessions": SESSIONS.list_sessions(),
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
    thread = SESSIONS.get_or_open(session, headless=headless)
    cmd: dict[str, Any] = {"action": "goto" if action == "open" else action}
    if action == "open":
        cmd["url"] = url
        cmd["timeout_ms"] = timeout_ms
    elif action == "click":
        cmd["selector"] = selector
        cmd["timeout_ms"] = min(timeout_ms, 15000)
    elif action == "fill":
        cmd["selector"] = selector
        cmd["text"] = text or ""
        cmd["timeout_ms"] = min(timeout_ms, 15000)
    elif action == "type":
        cmd["selector"] = selector or "body"
        cmd["key"] = key or "Enter"
        cmd["timeout_ms"] = min(timeout_ms, 15000)
    elif action == "eval":
        cmd["js"] = js
    elif action == "wait":
        if selector:
            cmd["selector"] = selector
        cmd["timeout_ms"] = timeout_ms
    elif action == "screenshot":
        cmd["full_page"] = full_page
    out = thread.call(timeout=timeout_ms // 1000 + 95, **cmd)
    out.update({"session": session, "action": action,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000)})
    return out


# ponytail: no network request interception or download handling yet — add
# `on_request` hooks when a login flow needs to capture tokens/redirects.
# Upgrade path: expose page.on("request") capture buffer in _do().


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

def scrape_stealth(
    url: str,
    timeout: int = 60,
    network_idle: bool = True,
    wait_selector: str | None = None,
    wait: int = 0,
    js: str | None = None,
    wait_for: str | None = None,
) -> FetchResult:
    """Open a real Chromium, optionally solve Cloudflare Turnstile, return content.

    Args:
        wait_selector: optional CSS selector to wait for before returning.
        wait: extra milliseconds to wait after the page settles.
        js: JavaScript **expression** evaluated against the live page after it
            settles; the value comes back in ``meta["js_result"]``. Needed for
            data that lives in DOM *properties* instead of serialized HTML —
            e.g. ``document.getElementById('mail').value`` on temp-mail, where
            JS writes the address into the input after an XHR and the attribute
            in the HTML source still reads ``Memuat``.
        wait_for: JavaScript **predicate expression** polled until it returns
            truthy (bounded by ``timeout``). Prefer this over guessing a ``wait``
            duration for content that arrives asynchronously.
    """
    js_box: dict[str, Any] = {}

    def _action(page):
        # Runs inside the browser thread, after CF solve + page stability.
        if wait_for:
            try:
                page.wait_for_function(f"() => ({wait_for})", timeout=timeout * 1000)
            except Exception as e:  # noqa: BLE001 — predicate may never flip
                js_box["wait_for_error"] = str(e)
        if js:
            try:
                js_box["result"] = page.evaluate(f"() => ({js})")
            except Exception as e:  # noqa: BLE001
                js_box["error"] = str(e)

    def _do_stealth():
        from scrapling.fetchers import StealthyFetcher
        kwargs: dict[str, Any] = dict(
            headless=True,
            network_idle=network_idle,
            solve_cloudflare=True,
            timeout=timeout * 1000,
            block_ads=True,
            disable_resources=True,
        )
        if wait_selector:
            kwargs["wait_selector"] = wait_selector
        if wait:
            kwargs["wait"] = wait
        if js or wait_for:
            kwargs["page_action"] = _action
        return StealthyFetcher.fetch(url, **kwargs)

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
    meta: dict[str, Any] = {
        "solved_cloudflare": True,
        "encoding": resp.encoding,
    }
    if js_box.get("result") is not None:
        meta["js_result"] = js_box["result"]
    if js_box.get("error"):
        meta["js_error"] = js_box["error"]
    if js_box.get("wait_for_error"):
        meta["wait_for_error"] = js_box["wait_for_error"]
    return FetchResult(
        url=url,
        final_url=resp.url,
        status=int(resp.status),
        html=html,
        markdown=md,
        title=title,
        method="scrapling.fetch_stealth",
        elapsed_ms=elapsed,
        meta=meta,
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
    extraction_strategy: Any | None = None,
    deep_crawl: bool = False,
    max_pages: int = 5,
    max_depth: int = 2,
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

    if extraction_schema and not extraction_strategy:
        config_kwargs["extraction_strategy"] = JsonCssExtractionStrategy(
            schema=extraction_schema,
        )
    elif extraction_strategy is not None:
        config_kwargs["extraction_strategy"] = extraction_strategy

    if deep_crawl:
        from crawl4ai import BFSDeepCrawlStrategy
        config_kwargs["deep_crawl_strategy"] = BFSDeepCrawlStrategy(
            max_depth=max_depth,
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
    extraction_strategy: Any | None = None,
    deep_crawl: bool = False,
    max_pages: int = 5,
    max_depth: int = 2,
) -> dict[str, Any]:
    """Sync wrapper around crawl4ai. Returns dict of CrawlResult.model_dump() per page."""
    return _run_coro(_arun_crawl4ai(
        url,
        fit_markdown=fit_markdown,
        css_selector=css_selector,
        extraction_schema=extraction_schema,
        extraction_strategy=extraction_strategy,
        deep_crawl=deep_crawl,
        max_pages=max_pages,
        max_depth=max_depth,
    ))


# ---------------------------------------------------------------------------
# Composite: scrape smart (auto-fallback ladder)
# ---------------------------------------------------------------------------

def scrape_smart(
    url: str,
    *,
    prefer: str = "auto",
    timeout: int = 30,
    js: str | None = None,
    wait_for: str | None = None,
) -> FetchResult:
    """Try fast HTTP -> stealth browser. Return the first good result.

    `prefer` can be "auto" | "fast" | "stealth" | "llm".
    `js` / `wait_for` are forwarded to `scrape_stealth` (ignored for fast/llm).
    """
    if prefer == "stealth":
        return scrape_stealth(url, timeout=max(timeout, 60), js=js, wait_for=wait_for)
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
    stealth = scrape_stealth(url, timeout=max(timeout, 60), js=js, wait_for=wait_for)
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

def _scope_to_selector(r: FetchResult, css_selector: str | None) -> FetchResult:
    """Re-scope a FetchResult's html/markdown to css_selector (non-LLM tiers).

    Cheap lxml pass over the already-fetched HTML — no second request.
    Returns r unchanged when no selector or the selector matches nothing.
    """
    if not css_selector or not r.html:
        return r
    try:
        from scrapling.parser import Selector
        nodes = list(Selector(r.html).css(css_selector) or [])
    except Exception:  # noqa: BLE001 — bad selector etc: keep full page
        return r
    if not nodes:
        return r
    html = "\n".join(n.html_content for n in nodes)
    return FetchResult(
        url=r.url, final_url=r.final_url, status=r.status,
        html=html, markdown=_html_to_markdown(html), title=r.title,
        method=r.method, elapsed_ms=r.elapsed_ms,
        meta={**r.meta, "css_selector": css_selector},
    )


def crawl_site(
    root: str,
    *,
    max_pages: int = 5,
    css_selector: str | None = None,
    prefer: str = "auto",
    include_paths: str | None = None,
    exclude_paths: str | None = None,
    max_depth: int = 0,
) -> CrawlResult:
    """Discover URLs on `root`, then scrape each through the smart ladder.

    Path filters (regex, matched against the full URL):
      include_paths — keep only URLs matching
      exclude_paths — drop URLs matching (applied after include)
      max_depth     — 0 = flat harvest (map_urls, default); >0 = BFS from
                      root up to that link depth, honoring the filters
      css_selector  — scope each page's html/markdown to the matched element
                      (llm tier: native crawl4ai css_selector; other tiers:
                      lxml re-scope of the fetched HTML, no extra request)

    For deep semantic crawling (BFS/DFS with filters), prefer="llm" delegates
    to Crawl4AI's BFSDeepCrawlStrategy (max_depth=0 maps to 1 = root + links).
    """
    import time
    from urllib.parse import urljoin, urlparse

    inc_re = re.compile(include_paths) if include_paths else None
    exc_re = re.compile(exclude_paths) if exclude_paths else None

    def _passes(u: str) -> bool:
        if inc_re and not inc_re.search(u):
            return False
        if exc_re and exc_re.search(u):
            return False
        return True

    if prefer == "llm":
        t0 = time.perf_counter()
        data = process_llm(root, fit_markdown=True, deep_crawl=True,
                           max_pages=max_pages, css_selector=css_selector,
                           max_depth=max_depth or 1)
        pages: list[FetchResult] = []
        for r in data["results"]:
            rurl = r.get("url", root)
            if not _passes(rurl):
                continue
            html = r.get("html") or r.get("cleaned_html") or ""
            md = r.get("markdown") or {}
            md_text = md.get("raw_markdown") or md.get("fit_markdown") or _html_to_markdown(html) if isinstance(md, dict) else (str(md) if md else _html_to_markdown(html))
            pages.append(FetchResult(
                url=rurl,
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
    root_host = urlparse(root).netloc

    def _internal_links(html: str, base_url: str) -> list[str]:
        out: list[str] = []
        for m in re.finditer(r'href=["\']([^"\']+)["\']', html or "", flags=re.IGNORECASE):
            href = m.group(1).strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            full = urljoin(base_url, href)
            p = urlparse(full)
            if p.scheme not in ("http", "https"):
                continue
            if root_host and p.netloc != root_host:
                continue
            out.append(f"{p.scheme}://{p.netloc}{p.path}" + (f"?{p.query}" if p.query else ""))
        return out

    pages: list[FetchResult] = []
    if max_depth and max_depth > 0:
        # BFS from root up to max_depth link-hops, applying filters + cap
        visited: set[str] = {root}
        frontier = [root]
        depth = 0
        while frontier and depth <= max_depth and len(pages) < max_pages:
            nxt: list[str] = []
            for u in frontier:
                if len(pages) >= max_pages:
                    break
                try:
                    raw = scrape_smart(u, prefer=prefer)
                    pages.append(_scope_to_selector(raw, css_selector))
                except Exception as e:  # noqa: BLE001
                    pages.append(FetchResult(
                        url=u, final_url=u, status=0, html="", markdown="",
                        title=None, method="error", elapsed_ms=0, meta={"error": str(e)},
                    ))
                    continue
                if depth < max_depth:
                    # discover links on the FULL html — scoping is for stored
                    # content only, or a nav-scoped selector would starve BFS
                    for link in _internal_links(raw.html, raw.final_url or u):
                        if link not in visited and _passes(link):
                            visited.add(link)
                            nxt.append(link)
            frontier = nxt
            depth += 1
    else:
        mapped = map_urls(root, limit=max_pages * 4 if (inc_re or exc_re) else max_pages)
        candidates = [u for u in mapped.urls if _passes(u)][:max_pages]
        for url in candidates:
            try:
                page = _scope_to_selector(scrape_smart(url, prefer=prefer), css_selector)
                pages.append(page)
            except Exception as e:  # noqa: BLE001
                pages.append(FetchResult(
                    url=url, final_url=url, status=0, html="", markdown="",
                    title=None, method="error", elapsed_ms=0, meta={"error": str(e)},
                ))
    return CrawlResult(
        root=root, pages=pages, method="bfs" if max_depth else "map+scrape",
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )


# ---------------------------------------------------------------------------
# Documents — PDF / DOCX / XLSX → markdown (no browser; lazy imports)
# ---------------------------------------------------------------------------

def scrape_document(
    url: str,
    *,
    max_pages: int = 50,
    timeout: int = 60,
) -> dict[str, Any]:
    """Extract text from a PDF/DOCX/PPTX into LLM-ready markdown.

    Content-type sniffs the response; routed to pypdf / python-docx /
    python-pptx (all optional deps). No browser, no JS. Returns
    {url, doc_type, markdown, pages, elapsed_ms} or {error}.
    """
    import io
    import time
    t0 = time.perf_counter()

    from urllib.parse import urlparse
    from scrapling.fetchers import Fetcher

    try:
        resp = Fetcher.get(url, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"error": f"download failed: {e}", "url": url}

    body = getattr(resp, "body", None) or getattr(resp, "content", b"") or b""
    ctype = (getattr(resp, "headers", {}) or {}).get("content-type", "")
    if isinstance(ctype, bytes):
        ctype = ctype.decode("latin-1", "replace")
    ext = urlparse(url).path.lower().rsplit(".", 1)[-1]

    doc_type = ""
    text = ""
    n_pages = 0

    if "pdf" in ctype or ext == "pdf" or body[:5] == b"%PDF-":
        doc_type = "pdf"
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(body))
            chunks = []
            for i, p in enumerate(reader.pages):
                if i >= max_pages:
                    break
                chunks.append(p.extract_text() or "")
                n_pages = i + 1
            text = "\n\n".join(c for c in chunks if c.strip())
        except ImportError:
            return {"error": "pypdf not installed — pip install 'pyrecrawl[docs]' or uv add pypdf", "url": url}
        except Exception as e:  # noqa: BLE001
            return {"error": f"pdf parse failed: {e}", "url": url}
    elif "officedocument.wordprocessingml" in ctype or ext == "docx":
        doc_type = "docx"
        try:
            import docx
            d = docx.Document(io.BytesIO(body))
            text = "\n\n".join(p.text for p in d.paragraphs if p.text.strip())
            for tbl in d.tables:
                text += "\n\n" + "\n".join(" | ".join(c.text for c in row.cells) for row in tbl.rows)
            n_pages = len(d.paragraphs)
        except ImportError:
            return {"error": "python-docx not installed — pip install 'pyrecrawl[docs]'", "url": url}
        except Exception as e:  # noqa: BLE001
            return {"error": f"docx parse failed: {e}", "url": url}
    elif "presentationml" in ctype or ext == "pptx":
        doc_type = "pptx"
        try:
            from pptx import Presentation
            prs = Presentation(io.BytesIO(body))
            slides = []
            for i, slide in enumerate(prs.slides):
                if i >= max_pages:
                    break
                txts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
                if txts:
                    slides.append(f"## Slide {i + 1}\n\n" + "\n\n".join(txts))
                n_pages = i + 1
            text = "\n\n".join(slides)
        except ImportError:
            return {"error": "python-pptx not installed — pip install 'pyrecrawl[docs]'", "url": url}
        except Exception as e:  # noqa: BLE001
            return {"error": f"pptx parse failed: {e}", "url": url}
    else:
        return {"error": f"unsupported document type (content-type={ctype!r}, ext={ext!r})", "url": url}

    return {
        "url": url,
        "doc_type": doc_type,
        "markdown": text.strip(),
        "pages_or_sections": n_pages,
        "bytes": len(body),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


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


# ---------------------------------------------------------------------------
# deep_research — search → scrape → evidence pack (NO LLM synthesis)
# ---------------------------------------------------------------------------

def deep_research(
    query: str,
    *,
    limit: int = 5,
    scrape_top: int = 3,
    prefer: str = "auto",
    max_concurrency: int = 4,
) -> dict[str, Any]:
    """Run a web search and pull the top sources as evidence.

    PyreCrawl stays out of the synthesis step — the calling agent does
    the reading. We return:
      * ranked search hits (title/snippet/url), and
      * markdown + title for the top `scrape_top` sources, with
        stable ``[n]`` citation numbers and the mapping in ``citations``.

    Args:
        query: search string.
        limit: how many search results to fetch (DDG free tier works fine
            for 5–10).
        scrape_top: how many of those to actually fetch content from
            (bigger = more context but slower).
        prefer: ladder preference, same as ``scrape``.
        max_concurrency: parallel workers for the per-URL scrape.

    Returns:
        dict with ``query``, ``hits`` (list), ``evidence`` (list of
        {n, url, title, markdown, status}), ``citations`` (list of
        {n, url, title}), and ``elapsed_ms``.
    """
    t0 = time.perf_counter()
    hits = search_web(query, limit=limit, prefer=prefer) or []
    top_urls: list[str] = []
    seen: set[str] = set()
    for h in hits:
        u = h.get("url", "")
        if u and u not in seen:
            seen.add(u)
            top_urls.append(u)
        if len(top_urls) >= scrape_top:
            break

    evidence: list[dict[str, Any]] = []
    if top_urls:
        batched = batch_scrape(
            top_urls, prefer=prefer,
            max_concurrency=max_concurrency, use_cache=True,
        )
        for i, r in enumerate(batched.get("results", []), start=1):
            evidence.append({
                "n": i,
                "url": r.get("url") or r.get("final_url"),
                "title": r.get("title"),
                "status": r.get("status"),
                "markdown": r.get("markdown", ""),
                "method": r.get("method"),
                "elapsed_ms": r.get("elapsed_ms"),
                "error": r.get("error"),
            })

    citations = [
        {"n": e["n"], "url": e["url"], "title": e["title"]}
        for e in evidence if e.get("error") is None
    ]
    return {
        "query": query,
        "hits": hits,
        "evidence": evidence,
        "citations": citations,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


# ponytail: deep_research could fold in the LLM (summarizer) layer next to
# the search hits, but that adds a non-optional second LLM call and breaks
# the "no API keys" promise. Agent-side synthesis is the right ceiling —
# upgrade path: add optional llm_synthesis=True for hosts that opt in.


# ---------------------------------------------------------------------------
# monitor — change detection with persisted snapshots
# ---------------------------------------------------------------------------

def _monitor_root() -> Path:
    raw = os.environ.get("PYRECRAWL_MONITOR_DIR", "").strip()
    if raw:
        root = Path(raw)
    else:
        root = Path.home() / ".pyrecrawl" / "monitors"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _monitor_path(url: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", url)[:200] or "url"
    return _monitor_root() / f"{safe}.json"


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="ignore")).hexdigest()


def _markdown_or_text(result: dict[str, Any]) -> str:
    return result.get("markdown") or result.get("text") or ""


def monitor(
    url: str,
    *,
    action: str = "check",
    prefer: str = "auto",
    css_selector: str | None = None,
) -> dict[str, Any]:
    """Track a URL over time and report meaningful content changes.

    Args:
        url: target URL.
        action:
          * "check"   — fetch and compare with last snapshot. Returns
            ``status`` of "new" | "unchanged" | "changed" | "error",
            a unified diff (``diff``) when changed, and the new snapshot.
          * "history" — return the list of stored snapshots for ``url``,
            newest first.
          * "forget"  — delete the stored snapshots for ``url``.
        prefer: ladder preference, same as ``scrape``.
        css_selector: optional CSS selector to scope the tracked content
            (stripped from the HTML before hashing/diffing so banner
            changes don't trigger false positives).

    Returns a dict with ``url``, ``status``, and a ``snapshot`` /
    ``diff`` / ``history`` payload depending on ``action``.
    """
    path = _monitor_path(url)
    now = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    if action == "forget":
        if path.exists():
            path.unlink()
        return {"url": url, "status": "forgotten"}

    if action == "history":
        if not path.exists():
            return {"url": url, "status": "no_history", "history": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"url": url, "status": "corrupt", "history": []}
        history = sorted(data.get("snapshots", []), key=lambda s: s["ts"], reverse=True)
        return {"url": url, "status": "ok", "history": history}

    # action == "check"
    try:
        r = scrape_smart(url, prefer=prefer)
        text = r.markdown or ""
        if css_selector:
            try:
                from scrapling.parser import Selector
                node = Selector(r.html or "").css(css_selector)
                text = "\n".join(n.get_all_text(strip=True) for n in node)
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        return {"url": url, "status": "error", "error": str(e)}

    new_hash = _content_hash(text)
    new_snap = {
        "ts": now, "iso": iso, "hash": new_hash,
        "title": r.title, "status": r.status, "method": r.method,
        "text": text,
    }

    prev: dict[str, Any] | None = None
    snapshots: list[dict[str, Any]] = []
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            prev = doc.get("latest")
            snapshots = list(doc.get("snapshots", []))
        except Exception:  # noqa: BLE001
            prev = None

    if prev is None:
        status = "new"
        diff_text = ""
    elif prev["hash"] == new_hash:
        status = "unchanged"
        diff_text = ""
    else:
        status = "changed"
        diff_text = "\n".join(
            difflib.unified_diff(
                (prev.get("text") or "").splitlines(),
                text.splitlines(),
                fromfile=f"{url}@{prev['iso']}",
                tofile=f"{url}@{iso}",
                lineterm="",
                n=2,
            )
        )

    # cap history to 20 snapshots to keep files small
    snapshots.append(new_snap)
    snapshots = snapshots[-20:]
    payload = {"url": url, "latest": new_snap, "snapshots": snapshots}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    out: dict[str, Any] = {
        "url": url, "status": status, "checked_at": iso,
        "hash": new_hash, "title": r.title,
    }
    if diff_text:
        # cap diff size to keep responses small
        out["diff"] = diff_text[:20_000]
        out["diff_truncated"] = len(diff_text) > 20_000
    return out


# ponytail: monitor doesn't honor ETag/Last-Modified yet — when ResponseCache
# grows conditional GET support, the monitor should reuse it so unchanged
# pages don't even get a fresh body. Add once cross-process cache ships.


# ---------------------------------------------------------------------------
# search_papers — academic search via arXiv + Crossref (no API keys, no LLM)
# ---------------------------------------------------------------------------

def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def search_papers(
    query: str,
    *,
    limit: int = 8,
    source: str = "arxiv",
    category: str | None = None,
) -> dict[str, Any]:
    """Search academic papers — arXiv (CS/physics/math) or Crossref (all fields).

    Plain HTTP against public endpoints via stdlib urllib — Scrapling's HTML
    normalization corrupts Atom/JSON API payloads, so this path deliberately
    bypasses the ladder. No API keys, no LLM.

    Args:
        query: free-text search, e.g. "transformer attention".
        limit: max results (1-25).
        source: "arxiv" (default, preprints) or "crossref" (DOI-backed).
        category: arXiv category filter, e.g. "cs.LG" (arxiv only).

    Returns {papers: [{source, id, url, pdf_url, title, authors, summary,
    published, categories}], count, query, source, error, elapsed_ms}.
    """
    import time
    import xml.etree.ElementTree as ET

    t0 = time.perf_counter()
    limit = max(1, min(limit, 25))
    query = (query or "").strip()
    if not query:
        return {"papers": [], "count": 0, "query": query, "source": source,
                "error": None, "elapsed_ms": 0}

    def _fetch_raw(url: str, timeout: int = 30) -> str:
        req = urllib.request.Request(url, headers={
            "User-Agent": "PyreCrawl/0.7 (https://github.com/SanggonBoy/PyreCrawl)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    papers: list[dict[str, Any]] = []
    err: str | None = None
    try:
        if source == "crossref":
            api = ("https://api.crossref.org/works?query.bibliographic="
                   + urllib.parse.quote(query)
                   + f"&rows={min(limit, 20)}&select=DOI,title,author,abstract,issued,URL")
            items = (json.loads(_fetch_raw(api)).get("message") or {}).get("items", [])
            for it in items:
                doi = it.get("DOI")
                dp = ((it.get("issued") or {}).get("date-parts") or [[]])[0]
                papers.append({
                    "source": "crossref",
                    "id": doi,
                    "doi": doi,
                    "url": it.get("URL") or (f"https://doi.org/{doi}" if doi else None),
                    "pdf_url": None,
                    "title": (it.get("title") or [""])[0],
                    "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip()
                                 for a in (it.get("author") or [])[:12]],
                    "summary": _strip_tags(it.get("abstract") or "")[:1500],
                    "published": "-".join(str(p) for p in dp) or None,
                    "categories": [],
                })
        else:  # arxiv (Atom XML)
            q = query.replace('"', " ").strip()
            # arXiv interprets space-separated terms as AND across all fields.
            # Use all:"phrase" only for single-phrase exact queries.
            phrase = " ".join(q.split())
            if not phrase:
                raise ValueError("empty query after cleaning")
            if len(phrase.split()) > 4:
                expr = " ".join(f"all:{w}" for w in phrase.split())
            else:
                expr = f"all:\"{phrase}\""
            if category:
                expr = f"cat:{category} AND ({expr})"
            api = ("http://export.arxiv.org/api/query?search_query="
                   + urllib.parse.quote(expr)
                   + f"&max_results={limit}&sortBy=relevance")
            root = ET.fromstring(_fetch_raw(api))
            for entry in root.iter():
                if _strip_ns(entry.tag) != "entry":
                    continue
                fields: dict[str, str] = {}
                auths: list[str] = []
                cats: list[str] = []
                pdf_url = None
                for child in entry:
                    tag = _strip_ns(child.tag)
                    if tag == "author":
                        nm = child.find("./{http://www.w3.org/2005/Atom}name")
                        if nm is not None and nm.text:
                            auths.append(nm.text.strip())
                    elif tag == "category":
                        if child.get("term"):
                            cats.append(child.get("term"))
                    elif tag == "link":
                        if child.get("title") == "pdf":
                            pdf_url = child.get("href")
                    elif tag in ("id", "title", "summary", "published"):
                        fields[tag] = (child.text or "").strip()
                abs_id = fields.get("id", "")
                papers.append({
                    "source": "arxiv",
                    "id": abs_id.rsplit("/", 1)[-1] if abs_id else None,
                    "doi": None,
                    "url": abs_id or None,
                    "pdf_url": pdf_url,
                    "title": re.sub(r"\s+", " ", fields.get("title", "")),
                    "authors": auths,
                    "summary": re.sub(r"\s+", " ", fields.get("summary", ""))[:1500],
                    "published": fields.get("published") or None,
                    "categories": cats,
                })
    except Exception as e:  # noqa: BLE001
        err = str(e)

    return {
        "papers": papers,
        "count": len(papers),
        "query": query,
        "source": source,
        "error": err,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }
