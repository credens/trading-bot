Y#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "🛑 Deteniendo Trading Bot HQ..."

for pidfile in .altcoin.pid .scalping.pid .dailyreport.pid .stateserver.pid .dashboard.pid .bot.pid; do
    if [ -f "$SCRIPT_DIR/$pidfile" ]; then
        PID=$(cat "$SCRIPT_DIR/$pidfile")
        kill $PID 2>/dev/null && echo "✓ Detenido PID $PID ($pidfile)"
        rm "$SCRIPT_DIR/$pidfile"
    fi
done

pkill -f "scalping_bot.py" 2>/dev/null
pkill -f "altcoin_bot.py" 2>/dev/null
pkill -f "daily_report.py" 2>/dev/null
pkill -f "local_server.py" 2>/dev/null
pkill -f "vite.*5173" 2>/dev/null

# WARP VPN (disabled — using proxy instead)
# WARP_CLI="/usr/local/bin/warp-cli"
# if [ -x "$WARP_CLI" ]; then
#     "$WARP_CLI" disconnect 2>/dev/null && echo "✓ WARP VPN desconectada"
# fi

osascript -e 'display notification "Scalping + Altcoins detenidos" with title "Trading Bot HQ"'
echo "✅ Todo detenido."
