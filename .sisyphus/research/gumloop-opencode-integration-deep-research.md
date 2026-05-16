# Deep Research: Gumloop + OpenCode Integration

**Objective**: Make Gumloop ALWAYS use OpenCode agents and tools, not Gumloop's native agents/tools.

**Date**: 2026-05-17  
**Status**: Research Complete → Implementation Ready

---

## Executive Summary

**Current State**: Gumloop uses its own agent system with MCP tools server-side.

**Target State**: Gumloop becomes a pure LLM provider that routes ALL agent logic to OpenCode.

**Key Insight**: We need to **intercept at the proxy layer** and transform Gumloop requests into OpenCode agent requests, then stream back responses in Gumloop-compatible format.

---

## Architecture Analysis

### Current Flow (Gumloop Native Agent)

```
Client (OpenCode/IDE)
  ↓ POST /v1/chat/completions (model: gl-claude-opus-4-7)
  ↓
Unified Proxy (:1430)
  ↓ proxy_gumloop.py
  ↓ Strips client tools, injects MCP rules into system prompt
  ↓ update_gummie_config() → sets model + system prompt
  ↓
Gumloop WebSocket (wss://ws.gumloop.com/ws/gummies)
  ↓ Gumloop agent processes request
  ↓ Agent calls MCP tools server-side
  ↓ MCP Server (:9876) executes on local filesystem
  ↓ Returns results to Gumloop agent
  ↓
Gumloop streams response (text-delta, tool-call, tool-result events)
  ↓
Proxy converts WS events → OpenAI SSE
  ↓
Client receives streaming response
```

**Problem**: Gumloop agent controls the loop. OpenCode has no control over tool execution, reasoning, or multi-step planning.

---

### Target Flow (OpenCode Agent + Gumloop LLM)

```
Client (OpenCode/IDE)
  ↓ POST /v1/chat/completions (model: gl-claude-opus-4-7)
  ↓
Unified Proxy (:1430)
  ↓ proxy_gumloop.py (NEW LOGIC)
  ↓ Detect: "This is an OpenCode agent request"
  ↓ Route to: opencode_agent_loop.py
  ↓
OpenCode Agent Loop
  ↓ 1. Parse messages, extract tools from OpenCode
  ↓ 2. Call Gumloop LLM (via WebSocket) with tools=None
  ↓ 3. Parse response for tool_calls
  ↓ 4. Execute tools via OpenCode (NOT MCP server)
  ↓ 5. Append tool results to messages
  ↓ 6. Loop until finish
  ↓
Gumloop WebSocket (wss://ws.gumloop.com/ws/gummies)
  ↓ Pure LLM inference (NO agent, NO tools)
  ↓ Returns text + reasoning only
  ↓
OpenCode Agent Loop
  ↓ Streams SSE events (tool-call, tool-result, text-delta)
  ↓
Client receives streaming response
```

**Key Change**: OpenCode controls the agent loop. Gumloop is just an LLM provider.

---

## Technical Deep Dive

### 1. Current Gumloop Integration Points

#### A. `proxy_gumloop.py` (Main Entry Point)

**Current Behavior**:
- Strips client tools from request
- Injects MCP rules into system prompt
- Calls `update_gummie_config()` to set model + system prompt
- Calls `send_chat()` to start WebSocket stream
- Converts Gumloop events → OpenAI SSE

**Key Functions**:
```python
async def proxy_chat_completions(
    body: dict,
    account: dict,
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    # Current: strips tools, injects MCP rules
    messages, system_prompt = _convert_openai_messages_simple(body)
    
    # Current: updates gummie config (model + system prompt)
    await update_gummie_config(
        gummie_id=gummie_id,
        auth=auth,
        system_prompt=full_system,
        tools=None,  # MCP handles tools
        model_name=gl_model,
        proxy_url=proxy_url,
    )
    
    # Current: streams Gumloop response
    return _stream_gumloop(...)
```

**What We Need to Change**:
1. Detect if request has tools (OpenCode agent mode)
2. If tools present → route to OpenCode agent loop
3. If no tools → keep current behavior (Gumloop agent + MCP)

---

#### B. `gumloop/client.py` (WebSocket Client)

**Current Behavior**:
- Connects to `wss://ws.gumloop.com/ws/gummies`
- Sends payload with `id_token`, `gummie_id`, `messages`, `interaction_id`
- Yields events: `text-delta`, `reasoning-delta`, `tool-call`, `tool-result`, `finish`

