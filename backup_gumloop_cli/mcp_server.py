#!/usr/bin/env python3
"""
MCP Server — Streamable HTTP transport (JSON-RPC 2.0).

Tools: read_file, write_file, edit_file, bash, list_directory, glob, grep, download_image
Workspace: scoped to WORKSPACE_DIR (default: ../_tmp_mcp_workspace)

Usage:
    python _tmp_mcp_server/mcp_server.py
    python _tmp_mcp_server/mcp_server.py --port 9876 --workspace /path/to/workspace
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MCP] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp")

# ─── Config ──────────────────────────────────────────────────────────────────

DEFAULT_PORT = 9876
DEFAULT_WORKSPACE = Path(__file__).resolve().parent.parent / "_tmp_mcp_workspace"

SERVER_NAME = "unified-mcp-server"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-11-25"


# ─── Path Safety ─────────────────────────────────────────────────────────────

def safe_path(workspace: Path, relative: str) -> Path:
    """Resolve a path and ensure it stays within workspace."""
    # Allow absolute paths that are within workspace
    p = Path(relative)
    if p.is_absolute():
        resolved = p.resolve()
    else:
        resolved = (workspace / relative).resolve()

    ws_resolved = workspace.resolve()
    if not str(resolved).startswith(str(ws_resolved)):
        raise ValueError(f"Path escapes workspace: {relative}")
    return resolved


# ─── Tool Implementations ────────────────────────────────────────────────────

async def tool_read_file(workspace: Path, args: dict) -> dict:
    """Read a file with optional offset/limit (line numbers)."""
    path = safe_path(workspace, args.get("path", ""))
    if not path.exists():
        return {"error": f"File not found: {path}"}
    if path.is_dir():
        return {"error": f"Path is a directory: {path}"}

    offset = max(1, args.get("offset", 1))  # 1-indexed
    limit = args.get("limit", 2000)

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": f"Read error: {e}"}

    lines = text.splitlines()
    total = len(lines)
    selected = lines[offset - 1 : offset - 1 + limit]
    numbered = [f"{offset + i}: {line}" for i, line in enumerate(selected)]

    return {
        "text": "\n".join(numbered),
        "total_lines": total,
        "showing": f"lines {offset}-{offset + len(selected) - 1} of {total}",
    }


async def tool_write_file(workspace: Path, args: dict) -> dict:
    """Write/overwrite a file."""
    path = safe_path(workspace, args.get("path", ""))
    content = args.get("content", "")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(path), "bytes": len(content.encode("utf-8"))}
    except Exception as e:
        return {"error": f"Write error: {e}"}


async def tool_edit_file(workspace: Path, args: dict) -> dict:
    """Search-and-replace in a file."""
    path = safe_path(workspace, args.get("path", ""))
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    replace_all = args.get("replace_all", False)

    if not path.exists():
        return {"error": f"File not found: {path}"}
    if not old_string:
        return {"error": "old_string is required"}

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"error": f"Read error: {e}"}

    count = text.count(old_string)
    if count == 0:
        return {"error": f"old_string not found in {path.name}"}
    if count > 1 and not replace_all:
        return {"error": f"Found {count} matches. Set replace_all=true or provide more context."}

    if replace_all:
        new_text = text.replace(old_string, new_string)
    else:
        new_text = text.replace(old_string, new_string, 1)

    path.write_text(new_text, encoding="utf-8")
    return {"ok": True, "replacements": count if replace_all else 1, "path": str(path)}


async def tool_bash(workspace: Path, args: dict) -> dict:
    """Execute a shell command in the workspace directory."""
    command = args.get("command", "")
    timeout = min(args.get("timeout", 30), 120)

    if not command:
        return {"error": "command is required"}

    try:
        # Use cmd.exe on Windows, bash on Linux
        if os.name == "nt":
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
            )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        result: dict[str, Any] = {"exit_code": proc.returncode}
        if stdout_str:
            result["stdout"] = stdout_str[:50000]
        if stderr_str:
            result["stderr"] = stderr_str[:10000]
        return result

    except asyncio.TimeoutError:
        return {"error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"error": f"Exec error: {e}"}


async def tool_list_directory(workspace: Path, args: dict) -> dict:
    """List files and directories."""
    rel = args.get("path", ".")
    path = safe_path(workspace, rel)

    if not path.exists():
        return {"error": f"Path not found: {path}"}
    if not path.is_dir():
        return {"error": f"Not a directory: {path}"}

    entries = []
    try:
        for item in sorted(path.iterdir()):
            name = item.name + ("/" if item.is_dir() else "")
            entries.append(name)
    except Exception as e:
        return {"error": f"List error: {e}"}

    return {"path": str(path), "entries": entries, "count": len(entries)}


async def tool_glob(workspace: Path, args: dict) -> dict:
    """Find files by glob pattern."""
    pattern = args.get("pattern", "*")

    matches = []
    try:
        for p in workspace.rglob(pattern):
            rel = p.relative_to(workspace)
            name = str(rel).replace("\\", "/")
            if p.is_dir():
                name += "/"
            matches.append(name)
    except Exception as e:
        return {"error": f"Glob error: {e}"}

    # Limit results
    matches = sorted(matches)[:200]
    return {"pattern": pattern, "matches": matches, "count": len(matches)}


async def tool_grep(workspace: Path, args: dict) -> dict:
    """Search file contents by regex pattern."""
    pattern = args.get("pattern", "")
    include = args.get("include", "*")
    max_results = min(args.get("max_results", 100), 500)

    if not pattern:
        return {"error": "pattern is required"}

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return {"error": f"Invalid regex: {e}"}

    results = []
    files_searched = 0

    for p in workspace.rglob(include):
        if not p.is_file():
            continue
        files_searched += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = str(p.relative_to(workspace)).replace("\\", "/")
                    results.append({"file": rel, "line": i, "text": line.strip()[:500]})
                    if len(results) >= max_results:
                        break
        except Exception:
            continue
        if len(results) >= max_results:
            break

    return {
        "pattern": pattern,
        "matches": results,
        "count": len(results),
        "files_searched": files_searched,
    }


def _parse_gl_url(gl_url: str) -> tuple[str, str]:
    """Parse gl:// URL into (file_path_for_api, user_id).

    gl:// format: gl://uid-{user_id}/{file_path}
    API expects: POST /download_file with {"file_name": "{file_path}", "user_id": "{user_id}"}
    The uid-{user_id}/ prefix must be stripped from the path.
    """
    gl_path = gl_url[5:]  # strip "gl://"

    # Extract user_id from uid-{user_id}/... prefix
    user_id = ""
    file_path = gl_path
    if gl_path.startswith("uid-"):
        parts = gl_path.split("/", 1)
        if len(parts) == 2:
            user_id = parts[0][4:]  # strip "uid-"
            file_path = parts[1]

    return file_path, user_id


async def _get_gumloop_auth() -> dict[str, str]:
    """Load and refresh Gumloop credentials for gl:// downloads."""
    creds_file = Path(__file__).resolve().parent.parent / "_tmp_auto_mcp" / "result.json"
    if not creds_file.exists():
        return {}

    import json as _json
    creds = _json.loads(creds_file.read_text())
    refresh_token = creds.get("refresh_token", "")
    if not refresh_token:
        return {}

    # Refresh Firebase token
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}",
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            ) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                return {
                    "id_token": data.get("id_token", ""),
                    "user_id": data.get("user_id", creds.get("user_id", "")),
                }
    except Exception as e:
        log.warning("[download] Auth refresh failed: %s", e)
        return {}


