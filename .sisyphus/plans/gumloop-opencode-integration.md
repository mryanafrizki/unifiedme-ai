# Gumloop → OpenCode Agent Integration - Deep Research & Implementation Plan

**Objective**: Make Gumloop ALWAYS use OpenCode agents and tools instead of Gumloop's native agents/tools.

**Date**: 2026-05-16
**Status**: Research Complete → Ready for Implementation

---

## 1. CURRENT ARCHITECTURE ANALYSIS

### 1.1 How Gumloop Currently Works

```
Client (IDE/OpenCode)
  ↓
POST /v1/chat/completions (model: gl-claude-opus-4-7)
  ↓
unified/router_proxy.py → proxy_gumloop.py
  ↓
Gumloop WebSocket (wss://ws.gumloop.com/ws/gummies)
  ↓
Gumloop's NATIVE agent processes request
  ↓
Agent calls Gumloop's NATIVE MCP tools (via MCP server URL configured in Gumloop UI)
  ↓
Results streamed back via WebSocket
  ↓
Proxy converts WS events → OpenAI SSE
  ↓
Client receives streaming response
```

**Key Files**:
- `unified/proxy_gumloop.py` - Main Gumloop proxy logic
- `unified/gumloop/client.py` - WebSocket client for Gumloop API
- `unified/gumloop/auth.py` - Firebase auth per account
- `unified/agent_loop.py` - Local agent loop (LLM + MCP tools)
- `unified/mcp_client.py` - MCP client for tool execution
- `mcp_server.py` - MCP server with 27 tools (file ops, bash, git, etc.)

### 1.2 Current MCP Integration

**Gumloop's Current MCP Usage**:
1. User manually configures MCP server URL in Gumloop UI (Settings → Credentials → Add MCP Server)
2. Gumloop's agent connects to that MCP server
3. Gumloop's agent decides when to call MCP tools
4. Tool execution happens on Gumloop's infrastructure

**Problem**: Gumloop's agent is in control, not OpenCode.

### 1.3 OpenCode Agent Loop (Local)

**File**: `unified/agent_loop.py`

```python
async def run_agent_loop(
    api_key: str,
    model: str,
    messages: list[dict],
    mcp_server_url: str,
) -> AsyncIterator[str]:
    """Run the agent loop, yielding SSE events for the chat UI."""
    
    # 1. Connect to MCP server, fetch tool definitions
    mcp = MCPClient(mcp_server_url)
    await mcp.connect()
    mcp_tools = await mcp.list_tools()
    openai_tools = mcp_tools_to_openai(mcp_tools)
    
    # 2. Inject system prompt that instructs model to use tools
    system_prompt = "You are an AI assistant with access to tools..."
    working_messages = [{"role": "system", "content": system_prompt}] + messages
    
    # 3. Agent loop — call LLM, execute tools, repeat
    while True:
        # Call LLM with tools
        async for chunk in _call_llm_streaming(api_key, model, working_messages, openai_tools):
            # Stream thinking, content, tool_calls
            if "tool_calls" in chunk:
                # Execute tools via MCP
                for tc in tool_calls:
                    result = await mcp.call_tool(tool_name, tool_args)
                    # Add result to messages
                    working_messages.append({"role": "tool", "content": result})
                # Continue loop — LLM sees results and decides next action
            else:
                # No tool calls → final response
                yield final_response
                break
```

**This is what we want for Gumloop!**

---

## 2. TARGET ARCHITECTURE

### 2.1 Desired Flow

```
Client (IDE/OpenCode)
  ↓
POST /v1/chat/completions (model: gl-claude-opus-4-7)
  ↓
unified/router_proxy.py → proxy_gumloop.py
  ↓
NEW: OpenCode Agent Loop (agent_loop.py)
  ├─ Connects to local MCP server (mcp_server.py)
  ├─ Fetches tool definitions
  ├─ Calls Gumloop LLM API (via WebSocket) WITH tools
  ├─ Executes tools locally via MCP
  └─ Loops until completion
  ↓
Gumloop WebSocket (wss://ws.gumloop.com/ws/gummies)
  ↓
Gumloop's LLM (ONLY for inference, NO tool execution)
  ↓
Results streamed back via WebSocket
  ↓
OpenCode agent executes tools locally
  ↓
Proxy converts to OpenAI SSE
  ↓
Client receives streaming response
```

