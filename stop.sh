#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "🛑 Deteniendo Trading Bot HQ..."

for pidfile in .btcscalp.pid .altscalp.pid .dailyreport.pid .stateserver.pid .dashboard.pid .telegram.pid; do
    if [ -f "$SCRIPT_DIR/$pidfile" ]; then
        PID=$(cat "$SCRIPT_DIR/$pidfile")
        kill $PID 2>/dev/null && echo "✓ Detenido PID $PID ($pidfile)"
        rm "$SCRIPT_DIR/$pidfile"
    fi
done

pkill -f "scalping_bot.py" 2>/dev/null
pkill -f "altscalp_bot.py" 2>/dev/null
pkill -f "btc_scalp.py" 2>/dev/null
pkill -f "alt_scalp.py" 2>/dev/null
pkill -f "daily_report.py" 2>/dev/null
pkill -f "local_server.py" 2>/dev/null
pkill -f "vite.*5173" 2>/dev/null

osascript -e 'display notification "BTC Scalp + Alt Scalp detenidos" with title "Trading Bot HQ"'
echo "✅ Todo detenido."
