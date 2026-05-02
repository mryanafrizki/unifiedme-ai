"""FastAPI router for /api/vps/* — Remote VPS management endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse

from .auth_middleware import verify_admin
from . import database as db

log = logging.getLogger("unified.router_vps")

router = APIRouter(prefix="/api/vps", tags=["vps"])


# ─── VPS CRUD ────────────────────────────────────────────────────────────────


@router.get("/list")
async def list_vps(_: bool = Depends(verify_admin)):
    """List all registered VPS servers."""
    servers = await db.get_vps_servers()
    return {"servers": servers, "count": len(servers)}


@router.post("/add")
async def add_vps(request: Request, _: bool = Depends(verify_admin)):
    """Add a new VPS server. Body: {host, username, password, port?, label?}."""
    body = await request.json()
    host = str(body.get("host", "")).strip()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()
    ssh_port = int(body.get("port", 22))
    label = str(body.get("label", "")).strip()

    if not host or not username or not password:
        return JSONResponse({"error": "host, username, and password are required"}, status_code=400)

    # Test connection first
    from .vps_manager import test_connection
    test = await test_connection(host, username, password, ssh_port)

    if not test.get("ok"):
        return {"ok": False, "error": f"Connection failed: {test.get('error', 'Unknown')}"}

    # Save to DB
    vps_id = await db.add_vps_server(
        host=host, username=username, password=password,
        ssh_port=ssh_port, label=label or host,
        os_info=test.get("os_info", ""),
    )

    return {"ok": True, "id": vps_id, "os_info": test.get("os_info", "")}


@router.put("/{vps_id}")
async def update_vps(vps_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Update VPS server details."""
    body = await request.json()
    allowed = {"host", "username", "password", "ssh_port", "label"}
    fields = {k: v for k, v in body.items() if k in allowed and v is not None}
    if not fields:
        return JSONResponse({"error": "No valid fields"}, status_code=400)
    ok = await db.update_vps_server(vps_id, **fields)
    if not ok:
        return JSONResponse({"error": "VPS not found"}, status_code=404)
    return {"ok": True}


@router.delete("/{vps_id}")
async def delete_vps(vps_id: int, _: bool = Depends(verify_admin)):
    """Delete a VPS server."""
    ok = await db.delete_vps_server(vps_id)
    if not ok:
        return JSONResponse({"error": "VPS not found"}, status_code=404)
    return {"ok": True}


@router.post("/{vps_id}/test")
async def test_vps(vps_id: int, _: bool = Depends(verify_admin)):
    """Test SSH connection to a VPS."""
    server = await db.get_vps_server(vps_id)
    if not server:
        return JSONResponse({"error": "VPS not found"}, status_code=404)

    from .vps_manager import test_connection
    result = await test_connection(
        server["host"], server["username"], server["password"], server["ssh_port"],
    )

    if result.get("ok"):
        await db.update_vps_server(vps_id, status="online", os_info=result.get("os_info", ""))
    else:
        await db.update_vps_server(vps_id, status="offline")

    return result


# ─── Remote Command Execution ────────────────────────────────────────────────