**Key Change**: OpenCode agent orchestrates everything. Gumloop is ONLY the LLM provider.

### 2.2 Implementation Strategy

**Option A: Intercept at Proxy Level (RECOMMENDED)**

Modify `proxy_gumloop.py` to:
1. Detect when request should use OpenCode agent (new flag or model prefix)
2. Instead of calling Gumloop WebSocket directly, call `agent_loop.py`
3. Agent loop uses Gumloop as LLM provider but executes tools locally

**Option B: Modify Gumloop Client**

Modify `gumloop/client.py` to:
1. Parse tool_calls from Gumloop's response
2. Execute tools locally via MCP
3. Send results back to Gumloop

**Verdict**: Option A is cleaner and reuses existing `agent_loop.py` code.

---

## 3. IMPLEMENTATION PLAN

### Phase 1: Research & Validation ✅ COMPLETE

- [x] Read README.md, DEVELOPER.md
- [x] Analyze proxy_gumloop.py architecture
- [x] Analyze agent_loop.py architecture
- [x] Analyze mcp_server.py tools
- [x] Analyze gumloop/client.py WebSocket protocol
- [x] Understand current MCP integration
- [x] Document findings in this plan

### Phase 2: Core Integration

#### Task 2.1: Create Gumloop LLM Provider for Agent Loop

**File**: `unified/llm_provider_gumloop.py` (NEW)

**Purpose**: Adapter that makes Gumloop WebSocket API compatible with agent_loop.py's LLM interface.

**Interface**:
```python
async def call_gumloop_llm_streaming(
    gummie_id: str,
    messages: list[dict],
    auth: GumloopAuth,
    turnstile: TurnstileSolver,
    tools: list[dict] | None = None,
    interaction_id: str | None = None,
    proxy_url: str | None = None,
) -> AsyncIterator[dict]:
    """
    Call Gumloop LLM via WebSocket, yield parsed chunks.
    
    Yields dicts with keys: content, thinking, tool_calls, finish_reason, error.
    Compatible with agent_loop.py's _call_llm_streaming interface.
    """
```

**Implementation**:
- Reuse `gumloop/client.py::send_chat()` for WebSocket communication
- Parse Gumloop events (text-delta, reasoning-delta, tool-call, tool-result)
- Convert to agent_loop.py's expected format
- Handle multi-step tool loops (final=false)

**Acceptance Criteria**:
- [ ] Function yields chunks compatible with agent_loop.py
- [ ] Handles text-delta → content
- [ ] Handles reasoning-delta → thinking
- [ ] Handles tool-call → tool_calls (if Gumloop returns them)
- [ ] Handles errors gracefully
- [ ] Preserves auth token refresh logic

#### Task 2.2: Modify Agent Loop to Support Gumloop Provider

**File**: `unified/agent_loop.py` (MODIFY)

**Changes**:
1. Extract LLM calling logic into pluggable interface
2. Support both local proxy and Gumloop provider
3. Add parameter to specify LLM provider

**New Interface**:
```python
async def run_agent_loop(
    api_key: str,
    model: str,
    messages: list[dict],
    mcp_server_url: str,
    llm_provider: str = "local",  # NEW: "local" or "gumloop"
    gumloop_config: dict | None = None,  # NEW: For Gumloop-specific params
) -> AsyncIterator[str]:
```

**Acceptance Criteria**:
- [ ] Supports llm_provider="local" (existing behavior)
- [ ] Supports llm_provider="gumloop" (new)
- [ ] Gumloop provider uses llm_provider_gumloop.py
- [ ] Tool execution always happens locally via MCP
- [ ] Streaming works for both providers

