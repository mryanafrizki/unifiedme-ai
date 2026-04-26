#!/usr/bin/env bash
set -e

# ─── Unified AI Proxy — Installer ───────────────────────────────────────────
# curl -sSL https://unified-api.roubot71.workers.dev/install | bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'
CHECK="${GREEN}[OK]${NC}"
CROSS="${RED}[MISSING]${NC}"
WARN="${YELLOW}[WARN]${NC}"

REPO="https://github.com/mryanafrizki/unifiedme-ai.git"
INSTALL_DIR="$HOME/unifiedme-ai"
MIN_PYTHON="3.10"

echo ""
echo -e "  ${CYAN}+======================================+${NC}"
echo -e "  ${CYAN}|   Unified AI Proxy - Installer       |${NC}"
echo -e "  ${CYAN}+======================================+${NC}"
echo ""

# ─── Detect OS ───────────────────────────────────────────────────────────────

IS_WINDOWS=false
VENV_BIN="bin"
PIP_CMD="pip"
PYTHON_CMD=""

case "$OSTYPE" in
    msys*|mingw*|cygwin*) IS_WINDOWS=true; VENV_BIN="Scripts" ;;
esac

# ─── Check dependencies ─────────────────────────────────────────────────────

ERRORS=0

# Python 3.10+
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PY=$("$cmd" --version 2>&1 | awk '{print $2}')
        PY_MAJOR=$(echo "$PY" | cut -d. -f1)
        PY_MINOR=$(echo "$PY" | cut -d. -f2)
        if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
            PYTHON_CMD="$cmd"
            echo -e "  $CHECK Python $PY ($cmd)"
            break
        fi
    fi
done
if [ -z "$PYTHON_CMD" ]; then
    echo -e "  $CROSS Python >= $MIN_PYTHON not found"
    ERRORS=$((ERRORS + 1))
fi

# Git
if command -v git &>/dev/null; then
    echo -e "  $CHECK git"
else
    echo -e "  $CROSS git"
    ERRORS=$((ERRORS + 1))
fi

# curl
if command -v curl &>/dev/null; then
    echo -e "  $CHECK curl"
else
    echo -e "  $CROSS curl"
    ERRORS=$((ERRORS + 1))
fi

echo ""

if [ "$ERRORS" -gt 0 ]; then
    echo -e "  ${RED}$ERRORS missing dependencies. Install them first:${NC}"
    echo ""
    if [ "$IS_WINDOWS" = true ]; then
        echo "    Install Python: https://www.python.org/downloads/"
        echo "    Install Git:    https://git-scm.com/downloads"
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
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
    git pull --ff-only 2>&1 || git pull 2>&1
    echo -e "  $CHECK Repository updated"
else
    echo -e "  Cloning repository..."
    if ! git clone "$REPO" "$INSTALL_DIR" 2>&1; then
        echo -e "  ${RED}Failed to clone. Check git access.${NC}"
        echo "  If private repo, run: gh auth login"
        exit 1
    fi
    cd "$INSTALL_DIR"
    echo -e "  $CHECK Repository cloned to $INSTALL_DIR"
fi

# ─── Create venv ─────────────────────────────────────────────────────────────

VENV_DIR="$INSTALL_DIR/.venv"
VENV_PIP="$VENV_DIR/$VENV_BIN/pip"
VENV_PYTHON="$VENV_DIR/$VENV_BIN/python"

if [ ! -d "$VENV_DIR" ]; then
    echo -e "  Creating virtual environment..."
    $PYTHON_CMD -m venv "$VENV_DIR"
    echo -e "  $CHECK Virtual environment created"
else
    echo -e "  $CHECK Virtual environment exists"
fi

# Verify venv python exists
if [ ! -f "$VENV_PYTHON" ]; then
    # Try alternate path (some systems)
    if [ -f "$VENV_DIR/bin/python" ]; then
        VENV_PYTHON="$VENV_DIR/bin/python"
        VENV_PIP="$VENV_DIR/bin/pip"
    elif [ -f "$VENV_DIR/Scripts/python.exe" ]; then
        VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
        VENV_PIP="$VENV_DIR/Scripts/pip.exe"
    else
        echo -e "  ${RED}Cannot find python in venv. Delete .venv and retry.${NC}"
        exit 1
    fi
