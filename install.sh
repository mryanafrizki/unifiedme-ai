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
MAX_PYTHON="3.13"
CMD_NAME="unifiedme"

echo ""
echo -e "  ${CYAN}+======================================+${NC}"
echo -e "  ${CYAN}|   Unified AI Proxy - Installer       |${NC}"
echo -e "  ${CYAN}+======================================+${NC}"
echo ""

# ─── Detect OS ───────────────────────────────────────────────────────────────

IS_WINDOWS=false
IS_LINUX=false
IS_MAC=false
case "$OSTYPE" in
    msys*|mingw*|cygwin*) IS_WINDOWS=true ;;
    linux-gnu*)           IS_LINUX=true ;;
    darwin*)              IS_MAC=true ;;
esac

# ─── Install native build dependencies ───────────────────────────────────────

echo -e "  ${CYAN}Checking native dependencies...${NC}"

if [ "$IS_WINDOWS" = true ]; then
    # VC++ Redistributable — required for greenlet, orjson, pydantic-core DLLs
    echo -e "  Checking VC++ Redistributable..."
    VC_INSTALLED=$(powershell.exe -NoProfile -Command "
        \$r = Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\X64' -ErrorAction SilentlyContinue
        if (\$r) { Write-Output 'YES' } else { Write-Output 'NO' }
    " 2>/dev/null | tr -d '\r')

    if [ "$VC_INSTALLED" = "YES" ]; then
        echo -e "  $CHECK VC++ Redistributable"
    else
        echo -e "  ${YELLOW}Installing VC++ Redistributable...${NC}"
        powershell.exe -NoProfile -Command "
            \$url = 'https://aka.ms/vs/17/release/vc_redist.x64.exe'
            \$out = \"\$env:TEMP\\vc_redist.x64.exe\"
            Invoke-WebRequest -Uri \$url -OutFile \$out -UseBasicParsing
            Start-Process \$out -ArgumentList '/install /quiet /norestart' -Wait
        " 2>/dev/null
        echo -e "  $CHECK VC++ Redistributable installed"
    fi

elif [ "$IS_LINUX" = true ]; then
    # build-essential — required for compiling node-pty, greenlet, etc.
    NEED_BUILD=false
    for pkg in gcc make; do
        if ! command -v "$pkg" &>/dev/null; then
            NEED_BUILD=true
            break
        fi
    done

    if [ "$NEED_BUILD" = true ]; then
        echo -e "  ${YELLOW}Installing build-essential...${NC}"
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq 2>/dev/null
            sudo apt-get install -y -qq build-essential python3-dev 2>/dev/null
        elif command -v yum &>/dev/null; then
            sudo yum groupinstall -y "Development Tools" 2>/dev/null
            sudo yum install -y python3-devel 2>/dev/null
        elif command -v dnf &>/dev/null; then
            sudo dnf groupinstall -y "Development Tools" 2>/dev/null
            sudo dnf install -y python3-devel 2>/dev/null
        fi
        echo -e "  $CHECK build-essential"
    else
        echo -e "  $CHECK build-essential"
    fi

elif [ "$IS_MAC" = true ]; then
    if ! xcode-select -p &>/dev/null; then
        echo -e "  ${YELLOW}Installing Xcode CLI tools...${NC}"
        xcode-select --install 2>/dev/null || true
        echo -e "  $WARN Xcode CLI tools — follow the popup to install, then re-run this script"
    else
        echo -e "  $CHECK Xcode CLI tools"
    fi
fi

echo ""

# ─── Check dependencies ─────────────────────────────────────────────────────

ERRORS=0
PYTHON_CMD=""

# Find suitable Python (3.10 - 3.13, reject 3.14+)
for cmd in python3.12 python3.11 python3.10 python3.13 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PY=$("$cmd" --version 2>&1 | awk '{print $2}')
        PY_MAJOR=$(echo "$PY" | cut -d. -f1)
        PY_MINOR=$(echo "$PY" | cut -d. -f2)
        if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ] && [ "$PY_MINOR" -le 13 ]; then
            PYTHON_CMD="$cmd"
            echo -e "  $CHECK Python $PY ($cmd)"
            break
        elif [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 14 ]; then
            echo -e "  $WARN Python $PY found but too new (max $MAX_PYTHON)"
        fi
    fi
