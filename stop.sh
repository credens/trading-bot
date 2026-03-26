#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "🛑 Deteniendo Trading Bot HQ..."

for pidfile in .altcoin.pid .binance.pid .trading2.pid .dashboard.pid .bot.pid .sp500.pid .stateserver.pid .rsi.pid .scalping.pid; do
    if [ -f "$SCRIPT_DIR/$pidfile" ]; then
        PID=$(cat "$SCRIPT_DIR/$pidfile")
        kill $PID 2>/dev/null && echo "✓ Detenido PID $PID ($pidfile)"
        rm -f "$SCRIPT_DIR/$pidfile"
    fi
done

# pkill por nombre como fallback (por si el PID file no existe)
pkill -f "scalping_bot.py" 2>/dev/null && echo "✓ scalping_bot"
pkill -f "binance_bot.py"  2>/dev/null && echo "✓ binance_bot"
pkill -f "trading2.py"    2>/dev/null && echo "✓ trading2"
pkill -f "altcoin_bot.py" 2>/dev/null && echo "✓ altcoin_bot"
pkill -f "local_server.py" 2>/dev/null && echo "✓ local_server"
pkill -f "sp500_bot.py"   2>/dev/null && echo "✓ sp500_bot"
pkill -f "server.py"      2>/dev/null && echo "✓ server"

# Vite dashboard — cualquier puerto
pkill -f "vite" 2>/dev/null && echo "✓ vite dashboard"

# Ollama
pkill -x "ollama" 2>/dev/null && echo "✓ ollama"

osascript -e 'display notification "Trading Bot HQ detenido" with title "Trading Bot"' 2>/dev/null
echo "✅ Todo detenido."