FIREBASE_API_KEY = "AIzaSyCYuXqbJ0YBNltoGS4-7Y6Hozrra8KKmaE"


PROXY_BASE_URL = os.environ.get("PROXY_BASE_URL", "http://127.0.0.1:1430")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "sk-fb131ee9f8fbb07e9f4c5216a49d2c67244e386b34ffb0da")


async def tool_download_image(workspace: Path, args: dict) -> dict:
    """Download an image from URL and save to workspace."""
    url = args.get("url", "")
    filename = args.get("filename", "")

    if not url:
        return {"error": "url is required"}

    import aiohttp

    if not filename:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        filename = Path(parsed.path).name or "download.png"

    save_path = safe_path(workspace, filename)

    try:
        async with aiohttp.ClientSession() as session:
            if url.startswith("gl://"):
                # Gumloop storage — download via proxy endpoint (has correct account auth)
                log.info("[download] gl:// → proxy /v1/gl-download (%s)", url[:80])
                headers = {
                    "Authorization": f"Bearer {PROXY_API_KEY}",
                    "Content-Type": "application/json",
                }
                async with session.post(
                    f"{PROXY_BASE_URL}/v1/gl-download",
                    json={"gl_url": url},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        return {"error": f"Gumloop download failed: HTTP {resp.status} — {error_text[:200]}"}
                    data = await resp.read()
            else:
                # Regular HTTP URL
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        return {"error": f"Download failed: HTTP {resp.status}"}
                    data = await resp.read()

            if len(data) == 0:
                return {"error": "Downloaded file is empty"}

            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(data)
            log.info("[download] Saved %d bytes → %s", len(data), save_path.name)
            return {
                "ok": True,
                "path": str(save_path),
                "bytes": len(data),
                "filename": filename,
            }

    except Exception as e:
        return {"error": f"Download error: {e}"}


# ─── Tool Registry ───────────────────────────────────────────────────────────

TOOLS = {
    "read_file": {
        "handler": tool_read_file,
        "description": "Read a file from the workspace. Returns content with line numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to workspace or absolute within workspace)"},
                "offset": {"type": "integer", "description": "Start line (1-indexed, default: 1)", "default": 1},
                "limit": {"type": "integer", "description": "Max lines to return (default: 2000)", "default": 2000},
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "handler": tool_write_file,
        "description": "Write content to a file. Creates parent directories if needed. Overwrites existing files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
    },
    "edit_file": {
        "handler": tool_edit_file,
        "description": "Search and replace text in a file. Fails if old_string not found or has multiple matches (unless replace_all=true).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to edit"},
                "old_string": {"type": "string", "description": "Text to find"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {"type": "boolean", "description": "Replace all occurrences (default: false)", "default": False},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    "bash": {
        "handler": tool_bash,
        "description": "Execute a shell command in the workspace directory. Returns stdout, stderr, and exit code.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30, max: 120)", "default": 30},
            },
            "required": ["command"],
        },
    },
    "list_directory": {
        "handler": tool_list_directory,
        "description": "List files and directories. Directories have trailing /.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: workspace root)", "default": "."},
            },
        },
    },
    "glob": {
        "handler": tool_glob,
        "description": "Find files matching a glob pattern recursively in the workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. '*.py', '**/*.html')"},
            },
            "required": ["pattern"],
        },
    },
    "grep": {
        "handler": tool_grep,
        "description": "Search file contents using regex pattern. Returns matching lines with file paths and line numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "include": {"type": "string", "description": "File glob filter (default: '*')", "default": "*"},
                "max_results": {"type": "integer", "description": "Max results (default: 100)", "default": 100},
            },
            "required": ["pattern"],
        },
    },
    "download_image": {
        "handler": tool_download_image,
        "description": "Download a file from URL and save to workspace. IMPORTANT: For Gumloop-generated images, always pass the gl:// storage_link URL directly (e.g. gl://uid-xxx/custom_agent_interactions/.../image.png). Do NOT convert to gumloop.com/files/ URLs. Also supports regular http/https URLs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to download. Use the gl:// storage_link for Gumloop images, or any http/https URL."},
                "filename": {"type": "string", "description": "Save filename (auto-detected if empty)"},
            },
            "required": ["url"],
        },
    },
}


