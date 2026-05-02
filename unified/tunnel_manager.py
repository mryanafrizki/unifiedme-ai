"""Tunnel Manager — Cloudflared tunnel + Nginx config management.

Manages cloudflared tunnels for:
- Proxy server (port 1430)
- MCP server (port 9876)

Supports 3 access modes:
- trycloudflare (auto-generated URL)
- IP VPS (direct IP access)
- Custom domain (with nginx reverse proxy)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .config import DATA_DIR, LISTEN_PORT

log = logging.getLogger("unified.tunnel")

# ─── Constants ───────────────────────────────────────────────────────────────

MCP_DEFAULT_PORT = 9876
MCP_WORKSPACE_BASE = Path.home() / "mcp-workspaces"

# ─── Tunnel State ────────────────────────────────────────────────────────────


# ─── Persistent Tunnel State (file-based, survives process restarts) ──────────

_TUNNEL_STATE_DIR = DATA_DIR / "tunnels"


def _state_file(target: str) -> Path:
    return _TUNNEL_STATE_DIR / f"{target}.json"


def _save_tunnel_state(target: str, data: dict) -> None:
    """Save tunnel state to disk."""
    _TUNNEL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_file(target).write_text(json.dumps(data))


def _load_tunnel_state(target: str) -> dict:
    """Load tunnel state from disk."""
    f = _state_file(target)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def _clear_tunnel_state(target: str) -> None:
    """Remove tunnel state file."""
    f = _state_file(target)
    if f.exists():
        f.unlink(missing_ok=True)


# ─── Cloudflared Detection & Install ─────────────────────────────────────────


def check_cloudflared() -> str | None:
    """Check if cloudflared is installed. Returns path or None."""
    return shutil.which("cloudflared")


def check_nginx() -> str | None:
    """Check if nginx is installed. Returns path or None."""
    return shutil.which("nginx")


def get_system_info() -> dict:
    """Get system info for install detection."""
    is_linux = platform.system() == "Linux"
    is_root = os.geteuid() == 0 if hasattr(os, "geteuid") else False
    return {
        "os": platform.system(),
        "is_linux": is_linux,
        "is_root": is_root,
        "cloudflared_installed": check_cloudflared() is not None,
        "nginx_installed": check_nginx() is not None,
        "cloudflared_path": check_cloudflared() or "",
        "nginx_path": check_nginx() or "",
    }


async def install_cloudflared() -> dict:
    """Install cloudflared on Linux. Returns {ok, message, error}."""
    if platform.system() != "Linux":
        return {"ok": False, "error": "Auto-install only supported on Linux"}

    if check_cloudflared():
        return {"ok": True, "message": "cloudflared already installed"}

    try:
        # Try official install method
        cmds = [
            "curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null",
            'echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflared.list',
            "sudo apt-get update",
            "sudo apt-get install -y cloudflared",
        ]

        for cmd in cmds:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)

        if check_cloudflared():
            return {"ok": True, "message": "cloudflared installed successfully"}

        # Fallback: direct binary download
        arch = platform.machine()
        if arch in ("x86_64", "amd64"):
            arch_suffix = "amd64"
        elif arch in ("aarch64", "arm64"):
            arch_suffix = "arm64"
        else:
            return {"ok": False, "error": f"Unsupported architecture: {arch}"}

        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch_suffix}"
        proc = await asyncio.create_subprocess_shell(
            f"sudo curl -fsSL -o /usr/local/bin/cloudflared {url} && sudo chmod +x /usr/local/bin/cloudflared",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if check_cloudflared():
            return {"ok": True, "message": "cloudflared installed (binary)"}

        return {"ok": False, "error": f"Install failed: {stderr.decode()[:200]}"}

    except asyncio.TimeoutError:
        return {"ok": False, "error": "Install timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def install_nginx() -> dict:
    """Install nginx on Linux. Returns {ok, message, error}."""
    if platform.system() != "Linux":
        return {"ok": False, "error": "Auto-install only supported on Linux"}

    if check_nginx():
        return {"ok": True, "message": "nginx already installed"}

    try:
        proc = await asyncio.create_subprocess_shell(
            "sudo apt-get update && sudo apt-get install -y nginx",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if check_nginx():
            return {"ok": True, "message": "nginx installed successfully"}

        return {"ok": False, "error": f"Install failed: {stderr.decode()[:200]}"}

    except asyncio.TimeoutError:
        return {"ok": False, "error": "Install timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Tunnel Start/Stop ───────────────────────────────────────────────────────


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    if pid <= 0:
        return False
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_tunnel(target: str, port: int | None = None) -> dict:
    """Start a cloudflared tunnel for the given target.

    Cloudflared runs as a fully detached daemon that survives CLI exit.
    State (PID, URL, port) is persisted to disk.

    Args:
        target: "proxy" or "mcp"
        port: Override port (default: 1430 for proxy, 9876 for mcp)

    Returns: {ok, url, error}
    """
    cf_path = check_cloudflared()
    if not cf_path:
        return {"ok": False, "error": "cloudflared not installed. Install it first."}

    default_port = LISTEN_PORT if target == "proxy" else MCP_DEFAULT_PORT
    actual_port = port or default_port

    # Check if already running (from persisted state)
    existing = _load_tunnel_state(target)
    if existing.get("pid") and _is_pid_alive(existing["pid"]):
        return {
            "ok": True,
            "url": existing.get("url", ""),
            "port": existing.get("port", actual_port),
            "pid": existing["pid"],
            "message": "Tunnel already running",
        }

    # Start cloudflared as a detached process
    # Key: stderr goes to a LOG FILE, not a pipe.
    # If we use PIPE, the pipe closes when CLI exits → cloudflared gets broken pipe → crashes.
    _TUNNEL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_file_path = _TUNNEL_STATE_DIR / f"{target}.log"

    try:
        log_fh = open(log_file_path, "w")

        if platform.system() == "Windows":
            proc = subprocess.Popen(
                [cf_path, "tunnel", "--url", f"http://localhost:{actual_port}"],
                stdout=subprocess.DEVNULL,
                stderr=log_fh,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            )
        else:
            proc = subprocess.Popen(
                [cf_path, "tunnel", "--url", f"http://localhost:{actual_port}"],
                stdout=subprocess.DEVNULL,
                stderr=log_fh,
                start_new_session=True,  # Detach from parent — survives CLI exit
                close_fds=True,
            )

        # Poll the log file for the tunnel URL (cloudflared writes it to stderr)
        tunnel_url = ""
        for _ in range(50):  # 25 seconds max (50 * 0.5s)
            time.sleep(0.5)
            if proc.poll() is not None:
                break  # Process died
            try:
                content = log_file_path.read_text(errors="replace")
                match = re.search(r'(https://[a-z0-9\-]+\.trycloudflare\.com)', content)
                if match:
                    tunnel_url = match.group(1)
                    break
            except Exception:
                pass

        if tunnel_url:
            state = {
                "pid": proc.pid,
                "url": tunnel_url,
                "port": actual_port,
                "started_at": time.time(),
                "target": target,
            }
            _save_tunnel_state(target, state)
            log.info("Tunnel started for %s: %s (port %d, PID %d)", target, tunnel_url, actual_port, proc.pid)
            return {"ok": True, "url": tunnel_url, "port": actual_port, "pid": proc.pid}
        else:
            if proc.poll() is not None:
                content = log_file_path.read_text(errors="replace")[:500]
                return {"ok": False, "error": f"cloudflared exited: {content}"}

            # Process running but no URL yet — save PID anyway
            _save_tunnel_state(target, {
                "pid": proc.pid, "url": "", "port": actual_port,
                "started_at": time.time(), "target": target,
            })
            return {"ok": False, "error": "Timeout waiting for tunnel URL", "pid": proc.pid}

    except Exception as e:
        return {"ok": False, "error": str(e)}


def stop_tunnel(target: str) -> dict:
    """Stop a cloudflared tunnel by killing its PID from persisted state.

    Args:
        target: "proxy" or "mcp"

    Returns: {ok, message}
    """
    state = _load_tunnel_state(target)
    pid = state.get("pid", 0)

    if not pid or not _is_pid_alive(pid):
        _clear_tunnel_state(target)
        return {"ok": True, "message": "No tunnel running"}

    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        else:
            os.kill(pid, 15)  # SIGTERM
            # Wait a bit, then force kill if needed
            for _ in range(10):
                time.sleep(0.5)
                if not _is_pid_alive(pid):
                    break
            else:
                os.kill(pid, 9)  # SIGKILL

        log.info("Tunnel stopped for %s (PID %d, was: %s)", target, pid, state.get("url", ""))
        _clear_tunnel_state(target)
        return {"ok": True, "message": f"Tunnel for {target} stopped (PID {pid})"}

    except Exception as e:
        _clear_tunnel_state(target)
        return {"ok": False, "error": str(e)}


def get_tunnel_status(target: str | None = None) -> dict:
    """Get tunnel status for one or all targets.

    Reads persisted state from disk and verifies PID is still alive.

    Args:
        target: "proxy", "mcp", or None for all

    Returns: dict with tunnel info
    """
    if target:
        state = _load_tunnel_state(target)
        pid = state.get("pid", 0)
        default_port = LISTEN_PORT if target == "proxy" else MCP_DEFAULT_PORT

        if pid and _is_pid_alive(pid):
            uptime = 0
            started_at = state.get("started_at", 0)
            if started_at:
                uptime = int(time.time() - started_at)
            return {
                "target": target,
                "status": "running",
                "url": state.get("url", ""),
                "port": state.get("port", default_port),
                "pid": pid,
                "uptime_seconds": uptime,
                "error": "",
            }
        else:
            # PID dead or no state — clean up
            if state:
                _clear_tunnel_state(target)
            return {
                "target": target,
                "status": "stopped",
                "url": "",
                "port": default_port,
                "pid": 0,
                "uptime_seconds": 0,
                "error": "",
            }

    # All tunnels
    result = {}
    for t in ("proxy", "mcp"):
        result[t] = get_tunnel_status(t)
    return result


def stop_all_tunnels():
    """Stop all running tunnels. Called on shutdown."""
    for target in ("proxy", "mcp"):
        state = _load_tunnel_state(target)
        if state.get("pid"):
            stop_tunnel(target)


# ─── Nginx Config Generation ────────────────────────────────────────────────


def generate_nginx_config(
    mode: str,
    domain: str = "",
    server_ip: str = "",
    proxy_port: int = LISTEN_PORT,
    mcp_port: int = MCP_DEFAULT_PORT,
    enable_ssl: bool = False,
    ssl_email: str = "",
) -> dict:
    """Generate nginx reverse proxy config.

    Args:
        mode: "trycloudflare", "ip", "domain"
        domain: Custom domain (required for "domain" mode)
        server_ip: VPS IP (required for "ip" mode)
        proxy_port: Proxy server port (default 1430)
        mcp_port: MCP server port (default 9876)
        enable_ssl: Enable Let's Encrypt SSL (domain mode only)
        ssl_email: Email for Let's Encrypt

    Returns: {ok, config, instructions, error}
    """
    if mode == "trycloudflare":
        return _nginx_trycloudflare(proxy_port, mcp_port)
    elif mode == "ip":
        if not server_ip:
            return {"ok": False, "error": "server_ip is required for IP mode"}
        return _nginx_ip_mode(server_ip, proxy_port, mcp_port)
    elif mode == "domain":
        if not domain:
            return {"ok": False, "error": "domain is required for domain mode"}
        return _nginx_domain_mode(domain, proxy_port, mcp_port, enable_ssl, ssl_email)
    else:
        return {"ok": False, "error": f"Unknown mode: {mode}. Use: trycloudflare, ip, domain"}


def _nginx_trycloudflare(proxy_port: int, mcp_port: int) -> dict:
    """Generate config for trycloudflare mode (no nginx needed)."""
    instructions = """# TryCloudflare Mode — No nginx needed!
