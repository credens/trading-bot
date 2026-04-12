"""
Scalping Bot v2 — BTC Futures 1m
==================================
Opera en 3 modos adaptativos según el régimen de mercado.

RÉGIMEN DETECTADO CON ADX + VOLUMEN:
  · TREND    (ADX > 25)      → EMA cross + CVD + MACD momentum
  · RANGE    (ADX < 20)      → Mean reversion en Bollinger Bands + RSI extremo
  · BREAKOUT (vol > 2.5x)    → Entrada en dirección del spike de volumen

SEÑALES NUEVAS:
  · CVD (Cumulative Volume Delta) — distingue presión compradora/vendedora real
    Si precio sube pero CVD cae → distribución → no LONG
    Si precio baja pero CVD sube → acumulación → no SHORT
  · ADX(14) — mide fuerza de tendencia, no dirección
  · Bollinger Bands %B — para mean reversion en rango
  · Trailing stop — mueve SL a breakeven cuando el trade llega a 50% del TP

FILTROS:
  · Horario: no operar 00:00–06:00 UTC (baja liquidez)
  · ATR máx: 0.5% en 1m (demasiada volatilidad para scalp)
  · CVD divergence blocker: no entrar si precio y CVD van en direcciones opuestas

GESTIÓN:
  · SL dinámico: ATR × 1.2 (mín 0.4%, máx 0.6%)
  · TP: SL × 2
  · Trailing stop: al llegar al 50% del TP → SL → breakeven
  · Circuit breaker: 3 SL seguidos → pausa 15 min
  · Ciclos de 30 segundos
"""

import os
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [SCALP] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
SYMBOL         = "BTCUSDT"
LEVERAGE       = int(os.getenv("LEVERAGE", "3"))
DRY_RUN        = os.getenv("DRY_RUN", "true").lower() == "true"
SCALP_CAPITAL  = float(os.getenv("SCALP_CAPITAL", "500"))
CYCLE_SECONDS  = int(os.getenv("SCALP_CYCLE_SECONDS", "30"))
SL_PCT         = 0.008   # 0.8% mínimo
TP_PCT         = 0.018   # 1.8% mínimo
POS_PCT        = 0.15    # 15% del capital por trade
MIN_HOLD_SECS  = 300     # 5 min mínimo antes de cerrar por SIGNAL
SIGNAL_COOLDOWN_SECS = 180  # 3 min cooldown después de cerrar por SIGNAL

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

# ─── Macro 1h cache (se refresca cada 5 min) ────────────────────────────────
_macro_cache = {"trend": "neutral", "gap": 0.0, "ts": 0}

def get_macro_1h(client):
    """BTC 1h macro trend con cache de 5 min."""
    import time as _t
    now = _t.time()
    if now - _macro_cache["ts"] < 300:  # 5 min cache
        return _macro_cache["trend"], _macro_cache["gap"]
    try:
        klines = client.futures_klines(symbol=SYMBOL, interval="1h", limit=210)
        closes = pd.Series([float(k[4]) for k in klines])
        ema50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(closes.ewm(span=200, adjust=False).mean().iloc[-1])
        gap = (ema200 - ema50) / ema200 * 100  # positivo = bearish
        trend = "bearish" if ema50 < ema200 else "bullish"
        _macro_cache.update({"trend": trend, "gap": gap, "ts": now})
        return trend, gap
    except Exception:
        return _macro_cache["trend"], _macro_cache["gap"]

# ─── Paper Trading ────────────────────────────────────────────────────────────
from paper_trading import get_scalping_engine, SCALPING_STATE
import json as _json

# ─── Binance Client ───────────────────────────────────────────────────────────
def get_client():
    from binance.client import Client
    # Aumentar el timeout de 10s (default) a 30s para evitar ReadTimeoutError
    return Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, requests_params={'timeout': 30})


