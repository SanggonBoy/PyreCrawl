# 🔥 PyreCrawl — Web Browsing Superpowers for Your AI Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MCP](https://img.shields.io/badge/MCP-1.0-blue.svg)](https://modelcontextprotocol.io/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/pyrecrawl.svg)](https://pypi.org/project/pyrecrawl/)
[![GitHub stars](https://img.shields.io/github/stars/SanggonBoy/PyreCrawl?logo=github)](https://github.com/SanggonBoy/PyreCrawl/stargazers)
[![Downloads / 30d](https://img.shields.io/endpoint?url=https%3A%2F%2Fpyrecrawl-stats.fajarnugraha90543.workers.dev%2Fbadge%2Fdownloads)](https://pypistats.org/packages/pyrecrawl)
[![Active users / 30d](https://img.shields.io/endpoint?url=https%3A%2F%2Fpyrecrawl-stats.fajarnugraha90543.workers.dev%2Fbadge%2Fusers)](https://github.com/SanggonBoy/PyreCrawl#-privacy--anonymous-usage-ping)

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
| `crawl(root, max_pages=5, prefer="auto", include_paths=None, exclude_paths=None, max_depth=0)` | Multi-page crawl with path filters + true BFS depth |
| `document(url)` | PDF/DOCX/PPTX → markdown (no browser, optional `[docs]` extras) |
| `search(query, limit=10)` | Web search via DuckDuckGo HTML (no API key) |
| `search_papers(query, limit=8, source="arxiv", category=None)` | Academic search via arXiv + Crossref (no API key) — feed `pdf_url` into `document` |
| `batch_scrape(urls[], ...)` | Many URLs in ONE call — parallel, deduped, cache-aware |
| `deep_research(query, limit=5, scrape_top=3)` | Search → evidence pack with [n] citations (no LLM synthesis — your agent does that) |
| `monitor(url, action, css_selector=None)` | Change detection with persisted snapshots + unified diff |
| `session(session, action, ...)` | Persistent browser session (cookies kept) — login walls, multi-step flows, screenshots |
| `cache(action)` | Inspect/clear/enable/disable the HTTP response cache |
| `health()` | Versions + import sanity check |

**MCP Resources** (read-only state without a tool call):
`pyrecrawl://cache/stats` · `pyrecrawl://sessions` · `pyrecrawl://monitors`

**MCP Prompts** (ready-made playbooks): `research(topic)` · `rag_ingest(site)` · `watch_page(url)`

### Env flags

| Variable | Default | Effect |
|---|---|---|
| `PYRECRAWL_CACHE` | off | `1` = in-memory LRU (128 pages), or a directory path (reserved for disk mode) |
| `PYRECRAWL_CACHE_TTL` | `900` | Cache entry lifetime in seconds |
| `PYRECRAWL_MONITOR_DIR` | `~/.pyrecrawl/monitors` | Where monitor snapshots persist |
| `PYRECRAWL_NO_TELEMETRY` | off | `1` = disable the anonymous startup ping (also honors `DO_NOT_TRACK=1`) |

`prefer` options: `"auto"` (default ladder) · `"fast"` (HTTP only) · `"stealth"` (CF bypass) · `"llm"` (deep processing).

---

## 🚀 Install & Use (one-liner)

### 1. Install

#### [UV](https://docs.astral.sh/uv/) (recommended — one command, zero Python setup)

UV is a fast Python package manager that handles Python itself —
no need to install Python separately. Get it once:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

[Learn more about UV →](https://docs.astral.sh/uv/)

Then run PyreCrawl directly — no venv, no `pip install`, no Python download:

```bash
uvx pyrecrawl@latest
```

#### Or via uv tool install (persistent, recommended for regular use)

```bash
uv tool install pyrecrawl
```

#### Or via pipx (alternative)

```bash
pipx install pyrecrawl
```

#### Or via pip into a venv

```bash
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

Available tools:

| Tool | What it does |
|------|-------------|
| `scrape` | Fetch a single URL → markdown (auto-escalates past Cloudflare) |
| `extract` | Scrape + structured extraction via CSS schema → JSON |
| `map_site` | Enumerate all internal URLs from a root |
| `crawl` | Multi-page crawl: discover + scrape in bulk |
| `batch_scrape` | Fetch many URLs in one parallel call |
| `search` | Web search via DuckDuckGo with anti-bot bypass |
| `search_papers` | Academic paper search (arXiv / Crossref) |
| `deep_research` | Search + scrape + citations in one call — **primary research tool** |
| `document` | Extract text from PDF/DOCX/PPTX URLs |
| `monitor` | Track a URL for content changes over time |
| `session` | Persistent browser session for login walls |
| `cache` | Inspect or clear the response cache |
| `health` | Verify engine availability + version |

Plus 3 guided prompts: `research`, `rag_ingest`, `watch_page`.

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
| Academic paper search | ❌ | ✅ arXiv + Crossref (`search_papers`) |
| Hosted search API | ✅ /search | ⚠️ DuckDuckGo HTML + arXiv/Crossref (no key) |

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
python scripts/selfcheck.py        # real-network smoke test (13 tools + engines)
python scripts/probe_stdio.py      # stdio JSON-RPC probe
python scripts/test_ladder_bug.py  # SPA-shell ladder escalation regression
python scripts/test_js_eval.py     # stealth js/wait_for params regression
python scripts/test_scope_selector.py  # crawl css_selector/max_depth wiring
python scripts/test_link_harvest.py    # map/BFS link purity regression
```

---

## 📦 Publish

Maintainers only:

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

GitHub Actions builds + uploads to PyPI via [trusted publishing](https://docs.pypi.org/trusted-publishers/).

---

## 🔔 Stay up to date

PyreCrawl checks PyPI on every startup and reports the latest version — your
MCP agent sees this automatically via the `health()` tool response and can
notify you inline.

To check manually:

```bash
pyrecrawl version
```

To upgrade:

```bash
pyrecrawl update   # runs: uv tool upgrade pyrecrawl
```

**Get notified of new releases:** click **Watch** → **Releases only** at the
[GitHub repo](https://github.com/SanggonBoy/PyreCrawl) to receive email
notifications when a new version is published.

---

> [!NOTE]
> PyreCrawl sends **one anonymous usage ping per 24 h** at server startup — see
> [Privacy](#-privacy--anonymous-usage-ping) for exactly what's sent and how to opt out.

## 🔒 Privacy — anonymous usage ping

PyreCrawl phones home **once per 24 h** with a tiny anonymous ping when the MCP
server starts, so we can count real users (DAU/MAU) instead of raw downloads.

| Sent (4 fields, ~100 bytes) | Never sent |
|---|---|
| Hashed machine id (SHA-256 of hostname+MAC — not reversible) | Your IP (not stored) |
| PyreCrawl version | Any URL you scrape |
| Python version | Any page content or search queries |
| OS family (`windows` / `linux` / `darwin`) | Anything else |

Client code: [`src/pyrecrawl/telemetry.py`](src/pyrecrawl/telemetry.py) (~90 lines, stdlib only) ·
Collector: [`workers/telemetry/`](workers/telemetry/) — a self-hostable Cloudflare Worker + D1, no third-party analytics service.

Opt out any time:

```bash
export PYRECRAWL_NO_TELEMETRY=1   # or the industry-standard DO_NOT_TRACK=1
```

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

<!-- mcp-name: io.github.SanggonBoy/PyreCrawl -->
