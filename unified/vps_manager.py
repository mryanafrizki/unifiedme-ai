"""VPS Manager — Remote VPS orchestration via SSH.

Manages remote VPS servers:
- SSH connection (password auth)
- Remote command execution
- Auto-install dependencies (Python, cloudflared, nginx, unifiedme)
- Service management (start/stop proxy, MCP, cloudflared, nginx)
- SFTP file upload (nginx configs, etc.)
- MCP workspace setup on remote
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

try:
    import asyncssh
except ImportError:
    asyncssh = None  # type: ignore — optional dependency

from .config import DATA_DIR

log = logging.getLogger("unified.vps")


# ─── SSH Connection ──────────────────────────────────────────────────────────


async def ssh_connect(
    host: str,
    username: str,
    password: str,
    port: int = 22,
    timeout: int = 15,
):
    """Connect to a remote VPS via SSH with password auth."""
    if asyncssh is None:
        raise ConnectionError("asyncssh not installed. Run: pip install asyncssh")
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                host,
                port=port,
                username=username,
                password=password,
                known_hosts=None,
                connect_timeout=timeout,
            ),
            timeout=timeout + 5,
        )
        return conn
    except asyncssh.PermissionDenied:
        raise ConnectionError(f"Authentication failed for {username}@{host}")
    except asyncssh.DisconnectError as e:
        raise ConnectionError(f"SSH disconnected: {e}")
    except asyncio.TimeoutError:
        raise ConnectionError(f"Connection timed out ({timeout}s)")
    except OSError as e:
        raise ConnectionError(f"Network error: {e}")


async def test_connection(host: str, username: str, password: str, port: int = 22) -> dict:
    """Test SSH connection to a VPS. Returns {ok, os_info, error}."""
    try:
        async with await ssh_connect(host, username, password, port) as conn:
            result = await conn.run("uname -a", check=False)
            os_info = result.stdout.strip() if result.stdout else ""
            return {"ok": True, "os_info": os_info}
    except ConnectionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Remote Command Execution ────────────────────────────────────────────────


async def run_command(
    host: str, username: str, password: str,
    command: str, port: int = 22, timeout: int = 60,
) -> dict:
    """Run a command on a remote VPS. Returns {stdout, stderr, exit_code, error}."""
    try:
        async with await ssh_connect(host, username, password, port) as conn:
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=timeout,
            )
            return {
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
                "exit_code": result.returncode,
            }
    except ConnectionError as e:
        return {"stdout": "", "stderr": "", "exit_code": -1, "error": str(e)}
    except asyncio.TimeoutError:
        return {"stdout": "", "stderr": "", "exit_code": -1, "error": f"Command timed out ({timeout}s)"}
    except Exception as e:
        return {"stdout": "", "stderr": "", "exit_code": -1, "error": str(e)}


async def run_command_stream(
    host: str, username: str, password: str,
    command: str, port: int = 22, timeout: int = 300,
):
    """Run a command and yield output lines as they come. For long-running installs."""
    try:
        conn = await ssh_connect(host, username, password, port)
    except ConnectionError as e:
        yield {"type": "error", "data": str(e)}
        return

    try:
        process = await conn.create_process(command)

        async def read_stream(stream, stream_name):
            while not stream.at_eof():
                line = await stream.readline()
                if line:
                    text = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
                    yield {"type": stream_name, "data": text.rstrip("\n")}

        # Read stdout and stderr concurrently
        async for item in read_stream(process.stdout, "stdout"):
            yield item

        await process.wait()
        yield {"type": "exit", "exit_code": process.returncode}

    except asyncio.TimeoutError:
        yield {"type": "error", "data": f"Command timed out ({timeout}s)"}
    except Exception as e:
        yield {"type": "error", "data": str(e)}
    finally:
        conn.close()


# ─── SFTP File Upload ────────────────────────────────────────────────────────


async def upload_file(
    host: str, username: str, password: str,
    content: str, remote_path: str, port: int = 22,
) -> dict:
    """Upload text content to a file on the remote VPS via SFTP."""
    try:
        async with await ssh_connect(host, username, password, port) as conn:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(remote_path, "w") as f:
                    await f.write(content)
            return {"ok": True, "path": remote_path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Nginx Config Apply ─────────────────────────────────────────────────────


async def apply_nginx_config(
    host: str, username: str, password: str,
    config_content: str, site_name: str = "unified-proxy",
    port: int = 22,
) -> dict:
    """Upload nginx config, enable site, test, and reload on remote VPS."""
    remote_path = f"/etc/nginx/sites-available/{site_name}"
    enabled_path = f"/etc/nginx/sites-enabled/{site_name}"

    try:
        async with await ssh_connect(host, username, password, port) as conn:
            # Upload config via SFTP
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(remote_path, "w") as f:
                    await f.write(config_content)

            # Enable site
            await conn.run(f"ln -sf {remote_path} {enabled_path}", check=False)
            # Remove default if exists
            await conn.run("rm -f /etc/nginx/sites-enabled/default", check=False)

            # Test nginx config
            test_result = await conn.run("nginx -t", check=False)
            if test_result.returncode != 0:
                return {
                    "ok": False,
                    "error": f"nginx config test failed: {test_result.stderr}",
                    "path": remote_path,
                }

            # Reload nginx
            reload_result = await conn.run("systemctl reload nginx", check=False)
            if reload_result.returncode != 0:
                return {
                    "ok": False,
                    "error": f"nginx reload failed: {reload_result.stderr}",
                    "path": remote_path,
                }

            return {"ok": True, "path": remote_path, "message": "Nginx config applied and reloaded"}

    except Exception as e:
        return {"ok": False, "error": str(e)}


async def apply_nginx_config_local(
    config_content: str, site_name: str = "unified-proxy",
) -> dict:
    """Apply nginx config on the local machine."""
    remote_path = f"/etc/nginx/sites-available/{site_name}"
    enabled_path = f"/etc/nginx/sites-enabled/{site_name}"

    try:
        # Write config
        proc = await asyncio.create_subprocess_shell(
            f"sudo tee {remote_path} > /dev/null",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate(input=config_content.encode())

        # Enable site
        await (await asyncio.create_subprocess_shell(
            f"sudo ln -sf {remote_path} {enabled_path}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )).communicate()

        # Remove default
        await (await asyncio.create_subprocess_shell(
            "sudo rm -f /etc/nginx/sites-enabled/default",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )).communicate()

        # Test
        proc = await asyncio.create_subprocess_shell(
            "sudo nginx -t",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"ok": False, "error": f"nginx test failed: {stderr.decode()}"}

        # Reload
        proc = await asyncio.create_subprocess_shell(
            "sudo systemctl reload nginx",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"ok": False, "error": f"nginx reload failed: {stderr.decode()}"}

        return {"ok": True, "path": remote_path, "message": "Nginx config applied locally"}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Service Management ─────────────────────────────────────────────────────


async def get_services_status(host: str, username: str, password: str, port: int = 22) -> dict:
    """Check status of services on remote VPS."""
    checks = {
        "nginx": "systemctl is-active nginx 2>/dev/null || echo inactive",
        "cloudflared_installed": "which cloudflared >/dev/null 2>&1 && echo yes || echo no",
        "python_installed": "python3 --version 2>/dev/null || echo not_found",
        "unifiedme_installed": "test -d ~/unifiedme-ai && echo yes || echo no",
        "proxy_running": "curl -s --max-time 3 http://127.0.0.1:1430/ >/dev/null 2>&1 && echo yes || echo no",
        "mcp_running": "curl -s --max-time 3 http://127.0.0.1:9876/mcp >/dev/null 2>&1 && echo yes || echo no",
    }

    combined = " && ".join(f'echo "@@{k}@@$({v})"' for k, v in checks.items())

    try:
        result = await run_command(host, username, password, combined, port, timeout=20)
        if result.get("error"):
            return {"error": result["error"]}

        status = {}
        for line in result["stdout"].split("\n"):
            line = line.strip()
            if line.startswith("@@") and "@@" in line[2:]:
                parts = line[2:].split("@@", 1)
                if len(parts) == 2:
                    status[parts[0]] = parts[1].strip()

        return {"ok": True, "services": status}

    except Exception as e:
        return {"error": str(e)}


async def toggle_service(
    host: str, username: str, password: str,
    service: str, action: str, port: int = 22,
) -> dict:
    """Start/stop/restart a service on remote VPS.

    service: nginx, cloudflared, proxy, mcp
    action: start, stop, restart, enable, disable
    """
    service_commands = {
        "nginx": {
            "start": "sudo systemctl start nginx",
            "stop": "sudo systemctl stop nginx",
            "restart": "sudo systemctl restart nginx",
            "enable": "sudo systemctl enable nginx",
            "disable": "sudo systemctl disable nginx",
        },
        "cloudflared": {
            "start": "cloudflared tunnel --url http://localhost:1430 &",
            "stop": "pkill -f 'cloudflared tunnel' || true",
        },
        "proxy": {
            "start": "cd ~/unifiedme-ai && nohup .venv/bin/python -m unified.main > unified/data/proxy.log 2>&1 &",
            "stop": "pkill -f 'unified.main' || true",
        },
        "mcp": {
            "start": "cd ~/unifiedme-ai && nohup .venv/bin/python mcp_server.py --no-interactive --no-tunnel > unified/data/mcp.log 2>&1 &",
            "stop": "pkill -f 'mcp_server.py' || true",
        },
    }

    if service not in service_commands:
        return {"ok": False, "error": f"Unknown service: {service}"}
    if action not in service_commands[service]:
        return {"ok": False, "error": f"Unknown action '{action}' for {service}"}

    cmd = service_commands[service][action]
    result = await run_command(host, username, password, cmd, port, timeout=30)

    if result.get("error"):
        return {"ok": False, "error": result["error"]}

    return {
        "ok": True,
        "service": service,
        "action": action,
        "exit_code": result["exit_code"],
        "stdout": result["stdout"][:500],
        "stderr": result["stderr"][:500],
    }


# ─── Auto-Install ────────────────────────────────────────────────────────────

# Install script that runs on the remote VPS
_INSTALL_SCRIPT = """#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

