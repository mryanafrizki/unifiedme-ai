# 01 — Codebase Structure

Unified AI Proxy v1.0.0 — single OpenAI-compatible endpoint yang route ke multiple AI provider dengan account rotation, credit tracking, dan centralized license management via Cloudflare D1.

---

## 1. Arsitektur Tingkat Tinggi

```
Client (IDE / CLI / OpenCode)
  │
  POST /v1/chat/completions
  Authorization: Bearer sk-xxx
  │
  ▼
┌─────────────────────────────────────────────────────┐
│  Unified Proxy (FastAPI, :1430)                     │
│                                                     │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │ Auth        │  │ Model Router │  │ Message    │ │
│  │ Middleware  │  │ (Tier-based) │  │ Filter     │ │
│  └──────┬──────┘  └──────┬───────┘  └─────┬──────┘ │
│         │                │                │        │
│  ┌──────▼────────────────▼────────────────▼──────┐ │
│  │           Account Manager                     │ │
│  │   (sticky rotation, auto-ban, credit track)   │ │
│  └───────────────────┬───────────────────────────┘ │
│                      │                             │
│  ┌───────┬───────┬───┴───┬────────┬────────┬─────┐ │
│  │ Kiro  │ Code  │ Wave  │Gumloop │ChatBAI │Wind │ │
│  │ Proxy │ Buddy │ Speed │ Proxy  │ Proxy  │surf │ │
│  │       │ Proxy │ Proxy │ (WS)   │        │Proxy│ │
│  └───┬───┴───┬───┴───┬───┴───┬────┴───┬────┴──┬──┘ │
│      │       │       │       │        │       │    │
└──────┼───────┼───────┼───────┼────────┼───────┼────┘
       ▼       ▼       ▼       ▼        ▼       ▼
   AWS Q   codebuddy  wavespeed gumloop  b.ai  windsurf
   API     .ai API    .ai API   WS API   API   sidecar

       │
       ▼
┌──────────────────────────────────┐
│  Cloudflare Worker (unified-api) │
│  ┌────────────────────────────┐  │
│  │  D1 Database (SQLite)     │  │
│  │  14 tables, APAC region   │  │
│  └────────────────────────────┘  │
│  ┌────────────────────────────┐  │
│  │  Admin Panel (HTML embed)  │  │
│  └────────────────────────────┘  │
│  Cron: daily 3 AM UTC purge     │
└──────────────────────────────────┘
```

---

## 2. Folder Structure

