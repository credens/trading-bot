"""
Trading2 — BTC Futures Bot con 3 Estrategias Independientes
============================================================
Tres estrategias autónomas. Cada una vota. El voto mayoritario decide.

  ESTRATEGIA 1 — MACD Momentum (15m)
    Captura el inicio de movimientos fuertes. Entra cuando el volumen
    confirma el cruce MACD y el precio rompe la EMA50. No persigue,
    espera el pullback al cruce.

  ESTRATEGIA 2 — RSI + VWAP Reversal (5m)
    Opera contra el pánico. Espera RSI extremo + precio desviado del
    VWAP. Apuesta a que el mercado vuelve al precio justo.

  ESTRATEGIA 3 — CVD Divergence (15m)
    Sigue el dinero, no el precio. Si el precio sube pero el dinero
    neto vende (CVD baja), es trampa — va short. Y viceversa.

SETUP:
  pip install python-binance python-dotenv pandas numpy

CONFIGURAR .env:
  BINANCE_API_KEY=...
  BINANCE_SECRET_KEY=...
  LEVERAGE=3
  MAX_RISK_PCT=0.02
  DRY_RUN=true
  INTERVAL_MINUTES=5
  ACTIVE_STRATEGY=VOTE    # MACD | RSI_VWAP | CVD | VOTE
"""

import os
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

SYMBOL           = "BTCUSDT"
LEVERAGE         = int(os.getenv("LEVERAGE", "3"))
MAX_RISK_PCT     = float(os.getenv("MAX_RISK_PCT", "0.02"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.10"))
DRY_RUN          = os.getenv("DRY_RUN", "true").lower() == "true"
INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "5"))
ACTIVE_STRATEGY  = os.getenv("ACTIVE_STRATEGY", "VOTE").upper()  # MACD | RSI_VWAP | CVD | VOTE

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")


# ─── Cliente ──────────────────────────────────────────────────────────────────

def get_binance_client():
    from binance.client import Client
    return Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)


# ─── Datos de Mercado ─────────────────────────────────────────────────────────

def fetch_ohlcv(client, symbol: str = SYMBOL, interval: str = "15m", limit: int = 150) -> pd.DataFrame:
    """Trae velas OHLCV de Binance Futures."""
    klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["taker_buy_base"]  = df["taker_buy_base"].astype(float)
    df["taker_buy_quote"] = df["taker_buy_quote"].astype(float)
    df["timestamp"]       = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def get_market_context(client) -> dict:
    """Funding rate, open interest, cambio 24h."""
    try:
        ticker  = client.futures_ticker(symbol=SYMBOL)
        funding = client.futures_funding_rate(symbol=SYMBOL, limit=1)
        oi      = client.futures_open_interest(symbol=SYMBOL)
        return {
            "change_24h_pct":     round(float(ticker.get("priceChangePercent", 0)), 2),
            "volume_24h":         round(float(ticker.get("quoteVolume", 0)) / 1e6, 1),
            "funding_rate":       round(float(funding[0]["fundingRate"]) * 100, 4) if funding else 0,
            "open_interest_usdt": round(float(oi.get("openInterest", 0)) * float(ticker.get("lastPrice", 0)) / 1e9, 2),
        }
    except Exception as e:
        log.warning(f"Error en contexto de mercado: {e}")
        return {"change_24h_pct": 0, "volume_24h": 0, "funding_rate": 0, "open_interest_usdt": 0}


# ─── Helpers de resultado ─────────────────────────────────────────────────────

