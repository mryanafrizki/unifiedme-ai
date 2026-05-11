# Analysis: Why gl-* Models Don't Behave Like Normal OpenCode/OMO Backends

## Date: 2026-05-12

---

## 1. Where are gl-* models mapped and routed?

### config.py (lines 427-428)
```python
def get_tier(model: str) -> Tier | None:
    if model.startswith("gl-"):
        return Tier.MAX_GL
```
Any model prefixed `gl-` gets routed to `Tier.MAX_GL`.

### router_proxy.py (line 330)
```python
if tier == Tier.MAX_GL:
    # Route to Gumloop -- WebSocket chat with instant account rotation on failure
    response, cost = await gumloop_proxy(body, account, client_wants_stream, proxy_url=proxy_url)
```

### proxy_gumloop.py (lines 99-106) -- model name mapping
```python
def _map_gl_model(model: str) -> str:
    bare = model.removeprefix("gl-")
    if any(x in bare for x in ("claude", "haiku", "sonnet", "opus")):
        bare = bare.replace(".", "-")
    if bare in GUMLOOP_MODELS:
        return bare
    return bare
```

**Summary**: `gl-claude-opus-4-6` -> `Tier.MAX_GL` -> `gumloop_proxy()` -> maps to `claude-opus-4-6` internal name.

---

## 2. Where does the request stop behaving like a normal OpenAI-style model backend?

The divergence happens in **5 layers**, each compounding:

### DIVERGENCE 1: Protocol Change (HTTP -> WebSocket)
- **Normal backends** (Kiro, CodeBuddy, WaveSpeed): HTTP POST to REST API, return SSE stream.
- **Gumloop**: Opens a **WebSocket** to `wss://ws.gumloop.com/ws/gummies`, sends a proprietary JSON payload with `type: "start"`, and receives a proprietary event stream.

**File**: `unified/gumloop/client.py:297`
```python
async with websockets.connect(WS_URL, **ws_kwargs) as ws:
    await ws.send(payload_str)
    async for message in ws:
        event = json.loads(message)
        yield event
```

### DIVERGENCE 2: Authentication Model (API key -> Firebase + Turnstile Captcha)
- **Normal backends**: Account has an access token or API key. One HTTP header.
- **Gumloop**: Requires Firebase OAuth token refresh (`GumloopAuth`) + Cloudflare Turnstile captcha solving via 2Captcha paid service (`TurnstileSolver`).

**Files**: `unified/gumloop/auth.py`, `unified/gumloop/turnstile.py`

### DIVERGENCE 3: Entity Model (Direct API -> Gummie + Interaction)
- **Normal backends**: Send a request, get a response. Stateless.
- **Gumloop**: Requires a pre-created "gummie" (agent entity) with `gummie_id`. Each chat is an "interaction" with persistent session state. The proxy must `update_gummie_config()` before each request to set model + system prompt.

**File**: `unified/proxy_gumloop.py:399-408`
```python
await update_gummie_config(
    gummie_id=gummie_id,
    auth=auth,
    system_prompt=full_system,
    tools=None,  # Don't touch tools -- MCP handles them
    model_name=gl_model,
)
```

### DIVERGENCE 4: Tool Ownership Transfer (Client -> Server-Side Agent)
**THIS IS THE CORE DIVERGENCE.**

- **Normal backends** (Kiro): Passthrough. Client sends `tools` in the request, backend returns `tool_calls` in SSE, client executes tools, client sends `tool` results back in next turn. The LLM is a **dumb pipe** -- OpenCode/OMO remains the orchestrator.
  ```python
  # Kiro: tools passed straight through
  tools=body_dict.get("tools"),  # line 111
  ```

- **Gumloop**: ALL tools are **stripped from the client request**. Gumloop's server-side agent loop handles tool execution autonomously via MCP. The client never sees `tool_calls` in the SSE stream -- instead, tool activity is flattened into **markdown text** in the content delta.