# ─── Datos de Mercado ─────────────────────────────────────────────────────────
def fetch_1m(client, limit: int = 120) -> pd.DataFrame:
    """Trae velas de 1 minuto con volumen de takers para CVD."""
    klines = client.futures_klines(symbol=SYMBOL, interval="1m", limit=limit)
    df = pd.DataFrame(klines, columns=[
        "ts","open","high","low","close","volume",
        "close_time","quote_vol","trades","taker_buy","taker_buy_q","ignore"
    ])
    for c in ["open","high","low","close","volume","taker_buy"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df


def calc_indicators(df: pd.DataFrame) -> dict:
    """Calcula indicadores incluyendo CVD, ADX y Bollinger Bands."""
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]
    taker  = df["taker_buy"]
    price  = float(close.iloc[-1])

    # ── EMAs ──────────────────────────────────────────────────
    ema9  = close.ewm(span=9,  adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema9_val  = float(ema9.iloc[-1])
    ema21_val = float(ema21.iloc[-1])

    cross_bullish = cross_bearish = False
    for i in range(-4, -1):
        if ema9.iloc[i-1] <= ema21.iloc[i-1] and ema9.iloc[i] > ema21.iloc[i]:
            cross_bullish = True
        if ema9.iloc[i-1] >= ema21.iloc[i-1] and ema9.iloc[i] < ema21.iloc[i]:
            cross_bearish = True

    ema_trend = "bullish" if ema9_val > ema21_val else "bearish"

    # ── VWAP intraday ─────────────────────────────────────────
    typical = (high + low + close) / 3
    vwap = float((typical * volume).cumsum().iloc[-1] / volume.cumsum().iloc[-1])
    price_vs_vwap = (price - vwap) / vwap * 100

    # ── RSI 14 ────────────────────────────────────────────────
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rsi   = float((100 - (100 / (1 + gain / loss.replace(0, np.nan)))).iloc[-1])

    # ── MACD rápido (3, 10, 5) ────────────────────────────────
    e3   = close.ewm(span=3,  adjust=False).mean()
    e10  = close.ewm(span=10, adjust=False).mean()
    macd = e3 - e10
    sig  = macd.ewm(span=5, adjust=False).mean()
    hist = macd - sig
    macd_hist = float(hist.iloc[-1])
    macd_prev = float(hist.iloc[-2])

    # ── Volumen relativo ──────────────────────────────────────
    vol_avg   = float(volume.rolling(20).mean().iloc[-1])
    vol_ratio = float(volume.iloc[-1]) / (vol_avg or 1)

    # ── ATR 14 ────────────────────────────────────────────────
    tr      = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    atr     = float(tr.rolling(14).mean().iloc[-1])
    atr_pct = atr / price * 100

    # ── CVD (Cumulative Volume Delta) ─────────────────────────
    # delta = compra de takers - venta de takers
    cvd_delta = taker - (volume - taker)
    cvd       = cvd_delta.cumsum()
    # Pendiente CVD últimas 5 velas
    cvd_slope = float(cvd.diff(5).iloc[-1])
    # Pendiente precio últimas 5 velas
    price_slope = float(close.diff(5).iloc[-1])
    # Divergencia: precio y CVD van en direcciones opuestas
    cvd_bullish   = cvd_slope > 0   # acumulación neta
    cvd_bearish   = cvd_slope < 0   # distribución neta
    cvd_divergence = (price_slope > 0 and cvd_slope < 0) or (price_slope < 0 and cvd_slope > 0)

    # ── ADX 14 (fuerza de tendencia) ──────────────────────────
    plus_dm  = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    # Si sube pero la baja anterior fue mayor → no es +DM real
    plus_dm[plus_dm < minus_dm]  = 0
    minus_dm[minus_dm < plus_dm] = 0
    atr14    = tr.ewm(span=14, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=14, adjust=False).mean()  / atr14.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(span=14, adjust=False).mean() / atr14.replace(0, np.nan)
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx      = float(dx.ewm(span=14, adjust=False).mean().iloc[-1])

    # ── Bollinger Bands 20,2 ──────────────────────────────────
    sma20    = close.rolling(20).mean()
    std20    = close.rolling(20).std()
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_pct   = float(((close - bb_lower) / (bb_upper - bb_lower).replace(0, np.nan)).iloc[-1])
    bb_mid   = float(sma20.iloc[-1])

    # ── Contexto EMA50 ────────────────────────────────────────
    ema50 = close.ewm(span=50, adjust=False).mean()
    trend_ctx = "bullish" if float(ema50.iloc[-1]) < price else "bearish"

    return {
        "price":          price,
        "ema9":           round(ema9_val, 2),
        "ema21":          round(ema21_val, 2),
        "ema_trend":      ema_trend,
        "cross_bullish":  cross_bullish,
        "cross_bearish":  cross_bearish,
        "vwap":           round(vwap, 2),
        "price_vs_vwap":  round(price_vs_vwap, 3),
        "rsi":            round(rsi, 2),
        "macd_hist":      round(macd_hist, 4),
        "macd_prev":      round(macd_prev, 4),
        "vol_ratio":      round(vol_ratio, 2),
        "atr":            round(atr, 2),
        "atr_pct":        round(atr_pct, 3),
        "trend_ctx":      trend_ctx,
        # CVD
        "cvd_slope":      round(cvd_slope, 2),
        "cvd_bullish":    cvd_bullish,
        "cvd_bearish":    cvd_bearish,
        "cvd_divergence": cvd_divergence,
        # ADX
        "adx":            round(adx, 1),
        # Bollinger
        "bb_pct":         round(bb_pct, 3),
        "bb_mid":         round(bb_mid, 2),
        "bb_upper":       round(float(bb_upper.iloc[-1]), 2),
        "bb_lower":       round(float(bb_lower.iloc[-1]), 2),
        "adx_prev":       round(adx, 1),  # MEJORA 4: Track ADX anterior para detectar aceleración
    }


# ─── Detección de régimen ─────────────────────────────────────────────────────
def detect_regime(ind: dict) -> str:
    """
    BREAKOUT : volumen > 2.5x promedio (movimiento explosivo)
    TREND    : ADX > 25 (mercado con dirección clara)
    RANGE    : ADX < 20 (mercado lateral, usar mean reversion)
    MIXED    : ADX entre 20-25 (transición, criterios más estrictos)
    """
    adx = ind["adx"]
    vol = ind["vol_ratio"]
    if vol > 2.5:
        return "BREAKOUT"
    elif adx > 25:
        return "TREND"
    elif adx < 20:
        return "RANGE"
    else:
        return "MIXED"


# ─── Estrategias por régimen ──────────────────────────────────────────────────
def analyze(ind: dict, current_position: str, scenario=None) -> dict:
    """Decide: LONG / SHORT / FLAT / HOLD según el régimen de mercado y escenario."""
    from market_scenario import is_with_trend, get_size

    price   = ind["price"]
    atr_pct = ind["atr_pct"]
    rsi     = ind["rsi"]
    vol     = ind["vol_ratio"]
    macd_h  = ind["macd_hist"]

    # SL/TP dinámicos basados en ATR
    atr_dec = atr_pct / 100  # convertir a decimal
    sl_pct = round(max(min(atr_dec * 1.5, 0.015), SL_PCT), 5)   # 1.5x ATR, clamp 0.4%-1.5%
    tp_mult = scenario.get("sc_tp_mult", 1.0) if scenario else 1.0
    tp_pct = round(sl_pct * 2.5 * tp_mult, 5)                    # R:R 2.5:1

    def make(decision, confidence, reasoning, signals):
        # Size dinámico según escenario y dirección
        if scenario and decision in ("LONG", "SHORT"):
            size = get_size(decision, scenario, "sc")
        else:
            size = POS_PCT
        return {
            "decision": decision, "confidence": confidence,
            "reasoning": reasoning, "key_signals": signals,
            "entry_price": price, "stop_loss_pct": sl_pct,
            "take_profit_pct": tp_pct, "position_size_pct": size,
        }

    # ── Volatilidad extrema ────────────────────────────────────
    if atr_pct > 0.5:
        return make("FLAT", "MEDIUM", f"ATR alto ({atr_pct:.2f}%) — demasiado riesgo", ["Alta volatilidad"])

    # ── Gestión de posición existente ─────────────────────────
    if current_position == "LONG":
        if ind["ema_trend"] == "bearish" and macd_h < 0 and not ind["cvd_bullish"]:
            return make("FLAT", "HIGH", "EMA+MACD+CVD giraron bearish → salida", ["Reversión confirmada"])
        return make("HOLD", "MEDIUM", f"LONG activo | ADX:{ind['adx']:.0f} CVD:{'↑' if ind['cvd_bullish'] else '↓'}", [f"RSI {rsi:.0f}"])

    if current_position == "SHORT":
        if ind["ema_trend"] == "bullish" and macd_h > 0 and not ind["cvd_bearish"]:
            return make("FLAT", "HIGH", "EMA+MACD+CVD giraron bullish → salida", ["Reversión confirmada"])
        return make("HOLD", "MEDIUM", f"SHORT activo | ADX:{ind['adx']:.0f} CVD:{'↓' if ind['cvd_bearish'] else '↑'}", [f"RSI {rsi:.0f}"])

    # ── Detectar régimen ───────────────────────────────────────
    regime = detect_regime(ind)

    # ── Filtrar régimen según escenario ────────────────────────
    if scenario:
        allowed = scenario.get("sc_regimes", ["TREND", "RANGE", "BREAKOUT", "MIXED"])
        if not allowed:
            return make("FLAT", "MEDIUM", f"Escenario {scenario['name']} — scalping pausado", ["Volatilidad extrema"])
        if regime not in allowed:
            return make("FLAT", "MEDIUM", f"Régimen {regime} desactivado en {scenario['name']}", [f"Permitidos: {','.join(allowed)}"])

    # ══════════════════════════════════════════════════════════
    # MODO BREAKOUT — volumen explosivo (>2.5x)
    # ══════════════════════════════════════════════════════════
    if regime == "BREAKOUT":
        candle_body = price - ind.get("open", price)  # no lo tenemos fácil, usar MACD
        if macd_h > 0 and rsi < 75:  # CVD relaxed for breakout
            return make("LONG", "HIGH",
                        f"Breakout LONG | Vol:{vol:.1f}x | CVD↑ | MACD:{macd_h:+.1f}",
                        [f"Vol {vol:.1f}x ✓", "CVD alcista ✓", f"MACD {macd_h:+.1f}"])
        if macd_h < 0 and rsi > 25:  # CVD relaxed for breakout
            return make("SHORT", "HIGH",
                        f"Breakout SHORT | Vol:{vol:.1f}x | CVD↓ | MACD:{macd_h:+.1f}",
                        [f"Vol {vol:.1f}x ✓", "CVD bajista ✓", f"MACD {macd_h:+.1f}"])
        return make("FLAT", "MEDIUM", f"Breakout sin dirección clara (CVD{'↑' if ind['cvd_bullish'] else '↓'} MACD:{macd_h:+.1f})", ["Sin confluencia"])

    # ══════════════════════════════════════════════════════════
    # MODO TREND — ADX > 25, seguir momentum
    # ══════════════════════════════════════════════════════════
    if regime == "TREND":
        long_score, long_sigs = 0, []
        short_score, short_sigs = 0, []

        if ind["cross_bullish"]:          long_score += 2;  long_sigs.append("EMA cross ✓")
        elif ind["ema_trend"] == "bullish": long_score += 1; long_sigs.append("EMA bullish")
        if ind["cross_bearish"]:          short_score += 2; short_sigs.append("EMA cross ✓")
        elif ind["ema_trend"] == "bearish": short_score += 1; short_sigs.append("EMA bearish")

        if 42 <= rsi <= 65:   long_score += 1;  long_sigs.append(f"RSI {rsi:.0f}")
        if 35 <= rsi <= 58:   short_score += 1; short_sigs.append(f"RSI {rsi:.0f}")

        if ind["price_vs_vwap"] > 0.05:  long_score += 1;  long_sigs.append(f"VWAP +{ind['price_vs_vwap']:.2f}%")
        if ind["price_vs_vwap"] < -0.05: short_score += 1; short_sigs.append(f"VWAP {ind['price_vs_vwap']:.2f}%")

        if vol > 1.2:   long_score += 1;  long_sigs.append(f"Vol {vol:.1f}x"); short_score += 1; short_sigs.append(f"Vol {vol:.1f}x")

        if macd_h > 0 and macd_h > ind["macd_prev"]: long_score += 1;  long_sigs.append("MACD ↑")
        if macd_h < 0 and macd_h < ind["macd_prev"]: short_score += 1; short_sigs.append("MACD ↓")

        # CVD como confirmador extra (+1 si alineado, bloquea si diverge)
        if ind["cvd_bullish"]:  long_score += 1;  long_sigs.append("CVD ↑")
        if ind["cvd_bearish"]:  short_score += 1; short_sigs.append("CVD ↓")

        log.info(f"  [TREND ADX:{ind['adx']:.0f}] L:{long_score} S:{short_score} | EMA:{ind['ema_trend']} RSI:{rsi:.0f} CVD:{'↑' if ind['cvd_bullish'] else '↓'} Vol:{vol:.1f}x")

        if long_score >= 2 and long_score > short_score:
            # CVD diverge fuerte: BLOQUEAR trade
            if ind["cvd_divergence"] and not ind["cvd_bullish"]:
                log.info(f"  ⛔ LONG bloqueado por CVD divergencia")
                return make("FLAT", "MEDIUM", "CVD diverge contra LONG", ["CVD bloqueó señal"])
            conf = "HIGH" if long_score >= 4 else "MEDIUM"
            return make("LONG", conf, f"TREND LONG score:{long_score} | {' | '.join(long_sigs[:3])}", long_sigs)

        if short_score >= 2 and short_score > long_score:
            # CVD diverge fuerte: BLOQUEAR trade
            if ind["cvd_divergence"] and not ind["cvd_bearish"]:
                log.info(f"  ⛔ SHORT bloqueado por CVD divergencia")
                return make("FLAT", "MEDIUM", "CVD diverge contra SHORT", ["CVD bloqueó señal"])
            conf = "HIGH" if short_score >= 4 else "MEDIUM"
            return make("SHORT", conf, f"TREND SHORT score:{short_score} | {' | '.join(short_sigs[:3])}", short_sigs)

        return make("FLAT", "MEDIUM", f"TREND sin confluencia (L:{long_score} S:{short_score})", ["Esperando señal"])

    # ══════════════════════════════════════════════════════════
    # MODO RANGE — ADX < 20, mean reversion en Bollinger Bands
    # ══════════════════════════════════════════════════════════
    if regime == "RANGE":
        bb_pct = ind["bb_pct"]
        log.info(f"  [RANGE ADX:{ind['adx']:.0f}] BB%:{bb_pct:.2f} RSI:{rsi:.0f} CVD:{'↑' if ind['cvd_bullish'] else '↓'}")

        # LONG en banda inferior: precio sobrevendido, CVD empieza a acumular
        if bb_pct < 0.20 and rsi < 45 and ind["cvd_bullish"]:
            return make("LONG", "HIGH",
                        f"RANGE reversion LONG | BB:{bb_pct:.2f} RSI:{rsi:.0f} CVD↑",
                        [f"BB lower {bb_pct:.2f} ✓", f"RSI {rsi:.0f} sobrevendido", "CVD acumulando ✓"])

        if bb_pct < 0.30 and rsi < 42:
            return make("LONG", "MEDIUM",
                        f"RANGE reversion LONG | BB:{bb_pct:.2f} RSI:{rsi:.0f}",
                        [f"BB lower {bb_pct:.2f}", f"RSI {rsi:.0f}"])

        # SHORT en banda superior: precio sobrecomprado, CVD empieza a distribuir
        if bb_pct > 0.80 and rsi > 55 and ind["cvd_bearish"]:
            return make("SHORT", "HIGH",
                        f"RANGE reversion SHORT | BB:{bb_pct:.2f} RSI:{rsi:.0f} CVD↓",
                        [f"BB upper {bb_pct:.2f} ✓", f"RSI {rsi:.0f} sobrecomprado", "CVD distribuyendo ✓"])

        if bb_pct > 0.70 and rsi > 58:
            return make("SHORT", "MEDIUM",
                        f"RANGE reversion SHORT | BB:{bb_pct:.2f} RSI:{rsi:.0f}",
                        [f"BB upper {bb_pct:.2f}", f"RSI {rsi:.0f}"])

        return make("FLAT", "MEDIUM", f"RANGE sin extremo (BB:{bb_pct:.2f} RSI:{rsi:.0f}) — esperando", ["En zona media"])

    # MIXED — ADX 20-25, criterios más estrictos (score >= 4 o HIGH de range)
    bb_pct = ind["bb_pct"]
    long_score = 0
    if ind["cross_bullish"]:           long_score += 2
    elif ind["ema_trend"] == "bullish": long_score += 1
    if 42 <= rsi <= 60:                long_score += 1
    if ind["price_vs_vwap"] > 0.05:   long_score += 1
    if macd_h > 0 and macd_h > ind["macd_prev"]: long_score += 1
    if ind["cvd_bullish"]:             long_score += 1

    short_score = 0
    if ind["cross_bearish"]:            short_score += 2
    elif ind["ema_trend"] == "bearish": short_score += 1
    if 40 <= rsi <= 58:                 short_score += 1
    if ind["price_vs_vwap"] < -0.05:   short_score += 1
    if macd_h < 0 and macd_h < ind["macd_prev"]: short_score += 1
    if ind["cvd_bearish"]:              short_score += 1

    log.info(f"  [MIXED ADX:{ind['adx']:.0f}] L:{long_score} S:{short_score} | BB:{bb_pct:.2f}")

    if long_score >= 3 and long_score > short_score:
        return make("LONG", "MEDIUM", f"MIXED LONG score:{long_score}", [f"ADX:{ind['adx']:.0f}", "CVD alineado"])
    if short_score >= 3 and short_score > long_score:
        return make("SHORT", "MEDIUM", f"MIXED SHORT score:{short_score}", [f"ADX:{ind['adx']:.0f}", "CVD alineado"])

    return make("FLAT", "MEDIUM", f"MIXED — esperando definición (ADX:{ind['adx']:.0f})", ["Transición de régimen"])


# ─── Trailing Stop Dinámico (ATR-based) ──────────────────────────────────────
def update_trailing_stop(open_trade, price: float, atr: float = 0) -> Optional[float]:
    """
    Trailing dinámico que acompaña el movimiento del precio.

    Lógica:
    1. Trackea best_price (máximo para LONG, mínimo para SHORT)
    2. El SL se calcula desde best_price (NO desde entry)
    3. La distancia se reduce progresivamente a medida que el profit crece:
       - Profit < 0.3%  → no se mueve (dejar respirar)
       - Profit 0.3-0.5% → SL = best_price - ATR × 2.0 (breakeven zone)
       - Profit 0.5-1.0% → SL = best_price - ATR × 1.5 (lock profit)
       - Profit > 1.0%   → SL = best_price - ATR × 1.0 (tight trail)
    4. El SL nunca retrocede (solo sube para LONG, solo baja para SHORT)
    """
    if not open_trade or atr <= 0:
        return None

    entry = open_trade.entry_price
    sl    = open_trade.stop_loss
    side  = open_trade.side

    # ── Actualizar best_price ────────────────────────────────────
    if open_trade.best_price is None:
        open_trade.best_price = price
    if side == "LONG":
        open_trade.best_price = max(open_trade.best_price, price)
    else:
        open_trade.best_price = min(open_trade.best_price, price)

    best = open_trade.best_price
    profit_pct = (best - entry) / entry if side == "LONG" else (entry - best) / entry

    # ── No mover si profit < 0.6% (dejar respirar el trade) ─────
    if profit_pct < 0.006:
        return None

    # ── Distancia dinámica basada en ATR + profit ────────────────
    if profit_pct < 0.005:
        trail_dist = atr * 1.5       # zona breakeven (era 2.0)
    elif profit_pct < 0.010:
        trail_dist = atr * 1.0       # lock profit (era 1.5)
    else:
        trail_dist = atr * 0.75      # trailing ajustado (era 1.0)

    # ── Calcular nuevo SL desde best_price ───────────────────────
    if side == "LONG":
        candidate = round(best - trail_dist, 2)
        # Nunca retroceder y nunca peor que entry
        if candidate > sl:
            return candidate
    else:
        candidate = round(best + trail_dist, 2)
        if candidate < sl:
            return candidate

    return None


# ─── Ciclo Principal ──────────────────────────────────────────────────────────
def run_cycle(client, paper):
    log.info(f"── CICLO {datetime.now().strftime('%H:%M:%S')} ──────────────────────────")

    # 0. MEJORA 5: Hour Filter — liquidity recheck cada 30 min fuera de peak hours
    now = datetime.now(timezone.utc)
    PEAK_HOURS_UTC = list(range(9, 21))  # 09:00-21:00 UTC (peak liquidity)

    next_check = None
    if paper.state.next_liquidity_check:
        try:
            next_check = datetime.fromisoformat(paper.state.next_liquidity_check)
        except Exception:
            next_check = None

    if now.hour not in PEAK_HOURS_UTC:
        if next_check and now < next_check:
            remaining = int((next_check - now).total_seconds() / 60)
            log.warning(f"  ⏸ Fuera de peak hours ({now.hour:02d}:00 UTC) — próxima verificación en {remaining}m")
            paper.add_log(f"⏸ Peak hours (09:00-21:00 UTC), next check in {remaining}m")
            return

        paper.state.next_liquidity_check = (now + timedelta(minutes=30)).isoformat()
        log.warning(f"  ⏸ Fuera de peak hours ({now.hour:02d}:00 UTC) — próxima verificación en 30m")
        paper.add_log("⏸ Peak hours (09:00-21:00 UTC), next liquidity check in 30m")
        paper.save()
        return

    if paper.state.next_liquidity_check:
        paper.state.next_liquidity_check = None
        paper.save()

    # 0a. Verificar pausa por drawdown
    from drawdown_monitor import is_paused
    if is_paused(SCALPING_STATE):
        log.warning("  ⛔ BOT PAUSADO por drawdown — no operando")
        paper.add_log("⛔ PAUSADO por drawdown")
        paper.save()
        return

    # 0. Cierre manual desde dashboard
    try:
        raw = _json.loads(SCALPING_STATE.read_text()) if SCALPING_STATE.exists() else {}
        if raw.get("manual_close"):
            log.info("🛑 Cierre manual solicitado")
            if paper.get_scalping_position():
                df_tmp = fetch_1m(client, limit=5)
                paper.close_scalping_position(float(df_tmp["close"].iloc[-1]), "MANUAL")
            raw["manual_close"] = False
            raw["cooldown_until"] = (datetime.now() + timedelta(minutes=5)).isoformat()
            SCALPING_STATE.write_text(_json.dumps(raw, indent=2))
    except Exception as e:
        log.warning(f"Error en cierre manual: {e}")

    # 1. Datos e indicadores PRIMERO (necesitamos precio real)
    df  = fetch_1m(client)
    ind = calc_indicators(df)
    price = ind["price"]

    # 2. Verificar stops con precio REAL (no con 0)
    paper.check_scalping_stops(price)
    paper.update_breakeven_stop(price)  # MEJORA 1: Breakeven Stop
    open_trade = paper.get_scalping_position()
    capital    = paper.state.current_capital + sum(t.size for t in paper.state.open_trades if t.bot == "scalping")
    current_pos = open_trade.side if open_trade else "FLAT"

    log.info(f"  BTC ${price:,.2f} | Regime:{detect_regime(ind)} ADX:{ind['adx']:.0f} | EMA:{ind['ema_trend']} RSI:{ind['rsi']} CVD:{'↑' if ind['cvd_bullish'] else '↓'} Vol:{ind['vol_ratio']:.1f}x | Pos:{current_pos}")

    # 3. Trailing stop dinámico (ATR-based)
    new_sl = update_trailing_stop(open_trade, price, atr=ind["atr"])
    if new_sl:
        move_pct = abs(price - open_trade.entry_price) / open_trade.entry_price * 100
        best = open_trade.best_price or price
        dist_pct = abs(best - new_sl) / best * 100
        log.info(f"  📈 Trailing → SL ${new_sl:,.2f} (profit +{move_pct:.2f}%, best ${best:,.2f}, dist {dist_pct:.3f}%)")
        open_trade.stop_loss = new_sl
        paper.save()

    # 4. Actualizar datos en state para el dashboard
    paper.update_market_data(
        btc_price=price, rsi=ind["rsi"],
        trend=ind["ema_trend"], macd_cross="bullish" if ind["macd_hist"] > 0 else "bearish",
        vol_ratio=ind["vol_ratio"],
    )

    # 5. Verificar cooldown
    try:
        raw = _json.loads(SCALPING_STATE.read_text()) if SCALPING_STATE.exists() else {}
        cooldown = raw.get("cooldown_until")
        if cooldown and datetime.fromisoformat(cooldown) > datetime.now():
            remaining = int((datetime.fromisoformat(cooldown) - datetime.now()).total_seconds() / 60)
            log.info(f"  ⏸ Cooldown activo — {remaining} min restantes")
            paper.add_log(f"Cooldown — {remaining} min restantes")
            paper.save()
            return
    except Exception: pass

    # 6. Detectar escenario + decisión
    from market_scenario import detect_scenario
    scenario = detect_scenario(client)
    log.info(f"  Escenario: {scenario['name']} | bias={scenario['direction']} | regimes={scenario.get('sc_regimes', [])}")

    decision = analyze(ind, current_pos, scenario=scenario)
    action   = decision["decision"]
    conf     = decision["confidence"]
    log.info(f"  → {action} [{conf}] | {decision['reasoning']}")

    # 7. Ejecutar
    if action == "HOLD":
        paper.add_log(f"HOLD — {decision['reasoning'][:60]}")

    elif action == "FLAT":
        if open_trade:
            min_hold_sec = scenario.get("sc_min_hold_sec", MIN_HOLD_SECS)
            held_secs = (datetime.now() - datetime.fromisoformat(open_trade.entry_time[:19])).total_seconds()
            # Check if trade is in profit
            if open_trade.side == "LONG":
                in_profit = price > open_trade.entry_price
            else:
                in_profit = price < open_trade.entry_price
            if held_secs < min_hold_sec:
                remaining = int(min_hold_sec - held_secs)
                paper.add_log(f"HOLD forzado — min hold {remaining}s ({scenario['name']})")
            elif in_profit:
                # En profit → NO cerrar por SIGNAL, dejar que trailing stop haga su trabajo
                paper.add_log(f"HOLD — en profit, trailing stop decide salida")
            else:
                # FIX 3: Requrir confluencia para SIGNAL exit
                # EMA cross + MACD + CVD deben estar TODOS contra posición
                should_close = False
                if open_trade.side == "LONG":
                    # Para cerrar LONG: bearish total
                    if ind["ema_trend"] == "bearish" and ind["macd_hist"] < 0 and ind["cvd_slope"] < 0:
                        should_close = True
                else:
                    # Para cerrar SHORT: bullish total
                    if ind["ema_trend"] == "bullish" and ind["macd_hist"] > 0 and ind["cvd_slope"] > 0:
                        should_close = True
                
                if should_close:
                    paper.close_scalping_position(price, "SIGNAL")
                    paper.state.last_signal_exit = datetime.now().isoformat()
                    # FIX 4: Cooldown 3 min post-SIGNAL para evitar re-entry inmediato
                    raw = _json.loads(SCALPING_STATE.read_text()) if SCALPING_STATE.exists() else {}
                    raw["cooldown_until"] = (datetime.now() + timedelta(minutes=3)).isoformat()
                    SCALPING_STATE.write_text(_json.dumps(raw, indent=2))
                else:
                    paper.add_log(f"SIGNAL parcial, no cierro (confluencia insuficiente)")
        else:
            paper.add_log(f"FLAT — {decision['reasoning'][:60]}")

    elif action in ("LONG", "SHORT"):
        if conf == "LOW":
            paper.add_log("LOW confidence — esperando")
        else:
            # Circuit breaker por dirección: SLs del mismo lado en últimas 2h
            try:
                raw2 = _json.loads(SCALPING_STATE.read_text()) if SCALPING_STATE.exists() else {}
                cutoff = (datetime.now() - timedelta(hours=2)).isoformat()
                scalp_trades = [
                    t for t in paper.state.closed_trades
                    if getattr(t, "bot", "") == "scalping"
                    and getattr(t, "exit_time", "9999") >= cutoff
                ]
                # Consecutivos del mismo lado
                consecutive_sl = 0
                for t in reversed(scalp_trades):
                    if getattr(t, "exit_reason") == "STOP_LOSS" and getattr(t, "side") == action and (getattr(t, "pnl", 0) or 0) < 0:
                        consecutive_sl += 1
                    else:
                        break
                # Total SLs con pérdida del mismo lado en 2h
                sl_same_2h = sum(
                    1 for t in scalp_trades
                    if getattr(t, "exit_reason") == "STOP_LOSS" and getattr(t, "side") == action and (getattr(t, "pnl", 0) or 0) < 0
                )
                if consecutive_sl >= 2 or sl_same_2h >= 3:
                    pause_min = 5
                    blocked_key = f"blocked_{action.lower()}_until"
                    raw2[blocked_key] = (datetime.now() + timedelta(minutes=pause_min)).isoformat()
                    SCALPING_STATE.write_text(_json.dumps(raw2, indent=2))
                    log.warning(f"  ⛔ Circuit {action}: {sl_same_2h} SL en 2h — bloqueado {pause_min} min")
                    paper.add_log(f"⛔ Circuit {action} ({sl_same_2h} SL en 2h) — {pause_min} min")
                    paper.save()
                    return
                # Verificar bloqueo previo por dirección
                blocked_key = f"blocked_{action.lower()}_until"
                blocked_until = raw2.get(blocked_key)
                if blocked_until and datetime.fromisoformat(blocked_until) > datetime.now():
                    remaining = int((datetime.fromisoformat(blocked_until) - datetime.now()).total_seconds() / 60)
                    log.info(f"  🚫 {action} bloqueado — {remaining} min restantes")
                    paper.add_log(f"🚫 {action} bloqueado — {remaining} min")
                    paper.save()
                    return
            except Exception as _ce:
                log.warning(f"  Error circuit breaker: {_ce}")

            # Bloqueo por size=0 del escenario (reemplaza macro filter)
            if decision.get("position_size_pct", 0) <= 0:
                log.info(f"  🚫 {action} bloqueado — size=0 en {scenario['name']}")
                paper.add_log(f"🚫 {action} bloqueado ({scenario['name']})")
                paper.save()
                return

            # Cooldown post-SIGNAL: no abrir nuevo trade por 3 min
            last_sig = getattr(paper.state, 'last_signal_exit', None)
            if last_sig:
                try:
                    elapsed = (datetime.now() - datetime.fromisoformat(last_sig[:19])).total_seconds()
                    if elapsed < SIGNAL_COOLDOWN_SECS:
                        remaining = int(SIGNAL_COOLDOWN_SECS - elapsed)
                        paper.add_log(f"Cooldown post-SIGNAL — {remaining}s restantes")
                        paper.save()
                        return
                except Exception:
                    pass

            if open_trade and open_trade.side != action:
                paper.close_scalping_position(price, "SIGNAL")
                paper.state.last_signal_exit = datetime.now().isoformat()
            if not paper.get_scalping_position():
                paper.open_scalping_trade(decision, price, capital, LEVERAGE)

    paper.save()
    log.info(f"  Capital: ${paper.state.current_capital:.2f} | P&L: {paper.state.total_pnl:+.2f} | Win: {paper.state.win_rate:.0f}%")

    # Drawdown check — usar capital + valor en posiciones abiertas
    from drawdown_monitor import check_drawdown
    pos_value = sum(t.size for t in paper.state.open_trades)
    effective_capital = paper.state.current_capital + pos_value
    check_drawdown("scalping", effective_capital, paper.state.initial_capital,
                   paper.state.peak_capital, SCALPING_STATE)


# ─── Run ──────────────────────────────────────────────────────────────────────
def run_forever():
    log.info("🔪 Scalping Bot v2 — BTC 1m — CVD + ADX Regime + Trailing Stop")
    log.info(f"   Modo: {'DRY RUN' if DRY_RUN else '⚠️  REAL'} | Leverage: {LEVERAGE}x | Ciclo: {CYCLE_SECONDS}s")
    log.info(f"   SL: 1.5×ATR (0.4%-1.5%) | TP: SL×2.5 | Capital: ${SCALP_CAPITAL}")

    client = get_client()
    paper  = get_scalping_engine(initial_capital=SCALP_CAPITAL)

    while True:
        try:
            run_cycle(client, paper)
        except KeyboardInterrupt:
            log.info("🛑 Scalping bot detenido")
            break
        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)

        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        client = get_client()
        paper  = get_scalping_engine(initial_capital=SCALP_CAPITAL)
        run_cycle(client, paper)
    else:
        run_forever()