#
# Cloudflared creates a temporary public URL automatically.
# Just start the tunnel from the dashboard or CLI:
#
#   1. Start proxy tunnel:  Click "Start Tunnel" for Proxy
#   2. Start MCP tunnel:    Click "Start Tunnel" for MCP
#
# The generated URLs (e.g. https://xxx-yyy.trycloudflare.com) are
# publicly accessible and route traffic through Cloudflare's network.
#
# Note: URLs change every time you restart the tunnel.
# For persistent URLs, use "Custom Domain" mode instead.
"""
    return {
        "ok": True,
        "config": "",
        "instructions": instructions,
        "mode": "trycloudflare",
        "message": "No nginx config needed for trycloudflare mode. Just start the tunnels.",
    }


def _nginx_ip_mode(server_ip: str, proxy_port: int, mcp_port: int) -> dict:
    """Generate nginx config for direct IP access."""
    config = f"""# Nginx config — Direct IP access
# Save to: /etc/nginx/sites-available/unified-proxy
# Enable:  sudo ln -sf /etc/nginx/sites-available/unified-proxy /etc/nginx/sites-enabled/
# Test:    sudo nginx -t
# Reload:  sudo systemctl reload nginx

# Unified AI Proxy (port {proxy_port})
server {{
    listen 80;
    server_name {server_ip};

    # Proxy API
    location / {{
        proxy_pass http://127.0.0.1:{proxy_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_buffering off;
        chunked_transfer_encoding on;
    }}
}}