fi

# ─── Install dependencies ───────────────────────────────────────────────────

echo -e "  Installing dependencies (this may take a minute)..."
"$VENV_PIP" install --upgrade pip 2>&1 | tail -1
if ! "$VENV_PIP" install -r requirements.txt 2>&1 | tail -5; then
    echo -e "  ${RED}pip install failed. Check errors above.${NC}"
    exit 1
fi

# Verify critical packages
echo ""
MISSING_PKGS=0
for pkg in fastapi uvicorn httpx pydantic aiosqlite aiohttp; do
    if "$VENV_PYTHON" -c "import $pkg" 2>/dev/null; then
        echo -e "  $CHECK $pkg"
    else
        echo -e "  $CROSS $pkg"
        MISSING_PKGS=$((MISSING_PKGS + 1))
    fi
done

if [ "$MISSING_PKGS" -gt 0 ]; then
    echo -e "\n  ${RED}$MISSING_PKGS packages failed. Run manually:${NC}"
    echo "    cd $INSTALL_DIR && $VENV_PIP install -r requirements.txt"
    exit 1
fi

# ─── Fetch Camoufox (optional) ───────────────────────────────────────────────

echo ""
echo -e "  Fetching Camoufox browser (optional, for batch login)..."
"$VENV_PYTHON" -m camoufox fetch 2>&1 | tail -1 && echo -e "  $CHECK Camoufox" || echo -e "  $WARN Camoufox skipped (install later with: $VENV_PYTHON -m camoufox fetch)"

# ─── Create data directory ───────────────────────────────────────────────────

mkdir -p "$INSTALL_DIR/unified/data"

# ─── Create unified command wrapper ─────────────────────────────────────────

# Create wrapper script in install dir
cat > "$INSTALL_DIR/unified" << WRAPPER
#!/usr/bin/env bash
cd "$INSTALL_DIR" || { echo "Install dir not found: $INSTALL_DIR"; exit 1; }
exec "$VENV_PYTHON" -m unified.cli "\$@"
WRAPPER
chmod +x "$INSTALL_DIR/unified"

# Try to symlink/copy to a PATH location
INSTALLED_TO=""
for BIN_DIR in "$HOME/.local/bin" "$HOME/bin" "/usr/local/bin"; do
    if [ -d "$BIN_DIR" ] || mkdir -p "$BIN_DIR" 2>/dev/null; then
        cp "$INSTALL_DIR/unified" "$BIN_DIR/unified" 2>/dev/null && chmod +x "$BIN_DIR/unified" 2>/dev/null
        if [ $? -eq 0 ]; then
            INSTALLED_TO="$BIN_DIR"
            break
        fi
    fi
done

echo ""
if [ -n "$INSTALLED_TO" ]; then
    echo -e "  $CHECK Command 'unified' installed to $INSTALLED_TO/unified"
    # Check if it's in PATH
    if ! command -v unified &>/dev/null; then
        echo -e "  $WARN Not in PATH. Add this to your shell profile:"
        echo -e "    ${CYAN}export PATH=\"$INSTALLED_TO:\$PATH\"${NC}"
    fi
else
    echo -e "  $WARN Could not install to PATH. Use directly:"
    echo -e "    ${CYAN}$INSTALL_DIR/unified run${NC}"
fi

# ─── Done ────────────────────────────────────────────────────────────────────

echo ""
echo -e "  ${GREEN}Installation complete!${NC}"
echo ""
echo -e "  ${CYAN}Commands:${NC}"
echo "    unified run          # Start proxy (foreground)"
echo "    unified start        # Start proxy (background)"
echo "    unified stop         # Stop proxy"
echo "    unified status       # Check status"
echo "    unified kill-port    # Free port 1430 if stuck"
echo "    unified logout       # Switch license key"
echo ""
echo -e "  ${CYAN}First time?${NC}"
echo "    1. Run: unified run"
echo "    2. Enter your license key"
echo "    3. Open http://localhost:1430/dashboard"
echo "    4. Set your admin password"
echo "    5. Get your API key"
echo ""
echo -e "  ${CYAN}Or run directly:${NC}"
echo "    cd $INSTALL_DIR && .venv/$VENV_BIN/python -m unified.cli run"
echo ""
