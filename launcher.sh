#!/bin/bash

# ─── Trading Bot HQ Launcher ───────────────────────────────────────────────────
# Lanza: Scalping BTC + Altcoins + Dashboard

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$SCRIPT_DIR"
DASHBOARD_DIR="$SCRIPT_DIR/polymarket-dashboard"
LOG_FILE="$SCRIPT_DIR/bot.log"


# RSI Bot
RSI_DATA_DIR="$SCRIPT_DIR/rsi_bot_data"

# Altcoin Bot
ALTCOIN_DATA_DIR="$SCRIPT_DIR/altcoin_data"
RSI_STATE="$RSI_DATA_DIR/state.json"

echo ""
echo "🤖 Trading Bot HQ"
echo "──────────────────────────────────"

# ─── Verificaciones ───────────────────────────────────────────────────────────
if [ ! -f "$BOT_DIR/.env" ]; then
    osascript -e 'display alert "Error" message "No se encontró el archivo .env. Configurá tus API keys primero." as critical'
    exit 1
fi

PYTHON="/opt/homebrew/bin/python3.14"
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

if ! command -v "$PYTHON" &> /dev/null; then
    osascript -e 'display alert "Error" message "Python3 no está instalado." as critical'
    exit 1
fi

echo "Usando $($PYTHON --version)..."
$PYTHON -c "import dotenv, requests, binance, pandas, numpy" 2>/dev/null
if [ $? -ne 0 ]; then
    osascript -e 'display dialog "Instalando dependencias Python..." buttons {"OK"} default button "OK"'
    $PYTHON -m pip install python-dotenv requests python-binance pandas numpy --quiet 2>/dev/null
fi

# ─── Local State Server (para dashboard) ─────────────────────────────────────
echo "Iniciando state server..."
nohup $PYTHON local_server.py >> "$LOG_FILE" 2>&1 &
SS_PID=$!
echo $SS_PID > "$SCRIPT_DIR/.stateserver.pid"
echo "✓ State server iniciado (PID: $SS_PID) → localhost:8765"

# ─── Lanzar Scalping Bot ─────────────────────────────────────────────────────
echo "Iniciando Scalping BTC 1m bot..."
nohup $PYTHON scalping_bot.py >> "$LOG_FILE" 2>&1 &
SC_PID=$!
echo $SC_PID > "$SCRIPT_DIR/.scalping.pid"
echo "✓ Scalping bot iniciado (PID: $SC_PID)"

# ─── Lanzar Altcoin Bot ──────────────────────────────────────────────────────
echo "Iniciando Multi-Altcoin bot..."
nohup $PYTHON altcoin_bot.py >> "$LOG_FILE" 2>&1 &
ALT_PID=$!
echo $ALT_PID > "$SCRIPT_DIR/.altcoin.pid"
echo "✓ Altcoin bot iniciado (PID: $ALT_PID)"

# ─── Lanzar AltScalp HFT Bot (con auto-restart) ──────────────────────────────
echo "Iniciando AltScalp HFT bot..."
nohup bash -c "cd \"$BOT_DIR\"; while true; do $PYTHON altscalp_bot.py; echo '[altscalp] reiniciando en 5s...'; sleep 5; done" >> "$LOG_FILE" 2>&1 &
AS_PID=$!
echo $AS_PID > "$SCRIPT_DIR/.altscalp.pid"
echo "✓ AltScalp bot iniciado (PID: $AS_PID, auto-restart activado)"

# ─── Lanzar Daily Report Daemon ─────────────────────────────────────────────
echo "Iniciando daily report daemon..."
nohup $PYTHON daily_report.py --loop >> "$LOG_FILE" 2>&1 &
DR_PID=$!
echo $DR_PID > "$SCRIPT_DIR/.dailyreport.pid"
echo "✓ Daily report daemon iniciado (PID: $DR_PID)"

# ─── Lanzar Telegram Commander ───────────────────────────────────────────────
echo "Iniciando Telegram commander..."
nohup $PYTHON telegram_commander.py >> "$LOG_FILE" 2>&1 &
TC_PID=$!
echo $TC_PID > "$SCRIPT_DIR/.telegram.pid"
echo "✓ Telegram commander iniciado (PID: $TC_PID)"

# ─── Lanzar Dashboard ─────────────────────────────────────────────────────────
HAS_NODE=false
if command -v node &> /dev/null && [ -d "$DASHBOARD_DIR/node_modules" ]; then
    HAS_NODE=true
fi

if [ "$HAS_NODE" = true ]; then
    echo "Iniciando dashboard..."
    cd "$DASHBOARD_DIR"
    nohup npm run dev -- --port 5173 > "$SCRIPT_DIR/dashboard.log" 2>&1 &
    DASH_PID=$!
    echo $DASH_PID > "$SCRIPT_DIR/.dashboard.pid"
    sleep 3
    open "http://localhost:5173"
    echo "✓ Dashboard iniciado → http://localhost:5173"
fi

echo ""
echo "✅ Todo corriendo!"
echo "   Scalping BTC: ciclos cada 30s"
echo "   Altcoins:     ciclos cada 1 min (top 20 por volumen)"
echo "   Log: tail -f $LOG_FILE"
echo ""

osascript -e 'display notification "Scalping + Altcoins iniciados" with title "Trading Bot HQ" subtitle "Dashboard en localhost:5173"'
tail -f "$LOG_FILE"

# ─── Sincronizar paper trading state con dashboard ────────────────────────────
if [ "$HAS_NODE" = true ]; then
    mkdir -p "$DASHBOARD_DIR/public/paper_trading"
    # Symlinks para que Vite sirva los JSON del paper trading
    mkdir -p "$DASHBOARD_DIR/public/rsi_bot_data"
    ln -sf "$BOT_DIR/rsi_bot_data/state.json" "$DASHBOARD_DIR/public/rsi_bot_data/state.json" 2>/dev/null
        mkdir -p "$DASHBOARD_DIR/public/altcoin_data"
    ln -sf "$BOT_DIR/altcoin_data/state.json" "$DASHBOARD_DIR/public/altcoin_data/state.json" 2>/dev/null
    echo "✓ Paper trading states vinculados al dashboard"
fi