# ─── JSON-RPC Handler ────────────────────────────────────────────────────────

async def handle_jsonrpc(workspace: Path, body: dict) -> dict:
    """Handle a single JSON-RPC 2.0 request."""
    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    def result(data: Any) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": data}

    def error(code: int, msg: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}

    # ── Initialize ──
    if method == "initialize":
        return result({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    # ── Ping ──
    if method == "ping":
        return result({})

    # ── Notifications (no response needed) ──
    if method == "notifications/initialized":
        return result({})

    # ── List tools ──
    if method == "tools/list":
        tools_list = []
        for name, spec in TOOLS.items():
            tools_list.append({
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            })
        return result({"tools": tools_list})

    # ── Call tool ──
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in TOOLS:
            return error(-32601, f"Unknown tool: {tool_name}")

        log.info("tools/call: %s(%s)", tool_name, json.dumps(arguments, ensure_ascii=False)[:200])

        try:
            handler = TOOLS[tool_name]["handler"]
            tool_result = await handler(workspace, arguments)

            # Format as MCP content
            text = json.dumps(tool_result, ensure_ascii=False, indent=2)
            return result({
                "content": [{"type": "text", "text": text}],
                "isError": "error" in tool_result,
            })
        except Exception as e:
            log.error("Tool %s error: %s", tool_name, e, exc_info=True)
            return result({
                "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                "isError": True,
            })

    return error(-32601, f"Method not found: {method}")


# ─── HTTP Server ─────────────────────────────────────────────────────────────

async def handle_post(request: web.Request) -> web.Response:
    """Handle POST requests (JSON-RPC)."""
    workspace = request.app["workspace"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status=400,
        )

    # Handle batch requests
    if isinstance(body, list):
        responses = []
        for item in body:
            resp = await handle_jsonrpc(workspace, item)
            if resp.get("id") is not None:  # Skip notifications
                responses.append(resp)
        return web.json_response(responses)

    response = await handle_jsonrpc(workspace, body)
    return web.json_response(response)


async def handle_get(request: web.Request) -> web.Response:
    """Handle GET requests (health check / SSE placeholder)."""
    accept = request.headers.get("Accept", "")
    if "text/event-stream" in accept:
        # SSE — not needed for our use case, return empty
        return web.Response(
            status=200,
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    return web.json_response({
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "protocol": PROTOCOL_VERSION,
        "status": "ok",
    })


async def handle_options(request: web.Request) -> web.Response:
    """Handle CORS preflight."""
    return web.Response(
        status=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, MCP-Protocol-Version, MCP-Session-Id",
            "Access-Control-Max-Age": "86400",
        },
    )


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Add CORS headers to all responses."""
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, MCP-Protocol-Version, MCP-Session-Id"
    return response


def create_app(workspace: Path) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["workspace"] = workspace

    # MCP endpoint — handle at root and /mcp
    app.router.add_post("/", handle_post)
    app.router.add_post("/mcp", handle_post)
    app.router.add_get("/", handle_get)
    app.router.add_get("/mcp", handle_get)
    app.router.add_route("OPTIONS", "/", handle_options)
    app.router.add_route("OPTIONS", "/mcp", handle_options)

    return app


def main():
    parser = argparse.ArgumentParser(description="MCP Server (Streamable HTTP)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Listen port (default: {DEFAULT_PORT})")
    parser.add_argument("--workspace", type=str, default=str(DEFAULT_WORKSPACE), help="Workspace directory")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 50)
    print(f"  MCP Server v{SERVER_VERSION}")
    print(f"  Port:      {args.port}")
    print(f"  Workspace: {workspace}")
    print(f"  Tools:     {', '.join(TOOLS.keys())}")
    print("=" * 50)
    print()
    print(f"  Endpoint:  http://localhost:{args.port}/")
    print()
    print("  Now run cloudflared:")
    print(f"    cloudflared tunnel --url http://localhost:{args.port}")
    print()

    app = create_app(workspace)
    web.run_app(app, host="0.0.0.0", port=args.port, print=None)


if __name__ == "__main__":
    main()