```
unifiedme-ai/                          # Project root
│
├── VERSION                            # Single source of truth versi (1.0.0)
├── requirements.txt                   # Python dependencies
├── install.sh                         # Auto-installer (curl | bash)
├── Dockerfile                         # Container build
├── start.py                           # Entry point (python start.py)
├── start.sh / stop.sh / setup.sh      # Shell scripts operasional
│
├── unified/                           # ══ CORE PROXY SERVER (FastAPI) ══
│   ├── __init__.py
│   ├── __main__.py                    # python -m unified entry
│   ├── main.py                        # FastAPI app + CLI license flow + lifespan
│   ├── cli.py                         # CLI commands (start/stop/update/tunnel/mcp/vps)
│   ├── config.py                      # Config, Tier enum, model→tier routing table
│   ├── models.py                      # Pydantic models (ChatCompletionRequest, etc.)
│   ├── database.py                    # Local SQLite layer (aiosqlite, WAL mode)
│   │
│   ├── auth_middleware.py             # API key + admin password verification
│   ├── account_manager.py             # Account rotation, error tracking, credit refresh
│   ├── license_client.py              # Central D1 sync (pull accounts, push usage/alerts)
│   ├── message_filter.py              # Content filter engine (find/replace before upstream)
│   ├── batch_runner.py                # Batch login runner (subprocess + SSE progress)
│   │
│   ├── router_proxy.py               # /v1/* — proxy routes (chat/completions, models, messages)
│   ├── router_admin.py                # /api/* — admin routes (accounts, batch, stats, settings)
│   ├── router_chat.py                 # /api/chat/* — chat session management + streaming
│   ├── router_explorer.py             # /api/explorer/* — file browser endpoints
│   ├── router_terminal.py             # /api/terminal/* — web terminal (SSH)
│   ├── router_vps.py                  # /api/vps/* — VPS management endpoints
│   │
│   ├── proxy_kiro.py                  # Kiro proxy → AWS Q API (direct Python, no Go binary)
│   ├── proxy_codebuddy.py             # CodeBuddy proxy → codebuddy.ai (SSE streaming)
│   ├── proxy_wavespeed.py             # WaveSpeed proxy → wavespeed.ai (OpenAI passthrough)
│   ├── proxy_gumloop.py               # Gumloop proxy → WebSocket + Firebase auth + MCP
│   ├── proxy_skillboss.py             # SkillBoss proxy → skillboss.co (Anthropic format)
│   ├── proxy_windsurf.py              # Windsurf proxy → localhost sidecar (Node.js :3003)
│   │
│   ├── tunnel_manager.py              # Cloudflared tunnel management (multi-port)
│   ├── vps_manager.py                 # Remote VPS orchestration via SSH (asyncssh)
│   ├── windsurf_login.py              # Windsurf account login (email/password + Google OAuth)
│   ├── windsurf_manager.py            # Windsurf sidecar spawn/stop/health check
│   ├── agent_loop.py                  # Agent loop: LLM + MCP tool execution cycle
│   ├── mcp_client.py                  # MCP Streamable HTTP client (JSON-RPC)
│   │
│   ├── dashboard.html                 # User dashboard UI
│   ├── chat.html                      # AI chat UI (Chat + Agent mode)
│   ├── explorer.html                  # File explorer + Monaco code editor
│   │
│   ├── kiro/                          # ── Kiro API client library ──
│   │   ├── auth.py                    # KiroAuthManager (token refresh, SigV4 signing)
│   │   ├── config.py                  # Kiro region, endpoints
│   │   ├── http_client.py             # HTTP client for AWS Q API
│   │   ├── model_resolver.py          # Model name → Kiro model ID
│   │   ├── models_openai.py           # OpenAI-format request/response models
│   │   ├── converters_openai.py       # OpenAI → Kiro payload conversion
│   │   ├── converters_core.py         # Core format converters
│   │   ├── streaming_openai.py        # Kiro stream → OpenAI SSE conversion
│   │   ├── streaming_core.py          # Core streaming parser
│   │   ├── parsers.py                 # Response parsers
│   │   ├── payload_guards.py          # Payload validation
│   │   ├── network_errors.py          # Network error handling
│   │   └── utils.py                   # Utilities (conversation ID, etc.)
│   │
│   ├── gumloop/                       # ── Gumloop client library ──
│   │   ├── auth.py                    # GumloopAuth (Firebase refresh token → ID token)
│   │   ├── client.py                  # WebSocket chat, file upload, gummie config
│   │   ├── parser.py                  # WS events → OpenAI SSE chunk builder
│   │   ├── tool_converter.py          # MCP tools → Gumloop format
│   │   └── turnstile.py               # Cloudflare Turnstile captcha solver
│   │
│   ├── chatbai/                       # ── ChatBAI client library ──
│   │   └── proxy.py                   # ChatBAI proxy (api.b.ai)
│   │
│   ├── windsurf/                      # ── Windsurf sidecar (Node.js) ──
│   │   ├── src/                       # Node.js source (WindsurfAPI)
│   │   ├── package.json               # Node dependencies
│   │   ├── Dockerfile                 # Container build
│   │   ├── docker-compose.yml         # Docker compose config
│   │   ├── accounts.json              # Windsurf account credentials
│   │   └── ...                        # Scripts, docs, tests
│   │
│   └── data/                          # Runtime data (gitignored)
│       ├── unified.db                 # Local SQLite database
│       ├── .license                   # Persisted license key
│       ├── .mcp_api_key               # Saved MCP API key
│       └── proxy.log                  # Proxy logs
│
├── worker/                            # ══ CLOUDFLARE WORKER (D1 API + Admin) ══
│   ├── src/
│   │   ├── index.js                   # Worker API source (1114 lines, all endpoints)
│   │   └── admin.html                 # Super Admin Panel HTML
│   ├── dist/
│   │   └── index.js                   # Built output (index.js + embedded HTML + install.sh)
│   ├── build.js                       # Build script (embeds admin.html + install.sh)
│   ├── schema.sql                     # D1 database schema (14 tables)
│   ├── wrangler.json                  # Cloudflare deployment config
│   └── package.json                   # Node deps (wrangler)
│
├── app/                               # ══ BROWSER AUTOMATION (Batch Login) ══
│   ├── browser.py                     # Anti-detection browser factory (Camoufox + Playwright)
│   ├── providers/
│   │   ├── base.py                    # ProviderAdapter ABC + NormalizedAccount dataclass
│   │   ├── kiro.py                    # Kiro Google OAuth login (PKCE + Cognito)
│   │   └── codebuddy.py              # CodeBuddy Google OAuth login (cookie-based)
│   └── errors/
│       ├── exceptions.py              # BatcherError, RetryableBatcherError, NonRetryableBatcherError
│       └── codes.py                   # ErrorCode enum (41 error codes)
│
├── login.py                           # CLI wrapper for account login (uses app/providers)
├── gumloop_login.py                   # Gumloop account login (Firebase auth)
├── add_accounts.py                    # Bulk account addition script
├── batch_dashboard.py                 # Batch login dashboard (standalone)
├── migrate_to_d1.py                   # Migration script: local DB → D1
│
├── mcp_server.py                      # ══ MCP SERVER (27 tools, FastMCP) ══
│                                      # Filesystem, shell, git, web search, Context7
│
├── skillboss/                         # SkillBoss registration scripts
│   └── register.py                    # SkillBoss account registration
│
├── wavespeed/                         # WaveSpeed registration scripts
│   └── register.py                    # WaveSpeed account registration
│
├── chatbai/                           # ChatBAI registration + debug scripts
│   ├── register.py                    # ChatBAI account registration
│   ├── force_push_d1.py               # Force push accounts to D1
│   ├── check_db.py                    # DB inspection
│   ├── test_api.py                    # API testing
│   └── analyze_har.py                 # HAR file analysis
│
├── backup_gumloop_cli/                # Archived Gumloop CLI tools
│
├── intercept_*.py                     # Network intercept/debug scripts
├── _*.py                              # Temporary debug/test scripts
│
├── DEVELOPER.md                       # Developer guide (deploy, D1, admin)
├── README.md                          # User documentation
├── GUMLOOP_HANDOFF.md                 # Gumloop integration handoff doc
└── DEPLOY GITHUB MANUAL.txt           # Manual deploy instructions
```

