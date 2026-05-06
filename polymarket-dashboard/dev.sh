#!/bin/bash

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"

if ! lsof -nP -iTCP:8082 -sTCP:LISTEN >/dev/null 2>&1; then
  mkdir -p "$ROOT_DIR/logs"
  nohup "$PYTHON_BIN" "$ROOT_DIR/local_server.py" >> "$ROOT_DIR/logs/server.log" 2>&1 &
  echo $! > "$ROOT_DIR/.stateserver.pid"
  sleep 1
fi

exec vite "$@"
