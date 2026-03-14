#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "🛑 Deteniendo Trading Bot HQ..."

for pidfile in .altcoin.pid .binance.pid .dashboard.pid .bot.pid; do
    if [ -f "$SCRIPT_DIR/$pidfile" ]; then
        PID=$(cat "$SCRIPT_DIR/$pidfile")
        kill $PID 2>/dev/null && echo "✓ Detenido PID $PID ($pidfile)"
        rm "$SCRIPT_DIR/$pidfile"
    fi
done

pkill -f "binance_bot.py" 2>/dev/null
pkill -f "altcoin_bot.py" 2>/dev/null
pkill -f "vite.*5174" 2>/dev/null

osascript -e 'display notification "Binance + RSI Bot detenidos" with title "Trading Bot HQ"'
echo "✅ Todo detenido."
