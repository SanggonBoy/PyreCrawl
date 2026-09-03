"""PyreCrawl MCP — LLM-ready scraper/crawler combining Crawl4AI + Scrapling.

A self-hosted Firecrawl alternative with a smart auto-fallback ladder:
  1. fast HTTP (Scrapling Fetcher, curl_cffi)
  2. stealth browser (Scrapling StealthyFetcher, Cloudflare bypass)
  3. LLM processing (Crawl4AI AsyncWebCrawler, BM25, deep crawl, structured extraction)
"""

__version__ = "0.2.1.post0"