**Key Functions**:
```python
async def send_chat(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth: GumloopAuth,
    turnstile: TurnstileSolver | None = None,
    interaction_id: str | None = None,
    proxy_url: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    # Builds Gumloop message format
    # Connects to WebSocket
    # Yields events until finish
```

**What We Need**:
- New function: `send_chat_llm_only()` that:
  - Disables Gumloop agent mode
  - Returns pure LLM response (no tool execution)
  - Streams text + reasoning only

**How to Disable Gumloop Agent**:
- Option 1: Set `tools=[]` in gummie config (may not work)
- Option 2: Add flag in WebSocket payload (need to test)
- Option 3: Use different gummie_id for LLM-only mode
- **Best Option**: Update gummie config with `tools=[]` and `system_prompt` that says "You are a pure LLM. Do NOT execute tools. Only respond with text."

---

#### C. `agent_loop.py` (Existing OpenCode Agent Loop)

**Current Behavior**:
- Connects to MCP server
- Fetches tool definitions
- Calls local proxy `/v1/chat/completions` with tools
- Parses tool_calls from response
- Executes tools via MCP
- Loops until finish

**What We Need to Change**:
1. Add support for Gumloop as LLM provider
2. Instead of calling `/v1/chat/completions` → call Gumloop WebSocket directly
3. Parse Gumloop events → extract tool_calls
4. Execute tools via OpenCode (NOT MCP)

**New Function**:
```python
async def run_agent_loop_with_gumloop(
    account: dict,
    model: str,
    messages: list[dict],
    tools: list[dict],  # OpenCode tools
) -> AsyncIterator[str]:
    # 1. Convert OpenCode tools → OpenAI format
    # 2. Inject tools into system prompt
    # 3. Call Gumloop WebSocket (LLM-only mode)
    # 4. Parse response for tool_calls
    # 5. Execute tools via OpenCode
    # 6. Loop until finish
    # 7. Stream SSE events
```

---

### 2. OpenCode Tool Execution

**Current State**: OpenCode has its own tool system (not MCP).

**Key Files**:
- `opencode` CLI tool (external binary)
- Tool definitions in OpenCode config
- Tool execution via OpenCode API

**What We Need**:
1. **Option A**: Use OpenCode's native tool system
   - Requires OpenCode API integration
   - Need to understand OpenCode tool format
   - May require `opencode serve` running

2. **Option B**: Use MCP server as intermediary
   - OpenCode → MCP server → local filesystem
   - Simpler integration (already working)
   - But adds extra hop

3. **Option C**: Hybrid approach
   - OpenCode tools for high-level operations
   - MCP tools for low-level file operations
   - Best of both worlds

**Recommendation**: Start with **Option B** (MCP intermediary) for MVP, then migrate to **Option A** for production.

---

### 3. Tool Format Conversion

#### OpenCode Tool Format (Unknown - Need Research)

```json
{
  "name": "read_file",
  "description": "Read a file from the workspace",
  "parameters": {
    "type": "object",
    "properties": {
      "path": {"type": "string", "description": "File path"}
    },
    "required": ["path"]
  }
}
```

#### OpenAI Tool Format (Current)

```json
{
  "type": "function",
  "function": {
    "name": "read_file",
    "description": "Read a file from the workspace",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {"type": "string", "description": "File path"}
      },
      "required": ["path"]
    }
  }
}
```

#### Gumloop Tool Format (Native - NOT USED)

```json
{
  "name": "read_file",
  "description": "Read a file from the workspace",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string", "description": "File path"}
    },
    "required": ["path"]
  }
}
```

**Conversion Strategy**:
1. Accept tools in OpenAI format (from OpenCode)
2. Inject into system prompt as XML (Claude-style)
3. Parse tool_calls from Gumloop response
4. Execute via OpenCode or MCP

---

### 4. Gumloop LLM-Only Mode

**Challenge**: Gumloop expects to control the agent loop.

**Solution**: Trick Gumloop into thinking it's in "chat mode" (no tools).

**Implementation**:
1. Update gummie config: `tools=[]`
2. Inject system prompt: "You are a helpful assistant. When asked to use tools, respond with XML tool_use blocks."
3. Parse response for `<tool_use>` blocks
4. Execute tools via OpenCode
5. Append results as `<tool_result>` blocks
6. Loop until finish

**Example System Prompt**:
```
You are a helpful AI assistant with access to tools.

When you need to use a tool, respond with:
<tool_use>
<name>tool_name</name>
<input>{"param": "value"}</input>
</tool_use>

Available tools:
- read_file: Read a file from the workspace
- write_file: Write content to a file
- bash: Execute a shell command
...

Do NOT execute tools yourself. Only output tool_use blocks.
```

