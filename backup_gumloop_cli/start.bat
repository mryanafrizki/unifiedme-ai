@echo off
REM Start MCP Server + Cloudflare Tunnel
REM Usage: _tmp_mcp_server\start.bat

powershell -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause
