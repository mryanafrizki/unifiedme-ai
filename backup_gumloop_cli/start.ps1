# Start MCP Server + Cloudflare Tunnel
# Usage: powershell -ExecutionPolicy Bypass -File _tmp_mcp_server\start.ps1

$ErrorActionPreference = "Stop"
$PORT = 9876
$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$PROJECT_DIR = Split-Path -Parent $SCRIPT_DIR
$SERVER_SCRIPT = Join-Path $SCRIPT_DIR "mcp_server.py"
$WORKSPACE = Join-Path $PROJECT_DIR "_tmp_mcp_workspace"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  MCP Server + Cloudflare Tunnel" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Start MCP server in background
Write-Host "  [1] Starting MCP server on port $PORT..." -ForegroundColor Yellow
$mcpJob = Start-Process -FilePath "python" -ArgumentList "$SERVER_SCRIPT --port $PORT --workspace $WORKSPACE" -PassThru -NoNewWindow
Write-Host "  [1] MCP server PID: $($mcpJob.Id)" -ForegroundColor Green

Start-Sleep -Seconds 2

# Start cloudflared tunnel
Write-Host "  [2] Starting cloudflared tunnel..." -ForegroundColor Yellow
Write-Host ""
Write-Host "  Look for the *.trycloudflare.com URL below:" -ForegroundColor Magenta
Write-Host "  Copy it and use as --mcp-url in auto_register_mcp.py" -ForegroundColor Magenta
Write-Host ""
Write-Host "  Press Ctrl+C to stop both." -ForegroundColor Gray
Write-Host ""

try {
    cloudflared tunnel --url http://localhost:$PORT
} finally {
    Write-Host ""
    Write-Host "  Stopping MCP server (PID $($mcpJob.Id))..." -ForegroundColor Yellow
    Stop-Process -Id $mcpJob.Id -Force -ErrorAction SilentlyContinue
    Write-Host "  Done." -ForegroundColor Green
}
