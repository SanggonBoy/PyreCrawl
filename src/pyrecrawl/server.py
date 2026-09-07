"""PyreCrawl — MCP server exposing the scraper/crawler as a stdio tool suite.

.. important::
    Stdio transport mixes raw bytes on stdout with JSON-RPC; ALL log output
    MUST go to stderr. This module routes all logging (root + httpx +
    crawl4ai loggers) to stderr for that reason.
"""

from __future__ import annotations

import json
import logging
import sys

logging.basicConfig(stream=sys.stderr)
for noisy in ("httpx", "httpcore", "crawl4ai", "scrapling"):
    try:
        lg = logging.getLogger(noisy)
        lg.setLevel(logging.WARNING)
        hdl = logging.StreamHandler(sys.stderr)
        hdl.setLevel(logging.WARNING)
        lg.addHandler(hdl)
        lg.propagate = False
    except Exception:  # noqa: BLE001
        pass

from typing import Any

from mcp.server.fastmcp import FastMCP

from .engines import (
    CACHE,
    crawl_site,
    extract_structured,
    extract_llm,
    map_urls,
    process_llm,
    scrape_fast,
    scrape_smart,
    scrape_stealth,
    search_web,
    batch_scrape,
)

log = logging.getLogger("pyrecrawl")
log.setLevel(logging.INFO)
if not log.handlers:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    log.addHandler(h)


SERVER_NAME = "pyrecrawl"
INSTRUCTIONS = (
    "PyreCrawl — self-hosted Firecrawl alternative. Combines Scrapling (fast HTTP / "
    "stealth browser) and Crawl4AI (BM25 fit-markdown, deep crawl, structured "
    "extraction) with a smart auto-fallback ladder. All tools return LLM-ready "
    "markdown. Prefer 'auto' for the default ladder; 'fast' for cheap static pages; "
    "'stealth' for Cloudflare; 'llm' for full browser rendering + BM25 / extraction."
)


# Cap markdown returned inline to keep MCP payloads reasonable.
MAX_MD_CHARS = 60_000
MAX_HTML_CHARS = 200_000


def _trim(result: dict[str, Any], *, include_html: bool = False) -> dict[str, Any]:
    """Trim heavy fields so MCP responses stay under common token caps."""
    if "markdown" in result and isinstance(result["markdown"], str):
        md = result["markdown"]
        if len(md) > MAX_MD_CHARS:
            result["markdown"] = md[:MAX_MD_CHARS]
            result["markdown_truncated"] = True
            result["markdown_full_chars"] = len(md)
    if "html" in result and isinstance(result["html"], str):
        if not include_html or len(result["html"]) > MAX_HTML_CHARS:
            result.pop("html", None)
        else:
            pass
    return result