**File**: `unified/proxy_gumloop.py:159-207` (`_convert_openai_messages_simple`)
```python
"""Strips tools entirely -- Gumloop uses MCP tools server-side.
Converts tool role messages and tool_calls to plain text."""

# Tool role (tool results from client) -> convert to user message
if role == "tool":
    tool_text = f"[Tool result for {tool_call_id}]: {content}"
    result.append({"role": "user", "content": tool_text})

# Assistant with tool_calls -> convert to plain text
if role == "assistant" and tool_calls:
    parts.append(f"[Called tool: {name}({args})]")
    result.append({"role": "assistant", "content": "\n".join(parts)})
```

### DIVERGENCE 5: Stream Event Translation (tool_calls -> markdown text)
- **Normal backends**: Return proper OpenAI SSE with `delta.tool_calls` array.
- **Gumloop**: Tool events are converted to **inline markdown text** in the content stream.

**File**: `unified/proxy_gumloop.py:539-573`
```python
# Tool call -> inline markdown
elif etype == "tool-call":
    tool_text = f"\n\n> **[Tool]** `{tool_name}({input_preview})`\n"
    chunk = emit_text(tool_text)

# Tool result -> inline markdown
elif etype == "tool-result":
    result_block = f"\n> **[Result]** `{tool_name}` ->\n> ```\n> {preview}\n> ```\n\n"
    chunk = emit_text(result_block)
```

This means OpenCode/OMO can NEVER see `tool_calls` in the response. It gets text that says `> **[Tool]** read_file(...)` -- which is useless for an orchestrator that expects to execute tools itself.

---

## 3. Where are tools stripped, rewritten, or replaced?

| What happens | File | Function | Line |
|---|---|---|---|
| Client tools stripped from body | `proxy_gumloop.py` | `_convert_openai_messages_simple()` | 159-241 |
| System prompt injected with MCP rules | `proxy_gumloop.py` | `proxy_chat_completions()` | 368-396 |
| Gummie config updated with NO tools | `proxy_gumloop.py` | `proxy_chat_completions()` | 399-408 |
| `tool_calls` from messages flattened to text | `proxy_gumloop.py` | `_convert_openai_messages_simple()` | 196-207 |
| `tool` role messages converted to user text | `proxy_gumloop.py` | `_convert_openai_messages_simple()` | 187-193 |
| WS tool-call events -> markdown text | `proxy_gumloop.py` | `_stream_gumloop()` | 539-573 |
| Tool definitions -> XML system prompt | `gumloop/tool_converter.py` | `tools_to_system_prompt()` | 10-37 |
| Tool loop detection | `gumloop/tool_converter.py` | `detect_tool_loop()` | 286-323 |
| WS finish with `final=false` -> continue | `proxy_gumloop.py` | `_stream_gumloop()` | 623-625 |

### The MCP system prompt injection (lines 368-396):
A **hardcoded** system prompt is prepended that tells the Gumloop agent to use MCP tools (read_file, write_file, bash, etc.) -- tools that live on the user's MCP server, NOT on the client side. This prompt explicitly says:
```
"MANDATORY RULES (never violate):
1. For ALL file operations: ONLY use MCP tools. NEVER use sandbox_python...
2. Sandbox tools run on a remote server, NOT the user's machine..."
```

---

## 4. Which files/functions would need to change for OpenCode/OMO to remain orchestrator?

### Critical changes needed:

**A. `unified/proxy_gumloop.py` -- The core proxy**
1. `_convert_openai_messages_simple()` -- REPLACE with passthrough that preserves tools, tool_calls, tool role
2. `proxy_chat_completions()` lines 295-296 -- STOP stripping tools
3. `proxy_chat_completions()` lines 368-396 -- REMOVE MCP system prompt injection
4. `proxy_chat_completions()` lines 399-408 -- PASS tools to gummie config instead of `tools=None`
5. `_stream_gumloop()` lines 539-573 -- RETURN proper `delta.tool_calls` SSE instead of markdown text

**B. `unified/gumloop/client.py` -- WebSocket protocol**
6. `_send_chat_inner()` -- The fundamental problem: Gumloop's WS protocol doesn't support OpenAI-style tool passthrough. The protocol is `{type: "start", payload: {context: {gummie_id, message, chat}}}`. There is NO `tools` field in the WS payload.

