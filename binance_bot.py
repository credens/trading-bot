"""
Binance BTC Futures Trading Bot
================================
Estrategia híbrida: indicadores técnicos + Claude AI decide long/short/neutral
Capital pequeño (<$500), apalancamiento conservador (3x-5x), stop loss automático

SETUP:
  pip install python-binance anthropic python-dotenv pandas numpy ta

CONFIGURAR .env:
  ANTHROPIC_API_KEY=sk-ant-...
  BINANCE_API_KEY=...
  BINANCE_SECRET_KEY=...
  LEVERAGE=3               # apalancamiento (recomendado 3-5x)
  MAX_RISK_PCT=0.02        # máximo 2% del capital por trade
  DRY_RUN=true             # siempre empezar en simulación
"""

import os
import json
import time
import logging
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
import pandas as pd
import numpy as np

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

SYMBOL = "BTCUSDT"
LEVERAGE = int(os.getenv("LEVERAGE", "3"))
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "0.02"))   # 2% del capital por trade
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.10"))  # máx 10% en una posición
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "15"))  # analizar cada 15 min

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

# ─── Clientes ─────────────────────────────────────────────────────────────────

def get_binance_client():
    from binance.client import Client
    return Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)


# ─── Paper Trading ───────────────────────────────────────────────────────────
from paper_trading import get_binance_engine

# ─── BTC RAG + Scoring (opcional) ────────────────────────────────────────────
try:
    from btc_rag_pipeline import find_similar_btc_patterns, score_btc_pattern, build_btc_enriched_prompt
    BTC_RAG_AVAILABLE = True
except ImportError:
    BTC_RAG_AVAILABLE = False

import anthropic as anthropic_lib
ai_client = anthropic_lib.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# ─── Indicadores Técnicos ──────────────────────────────────────────────────────

def fetch_ohlcv(client, symbol: str = SYMBOL, interval: str = "15m", limit: int = 100) -> pd.DataFrame:
    """Trae velas OHLCV de Binance Futures."""
    klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def calculate_indicators(df: pd.DataFrame) -> dict:
    """Calcula indicadores técnicos clave."""
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # RSI (14)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    # MACD (12, 26, 9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - signal_line

    # Bollinger Bands (20, 2)
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_pct = (close - bb_lower) / (bb_upper - bb_lower)  # 0=lower, 1=upper

    # ATR (14) — volatilidad
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()

    # Volumen relativo
    vol_sma20 = volume.rolling(20).mean()
    vol_ratio = volume / vol_sma20

    # EMA trend (50, 200)
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()

    last = -1  # última vela cerrada
    current_price = float(close.iloc[last])

    return {
        "price": current_price,
        "rsi": round(float(rsi.iloc[last]), 2),
        "macd": round(float(macd_line.iloc[last]), 2),
        "macd_signal": round(float(signal_line.iloc[last]), 2),
        "macd_hist": round(float(macd_hist.iloc[last]), 2),
        "macd_cross": "bullish" if macd_hist.iloc[last] > 0 and macd_hist.iloc[-2] <= 0
                      else "bearish" if macd_hist.iloc[last] < 0 and macd_hist.iloc[-2] >= 0
                      else "neutral",
        "bb_upper": round(float(bb_upper.iloc[last]), 2),
        "bb_lower": round(float(bb_lower.iloc[last]), 2),
        "bb_pct": round(float(bb_pct.iloc[last]), 3),
        "atr": round(float(atr.iloc[last]), 2),
        "atr_pct": round(float(atr.iloc[last] / current_price * 100), 3),
        "vol_ratio": round(float(vol_ratio.iloc[last]), 2),
        "ema50": round(float(ema50.iloc[last]), 2),
        "ema200": round(float(ema200.iloc[last]), 2),
        "trend": "bullish" if ema50.iloc[last] > ema200.iloc[last] else "bearish",
        "price_vs_ema50": round((current_price - float(ema50.iloc[last])) / float(ema50.iloc[last]) * 100, 2),
        # Últimas 3 velas para contexto
        "last_3_candles": [
            {
                "open": float(df["open"].iloc[i]),
                "close": float(df["close"].iloc[i]),
                "change_pct": round((float(df["close"].iloc[i]) - float(df["open"].iloc[i])) / float(df["open"].iloc[i]) * 100, 2)
            }
            for i in [-3, -2, -1]
        ],
    }