@router.post("/{vps_id}/exec")
async def exec_command(vps_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Execute a command on a remote VPS. Body: {command, timeout?}."""
    server = await db.get_vps_server(vps_id)
    if not server:
        return JSONResponse({"error": "VPS not found"}, status_code=404)

    body = await request.json()
    command = str(body.get("command", "")).strip()
    timeout = int(body.get("timeout", 60))

    if not command:
        return JSONResponse({"error": "command is required"}, status_code=400)

    from .vps_manager import run_command
    result = await run_command(
        server["host"], server["username"], server["password"],
        command, server["ssh_port"], timeout,
    )
    return result


# ─── Service Management ─────────────────────────────────────────────────────


@router.get("/{vps_id}/services")
async def get_services(vps_id: int, _: bool = Depends(verify_admin)):
    """Get status of services on a remote VPS."""
    server = await db.get_vps_server(vps_id)
    if not server:
        return JSONResponse({"error": "VPS not found"}, status_code=404)

    from .vps_manager import get_services_status
    result = await get_services_status(
        server["host"], server["username"], server["password"], server["ssh_port"],
    )
    return result


@router.post("/{vps_id}/services/{service}/{action}")
async def control_service(
    vps_id: int, service: str, action: str, _: bool = Depends(verify_admin),
):
    """Control a service on a remote VPS. service: nginx|cloudflared|proxy|mcp, action: start|stop|restart."""
    server = await db.get_vps_server(vps_id)
    if not server:
        return JSONResponse({"error": "VPS not found"}, status_code=404)

    from .vps_manager import toggle_service
    result = await toggle_service(
        server["host"], server["username"], server["password"],
        service, action, server["ssh_port"],
    )
    return result


# ─── Auto-Install ────────────────────────────────────────────────────────────


@router.post("/{vps_id}/install")
async def auto_install_vps(vps_id: int, _: bool = Depends(verify_admin)):
    """Run full auto-install on a remote VPS (Python, cloudflared, nginx, unifiedme)."""
    server = await db.get_vps_server(vps_id)
    if not server:
        return JSONResponse({"error": "VPS not found"}, status_code=404)

    from .vps_manager import auto_install
    result = await auto_install(
        server["host"], server["username"], server["password"], server["ssh_port"],
    )

    if result.get("ok"):
        await db.update_vps_server(vps_id, status="installed")

    return result


# ─── Nginx Config ────────────────────────────────────────────────────────────


@router.post("/{vps_id}/nginx/apply")
async def apply_nginx(vps_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Generate and apply nginx config on a remote VPS.

    Body: {mode, domain?, server_ip?, proxy_port?, mcp_port?, enable_ssl?, ssl_email?}
    """
    server = await db.get_vps_server(vps_id)
    if not server:
        return JSONResponse({"error": "VPS not found"}, status_code=404)

    body = await request.json()

    # Generate config
    from .tunnel_manager import generate_nginx_config
    config_result = generate_nginx_config(
        mode=str(body.get("mode", "ip")).strip(),
        domain=str(body.get("domain", "")).strip(),
        server_ip=str(body.get("server_ip", server["host"])).strip(),
        proxy_port=int(body.get("proxy_port", 1430)),
        mcp_port=int(body.get("mcp_port", 9876)),
        enable_ssl=bool(body.get("enable_ssl", False)),
        ssl_email=str(body.get("ssl_email", "")).strip(),
    )

    if not config_result.get("ok"):
        return config_result

    config_content = config_result.get("config", "")
    if not config_content:
        return {"ok": True, "message": "No nginx config needed for this mode", **config_result}

    # Apply to remote VPS
    from .vps_manager import apply_nginx_config
    apply_result = await apply_nginx_config(
        server["host"], server["username"], server["password"],
        config_content, "unified-proxy", server["ssh_port"],
    )

    return {**apply_result, "access": config_result.get("access", {})}


@router.post("/local/nginx/apply")
async def apply_nginx_local(request: Request, _: bool = Depends(verify_admin)):
    """Generate and apply nginx config on the LOCAL machine.

    Body: {mode, domain?, server_ip?, proxy_port?, mcp_port?, enable_ssl?, ssl_email?}
    """
    body = await request.json()

    from .tunnel_manager import generate_nginx_config
    config_result = generate_nginx_config(
        mode=str(body.get("mode", "ip")).strip(),
        domain=str(body.get("domain", "")).strip(),
        server_ip=str(body.get("server_ip", "")).strip(),
        proxy_port=int(body.get("proxy_port", 1430)),
        mcp_port=int(body.get("mcp_port", 9876)),
        enable_ssl=bool(body.get("enable_ssl", False)),
        ssl_email=str(body.get("ssl_email", "")).strip(),
    )

    if not config_result.get("ok"):
        return config_result

    config_content = config_result.get("config", "")
    if not config_content:
        return {"ok": True, "message": "No nginx config needed for this mode", **config_result}

    from .vps_manager import apply_nginx_config_local
    apply_result = await apply_nginx_config_local(config_content, "unified-proxy")

    return {**apply_result, "access": config_result.get("access", {})}


# ─── MCP Workspace on Remote ────────────────────────────────────────────────


@router.post("/{vps_id}/mcp/workspace")
async def setup_remote_workspace(vps_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Create MCP workspace folder on a remote VPS. Body: {name}."""
    server = await db.get_vps_server(vps_id)
    if not server:
        return JSONResponse({"error": "VPS not found"}, status_code=404)

    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)

    from .vps_manager import setup_remote_mcp_workspace
    result = await setup_remote_mcp_workspace(
        server["host"], server["username"], server["password"],
        name, server["ssh_port"],
    )
    return result


@router.get("/{vps_id}/mcp/workspaces")
async def list_remote_workspaces(vps_id: int, _: bool = Depends(verify_admin)):
    """List MCP workspace folders on a remote VPS."""
    server = await db.get_vps_server(vps_id)
    if not server:
        return JSONResponse({"error": "VPS not found"}, status_code=404)

    from .vps_manager import list_remote_mcp_workspaces
    result = await list_remote_mcp_workspaces(
        server["host"], server["username"], server["password"], server["ssh_port"],
    )
    return result