def _result(decision: str, confidence: str, reasoning: str,
            signals: list, price: float, atr_pct: float,
            strategy_name: str) -> dict:
    sl_pct = round(max(atr_pct * 1.5 / 100, 0.018), 4)
    tp_pct = round(sl_pct * 2.5, 4)
    log.info(f"  [{strategy_name}] → {decision} ({confidence}) | {reasoning}")
    return {
        "decision":        decision,
        "confidence":      confidence,
        "reasoning":       f"[{strategy_name}] {reasoning}",
        "key_signals":     signals,
        "entry_price":     price,
        "stop_loss_pct":   sl_pct,
        "take_profit_pct": tp_pct,
        "position_size_pct": 0.10 if confidence == "HIGH" else 0.06,
        "_strategy":       strategy_name,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA 1 — MACD MOMENTUM (15m)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Filosofía: los movimientos fuertes empiezan con el cruce MACD + volumen.
# No entra en cualquier cruce — exige:
#   - Cruce MACD reciente (últimas 2 velas)
#   - Volumen del cruce > 1.5x promedio (dinero real detrás)
#   - Precio del lado correcto de EMA50 (tendencia acompaña)
#   - ATR razonable (no entrar en mercado muerto ni en explosión de volatilidad)
#
# SL: debajo/encima de EMA50 (nivel de invalidación natural)
# TP: 2.5x el SL (ratio mínimo)

def strategy_macd_momentum(client, current_position: str, capital: float) -> dict:
    """MACD Momentum — captura el inicio de movimientos con confirmación de volumen."""
    df  = fetch_ohlcv(client, interval="15m", limit=150)
    price = float(df["close"].iloc[-1])

    # Indicadores
    close    = df["close"]
    volume   = df["volume"]
    ema12    = close.ewm(span=12, adjust=False).mean()
    ema26    = close.ewm(span=26, adjust=False).mean()
    macd     = ema12 - ema26
    signal   = macd.ewm(span=9, adjust=False).mean()
    hist     = macd - signal
    ema50    = close.ewm(span=50, adjust=False).mean()
    ema200   = close.ewm(span=200, adjust=False).mean()
    vol_sma  = volume.rolling(20).mean()
    vol_ratio = float(volume.iloc[-1] / vol_sma.iloc[-1])

    # ATR
    tr   = pd.concat([df["high"] - df["low"],
                       (df["high"] - close.shift()).abs(),
                       (df["low"]  - close.shift()).abs()], axis=1).max(axis=1)
    atr     = tr.rolling(14).mean()
    atr_pct = float(atr.iloc[-1] / price * 100)

    # Estado actual
    hist_now  = float(hist.iloc[-1])
    hist_prev = float(hist.iloc[-2])
    ema50_val = float(ema50.iloc[-1])
    ema200_val= float(ema200.iloc[-1])
    trend     = "bullish" if ema50_val > ema200_val else "bearish"

    # Cruce MACD (última o antepenúltima vela para no llegar tarde)
    bullish_cross = (hist_now > 0 and hist_prev <= 0) or (float(hist.iloc[-2]) > 0 and float(hist.iloc[-3]) <= 0)
    bearish_cross = (hist_now < 0 and hist_prev >= 0) or (float(hist.iloc[-2]) < 0 and float(hist.iloc[-3]) >= 0)

    signals = []
    name    = "MACD_MOMENTUM"

    # ── Gestión de posición existente ──
    if current_position == "LONG":
        # Cerrar si MACD cruza negativo o precio cae bajo EMA50
        if hist_now < 0 or price < ema50_val * 0.998:
            return _result("FLAT", "HIGH", "MACD negativo o precio bajo EMA50 → cierre",
                           ["Salida MACD momentum"], price, atr_pct, name)
        return _result("HOLD", "MEDIUM", "LONG activo, MACD aún positivo",
                       ["Manteniendo momentum long"], price, atr_pct, name)

    if current_position == "SHORT":
        if hist_now > 0 or price > ema50_val * 1.002:
            return _result("FLAT", "HIGH", "MACD positivo o precio sobre EMA50 → cierre",
                           ["Salida MACD momentum"], price, atr_pct, name)
        return _result("HOLD", "MEDIUM", "SHORT activo, MACD aún negativo",
                       ["Manteniendo momentum short"], price, atr_pct, name)

    # ── Condiciones de entrada LONG ──
    if bullish_cross and trend == "bullish" and price > ema50_val:
        signals.append(f"MACD bullish cross ✓")
        signals.append(f"Tendencia EMA bullish ✓")
        score = 0
        if vol_ratio > 1.8:
            score += 1; signals.append(f"Volumen alto ({vol_ratio:.1f}x) ✓")
        if price > ema50_val * 1.001:
            score += 1; signals.append(f"Precio sobre EMA50 ✓")
        if atr_pct < 1.5:
            score += 1; signals.append(f"ATR controlado ({atr_pct:.2f}%)")

        confidence = "HIGH" if score >= 2 else "MEDIUM" if score >= 1 else "LOW"
        return _result("LONG", confidence,
                       f"Bullish cross + vol {vol_ratio:.1f}x + trend {trend}",
                       signals, price, atr_pct, name)

    # ── Condiciones de entrada SHORT ──
    if bearish_cross and trend == "bearish" and price < ema50_val:
        signals.append(f"MACD bearish cross ✓")
        signals.append(f"Tendencia EMA bearish ✓")
        score = 0
        if vol_ratio > 1.8:
            score += 1; signals.append(f"Volumen alto ({vol_ratio:.1f}x) ✓")
        if price < ema50_val * 0.999:
            score += 1; signals.append(f"Precio bajo EMA50 ✓")
        if atr_pct < 1.5:
            score += 1; signals.append(f"ATR controlado ({atr_pct:.2f}%)")

        confidence = "HIGH" if score >= 2 else "MEDIUM" if score >= 1 else "LOW"
        return _result("SHORT", confidence,
                       f"Bearish cross + vol {vol_ratio:.1f}x + trend {trend}",
                       signals, price, atr_pct, name)

    return _result("FLAT", "MEDIUM", f"Sin cruce MACD válido (hist={hist_now:.1f}, trend={trend})",
                   [f"Esperando cruce"], price, atr_pct, name)


# ═══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA 2 — RSI + VWAP REVERSAL (5m)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Filosofía: el mercado exagera. Cuando el RSI está en extremos Y el precio
# se desvió mucho del VWAP (precio justo del día), el mercado va a revertir.
#
# VWAP = precio ponderado por volumen desde el inicio del día.
# Si precio << VWAP → el mercado sobrevendió → potencial rebote LONG.
# Si precio >> VWAP → el mercado sobrecompró → potencial caída SHORT.
#
# Filtro adicional: el RSI debe estar volviendo del extremo (no en caída libre).
# SL: más allá del extremo reciente (no queremos que nos liquiden en el piso).

def strategy_rsi_vwap_reversal(client, current_position: str, capital: float) -> dict:
    """RSI + VWAP Reversal — opera las exageraciones del mercado."""
    df    = fetch_ohlcv(client, interval="5m", limit=200)
    price = float(df["close"].iloc[-1])
    name  = "RSI_VWAP"

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    # RSI(14)
    delta  = close.diff()
    gain   = delta.clip(lower=0).rolling(14).mean()
    loss   = (-delta.clip(upper=0)).rolling(14).mean()
    rs     = gain / loss.replace(0, np.nan)
    rsi_s  = 100 - (100 / (1 + rs))
    rsi    = float(rsi_s.iloc[-1])
    rsi_prev = float(rsi_s.iloc[-2])

    # VWAP diario (desde la vela más reciente a medianoche)
    typical_price = (high + low + close) / 3
    cum_tp_vol    = (typical_price * volume).cumsum()
    cum_vol       = volume.cumsum()
    vwap_s        = cum_tp_vol / cum_vol
    vwap          = float(vwap_s.iloc[-1])
    vwap_dev_pct  = (price - vwap) / vwap * 100  # % de desviación

    # ATR
    tr    = pd.concat([high - low,
                        (high - close.shift()).abs(),
                        (low  - close.shift()).abs()], axis=1).max(axis=1)
    atr   = tr.rolling(14).mean()
    atr_pct = float(atr.iloc[-1] / price * 100)

    # Bollinger Bands (20,2) en 5m para confirmar extensión
    sma20  = close.rolling(20).mean()
    std20  = close.rolling(20).std()
    bb_pct = float(((close - (sma20 - 2*std20)) / (4*std20)).iloc[-1])

    signals = []

    # ── Gestión de posición existente ──
    if current_position == "LONG":
        # Salir si RSI vuelve a zona neutral o precio supera VWAP
        if rsi > 55 or price >= vwap * 1.003:
            return _result("FLAT", "HIGH", f"Reversión completada (RSI {rsi:.0f}, dev VWAP {vwap_dev_pct:+.2f}%)",
                           ["Objetivo de reversión alcanzado"], price, atr_pct, name)
        return _result("HOLD", "MEDIUM", f"LONG reversión en curso (RSI {rsi:.0f})",
                       [f"RSI {rsi:.0f}", f"VWAP dev {vwap_dev_pct:+.2f}%"], price, atr_pct, name)

    if current_position == "SHORT":
        if rsi < 45 or price <= vwap * 0.997:
            return _result("FLAT", "HIGH", f"Reversión completada (RSI {rsi:.0f}, dev VWAP {vwap_dev_pct:+.2f}%)",
                           ["Objetivo de reversión alcanzado"], price, atr_pct, name)
        return _result("HOLD", "MEDIUM", f"SHORT reversión en curso (RSI {rsi:.0f})",
                       [f"RSI {rsi:.0f}", f"VWAP dev {vwap_dev_pct:+.2f}%"], price, atr_pct, name)

    # ── Entrada LONG: RSI sobrevendido + precio muy bajo del VWAP ──
    rsi_oversold  = rsi < 32
    vwap_extended_down = vwap_dev_pct < -0.6
    rsi_recovering_long = rsi > rsi_prev  # RSI empezando a subir

    if rsi_oversold and vwap_extended_down:
        signals.append(f"RSI sobrevendido ({rsi:.0f})")
        signals.append(f"Precio {vwap_dev_pct:.2f}% bajo VWAP (${vwap:,.0f})")
        score = 0
        if rsi_recovering_long:
            score += 2; signals.append("RSI girando hacia arriba ✓")
        if bb_pct < 0.1:
            score += 1; signals.append("BB lower band ✓")
        if rsi < 25:
            score += 1; signals.append(f"RSI extremo ({rsi:.0f}) — sobrevendido fuerte")

        confidence = "HIGH" if score >= 2 else "MEDIUM"
        return _result("LONG", confidence,
                       f"RSI {rsi:.0f} + VWAP dev {vwap_dev_pct:.2f}%",
                       signals, price, atr_pct, name)

    # ── Entrada SHORT: RSI sobrecomprado + precio muy alto del VWAP ──
    rsi_overbought  = rsi > 68
    vwap_extended_up = vwap_dev_pct > 0.6
    rsi_recovering_short = rsi < rsi_prev  # RSI empezando a bajar

    if rsi_overbought and vwap_extended_up:
        signals.append(f"RSI sobrecomprado ({rsi:.0f})")
        signals.append(f"Precio {vwap_dev_pct:.2f}% sobre VWAP (${vwap:,.0f})")
        score = 0
        if rsi_recovering_short:
            score += 2; signals.append("RSI girando hacia abajo ✓")
        if bb_pct > 0.9:
            score += 1; signals.append("BB upper band ✓")
        if rsi > 75:
            score += 1; signals.append(f"RSI extremo ({rsi:.0f}) — sobrecomprado fuerte")

        confidence = "HIGH" if score >= 2 else "MEDIUM"
        return _result("SHORT", confidence,
                       f"RSI {rsi:.0f} + VWAP dev {vwap_dev_pct:.2f}%",
                       signals, price, atr_pct, name)

    return _result("FLAT", "MEDIUM",
                   f"Sin extremo (RSI {rsi:.0f}, VWAP dev {vwap_dev_pct:+.2f}%)",
                   ["Esperando RSI extremo + desviación VWAP"], price, atr_pct, name)


# ═══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA 3 — CVD DIVERGENCE (15m)
# ═══════════════════════════════════════════════════════════════════════════════
#
# CVD = Cumulative Volume Delta = taker_buy_volume - taker_sell_volume
# Representa el flujo neto de dinero agresivo (órdenes market).
#
# Si el precio sube pero el CVD baja → los compradores compran suave pero
# los vendedores agresivos controlan → el rally es falso → SHORT.
#
# Si el precio baja pero el CVD sube → los vendedores venden suave pero
# los compradores agresivos absorben → la caída es falsa → LONG.
#
# Confirmación: divergencia debe mantenerse al menos 3 velas y el precio
# debe haberse movido >0.5% para que la divergencia sea relevante.

def strategy_cvd_divergence(client, current_position: str, capital: float) -> dict:
    """CVD Divergence — sigue el dinero, no el precio."""
    df    = fetch_ohlcv(client, interval="15m", limit=150)
    price = float(df["close"].iloc[-1])
    name  = "CVD_DIVERGENCE"

    close  = df["close"]
    volume = df["volume"]

    # CVD = acumulado de (taker_buy - taker_sell)
    taker_sell = volume - df["taker_buy_base"]
    delta_vol  = df["taker_buy_base"] - taker_sell
    cvd        = delta_vol.cumsum()

    # Suavizar CVD con EMA para filtrar ruido
    cvd_ema   = cvd.ewm(span=5, adjust=False).mean()

    # ATR para filtros y SL
    tr      = pd.concat([df["high"] - df["low"],
                          (df["high"] - close.shift()).abs(),
                          (df["low"]  - close.shift()).abs()], axis=1).max(axis=1)
    atr     = tr.rolling(14).mean()
    atr_pct = float(atr.iloc[-1] / price * 100)

    # Lookback para divergencia: últimas N velas
    N = 5  # ventana de análisis

    price_change = float(close.iloc[-1] - close.iloc[-N]) / float(close.iloc[-N]) * 100
    cvd_change   = float(cvd_ema.iloc[-1] - cvd_ema.iloc[-N])
    cvd_direction = "up" if cvd_change > 0 else "down"
    price_direction = "up" if price_change > 0 else "down"

    # Magnitud para filtrar ruido
    price_moved = abs(price_change) > 0.4
    cvd_moved   = abs(cvd_change) > (volume.iloc[-N:].mean() * 0.05)

    # Tendencia macro (EMA50/200) como filtro extra
    ema50    = close.ewm(span=50, adjust=False).mean()
    ema200   = close.ewm(span=200, adjust=False).mean()
    macro_trend = "bullish" if float(ema50.iloc[-1]) > float(ema200.iloc[-1]) else "bearish"

    # RSI para evitar entrar en extremos
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = float((100 - (100 / (1 + rs))).iloc[-1])

    signals = []

    # ── Gestión de posición existente ──
    if current_position == "LONG":
        # Cerrar si la divergencia se revirtió (CVD ahora bajando con precio)
        if cvd_direction == "down" and price_direction == "down":
            return _result("FLAT", "HIGH", "CVD y precio alineados a la baja → cierre LONG",
                           ["Divergencia resuelta"], price, atr_pct, name)
        return _result("HOLD", "MEDIUM", f"LONG activo, CVD: {cvd_direction}",
                       [f"CVD {cvd_direction}", f"Precio {price_change:+.2f}%"], price, atr_pct, name)

    if current_position == "SHORT":
        if cvd_direction == "up" and price_direction == "up":
            return _result("FLAT", "HIGH", "CVD y precio alineados al alza → cierre SHORT",
                           ["Divergencia resuelta"], price, atr_pct, name)
        return _result("HOLD", "MEDIUM", f"SHORT activo, CVD: {cvd_direction}",
                       [f"CVD {cvd_direction}", f"Precio {price_change:+.2f}%"], price, atr_pct, name)

    # ── Divergencia BAJISTA: precio sube pero CVD baja → SHORT ──
    # Precio va para arriba pero el dinero agresivo está vendiendo
    bearish_divergence = (price_direction == "up" and cvd_direction == "down"
                          and price_moved and cvd_moved)

    if bearish_divergence and rsi < 72:  # no entrar si RSI ya está extremo
        signals.append(f"Precio +{price_change:.2f}% pero CVD bajando ← divergencia bajista")
        signals.append(f"Dinero agresivo vendiendo mientras precio sube")
        score = 0
        if macro_trend == "bearish":
            score += 2; signals.append(f"Tendencia macro bajista confirma ✓")
        if rsi > 60:
            score += 1; signals.append(f"RSI elevado ({rsi:.0f}) — terreno de venta ✓")
        if abs(cvd_change) > (volume.iloc[-N:].mean() * 0.15):
            score += 1; signals.append(f"CVD delta grande — presión vendedora fuerte ✓")

        confidence = "HIGH" if score >= 2 else "MEDIUM"
        return _result("SHORT", confidence,
                       f"Bearish CVD divergence | precio {price_change:+.2f}% | CVD ↓",
                       signals, price, atr_pct, name)

    # ── Divergencia ALCISTA: precio baja pero CVD sube → LONG ──
    # Precio va para abajo pero el dinero agresivo está comprando
    bullish_divergence = (price_direction == "down" and cvd_direction == "up"
                          and price_moved and cvd_moved)

    if bullish_divergence and rsi > 28:
        signals.append(f"Precio {price_change:.2f}% pero CVD subiendo → divergencia alcista")
        signals.append(f"Dinero agresivo comprando mientras precio cae")
        score = 0
        if macro_trend == "bullish":
            score += 2; signals.append(f"Tendencia macro alcista confirma ✓")
        if rsi < 40:
            score += 1; signals.append(f"RSI bajo ({rsi:.0f}) — terreno de compra ✓")
        if abs(cvd_change) > (volume.iloc[-N:].mean() * 0.15):
            score += 1; signals.append(f"CVD delta grande — absorción fuerte ✓")

        confidence = "HIGH" if score >= 2 else "MEDIUM"
        return _result("LONG", confidence,
                       f"Bullish CVD divergence | precio {price_change:+.2f}% | CVD ↑",
                       signals, price, atr_pct, name)

    return _result("FLAT", "MEDIUM",
                   f"Sin divergencia (precio {price_direction} {price_change:+.2f}%, CVD {cvd_direction})",
                   ["Esperando divergencia precio/CVD"], price, atr_pct, name)


# ═══════════════════════════════════════════════════════════════════════════════
# SISTEMA DE VOTACIÓN
# ═══════════════════════════════════════════════════════════════════════════════
#
# Cada estrategia vota LONG, SHORT o FLAT.
# Para entrar se necesita mayoría (≥2 de 3).
# Si hay empate → FLAT (preservar capital).
# Confianza final: HIGH si las 3 coinciden, MEDIUM si 2 de 3.

def vote(results: list) -> dict:
    """Sistema de votación entre las 3 estrategias."""
    votes = {"LONG": 0, "SHORT": 0, "FLAT": 0, "HOLD": 0}
    for r in results:
        d = r.get("decision", "FLAT")
        votes[d] = votes.get(d, 0) + 1

    # HOLD cuenta como FLAT para la votación
    votes["FLAT"] += votes.pop("HOLD", 0)

    long_votes  = votes["LONG"]
    short_votes = votes["SHORT"]

    log.info(f"  VOTE → LONG:{long_votes} SHORT:{short_votes} FLAT:{votes['FLAT']}")
    for r in results:
        log.info(f"    {r['_strategy']}: {r['decision']} ({r['confidence']}) — {r['reasoning']}")

    # El precio y ATR del primer resultado disponible
    price   = results[0]["entry_price"]
    sl_pct  = max(r["stop_loss_pct"] for r in results)   # el más conservador
    tp_pct  = max(r["take_profit_pct"] for r in results)

    all_signals = []
    for r in results:
        all_signals.extend(r.get("key_signals", [])[:2])

    if long_votes >= 2:
        confidence = "HIGH" if long_votes == 3 else "MEDIUM"
        return {
            "decision":        "LONG",
            "confidence":      confidence,
            "reasoning":       f"Voto {long_votes}/3 LONG | " + " | ".join(r["reasoning"] for r in results if r["decision"] == "LONG"),
            "key_signals":     all_signals,
            "entry_price":     price,
            "stop_loss_pct":   sl_pct,
            "take_profit_pct": tp_pct,
            "position_size_pct": 0.10 if confidence == "HIGH" else 0.06,
        }
    elif short_votes >= 2:
        confidence = "HIGH" if short_votes == 3 else "MEDIUM"
        return {
            "decision":        "SHORT",
            "confidence":      confidence,
            "reasoning":       f"Voto {short_votes}/3 SHORT | " + " | ".join(r["reasoning"] for r in results if r["decision"] == "SHORT"),
            "key_signals":     all_signals,
            "entry_price":     price,
            "stop_loss_pct":   sl_pct,
            "take_profit_pct": tp_pct,
            "position_size_pct": 0.10 if confidence == "HIGH" else 0.06,
        }
    else:
        # Check HOLD mayoritario
        hold_results = [r for r in results if r["decision"] == "HOLD"]
        if len(hold_results) >= 2:
            return {**hold_results[0], "decision": "HOLD", "confidence": "MEDIUM",
                    "reasoning": "Mayoría HOLD — manteniendo posición"}
        return {
            "decision":        "FLAT",
            "confidence":      "MEDIUM",
            "reasoning":       f"Sin mayoría (L:{long_votes} S:{short_votes} F:{votes['FLAT']}) → esperando",
            "key_signals":     ["Señales divididas — no entrar"],
            "entry_price":     price,
            "stop_loss_pct":   sl_pct,
            "take_profit_pct": tp_pct,
            "position_size_pct": 0.06,
        }


def run_strategies(client, current_position: str, capital: float) -> dict:
    """Ejecuta la(s) estrategia(s) según ACTIVE_STRATEGY."""
    if ACTIVE_STRATEGY == "MACD":
        return strategy_macd_momentum(client, current_position, capital)
    elif ACTIVE_STRATEGY == "RSI_VWAP":
        return strategy_rsi_vwap_reversal(client, current_position, capital)
    elif ACTIVE_STRATEGY == "CVD":
        return strategy_cvd_divergence(client, current_position, capital)
    else:  # VOTE — las 3 estrategias votan
        r1 = strategy_macd_momentum(client, current_position, capital)
        r2 = strategy_rsi_vwap_reversal(client, current_position, capital)
        r3 = strategy_cvd_divergence(client, current_position, capital)
        result = vote([r1, r2, r3])
        # Adjuntar votos individuales para que run_cycle los pase al paper engine
        result["_votes"] = {
            "macd":     r1["decision"],
            "rsi_vwap": r2["decision"],
            "cvd":      r3["decision"],
        }
        return result


# ─── Cuenta y Órdenes ────────────────────────────────────────────────────────

def get_account_info(client) -> dict:
    try:
        account    = client.futures_account()
        balance    = float(account.get("availableBalance", 0))
        positions  = client.futures_position_information(symbol=SYMBOL)
        current_pos = "FLAT"
        pos_size    = 0.0
        pos_entry   = 0.0
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt > 0:
                current_pos = "LONG";  pos_size = amt;      pos_entry = float(p.get("entryPrice", 0))
            elif amt < 0:
                current_pos = "SHORT"; pos_size = abs(amt); pos_entry = float(p.get("entryPrice", 0))
        return {"balance": balance, "position": current_pos,
                "position_size": pos_size, "entry_price": pos_entry}
    except Exception as e:
        log.error(f"Error cuenta: {e}")
        return {"balance": 500.0, "position": "FLAT", "position_size": 0, "entry_price": 0}


def set_leverage(client, leverage: int = LEVERAGE):
    try:
        client.futures_change_leverage(symbol=SYMBOL, leverage=leverage)
        log.info(f"Apalancamiento: {leverage}x")
    except Exception as e:
        log.warning(f"No se pudo setear leverage: {e}")


def close_position(client, current_position: str, position_size: float) -> bool:
    if current_position == "FLAT" or position_size == 0:
        return True
    side = "SELL" if current_position == "LONG" else "BUY"
    log.info(f"Cerrando posición {current_position} ({position_size} BTC)...")
    if DRY_RUN:
        log.info("  [DRY RUN] Posición cerrada (simulación)")
        return True
    try:
        client.futures_create_order(
            symbol=SYMBOL, side=side, type="MARKET",
            quantity=position_size, reduceOnly=True,
        )
        log.info("  ✅ Posición cerrada")
        return True
    except Exception as e:
        log.error(f"  ❌ Error cerrando: {e}")
        return False


def open_position(client, decision: dict, capital: float, current_price: float) -> bool:
    side    = "BUY" if decision["decision"] == "LONG" else "SELL"
    pos_pct = min(float(decision.get("position_size_pct", 0.06)), MAX_POSITION_PCT)
    sl_pct  = float(decision.get("stop_loss_pct", 0.02))
    tp_pct  = float(decision.get("take_profit_pct", 0.05))

    usdt_to_use  = capital * pos_pct * LEVERAGE
    btc_quantity = round(usdt_to_use / current_price, 3)

    if decision["decision"] == "LONG":
        sl_price = round(current_price * (1 - sl_pct), 1)
        tp_price = round(current_price * (1 + tp_pct), 1)
    else:
        sl_price = round(current_price * (1 + sl_pct), 1)
        tp_price = round(current_price * (1 - tp_pct), 1)

    log.info(f"\n{'='*55}")
    log.info(f"NUEVA POSICIÓN: {decision['decision']} [{decision.get('_strategy', ACTIVE_STRATEGY)}]")
    log.info(f"  Entrada:    ${current_price:,.2f}")
    log.info(f"  Cantidad:   {btc_quantity} BTC (${usdt_to_use:.0f} USDT × {LEVERAGE}x)")
    log.info(f"  Stop Loss:  ${sl_price:,.2f} (-{sl_pct:.1%})")
    log.info(f"  Take Profit:${tp_price:,.2f} (+{tp_pct:.1%})")
    log.info(f"  Confianza:  {decision.get('confidence')}")
    log.info(f"  Razón:      {decision.get('reasoning', '')}")
    log.info(f"  Señales:    {', '.join(decision.get('key_signals', [])[:4])}")

    if DRY_RUN:
        log.info("  [DRY RUN] Orden NO ejecutada")
        return True

    try:
        client.futures_create_order(
            symbol=SYMBOL, side=side, type="MARKET", quantity=btc_quantity,
        )
        sl_side = "SELL" if side == "BUY" else "BUY"
        client.futures_create_order(
            symbol=SYMBOL, side=sl_side, type="STOP_MARKET",
            stopPrice=sl_price, closePosition=True,
        )
        client.futures_create_order(
            symbol=SYMBOL, side=sl_side, type="TAKE_PROFIT_MARKET",
            stopPrice=tp_price, closePosition=True,
        )
        log.info("  ✅ Posición abierta con SL y TP")
        return True
    except Exception as e:
        log.error(f"  ❌ Error abriendo posición: {e}")
        return False


# ─── Loop Principal ───────────────────────────────────────────────────────────

def run_cycle(client, paper=None):
    log.info(f"\n{'#'*60}")
    log.info(f"CICLO [{ACTIVE_STRATEGY}] — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"{'#'*60}")

    # 1. Estado
    if DRY_RUN and paper:
        paper.check_t2_stops(0)          # se actualiza con precio real abajo
        open_trade       = paper.get_t2_position()
        capital          = paper.state.current_capital + sum(t.size for t in paper.state.open_trades)
        current_position = open_trade.side if open_trade else "FLAT"
        account = {
            "balance": capital, "position": current_position,
            "position_size": 0, "entry_price": open_trade.entry_price if open_trade else 0,
        }
    else:
        account          = get_account_info(client)
        capital          = account["balance"]
        current_position = account["position"]

    log.info(f"Balance: ${capital:.2f} USDT | Posición: {current_position}")
    if current_position != "FLAT":
        log.info(f"  Entrada: ${account['entry_price']:,.2f}")

    # 2. Estrategia(s) deciden
    decision = run_strategies(client, current_position, capital)
    if not decision:
        log.warning("Sin decisión. Saltando ciclo.")
        return

    action     = decision.get("decision", "FLAT")
    confidence = decision.get("confidence", "LOW")

    # 3. Ejecutar
    if DRY_RUN and paper:
        price = decision["entry_price"]
        paper.check_t2_stops(price)
        paper.state.btc_price       = price
        paper.state.active_strategy = ACTIVE_STRATEGY

        # Guardar voto
        if ACTIVE_STRATEGY == "VOTE" and "_votes" in decision:
            v = decision["_votes"]
            paper.update_vote(
                macd=v.get("macd", "--"),
                rsi_vwap=v.get("rsi_vwap", "--"),
                cvd=v.get("cvd", "--"),
                result=action,
            )
        else:
            paper.update_vote(
                macd=action     if ACTIVE_STRATEGY == "MACD"     else "--",
                rsi_vwap=action if ACTIVE_STRATEGY == "RSI_VWAP" else "--",
                cvd=action      if ACTIVE_STRATEGY == "CVD"      else "--",
                result=action,
            )

        if action == "HOLD":
            paper.add_log(f"HOLD — manteniendo [{decision.get('_strategy','?')}]")

        elif action == "FLAT":
            open_trade = paper.get_t2_position()
            if open_trade:
                paper.close_t2_position(price, "SIGNAL")
            else:
                paper.add_log("FLAT — sin señal, esperando")

        elif action in ("LONG", "SHORT"):
            if confidence == "LOW":
                paper.add_log("LOW confidence — no entrando")
            else:
                # Verificar cooldown
                cooldown_ok = True
                try:
                    from paper_trading import TRADING2_STATE
                    import json as _json
                    raw = _json.loads(TRADING2_STATE.read_text()) if TRADING2_STATE.exists() else {}
                    cooldown = raw.get("cooldown_until")
                    if cooldown and datetime.fromisoformat(cooldown) > datetime.now():
                        log.info(f"  Cooldown activo hasta {cooldown[:16]}")
                        paper.add_log("Cooldown activo — esperando")
                        cooldown_ok = False
                except Exception:
                    pass

                if cooldown_ok:
                    open_trade = paper.get_t2_position()
                    if open_trade and open_trade.side != action:
                        paper.close_t2_position(price, "SIGNAL")
                    if not paper.get_t2_position():
                        paper.open_t2_trade(decision, price, capital, LEVERAGE)

        paper.save()
        log.info(f"  [T2 PAPER] Capital: ${paper.state.current_capital:.2f} | P&L: {paper.state.total_pnl:+.2f} | Win: {paper.state.win_rate:.0f}%")

    else:
        # Trading real
        if action == "HOLD":
            log.info("HOLD — manteniendo posición actual.")
        elif action == "FLAT":
            if current_position != "FLAT":
                close_position(client, current_position, account["position_size"])
            else:
                log.info("FLAT — sin posición, esperando señal.")
        elif action in ("LONG", "SHORT"):
            if confidence == "LOW":
                log.info("Confianza LOW — no entrando.")
                return
            if current_position != "FLAT" and current_position != action:
                close_position(client, current_position, account["position_size"])
                time.sleep(1)
                account          = get_account_info(client)
                current_position = account["position"]
            if current_position == "FLAT":
                open_position(client, decision, capital, decision["entry_price"])
            else:
                log.info(f"Ya en {action}. Manteniendo.")


def run_forever():
    from paper_trading import get_trading2_engine
    log.info("🤖 Trading2 Bot iniciado")
    log.info(f"   Modo:      {'DRY RUN' if DRY_RUN else '⚠️  REAL'}")
    log.info(f"   Estrategia:{ACTIVE_STRATEGY}")
    log.info(f"   Par:       {SYMBOL} | Leverage: {LEVERAGE}x | Intervalo: {INTERVAL_MINUTES}min")

    client = get_binance_client()
    set_leverage(client)
    paper  = get_trading2_engine(initial_capital=500.0) if DRY_RUN else None

    while True:
        try:
            run_cycle(client, paper)
        except KeyboardInterrupt:
            log.info("\n🛑 Bot detenido")
            break
        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)

        log.info(f"\n⏰ Próximo ciclo en {INTERVAL_MINUTES} minutos...")
        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    import sys
    from paper_trading import get_trading2_engine
    client = get_binance_client()
    set_leverage(client)
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        paper = get_trading2_engine(initial_capital=500.0) if DRY_RUN else None
        run_cycle(client, paper)
    else:
        run_forever()

def get_account_info(client) -> dict:
    try:
        account    = client.futures_account()
        balance    = float(account.get("availableBalance", 0))
        positions  = client.futures_position_information(symbol=SYMBOL)
        current_pos = "FLAT"
        pos_size    = 0.0
        pos_entry   = 0.0
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if amt > 0:
                current_pos = "LONG";  pos_size = amt;      pos_entry = float(p.get("entryPrice", 0))
            elif amt < 0:
                current_pos = "SHORT"; pos_size = abs(amt); pos_entry = float(p.get("entryPrice", 0))
        return {"balance": balance, "position": current_pos,
                "position_size": pos_size, "entry_price": pos_entry}
    except Exception as e:
        log.error(f"Error cuenta: {e}")
        return {"balance": 500.0, "position": "FLAT", "position_size": 0, "entry_price": 0}


def set_leverage(client, leverage: int = LEVERAGE):
    try:
        client.futures_change_leverage(symbol=SYMBOL, leverage=leverage)
        log.info(f"Apalancamiento: {leverage}x")
    except Exception as e:
        log.warning(f"No se pudo setear leverage: {e}")


def close_position(client, current_position: str, position_size: float) -> bool:
    if current_position == "FLAT" or position_size == 0:
        return True
    side = "SELL" if current_position == "LONG" else "BUY"
    log.info(f"Cerrando posición {current_position} ({position_size} BTC)...")
    if DRY_RUN:
        log.info("  [DRY RUN] Posición cerrada (simulación)")
        return True
    try:
        client.futures_create_order(
            symbol=SYMBOL, side=side, type="MARKET",
            quantity=position_size, reduceOnly=True,
        )
        log.info("  ✅ Posición cerrada")
        return True
    except Exception as e:
        log.error(f"  ❌ Error cerrando: {e}")
        return False


def open_position(client, decision: dict, capital: float, current_price: float) -> bool:
    side    = "BUY" if decision["decision"] == "LONG" else "SELL"
    pos_pct = min(float(decision.get("position_size_pct", 0.06)), MAX_POSITION_PCT)
    sl_pct  = float(decision.get("stop_loss_pct", 0.02))
    tp_pct  = float(decision.get("take_profit_pct", 0.05))

    usdt_to_use  = capital * pos_pct * LEVERAGE
    btc_quantity = round(usdt_to_use / current_price, 3)

    if decision["decision"] == "LONG":
        sl_price = round(current_price * (1 - sl_pct), 1)
        tp_price = round(current_price * (1 + tp_pct), 1)
    else:
        sl_price = round(current_price * (1 + sl_pct), 1)
        tp_price = round(current_price * (1 - tp_pct), 1)

    log.info(f"\n{'='*55}")
    log.info(f"NUEVA POSICIÓN: {decision['decision']} [{decision.get('_strategy', ACTIVE_STRATEGY)}]")
    log.info(f"  Entrada:    ${current_price:,.2f}")
    log.info(f"  Cantidad:   {btc_quantity} BTC (${usdt_to_use:.0f} USDT × {LEVERAGE}x)")
    log.info(f"  Stop Loss:  ${sl_price:,.2f} (-{sl_pct:.1%})")
    log.info(f"  Take Profit:${tp_price:,.2f} (+{tp_pct:.1%})")
    log.info(f"  Confianza:  {decision.get('confidence')}")
    log.info(f"  Razón:      {decision.get('reasoning', '')}")
    log.info(f"  Señales:    {', '.join(decision.get('key_signals', [])[:4])}")

    if DRY_RUN:
        log.info("  [DRY RUN] Orden NO ejecutada")
        return True

    try:
        client.futures_create_order(
            symbol=SYMBOL, side=side, type="MARKET", quantity=btc_quantity,
        )
        sl_side = "SELL" if side == "BUY" else "BUY"
        client.futures_create_order(
            symbol=SYMBOL, side=sl_side, type="STOP_MARKET",
            stopPrice=sl_price, closePosition=True,
        )
        client.futures_create_order(
            symbol=SYMBOL, side=sl_side, type="TAKE_PROFIT_MARKET",
            stopPrice=tp_price, closePosition=True,
        )
        log.info("  ✅ Posición abierta con SL y TP")
        return True
    except Exception as e:
        log.error(f"  ❌ Error abriendo posición: {e}")
        return False


# ─── Loop Principal ───────────────────────────────────────────────────────────

def run_cycle(client):
    log.info(f"\n{'#'*60}")
    log.info(f"CICLO [{ACTIVE_STRATEGY}] — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"{'#'*60}")

    # 1. Estado de cuenta
    account          = get_account_info(client)
    capital          = account["balance"]
    current_position = account["position"]

    log.info(f"Balance: ${capital:.2f} USDT | Posición: {current_position}")
    if current_position != "FLAT":
        log.info(f"  Entrada: ${account['entry_price']:,.2f}")

    # 2. Estrategia(s) deciden
    decision = run_strategies(client, current_position, capital)
    if not decision:
        log.warning("Sin decisión. Saltando ciclo.")
        return

    action     = decision.get("decision", "FLAT")
    confidence = decision.get("confidence", "LOW")

    # 3. Ejecutar
    if action == "HOLD":
        log.info("HOLD — manteniendo posición actual.")

    elif action == "FLAT":
        if current_position != "FLAT":
            close_position(client, current_position, account["position_size"])
        else:
            log.info("FLAT — sin posición, esperando señal.")

    elif action in ("LONG", "SHORT"):
        if confidence == "LOW":
            log.info(f"Confianza LOW — no entrando.")
            return
        if current_position != "FLAT" and current_position != action:
            close_position(client, current_position, account["position_size"])
            time.sleep(1)
            account = get_account_info(client)
            current_position = account["position"]
        if current_position == "FLAT":
            open_position(client, decision, capital, decision["entry_price"])
        else:
            log.info(f"Ya en {action}. Manteniendo.")


def run_forever():
    log.info(f"🤖 Trading2 Bot iniciado")
    log.info(f"   Modo:      {'DRY RUN' if DRY_RUN else '⚠️  REAL'}")
    log.info(f"   Estrategia:{ACTIVE_STRATEGY}")
    log.info(f"   Par:       {SYMBOL} | Leverage: {LEVERAGE}x | Intervalo: {INTERVAL_MINUTES}min")

    client = get_binance_client()
    set_leverage(client)

    while True:
        try:
            run_cycle(client)
        except KeyboardInterrupt:
            log.info("\n🛑 Bot detenido")
            break
        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)

        log.info(f"\n⏰ Próximo ciclo en {INTERVAL_MINUTES} minutos...")
        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    import sys
    client = get_binance_client()
    set_leverage(client)
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_cycle(client)
    else:
        run_forever()
