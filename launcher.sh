#!/bin/bash

# ─── Trading Bot HQ Launcher ───────────────────────────────────────────────────
# Lanza: Polymarket bot + Binance bot + Dashboard
# Setup RAG automático para ambos bots

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$SCRIPT_DIR"
DASHBOARD_DIR="$SCRIPT_DIR/polymarket-dashboard"
LOG_FILE="$SCRIPT_DIR/bot.log"


# Binance RAG
BN_DATA_DIR="$SCRIPT_DIR/binance_data"
BN_MODEL="$BN_DATA_DIR/scoring_model.pkl"
BN_DB="$BN_DATA_DIR/historical.db"

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

if ! command -v python3 &> /dev/null; then
    osascript -e 'display alert "Error" message "Python3 no está instalado." as critical'
    exit 1
fi

echo "Verificando dependencias..."
python3 -c "import anthropic, dotenv, requests, binance, pandas, numpy" 2>/dev/null
if [ $? -ne 0 ]; then
    osascript -e 'display dialog "Instalando dependencias Python..." buttons {"OK"} default button "OK"'
    pip3 install anthropic python-dotenv requests python-binance pandas numpy --quiet 2>/dev/null
fi

# ─── RAG Binance BTC ──────────────────────────────────────────────────────────
echo ""
echo "📈 Binance BTC RAG..."

if [ ! -f "$BN_MODEL" ] || [ ! -f "$BN_DB" ]; then
    echo "   Primera vez: descargando 1 año de velas BTC (~3 min)..."
    python3 btc_rag_pipeline.py >> "$LOG_FILE" 2>&1
    echo "   ✓ Setup BTC RAG completo"
else
    BN_DAYS=$(( ( $(date +%s) - $(date -r "$BN_MODEL" +%s) ) / 86400 ))
    if [ "$BN_DAYS" -ge 7 ]; then
        echo "   🔄 Actualizando BTC RAG en background (${BN_DAYS} días)..."
        python3 btc_rag_pipeline.py refresh >> "$LOG_FILE" 2>&1 &
    else
        echo "   ✓ BTC RAG OK (hace ${BN_DAYS} días)"
    fi
fi

# ─── Lanzar Binance Bot ───────────────────────────────────────────────────────
echo "Iniciando Binance BTC bot..."
nohup python3 binance_bot.py >> "$LOG_FILE" 2>&1 &
BN_PID=$!
echo $BN_PID > "$SCRIPT_DIR/.binance.pid"
echo "✓ Binance bot iniciado (PID: $BN_PID)"

# ─── Lanzar Altcoin Bot ──────────────────────────────────────────────────────
echo "Iniciando Multi-Altcoin bot..."
nohup python3 altcoin_bot.py >> "$LOG_FILE" 2>&1 &
ALT_PID=$!
echo $ALT_PID > "$SCRIPT_DIR/.altcoin.pid"
echo "✓ Altcoin bot iniciado (PID: $ALT_PID)"

# ─── Lanzar RSI Bot (Alpaca Paper Trading) ───────────────────────────────────
echo "Iniciando RSI Mean Reversion bot..."
if python3 -c "import alpaca_trade_api" 2>/dev/null && grep -q "ALPACA_API_KEY" "$BOT_DIR/.env" && ! grep -q "ALPACA_API_KEY=tu_" "$BOT_DIR/.env"; then
    nohup python3 rsi_bot.py paper >> "$LOG_FILE" 2>&1 &
    RSI_PID=$!
    echo $RSI_PID > "$SCRIPT_DIR/.rsi.pid"
    echo "✓ RSI bot iniciado (PID: $RSI_PID)"
else
    echo "⚠ RSI bot saltado (configurá ALPACA_API_KEY en .env)"
fi

# ─── Lanzar Dashboard ─────────────────────────────────────────────────────────
HAS_NODE=false
if command -v node &> /dev/null && [ -d "$DASHBOARD_DIR/node_modules" ]; then
    HAS_NODE=true
fi

if [ "$HAS_NODE" = true ]; then
    echo "Iniciando dashboard..."
    cd "$DASHBOARD_DIR"
    nohup npm run dev -- --port 5174 > "$SCRIPT_DIR/dashboard.log" 2>&1 &
    DASH_PID=$!
    echo $DASH_PID > "$SCRIPT_DIR/.dashboard.pid"
    sleep 3
    open "http://localhost:5174"
    echo "✓ Dashboard iniciado → http://localhost:5174"
fi

echo ""
echo "✅ Todo corriendo!"
echo "   Binance BTC: ciclos cada 15 min"
echo "   Altcoins:    ciclos cada 15 min (top 20 por volumen)"
echo "   RSI S&P500:  opera al cierre del mercado US"
echo "   Log: tail -f $LOG_FILE"
echo ""

osascript -e 'display notification "Binance + Altcoins + RSI Bot iniciados" with title "Trading Bot HQ" subtitle "Dashboard en localhost:5174"'
tail -f "$LOG_FILE"

# ─── Sincronizar paper trading state con dashboard ────────────────────────────
if [ "$HAS_NODE" = true ]; then
    mkdir -p "$DASHBOARD_DIR/public/paper_trading"
    # Symlinks para que Vite sirva los JSON del paper trading
    ln -sf "$BOT_DIR/paper_trading/binance_state.json" "$DASHBOARD_DIR/public/paper_trading/binance_state.json" 2>/dev/null
    mkdir -p "$DASHBOARD_DIR/public/rsi_bot_data"
    ln -sf "$BOT_DIR/rsi_bot_data/state.json" "$DASHBOARD_DIR/public/rsi_bot_data/state.json" 2>/dev/null
        mkdir -p "$DASHBOARD_DIR/public/altcoin_data"
    ln -sf "$BOT_DIR/altcoin_data/state.json" "$DASHBOARD_DIR/public/altcoin_data/state.json" 2>/dev/null
    echo "✓ Paper trading states vinculados al dashboard"
fi