#### Task 2.3: Integrate into Proxy Gumloop

**File**: `unified/proxy_gumloop.py` (MODIFY)

**Changes**:
1. Add new mode: `use_opencode_agent` (detect via model prefix or flag)
2. When enabled, call `agent_loop.py` instead of direct WebSocket
3. Pass Gumloop auth/config to agent loop

**Detection Logic**:
```python
# Option 1: Model prefix
if model.startswith("gl-oc-"):  # e.g. gl-oc-claude-opus-4-7
    use_opencode_agent = True

# Option 2: Body flag
if body.get("use_opencode_agent", False):
    use_opencode_agent = True

# Option 3: Environment variable (global toggle)
if os.getenv("GUMLOOP_USE_OPENCODE_AGENT", "false").lower() == "true":
    use_opencode_agent = True
```

**Implementation**:
```python
async def proxy_chat_completions(
    body: dict,
    account: dict,
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    
    # Detect mode
    use_opencode_agent = _should_use_opencode_agent(body, account)
    
    if use_opencode_agent:
        # NEW: Use OpenCode agent loop
        return await _proxy_via_opencode_agent(body, account, client_wants_stream, proxy_url)
    else:
        # EXISTING: Direct Gumloop WebSocket
        return await _proxy_via_gumloop_native(body, account, client_wants_stream, proxy_url)
```

**Acceptance Criteria**:
- [ ] Detects when to use OpenCode agent
- [ ] Calls agent_loop.py with Gumloop provider
- [ ] Passes auth, gummie_id, interaction_id correctly
- [ ] Streaming works end-to-end
- [ ] Backward compatible (existing behavior unchanged)

#### Task 2.4: MCP Server Auto-Start

**File**: `unified/proxy_gumloop.py` (MODIFY)

**Purpose**: Ensure MCP server is running before agent loop starts.

**Implementation**:
```python
_mcp_server_process: subprocess.Popen | None = None
_mcp_server_url: str = ""

async def _ensure_mcp_server_running() -> str:
    """Start MCP server if not running, return URL."""
    global _mcp_server_process, _mcp_server_url
    
    if _mcp_server_url:
        # Check if still alive
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{_mcp_server_url}/health")
                if resp.status_code == 200:
                    return _mcp_server_url
        except Exception:
            pass
    
    # Start MCP server
    port = int(os.getenv("MCP_SERVER_PORT", "9876"))
    workspace = os.getenv("MCP_WORKSPACE", os.getcwd())
    api_key = os.getenv("PROXY_API_KEY", "")
    
    _mcp_server_process = subprocess.Popen(
        [sys.executable, "mcp_server.py", 
         "--port", str(port),
         "--workspace", workspace,
         "--api-key", api_key,
         "--no-tunnel",
         "--no-interactive"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    
    _mcp_server_url = f"http://localhost:{port}"
    
    # Wait for server to be ready
    for _ in range(30):
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resp = await client.get(f"{_mcp_server_url}/health")
                if resp.status_code == 200:
                    log.info("MCP server started at %s", _mcp_server_url)
                    return _mcp_server_url
        except Exception:
            await asyncio.sleep(0.5)
    
    raise RuntimeError("MCP server failed to start")
```

**Acceptance Criteria**:
- [ ] MCP server starts automatically on first Gumloop request
- [ ] Reuses existing server if already running
- [ ] Handles server crashes gracefully
- [ ] Logs server status

### Phase 3: Testing & Validation

#### Task 3.1: Unit Tests

**File**: `tests/test_gumloop_opencode_integration.py` (NEW)

**Tests**:
- [ ] Gumloop LLM provider yields correct chunk format
- [ ] Agent loop works with Gumloop provider
- [ ] Tool execution happens locally
- [ ] Streaming works end-to-end
- [ ] Error handling works