---

## 3. Backend Cloudflare D1

### 3.1 Worker (`worker/`)

Single Cloudflare Worker (`unified-api`) deployed di `https://unified-api.roubot71.workers.dev`.

**File utama:**
- `src/index.js` — Semua API endpoints dalam 1 file (1114 baris)
- `src/admin.html` — Super Admin Panel (embedded saat build)
- `build.js` — Build script: embed `admin.html` + `install.sh` ke `dist/index.js`
- `schema.sql` — D1 schema (14 tables + indexes)
- `wrangler.json` — Config: D1 binding, cron trigger, admin password

**Build flow:**
```
src/index.js + src/admin.html + ../install.sh
        │ node build.js
        ▼
    dist/index.js (single bundled file)
        │ npx wrangler deploy
        ▼
    Cloudflare Edge (unified-api worker)
```

### 3.2 D1 Database Schema (14 Tables)

| # | Table | Deskripsi | Purge Policy |
|---|-------|-----------|--------------|
| 1 | `licenses` | License keys (id, owner, tier, max_devices, max_accounts, expires_at) | Never |
| 2 | `device_bindings` | Device → license binding (fingerprint, name, OS, IP, machine_id) | Never |
| 3 | `accounts` | Multi-provider accounts per license (email, password, per-provider tokens/status/credits) | Never |
| 4 | `api_keys` | API keys per device (key_hash, prefix, usage_count) | Never |
| 5 | `settings` | Key-value settings per license | Never |
| 6 | `proxies` | Outbound proxy config per device (URL, type, latency, active) | Auto-purge inactive |
| 7 | `filters` | Find/replace rules per license (regex support, hit_count) | Never |
| 8 | `watchwords` | Global keyword monitoring (severity, regex) | Never |
| 9 | `watchword_alerts` | Alert log per device (keyword matched, message snippet, model) | Never |
| 10 | `global_stats` | Lifetime counters per license (requests, tokens, accounts) | Never |
| 11 | `model_usage_stats` | Per-model usage per license (requests, tokens, latency) | Never |
| 12 | `usage_summary` | Aggregated per 5-min bucket (model, tier, proxy) | 30 days |
| 13 | `usage_logs` | Per-request metadata (model, status, latency, account) | 7 days |
| 14 | `used_emails` | Global email duplicate prevention (email + provider) | Never |

