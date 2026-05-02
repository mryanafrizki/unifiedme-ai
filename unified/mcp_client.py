"""MCP Streamable HTTP client — connects to FastMCP server via JSON-RPC over HTTP.

Handles:
- Initialize handshake + session ID management
- tools/list → fetch available tools
- tools/call → execute a tool with arguments
- SSE response parsing for streamed results
- Connection lifecycle (connect, disconnect, reconnect)
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger("unified.mcp_client")


class MCPClient:
    """Stateful MCP client for Streamable HTTP transport."""

    def __init__(self, server_url: str, timeout: float = 60.0):
        """
        Args:
            server_url: MCP server endpoint, e.g. "http://localhost:9876/mcp"
                        or "https://xxx.trycloudflare.com/mcp".
                        If URL doesn't end with /mcp, it's appended.
            timeout: HTTP request timeout in seconds.
        """
        url = server_url.rstrip("/")
        if not url.endswith("/mcp"):
            url += "/mcp"
        self.server_url = url
        self.timeout = timeout
        self.session_id: str = ""
        self._client: httpx.AsyncClient | None = None
        self._msg_id: int = 0
        self._initialized: bool = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Initialize MCP connection: handshake + get session ID."""
        if self._initialized:
            return

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=self.timeout, write=30, pool=10),
            follow_redirects=True,
        )

        # Step 1: initialize
        init_payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "unified-agent", "version": "1.0"},
            },
        }

        resp = await self._client.post(
            self.server_url,
            json=init_payload,
            headers=self._base_headers(),
        )
        resp.raise_for_status()

        # Extract session ID from response header
        self.session_id = resp.headers.get("mcp-session-id", "")
        if not self.session_id:
            log.warning("MCP server did not return mcp-session-id header")

        # Step 2: notifications/initialized
        notify_payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
        await self._client.post(
            self.server_url,
            json=notify_payload,
            headers=self._session_headers(),
        )

        self._initialized = True
        log.info("MCP client connected to %s (session=%s)", self.server_url, self.session_id[:16] if self.session_id else "none")

    async def disconnect(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._initialized = False
        self.session_id = ""
        self._msg_id = 0

    async def ensure_connected(self) -> None:
        """Connect if not already connected."""
        if not self._initialized:
            await self.connect()

    # ── Tools ────────────────────────────────────────────────────────────

    async def list_tools(self) -> list[dict[str, Any]]:
        """Fetch available tools from MCP server.

        Returns list of tool definitions:
        [{"name": "bash", "description": "...", "inputSchema": {...}}, ...]
        """
        await self.ensure_connected()
        assert self._client is not None

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
            "params": {},
        }

        resp = await self._client.post(
            self.server_url,
            json=payload,
            headers=self._session_headers(),
        )
        resp.raise_for_status()

        result = self._parse_response(resp)
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Execute a tool on the MCP server.

        Args:
            name: Tool name (e.g. "bash", "read_file")
            arguments: Tool arguments dict

        Returns:
            Tool result (parsed from JSON-RPC response content).
        """
        await self.ensure_connected()
        assert self._client is not None

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        }

        resp = await self._client.post(
            self.server_url,
            json=payload,
            headers=self._session_headers(),
        )
        resp.raise_for_status()

        result = self._parse_response(resp)

        # MCP returns content as array: [{"type": "text", "text": "..."}]
        content_items = result.get("content", [])
        texts = []
        for item in content_items:
            if item.get("type") == "text":
                # Try to parse as JSON (most tools return JSON text)
                raw = item.get("text", "")
                try:
                    parsed = json.loads(raw)
                    texts.append(parsed)
                except (json.JSONDecodeError, ValueError):
                    texts.append(raw)
            elif item.get("type") == "image":
                texts.append(f"[Image: {item.get('mimeType', 'image/png')}]")

        if len(texts) == 1:
            return texts[0]
        return texts if texts else result

    # ── Helpers ──────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _base_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    def _session_headers(self) -> dict[str, str]:
        h = self._base_headers()
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def _parse_response(self, resp: httpx.Response) -> dict[str, Any]:
        """Parse MCP server response — handles both direct JSON and SSE format."""
        text = resp.text.strip()

        # Direct JSON response
        if text.startswith("{"):
            data = json.loads(text)
            if "error" in data:
                err = data["error"]
                raise MCPError(err.get("message", "Unknown MCP error"), err.get("code", -1))
            return data.get("result", data)

        # SSE format: data: {...}\n\n
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                json_str = line[5:].strip()
                if not json_str:
                    continue
                try:
                    data = json.loads(json_str)
                    if "error" in data:
                        err = data["error"]
                        raise MCPError(err.get("message", "Unknown MCP error"), err.get("code", -1))
                    if "result" in data:
                        return data["result"]
                except json.JSONDecodeError:
                    continue

        raise MCPError(f"Could not parse MCP response: {text[:200]}")


class MCPError(Exception):
    """Error from MCP server."""

    def __init__(self, message: str, code: int = -1):
        self.code = code
        super().__init__(message)


def _clean_schema(schema: dict) -> dict:
    """Deep-clean a JSON Schema for OpenAI/CodeBuddy compatibility.

    Removes fields that cause parse errors on some providers:
    $schema, title, additionalProperties, $defs, allOf wrappers, etc.
    """
    if not isinstance(schema, dict):
        return schema

    cleaned: dict = {}
    # Fields to strip at every level
    _STRIP_KEYS = {"$schema", "title", "additionalProperties", "$defs", "definitions", "default"}

    for k, v in schema.items():
        if k in _STRIP_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            cleaned[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            cleaned[k] = _clean_schema(v)
        elif k == "allOf" and isinstance(v, list) and len(v) == 1:
            # Unwrap single-item allOf (common in Pydantic/FastMCP schemas)
            cleaned.update(_clean_schema(v[0]))
        elif k == "anyOf" and isinstance(v, list):
            cleaned[k] = [_clean_schema(item) for item in v]
        else:
            cleaned[k] = v

    return cleaned


def mcp_tools_to_openai(mcp_tools: list[dict]) -> list[dict]:
    """Convert MCP tool definitions to OpenAI function calling format.

    MCP format:
        {"name": "bash", "description": "...", "inputSchema": {"type": "object", "properties": {...}, "required": [...]}}

    OpenAI format:
        {"type": "function", "function": {"name": "bash", "description": "...", "parameters": {"type": "object", "properties": {...}, "required": [...]}}}
    """
    openai_tools = []
    for tool in mcp_tools:
        schema = tool.get("inputSchema", {})
        # Deep-clean schema for provider compatibility
        cleaned = _clean_schema(schema)
        params = {
            "type": cleaned.get("type", "object"),
            "properties": cleaned.get("properties", {}),
        }
        if "required" in cleaned:
            params["required"] = cleaned["required"]

        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": params,
            },
        })
    return openai_tools
