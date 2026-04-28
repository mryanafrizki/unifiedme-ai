# Gumloop Integration — Handoff & Recap

## Status: MCP Proven, Proxy Rewritten, Perlu Integration ke OpenCode

---

## Apa yang Udah Dilakukan

### 1. Reverse Engineering Gumloop WebSocket API
- Gumloop pakai `wss://ws.gumloop.com/ws/gummies` untuk chat
- Events: `text-delta`, `reasoning-delta`, `tool-call`, `tool-input-start`, `tool-input-delta`, `tool-result`, `finish`
- Auth: Firebase token (refresh via `securetoken.googleapis.com`)
- Captcha: Cloudflare Turnstile (solve via 2Captcha, prefetch strategy)
- Model config: `PATCH /gummies/{id}` (model_name, system_prompt, tools)

### 2. Tool Calling Discovery
- **Custom tools via PATCH `/gummies/{id}`**: TIDAK WORK. Agent ignore custom tools, hanya pakai built-in (sandbox_python, sandbox_file, dll)
- **MCP tools**: WORK NATIVE. Agent call MCP tools via WS events (`tool-call` → `tool-result`), server-side execution
- **XML tool emulation**: WORK di Sonnet, TIDAK RELIABLE di Opus (model bypass XML instructions, pakai sandbox tools)

### 3. MCP Server (Localhost)
- File: `_test_mcp_local_server.py`
- 8 tools: `read_file`, `write_file`, `edit_file`, `bash`, `list_directory`, `glob`, `grep`, `download_image`
- Transport: HTTP JSON-RPC (MCP Streamable HTTP)
- Tunnel: Cloudflare Tunnel (`cloudflared tunnel --url http://localhost:9876`)
- Workspace: scoped ke folder tertentu, path sandboxed