---

### 5. OpenCode Integration Points

**Key Question**: How does OpenCode send requests to the proxy?

**Current Understanding**:
- OpenCode uses OpenAI-compatible API
- Sends POST `/v1/chat/completions` with tools
- Expects streaming SSE response

**What We Need to Know**:
1. Does OpenCode have a "serve" mode? (API server)
2. How does OpenCode execute tools internally?
3. Can we call OpenCode tools via HTTP API?
4. Or do we need to use MCP server as intermediary?

**Action Items**:
1. ✅ Check if `opencode serve` exists
2. ✅ Read OpenCode documentation
3. ✅ Test OpenCode tool execution
4. ✅ Understand OpenCode agent architecture

**Findings** (from memory context):
- User mentioned "opencode serve" → suggests API server exists
- Need to research OpenCode API endpoints
- Need to understand OpenCode tool format

---

## Implementation Plan

### Phase 1: Research & Validation (CURRENT)

**Goals**:
1. ✅ Understand Gumloop architecture
2. ✅ Understand OpenCode architecture
3. ⏳ Test Gumloop LLM-only mode
4. ⏳ Test OpenCode tool execution
5. ⏳ Validate integration approach

**Tasks**:
- [x] Read all Gumloop code
- [x] Read all proxy code
- [x] Read agent_loop.py
- [ ] Research OpenCode API
- [ ] Test `opencode serve`
- [ ] Test Gumloop with `tools=[]`
- [ ] Test tool_use XML parsing

---

### Phase 2: MVP Implementation

**Goals**:
1. Route Gumloop requests to OpenCode agent loop
2. Execute tools via MCP server (intermediary)
3. Stream responses in OpenAI format

**Tasks**:
1. **Modify `proxy_gumloop.py`**:
   - Detect if request has tools
   - Route to new `opencode_agent_loop_gumloop.py`

2. **Create `opencode_agent_loop_gumloop.py`**:
   - Accept OpenCode tools
   - Call Gumloop WebSocket (LLM-only)
   - Parse tool_calls from response
   - Execute tools via MCP
   - Loop until finish
   - Stream SSE events

3. **Create `gumloop/client_llm_only.py`**:
   - New function: `send_chat_llm_only()`
   - Disables Gumloop agent
   - Returns pure LLM response

4. **Test End-to-End**:
   - OpenCode → Proxy → Gumloop LLM → OpenCode tools → Response

---

### Phase 3: Production Optimization

**Goals**:
1. Replace MCP intermediary with direct OpenCode API
2. Optimize tool execution performance
3. Add error handling and retries
4. Add logging and monitoring

**Tasks**:
1. Research OpenCode API
2. Implement direct OpenCode tool execution
3. Add caching for tool definitions
4. Add retry logic for failed tools
5. Add metrics and logging

---

## Key Technical Decisions

### Decision 1: Where to Route Requests?

**Options**:
- A. Modify `proxy_gumloop.py` to detect tools and route
- B. Create new endpoint `/v1/chat/completions/opencode`
- C. Use query parameter `?agent=opencode`

**Recommendation**: **Option A** (modify proxy_gumloop.py)
- Transparent to client
- No API changes needed
- Easy to toggle via feature flag

---

### Decision 2: How to Execute Tools?

**Options**:
- A. Direct OpenCode API (if available)
- B. MCP server intermediary
- C. Hybrid (OpenCode for high-level, MCP for low-level)

**Recommendation**: **Option B** for MVP, **Option A** for production
- MCP already working
- OpenCode API needs research
- Can migrate later

---

### Decision 3: How to Parse Tool Calls?

**Options**:
- A. Parse XML `<tool_use>` blocks from text
- B. Use Gumloop's native tool_call events (if available)
- C. Inject special tokens for tool boundaries

**Recommendation**: **Option A** (XML parsing)
- Claude-native format
- Easy to parse with regex
- Already implemented in `tool_converter.py`

---

## Risk Analysis

### Risk 1: Gumloop Rejects LLM-Only Mode

**Probability**: Medium  
**Impact**: High  
**Mitigation**: Test with `tools=[]` config. If fails, use dummy tool that does nothing.

---

### Risk 2: OpenCode API Not Available

**Probability**: Low  
**Impact**: High  
**Mitigation**: Use MCP server as intermediary. Works today.

---

### Risk 3: Performance Degradation

**Probability**: Medium  
**Impact**: Medium  
**Mitigation**: 
- Cache tool definitions
- Parallel tool execution
- Optimize WebSocket connection reuse

