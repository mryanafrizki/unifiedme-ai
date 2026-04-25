#!/bin/bash
set -e
cd /root/kiro-auth
echo "=== Creating venv ==="
python3 -m venv .venv
echo "=== Upgrading pip ==="
.venv/bin/pip install --quiet --upgrade pip
echo "=== Installing requirements ==="
.venv/bin/pip install --quiet -r requirements.txt
echo "=== Fetching Camoufox browser ==="
.venv/bin/python -m camoufox fetch
echo "=== Verifying imports ==="
.venv/bin/python -c "import camoufox; print('camoufox OK')"
.venv/bin/python -c "import aiohttp; print('aiohttp OK')"
.venv/bin/python -c "import playwright; print('playwright OK')"
echo "=== SETUP COMPLETE ==="