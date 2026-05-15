#!/bin/bash
# Batch Gumloop University Automation - Linux/Mac Runner

echo "================================"
echo "Batch Gumloop Automation (NEW)"
echo "================================"
echo ""

if [ $# -lt 2 ]; then
    echo "Usage:"
    echo ""
    echo "Single account:"
    echo "  ./run_batch.sh single EMAIL PASSWORD MCP_URL"
    echo ""
    echo "Batch mode:"
    echo "  ./run_batch.sh batch ACCOUNTS_FILE MCP_URL"
    echo ""
    echo "Examples:"
    echo "  ./run_batch.sh single user@example.com pass123 http://localhost:1430"
    echo "  ./run_batch.sh batch accounts.txt http://localhost:1430"
    echo ""
    exit 1
fi

MODE="$1"

if [ "$MODE" == "single" ]; then
    EMAIL="$2"
    PASSWORD="$3"
    MCP_URL="$4"
    
    echo "Mode: Single Account"
    echo "Email: $EMAIL"
    echo ""
    
    python3 batch_gumloop_university.py --email "$EMAIL" --password "$PASSWORD" --mcp-url "$MCP_URL"
    
elif [ "$MODE" == "batch" ]; then
    BATCH_FILE="$2"
    MCP_URL="$3"
    
    echo "Mode: Batch Processing"
    echo "File: $BATCH_FILE"
    echo ""
    
    python3 batch_gumloop_university.py --batch "$BATCH_FILE" --mcp-url "$MCP_URL"
    
else
    echo "❌ Invalid mode: $MODE"
    echo "Use 'single' or 'batch'"
    exit 1
fi

echo ""
echo "================================"
echo "Automation Complete"
echo "================================"