**C. `unified/gumloop/parser.py` -- SSE builders**
7. `build_openai_tool_call_chunk()` -- Already exists but is NEVER CALLED by the streaming code. Would need to be wired in.

**D. `unified/router_proxy.py` -- Router (line 330)**
8. Could add an alternative code path for "passthrough mode" vs "agent mode" based on whether the client sent tools.

---

## 5. Minimum-change path to make gl-* feel like a standard OpenCode model backend

### The fundamental constraint:
Gumloop is NOT an API -- it's an **agent platform**. The WebSocket protocol (`wss://ws.gumloop.com/ws/gummies`) was designed for:
- Server-side agent loops with MCP tools
- Persistent chat sessions (interaction_id)
- Gummie configuration (system prompt, model, MCP servers)
- Captcha-gated WebSocket auth

**There is no "passthrough" mode in Gumloop's protocol.** The LLM runs server-side, tools execute server-side, and the client only sees text output + tool activity events.

### Two realistic paths:

#### Path A: "Fake Passthrough" (Medium effort, Lossy)
- Keep Gumloop as the backend but translate its proprietary events back to OpenAI tool_calls format
- When `_stream_gumloop()` receives `tool-call` events, emit proper `delta.tool_calls` SSE chunks instead of markdown
- When `_stream_gumloop()` receives `tool-result`, emit tool result as a new assistant turn
- Problem: **OpenCode can't actually execute the tools** because Gumloop already executed them server-side. This creates a phantom-orchestrator pattern where OpenCode thinks it's in control but isn't.

#### Path B: "Bypass Gumloop Agent Loop" (Hard, but correct)
- Instead of using `wss://ws.gumloop.com/ws/gummies` (which triggers Gumloop's full agent pipeline), find or create a **direct LLM API endpoint** that returns raw model output
- This would require Gumloop to expose a raw `/v1/chat/completions` REST API (which it currently doesn't for free-tier users)
- Without such an API, this path is blocked.

#### Path C: "Hybrid Mode" (Minimum viable, Recommended)
1. **When client sends `tools` in the request**: Detect this and switch to "passthrough mode":
   - Don't strip tools from messages
   - Don't inject MCP system prompt
   - Pass tools to gummie config
   - Translate `tool-call` WS events to proper `delta.tool_calls` SSE
   - On `finish` with `final=false`, send `finish_reason: "tool_calls"` so OpenCode can execute
   - PROBLEM: Gumloop will STILL execute tools server-side. You'd need to configure the gummie with NO MCP servers to prevent this.

2. **When client sends NO tools**: Keep current behavior (MCP agent mode)

### Exact changes for Path C:

```
proxy_gumloop.py:
  proxy_chat_completions() -- add `has_client_tools = bool(body.get("tools"))`
  - If has_client_tools:
    - Skip MCP system prompt injection
    - Pass client tools to update_gummie_config (or skip config update)
    - Use _stream_gumloop_passthrough() instead of _stream_gumloop()
  
  NEW: _stream_gumloop_passthrough()
    - On "tool-call": emit build_openai_tool_call_chunk()
    - On "finish" with final=false: emit finish_reason="tool_calls"
    - On "text-delta": emit build_openai_chunk() as normal

  NEW: _convert_openai_messages_passthrough()
    - Keep tools, tool_calls, tool role messages intact
    - Convert to Gumloop WS format with tool context preserved

gumloop/client.py:
  - Unclear if Gumloop WS protocol even supports receiving tool_results
  - Would need investigation into whether the WS accepts tool execution results

router_proxy.py:
  - No changes needed (already routes to gumloop_proxy correctly)
```

### Blocking question:
**Does Gumloop's WebSocket protocol support receiving client-side tool results?**
If not, Path C is impossible and the only option is to wait for Gumloop to expose a raw LLM API endpoint, or to use a different provider for models that OpenCode needs to orchestrate.
