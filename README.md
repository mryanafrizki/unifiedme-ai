# Unified AI Proxy

Single OpenAI-compatible endpoint that routes to multiple AI providers with account rotation, credit tracking, and centralized license management.

## Providers

| Tier | Provider | Models |
|------|----------|--------|
| Standard | Kiro (AWS Q) | `claude-sonnet-4`, `deepseek-3.2`, `qwen3-coder-next` |
| Max | CodeBuddy | `claude-opus-4.6`, `gpt-5.4`, `gemini-2.5-pro` |
| WaveSpeed | WaveSpeed LLM | `new-claude-opus-4.7`, `new-gpt-5.2` |
| Max GL | Gumloop | `gl-claude-opus-4-7`, `gl-gpt-5.4` |

## Quick Install

```bash
curl -sSL https://unified-api.roubot71.workers.dev/install | bash
```

Requirements: Python 3.10+, Git, curl.

## Commands

```bash
unifiedme run          # Start proxy (foreground, interactive)
unifiedme start        # Start proxy (background daemon)
unifiedme stop         # Stop background proxy
unifiedme status       # Show status, version, uptime
unifiedme update       # Pull latest code + reinstall deps
unifiedme kill-port    # Kill zombie process on port 1430
unifiedme logout       # Clear license key (switch license)
unifiedme help         # Show help
```

## First Time Setup

```
1. unifiedme run
2. Enter your license key (UNIF-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX)
3. Open http://localhost:1430/dashboard
4. Set your admin password
5. Copy your API key (sk-xxx)
6. Use in your IDE/CLI:
   Authorization: Bearer sk-xxx
```

## API Endpoints

```
POST /v1/chat/completions    OpenAI-compatible chat (routes by model)
POST /v1/messages            Anthropic-compatible messages
GET  /v1/models              List available models
GET  /dashboard              Admin dashboard
```

## Architecture

```
Client (IDE/CLI)
  |
  POST /v1/chat/completions
  Authorization: Bearer sk-xxx
  |
Unified Proxy (:1430)
  |-- Model Router (by model name -> tier)
  |-- Account Manager (sticky rotation, auto-ban)
  |-- Message Filter (find/replace before upstream)
  |-- Watchword Scanner (alert on keywords)
  |
  |-- Kiro Proxy     -> AWS Q API (direct Python)
  |-- CodeBuddy Proxy -> codebuddy.ai
  |-- WaveSpeed Proxy -> wavespeed.ai
  |-- Gumloop Proxy   -> WebSocket + Firebase auth
  |
  |-- License Client  -> Central D1 Database
      (sync accounts, usage logs, alerts every 5 min)
```

## License System

Each deployment requires a license key. Licenses are managed centrally via Cloudflare D1.

- License validates on startup
- Device bound by machine ID (hardware fingerprint)
- Accounts synced from central DB
- Usage logs pushed to central DB
- Watchword alerts for content monitoring

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LICENSE_KEY` | (from file) | License key (auto-saved after first input) |
| `UNIFIED_PORT` | `1430` | Listen port |
| `CENTRAL_API_URL` | `https://unified-api.roubot71.workers.dev` | Central API |

## Project Structure

```
unified/              Core proxy server
  main.py             FastAPI app + CLI license flow
  cli.py              CLI commands (start/stop/update/tunnel/mcp)
  config.py           Configuration + model routing
  license_client.py   Central D1 sync client
  database.py         Local SQLite layer
  account_manager.py  Account rotation + error tracking
  auth_middleware.py   API key + admin auth
  router_proxy.py     /v1/* proxy routes
  router_admin.py     /api/* admin routes
  router_chat.py      /api/chat/* chat + agent endpoints
  router_explorer.py  /api/explorer/* file operations
  agent_loop.py       Agent loop (LLM + MCP tool execution)
  mcp_client.py       MCP Streamable HTTP client
  tunnel_manager.py   Cloudflared tunnel management (multi-port)
  proxy_kiro.py       Kiro direct API proxy
  proxy_codebuddy.py  CodeBuddy proxy
  proxy_wavespeed.py  WaveSpeed proxy
  proxy_gumloop.py    Gumloop WebSocket proxy
  message_filter.py   Content filter engine
  dashboard.html      Admin dashboard UI
  chat.html           AI chat UI (Chat + Agent mode)
  explorer.html       File explorer + Monaco code editor
  kiro/               Kiro API client library
  gumloop/            Gumloop client library
app/                  Browser automation (Camoufox + Playwright)
  providers/
    kiro.py           Kiro Google OAuth login
    codebuddy.py      CodeBuddy Google OAuth login
mcp_server.py         MCP server (27 tools, FastMCP, Streamable HTTP)
VERSION               Current version
install.sh            Auto-installer
```

