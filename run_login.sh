#!/bin/bash
set -e
cd /root/kiro-auth
export BATCHER_ENABLE_CAMOUFOX=true
export BATCHER_CAMOUFOX_HEADLESS=true
export BATCHER_KIRO_AUTH_DEBUG=true
export BATCHER_CONCURRENT=1
export BATCHER_PRIORITY=standard
.venv/bin/python login.py --email "ynqgep@gminol.com" --password "qwertyui" 2>&1