**Cron:** Daily 3 AM UTC — purge `usage_logs` > 7 hari, `usage_summary` > 30 hari.

### 3.3 Worker API Endpoints

```
GET  /                              Health check
GET  /health                        Health check
GET  /api/version                   Version check (CLI update)
GET  /admin                         Admin Panel HTML
GET  /install                       Install script (curl | bash)

POST /api/auth/activate             License activation + device binding
POST /api/auth/heartbeat            Device heartbeat (update last_seen)

GET  /api/sync/pull                 Pull accounts, settings, filters, watchwords, proxies
POST /api/sync/push                 Push usage logs, alerts, account updates
POST /api/sync/check-emails         Check email duplicates across licenses

── Admin (require X-Admin-Password) ──
GET    /api/admin/licenses          List licenses (paginated)
POST   /api/admin/licenses          Create license
GET    /api/admin/licenses/:id      Get license detail
PUT    /api/admin/licenses/:id      Update license
DELETE /api/admin/licenses/:id      Delete license
POST   /api/admin/licenses/:id/edit Edit license (max_accounts, tier, etc.)
POST   /api/admin/licenses/:id/transfer  Transfer accounts between licenses

GET    /api/admin/stats             Global stats
GET    /api/admin/stats/:id         Per-license stats

GET    /api/admin/watchwords        List watchwords
POST   /api/admin/watchwords        Create watchword
PUT    /api/admin/watchwords/:id    Update watchword
DELETE /api/admin/watchwords/:id    Delete watchword

GET    /api/admin/global-filters    List global filters
POST   /api/admin/global-filters    Create global filter
PUT    /api/admin/global-filters/:id Update global filter
DELETE /api/admin/global-filters/:id Delete global filter

GET    /api/admin/alerts            List watchword alerts (paginated)
POST   /api/admin/alerts/:id/ack    Acknowledge alert

GET    /api/admin/logs              Usage logs (paginated)
GET    /api/admin/accounts          List all accounts

POST   /api/admin/devices/:id/ban   Ban device (global by machine_id)
POST   /api/admin/devices/:id/unban Unban device
DELETE /api/admin/devices/:id       Delete device
GET    /api/admin/banned-devices    List banned devices
DELETE /api/admin/banned-devices/:id Unban by machine
```

---

## 4. Batch Login System

### 4.1 Flow

```
Dashboard UI (/dashboard)
  │ POST /api/batch/start {emails, passwords, providers, headless, concurrency}
  ▼
router_admin.py → batch_runner.py (BatchState)
  │
  │ Untuk setiap account:
  │   1. Insert ke local DB (status: pending)
  │   2. Spawn subprocess per provider
  │   3. SSE broadcast progress ke dashboard
  ▼
┌─────────────────────────────────────────────┐
│  Subprocess: python login.py                │
│                                             │
│  login.py                                   │
│    ├── KiroProviderAdapter (app/providers/)  │
│    ├── CodeBuddyProviderAdapter             │
│    └── Output: JSON lines (stdout)          │
│         {type: "progress|result|error"}     │
│                                             │
│  Provider flow:                             │
│    1. bootstrap_session() → Camoufox browser│
│    2. authenticate() → Google OAuth / email │
│    3. fetch_tokens() → API keys/tokens      │
│    4. fetch_quota() → credit balance        │
│    5. cleanup_session() → close browser     │
└─────────────────────────────────────────────┘
  │
  ▼
batch_runner.py
  │ Parse JSON output dari subprocess
  │ Update local DB (tokens, credits, status)
  │ Push ke D1 via license_client
  ▼
Account ready untuk proxy rotation
```

