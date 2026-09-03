# 🔥 PyreCrawl — Web Browsing Superpowers for Your AI Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MCP](https://img.shields.io/badge/MCP-1.0-blue.svg)](https://modelcontextprotocol.io/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/pyrecrawl.svg)](https://pypi.org/project/pyrecrawl/)

**One command gives any AI agent the whole web.** Scrape, extract, crawl, map, and search —
self-hosted, no API keys, no rate limits, no subscription.

PyreCrawl speaks **MCP** (Model Context Protocol), the standard tool interface for Claude,
Cursor, VS Code, Codex, OpenCode, Hermes, and any MCP-compatible agent.

A **smart auto-fallback ladder** always picks the cheapest method that succeeds:

```
fast HTTP
    │  (403/503/Cloudflare challenge or empty body)
    ▼
stealth browser (real Chromium + Cloudflare solver)
    │  (still blocked, or the page needs full JS rendering)
    ▼
deep processing (LLM-ready markdown, citations, structured extraction)
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

`prefer` options: `"auto"` (default ladder) · `"fast"` (HTTP only) · `"stealth"` (CF bypass) · `"llm"` (deep processing).

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

This installs Chromium + stealth browser engines (~2 min, one-time).

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

## 🧠 How the ladder chooses

PyreCrawl runs each request through three tiers, stopping at the first one that returns
a complete, LLM-ready result:

| Concern | Fast tier | Stealth tier | Deep tier |
|---|---|---|---|
| Static HTML page | ✅ ~200ms | — | — |
| Cloudflare-protected | ❌ | ✅ Turnstile solver | — |
| JS-heavy SPA | ❌ | ✅ real Chromium | — |
| Live DOM data (input `.value`, JS state) | ❌ | ✅ `js` param | — |
| LLM-ready markdown + citations | — | — | ✅ BM25, fit-markdown |
| Structured extraction (CSS schema) | — | — | ✅ |
| Deep crawl (BFS/DFS/BestFirst) | — | — | ✅ adaptive |

The agent never has to pick. `prefer="auto"` does it every call.

### Live DOM data with `js` and `wait_for`

Some sites keep the data you want in a DOM *property* (e.g. an `<input>`'s `.value`)
that JS writes after an XHR — it never appears in the serialized HTML. The
`scrape` tool accepts two stealth-tier params for exactly this:

```json
{
  "url": "https://temp-mail.org/id",
  "prefer": "stealth",
  "wait_for": "document.getElementById('mail').value.includes('@')",
  "js": "document.getElementById('mail').value"
}
```

- `wait_for` — a JS **predicate expression** polled until truthy (bounded by `timeout`).
  Use it instead of guessing a sleep for anything that arrives asynchronously.
- `js` — a JS **expression** evaluated once the page settles; the value comes back
  in `meta.js_result`. Errors are captured in `meta.js_error` (the page result is
  still returned, never a crash).

---

## 📊 Compared to Firecrawl (hosted)

| | Firecrawl | PyreCrawl |
|---|---|---|
| Cost | Free 1k/mo, then $16–333/mo | **Free, self-hosted** |
| Local LLM support | ❌ | ✅ Ollama / any LLM |
| Cloudflare bypass | ✅ (Fire-Engine, paid) | ✅ (free, built-in) |
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

## 🙏 Credits

Built on the shoulders of [Scrapling](https://github.com/D4Vinci/Scrapling) and
[Crawl4AI](https://github.com/unclecode/crawl4ai) — both MIT, both excellent.

<!-- mcp-name: io.github.SanggonBoy/PyreCrawl -->
