#!/bin/bash
# ─── UnifiedMe AI — VPS Auto-Install Script ─────────────────────────────────
# Run: curl -fsSL https://raw.githubusercontent.com/unifiedaa/unifiedme-ai/main/setup_vps.sh | bash
#
# Installs: system packages, Python 3, cloudflared, nginx, unifiedme-ai, firewall
# ─────────────────────────────────────────────────────────────────────────────
set -e
export DEBIAN_FRONTEND=noninteractive

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
NC='\033[0m'

REPO="https://github.com/unifiedaa/unifiedme-ai.git"
INSTALL_DIR="$HOME/unifiedme-ai"

echo ""
echo -e "  ${CYAN}+==========================================+${NC}"
echo -e "  ${CYAN}|   UnifiedMe AI — VPS Auto-Install        |${NC}"
echo -e "  ${CYAN}+==========================================+${NC}"
echo ""

# ─── Step 1: System update + upgrade ────────────────────────────────────────

echo -e "  ${CYAN}[1/8]${NC} Updating system packages..."
apt-get update -qq
echo -e "  ${CYAN}[1/8]${NC} Upgrading system packages..."
apt-get upgrade -y -qq
echo -e "  ${GREEN}[OK]${NC} System updated"

# ─── Step 2: Base dependencies ──────────────────────────────────────────────

echo -e "  ${CYAN}[2/8]${NC} Installing base dependencies..."
apt-get install -y -qq python3 python3-pip python3-venv git curl ufw
echo -e "  ${GREEN}[OK]${NC} Base dependencies installed"

# ─── Step 3: cloudflared ────────────────────────────────────────────────────

echo -e "  ${CYAN}[3/8]${NC} Installing cloudflared..."
if ! command -v cloudflared &>/dev/null; then
    ARCH=$(dpkg --print-architecture)
    curl -fsSL -o /tmp/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}"
    install -m 755 /tmp/cloudflared /usr/local/bin/cloudflared
    rm -f /tmp/cloudflared
    echo -e "  ${GREEN}[OK]${NC} cloudflared installed: $(cloudflared --version 2>&1 | head -1)"
else
    echo -e "  ${GREEN}[OK]${NC} cloudflared already installed: $(cloudflared --version 2>&1 | head -1)"
fi

# ─── Step 4: nginx ──────────────────────────────────────────────────────────

echo -e "  ${CYAN}[4/8]${NC} Installing nginx..."
if ! command -v nginx &>/dev/null; then
    apt-get install -y -qq nginx
    systemctl enable nginx
    echo -e "  ${GREEN}[OK]${NC} nginx installed"
else
    echo -e "  ${GREEN}[OK]${NC} nginx already installed"
fi

# ─── Step 5: Clone / update repo ────────────────────────────────────────────

echo -e "  ${CYAN}[5/8]${NC} Setting up unifiedme-ai..."
if [ -d "$INSTALL_DIR" ]; then
    cd "$INSTALL_DIR"
    git pull --ff-only 2>/dev/null || git pull 2>/dev/null || true
    echo -e "  ${GREEN}[OK]${NC} Updated existing installation"
else
    git clone "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    echo -e "  ${GREEN}[OK]${NC} Cloned fresh from $REPO"
fi

# ─── Step 6: Python venv + dependencies ─────────────────────────────────────

echo -e "  ${CYAN}[6/8]${NC} Setting up Python venv + dependencies..."
cd "$INSTALL_DIR"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
echo -e "  ${GREEN}[OK]${NC} Python dependencies installed"

# ─── Step 7: CLI + directories ──────────────────────────────────────────────

echo -e "  ${CYAN}[7/8]${NC} Setting up CLI + directories..."
mkdir -p "$INSTALL_DIR/unified/data"
mkdir -p "$HOME/mcp-workspaces"

# Generate CLI wrapper with correct paths (don't use the repo's unifiedme — it has dev machine paths)
VENV_PYTHON="$INSTALL_DIR/.venv/bin/python"
cat > "$INSTALL_DIR/unifiedme" << WRAPPER
#!/usr/bin/env bash
cd "$INSTALL_DIR" || { echo "Not found: $INSTALL_DIR"; exit 1; }
exec "$VENV_PYTHON" -m unified.cli "\$@"
WRAPPER
chmod +x "$INSTALL_DIR/unifiedme"

# Install to /usr/local/bin so it's in PATH everywhere
cp "$INSTALL_DIR/unifiedme" /usr/local/bin/unifiedme 2>/dev/null || true
chmod +x /usr/local/bin/unifiedme 2>/dev/null || true

# Also install to ~/.local/bin as fallback
mkdir -p "$HOME/.local/bin" 2>/dev/null || true
cp "$INSTALL_DIR/unifiedme" "$HOME/.local/bin/unifiedme" 2>/dev/null || true
chmod +x "$HOME/.local/bin/unifiedme" 2>/dev/null || true

# Ensure ~/.local/bin is in PATH (for non-root users)
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc" 2>/dev/null || true
fi

echo -e "  ${GREEN}[OK]${NC} CLI ready: $(which unifiedme 2>/dev/null || echo "$INSTALL_DIR/unifiedme")"

# ─── Step 8: Firewall ───────────────────────────────────────────────────────

echo -e "  ${CYAN}[8/8]${NC} Configuring firewall..."
ufw allow 22/tcp   >/dev/null 2>&1 || true
ufw allow 80/tcp   >/dev/null 2>&1 || true
ufw allow 443/tcp  >/dev/null 2>&1 || true
ufw allow 1430/tcp >/dev/null 2>&1 || true
ufw allow 9876/tcp >/dev/null 2>&1 || true
echo "y" | ufw enable >/dev/null 2>&1 || true
echo -e "  ${GREEN}[OK]${NC} Firewall configured (ports: 22, 80, 443, 1430, 9876)"

# ─── Done ───────────────────────────────────────────────────────────────────

VERSION=$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo "unknown")

echo ""
echo -e "  ${GREEN}+==========================================+${NC}"
echo -e "  ${GREEN}|   Installation Complete!                  |${NC}"
echo -e "  ${GREEN}+==========================================+${NC}"
echo ""
echo -e "  Version:   ${CYAN}$VERSION${NC}"
echo -e "  Path:      ${DIM}$INSTALL_DIR${NC}"
echo -e "  MCP Dir:   ${DIM}$HOME/mcp-workspaces${NC}"
echo ""
echo -e "  ${YELLOW}Next steps:${NC}"
echo -e "    1. Run:  ${CYAN}unifiedme run${NC}"
echo -e "    2. Enter your license key"
echo -e "    3. Open dashboard at http://YOUR_IP:1430/dashboard"
echo ""