echo "@@STEP@@Updating package lists..."
apt-get update -qq

echo "@@STEP@@Upgrading system packages..."
apt-get upgrade -y -qq

echo "@@STEP@@Installing base dependencies..."
apt-get install -y -qq python3 python3-pip python3-venv git curl ufw

echo "@@STEP@@Installing cloudflared..."
if ! command -v cloudflared &>/dev/null; then
    ARCH=$(dpkg --print-architecture)
    curl -fsSL -o /tmp/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}"
    install -m 755 /tmp/cloudflared /usr/local/bin/cloudflared
    rm -f /tmp/cloudflared
    echo "cloudflared installed: $(cloudflared --version)"
else
    echo "cloudflared already installed: $(cloudflared --version)"
fi

echo "@@STEP@@Installing nginx..."
if ! command -v nginx &>/dev/null; then
    apt-get install -y -qq nginx
    systemctl enable nginx
    echo "nginx installed"
else
    echo "nginx already installed"
fi

echo "@@STEP@@Cloning unifiedme-ai..."
INSTALL_DIR="$HOME/unifiedme-ai"
if [ -d "$INSTALL_DIR" ]; then
    cd "$INSTALL_DIR"
    git pull --ff-only || git pull
    echo "Updated existing installation"
else
    git clone https://github.com/unifiedaa/unifiedme-ai.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    echo "Cloned fresh"