def get_market_context(client) -> dict:
    """Trae contexto adicional: funding rate, open interest, precio 24h."""
    try:
        ticker = client.futures_ticker(symbol=SYMBOL)
        funding = client.futures_funding_rate(symbol=SYMBOL, limit=1)
        oi = client.futures_open_interest(symbol=SYMBOL)

        return {
            "change_24h_pct": round(float(ticker.get("priceChangePercent", 0)), 2),
            "volume_24h": round(float(ticker.get("quoteVolume", 0)) / 1e6, 1),  # en millones
            "funding_rate": round(float(funding[0]["fundingRate"]) * 100, 4) if funding else 0,
            "open_interest_usdt": round(float(oi.get("openInterest", 0)) * float(ticker.get("lastPrice", 0)) / 1e9, 2),
        }
    except Exception as e:
        log.warning(f"Error trayendo contexto de mercado: {e}")
        return {}


# ─── Análisis con Claude ───────────────────────────────────────────────────────

ANALYSIS_PROMPT = """Eres un trader experto en BTC Futures con enfoque en gestión de riesgo. Analizá la siguiente situación de mercado y decidí si operar.

PRECIO ACTUAL BTC: ${price:,.2f}

INDICADORES TÉCNICOS (velas de 15min):
- RSI(14): {rsi}
- MACD: {macd:.2f} | Signal: {macd_signal:.2f} | Histograma: {macd_hist:.2f} | Cruz: {macd_cross}
- Bollinger Bands: upper={bb_upper:,.0f} / lower={bb_lower:,.0f} | posición: {bb_pct:.0%}
- Tendencia EMA50/200: {trend} | precio vs EMA50: {price_vs_ema50:+.2f}%
- ATR(14): ${atr:.0f} ({atr_pct:.2f}% del precio) — volatilidad actual
- Volumen relativo: {vol_ratio:.1f}x promedio

CONTEXTO 24H:
- Cambio precio: {change_24h_pct:+.2f}%
- Volumen: ${volume_24h:.0f}M USDT
- Funding rate: {funding_rate:+.4f}% (positivo = longs pagan a shorts)
- Open interest: ${open_interest_usdt:.1f}B USDT

ÚLTIMAS 3 VELAS (15min):
{candles_str}

PARÁMETROS DEL BOT:
- Apalancamiento: {leverage}x
- Capital total: ${capital:.2f} USDC
- Posición actual: {current_position}
- Stop loss: automático en 2% del capital

INSTRUCCIONES:
1. Analizá la confluencia de señales técnicas
2. Considerá el contexto macro (funding, OI, volumen)
3. La estrategia es HÍBRIDA: podés ir long, short o quedarte flat
4. Con capital pequeño (<$500), la preservación de capital es prioridad
5. Solo entrá si hay confluencia clara de al menos 3 señales
6. Si ya hay posición abierta, decidí si mantener, cerrar o invertir

Respondé ÚNICAMENTE con JSON válido:
{{
  "decision": "LONG|SHORT|FLAT|HOLD",
  "confidence": "HIGH|MEDIUM|LOW",
  "reasoning": "análisis de máximo 3 oraciones",
  "key_signals": ["señal1", "señal2", "señal3"],
  "entry_price": {price:.2f},
  "stop_loss_pct": 0.015,
  "take_profit_pct": 0.03,
  "position_size_pct": 0.05
}}

FLAT = no operar / cerrar posición si hay una abierta
HOLD = mantener posición actual sin cambios
stop_loss_pct: distancia del stop desde entrada (ej: 0.015 = 1.5%)
take_profit_pct: objetivo de ganancia (ej: 0.03 = 3%)
position_size_pct: % del capital a usar (máx 0.10 = 10%)
"""


def analyze_with_claude(indicators: dict, context: dict, capital: float, current_position: str) -> Optional[dict]:
    """Llama a Claude con todos los indicadores y pide decisión de trading."""
    if not ai_client:
        log.error("Claude no inicializado. Configurá ANTHROPIC_API_KEY.")
        return None

    candles_str = "\n".join([
        f"  Vela {i+1}: open={c['open']:,.0f} close={c['close']:,.0f} ({c['change_pct']:+.2f}%)"
        for i, c in enumerate(indicators.get("last_3_candles", []))
    ])

    prompt = ANALYSIS_PROMPT.format(
        **indicators,
        **context,
        candles_str=candles_str,
        leverage=LEVERAGE,
        capital=capital,
        current_position=current_position,
    )

    # Enriquecer con RAG si está disponible
    if BTC_RAG_AVAILABLE:
        indicators_for_rag = {**indicators, **context, "capital": capital, "current_position": current_position, "leverage": LEVERAGE}
        similar = find_similar_btc_patterns(indicators_for_rag, n=5)
        ml_score = score_btc_pattern(indicators_for_rag)
        prompt = build_btc_enriched_prompt(indicators_for_rag, similar, ml_score)
        if similar:
            log.info(f"    BTC RAG: {len(similar)} patrones similares | ML score: {ml_score:.1%}")

    try:
        response = ai_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())

    except Exception as e:
        log.error(f"Error analizando con Claude: {e}")
        return None