# MCP Server (port {mcp_port})
server {{
    listen 8080;
    server_name {server_ip};

    location / {{
        proxy_pass http://127.0.0.1:{mcp_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }}
}}
"""
    instructions = f"""# Setup Instructions — Direct IP Mode
#
# 1. Save the config:
#    sudo tee /etc/nginx/sites-available/unified-proxy << 'NGINX_EOF'
{config}
#    NGINX_EOF
#
# 2. Enable the site:
#    sudo ln -sf /etc/nginx/sites-available/unified-proxy /etc/nginx/sites-enabled/
#    sudo rm -f /etc/nginx/sites-enabled/default
#
# 3. Test & reload:
#    sudo nginx -t && sudo systemctl reload nginx
#
# 4. Access:
#    Proxy:     http://{server_ip}
#    Dashboard: http://{server_ip}/dashboard
#    MCP:       http://{server_ip}:8080/mcp
#
# 5. Firewall (if needed):
#    sudo ufw allow 80/tcp
#    sudo ufw allow 8080/tcp
"""
    return {
        "ok": True,
        "config": config,
        "instructions": instructions,
        "mode": "ip",
        "access": {
            "proxy": f"http://{server_ip}",
            "dashboard": f"http://{server_ip}/dashboard",
            "mcp": f"http://{server_ip}:8080/mcp",
        },
    }


def _nginx_domain_mode(
    domain: str, proxy_port: int, mcp_port: int,
    enable_ssl: bool, ssl_email: str,
) -> dict:
    """Generate nginx config for custom domain."""
    mcp_subdomain = f"mcp.{domain}"

    if enable_ssl:
        config = f"""# Nginx config — Custom Domain with SSL
# Requires: certbot (Let's Encrypt)

# Proxy — {domain}
server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl http2;
    server_name {domain};

    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location / {{
        proxy_pass http://127.0.0.1:{proxy_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_buffering off;
        chunked_transfer_encoding on;
    }}
}}