### 4. MCP Test Results (ALL PASSED)
- `list_directory` → list files ✅
- `read_file` → baca file dengan line numbers ✅
- `write_file` → create/overwrite file di localhost ✅
- `edit_file` → search-and-replace ✅
- `bash` → execute shell command ✅
- `glob` → find files by pattern ✅
- `grep` → search content by regex ✅
- `download_image` → download dari Gumloop storage (gl:// URL) via `/download_file` API ✅

### 5. Complex Task Tests (MCP)
- **HTML generation**: Agent create 12KB HTML file di localhost ✅
- **HTML edit with web reference**: Agent fetch rdp.cobain.dev, baca existing file, rewrite tanpa emoji ✅
- **CRUD app**: Agent create 4 files (index.html, style.css, app.js, README.md) di localhost ✅
- **Waktu Indonesia**: Agent pakai bash → python buat convert UTC ke WIB/WITA/WIT ✅
- **Image generation + save**: Agent generate image → download 941KB ke localhost ✅
- **Prayer time reminder**: Agent create 32KB single-file web app (realtime clock + 5 waktu sholat) ✅

### 6. XML Tool Executor Test
- File: `_test_xml_executor/xml_executor.py`
- Flow: Agent output `<tool_use>` XML → proxy parse → execute locally → send result back
- Result Sonnet: 5/5 tools executed, multi-loop works ✅
- Result Opus: Agent bypass XML, pakai sandbox tools ❌
- Conclusion: XML approach TIDAK RELIABLE untuk production

### 7. Proxy Rewrite
- File: `unified/proxy_gumloop.py` (rewritten)
- Stripped client tools (OpenCode tools ignored)
- Stream text-delta + reasoning-delta as OpenAI SSE chunks
- Multi-step tool loops: continue on `finish(final=false)`, break on `final=true`
- Tool events logged but not emitted as text (agent summarizes)
- Removed XML parsing dependency (`tool_converter.py` not imported)

---

## Architecture

```
OpenCode → POST /v1/chat/completions (model: gl-claude-opus-4-7)
  ↓
Unified Proxy (localhost:1430)
  ↓ strip tools, convert messages
Gumloop WebSocket (wss://ws.gumloop.com/ws/gummies)
  ↓ agent processes message
  ↓ agent calls MCP tools natively
  ↓
MCP Server (localhost:9876 → cloudflared tunnel → HTTPS)
  ↓ execute on localhost filesystem
  ↓ return results to agent
  ↓
Agent continues, streams response
  ↓
Proxy converts WS events → OpenAI SSE
  ↓
OpenCode receives streaming response
```

---

## Key Files

| File | Role |
|------|------|
| `unified/proxy_gumloop.py` | Proxy — rewritten for MCP mode |
| `unified/gumloop/client.py` | WS client, send_chat, GumloopStreamHandler |
| `unified/gumloop/auth.py` | Firebase auth per-account |
| `unified/gumloop/turnstile.py` | Captcha solver (2Captcha, prefetch) |
| `unified/gumloop/parser.py` | OpenAI SSE chunk builders |
| `unified/gumloop/tool_converter.py` | XML tool conversion (legacy, not used in MCP mode) |
| `gumloop_login.py` | Browser automation login (Camoufox + Google OAuth) |
| `_test_mcp_local_server.py` | MCP server (8 tools, localhost) |
| `_test_mcp_worker/` | MCP server on CF Workers (test, deployed) |
| `_test_xml_executor/xml_executor.py` | XML tool executor (standalone test) |

---

## Gumloop Account Setup

Setiap akun Gumloop perlu:
1. **Batch login** → extract Firebase tokens + create gummie
2. **Manual MCP setup** di Gumloop dashboard:
   - Settings → Credentials → Add MCP Server → URL tunnel
   - Agent settings → Add tools → select MCP Server
3. **Cloudflared tunnel** harus jalan (`cloudflared tunnel --url http://localhost:9876`)

Akun yang udah di-setup MCP:
- `GisellePaige@ggmel.com` (gummie: `BNmkQdGydjtwQyJjWqrvng`) — MCP connected ✅

---

## OpenCode Config

File: `C:\Users\User\AppData\Roaming\opencode\opencode.json`

Model `gl-claude-opus-4-7` dan `gl-claude-sonnet-4-6` udah ditambahin di provider `enowxlabs` yang point ke `localhost:1430`.

---

## Known Issues

1. **MCP per-account setup manual** — setiap akun baru harus connect MCP via Gumloop web UI. Belum ada automation.
2. **Cloudflared tunnel URL berubah** setiap restart — harus update di Gumloop dashboard. Solusi: pakai named tunnel (perlu Cloudflare account).
3. **Captcha solve kadang timeout** — 2Captcha bisa lambat (>2 menit). Prefetch strategy helps tapi gak 100%.
4. **Opus bypass XML tools** — kalau pakai XML approach, Opus prefer sandbox tools. MCP approach gak kena issue ini.
5. **Proxy gak return tool_calls ke OpenCode** — OpenCode cuma dapet text response. Gak ada tool execution indicators di UI.

---

## Next Steps

### Priority 1: Auto MCP Setup per Account
- Extend `gumloop_login.py` — setelah login, navigate ke Settings → add MCP credential → attach ke agent
- Atau: explore `add_server_awaiter` built-in tool (buat known servers only, gak buat custom URL)

### Priority 2: Named Cloudflare Tunnel
- Bikin permanent tunnel URL (gak berubah setiap restart)
- `cloudflared tunnel create unifiedme-mcp`
- Atau host MCP server di VPS dengan domain tetap

### Priority 3: Test di OpenCode
- Start MCP server + tunnel
- Connect MCP ke akun yang dipake
- Buka OpenCode, pilih model `gl-claude-opus-4-7`
- Test: simple chat → complex task → codebase reading
- Verify streaming works untuk long tasks

### Priority 4: Improve Proxy Response
- Option A (current): text-only response, agent summarizes tool results
- Option B (future): convert WS tool events ke OpenAI tool_calls format — OpenCode bisa display tool indicators

### Priority 5: MCP Server Production
- Host di VPS dengan domain tetap (gak perlu cloudflared)
- Add more tools: LSP operations, git commands, project-specific tools
- Workspace management: multiple projects, switch workspace

---

## Test Commands (Quick Reference)

```bash
# Start MCP server
python _test_mcp_local_server.py

# Start tunnel
cloudflared tunnel --url http://localhost:9876

# Test MCP tools locally
python -c "import httpx; print(httpx.post('http://127.0.0.1:9876', json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}).json())"

# Test via Gumloop (simple)
python _test_mcp_quick.py

# Test 4 tasks via MCP
python _test_mcp_4tasks.py

# Test XML executor (standalone)
python _test_xml_executor/xml_executor.py

# Test image download
python _test_gl_image_save.py
```

---

## Conclusion

**MCP approach = proven reliable.** Agent call tools natively, execute di localhost, semua model support (sonnet + opus). Bottleneck sekarang: manual MCP setup per-account dan tunnel URL management. Proxy udah rewritten, OpenCode config udah ready. Tinggal test end-to-end di OpenCode dan automate MCP setup.