fi

echo "@@STEP@@Setting up Python venv + dependencies..."
cd "$INSTALL_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q

echo "@@STEP@@Setting up CLI..."
chmod +x unifiedme 2>/dev/null || true
ln -sf "$INSTALL_DIR/unifiedme" /usr/local/bin/unifiedme 2>/dev/null || true

echo "@@STEP@@Creating directories..."
mkdir -p "$INSTALL_DIR/unified/data"
mkdir -p "$HOME/mcp-workspaces"

echo "@@STEP@@Configuring firewall..."
ufw allow 22/tcp   >/dev/null 2>&1 || true
ufw allow 80/tcp   >/dev/null 2>&1 || true
ufw allow 443/tcp  >/dev/null 2>&1 || true
ufw allow 1430/tcp >/dev/null 2>&1 || true
ufw allow 9876/tcp >/dev/null 2>&1 || true
echo "y" | ufw enable >/dev/null 2>&1 || true
echo "Firewall: ports 22, 80, 443, 1430, 9876 opened"

echo "@@DONE@@Installation complete!"
echo "Version: $(cat $INSTALL_DIR/VERSION 2>/dev/null || echo unknown)"
echo "Path: $INSTALL_DIR"
"""


async def auto_install(host: str, username: str, password: str, port: int = 22) -> dict:
    """Run full auto-install on a remote VPS. Returns step-by-step results."""
    steps = []

    try:
        async with await ssh_connect(host, username, password, port, timeout=20) as conn:
            # Upload install script via SFTP
            async with conn.start_sftp_client() as sftp:
                async with sftp.open("/tmp/unifiedme_install.sh", "w") as f:
                    await f.write(_INSTALL_SCRIPT)

            # Run the script using conn.run() — simpler and more reliable than PTY
            log.info("[auto-install %s] Starting install script...", host)
            result = await asyncio.wait_for(
                conn.run("bash /tmp/unifiedme_install.sh 2>&1", check=False),
                timeout=600,  # 10 min max for full install
            )

            stdout = result.stdout or ""
            exit_code = result.returncode

            # Parse steps from output
            output_lines = []
            for line in stdout.split("\n"):
                line = line.strip()
                if line.startswith("@@STEP@@"):
                    step_name = line[8:]
                    steps.append({"step": step_name, "status": "done"})
                    log.info("[auto-install %s] %s", host, step_name)
                elif line.startswith("@@DONE@@"):
                    steps.append({"step": line[8:], "status": "done"})
                elif line:
                    output_lines.append(line)

            # If exit code non-zero, mark last step as failed
            if exit_code != 0 and steps:
                steps[-1]["status"] = "failed"

            # Cleanup
            await conn.run("rm -f /tmp/unifiedme_install.sh", check=False)

            return {
                "ok": exit_code == 0,
                "steps": steps,
                "exit_code": exit_code,
                "output": "\n".join(output_lines[-50:]),
                "stderr": (result.stderr or "")[:500],
            }

    except asyncio.TimeoutError:
        return {"ok": False, "error": "Install timed out (10 min limit)", "steps": steps}
    except ConnectionError as e:
        return {"ok": False, "error": str(e), "steps": steps}
    except Exception as e:
        return {"ok": False, "error": str(e), "steps": steps}


# ─── MCP Workspace on Remote ────────────────────────────────────────────────


async def setup_remote_mcp_workspace(
    host: str, username: str, password: str,
    folder_name: str, port: int = 22,
) -> dict:
    """Create an MCP workspace folder on a remote VPS."""
    # Sanitize
    import re
    safe_name = re.sub(r'[^\w\-.]', '_', folder_name.strip())
    if not safe_name:
        return {"ok": False, "error": "Invalid folder name"}

    cmd = f'mkdir -p "$HOME/mcp-workspaces/{safe_name}" && echo "$HOME/mcp-workspaces/{safe_name}"'
    result = await run_command(host, username, password, cmd, port)

    if result.get("error"):
        return {"ok": False, "error": result["error"]}

    path = result["stdout"].strip()
    return {"ok": True, "name": safe_name, "path": path}


async def list_remote_mcp_workspaces(
    host: str, username: str, password: str, port: int = 22,
) -> dict:
    """List MCP workspace folders on a remote VPS."""
    cmd = 'ls -1d "$HOME/mcp-workspaces"/*/ 2>/dev/null || echo ""'
    result = await run_command(host, username, password, cmd, port)

    if result.get("error"):
        return {"ok": False, "error": result["error"]}

    workspaces = []
    for line in result["stdout"].strip().split("\n"):
        line = line.strip().rstrip("/")
        if line:
            name = line.split("/")[-1]
            workspaces.append({"name": name, "path": line})

    return {"ok": True, "workspaces": workspaces}