### 4.2 Provider Adapters (`app/providers/`)

| File | Provider | Auth Method | Token Output |
|------|----------|-------------|--------------|
| `kiro.py` | Kiro (AWS Q) | PKCE + Cognito OAuth | access_token, refresh_token, profile_arn |
| `codebuddy.py` | CodeBuddy | Google OAuth (cookie-based, Camoufox) | API key |

**Base class** (`base.py`):
```python
class ProviderAdapter(ABC):
    async def parse_account(raw_line) → NormalizedAccount
    async def bootstrap_session(account) → session
    async def authenticate(account, session) → auth_state
    async def fetch_tokens(account, auth_state, session) → tokens
    async def fetch_quota(account, tokens, session) → quota
    async def cleanup_session(session) → None
```

### 4.3 Non-Browser Login Scripts

| Script | Provider | Method |
|--------|----------|--------|
| `gumloop_login.py` | Gumloop | Firebase email/password auth (HTTP) |
| `wavespeed/register.py` | WaveSpeed | HTTP registration |
| `chatbai/register.py` | ChatBAI | HTTP registration |
| `skillboss/register.py` | SkillBoss | HTTP registration |
| `unified/windsurf_login.py` | Windsurf | Email/password + Google OAuth (Camoufox) |

### 4.4 Browser Factory (`app/browser.py`)

Anti-detection browser menggunakan **Camoufox** (Firefox fork) + **Playwright**:
- OS fingerprint randomization (weighted Windows)
- Screen resolution randomization
- WebGL/timezone spoofing
- Automation trace removal

### 4.5 Error Handling (`app/errors/`)

- `ErrorCode` enum — 41 error codes (input, auth, network, browser, provider, system)
- `BatcherError` → base exception (code, message, retryable flag)
- `RetryableBatcherError` → auto-retry (network timeout, rate limit, etc.)
- `NonRetryableBatcherError` → permanent fail (invalid credentials, account locked)

---

## 5. Proxy v1 (`unified/`)

### 5.1 Request Flow

```
Client
  │ POST /v1/chat/completions
  │ Authorization: Bearer sk-xxx
  │ Body: {model: "claude-sonnet-4", messages: [...]}
  ▼
auth_middleware.py
  │ verify_api_key() → check key_hash di local DB
  ▼
router_proxy.py
  │ 1. Parse body → extract model name
  │ 2. config.get_tier(model) → Tier enum
  │ 3. message_filter.filter_messages() → apply find/replace rules
  │ 4. account_manager.get_next_account(tier) → sticky rotation
  │ 5. Route ke provider proxy berdasarkan tier
  │ 6. Retry loop (max 3 accounts) on failure
  │ 7. Log usage ke local DB + buffer untuk D1 sync
  ▼
Provider Proxy (salah satu):
  ├── proxy_kiro.py        → Tier.STANDARD
  ├── proxy_codebuddy.py   → Tier.MAX
  ├── proxy_wavespeed.py   → Tier.WAVESPEED
  ├── proxy_gumloop.py     → Tier.MAX_GL
  ├── chatbai/proxy.py     → Tier.CHATBAI
  ├── proxy_skillboss.py   → Tier.SKILLBOSS
  └── proxy_windsurf.py    → Tier.WINDSURF
```

### 5.2 Tier & Model Routing (`config.py`)

| Tier | Prefix/Pattern | Provider | Contoh Model |
|------|---------------|----------|--------------|
| `standard` | (default) | Kiro (AWS Q) | `claude-sonnet-4`, `deepseek-3.2`, `qwen3-coder-next` |
| `max` | (explicit list) | CodeBuddy | `claude-opus-4.6`, `gpt-5.4`, `gemini-2.5-pro` |
| `wavespeed` | `new-*` atau `provider/model` | WaveSpeed | `new-claude-opus-4.7`, `anthropic/claude-opus-4.7` |
| `max_gl` | `gl-*` | Gumloop | `gl-claude-opus-4-7`, `gl-gpt-5.4` |
| `chatbai` | `bchatai-*` | ChatBAI | `bchatai-gpt-5.5`, `bchatai-claude-opus-4.7` |
| `skillboss` | `skboss-*` | SkillBoss | `skboss-claude-opus-4.7`, `skboss-gpt-5.4` |
| `windsurf` | `windsurf-*` | Windsurf | `windsurf-claude-opus-4.6`, `windsurf-gpt-5.5` (140+ models) |