# MCP Server — {mcp_subdomain}
server {{
    listen 80;
    server_name {mcp_subdomain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl http2;
    server_name {mcp_subdomain};

    ssl_certificate /etc/letsencrypt/live/{mcp_subdomain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{mcp_subdomain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location / {{
        proxy_pass http://127.0.0.1:{mcp_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }}
}}
"""
        ssl_cmd = f"sudo certbot --nginx -d {domain} -d {mcp_subdomain}"
        if ssl_email:
            ssl_cmd += f" --email {ssl_email} --agree-tos --non-interactive"

        instructions = f"""# Setup Instructions — Custom Domain with SSL
#
# Prerequisites:
#   - Domain {domain} pointing to your VPS IP (A record)
#   - Subdomain {mcp_subdomain} pointing to same IP (A record)
#
# 1. Install certbot:
#    sudo apt install -y certbot python3-certbot-nginx
#
# 2. Save the config (HTTP-only first):
#    sudo tee /etc/nginx/sites-available/unified-proxy << 'NGINX_EOF'
{_nginx_domain_http_only(domain, mcp_subdomain, proxy_port, mcp_port)}
#    NGINX_EOF
#
# 3. Enable & reload:
#    sudo ln -sf /etc/nginx/sites-available/unified-proxy /etc/nginx/sites-enabled/
#    sudo rm -f /etc/nginx/sites-enabled/default
#    sudo nginx -t && sudo systemctl reload nginx
#
# 4. Get SSL certificates:
#    {ssl_cmd}
#
# 5. Certbot will auto-update the nginx config with SSL.
#
# 6. Access:
#    Proxy:     https://{domain}
#    Dashboard: https://{domain}/dashboard
#    MCP:       https://{mcp_subdomain}/mcp
"""
    else:
        config = _nginx_domain_http_only(domain, mcp_subdomain, proxy_port, mcp_port)
        instructions = f"""# Setup Instructions — Custom Domain (HTTP)
#
# Prerequisites:
#   - Domain {domain} pointing to your VPS IP (A record)
#   - Subdomain {mcp_subdomain} pointing to same IP (A record)
#
# 1. Save the config:
#    sudo tee /etc/nginx/sites-available/unified-proxy << 'NGINX_EOF'
{config}
#    NGINX_EOF
#
# 2. Enable & reload:
#    sudo ln -sf /etc/nginx/sites-available/unified-proxy /etc/nginx/sites-enabled/
#    sudo rm -f /etc/nginx/sites-enabled/default
#    sudo nginx -t && sudo systemctl reload nginx
#
# 3. Access:
#    Proxy:     http://{domain}
#    Dashboard: http://{domain}/dashboard
#    MCP:       http://{mcp_subdomain}/mcp
#
# 4. (Optional) Add SSL later:
#    sudo apt install -y certbot python3-certbot-nginx
#    sudo certbot --nginx -d {domain} -d {mcp_subdomain}
"""

    return {
        "ok": True,
        "config": config,
        "instructions": instructions,
        "mode": "domain",
        "access": {
            "proxy": f"{'https' if enable_ssl else 'http'}://{domain}",
            "dashboard": f"{'https' if enable_ssl else 'http'}://{domain}/dashboard",
            "mcp": f"{'https' if enable_ssl else 'http'}://{mcp_subdomain}/mcp",
        },
    }


def _nginx_domain_http_only(
    domain: str, mcp_subdomain: str, proxy_port: int, mcp_port: int,
) -> str:
    """Generate HTTP-only nginx config for domain mode."""
    return f"""# Nginx config — Custom Domain (HTTP)

# Proxy — {domain}
server {{
    listen 80;
    server_name {domain};

    location / {{
        proxy_pass http://127.0.0.1:{proxy_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_buffering off;
        chunked_transfer_encoding on;
    }}
}}

# MCP Server — {mcp_subdomain}
server {{
    listen 80;
    server_name {mcp_subdomain};

    location / {{
        proxy_pass http://127.0.0.1:{mcp_port};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }}
}}
"""


# ─── MCP Workspace Management ───────────────────────────────────────────────


def setup_mcp_workspace(folder_name: str) -> dict:
    """Create an MCP workspace folder.

    Args:
        folder_name: Name for the workspace folder

    Returns: {ok, path, error}
    """
    if not folder_name or not folder_name.strip():
        return {"ok": False, "error": "Folder name is required"}

    # Sanitize folder name
    safe_name = re.sub(r'[^\w\-.]', '_', folder_name.strip())
    if not safe_name:
        return {"ok": False, "error": "Invalid folder name"}

    workspace_path = MCP_WORKSPACE_BASE / safe_name

    try:
        workspace_path.mkdir(parents=True, exist_ok=True)
        full_path = str(workspace_path.resolve())

        # Save to settings for persistence
        _save_mcp_workspace_config(safe_name, full_path)

        log.info("MCP workspace created: %s", full_path)
        return {
            "ok": True,
            "name": safe_name,
            "path": full_path,
            "message": f"Workspace folder created at: {full_path}",
        }

    except Exception as e:
        return {"ok": False, "error": f"Failed to create workspace: {e}"}


def get_mcp_workspaces() -> list[dict]:
    """List existing MCP workspace folders."""
    workspaces = []

    if MCP_WORKSPACE_BASE.exists():
        for item in sorted(MCP_WORKSPACE_BASE.iterdir()):
            if item.is_dir():
                workspaces.append({
                    "name": item.name,
                    "path": str(item.resolve()),
                })

    # Also check saved config
    config = _load_mcp_workspace_config()
    if config:
        for ws in config:
            path = Path(ws.get("path", ""))
            if path.exists() and not any(w["path"] == str(path) for w in workspaces):
                workspaces.append({
                    "name": ws.get("name", path.name),
                    "path": str(path),
                })

    return workspaces


def _mcp_config_path() -> Path:
    return DATA_DIR / ".mcp_workspaces.json"


def _save_mcp_workspace_config(name: str, path: str) -> None:
    """Save workspace to config file."""
    import json
    config_file = _mcp_config_path()
    config: list[dict] = []

    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
        except Exception:
            config = []

    # Add or update
    found = False
    for ws in config:
        if ws.get("name") == name:
            ws["path"] = path
            found = True
            break
    if not found:
        config.append({"name": name, "path": path})

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(config, indent=2))


def _load_mcp_workspace_config() -> list[dict]:
    """Load workspace config."""
    import json
    config_file = _mcp_config_path()
    if config_file.exists():
        try:
            return json.loads(config_file.read_text())
        except Exception:
            return []
    return []


def delete_mcp_workspace(name: str) -> dict:
    """Delete an MCP workspace folder and its config entry.

    Args:
        name: Workspace folder name

    Returns: {ok, message, error}
    """
    import json

    workspace_path = MCP_WORKSPACE_BASE / name

    # Remove from config
    config_file = _mcp_config_path()
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
            config = [ws for ws in config if ws.get("name") != name]
            config_file.write_text(json.dumps(config, indent=2))
        except Exception:
            pass

    # Remove folder (only if under MCP_WORKSPACE_BASE for safety)
    if workspace_path.exists() and str(workspace_path).startswith(str(MCP_WORKSPACE_BASE)):
        try:
            shutil.rmtree(workspace_path)
            return {"ok": True, "message": f"Workspace '{name}' deleted"}
        except Exception as e:
            return {"ok": False, "error": f"Failed to delete folder: {e}"}

    return {"ok": True, "message": f"Workspace '{name}' config removed (folder not found)"}


# ─── VPS IP Detection ────────────────────────────────────────────────────────


async def detect_vps_ip() -> str:
    """Try to detect the public IP of this VPS."""
    try:
        proc = await asyncio.create_subprocess_shell(
            "curl -s --max-time 5 https://ifconfig.me",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        ip = stdout.decode().strip()
        if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip):
            return ip
    except Exception:
        pass

    # Fallback
    try:
        proc = await asyncio.create_subprocess_shell(
            "curl -s --max-time 5 https://api.ipify.org",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        ip = stdout.decode().strip()
        if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip):
            return ip
    except Exception:
        pass

    return ""
