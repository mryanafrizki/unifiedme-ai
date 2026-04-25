#!/bin/bash
# Start unified proxy in background, log to file
cd "$(dirname "$0")"

# Create logs dir
mkdir -p logs

# Kill existing if running
if [ -f server.pid ]; then
    OLD_PID=$(cat server.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing server (PID $OLD_PID)..."
        kill "$OLD_PID" 2>/dev/null
        sleep 1
    fi
    rm -f server.pid
fi

# Start in background
echo "Starting unified proxy..."
nohup python -m uvicorn unified.main:app --host 0.0.0.0 --port 1430 \
    >> logs/server.log 2>&1 &

echo $! > server.pid
echo "Server started (PID $(cat server.pid))"
echo "Logs: tail -f logs/server.log"