## MCP Server (Gumloop)

Built-in MCP server that gives Gumloop AI agents full access to your local filesystem, shell, git, web search, and more. Powered by [FastMCP](https://gofastmcp.com).

### Quick Start

```bash
python mcp_server.py
```

Interactive CLI will ask for:
1. **API Key** — your unified proxy API key (`sk-xxx`) for `gl://` image downloads
2. **Workspace** — folder the AI agent can read/write
3. **Port** — default `9876`

Then it auto-starts a [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) tunnel and prints the public URL. Add that URL to your Gumloop MCP settings.

### CLI Options

```bash
# Interactive mode (default)
python mcp_server.py

# Direct mode (skip prompts)
python mcp_server.py --workspace /path/to/project --api-key sk-xxx --port 9876

# No tunnel (manual cloudflared)
python mcp_server.py --workspace /path/to/project --no-tunnel --no-interactive

# Switch workspace (just change the path)
python mcp_server.py --workspace D:\PROJECT\another-project
```

### Available Tools (26)

| Category | Tools | Description |
|----------|-------|-------------|
| **File** | `read_file`, `write_file`, `edit_file`, `delete_file`, `rename_file`, `copy_file`, `file_info`, `read_image` | Full file CRUD + image viewing |
| **Directory** | `list_directory`, `tree`, `create_directory` | Browse and create folders |
| **Search** | `glob_search`, `grep` | Find files by pattern, search content by regex |
| **Shell** | `bash`, `run_python` | Execute shell commands and Python code |
| **Git** | `git` | Run any git subcommand (status, diff, commit, etc.) |
| **Network** | `http_request`, `download_file` | HTTP requests, download files (supports `gl://` URLs) |
| **Archive** | `zip_files`, `unzip_file` | Create and extract zip archives |
| **Text** | `diff`, `patch` | Unified diff between files, apply patches |
| **Research** | `search_docs`, `web_search`, `fetch_url`, `search_github_code` | Library docs (Context7), web search (DuckDuckGo), read web pages, search GitHub code (grep.app) |

### Architecture (with Gumloop)

```
Client (OpenCode / IDE)
  → POST /v1/chat/completions (model: gl-claude-opus-4-7)
  → Unified Proxy (:1430)
    → Gumloop WebSocket (agent processes request)
      → Agent calls MCP tools natively
        → Cloudflare Tunnel (trycloudflare.com)
          → MCP Server (:9876)
            → Executes on local filesystem
            → Returns results to agent
      → Agent streams response
    → Proxy converts WS events → OpenAI SSE
  → Client receives streaming response
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_API_KEY` | (from config file) | Unified proxy API key for `gl://` downloads |
| `PROXY_BASE_URL` | `http://127.0.0.1:1430` | Unified proxy URL |
| `CONTEXT7_API_KEY` | (optional) | Context7 API key for higher rate limits on `search_docs` |

### API Key Persistence

The API key is saved to `unified/data/.mcp_api_key` after first input. Priority order:

```
--api-key CLI arg  >  PROXY_API_KEY env var  >  saved config file
```

---

## Web Chat UI (`/chat`)

Built-in AI chat interface with agent mode — use any model with MCP tools, similar to OpenCode.

### Features

- **Model selector** — dropdown + custom model input for any model
- **Chat mode** — direct LLM streaming (text, thinking blocks, markdown, code highlighting)
- **Agent mode** — LLM + MCP tool execution loop (file ops, bash, git, search, web)
- **MCP integration** — connect to any MCP server, test connectivity, see tool count
- **Streaming** — real-time SSE with thinking blocks, tool calls, tool results
- **Session management** — create, switch, delete, export/import as JSON
- **File upload** — attach images and text files to messages

### Agent Mode

Switch the dropdown to "Agent", enter your MCP server URL, click Test, then chat. The LLM will automatically call MCP tools to accomplish tasks.

```
Agent mode flow:
  User message → LLM (with tool definitions) → tool_calls → MCP server executes
  → results fed back to LLM → repeat until done → final response
```

Supported tools (27): `read_file`, `write_file`, `edit_file`, `bash`, `git`, `grep`, `glob_search`, `tree`, `search_docs` (Context7), `web_search`, `fetch_url`, `search_github_code` (grep.app), and more.

---

## File Explorer (`/explorer`)

Web-based file manager with built-in code editor.

### Features

- **Browse** — navigate directories, breadcrumb navigation
- **Monaco Editor** — VS Code engine, syntax highlighting for 40+ languages, Ctrl+S save
- **File operations** — create, rename, delete, cut/copy/paste, upload, download
- **MCP server management** — enable/disable MCP per folder, start/stop, tunnel management
- **Sortable columns** — name, date modified, size
- **Keyboard shortcuts** — Delete, F2 (rename), Ctrl+C/X/V, Backspace (parent dir)

---

## Tunnel Management

Cloudflared tunnels for exposing local services via public URLs. Supports **multiple simultaneous tunnels** on different ports.

### Commands

```bash
# Start tunnels (each port gets its own URL)
unifiedme tunnel start --port 1430      # Proxy tunnel
unifiedme tunnel start --port 4096      # OpenCode web tunnel
unifiedme tunnel start mcp --port 9876  # MCP server tunnel

# Status (shows all running tunnels)
unifiedme tunnel status

# Stop specific port
unifiedme tunnel stop --port 4096

# Stop all tunnels
unifiedme tunnel stop all

# Install cloudflared + nginx
unifiedme tunnel install
```

### OpenCode Web via Tunnel

```bash
# Start OpenCode web in background
nohup opencode web --port 4096 > /dev/null 2>&1 &

# Create tunnel to it
unifiedme tunnel start --port 4096

# Restart OpenCode web
pkill -f "opencode web"; sleep 1; nohup opencode web --port 4096 > /dev/null 2>&1 &
```

---

## Changelog

### 2025-05-02

- **Agent Loop** — backend MCP tool execution for chat UI, any model can use 27 MCP tools (file ops, bash, git, Context7, grep.app, web search)
- **Monaco Editor** — explorer file preview replaced with full VS Code editor (40+ languages, Ctrl+S save, dirty state tracking)
- **Multiple Tunnels** — each port gets its own independent tunnel URL, `tunnel stop --port/all` support
- **Tool Schema Cleaning** — strips `$schema`, `title`, `additionalProperties` for CodeBuddy compatibility
- **Agent Result Formatting** — tree/entries/stdout/search results rendered as clean text instead of raw JSON
- **MCP Ping** — `/api/chat/mcp-ping` endpoint to test MCP server connectivity
- **Write File API** — `POST /api/explorer/write-file` for saving edited files

### 2025-05-01

- **Chat UI** — web AI chat with streaming, thinking blocks, tool call rendering, markdown, code highlighting, file upload, session management (CRUD, export/import)
- **Custom Model Input** — type any model name for new/unlisted models
- **Explorer Date Column** — sortable columns (name/date/size), default sort by newest
- **Admin Panel** — edit license (max_accounts/devices/tier), transfer accounts between licenses
- **Chat Error Display** — detailed errors (502/503/exhausted) with model info

### Earlier

- MCP server (27 tools, FastMCP, Streamable HTTP)
- Cloudflare Worker admin panel
- VPS orchestration (SSH, auto-install, web terminal)
- File explorer (browse, preview, CRUD, upload)
- Proxy with 4 providers (Kiro, CodeBuddy, WaveSpeed, Gumloop)
- Account rotation, credit tracking, license management

---

## Version

Current: see `VERSION` file.

Check for updates: `unifiedme status` or `unifiedme update`.