# ─── Ejecución de Órdenes ──────────────────────────────────────────────────────

def get_account_info(client) -> dict:
    """Trae balance y posición actual."""
    try:
        account = client.futures_account()
        balance = float(account.get("availableBalance", 0))

        positions = client.futures_position_information(symbol=SYMBOL)
        current_pos = "FLAT"
        pos_size = 0.0
        pos_entry = 0.0

        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt > 0:
                current_pos = "LONG"
                pos_size = amt
                pos_entry = float(p.get("entryPrice", 0))
            elif amt < 0:
                current_pos = "SHORT"
                pos_size = abs(amt)
                pos_entry = float(p.get("entryPrice", 0))

        return {
            "balance": balance,
            "position": current_pos,
            "position_size": pos_size,
            "entry_price": pos_entry,
        }
    except Exception as e:
        log.error(f"Error trayendo cuenta: {e}")
        return {"balance": 500.0, "position": "FLAT", "position_size": 0, "entry_price": 0}


def set_leverage(client, leverage: int = LEVERAGE):
    try:
        client.futures_change_leverage(symbol=SYMBOL, leverage=leverage)
        log.info(f"Apalancamiento configurado: {leverage}x")
    except Exception as e:
        log.warning(f"No se pudo setear leverage: {e}")


def close_position(client, current_position: str, position_size: float) -> bool:
    """Cierra posición abierta con orden market."""
    if current_position == "FLAT" or position_size == 0:
        return True

    side = "SELL" if current_position == "LONG" else "BUY"
    log.info(f"Cerrando posición {current_position} ({position_size} BTC)...")

    if DRY_RUN:
        log.info("  [DRY RUN] Posición cerrada (simulación)")
        return True

    try:
        client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type="MARKET",
            quantity=position_size,
            reduceOnly=True,
        )
        log.info("  ✅ Posición cerrada")
        return True
    except Exception as e:
        log.error(f"  ❌ Error cerrando posición: {e}")
        return False


def open_position(client, decision: dict, capital: float, current_price: float) -> bool:
    """Abre nueva posición con stop loss y take profit."""
    side = "BUY" if decision["decision"] == "LONG" else "SELL"
    pos_pct = min(float(decision.get("position_size_pct", 0.05)), MAX_POSITION_PCT)
    sl_pct = float(decision.get("stop_loss_pct", 0.015))
    tp_pct = float(decision.get("take_profit_pct", 0.03))

    # Calcular size en BTC
    usdt_to_use = capital * pos_pct * LEVERAGE
    btc_quantity = round(usdt_to_use / current_price, 3)

    # Calcular precios de SL y TP
    if decision["decision"] == "LONG":
        sl_price = round(current_price * (1 - sl_pct), 1)
        tp_price = round(current_price * (1 + tp_pct), 1)
    else:
        sl_price = round(current_price * (1 + sl_pct), 1)
        tp_price = round(current_price * (1 - tp_pct), 1)

    log.info(f"\n{'='*55}")
    log.info(f"NUEVA POSICIÓN: {decision['decision']}")
    log.info(f"  Precio entrada: ${current_price:,.2f}")
    log.info(f"  Cantidad: {btc_quantity} BTC (${usdt_to_use:.0f} USDT con {LEVERAGE}x)")
    log.info(f"  Stop Loss: ${sl_price:,.2f} (-{sl_pct:.1%})")
    log.info(f"  Take Profit: ${tp_price:,.2f} (+{tp_pct:.1%})")
    log.info(f"  Confianza Claude: {decision.get('confidence')}")
    log.info(f"  Reasoning: {decision.get('reasoning', '')}")
    log.info(f"  Señales: {', '.join(decision.get('key_signals', []))}")

    if DRY_RUN:
        log.info("  [DRY RUN] Orden NO ejecutada (simulación)")
        return True

    try:
        # Orden principal
        client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type="MARKET",
            quantity=btc_quantity,
        )

        # Stop Loss
        sl_side = "SELL" if side == "BUY" else "BUY"
        client.futures_create_order(
            symbol=SYMBOL,
            side=sl_side,
            type="STOP_MARKET",
            stopPrice=sl_price,
            closePosition=True,
        )

        # Take Profit
        client.futures_create_order(
            symbol=SYMBOL,
            side=sl_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=tp_price,
            closePosition=True,
        )

        log.info("  ✅ Posición abierta con SL y TP")
        return True

    except Exception as e:
        log.error(f"  ❌ Error abriendo posición: {e}")
        return False


# ─── Loop Principal ────────────────────────────────────────────────────────────

