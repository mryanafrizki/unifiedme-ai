"""Agent loop — orchestrates LLM + MCP tool execution.

Flow:
1. Connect to MCP server, fetch tool definitions
2. Convert MCP tools → OpenAI function calling format
3. Send messages + tools to LLM via local proxy (/v1/chat/completions)
4. Parse streaming response for tool_calls
5. If tool_calls: execute via MCP, append results, go to step 3
6. If no tool_calls: stream final text response
7. Yield SSE events throughout for real-time UI updates

SSE output format (embedded in content text, compatible with existing chat.html):
- Thinking blocks: streamed via delta.thinking
- Tool calls: "[Tool] tool_name({args})"
- Tool results: "[Result] tool_name → {result}"
- Final text: streamed via delta.content
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from .config import LISTEN_PORT
from .mcp_client import MCPClient, MCPError, mcp_tools_to_openai

log = logging.getLogger("unified.agent_loop")


def _sse_chunk(content: str = "", thinking: str = "", finish_reason: str | None = None, model: str = "") -> str:
    """Build an OpenAI-compatible SSE chunk."""
    delta: dict[str, Any] = {}
    if content:
        delta["content"] = content
    if thinking:
        delta["thinking"] = thinking

    chunk: dict[str, Any] = {
        "id": "agent-loop",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _format_tool_call_text(name: str, arguments: dict) -> str:
    """Format tool call as text for UI rendering: [Tool] name({args})"""
    # Compact args display — show key=value pairs
    args_parts = []
    for k, v in arguments.items():
        val = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        # Truncate long values
        if len(val) > 120:
            val = val[:117] + "..."
        args_parts.append(f"{k}={val}")
    args_str = ", ".join(args_parts)
    return f"\n\n[Tool] **`{name}`**({args_str})\n"


def _format_tool_result_text(name: str, result: Any) -> str:
    """Format tool result as text for UI rendering: [Result] name → content"""
    if isinstance(result, dict):
        # Check for error
        if "error" in result:
            result_str = f"Error: {result['error']}"
        elif "tree" in result:
            # Tree output — render as-is (already has newlines)
            result_str = result["tree"]
        elif "text" in result and isinstance(result["text"], str):
            # Text content (e.g. read_file)
            result_str = result["text"]
            if result.get("total_lines"):
                result_str += f"\n({result.get('showing', '')})"
        elif "stdout" in result:
            # Shell command output
            result_str = result.get("stdout", "")
            if result.get("stderr"):
                result_str += f"\nSTDERR: {result['stderr']}"
            if result.get("exit_code", 0) != 0:
                result_str += f"\n(exit code: {result['exit_code']})"
        elif "entries" in result and isinstance(result["entries"], list):
            # Directory listing
            result_str = "\n".join(result["entries"])
            if result.get("path"):
                result_str = f"[{result['path']}]\n{result_str}"
        elif "matches" in result and isinstance(result["matches"], list):
            # Search results (grep, glob)
            items = result["matches"]
            if items and isinstance(items[0], dict):
                # grep results with file/line/text
                lines = []
                for m in items[:50]:
                    if "file" in m and "line" in m:
                        lines.append(f"{m['file']}:{m['line']}: {m.get('text', '')}")
                    else:
                        lines.append(str(m))
                result_str = "\n".join(lines)
            elif items and isinstance(items[0], str):
                # glob results
                result_str = "\n".join(items)
            else:
                result_str = json.dumps(items, ensure_ascii=False, indent=2)
            if result.get("count"):
                result_str += f"\n({result['count']} matches)"
        elif "ok" in result:
            # Success result (write_file, edit_file, etc.)
            parts = [f"OK"]
            for k in ("path", "bytes", "replacements", "deleted", "from", "to"):
                if k in result:
                    parts.append(f"{k}: {result[k]}")
            result_str = " | ".join(parts)
        elif "documentation" in result:
            # search_docs result
            result_str = result.get("documentation", "")[:2000]
            if result.get("library"):
                result_str = f"Library: {result['library']}\n\n{result_str}"
        elif "results" in result and isinstance(result["results"], list):
            # web_search / search_github_code results
            lines = []
            for r in result["results"][:10]:
                if "title" in r:
                    lines.append(f"• {r['title']}\n  {r.get('url', '')}\n  {r.get('snippet', '')}")
                elif "repo" in r:
                    lines.append(f"• {r['repo']} — {r.get('file', '')}\n  {r.get('lines', '')}")
                else:
                    lines.append(str(r))
            result_str = "\n".join(lines)
        elif "content" in result and isinstance(result["content"], str):
            # fetch_url result
            result_str = result["content"][:2000]
        elif "diff" in result:
            result_str = result["diff"]
        else:
            result_str = json.dumps(result, ensure_ascii=False, indent=2)
    elif isinstance(result, list):
        result_str = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        result_str = str(result)

    # Truncate very long results
    if len(result_str) > 3000:
        result_str = result_str[:2997] + "..."

    return f"\n[Result] **`{name}`** →\n```\n{result_str}\n```\n\n"


async def _do_llm_request(
    proxy_url: str,
    req_headers: dict,
    payload: dict,
) -> AsyncIterator[dict]:
    """Single LLM streaming request. Yields parsed chunks or error."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=15, read=300, write=30, pool=10)) as client:
        async with client.stream("POST", proxy_url, json=payload, headers=req_headers) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                error_text = body.decode("utf-8", errors="replace")
                yield {"error": f"LLM API error HTTP {resp.status_code}: {error_text[:500]}", "_status": resp.status_code}
                return

            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    continue

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                if "error" in chunk:
                    err = chunk["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    yield {"error": msg}
                    return

                choices = chunk.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                result: dict[str, Any] = {}

                if delta.get("content"):
                    result["content"] = delta["content"]
                if delta.get("thinking"):
                    result["thinking"] = delta["thinking"]
                if delta.get("tool_calls"):
                    result["tool_calls"] = delta["tool_calls"]
                if choice.get("finish_reason"):
                    result["finish_reason"] = choice["finish_reason"]

                if result:
                    yield result


async def _call_llm_streaming(
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """Call local /v1/chat/completions with streaming, yield parsed SSE chunks.

    Yields dicts with keys: content, thinking, tool_calls, finish_reason, error.
    If the provider returns 400 with tools, retries without tools param.
    """
    proxy_url = f"http://127.0.0.1:{LISTEN_PORT}/v1/chat/completions"
    req_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    # First attempt — with tools
    got_400 = False
    async for item in _do_llm_request(proxy_url, req_headers, payload):
        if item.get("_status") == 400 and tools:
            # Provider rejected tools — will retry without
            log.warning("LLM rejected tools param (HTTP 400), retrying without tools")
            got_400 = True
            break
        item.pop("_status", None)
        yield item

    if not got_400:
        return

    # Retry without tools
    payload_no_tools: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    async for item in _do_llm_request(proxy_url, req_headers, payload_no_tools):
        item.pop("_status", None)
        yield item


async def run_agent_loop(
    api_key: str,
    model: str,
    messages: list[dict],
    mcp_server_url: str,
) -> AsyncIterator[str]:
    """Run the agent loop, yielding SSE events for the chat UI.

    Args:
        api_key: Unified proxy API key
        model: Model name (e.g. "claude-sonnet-4", "claude-opus-4.6")
        messages: Conversation messages in OpenAI format
        mcp_server_url: MCP server URL (e.g. "http://localhost:9876")

    Yields:
        SSE-formatted strings: "data: {...}\n\n"
    """
    mcp = MCPClient(mcp_server_url)

    try:
        # Step 1: Connect to MCP and fetch tools
        try:
            await mcp.connect()
        except Exception as e:
            yield _sse_chunk(content=f"\n\n**MCP Error:** Could not connect to {mcp_server_url}: {e}\n", model=model)
            yield _sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        try:
            mcp_tools = await mcp.list_tools()
        except Exception as e:
            yield _sse_chunk(content=f"\n\n**MCP Error:** Could not list tools: {e}\n", model=model)
            yield _sse_chunk(finish_reason="stop", model=model)
            yield "data: [DONE]\n\n"
            return

        openai_tools = mcp_tools_to_openai(mcp_tools)
        tool_count = len(openai_tools)
        log.info("Agent loop: %d MCP tools loaded from %s", tool_count, mcp_server_url)

        # Yield initial status
        yield _sse_chunk(content=f"_Connected to MCP server ({tool_count} tools available)_\n\n", model=model)

        # Step 2: Agent loop — call LLM, execute tools, repeat
        # Inject system prompt that instructs the model to use tools
        tool_names = ", ".join(t["function"]["name"] for t in openai_tools)
        system_prompt = (
            "You are an AI assistant with access to tools. "
            "When the user asks you to do something, USE the tools to accomplish it. "
            "Do NOT just describe what you would do — actually call the tools. "
            f"Available tools: {tool_names}"
        )
        working_messages = [{"role": "system", "content": system_prompt}] + list(messages)
        iteration = 0

        while True:
            iteration += 1
            log.info("Agent loop iteration %d (messages: %d)", iteration, len(working_messages))

            # Accumulate full response for this iteration
            full_content = ""
            full_thinking = ""
            accumulated_tool_calls: dict[int, dict] = {}  # index → {id, function: {name, arguments}}

            async for chunk in _call_llm_streaming(api_key, model, working_messages, openai_tools):
                # Handle errors
                if "error" in chunk:
                    yield _sse_chunk(content=f"\n\n**Error:** {chunk['error']}\n", model=model)
                    yield _sse_chunk(finish_reason="stop", model=model)
                    yield "data: [DONE]\n\n"
                    return

                # Stream thinking
                if "thinking" in chunk:
                    full_thinking += chunk["thinking"]
                    yield _sse_chunk(thinking=chunk["thinking"], model=model)

                # Stream content (only if no tool_calls are being accumulated)
                if "content" in chunk and not accumulated_tool_calls:
                    full_content += chunk["content"]
                    yield _sse_chunk(content=chunk["content"], model=model)
                elif "content" in chunk:
                    full_content += chunk["content"]

                # Accumulate tool_calls from delta
                if "tool_calls" in chunk:
                    for tc in chunk["tool_calls"]:
                        idx = tc.get("index", 0)
                        if idx not in accumulated_tool_calls:
                            accumulated_tool_calls[idx] = {
                                "id": tc.get("id", f"call_{idx}"),
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        entry = accumulated_tool_calls[idx]
                        func = tc.get("function", {})
                        if func.get("name"):
                            entry["function"]["name"] = func["name"]
                        if func.get("arguments"):
                            entry["function"]["arguments"] += func["arguments"]

            # No tool calls → final response, we're done
            if not accumulated_tool_calls:
                # If we had content that wasn't streamed yet (shouldn't happen, but safety)
                yield _sse_chunk(finish_reason="stop", model=model)
                yield "data: [DONE]\n\n"
                return

            # Tool calls found → execute them
            # First, add assistant message with tool_calls to working messages
            tool_calls_list = list(accumulated_tool_calls.values())
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": full_content or None}
            assistant_msg["tool_calls"] = tool_calls_list
            working_messages.append(assistant_msg)

            # Execute each tool call
            for tc in tool_calls_list:
                func = tc["function"]
                tool_name = func["name"]
                try:
                    tool_args = json.loads(func["arguments"]) if func["arguments"] else {}
                except json.JSONDecodeError:
                    tool_args = {"raw": func["arguments"]}

                # Stream tool call to UI
                yield _sse_chunk(content=_format_tool_call_text(tool_name, tool_args), model=model)

                # Execute via MCP
                try:
                    result = await mcp.call_tool(tool_name, tool_args)
                except MCPError as e:
                    result = {"error": str(e)}
                except Exception as e:
                    result = {"error": f"Tool execution failed: {e}"}

                # Stream result to UI
                yield _sse_chunk(content=_format_tool_result_text(tool_name, result), model=model)

                # Add tool result to working messages
                result_str = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_str,
                })

            # Continue loop — LLM will see tool results and decide next action

    except Exception as e:
        log.error("Agent loop error: %s", e, exc_info=True)
        yield _sse_chunk(content=f"\n\n**Agent Error:** {e}\n", model=model)
        yield _sse_chunk(finish_reason="stop", model=model)
        yield "data: [DONE]\n\n"
    finally:
        await mcp.disconnect()
