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
  cli.py              CLI commands (start/stop/update/etc)
  config.py           Configuration + model routing
  license_client.py   Central D1 sync client
  database.py         Local SQLite layer
  account_manager.py  Account rotation + error tracking
  auth_middleware.py   API key + admin auth
  router_proxy.py     /v1/* proxy routes
  router_admin.py     /api/* admin routes
  proxy_kiro.py       Kiro direct API proxy
  proxy_codebuddy.py  CodeBuddy proxy
  proxy_wavespeed.py  WaveSpeed proxy
  proxy_gumloop.py    Gumloop WebSocket proxy
  message_filter.py   Content filter engine
  batch_runner.py     Batch login orchestrator
  dashboard.html      Admin dashboard UI
  kiro/               Kiro API client library
  gumloop/            Gumloop client library
app/                  Browser automation (Camoufox + Playwright)
  providers/
    kiro.py           Kiro Google OAuth login
    codebuddy.py      CodeBuddy Google OAuth login
VERSION               Current version
install.sh            Auto-installer
migrate_to_d1.py      SQLite -> D1 migration tool
```

## Version

Current: see `VERSION` file.

Check for updates: `unifiedme status` or `unifiedme update`.
