from __future__ import annotations

from typing import Any

from unified.mcp_client import MCPClient


class SandboxMCPClient:
    def __init__(self, server_url: str, timeout: float = 30.0):
        self._client = MCPClient(server_url, timeout=timeout)

    async def connect(self) -> None:
        await self._client.connect()

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self._client.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._client.call_tool(name, arguments)

    async def close(self) -> None:
        await self._client.disconnect()
