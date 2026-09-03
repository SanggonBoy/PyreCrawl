# 🔥 PyreCrawl — Self-Hosted Firecrawl Alternative as an MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MCP](https://img.shields.io/badge/MCP-1.0-blue.svg)](https://modelcontextprotocol.io/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/pyrecrawl.svg)](https://pypi.org/project/pyrecrawl/)

A **self-hosted Firecrawl alternative** combining two best-in-class open-source scrapers
behind a single Model Context Protocol (MCP) server:

- **[Scrapling](https://github.com/D4Vinci/Scrapling)** — fast HTTP (curl_cffi) + stealth browser with **Cloudflare Turnstile bypass**, adaptive element tracking, and XHR capture
- **[Crawl4AI](https://github.com/unclecode/crawl4ai)** — LLM-first crawler: BM25 fit-markdown, citations, structured extraction, deep crawl (BFS/DFS/BestFirst)

A **smart auto-fallback ladder** tries the cheapest engine that succeeds:

```
fast HTTP (Scrapling)
    │  (403/503/Cloudflare challenge or empty body)
    ▼
stealth browser (Scrapling StealthyFetcher + CF solver)
    │  (still blocked or page needs full JS rendering)
    ▼
full LLM processing (Crawl4AI AsyncWebCrawler + BM25)
```

## ⚡ Tools exposed

| Tool | What it does |
|---|---|
| `scrape(url, prefer="auto")` | Single URL → LLM-ready markdown |
| `extract(url, schema)` | Scrape + structured extraction (JsonCss schema) |
| `map_site(root, include_pattern=None, limit=200)` | Enumerate all internal URLs |
| `crawl(root, max_pages=5, prefer="auto")` | Multi-page crawl with auto-fallback per page |
| `search(query, limit=10)` | Web search via DuckDuckGo HTML (no API key) |
| `health()` | Versions + import sanity check |

`prefer` options: `"auto"` (default ladder) · `"fast"` (HTTP only) · `"stealth"` (CF bypass) · `"llm"` (full Crawl4AI).

---

## 🚀 Install & Use (one-liner)

### 1. Install

```bash
# Using uv (recommended — fast, isolated, no venv needed)
uv tool install pyrecrawl

# Or pipx (alternative)
pipx install pyrecrawl

# Or pip into a venv
pip install pyrecrawl
```

### 2. One-time browser engines

```bash
pyrecrawl setup
```

This installs Playwright Chromium + Scrapling engines (~2 min, one-time).

### 3. Register with your AI agent

```bash
# Auto-detect installed agents and write their MCP configs
pyrecrawl install

# Or target specific agents
pyrecrawl install claude-desktop cursor

# Dry-run to preview what would change
pyrecrawl install --dry-run
```

Supported agents: `claude-desktop`, `claude-code`, `cursor`, `vscode`, `codex`, `opencode`, `hermes`.

### 4. Start chatting

After installing + registering, **restart your agent** (or start a new session). Then ask:

> *"Scrape https://example.com and summarize it."*

The tools appear as `mcp_pyrecrawl_scrape`, `mcp_pyrecrawl_extract`, `mcp_pyrecrawl_map_site`, `mcp_pyrecrawl_crawl`, `mcp_pyrecrawl_search`, `mcp_pyrecrawl_health`.

---

## 📚 Manual config (if `pyrecrawl install` doesn't match your setup)

### Claude Desktop

**Config file**
- Linux: `~/.config/Claude/claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%AppData%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "pyrecrawl": {
      "command": "uvx",
      "args": ["--from", "pyrecrawl", "pyrecrawl", "serve"]
    }
  }
}
```

### Claude Code

**Config file**: project-scoped `.mcp.json`

```json
{
  "mcpServers": {
    "pyrecrawl": {
      "command": "uvx",
      "args": ["--from", "pyrecrawl", "pyrecrawl", "serve"]
    }
  }
}
```

### Cursor

**Config file**: `~/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "pyrecrawl": {
      "command": "uvx",
      "args": ["--from", "pyrecrawl", "pyrecrawl", "serve"]
    }
  }
}
```

### VS Code / Copilot

**Config file**: `.vscode/mcp.json` (project-scoped)

```json
{
  "servers": {
    "pyrecrawl": {
      "command": "uvx",
      "args": ["--from", "pyrecrawl", "pyrecrawl", "serve"],
      "type": "stdio"
    }
  }
}
```

### Codex CLI

**Config file**: `~/.codex/config.toml`

```toml
[mcp_servers.pyrecrawl]
command = "uvx"
args = ["--from", "pyrecrawl", "pyrecrawl", "serve"]
```

### OpenCode

**Config file**: `~/.config/opencode/opencode.json`

```json
{
  "mcp": {
    "pyrecrawl": {
      "type": "local",
      "command": ["uvx", "--from", "pyrecrawl", "pyrecrawl", "serve"],
      "enabled": true
    }
  }
}
```

### Hermes

**Config file**
- Linux/macOS: `~/.hermes/config.yaml`
- Windows: `%LocalAppData%\hermes\config.yaml`

```yaml
mcp_servers:
  pyrecrawl:
    command: uvx
    args:
      - --from
      - pyrecrawl
      - pyrecrawl
      - serve
    enabled: true
```

> **Windows note:** `uvx` must be on PATH. If not, use the full path to `uvx.exe` (e.g. `C:\Users\<you>\AppData\Local\hermes\bin\uvx.exe`).

---

## 🧠 Why two engines?

| Concern | Scrapling | Crawl4AI |
|---|---|---|
| Static HTML page | ✅ curl_cffi, ~200ms | ✅ browser overhead, ~3s |
| Cloudflare-protected | ✅ Turnstile solver | ⚠️ requires stealth setup |
| JS-heavy SPA | ✅ Chromium real browser | ✅ same |
| LLM-ready markdown | ⚠️ basic | ✅ BM25 + citations + fit |
| Structured extraction (CSS schema) | ❌ | ✅ JsonCss strategy |
| Deep crawl (BFS/DFS/BestFirst) | ✅ Spider + AutoThrottle | ✅ BFS/DFS/BestFirst + adaptive |
| Adaptive element tracking | ✅ parser relocates moved elements | ❌ |

**PyreCrawl = Scrapling for fetch & bypass + Crawl4AI for processing & extraction.**

---

## 📊 Compared to Firecrawl (hosted)

| | Firecrawl | PyreCrawl |
|---|---|---|
| Cost | Free 1k/mo, then $16–333/mo | **Free, self-hosted** |
| Local LLM support | ❌ | ✅ Ollama / any LLM |
| Cloudflare bypass | ✅ (Fire-Engine, paid) | ✅ (free, Scrapling) |
| Markdown + BM25 | ✅ | ✅ |
| Self-host | ❌ | ✅ |
| Hosted search API | ✅ /search | ⚠️ DuckDuckGo HTML (no key) |

---

## 🔧 Development

```bash
git clone https://github.com/SanggonBoy/PyreCrawl.git
cd PyreCrawl
uv venv --python 3.12 .venv
source .venv/Scripts/activate  # Windows; or .venv/bin/activate on macOS/Linux
uv pip install -e ".[dev]"
python -m playwright install chromium
scrapling install
```

### Run tests

```bash
python scripts/selfcheck.py   # real-network smoke test
python scripts/probe_stdio.py # stdio JSON-RPC probe
```

---

## 📦 Publish

Maintainers only:

```bash
git tag v0.2.1
git push origin v0.2.1
```

GitHub Actions builds + uploads to PyPI via [trusted publishing](https://docs.pypi.org/trusted-publishers/).

---

## 📜 Uninstall

```bash
# Remove from all agent configs
pyrecrawl uninstall

# Remove the package
uv tool uninstall pyrecrawl
```

---

## 🛡️ License

MIT — see [LICENSE](LICENSE).