def _scrape_to_dict(r, *, include_html: bool = False) -> dict[str, Any]:
    out = {
        "url": r.url,
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
    return _trim(out, include_html=include_html)


def build_server() -> FastMCP:
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)

    @mcp.tool()
    def scrape(
        url: str,
        prefer: str = "auto",
        timeout: int = 30,
        include_html: bool = False,
        js: str | None = None,
        wait_for: str | None = None,
    ) -> dict[str, Any]:
        """Scrape a single URL → LLM-ready markdown.

        Args:
            url: Target URL (http/https/file/raw:).
            prefer: "auto" | "fast" | "stealth" | "llm".
                    auto = fast first, escalate to stealth on block/short page.
                    fast = cheap HTTP only (no JS).
                    stealth = real Chromium + Cloudflare solver.
                    llm = full Crawl4AI browser + BM25 fit-markdown.
            timeout: per-attempt timeout in seconds.
            include_html: include raw HTML in the response (large; off by default).
            js: (stealth only) JS expression evaluated against the live page
                after it settles. The value comes back in ``meta.js_result``.
                Use for data that lives in DOM *properties* (e.g. an input's
                ``.value``) rather than in serialized HTML.
            wait_for: (stealth only) JS predicate expression polled until truthy
                (bounded by ``timeout``). Use to wait for content that arrives
                asynchronously after ``network_idle``.
        """
        try:
            if prefer == "fast":
                r = scrape_fast(url, timeout=timeout)
            elif prefer == "stealth":
                r = scrape_stealth(url, timeout=max(timeout, 60), js=js, wait_for=wait_for)
            elif prefer == "llm":
                data = process_llm(url, fit_markdown=True)
                first = data["results"][0] if data["results"] else {}
                html = first.get("html", "") or first.get("cleaned_html", "")
                md_obj = first.get("markdown") or {}
                md_text = (
                    md_obj.get("fit_markdown") or md_obj.get("raw_markdown") or ""
                ) if isinstance(md_obj, dict) else str(md_obj)
                from .engines import _extract_title, _html_to_markdown
                return _trim({
                    "url": url,
                    "final_url": first.get("redirected_url") or url,
                    "status": int(first.get("status_code") or 200),
                    "markdown": md_text or _html_to_markdown(html),
                    "title": _extract_title(html),
                    "method": "crawl4ai.llm",
                    "elapsed_ms": 0,
                    "meta": {
                        "fit": bool(md_obj.get("fit_markdown")) if isinstance(md_obj, dict) else False,
                        "screenshot": first.get("screenshot"),
                    },
                }, include_html=include_html)
            else:
                r = scrape_smart(url, prefer="auto", timeout=timeout, js=js, wait_for=wait_for)
            return _scrape_to_dict(r, include_html=include_html)
        except Exception as e:  # noqa: BLE001
            log.exception("scrape failed")
            return {"error": str(e), "url": url, "method": prefer}

    @mcp.tool()
    def extract(
        url: str,
        schema: dict[str, Any],
        prefer: str = "auto",
    ) -> dict[str, Any]:
        """Scrape + structured extraction using a CSS-based JSON schema.

        The schema is a JsonCssExtractionStrategy schema:
          { "name": "PageItems", "baseSelector": "div.item",
            "fields": [{"name": "title", "selector": "h2", "type": "text"}, ...] }

        Returns parsed JSON in `data`.
        """
        try:
            r = extract_structured(url, schema, prefer=prefer)
            return {
                "url": r.url,
                "data": r.data,
                "method": r.method,
                "elapsed_ms": r.elapsed_ms,
            }
        except Exception as e:  # noqa: BLE001
            log.exception("extract failed")
            return {"error": str(e), "url": url, "schema": schema}

    @mcp.tool()
    def map_site(
        root: str,
        include_pattern: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Enumerate all internal URLs reachable from `root`.

        Args:
            root: Website root (e.g. "https://example.com/docs").
            include_pattern: Optional regex; only URLs matching are returned.
            limit: Hard cap on returned URLs.
        """
        try:
            r = map_urls(root, include_pattern=include_pattern, limit=limit)
            return {
                "root": r.root,
                "urls": r.urls,
                "count": len(r.urls),
                "method": r.method,
                "elapsed_ms": r.elapsed_ms,
            }
        except Exception as e:  # noqa: BLE001
            log.exception("map failed")
            return {"error": str(e), "root": root}

    @mcp.tool()
    def crawl(
        root: str,
        max_pages: int = 5,
        css_selector: str | None = None,
        prefer: str = "auto",
    ) -> dict[str, Any]:
        """Multi-page crawl: discover URLs on `root`, then scrape each.

        prefer="llm" delegates to Crawl4AI's BFS deep-crawl strategy.
        prefer="auto"/"fast"/"stealth" uses the smart ladder per page.
        """
        try:
            r = crawl_site(root, max_pages=max_pages, css_selector=css_selector, prefer=prefer)
            return {
                "root": r.root,
                "pages": [_scrape_to_dict(p, include_html=False) for p in r.pages],
                "count": len(r.pages),
                "method": r.method,
                "elapsed_ms": r.elapsed_ms,
            }
        except Exception as e:  # noqa: BLE001
            log.exception("crawl failed")
            return {"error": str(e), "root": root}

    @mcp.tool()
    def search(
        query: str,
        limit: int = 10,
        prefer: str = "auto",
    ) -> dict[str, Any]:
        """Web search via DuckDuckGo HTML (no API key required).

        Returns [{url, title, snippet}, ...]. The smart ladder bypasses
        DDG's bot detection if needed.
        """
        try:
            results = search_web(query, limit=limit, prefer=prefer)
            return {"query": query, "results": results, "count": len(results)}
        except Exception as e:  # noqa: BLE001
            log.exception("search failed")
            return {"error": str(e), "query": query}

    @mcp.tool(name="batch_scrape")
    def batch_scrape_tool(
        urls: list[str],
        prefer: str = "auto",
        timeout: int = 30,
        max_concurrency: int = 4,
        include_html: bool = False,
    ) -> dict[str, Any]:
        """Scrape MANY URLs in ONE call (parallel, deduped, cache-aware).

        Args:
            urls: Target URLs (deduped automatically; empties dropped).
            prefer: "auto" | "fast" | "stealth" | "llm".
            timeout: per-URL timeout in seconds.
            max_concurrency: parallel workers (default 4).
            include_html: include raw HTML per result (large; off by default).

        Returns {requested, unique, succeeded, failed, results[]}.
        Per-URL failures are isolated — other URLs still succeed.
        """
        from .engines import batch_scrape as _batch
        try:
            out = _batch(
                urls, prefer=prefer, timeout=timeout,
                max_concurrency=max_concurrency, include_html=include_html,
            )
            # Trim markdowns the same way single-page results are trimmed.
            for r in out.get("results", []):
                if isinstance(r.get("markdown"), str) and len(r["markdown"]) > MAX_MD_CHARS:
                    md_len = len(r["markdown"])
                    r["markdown"] = r["markdown"][:MAX_MD_CHARS]
                    r["markdown_truncated"] = True
                    r["markdown_full_chars"] = md_len
            return out
        except Exception as e:  # noqa: BLE001
            log.exception("batch_scrape failed")
            return {"error": str(e), "urls": urls}

    @mcp.tool(name="extract_llm")
    def extract_llm_tool(
        url: str,
        instruction: str,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Extract structured data from a page with NATURAL LANGUAGE instructions.

        No CSS selectors needed — describe what you want in plain English.
        Renders the page (llm tier) then asks an LLM to fill the output.

        Args:
            url: Target URL (http/https/file/raw:).
            instruction: what to extract, in plain English.
                Example: "Extract every product with name, price, and rating."
            schema: optional JSON schema (dict) constraining the output.
                When set, the LLM is forced to return matching JSON.

        LLM config comes from env: PYRECRAWL_LLM_PROVIDER (default
        "openai/gpt-4o-mini", or "ollama/llama3.1" for local),
        PYRECRAWL_LLM_API_TOKEN (or OPENAI_API_KEY), OPENAI_BASE_URL or
        OLLAMA_BASE_URL (default http://localhost:11434/v1).
        """
        try:
            r = extract_llm(url, instruction, schema)
            return {
                "url": r.url,
                "instruction": instruction,
                "data": r.data,
                "method": r.method,
                "elapsed_ms": r.elapsed_ms,
            }
        except Exception as e:  # noqa: BLE001
            log.exception("extract_llm failed")
            return {"error": str(e), "url": url, "instruction": instruction}

    @mcp.tool(name="cache")
    def cache_tool(action: str = "stats") -> dict[str, Any]:
        """Inspect the response cache: stats or clear.

        Args:
            action: "stats" | "clear" | "disable" | "enable".
        """
        try:
            if action == "clear":
                import time as _t
                with CACHE._lock:
                    CACHE._mem.clear()
                return {"cleared": True, "stats": CACHE.stats()}
            if action == "disable":
                CACHE.max_items = 0
                return {"enabled": False}
            if action == "enable":
                CACHE.max_items = 128
                return {"enabled": True, "stats": CACHE.stats()}
            return CACHE.stats()
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    @mcp.tool()
    def health() -> dict[str, Any]:
        """Sanity check: verify engines are importable + return versions."""
        info: dict[str, Any] = {"server": SERVER_NAME}
        for pkg in ("crawl4ai", "scrapling", "mcp"):
            try:
                from importlib.metadata import version as _v
                info[pkg] = _v(pkg)
            except Exception as e:  # noqa: BLE001
                info[f"{pkg}_error"] = str(e)
        return info

    return mcp


def main() -> None:
    mcp = build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
