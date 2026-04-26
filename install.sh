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
CMD_NAME="unifiedme"

echo ""
echo -e "  ${CYAN}+======================================+${NC}"
echo -e "  ${CYAN}|   Unified AI Proxy - Installer       |${NC}"
echo -e "  ${CYAN}+======================================+${NC}"
echo ""

# ─── Detect OS ───────────────────────────────────────────────────────────────

IS_WINDOWS=false
case "$OSTYPE" in
    msys*|mingw*|cygwin*) IS_WINDOWS=true ;;
esac

# ─── Check dependencies ─────────────────────────────────────────────────────

ERRORS=0
PYTHON_CMD=""

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

if command -v git &>/dev/null; then
    echo -e "  $CHECK git"
else
    echo -e "  $CROSS git"
    ERRORS=$((ERRORS + 1))
fi

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
        echo -e "  ${RED}Failed to clone. If private repo, run: gh auth login${NC}"
        exit 1
    fi
    cd "$INSTALL_DIR"
    echo -e "  $CHECK Repository cloned to $INSTALL_DIR"
fi

# ─── Create venv + find python/pip ───────────────────────────────────────────

VENV_DIR="$INSTALL_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo -e "  Creating virtual environment..."
    $PYTHON_CMD -m venv "$VENV_DIR"
    echo -e "  $CHECK Virtual environment created"
else
    echo -e "  $CHECK Virtual environment exists"
fi

# Find python/pip in venv (handles Windows Scripts/ vs Linux bin/)
VENV_PYTHON=""
VENV_PIP=""
for p in "$VENV_DIR/Scripts/python.exe" "$VENV_DIR/Scripts/python" "$VENV_DIR/bin/python3" "$VENV_DIR/bin/python"; do
    if [ -f "$p" ]; then VENV_PYTHON="$p"; break; fi
done
for p in "$VENV_DIR/Scripts/pip.exe" "$VENV_DIR/Scripts/pip" "$VENV_DIR/bin/pip3" "$VENV_DIR/bin/pip"; do
    if [ -f "$p" ]; then VENV_PIP="$p"; break; fi
done

if [ -z "$VENV_PYTHON" ] || [ -z "$VENV_PIP" ]; then
    echo -e "  ${RED}Cannot find python/pip in venv. Try: rm -rf $VENV_DIR${NC}"
    exit 1
fi

echo -e "  $CHECK venv: $VENV_PYTHON"

# ─── Install dependencies ───────────────────────────────────────────────────

echo -e "  Installing dependencies (this may take a minute)..."
"$VENV_PIP" install --upgrade pip 2>&1 | tail -1
if ! "$VENV_PIP" install -r requirements.txt 2>&1 | tail -5; then
    echo -e "  ${RED}pip install failed. Check errors above.${NC}"
    exit 1
fi

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

# ─── Camoufox (optional, ask user) ──────────────────────────────────────────

echo ""
echo -e "  ${CYAN}Camoufox is needed for batch login (browser automation, ~65MB).${NC}"
echo -n "  Install Camoufox now? [y/N]: "
read -r INSTALL_CAMOUFOX </dev/tty 2>/dev/null || INSTALL_CAMOUFOX="n"
if [[ "$INSTALL_CAMOUFOX" =~ ^[Yy]$ ]]; then
    echo -e "  Downloading Camoufox..."
    "$VENV_PYTHON" -m camoufox fetch 2>&1 | tail -3
    echo -e "  $CHECK Camoufox installed"
else
    echo -e "  $WARN Skipped. Install later: $VENV_PYTHON -m camoufox fetch"
fi

# ─── Create data directory ───────────────────────────────────────────────────

mkdir -p "$INSTALL_DIR/unified/data"

# ─── Create command wrapper ──────────────────────────────────────────────────
# Named "unifiedme" to avoid conflict with "unified/" package directory

cat > "$INSTALL_DIR/$CMD_NAME" << WRAPPER
#!/usr/bin/env bash
cd "$INSTALL_DIR" || { echo "Not found: $INSTALL_DIR"; exit 1; }
exec "$VENV_PYTHON" -m unified.cli "\$@"
WRAPPER
chmod +x "$INSTALL_DIR/$CMD_NAME"

# Install to PATH
INSTALLED_TO=""
for BIN_DIR in "$HOME/.local/bin" "$HOME/bin"; do
    mkdir -p "$BIN_DIR" 2>/dev/null || continue
    if cp "$INSTALL_DIR/$CMD_NAME" "$BIN_DIR/$CMD_NAME" 2>/dev/null; then
        chmod +x "$BIN_DIR/$CMD_NAME"
        INSTALLED_TO="$BIN_DIR"
        break
    fi
done

echo ""
if [ -n "$INSTALLED_TO" ]; then
    echo -e "  $CHECK Command '$CMD_NAME' installed to $INSTALLED_TO/$CMD_NAME"
    if ! command -v "$CMD_NAME" &>/dev/null; then
        echo ""
        echo -e "  ${YELLOW}Not in PATH yet. Run this, then restart terminal:${NC}"
        echo -e "    ${CYAN}echo 'export PATH=\"$INSTALLED_TO:\$PATH\"' >> ~/.bashrc && source ~/.bashrc${NC}"
    fi
else
    echo -e "  $WARN Could not install to PATH. Use directly:"
    echo -e "    ${CYAN}$INSTALL_DIR/$CMD_NAME run${NC}"
fi

# ─── Done ────────────────────────────────────────────────────────────────────

echo ""
echo -e "  ${GREEN}Installation complete!${NC}"
echo ""
echo -e "  ${CYAN}Commands:${NC}"
echo "    $CMD_NAME run          # Start proxy (foreground)"
echo "    $CMD_NAME start        # Start proxy (background)"
echo "    $CMD_NAME stop         # Stop proxy"
echo "    $CMD_NAME status       # Check status"
echo "    $CMD_NAME kill-port    # Free port 1430 if stuck"
echo "    $CMD_NAME logout       # Switch license key"
echo ""
echo -e "  ${CYAN}First time:${NC}"
echo "    1. $CMD_NAME run"
echo "    2. Enter your license key"
echo "    3. Open http://localhost:1430/dashboard"
echo "    4. Set your admin password"
echo "    5. Get your API key"
echo ""
echo -e "  ${CYAN}Or run directly:${NC}"
echo "    cd $INSTALL_DIR && $VENV_PYTHON -m unified.cli run"
echo ""