done

# Windows: also check py launcher for 3.12
if [ -z "$PYTHON_CMD" ] && [ "$IS_WINDOWS" = true ]; then
    if command -v py &>/dev/null; then
        for minor in 12 11 10 13; do
            if py -3.$minor --version &>/dev/null 2>&1; then
                PY=$(py -3.$minor --version 2>&1 | awk '{print $2}')
                PYTHON_CMD="py -3.$minor"
                echo -e "  $CHECK Python $PY (py -3.$minor)"
                break
            fi
        done
    fi
fi

if [ -z "$PYTHON_CMD" ]; then
    echo -e "  $CROSS Python >= $MIN_PYTHON, <= $MAX_PYTHON not found"
    echo ""
    echo -e "  ${RED}Python 3.14+ is NOT supported (greenlet/camoufox incompatible).${NC}"
    echo -e "  ${YELLOW}Install Python 3.12 (recommended):${NC}"
    if [ "$IS_WINDOWS" = true ]; then
        echo "    https://www.python.org/downloads/release/python-3129/"
    elif [ "$IS_LINUX" = true ]; then
        echo "    sudo apt install -y python3.12 python3.12-venv"
    elif [ "$IS_MAC" = true ]; then
        echo "    brew install python@3.12"
    fi
    echo ""
    exit 1
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
        echo "    Install Git:    https://git-scm.com/downloads"
    elif [ "$IS_LINUX" = true ]; then
        echo "    sudo apt update && sudo apt install -y git curl"
    elif [ "$IS_MAC" = true ]; then
        echo "    brew install git curl"
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

# Check existing venv Python version — recreate if wrong version
if [ -d "$VENV_DIR" ]; then
    VENV_PY_CHECK=""
    for p in "$VENV_DIR/Scripts/python.exe" "$VENV_DIR/bin/python3" "$VENV_DIR/bin/python"; do
        if [ -f "$p" ]; then
            VENV_PY_CHECK=$("$p" --version 2>&1 | awk '{print $2}')
            VENV_PY_MINOR=$(echo "$VENV_PY_CHECK" | cut -d. -f2)
            if [ "$VENV_PY_MINOR" -ge 14 ]; then
                echo -e "  $WARN Existing venv uses Python $VENV_PY_CHECK (too new), recreating..."
                rm -rf "$VENV_DIR"
            fi
            break
        fi
    done
fi

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
for pkg in fastapi uvicorn httpx pydantic aiosqlite aiohttp greenlet; do
    if "$VENV_PYTHON" -c "import $pkg" 2>/dev/null; then
        echo -e "  $CHECK $pkg"
    else
        echo -e "  $CROSS $pkg"
        MISSING_PKGS=$((MISSING_PKGS + 1))
    fi
done

# Verify native C extensions (the ones that break on wrong Python/missing VC++)
NATIVE_OK=true
for pkg in greenlet orjson pydantic_core; do
    if ! "$VENV_PYTHON" -c "import $pkg" 2>/dev/null; then
        echo -e "  $CROSS $pkg (native extension failed)"
        NATIVE_OK=false
    fi
done

if [ "$NATIVE_OK" = false ]; then
    echo ""
    echo -e "  ${YELLOW}Native extensions failed. Attempting force reinstall...${NC}"
    "$VENV_PIP" install --force-reinstall greenlet orjson pydantic-core 2>&1 | tail -3

    # Re-check
    STILL_BROKEN=false
    for pkg in greenlet orjson pydantic_core; do
        if ! "$VENV_PYTHON" -c "import $pkg" 2>/dev/null; then
            STILL_BROKEN=true
            echo -e "  $CROSS $pkg still broken"
        fi
    done

    if [ "$STILL_BROKEN" = true ]; then
        echo ""
        echo -e "  ${RED}Native extensions still failing.${NC}"
        if [ "$IS_WINDOWS" = true ]; then
            echo -e "  ${YELLOW}Try: restart terminal/RDP, then re-run this script.${NC}"
            echo -e "  ${YELLOW}VC++ Redistributable may need a reboot to take effect.${NC}"
        else
            echo -e "  ${YELLOW}Try: sudo apt install -y python3-dev build-essential${NC}"
        fi
        exit 1
    fi
    echo -e "  $CHECK Native extensions fixed"
fi

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