### 5.3 Provider Proxies

| Proxy | Upstream | Protocol | Auth |
|-------|----------|----------|------|
| `proxy_kiro.py` | `q.{region}.amazonaws.com` | HTTPS + SigV4 | access_token + refresh_token |
| `proxy_codebuddy.py` | `codebuddy.ai/v2/chat/completions` | HTTPS SSE | API key header |
| `proxy_wavespeed.py` | `llm.wavespeed.ai/v1/chat/completions` | HTTPS (OpenAI compat) | API key header |
| `proxy_gumloop.py` | `ws.gumloop.com/ws/gummies` | WebSocket | Firebase ID token + Turnstile |
| `chatbai/proxy.py` | `api.b.ai` | HTTPS | API key / session token |
| `proxy_skillboss.py` | `api.skillboss.co/v1/run` | HTTPS (Anthropic format) | API key header |
| `proxy_windsurf.py` | `localhost:3003/v1/chat/completions` | HTTPS (sidecar) | Internal key header |

### 5.4 Account Manager (`account_manager.py`)

- **Sticky rotation**: account di-"pin" ke tier sampai error
- **Auto-ban**: 3 consecutive errors → status `failed`, clear sticky pointer
- **Credit tracking**: per-provider credit deduction setelah request
- **Instant D1 push**: auto-fail langsung push ke D1 (critical change)
- **Credit refresh**: periodic check via provider API

### 5.5 License Client (`license_client.py`)

Sync loop antara local proxy dan central D1:

```
Setiap 2 menit (SYNC_INTERVAL=120):
  1. POST /api/auth/heartbeat → update last_seen
  2. GET  /api/sync/pull      → pull accounts, settings, filters, watchwords
  3. POST /api/sync/push      → push usage logs, alerts, account updates
```

- **Device fingerprint**: machine-id based (Linux: `/etc/machine-id`, Windows: `Win32_ComputerSystemProduct.UUID`)
- **Tombstone set**: prevent re-push of deleted accounts
- **Watchword scanning**: scan message content, buffer alerts for push

### 5.6 Kiro Client Library (`unified/kiro/`)

Full Kiro API client (menggantikan Go binary):

| File | Fungsi |
|------|--------|
| `auth.py` | Token management (refresh, SigV4 signing untuk AWS) |
| `config.py` | Region, endpoint URLs |
| `http_client.py` | HTTP client untuk AWS Q API |
| `model_resolver.py` | Model name → Kiro internal model ID |
| `converters_openai.py` | OpenAI format → Kiro payload |
| `converters_core.py` | Core format converters |
| `streaming_openai.py` | Kiro event stream → OpenAI SSE |
| `streaming_core.py` | Core streaming parser |
| `parsers.py` | Response parsers |
| `payload_guards.py` | Payload validation/sanitization |
| `network_errors.py` | Network error classification |

### 5.7 Gumloop Client Library (`unified/gumloop/`)

| File | Fungsi |
|------|--------|
| `auth.py` | Firebase refresh token → ID token exchange |
| `client.py` | WebSocket chat, file upload, gummie config update |
| `parser.py` | WS events → OpenAI SSE chunk builder |
| `tool_converter.py` | MCP tools → Gumloop format conversion |
| `turnstile.py` | Cloudflare Turnstile captcha solver |

---

## 6. Komponen Pendukung

### 6.1 MCP Server (`mcp_server.py`)

Standalone MCP server (FastMCP, Streamable HTTP) — 27 tools untuk Gumloop AI agents:

| Category | Tools |
|----------|-------|
| File | `read_file`, `write_file`, `edit_file`, `delete_file`, `rename_file`, `copy_file`, `file_info`, `read_image` |
| Directory | `list_directory`, `tree`, `create_directory` |
| Search | `glob_search`, `grep` |
| Shell | `bash`, `run_python` |
| Git | `git` |
| Network | `http_request`, `download_file` |
| Archive | `zip_files`, `unzip_file` |
| Text | `diff`, `patch` |
| Research | `search_docs`, `web_search`, `fetch_url`, `search_github_code` |