def run_cycle(client, paper=None):
    log.info(f"\n{'#'*55}")
    log.info(f"CICLO — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"{'#'*55}")

    # 1. Info de cuenta
    if DRY_RUN and paper:
        paper.check_binance_stops(0)  # se actualiza con precio real abajo
        open_trade = paper.get_binance_position()
        capital = paper.state.current_capital + sum(t.size for t in paper.state.open_trades)
        current_position = open_trade.side if open_trade else "FLAT"
        account = {"balance": capital, "position": current_position, "position_size": 0, "entry_price": open_trade.entry_price if open_trade else 0}
    else:
        account = get_account_info(client)
        capital = account["balance"]
        current_position = account["position"]

    log.info(f"Balance: ${capital:.2f} USDT | Posición: {current_position}")
    if current_position != "FLAT":
        log.info(f"  Entrada: ${account['entry_price']:,.2f}")

    # 2. Datos de mercado
    df = fetch_ohlcv(client)
    indicators = calculate_indicators(df)
    context = get_market_context(client)

    log.info(f"BTC: ${indicators['price']:,.2f} | RSI: {indicators['rsi']} | Tendencia: {indicators['trend']} | Vol: {indicators['vol_ratio']:.1f}x")

    # 3. Claude decide
    log.info("Consultando Claude...")
    decision = analyze_with_claude(indicators, context, capital, current_position)

    if not decision:
        log.warning("No se pudo obtener decisión de Claude. Saltando ciclo.")
        return

    action = decision.get("decision", "FLAT")
    confidence = decision.get("confidence", "LOW")
    log.info(f"Claude decide: {action} | Confianza: {confidence}")
    log.info(f"  → {decision.get('reasoning', '')}")

    # 4. Ejecutar decisión
    if DRY_RUN and paper:
        # ── Paper Trading: ejecutar con datos reales, sin dinero real ──
        paper.check_binance_stops(indicators["price"])
        paper.update_market_data(
            btc_price=indicators["price"],
            rsi=indicators["rsi"],
            trend=indicators["trend"],
            macd_cross=indicators.get("macd_cross", "neutral"),
            funding_rate=context.get("funding_rate", 0),
            vol_ratio=indicators.get("vol_ratio", 1),
        )

        if action == "HOLD":
            paper.add_log("HOLD — manteniendo posición actual")

        elif action == "FLAT":
            open_trade = paper.get_binance_position()
            if open_trade:
                paper.close_binance_position(indicators["price"], "SIGNAL")
            else:
                paper.add_log("FLAT — sin señal clara, esperando")

        elif action in ("LONG", "SHORT"):
            if confidence == "LOW":
                paper.add_log(f"LOW confidence — no entrando")
            else:
                open_trade = paper.get_binance_position()
                if open_trade and open_trade.side != action:
                    paper.close_binance_position(indicators["price"], "SIGNAL")
                if not paper.get_binance_position():
                    paper.open_binance_trade(decision, indicators["price"], capital, LEVERAGE)

        paper.save()
        log.info(f"  [PAPER] Capital: ${paper.state.current_capital:.2f} | P&L: {paper.state.total_pnl:+.2f} | Win: {paper.state.win_rate:.0f}%")

    else:
        # ── Trading real ──
        if action == "HOLD":
            log.info("Manteniendo posición actual.")
        elif action == "FLAT":
            if current_position != "FLAT":
                close_position(client, current_position, account["position_size"])
            else:
                log.info("Sin posición abierta. Mercado no favorable.")
        elif action in ("LONG", "SHORT"):
            if confidence == "LOW":
                log.info(f"Confianza LOW — no entrando al mercado.")
                return
            if current_position != "FLAT" and current_position != action:
                close_position(client, current_position, account["position_size"])
                time.sleep(1)
            if current_position == action:
                log.info(f"Ya estás en {action}. Manteniendo.")
            else:
                open_position(client, decision, capital, indicators["price"])


def run_forever():
    log.info(f"🤖 Binance BTC Futures Bot iniciado")
    log.info(f"   Modo: {'DRY RUN (simulación)' if DRY_RUN else '⚠️  REAL — cuidado con tu dinero'}")
    log.info(f"   Par: {SYMBOL} | Leverage: {LEVERAGE}x | Intervalo: {INTERVAL_MINUTES}min")

    client = get_binance_client()
    set_leverage(client)
    paper = get_binance_engine(initial_capital=500.0)

    while True:
        try:
            run_cycle(client, paper)
        except KeyboardInterrupt:
            log.info("\n🛑 Bot detenido por el usuario")
            break
        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)

        log.info(f"\n⏰ Próximo ciclo en {INTERVAL_MINUTES} minutos...")
        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        client = get_binance_client()
        set_leverage(client)
        run_cycle(client)
    else:
        run_forever()
