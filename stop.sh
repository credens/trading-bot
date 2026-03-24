#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "🛑 Deteniendo Trading Bot HQ..."

for pidfile in .altcoin.pid .binance.pid .trading2.pid .dashboard.pid .bot.pid; do
    if [ -f "$SCRIPT_DIR/$pidfile" ]; then
        PID=$(cat "$SCRIPT_DIR/$pidfile")
        kill $PID 2>/dev/null && echo "✓ Detenido PID $PID ($pidfile)"
        rm "$SCRIPT_DIR/$pidfile"
    fi
done

pkill -f "binance_bot.py"  2>/dev/null
pkill -f "trading2.py"    2>/dev/null
pkill -f "altcoin_bot.py" 2>/dev/null
pkill -f "local_server.py" 2>/dev/null
pkill -f "sp500_bot.py"   2>/dev/null
pkill -f "ollama" 2>/dev/null && echo "✓ Ollama detenido"

# Detener Ollama
if pgrep -x "ollama" > /dev/null 2>&1; then
    pkill -x "ollama" 2>/dev/null
    echo "✓ Ollama detenido"
fi
pkill -f "vite.*5174" 2>/dev/null

osascript -e 'display notification "Binance + RSI Bot detenidos" with title "Trading Bot HQ"'
echo "✅ Todo detenido."
