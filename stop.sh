#!/bin/bash
# Stop unified proxy
cd "$(dirname "$0")"

if [ -f server.pid ]; then
    PID=$(cat server.pid)
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping server (PID $PID)..."
        kill "$PID"
        sleep 1
        # Force kill if still alive
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 "$PID"
        fi
        echo "Server stopped."
    else
        echo "Server not running (stale PID)."
    fi
    rm -f server.pid
else
    echo "No server.pid found. Trying to find process..."
    PID=$(pgrep -f "uvicorn unified.main:app" | head -1)
    if [ -n "$PID" ]; then
        echo "Found server (PID $PID), stopping..."
        kill "$PID"
        echo "Server stopped."
    else
        echo "No server running."
    fi
fi
