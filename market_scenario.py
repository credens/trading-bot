"""
Market Scenario Detection
==========================
Detecta el escenario de mercado BTC y devuelve parámetros de trading
adaptados para cada bot. Cache de 5 min para no saturar la API.

Escenarios:
  TREND_STRONG    — tendencia fuerte (gap > 1.5%), solo operar con la tendencia
  TREND_MODERATE  — tendencia moderada (0.5-1.5%), favorecer dirección
  RANGE           — lateral (gap < 0.5%), ambas direcciones, mean reversion OK
  VOLATILE        — volatilidad extrema (ATR alto), reducir tamaño
  CAPITULATION    — caída fuerte + RSI < 25, potencial reversal
"""

import time
import logging
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Cache compartido ────────────────────────────────────────────────────────
_cache = {"scenario": None, "ts": 0}
CACHE_TTL = 300  # 5 min


def detect_scenario(client, symbol="BTCUSDT"):
    """Detecta escenario de mercado. Cachea 5 min."""
    now = time.time()
    if _cache["scenario"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["scenario"]

    try:
        klines = client.futures_klines(symbol=symbol, interval="1h", limit=220)
        df = pd.DataFrame(klines, columns=[
            "ts", "open", "high", "low", "close", "volume",
            "ct", "qv", "trades", "tb", "tbq", "ig"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)

        closes = df["close"]
        high = df["high"]
        low = df["low"]
        price = float(closes.iloc[-1])

        # ── EMA 50/200 ──
        ema50 = float(closes.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(closes.ewm(span=200, adjust=False).mean().iloc[-1])
        macro_gap = (ema200 - ema50) / ema200 * 100  # positivo = bearish
        direction = "bearish" if ema50 < ema200 else "bullish"

        # ── RSI 1h ──
        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rsi_1h = float((100 - 100 / (1 + gain / loss.replace(0, np.nan))).iloc[-1])

        # ── ATR 1h ──
        tr = pd.concat([
            high - low,
            (high - closes.shift()).abs(),
            (low - closes.shift()).abs()
        ], axis=1).max(axis=1)
        atr_pct = float(tr.rolling(14).mean().iloc[-1] / price * 100)

        # ── ADX 1h ──
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        plus_dm[plus_dm < minus_dm] = 0
        minus_dm[minus_dm < plus_dm] = 0
        atr14 = tr.rolling(14).mean()
        plus_di = 100 * plus_dm.rolling(14).mean() / atr14.replace(0, np.nan)
        minus_di = 100 * minus_dm.rolling(14).mean() / atr14.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = float(dx.rolling(14).mean().iloc[-1])

        gap_abs = abs(macro_gap)

        # ── Clasificación ──
        if gap_abs > 0.5 and rsi_1h < 25:
            scenario = _capitulation(direction, macro_gap, rsi_1h, atr_pct, adx)
        elif atr_pct > 2.5:
            scenario = _volatile(direction, macro_gap, rsi_1h, atr_pct, adx)
        elif gap_abs > 1.5:
            scenario = _trend_strong(direction, macro_gap, rsi_1h, atr_pct, adx)
        elif gap_abs > 0.5:
            scenario = _trend_moderate(direction, macro_gap, rsi_1h, atr_pct, adx)
        else:
            scenario = _range(direction, macro_gap, rsi_1h, atr_pct, adx)

        _cache["scenario"] = scenario
        _cache["ts"] = now
        return scenario

    except Exception as e:
        log.warning(f"Error detectando escenario: {e}")
        if _cache["scenario"]:
            return _cache["scenario"]
        return _default_scenario()


# ── Builders de escenario ───────────────────────────────────────────────────

def _base(name, direction, macro_gap, rsi_1h, atr_pct, adx):
    return {
        "name": name,
        "direction": direction,          # "bullish" / "bearish"
        "macro_gap": macro_gap,
        "rsi_1h": rsi_1h,
        "atr_pct": atr_pct,
        "adx": adx,
    }


def _trend_strong(direction, macro_gap, rsi_1h, atr_pct, adx):
    """Gap > 1.5% — tendencia fuerte, favorecer la tendencia pero permitir contra con size reducido."""
    return {
        **_base("TREND_STRONG", direction, macro_gap, rsi_1h, atr_pct, adx),
        # Binance
        "bn_strategies": ["MACD_MOM", "CVD_DIV"],       # sin RSI_VWAP (churn)
        "bn_tp_mult": 1.4,
        "bn_size_with": 0.08,                           # size con la tendencia
        "bn_size_against": 0.03,                         # reducido (antes 0 = bloqueado)
        "bn_min_hold_min": 10,
        "bn_vote_threshold_with": 1,                     # 1/2 basta con la tendencia
        "bn_vote_threshold_against": 3,                   # necesita unanimidad contra
        # Scalping
        "sc_regimes": ["TREND", "BREAKOUT", "MIXED", "RANGE"],  # RANGE permitido para mean reversion
        "sc_tp_mult": 1.3,
        "sc_size_with": 0.50,
        "sc_size_against": 0.10,
        "sc_min_hold_sec": 180,                           # 3 min (era 10 min)
        # Altcoins
        "alt_strategies": ["RANGE", "MOMENTUM", "SQUEEZE_BREAKOUT", "STRONG_TREND"],
        "alt_tp_mult": 1.8,                               # Aumentado para Max Profit
        "alt_trailing_trigger": 0.010,                    # 1.0%
        "alt_size_mult": 1.3,                             # Más agresivo en tendencia fuerte
        "alt_block_counter": False,                       # permitir contra con score alto
        # Test Bot (aggressive scalper 30s)
        "tb_strategies": ["MOM_BURST", "EMA_CVD", "BB_REV"],
        "tb_tp_mult": 1.4,
        "tb_size_with": 0.14,                             # agresivo con tendencia
        "tb_size_against": 0.06,                          # reducido contra tendencia
        "tb_vote_threshold_with": 1,                      # 1 HIGH basta con tendencia
        "tb_vote_threshold_against": 2,                    # 2/3 contra
        "tb_min_hold_min": 3,                             # 3 min (scalper)
    }


def _trend_moderate(direction, macro_gap, rsi_1h, atr_pct, adx):
    """Gap 0.5-1.5% — tendencia moderada, favorecer dirección."""
    return {
        **_base("TREND_MODERATE", direction, macro_gap, rsi_1h, atr_pct, adx),
        "bn_strategies": ["MACD_MOM", "RSI_VWAP", "CVD_DIV"],
        "bn_tp_mult": 1.2,
        "bn_size_with": 0.07,
        "bn_size_against": 0.04,                          # reducido
        "bn_min_hold_min": 5,
        "bn_vote_threshold_with": 1,                      # 1/3 HIGH basta
        "bn_vote_threshold_against": 3,                    # necesita unanimidad
        "sc_regimes": ["TREND", "BREAKOUT", "MIXED", "RANGE"],
        "sc_tp_mult": 1.1,
        "sc_size_with": 0.50,
        "sc_size_against": 0.10,
        "sc_min_hold_sec": 300,                            # 5 min
        "alt_strategies": ["RANGE", "MOMENTUM", "STRONG_TREND"],
        "alt_tp_mult": 1.4,
        "alt_trailing_trigger": 0.008,                     # 0.8%
        "alt_size_mult": 1.1,
        "alt_block_counter": True,
        # Test Bot (aggressive scalper 30s)
        "tb_strategies": ["MOM_BURST", "EMA_CVD", "BB_REV"],
        "tb_tp_mult": 1.2,
        "tb_size_with": 0.12,
        "tb_size_against": 0.07,
        "tb_vote_threshold_with": 1,
        "tb_vote_threshold_against": 2,
        "tb_min_hold_min": 2,                             # 2 min
    }


def _range(direction, macro_gap, rsi_1h, atr_pct, adx):
    """Gap < 0.5% — lateral, ambas direcciones, mean reversion OK."""
    return {
        **_base("RANGE", direction, macro_gap, rsi_1h, atr_pct, adx),
        "bn_strategies": ["MACD_MOM", "RSI_VWAP", "CVD_DIV"],
        "bn_tp_mult": 0.9,                                # TP más corto
        "bn_size_with": 0.06,
        "bn_size_against": 0.06,                           # ambas igual
        "bn_min_hold_min": 3,
        "bn_vote_threshold_with": 2,
        "bn_vote_threshold_against": 2,
        "sc_regimes": ["TREND", "RANGE", "BREAKOUT", "MIXED"],
        "sc_tp_mult": 0.9,
        "sc_size_with": 0.50,
        "sc_size_against": 0.15,
        "sc_min_hold_sec": 180,
        "alt_strategies": ["RANGE", "MEAN_REVERSION", "MOMENTUM"],
        "alt_tp_mult": 1.0,
        "alt_trailing_trigger": 0.005,                     # normal
        "alt_size_mult": 1.0,
        "alt_block_counter": False,
        # Test Bot (aggressive scalper 30s)
        "tb_strategies": ["MOM_BURST", "EMA_CVD", "BB_REV"],
        "tb_tp_mult": 1.0,
        "tb_size_with": 0.10,
        "tb_size_against": 0.10,                           # igual ambas en rango
        "tb_vote_threshold_with": 1,
        "tb_vote_threshold_against": 1,
        "tb_min_hold_min": 2,                             # 2 min (scalper)
    }


def _volatile(direction, macro_gap, rsi_1h, atr_pct, adx):
    """ATR > 2.5% — volatilidad extrema, reducir tamaño."""
    return {
        **_base("VOLATILE", direction, macro_gap, rsi_1h, atr_pct, adx),
        "bn_strategies": ["MACD_MOM"],                     # solo una
        "bn_tp_mult": 1.0,
        "bn_size_with": 0.04,                              # mitad
        "bn_size_against": 0.0,
        "bn_min_hold_min": 8,
        "bn_vote_threshold_with": 1,
        "bn_vote_threshold_against": 99,
        "sc_regimes": [],                                   # sentarse
        "sc_tp_mult": 1.0,
        "sc_size_with": 0.0,
        "sc_size_against": 0.0,
        "sc_min_hold_sec": 600,
        "alt_strategies": ["MOMENTUM"],
        "alt_tp_mult": 1.0,
        "alt_trailing_trigger": 0.010,                      # 1% generoso
        "alt_size_mult": 0.5,
        "alt_block_counter": True,
        # Test Bot — solo momentum (más robusto en volatilidad)
        "tb_strategies": ["MOM_BURST"],
        "tb_tp_mult": 1.0,
        "tb_size_with": 0.06,
        "tb_size_against": 0.0,
        "tb_vote_threshold_with": 1,
        "tb_vote_threshold_against": 99,
        "tb_min_hold_min": 5,                             # 5 min (cauteloso)
    }


def _capitulation(direction, macro_gap, rsi_1h, atr_pct, adx):
    """RSI < 25 + tendencia — capitulación, potencial reversal."""
    # En capitulación, si RSI < 25 el mercado puede rebotar
    # Ser cauteloso: size muy reducido, buscar reversals
    return {
        **_base("CAPITULATION", direction, macro_gap, rsi_1h, atr_pct, adx),
        "bn_strategies": ["RSI_VWAP"],                     # buscar reversal
        "bn_tp_mult": 1.8,                                 # TP ancho por si rebota fuerte
        "bn_size_with": 0.04,
        "bn_size_against": 0.03,                           # permitir reversal chico
        "bn_min_hold_min": 15,
        "bn_vote_threshold_with": 1,
        "bn_vote_threshold_against": 1,
        "sc_regimes": [],                                   # sentarse
        "sc_tp_mult": 1.0,
        "sc_size_with": 0.0,
        "sc_size_against": 0.0,
        "sc_min_hold_sec": 900,
        "alt_strategies": ["MEAN_REVERSION"],
        "alt_tp_mult": 2.0,
        "alt_trailing_trigger": 0.010,
        "alt_size_mult": 0.25,
        "alt_block_counter": False,                         # permitir contra-tendencia (reversal)
        # Test Bot — BB_REV para reversals + MOM_BURST para rebote
        "tb_strategies": ["BB_REV", "MOM_BURST"],
        "tb_tp_mult": 1.8,
        "tb_size_with": 0.06,
        "tb_size_against": 0.05,                           # permitir reversal
        "tb_vote_threshold_with": 1,
        "tb_vote_threshold_against": 1,
        "tb_min_hold_min": 5,                             # 5 min (cauteloso)
    }


def _default_scenario():
    """Fallback si no se puede detectar."""
    return _range("neutral", 0, 50, 1.0, 20)


def is_with_trend(action, scenario):
    """Retorna True si la acción va con la tendencia del escenario."""
    if scenario["direction"] == "bullish":
        return action == "LONG"
    elif scenario["direction"] == "bearish":
        return action == "SHORT"
    return True  # neutral = cualquiera


def get_size(action, scenario, prefix="bn"):
    """Retorna el size según dirección y escenario."""
    if is_with_trend(action, scenario):
        return scenario.get(f"{prefix}_size_with", 0.07)
    return scenario.get(f"{prefix}_size_against", 0.05)