### 6.2 Agent Loop (`agent_loop.py` + `mcp_client.py`)

Backend agent execution untuk Chat UI:
1. Connect ke MCP server → fetch tool definitions
2. Convert MCP tools → OpenAI function calling format
3. LLM call via local proxy → parse tool_calls
4. Execute tools via MCP → append results → loop
5. Stream SSE events ke UI (thinking, tool calls, results, text)

### 6.3 Tunnel Manager (`tunnel_manager.py`)

Cloudflared tunnel management — multiple simultaneous tunnels:
- Proxy tunnel (:1430)
- MCP tunnel (:9876)
- Custom port tunnels
- 3 modes: trycloudflare, IP VPS, custom domain (nginx)

### 6.4 VPS Manager (`vps_manager.py`)

Remote VPS orchestration via SSH (asyncssh):
- SSH connection (password auth)
- Remote command execution
- Auto-install dependencies
- Service management (start/stop)
- SFTP file upload

### 6.5 Windsurf Sidecar (`unified/windsurf/`)

Node.js proxy (WindsurfAPI) yang jalan di localhost:3003:
- Managed oleh `windsurf_manager.py` (spawn/stop/health)
- Login via `windsurf_login.py` (email/password + Google OAuth)
- 140+ model support

### 6.6 Web UIs

| Path | File | Deskripsi |
|------|------|-----------|
| `/dashboard` | `unified/dashboard.html` | Admin dashboard (accounts, batch, stats, settings) |
| `/chat` | `unified/chat.html` | AI chat (streaming, thinking blocks, agent mode + MCP) |
| `/explorer` | `unified/explorer.html` | File browser + Monaco code editor |
| `/admin` | `worker/src/admin.html` | Super Admin Panel (Cloudflare Worker) |

---

## 7. Data Flow: Local ↔ Central

```
┌──────────────────────┐         ┌──────────────────────┐
│  Local Proxy          │         │  Cloudflare Worker    │
│  (user's machine)     │         │  (unified-api)        │
│                       │         │                       │
│  unified.db (SQLite)  │◄──pull──│  D1 Database          │
│  - accounts           │         │  - accounts           │
│  - settings           │──push──►│  - usage_logs         │
│  - filters            │         │  - usage_summary      │
│  - api_keys           │         │  - watchword_alerts   │
│  - usage buffer       │         │  - global_stats       │
│                       │         │  - model_usage_stats  │
│  Sync: every 2 min    │         │                       │
│  license_client.py    │         │  Purge: daily 3 AM    │
└──────────────────────┘         └──────────────────────┘
```

---

## 8. Key Config & Env Vars

| Variable | Default | Lokasi |
|----------|---------|--------|
| `UNIFIED_PORT` | `1430` | Proxy listen port |
| `CENTRAL_API_URL` | `https://unified-api.roubot71.workers.dev` | D1 API URL |
| `ADMIN_PASSWORD` | `kUcingku0` | Admin password (overridable via DB) |
| `LICENSE_KEY` | (from `.license` file) | License key |
| `SYNC_INTERVAL` | `120` | Sync interval (seconds) |
| `WINDSURF_PORT` | `3003` | Windsurf sidecar port |
| `PROXY_ENABLED` | `false` | Outbound proxy toggle |
| `PROXY_URL` | (empty) | Outbound proxy URL |

---

## 9. CLI Commands (`unifiedme`)

```
unifiedme run              Start proxy (foreground, interactive)
unifiedme start            Start proxy (background daemon)
unifiedme stop             Stop background proxy
unifiedme status           Show status, version, uptime
unifiedme update           Pull latest code + reinstall deps
unifiedme fix              Check environment, auto-install missing deps
unifiedme kill-port        Kill zombie process on port 1430
unifiedme logout           Clear license key
unifiedme addaccounts add  Batch add accounts (interactive)
unifiedme windsurf env     Setup Windsurf sidecar .env
unifiedme mcp add <path>   Add MCP server for a folder
unifiedme tunnel start     Start cloudflared tunnel
unifiedme vps list         List registered VPS servers
unifiedme help             Show help
```