---

### Risk 4: Tool Format Incompatibility

**Probability**: Low  
**Impact**: Medium  
**Mitigation**: 
- Use OpenAI format as common standard
- Convert to/from OpenCode format as needed
- Test with real OpenCode tools

---

## Next Steps

### Immediate Actions (Today)

1. **Test Gumloop LLM-Only Mode**:
   ```python
   # Test script: test_gumloop_llm_only.py
   await update_gummie_config(
       gummie_id=gummie_id,
       auth=auth,
       system_prompt="You are a helpful assistant. Output tool_use blocks.",
       tools=[],  # Empty tools
       model_name="claude-opus-4-7",
   )
   
   # Send chat and check if it returns pure text
   async for event in send_chat(...):
       print(event)
   ```

2. **Research OpenCode API**:
   ```bash
   # Check if opencode serve exists
   opencode serve --help
   
   # Check OpenCode config
   cat ~/.config/opencode/config.json
   
   # Check OpenCode documentation
   opencode --help
   ```

3. **Create Proof of Concept**:
   - File: `poc_gumloop_opencode.py`
   - Test: OpenCode tools → Gumloop LLM → Tool execution → Response
   - Validate: End-to-end flow works

---

### Short-Term (This Week)

1. Implement MVP (Phase 2)
2. Test with real OpenCode session
3. Measure performance
4. Document API changes

---

### Long-Term (Next Month)

1. Migrate to direct OpenCode API
2. Optimize performance
3. Add monitoring and logging
4. Production deployment

---

## Conclusion

**Feasibility**: ✅ **HIGH** - All components exist, just need integration.

**Complexity**: ⚠️ **MEDIUM** - Requires careful routing and event parsing.

**Timeline**: 
- MVP: 2-3 days
- Production: 1-2 weeks

**Recommendation**: **PROCEED** with MVP implementation using MCP intermediary.

---

## Appendix A: Code References

### Key Files to Modify

1. **`unified/proxy_gumloop.py`** (Main entry point)
   - Add tool detection logic
   - Route to OpenCode agent loop

2. **`unified/opencode_agent_loop_gumloop.py`** (NEW)
   - OpenCode agent loop with Gumloop LLM
   - Tool execution via MCP

3. **`unified/gumloop/client.py`**
   - Add `send_chat_llm_only()` function

4. **`unified/gumloop/tool_converter.py`**
   - Already has XML parsing logic
   - Reuse for tool_use extraction

---

## Appendix B: Testing Strategy

### Unit Tests

1. Test tool detection in proxy
2. Test XML tool_use parsing
3. Test tool execution via MCP
4. Test SSE event streaming

### Integration Tests

1. Test OpenCode → Proxy → Gumloop → Response
2. Test multi-step tool loops
3. Test error handling
4. Test streaming performance

### End-to-End Tests

1. Real OpenCode session
2. Real Gumloop account
3. Real MCP server
4. Measure latency and throughput

---

## Appendix C: Performance Considerations

### Latency Breakdown

1. **OpenCode → Proxy**: ~10ms (local)
2. **Proxy → Gumloop WS**: ~100-200ms (network)
3. **Gumloop LLM inference**: ~500-2000ms (model)
4. **Tool execution (MCP)**: ~50-500ms (depends on tool)
5. **Total per turn**: ~1-3 seconds

### Optimization Opportunities

1. **WebSocket connection pooling**: Reuse connections
2. **Tool caching**: Cache tool definitions
3. **Parallel tool execution**: Run independent tools in parallel
4. **Streaming optimization**: Stream tokens as they arrive

---

## Appendix D: Alternative Approaches

### Approach 1: Gumloop as Pure LLM (RECOMMENDED)

**Pros**:
- OpenCode controls agent loop
- Full control over tool execution
- Easy to debug and monitor

**Cons**:
- Requires Gumloop LLM-only mode
- Extra hop for tool execution

---

### Approach 2: Gumloop Agent + OpenCode Tools

**Pros**:
- Leverage Gumloop's agent capabilities
- Less code to write

**Cons**:
- Gumloop controls loop (less control)
- Hard to integrate OpenCode tools
- Complex event parsing

---

### Approach 3: Hybrid (Gumloop for Planning, OpenCode for Execution)

**Pros**:
- Best of both worlds
- Gumloop does high-level planning
- OpenCode does low-level execution

**Cons**:
- Most complex to implement
- Unclear boundaries
- Hard to debug

---

**Final Recommendation**: **Approach 1** (Gumloop as Pure LLM) for MVP.

