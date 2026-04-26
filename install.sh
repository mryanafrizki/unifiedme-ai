#!/usr/bin/env bash
set -e

# ─── Unified AI Proxy — Installer ───────────────────────────────────────────
# Usage: curl -sSL https://api.unifiedme.dev/install/unifiedme-ai | bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
CHECK="${GREEN}[OK]${NC}"
CROSS="${RED}[MISSING]${NC}"

REPO="https://github.com/mryanafrizki/unifiedme-ai.git"
INSTALL_DIR="$HOME/unifiedme-ai"
VENV_DIR="$INSTALL_DIR/.venv"
MIN_PYTHON="3.10"

echo ""
echo -e "  ${CYAN}+======================================+${NC}"
echo -e "  ${CYAN}|   Unified AI Proxy — Installer       |${NC}"
echo -e "  ${CYAN}+======================================+${NC}"
echo ""

# ─── Check dependencies ─────────────────────────────────────────────────────

ERRORS=0

# Python 3.10+
if command -v python3 &>/dev/null; then
    PY=$(python3 --version 2>&1 | awk '{print $2}')
    PY_MAJOR=$(echo "$PY" | cut -d. -f1)
    PY_MINOR=$(echo "$PY" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        echo -e "  $CHECK Python $PY"
        PYTHON_CMD="python3"
    else
        echo -e "  $CROSS Python $PY (need >= $MIN_PYTHON)"
        ERRORS=$((ERRORS + 1))
    fi
elif command -v python &>/dev/null; then
    PY=$(python --version 2>&1 | awk '{print $2}')
    PY_MAJOR=$(echo "$PY" | cut -d. -f1)
    PY_MINOR=$(echo "$PY" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        echo -e "  $CHECK Python $PY"
        PYTHON_CMD="python"
    else
        echo -e "  $CROSS Python $PY (need >= $MIN_PYTHON)"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo -e "  $CROSS Python (not found, need >= $MIN_PYTHON)"
    ERRORS=$((ERRORS + 1))
fi

# Git
if command -v git &>/dev/null; then
    echo -e "  $CHECK git $(git --version | awk '{print $3}')"
else
    echo -e "  $CROSS git (not found)"
    ERRORS=$((ERRORS + 1))
fi

# pip
if command -v pip3 &>/dev/null || command -v pip &>/dev/null; then
    echo -e "  $CHECK pip"
else
    echo -e "  $CROSS pip (not found)"
    ERRORS=$((ERRORS + 1))
fi

# curl
if command -v curl &>/dev/null; then
    echo -e "  $CHECK curl"
else
    echo -e "  $CROSS curl (not found)"
    ERRORS=$((ERRORS + 1))
fi

echo ""

if [ "$ERRORS" -gt 0 ]; then
    echo -e "  ${RED}$ERRORS missing dependencies. Install them first:${NC}"
    echo ""
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        echo "    sudo apt update && sudo apt install -y python3 python3-pip python3-venv git curl"
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        echo "    brew install python3 git curl"
    fi
    echo ""
    exit 1
fi

# ─── Clone or update repo ───────────────────────────────────────────────────

if [ -d "$INSTALL_DIR/.git" ]; then
    echo -e "  Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull --ff-only 2>/dev/null || git pull
    echo -e "  $CHECK Repository updated"
else
    echo -e "  Cloning repository..."
    git clone "$REPO" "$INSTALL_DIR" 2>/dev/null
    cd "$INSTALL_DIR"
    echo -e "  $CHECK Repository cloned to $INSTALL_DIR"
fi

# ─── Create venv ─────────────────────────────────────────────────────────────

if [ ! -d "$VENV_DIR" ]; then
    echo -e "  Creating virtual environment..."
    $PYTHON_CMD -m venv "$VENV_DIR"
    echo -e "  $CHECK Virtual environment created"
else
    echo -e "  $CHECK Virtual environment exists"
fi

# ─── Install dependencies ───────────────────────────────────────────────────

echo -e "  Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip 2>/dev/null
"$VENV_DIR/bin/pip" install --quiet -r requirements.txt 2>/dev/null

# Check critical packages
MISSING_PKGS=0
for pkg in fastapi uvicorn httpx pydantic aiosqlite; do
    if "$VENV_DIR/bin/python" -c "import $pkg" 2>/dev/null; then
        echo -e "  $CHECK $pkg"
    else
        echo -e "  $CROSS $pkg"
        MISSING_PKGS=$((MISSING_PKGS + 1))
    fi
done

if [ "$MISSING_PKGS" -gt 0 ]; then
    echo -e "\n  ${RED}$MISSING_PKGS packages failed to install. Run manually:${NC}"
    echo "    cd $INSTALL_DIR && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

# ─── Fetch Camoufox browser (for batch login) ───────────────────────────────

echo -e "  Fetching Camoufox browser..."
"$VENV_DIR/bin/python" -m camoufox fetch 2>/dev/null && echo -e "  $CHECK Camoufox browser" || echo -e "  ${YELLOW}[SKIP]${NC} Camoufox (optional, for batch login)"

# ─── Create data directory ───────────────────────────────────────────────────

mkdir -p "$INSTALL_DIR/unified/data"

# ─── Create unified command ──────────────────────────────────────────────────

UNIFIED_BIN="/usr/local/bin/unified"
cat > /tmp/unified_cmd << 'SCRIPT'
#!/usr/bin/env bash
INSTALL_DIR="$HOME/unifiedme-ai"
cd "$INSTALL_DIR" || { echo "Install dir not found: $INSTALL_DIR"; exit 1; }
exec "$INSTALL_DIR/.venv/bin/python" -m unified.cli "$@"
SCRIPT

if [ -w "/usr/local/bin" ]; then
    mv /tmp/unified_cmd "$UNIFIED_BIN"
    chmod +x "$UNIFIED_BIN"
    echo -e "  $CHECK Command 'unified' installed to $UNIFIED_BIN"
else
    sudo mv /tmp/unified_cmd "$UNIFIED_BIN" 2>/dev/null && sudo chmod +x "$UNIFIED_BIN" 2>/dev/null
    if [ $? -eq 0 ]; then
        echo -e "  $CHECK Command 'unified' installed to $UNIFIED_BIN"
    else
        # Fallback: install to ~/.local/bin
        LOCAL_BIN="$HOME/.local/bin"
        mkdir -p "$LOCAL_BIN"
        mv /tmp/unified_cmd "$LOCAL_BIN/unified"
        chmod +x "$LOCAL_BIN/unified"
        echo -e "  $CHECK Command 'unified' installed to $LOCAL_BIN/unified"
        echo -e "  ${YELLOW}Add to PATH if needed: export PATH=\"\$HOME/.local/bin:\$PATH\"${NC}"
    fi
fi

# ─── Done ────────────────────────────────────────────────────────────────────

echo ""
echo -e "  ${GREEN}Installation complete!${NC}"
echo ""
echo -e "  ${CYAN}Quick start:${NC}"
echo "    unified run          # Start proxy (foreground)"
echo "    unified start        # Start proxy (background)"
echo "    unified stop         # Stop proxy"
echo "    unified status       # Check status"
echo "    unified kill-port    # Free port 1430 if stuck"
echo ""
echo -e "  ${CYAN}First time?${NC}"
echo "    1. Run 'unified run'"
echo "    2. Enter your license key when prompted"
echo "    3. Open http://localhost:1430/dashboard"
echo "    4. Set your admin password"
echo "    5. Get your API key"
echo ""
