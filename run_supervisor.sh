#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="$PYTHON"
elif [ -x "/opt/homebrew/bin/python3.14" ]; then
  PYTHON_BIN="/opt/homebrew/bin/python3.14"
elif [ -x "/opt/homebrew/bin/python3" ]; then
  PYTHON_BIN="/opt/homebrew/bin/python3"
else
  PYTHON_BIN="/usr/bin/python3"
fi

cd "$SCRIPT_DIR" || exit 1
mkdir -p logs

export PYTHON="$PYTHON_BIN"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec "$PYTHON_BIN" "$SCRIPT_DIR/bot_supervisor.py"
