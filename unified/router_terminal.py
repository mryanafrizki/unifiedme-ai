"""FastAPI WebSocket router for interactive SSH terminal sessions.

Bridges browser xterm.js ↔ WebSocket ↔ SSH PTY on remote VPS.

Protocol:
  - Text messages: raw keystrokes forwarded to SSH stdin
  - JSON messages: control commands
    - {"type": "resize", "cols": 120, "rows": 40}  → resize PTY
    - {"type": "init", "vps_id": 1}                 → connect to VPS (after auth)
"""

from __future__ import annotations

import asyncio
import json
import logging

try:
    import asyncssh
except ImportError:
    asyncssh = None  # type: ignore — optional dependency

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import database as db

log = logging.getLogger("unified.terminal")

router = APIRouter(tags=["terminal"])


def _parse_control_message(msg: str) -> dict | None:
    """Try to parse a JSON control message. Returns dict or None for raw input."""
    try:
        data = json.loads(msg)
        if isinstance(data, dict) and "type" in data:
            return data
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


async def _verify_ws_auth(token: str) -> bool:
    """Verify admin password from WebSocket query param."""
    if not token:
        return False
    # Check against DB admin password
    saved_pw = await db.get_setting("admin_password", "")
    if saved_pw and token == saved_pw:
        return True
    # Check against default
    from .config import ADMIN_PASSWORD
    return token == ADMIN_PASSWORD


@router.websocket("/ws/terminal/{vps_id}")
async def terminal_websocket(websocket: WebSocket, vps_id: int):
    """Interactive SSH terminal via WebSocket.

    Connect: ws://host/ws/terminal/{vps_id}?token={admin_password}

    First message should be {"type": "resize", "cols": N, "rows": N} to set initial size.
    Subsequent text messages are forwarded as keystrokes.
    JSON {"type": "resize", ...} messages resize the PTY.
    """
    # Auth via query param
    token = websocket.query_params.get("token", "")
    if not await _verify_ws_auth(token):
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()

    # Special case: vps_id=0 means local terminal
    if vps_id == 0:
        await _local_terminal(websocket)
        return

    # Check asyncssh available
    if asyncssh is None:
        await websocket.send_text("\r\n\x1b[31mError: asyncssh not installed. Run: pip install asyncssh\x1b[0m\r\n")
        await websocket.close()
        return

    # Get VPS credentials
    server = await db.get_vps_server(vps_id)
    if not server:
        await websocket.send_text("\r\n\x1b[31mError: VPS not found\x1b[0m\r\n")
        await websocket.close()
        return

    conn = None
    process = None

    try:
        # Wait for initial resize message to get terminal dimensions
        cols, rows = 80, 24
        try:
            first_msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            ctrl = _parse_control_message(first_msg)
            if ctrl and ctrl.get("type") == "resize":
                cols = int(ctrl.get("cols", 80))
                rows = int(ctrl.get("rows", 24))
        except asyncio.TimeoutError:
            pass

        # Connect to VPS
        await websocket.send_text(
            f"\x1b[33mConnecting to {server['host']}...\x1b[0m\r\n"
        )

        conn = await asyncssh.connect(
            server["host"],
            port=server["ssh_port"],
            username=server["username"],
            password=server["password"],
            known_hosts=None,
            connect_timeout=15,
            keepalive_interval=30,
        )

        # Create interactive PTY process
        process = await conn.create_process(
            term_type="xterm-256color",
            term_size=(cols, rows),
        )

        await websocket.send_text(
            f"\x1b[32mConnected to {server['label'] or server['host']}\x1b[0m\r\n"
        )

        # Bidirectional bridge
        async def ssh_to_ws():
            """Forward SSH stdout → WebSocket."""
            try:
                while not process.stdout.at_eof():
                    data = await process.stdout.read(4096)
                    if data:
                        text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
                        await websocket.send_text(text)
            except (asyncssh.Error, ConnectionError):
                pass

        async def ws_to_ssh():
            """Forward WebSocket input → SSH stdin."""
            try:
                while True:
                    msg = await websocket.receive_text()
                    ctrl = _parse_control_message(msg)
                    if ctrl:
                        if ctrl.get("type") == "resize":
                            c = int(ctrl.get("cols", 80))
                            r = int(ctrl.get("rows", 24))
                            process.change_terminal_size(c, r)
                        # Other control messages can be added here
                    else:
                        process.stdin.write(msg)
            except WebSocketDisconnect:
                pass

        # Run both directions concurrently
        done, pending = await asyncio.wait(
            [asyncio.create_task(ssh_to_ws()), asyncio.create_task(ws_to_ssh())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

    except asyncssh.PermissionDenied:
        await websocket.send_text("\r\n\x1b[31mAuthentication failed\x1b[0m\r\n")
    except asyncssh.DisconnectError as e:
        await websocket.send_text(f"\r\n\x1b[31mSSH disconnected: {e}\x1b[0m\r\n")
    except (OSError, asyncio.TimeoutError) as e:
        await websocket.send_text(f"\r\n\x1b[31mConnection failed: {e}\x1b[0m\r\n")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("Terminal session error for VPS %d", vps_id)
        try:
            await websocket.send_text(f"\r\n\x1b[31mError: {e}\x1b[0m\r\n")
        except Exception:
            pass
    finally:
        if process:
            try:
                process.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass


async def _local_terminal(websocket: WebSocket):
    """Local terminal session (no SSH, direct subprocess)."""
    import os
    import sys

    cols, rows = 80, 24
    try:
        first_msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
        ctrl = _parse_control_message(first_msg)
        if ctrl and ctrl.get("type") == "resize":
            cols = int(ctrl.get("cols", 80))
            rows = int(ctrl.get("rows", 24))
    except asyncio.TimeoutError:
        pass

    shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
    if os.name == "nt":
        shell = "powershell.exe"

    await websocket.send_text(f"\x1b[32mLocal terminal ({shell})\x1b[0m\r\n")

    try:
        # Use asyncio subprocess with PTY-like behavior
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)

        process = await asyncio.create_subprocess_exec(
            shell,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        async def proc_to_ws():
            while True:
                data = await process.stdout.read(4096)
                if not data:
                    break
                await websocket.send_text(data.decode("utf-8", errors="replace"))

        async def ws_to_proc():
            try:
                while True:
                    msg = await websocket.receive_text()
                    ctrl = _parse_control_message(msg)
                    if ctrl and ctrl.get("type") == "resize":
                        continue  # Can't resize subprocess easily
                    if process.stdin:
                        process.stdin.write(msg.encode("utf-8"))
                        await process.stdin.drain()
            except WebSocketDisconnect:
                pass

        done, pending = await asyncio.wait(
            [asyncio.create_task(proc_to_ws()), asyncio.create_task(ws_to_proc())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

    except Exception as e:
        try:
            await websocket.send_text(f"\r\n\x1b[31mError: {e}\x1b[0m\r\n")
        except Exception:
            pass
    finally:
        try:
            if process and process.returncode is None:
                process.terminate()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