#### Task 3.2: Integration Tests

**Manual Testing**:
1. Start unified proxy: `unifiedme run`
2. Start MCP server: `python mcp_server.py`
3. Send request with `gl-oc-claude-opus-4-7` model
4. Verify:
   - [ ] Agent loop starts
   - [ ] MCP tools are loaded
   - [ ] LLM calls go to Gumloop
   - [ ] Tool execution happens locally
   - [ ] Results stream correctly
   - [ ] Final response is correct

#### Task 3.3: OpenCode Integration Test

**Test with OpenCode**:
1. Configure OpenCode to use unified proxy
2. Use Gumloop model with OpenCode agent mode
3. Verify:
   - [ ] OpenCode can call Gumloop via unified proxy
   - [ ] Tools execute on local machine
   - [ ] File operations work
   - [ ] Git operations work
   - [ ] Web search works

### Phase 4: Documentation & Deployment

#### Task 4.1: Update README

**File**: `README.md` (MODIFY)

**Add Section**:
```markdown
## Gumloop with OpenCode Agent

Use Gumloop's LLM with OpenCode's local agent and tools.

### Enable OpenCode Agent Mode

**Option 1: Model Prefix**
```python
model = "gl-oc-claude-opus-4-7"  # Prefix with gl-oc-
```

**Option 2: Request Flag**
```python
body = {
    "model": "gl-claude-opus-4-7",
    "use_opencode_agent": True,
    "messages": [...]
}
```

**Option 3: Environment Variable (Global)**
```bash
export GUMLOOP_USE_OPENCODE_AGENT=true
unifiedme run
```

### How It Works

1. Request goes to unified proxy
2. Proxy detects OpenCode agent mode
3. Agent loop starts with Gumloop as LLM provider
4. MCP server provides tools (file ops, bash, git, etc.)
5. Agent executes tools locally
6. Results stream back to client

### Benefits

- **Local Execution**: All tools run on your machine, not Gumloop's servers
- **Full Control**: OpenCode agent orchestrates everything
- **Privacy**: Files never leave your machine
- **Flexibility**: Use any MCP tools, not just Gumloop's
```

#### Task 4.2: Update DEVELOPER.md

**File**: `DEVELOPER.md` (MODIFY)

**Add Section**:
```markdown
## Gumloop + OpenCode Integration

### Architecture

```
Client → Proxy → Agent Loop → Gumloop LLM (inference only)
                      ↓
                  MCP Server (local tool execution)
```

### Key Files

- `unified/proxy_gumloop.py` - Proxy entry point, mode detection
- `unified/llm_provider_gumloop.py` - Gumloop LLM adapter for agent loop
- `unified/agent_loop.py` - Agent orchestration (LLM + tools)
- `unified/mcp_client.py` - MCP client for tool execution
- `mcp_server.py` - MCP server with 27 tools

### Adding New Tools

1. Add tool to `mcp_server.py`
2. Restart MCP server
3. Agent loop auto-detects new tools
```

---

## 4. TECHNICAL CHALLENGES & SOLUTIONS

### Challenge 1: Gumloop WebSocket Protocol

**Problem**: Gumloop uses custom WebSocket protocol, not standard OpenAI API.

**Solution**: Create adapter (`llm_provider_gumloop.py`) that translates Gumloop events to agent_loop.py's expected format.

### Challenge 2: Tool Call Format

**Problem**: Gumloop may not return tool_calls in OpenAI format.

**Solution**: 
- If Gumloop returns tool_calls: parse and use them
- If not: agent loop decides when to call tools based on LLM response

### Challenge 3: Multi-Step Tool Loops

**Problem**: Gumloop supports multi-step tool execution (final=false).

**Solution**: Agent loop already handles this — it loops until finish_reason="stop".

### Challenge 4: Session Persistence

**Problem**: Gumloop uses interaction_id for session continuity.

**Solution**: Pass interaction_id through agent loop to Gumloop provider.

