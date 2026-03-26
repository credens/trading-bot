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
INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "3"))  # analizar cada 3 min

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

from ai_client import call_ai, parse_json_response, is_available, get_model_info
log.info(f"AI Engine: {get_model_info()}")

# ─── Indicadores Técnicos ──────────────────────────────────────────────────────

def fetch_ohlcv(client, symbol: str = SYMBOL, interval: str = "15m", limit: int = 300) -> pd.DataFrame:
    """Trae velas OHLCV de Binance Futures."""
    klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume", "taker_buy_base", "taker_buy_quote"]:
        df[col] = df[col].astype(float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def fetch_macro_trend(client) -> dict:
    """
    Calcula la tendencia macro usando velas de 1h (200 velas = ~8 días).
    Retorna el lado permitido y métricas de contexto.

    Criterios (se necesitan al menos 2 de 3 para declarar tendencia):
      - EMA50 > EMA200 en 1h  → bullish
      - Precio > EMA50 en 1h  → bullish
      - MACD hist > 0 en 1h   → bullish
    """
    try:
        df = fetch_ohlcv(client, interval="1h", limit=220)
        close = df["close"]

        ema50_1h  = close.ewm(span=50,  adjust=False).mean()
        ema200_1h = close.ewm(span=200, adjust=False).mean()
        ema12_1h  = close.ewm(span=12,  adjust=False).mean()
        ema26_1h  = close.ewm(span=26,  adjust=False).mean()
        macd_1h   = ema12_1h - ema26_1h
        sig_1h    = macd_1h.ewm(span=9, adjust=False).mean()
        hist_1h   = macd_1h - sig_1h

        price     = float(close.iloc[-1])
        e50       = float(ema50_1h.iloc[-1])
        e200      = float(ema200_1h.iloc[-1])
        hist      = float(hist_1h.iloc[-1])

        bull_signals = sum([
            e50 > e200,          # estructura macro alcista
            price > e50,         # precio sobre media rápida
            hist > 0,            # momentum positivo
        ])
        bear_signals = sum([
            e50 < e200,
            price < e50,
            hist < 0,
        ])

        if bull_signals >= 2:
            macro = "bullish"
        elif bear_signals >= 2:
            macro = "bearish"
        else:
            macro = "neutral"

        log.info(f"  Macro 1h: EMA50={e50:,.0f} EMA200={e200:,.0f} | bull={bull_signals} bear={bear_signals} → {macro}")
        return {
            "macro_trend":   macro,
            "macro_bull":    bull_signals,
            "macro_bear":    bear_signals,
            "ema50_1h":      round(e50, 2),
            "ema200_1h":     round(e200, 2),
            "price_vs_ema50_1h": round((price - e50) / e50 * 100, 2),
        }
    except Exception as e:
        log.warning(f"Error calculando tendencia macro: {e}")
        return {"macro_trend": "neutral", "macro_bull": 0, "macro_bear": 0}


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


# ─── ESTRATEGIAS DE TRADING (3 independientes + votación) ─────────────────────
#
# Estrategia 1 — MACD Momentum (15m): captura cruces MACD + volumen + EMA
# Estrategia 2 — RSI + VWAP Reversal (5m): opera extremos RSI con VWAP
# Estrategia 3 — CVD Divergence (15m): detecta divergencias precio vs flujo
# Voto: necesitan ≥2/3 para entrar. Empate → FLAT.

# ── ANÁLISIS PROMPT LEGACY (no usado) ───────────────────────────────────────
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

REGLAS ESTRICTAS — SEGUIR AL PIE DE LA LETRA:
1. Si trend=bearish → PROHIBIDO abrir LONG. Solo SHORT o FLAT.
2. Si trend=bullish → PROHIBIDO abrir SHORT. Solo LONG o FLAT.
3. Si trend=neutral → podés ir en cualquier dirección pero con alta confluencia.
4. MACD cross bullish + trend bearish = señal contradictoria → FLAT
5. RSI<35 + trend bearish = rebote posible pero arriesgado → preferí FLAT
6. Solo entrá si la tendencia EMA Y al menos 2 indicadores más confirman la dirección
7. Con funding rate positivo alto (>0.03%) → favorece SHORT
8. stop_loss: 2-3% | take_profit: 5-8%
9. Si tenés dudas → FLAT. Preservar capital es prioridad.

Respondé ÚNICAMENTE con JSON válido:
{{
  "decision": "LONG|SHORT|FLAT|HOLD",
  "confidence": "HIGH|MEDIUM|LOW",
  "reasoning": "análisis de máximo 3 oraciones",
  "key_signals": ["señal1", "señal2", "señal3"],
  "entry_price": {price:.2f},
  "stop_loss_pct": 0.025,
  "take_profit_pct": 0.06,
  "position_size_pct": 0.10
}}

FLAT = no operar / cerrar posición si hay una abierta
HOLD = mantener posición actual sin cambios
stop_loss_pct: distancia del stop desde entrada (ej: 0.015 = 1.5%)
take_profit_pct: objetivo de ganancia (ej: 0.03 = 3%)
position_size_pct: % del capital a usar (máx 0.10 = 10%)
"""


def _analyze_legacy(indicators: dict, context: dict, capital: float, current_position: str) -> Optional[dict]:
    """LEGACY — reemplazada por run_strategies(). Se mantiene por referencia.
    Análisis técnico puro — sin LLM. Doble filtro de tendencia.

    LÓGICA EN CAPAS:
      1. FILTRO MACRO (bloqueante, 1h): macro_trend define el único lado
         permitido. Si macro es bearish → solo SHORT. Si bullish → solo LONG.
         Si neutral → requiere que el 15m sea claro, si no FLAT.

      2. CONFIRMACIÓN 15m: la EMA50/200 de 15m debe estar alineada con el
         macro. Si contradice → FLAT. Si confirma → continúa al score.

      3. SCORE DE CONFLUENCIA: MACD, RSI, BB, volumen, funding, cambio 24h.
         Mínimo score 2 para entrar (MEDIUM) o 3 para HIGH.

      4. GESTIÓN DE POSICIÓN EXISTENTE.
    """
    rsi            = indicators.get("rsi", 50)
    bb_pct         = indicators.get("bb_pct", 0.5)
    macd_hist      = indicators.get("macd_hist", 0)
    macd_cross     = indicators.get("macd_cross", "neutral")
    vol_ratio      = indicators.get("vol_ratio", 1)
    atr_pct        = indicators.get("atr_pct", 1)
    trend_15m      = indicators.get("trend", "neutral")       # EMA50/200 en 15m (ahora con 300 velas = válida)
    macro_trend    = indicators.get("macro_trend", "neutral") # EMA50/200 en 1h (fuente de verdad macro)
    funding        = context.get("funding_rate", 0)
    change_24h     = context.get("change_24h_pct", 0)
    price          = indicators.get("price", 0)
    price_vs_ema50 = indicators.get("price_vs_ema50", 0)

    signals = []

    # ── CAPA 1: FILTRO MACRO 1h (fuente de verdad) ────────────────────────────
    # La tendencia de 1h manda. Si no hay tendencia clara en 1h → FLAT.
    if macro_trend == "bullish":
        allowed_side = "LONG"
        signals.append(f"Macro 1h: BULLISH → solo LONG")
    elif macro_trend == "bearish":
        allowed_side = "SHORT"
        signals.append(f"Macro 1h: BEARISH → solo SHORT")
    else:
        # Macro neutral: solo operar si el 15m es muy claro (ambas EMAs alineadas)
        if trend_15m == "bullish":
            allowed_side = "LONG"
            signals.append("Macro neutral + 15m bullish → LONG con cautela")
        elif trend_15m == "bearish":
            allowed_side = "SHORT"
            signals.append("Macro neutral + 15m bearish → SHORT con cautela")
        else:
            log.info("  Técnico: macro neutral + 15m neutral → FLAT")
            return _make_result("FLAT", "MEDIUM", "Sin tendencia en ningún timeframe", signals, price, atr_pct)

    # ── CAPA 2: CONFIRMACIÓN 15m ──────────────────────────────────────────────
    # El 15m no puede contradecir al macro. Si lo contradice → FLAT.
    # Si el 15m confirma → bonus en el score. Si es neutral → pasa igual.
    trend_15m_confirms = (
        (allowed_side == "LONG"  and trend_15m == "bullish") or
        (allowed_side == "SHORT" and trend_15m == "bearish")
    )
    trend_15m_contradicts = (
        (allowed_side == "LONG"  and trend_15m == "bearish") or
        (allowed_side == "SHORT" and trend_15m == "bullish")
    )

    if trend_15m_contradicts:
        log.info(f"  Técnico: 15m {trend_15m} contradice macro {macro_trend} → FLAT")
        # Si hay posición abierta contra la macro, cerrar
        if current_position != "FLAT" and current_position != allowed_side:
            return _make_result("FLAT", "HIGH",
                                f"15m contradice macro — cerrar {current_position}",
                                signals, price, atr_pct)
        return _make_result("FLAT", "MEDIUM",
                            f"15m {trend_15m} vs macro {macro_trend} — esperando alineación",
                            signals, price, atr_pct)

    # ── CAPA 3: SCORE DE CONFLUENCIA ─────────────────────────────────────────
    score = 0

    # Bonus si el 15m confirma al macro
    if trend_15m_confirms:
        score += 1
        signals.append(f"15m {trend_15m} confirma macro ✓")

    # MACD — timing de entrada
    if allowed_side == "LONG":
        if macd_cross == "bullish":
            score += 3; signals.append("MACD bullish cross ✓")
        elif macd_hist > 0:
            score += 1; signals.append(f"MACD hist positivo ({macd_hist:.1f})")
        elif macd_cross == "bearish" or macd_hist < 0:
            score -= 1; signals.append("MACD en contra")
    else:  # SHORT
        if macd_cross == "bearish":
            score += 3; signals.append("MACD bearish cross ✓")
        elif macd_hist < 0:
            score += 1; signals.append(f"MACD hist negativo ({macd_hist:.1f})")
        elif macd_cross == "bullish" or macd_hist > 0:
            score -= 1; signals.append("MACD en contra")

    # RSI — zona óptima (momentum, no rebote)
    if allowed_side == "LONG":
        if 40 <= rsi <= 55:
            score += 2; signals.append(f"RSI zona óptima long ({rsi:.0f})")
        elif rsi < 40:
            score += 1; signals.append(f"RSI bajo ({rsi:.0f})")
        elif rsi > 70:
            score -= 2; signals.append(f"RSI sobrecomprado ({rsi:.0f})")
        elif rsi > 60:
            score -= 1; signals.append(f"RSI elevado ({rsi:.0f})")
    else:  # SHORT
        if 45 <= rsi <= 60:
            score += 2; signals.append(f"RSI zona óptima short ({rsi:.0f})")
        elif rsi > 60:
            score += 1; signals.append(f"RSI alto ({rsi:.0f})")
        elif rsi < 30:
            score -= 2; signals.append(f"RSI sobrevendido ({rsi:.0f}) — riesgo rebote")
        elif rsi < 40:
            score -= 1; signals.append(f"RSI bajo ({rsi:.0f})")

    # Bollinger Bands — timing de entrada
    if allowed_side == "LONG":
        if bb_pct < 0.2:
            score += 2; signals.append(f"BB lower band ({bb_pct:.0%})")
        elif 0.2 <= bb_pct <= 0.5:
            score += 1; signals.append(f"BB zona media-baja ({bb_pct:.0%})")
        elif bb_pct > 0.85:
            score -= 2; signals.append(f"BB upper band ({bb_pct:.0%})")
    else:  # SHORT
        if bb_pct > 0.8:
            score += 2; signals.append(f"BB upper band ({bb_pct:.0%})")
        elif 0.5 <= bb_pct <= 0.8:
            score += 1; signals.append(f"BB zona media-alta ({bb_pct:.0%})")
        elif bb_pct < 0.15:
            score -= 2; signals.append(f"BB lower band ({bb_pct:.0%})")

    # Precio vs EMA50 15m — no cazar movimientos extendidos
    if allowed_side == "LONG" and price_vs_ema50 > 3:
        score -= 1; signals.append(f"Precio alejado de EMA50 (+{price_vs_ema50:.1f}%)")
    elif allowed_side == "SHORT" and price_vs_ema50 < -3:
        score -= 1; signals.append(f"Precio alejado de EMA50 ({price_vs_ema50:.1f}%)")

    # Volumen
    if vol_ratio > 1.8:
        score += 1; signals.append(f"Volumen alto ({vol_ratio:.1f}x)")

    # Funding rate
    if allowed_side == "LONG"  and funding < -0.02:
        score += 1; signals.append(f"Funding negativo ({funding:.4f}%)")
    elif allowed_side == "SHORT" and funding > 0.02:
        score += 1; signals.append(f"Funding positivo ({funding:.4f}%)")

    # Cambio 24h
    if allowed_side == "LONG"  and change_24h > 1.5:
        score += 1; signals.append(f"Momentum 24h +{change_24h:.1f}%")
    elif allowed_side == "SHORT" and change_24h < -1.5:
        score += 1; signals.append(f"Momentum 24h {change_24h:.1f}%")

    log.info(f"  Técnico: macro={macro_trend} 15m={trend_15m} score={score:+d} lado={allowed_side}")

    # ── CAPA 4: GESTIÓN DE POSICIÓN ACTUAL ────────────────────────────────────
    sl_pct = round(max(atr_pct * 1.5 / 100, 0.02), 4)
    tp_pct = round(sl_pct * 2.5, 4)

    if current_position == allowed_side:
        if score >= 2:
            return _make_result("HOLD", "MEDIUM",
                                f"Manteniendo {allowed_side} (score {score:+d})",
                                signals, price, atr_pct)
        else:
            return _make_result("FLAT", "MEDIUM",
                                f"Señal debilitada (score {score:+d}) → cerrando {allowed_side}",
                                signals, price, atr_pct)

    if current_position != "FLAT" and current_position != allowed_side:
        return _make_result("FLAT", "HIGH",
                            f"Posición {current_position} contra macro {macro_trend} → cerrar",
                            signals, price, atr_pct)

    # ── NUEVA ENTRADA ─────────────────────────────────────────────────────────
    if score >= 3:
        decision, confidence = allowed_side, "HIGH"
    elif score >= 2:
        decision, confidence = allowed_side, "MEDIUM"
    else:
        decision, confidence = "FLAT", "MEDIUM"

    reasoning = f"Macro {macro_trend} | 15m {trend_15m} | Score {score:+d}"
    log.info(f"  → {decision} | {confidence}")

    return {
        "decision":          decision,
        "confidence":        confidence,
        "reasoning":         reasoning,
        "key_signals":       signals,
        "entry_price":       price,
        "stop_loss_pct":     sl_pct,
        "take_profit_pct":   tp_pct,
        "position_size_pct": 0.10 if confidence == "HIGH" else 0.06,
    }


def _make_result(decision: str, confidence: str, reasoning: str,
                 signals: list, price: float, atr_pct: float) -> dict:
    sl_pct = round(max(atr_pct * 1.5 / 100, 0.02), 4)
    tp_pct = round(sl_pct * 2.5, 4)
    return {
        "decision": decision, "confidence": confidence, "reasoning": reasoning,
        "key_signals": signals, "entry_price": price,
        "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
        "position_size_pct": 0.10 if confidence == "HIGH" else 0.06,
    }


def _strat_result(decision, confidence, reasoning, signals, price, atr_pct, name):
    sl_pct = round(max(atr_pct * 1.5 / 100, 0.018), 4)
    tp_pct = round(sl_pct * 2.5, 4)
    log.info(f"  [{name}] → {decision} ({confidence}) | {reasoning}")
    return {
        "decision": decision, "confidence": confidence,
        "reasoning": f"[{name}] {reasoning}", "key_signals": signals,
        "entry_price": price, "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
        "position_size_pct": 0.10 if confidence == "HIGH" else 0.06,
        "_strategy": name,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA 1 — MACD MOMENTUM (15m)
# Entra en cruces MACD reales con volumen + precio del lado correcto de EMA50
# ══════════════════════════════════════════════════════════════════════════════
def strategy_macd_momentum(client, current_position: str, capital: float) -> dict:
    df = fetch_ohlcv(client, interval="15m", limit=150)
    price = float(df["close"].iloc[-1])
    close = df["close"]; volume = df["volume"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26; signal = macd.ewm(span=9, adjust=False).mean(); hist = macd - signal
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    vol_sma = volume.rolling(20).mean()
    vol_ratio = float(volume.iloc[-1] / vol_sma.iloc[-1])
    tr = pd.concat([df["high"]-df["low"], (df["high"]-close.shift()).abs(), (df["low"]-close.shift()).abs()], axis=1).max(axis=1)
    atr_pct = float(tr.rolling(14).mean().iloc[-1] / price * 100)
    hist_now = float(hist.iloc[-1]); hist_prev = float(hist.iloc[-2])
    ema50_val = float(ema50.iloc[-1]); ema200_val = float(ema200.iloc[-1])
    trend = "bullish" if ema50_val > ema200_val else "bearish"
    bullish_cross = (hist_now > 0 and hist_prev <= 0) or (float(hist.iloc[-2]) > 0 and float(hist.iloc[-3]) <= 0)
    bearish_cross = (hist_now < 0 and hist_prev >= 0) or (float(hist.iloc[-2]) < 0 and float(hist.iloc[-3]) >= 0)
    name = "MACD_MOM"
    if current_position == "LONG":
        if hist_now < 0 or price < ema50_val * 0.998:
            return _strat_result("FLAT", "HIGH", "MACD neg o precio bajo EMA50 → cierre", ["Salida MACD"], price, atr_pct, name)
        return _strat_result("HOLD", "MEDIUM", "LONG activo, MACD positivo", ["Manteniendo long"], price, atr_pct, name)
    if current_position == "SHORT":
        if hist_now > 0 or price > ema50_val * 1.002:
            return _strat_result("FLAT", "HIGH", "MACD pos o precio sobre EMA50 → cierre", ["Salida MACD"], price, atr_pct, name)
        return _strat_result("HOLD", "MEDIUM", "SHORT activo, MACD negativo", ["Manteniendo short"], price, atr_pct, name)
    if bullish_cross and trend == "bullish" and price > ema50_val:
        signals = ["MACD bullish cross ✓", "Trend EMA bullish ✓"]
        score = sum([vol_ratio > 1.8, price > ema50_val * 1.001, atr_pct < 1.5])
        if vol_ratio > 1.8: signals.append(f"Vol {vol_ratio:.1f}x ✓")
        return _strat_result("LONG", "HIGH" if score >= 2 else "MEDIUM",
                             f"Bullish cross+vol {vol_ratio:.1f}x+trend {trend}", signals, price, atr_pct, name)
    if bearish_cross and trend == "bearish" and price < ema50_val:
        signals = ["MACD bearish cross ✓", "Trend EMA bearish ✓"]
        score = sum([vol_ratio > 1.8, price < ema50_val * 0.999, atr_pct < 1.5])
        if vol_ratio > 1.8: signals.append(f"Vol {vol_ratio:.1f}x ✓")
        return _strat_result("SHORT", "HIGH" if score >= 2 else "MEDIUM",
                             f"Bearish cross+vol {vol_ratio:.1f}x+trend {trend}", signals, price, atr_pct, name)
    return _strat_result("FLAT", "MEDIUM", f"Sin cruce MACD válido (hist={hist_now:.1f}, trend={trend})", ["Esperando cruce"], price, atr_pct, name)


# ══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA 2 — RSI + VWAP REVERSAL (5m)
# Opera contra extremos RSI cuando el precio se desvió del VWAP
# ══════════════════════════════════════════════════════════════════════════════
def strategy_rsi_vwap(client, current_position: str, capital: float) -> dict:
    df = fetch_ohlcv(client, interval="5m", limit=200)
    price = float(df["close"].iloc[-1])
    close = df["close"]; high = df["high"]; low = df["low"]; volume = df["volume"]
    name = "RSI_VWAP"
    delta = close.diff(); gain = delta.clip(lower=0).rolling(14).mean(); loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi_s = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    rsi = float(rsi_s.iloc[-1]); rsi_prev = float(rsi_s.iloc[-2])
    typical = (high + low + close) / 3
    vwap = float((typical * volume).cumsum().iloc[-1] / volume.cumsum().iloc[-1])
    vwap_dev = (price - vwap) / vwap * 100
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    atr_pct = float(tr.rolling(14).mean().iloc[-1] / price * 100)
    sma20 = close.rolling(20).mean(); std20 = close.rolling(20).std()
    bb_pct = float(((close - (sma20 - 2*std20)) / (4*std20)).iloc[-1])
    if current_position == "LONG":
        if rsi > 55 or price >= vwap * 1.003:
            return _strat_result("FLAT", "HIGH", f"Reversión completada (RSI {rsi:.0f})", ["Objetivo alcanzado"], price, atr_pct, name)
        return _strat_result("HOLD", "MEDIUM", f"LONG reversión en curso RSI {rsi:.0f}", [f"RSI {rsi:.0f}"], price, atr_pct, name)
    if current_position == "SHORT":
        if rsi < 45 or price <= vwap * 0.997:
            return _strat_result("FLAT", "HIGH", f"Reversión completada (RSI {rsi:.0f})", ["Objetivo alcanzado"], price, atr_pct, name)
        return _strat_result("HOLD", "MEDIUM", f"SHORT reversión en curso RSI {rsi:.0f}", [f"RSI {rsi:.0f}"], price, atr_pct, name)
    if rsi < 32 and vwap_dev < -0.6:
        signals = [f"RSI sobrevendido ({rsi:.0f})", f"Dev VWAP {vwap_dev:.2f}%"]
        score = sum([rsi > rsi_prev, bb_pct < 0.1, rsi < 25])
        if rsi > rsi_prev: signals.append("RSI girando arriba ✓")
        return _strat_result("LONG", "HIGH" if score >= 2 else "MEDIUM", f"RSI {rsi:.0f}+VWAP {vwap_dev:.2f}%", signals, price, atr_pct, name)
    if rsi > 68 and vwap_dev > 0.6:
        signals = [f"RSI sobrecomprado ({rsi:.0f})", f"Dev VWAP {vwap_dev:.2f}%"]
        score = sum([rsi < rsi_prev, bb_pct > 0.9, rsi > 75])
        if rsi < rsi_prev: signals.append("RSI girando abajo ✓")
        return _strat_result("SHORT", "HIGH" if score >= 2 else "MEDIUM", f"RSI {rsi:.0f}+VWAP {vwap_dev:.2f}%", signals, price, atr_pct, name)
    return _strat_result("FLAT", "MEDIUM", f"Sin extremo (RSI {rsi:.0f}, VWAP dev {vwap_dev:+.2f}%)", ["Esperando extremo RSI+VWAP"], price, atr_pct, name)


# ══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA 3 — CVD DIVERGENCE (15m)
# Divergencia entre precio y flujo neto de dinero (taker buy vs sell)
# ══════════════════════════════════════════════════════════════════════════════
def strategy_cvd_divergence(client, current_position: str, capital: float) -> dict:
    df = fetch_ohlcv(client, interval="15m", limit=150)
    price = float(df["close"].iloc[-1])
    close = df["close"]; volume = df["volume"]
    name = "CVD_DIV"
    taker_sell = volume - df["taker_buy_base"]
    cvd = (df["taker_buy_base"] - taker_sell).cumsum().ewm(span=5, adjust=False).mean()
    tr = pd.concat([df["high"]-df["low"], (df["high"]-close.shift()).abs(), (df["low"]-close.shift()).abs()], axis=1).max(axis=1)
    atr_pct = float(tr.rolling(14).mean().iloc[-1] / price * 100)
    N = 5
    price_chg = float(close.iloc[-1] - close.iloc[-N]) / float(close.iloc[-N]) * 100
    cvd_chg = float(cvd.iloc[-1] - cvd.iloc[-N])
    price_dir = "up" if price_chg > 0 else "down"
    cvd_dir = "up" if cvd_chg > 0 else "down"
    price_moved = abs(price_chg) > 0.4
    cvd_moved = abs(cvd_chg) > (volume.iloc[-N:].mean() * 0.05)
    ema50 = close.ewm(span=50, adjust=False).mean(); ema200 = close.ewm(span=200, adjust=False).mean()
    macro = "bullish" if float(ema50.iloc[-1]) > float(ema200.iloc[-1]) else "bearish"
    delta = close.diff(); gain = delta.clip(lower=0).rolling(14).mean(); loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = float((100 - (100 / (1 + gain / loss.replace(0, np.nan)))).iloc[-1])
    if current_position == "LONG":
        if cvd_dir == "down" and price_dir == "down":
            return _strat_result("FLAT", "HIGH", "CVD y precio a la baja → cierre LONG", ["Divergencia resuelta"], price, atr_pct, name)
        return _strat_result("HOLD", "MEDIUM", f"LONG activo CVD:{cvd_dir}", [f"CVD {cvd_dir}"], price, atr_pct, name)
    if current_position == "SHORT":
        if cvd_dir == "up" and price_dir == "up":
            return _strat_result("FLAT", "HIGH", "CVD y precio al alza → cierre SHORT", ["Divergencia resuelta"], price, atr_pct, name)
        return _strat_result("HOLD", "MEDIUM", f"SHORT activo CVD:{cvd_dir}", [f"CVD {cvd_dir}"], price, atr_pct, name)
    if price_dir == "up" and cvd_dir == "down" and price_moved and cvd_moved and rsi < 72:
        signals = [f"Precio +{price_chg:.2f}% pero CVD bajando", "Dinero agresivo vendiendo"]
        score = sum([macro == "bearish", rsi > 60, abs(cvd_chg) > volume.iloc[-N:].mean() * 0.15])
        if macro == "bearish": signals.append("Macro bearish confirma ✓")
        return _strat_result("SHORT", "HIGH" if score >= 2 else "MEDIUM",
                             f"Bearish CVD div | precio {price_chg:+.2f}% | CVD ↓", signals, price, atr_pct, name)
    if price_dir == "down" and cvd_dir == "up" and price_moved and cvd_moved and rsi > 28:
        signals = [f"Precio {price_chg:.2f}% pero CVD subiendo", "Dinero agresivo comprando"]
        score = sum([macro == "bullish", rsi < 40, abs(cvd_chg) > volume.iloc[-N:].mean() * 0.15])
        if macro == "bullish": signals.append("Macro bullish confirma ✓")
        return _strat_result("LONG", "HIGH" if score >= 2 else "MEDIUM",
                             f"Bullish CVD div | precio {price_chg:+.2f}% | CVD ↑", signals, price, atr_pct, name)
    return _strat_result("FLAT", "MEDIUM", f"Sin divergencia (precio {price_dir} {price_chg:+.2f}%, CVD {cvd_dir})", ["Esperando divergencia"], price, atr_pct, name)


# ══════════════════════════════════════════════════════════════════════════════
# SISTEMA DE VOTACIÓN — necesita ≥2/3 para entrar
# ══════════════════════════════════════════════════════════════════════════════
def run_strategies(client, current_position: str, capital: float) -> dict:
    """Ejecuta las 3 estrategias y decide por votación (≥2/3)."""
    r1 = strategy_macd_momentum(client, current_position, capital)
    r2 = strategy_rsi_vwap(client, current_position, capital)
    r3 = strategy_cvd_divergence(client, current_position, capital)

    votes = {"LONG": 0, "SHORT": 0, "FLAT": 0, "HOLD": 0}
    for r in [r1, r2, r3]:
        votes[r.get("decision", "FLAT")] = votes.get(r.get("decision", "FLAT"), 0) + 1
    votes["FLAT"] += votes.pop("HOLD", 0)
    long_v = votes["LONG"]; short_v = votes["SHORT"]
    log.info(f"  VOTE → LONG:{long_v} SHORT:{short_v} FLAT:{votes['FLAT']}")
    for r in [r1, r2, r3]:
        log.info(f"    {r['_strategy']}: {r['decision']} — {r['reasoning']}")

    price = r1["entry_price"]
    sl_pct = max(r["stop_loss_pct"] for r in [r1, r2, r3])
    tp_pct = max(r["take_profit_pct"] for r in [r1, r2, r3])
    all_signals = []
    for r in [r1, r2, r3]:
        all_signals.extend(r.get("key_signals", [])[:2])

    if long_v >= 2:
        conf = "HIGH" if long_v == 3 else "MEDIUM"
        return {"decision": "LONG", "confidence": conf,
                "reasoning": f"Voto {long_v}/3 LONG", "key_signals": all_signals,
                "entry_price": price, "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
                "position_size_pct": 0.10 if conf == "HIGH" else 0.06}
    if short_v >= 2:
        conf = "HIGH" if short_v == 3 else "MEDIUM"
        return {"decision": "SHORT", "confidence": conf,
                "reasoning": f"Voto {short_v}/3 SHORT", "key_signals": all_signals,
                "entry_price": price, "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
                "position_size_pct": 0.10 if conf == "HIGH" else 0.06}
    # Mayoría HOLD — mantener posición
    hold_rs = [r for r in [r1, r2, r3] if r["decision"] == "HOLD"]
    if len(hold_rs) >= 2:
        return {**hold_rs[0], "decision": "HOLD", "confidence": "MEDIUM", "reasoning": "Mayoría HOLD"}
    return {"decision": "FLAT", "confidence": "MEDIUM",
            "reasoning": f"Sin mayoría (L:{long_v} S:{short_v} F:{votes['FLAT']}) → esperando",
            "key_signals": ["Señales divididas"], "entry_price": price,
            "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct, "position_size_pct": 0.06}


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

    # 0. Verificar cierre manual
    if DRY_RUN and paper:
        from paper_trading import BINANCE_STATE
        import json as _json
        try:
            raw = _json.loads(BINANCE_STATE.read_text()) if BINANCE_STATE.exists() else {}
            if raw.get("manual_close"):
                log.info("🛑 CIERRE MANUAL solicitado desde dashboard")
                open_trade = paper.get_binance_position()
                if open_trade:
                    # Obtener precio actual para cierre
                    _df = fetch_ohlcv(get_binance_client())
                    _price = float(_df["close"].iloc[-1])
                    paper.close_binance_position(_price, "MANUAL")
                    log.info(f"  ✅ Posición cerrada manualmente a ${_price:,.2f}")
                # Cooldown: no re-abrir por 2 horas
                from datetime import timedelta
                raw["manual_close"] = False
                raw["cooldown_until"] = (datetime.now() + timedelta(minutes=3)).isoformat()
                BINANCE_STATE.write_text(_json.dumps(raw, indent=2))
        except Exception as e:
            log.warning(f"Error en cierre manual: {e}")

    # 1. Info de cuenta
    if DRY_RUN and paper:
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

    # Tendencia macro en 1h — fuente de verdad para la dirección
    macro = fetch_macro_trend(client)
    indicators.update(macro)  # agrega macro_trend, ema50_1h, ema200_1h, etc.

    log.info(f"BTC: ${indicators['price']:,.2f} | RSI: {indicators['rsi']} | 15m: {indicators['trend']} | Macro 1h: {indicators['macro_trend']} | Vol: {indicators['vol_ratio']:.1f}x")

    # 3. Estrategias votan (≥2/3 para entrar)
    log.info("Evaluando estrategias...")
    decision = run_strategies(client, current_position, capital)

    if not decision:
        log.warning("No se pudo obtener decisión. Saltando ciclo.")
        return

    action = decision.get("decision", "FLAT")
    confidence = decision.get("confidence", "LOW")

    # ── MACRO OVERRIDE: veto dirección contraria al macro 1h ──────────────────
    macro = indicators.get("macro_trend", "neutral")
    if action == "LONG" and macro == "bearish":
        log.warning(f"  🚫 LONG vetado — macro 1h BEARISH ({indicators.get('macro_bear',0)} señales bajistas)")
        action = "FLAT"
        decision["decision"] = "FLAT"
        decision["reasoning"] = f"LONG vetado por macro bearish 1h"
    elif action == "SHORT" and macro == "bullish":
        log.warning(f"  🚫 SHORT vetado — macro 1h BULLISH ({indicators.get('macro_bull',0)} señales alcistas)")
        action = "FLAT"
        decision["decision"] = "FLAT"
        decision["reasoning"] = f"SHORT vetado por macro bullish 1h"

    log.info(f"Decisión: {action} | Confianza: {confidence}")
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
                # Verificar cooldown post cierre manual y circuit breaker por dirección
                try:
                    raw = _json.loads(BINANCE_STATE.read_text()) if BINANCE_STATE.exists() else {}
                    cooldown = raw.get("cooldown_until")
                    if cooldown and datetime.fromisoformat(cooldown) > datetime.now():
                        log.info(f"  Cooldown activo hasta {cooldown[:16]} — no re-abriendo")
                        paper.add_log(f"Cooldown activo — esperando para re-abrir")
                    else:
                        # Circuit breaker por dirección: bloquear si >= 2 SLs consecutivos en la misma dirección
                        blocked_key = f"blocked_{action.lower()}_until"
                        blocked_until = raw.get(blocked_key)
                        if blocked_until and datetime.fromisoformat(blocked_until) > datetime.now():
                            log.info(f"  Circuit breaker {action}: bloqueado hasta {blocked_until[:16]}")
                            paper.add_log(f"Circuit breaker {action} — esperando enfriamiento")
                        else:
                            # Contar SLs consecutivos en esta dirección en últimas 2h
                            from datetime import timedelta
                            cutoff = datetime.now() - timedelta(hours=2)
                            recent = [t for t in paper.state.closed_trades
                                      if t.exit_time and datetime.fromisoformat(t.exit_time[:19]) > cutoff]
                            # Consecutivos desde el último trade (mismo side, STOP_LOSS)
                            consec_sl = 0
                            for t in reversed(recent):
                                if t.side == action and t.exit_reason == "STOP_LOSS":
                                    consec_sl += 1
                                else:
                                    break
                            total_sl_dir = sum(1 for t in recent if t.side == action and t.exit_reason == "STOP_LOSS")
                            if consec_sl >= 2 or total_sl_dir >= 3:
                                pause_min = 120 if total_sl_dir >= 3 else 45  # 2h o 45min
                                pause_until = (datetime.now() + timedelta(minutes=pause_min)).isoformat()
                                raw[blocked_key] = pause_until
                                BINANCE_STATE.write_text(_json.dumps(raw, indent=2))
                                log.warning(f"  ⛔ Circuit breaker {action}: {consec_sl} SLs consecutivos / {total_sl_dir} en 2h → bloqueado {pause_min}min")
                                paper.add_log(f"Circuit breaker {action} activado — {pause_min}min de pausa")
                            else:
                                open_trade = paper.get_binance_position()
                                if open_trade and open_trade.side != action:
                                    paper.close_binance_position(indicators["price"], "SIGNAL")
                                if not paper.get_binance_position():
                                    decision["_trend"] = indicators.get("trend", "neutral")
                                    paper.open_binance_trade(decision, indicators["price"], capital, LEVERAGE)
                except Exception:
                    open_trade = paper.get_binance_position()
                    if open_trade and open_trade.side != action:
                        paper.close_binance_position(indicators["price"], "SIGNAL")
                    if not paper.get_binance_position():
                        # Pasar trend en decision para guardarlo
                        decision["_trend"] = indicators.get("trend", "neutral")
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