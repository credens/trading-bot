#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_DIR="$SCRIPT_DIR/polymarket-dashboard"
PYTHON="${PYTHON:-python3}"

echo "Iniciando state server local..."
if ! lsof -nP -iTCP:8082 -sTCP:LISTEN >/dev/null 2>&1; then
    nohup "$PYTHON" "$SCRIPT_DIR/local_server.py" >> "$SCRIPT_DIR/logs/server.log" 2>&1 &
    echo $! > "$SCRIPT_DIR/.stateserver.pid"
    sleep 1
fi

echo "Iniciando dashboard..."
cd "$DASHBOARD_DIR" || exit 1
npm run dev -- --port 5173
