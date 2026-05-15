#!/bin/bash
# Gumloop University Automation - Production Runner (Linux/Mac)

echo "================================"
echo "Gumloop University Automation"
echo "Production Mode (No Logs)"
echo "================================"
echo ""

if [ $# -lt 3 ]; then
    echo "Usage: ./run_production.sh EMAIL PASSWORD MCP_URL [ANSWERS]"
    echo ""
    echo "Examples:"
    echo "  ./run_production.sh user@example.com secret123 http://localhost:1430"
    echo "  ./run_production.sh user@example.com secret123 http://localhost:1430 \"2,3,3,2,2,2,2,2,2,2,2,2,2\""
    echo ""
    exit 1
fi

EMAIL="$1"
PASSWORD="$2"
MCP_URL="$3"
ANSWERS="$4"

echo "Email: $EMAIL"
echo "MCP URL: $MCP_URL"
echo ""

if [ -z "$ANSWERS" ]; then
    python3 gumloop_university.py --email "$EMAIL" --password "$PASSWORD" --mcp-url "$MCP_URL"
else
    python3 gumloop_university.py --email "$EMAIL" --password "$PASSWORD" --mcp-url "$MCP_URL" --answers "$ANSWERS"
fi

echo ""
echo "================================"
echo "Automation Complete"
echo "Check result.json for output"
echo "================================"