### Challenge 5: MCP Server Lifecycle

**Problem**: MCP server needs to be running before agent loop starts.

**Solution**: Auto-start MCP server in proxy_gumloop.py if not running.

---

## 5. ROLLOUT STRATEGY

### Phase 1: Opt-In (Week 1)
- Deploy with model prefix detection (`gl-oc-*`)
- Users explicitly opt in
- Monitor for issues

### Phase 2: Flag-Based (Week 2)
- Add `use_opencode_agent` flag
- Allow per-request control
- Gather feedback

### Phase 3: Environment Variable (Week 3)
- Add `GUMLOOP_USE_OPENCODE_AGENT` env var
- Allow global toggle
- Test at scale

### Phase 4: Default (Week 4+)
- Make OpenCode agent the default for Gumloop
- Keep opt-out option for backward compatibility

---

## 6. SUCCESS METRICS

### Functional Metrics
- [ ] Agent loop works with Gumloop LLM
- [ ] All 27 MCP tools work correctly
- [ ] Streaming works end-to-end
- [ ] Session persistence works
- [ ] Error handling works

### Performance Metrics
- [ ] Latency: < 500ms overhead vs direct Gumloop
- [ ] Throughput: Same as direct Gumloop
- [ ] Memory: < 100MB additional per request

### Quality Metrics
- [ ] Tool execution accuracy: 100%
- [ ] Response quality: Same as direct Gumloop
- [ ] Error rate: < 1%

---

## 7. RISKS & MITIGATIONS

### Risk 1: Gumloop API Changes
**Impact**: High
**Mitigation**: Version lock Gumloop client, monitor for breaking changes

### Risk 2: Performance Degradation
**Impact**: Medium
**Mitigation**: Benchmark before/after, optimize hot paths

### Risk 3: Tool Execution Failures
**Impact**: High
**Mitigation**: Comprehensive error handling, fallback to direct Gumloop

### Risk 4: Session State Loss
**Impact**: Medium
**Mitigation**: Persist interaction_id, test session continuity

---

## 8. NEXT STEPS

1. **Immediate**: Start Task 2.1 (Create Gumloop LLM Provider)
2. **Week 1**: Complete Phase 2 (Core Integration)
3. **Week 2**: Complete Phase 3 (Testing & Validation)
4. **Week 3**: Complete Phase 4 (Documentation & Deployment)
5. **Week 4**: Monitor production, gather feedback

---

## 9. OPEN QUESTIONS

1. **Q**: Should we support Gumloop's native MCP tools alongside OpenCode tools?
   **A**: No, OpenCode tools only. Simplifies architecture.

2. **Q**: What if Gumloop returns tool_calls that don't match our MCP tools?
   **A**: Ignore them, agent loop decides tool usage.

3. **Q**: How to handle Gumloop-specific features (e.g., image generation)?
   **A**: Keep them, but download results via MCP's download_file tool.

4. **Q**: Should we cache MCP tool definitions?
   **A**: Yes, fetch once per agent loop session.

5. **Q**: How to handle MCP server crashes?
   **A**: Auto-restart, return error to client if restart fails.

---

## 10. CONCLUSION

**Feasibility**: ✅ HIGH

The integration is feasible because:
1. `agent_loop.py` already exists and works
2. Gumloop WebSocket protocol is well-understood
3. MCP server is stable and feature-complete
4. Architecture is clean and modular

**Effort Estimate**: 3-4 days
- Day 1: Task 2.1 (Gumloop LLM Provider)
- Day 2: Task 2.2 + 2.3 (Agent Loop + Proxy Integration)
- Day 3: Task 2.4 + Phase 3 (MCP Auto-Start + Testing)
- Day 4: Phase 4 (Documentation + Deployment)

**Recommendation**: Proceed with implementation. Start with Task 2.1.

---

**Research Complete**: 2026-05-16 17:38 UTC
**Next Action**: Begin Task 2.1 - Create Gumloop LLM Provider
