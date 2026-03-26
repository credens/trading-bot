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

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [SCALP] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
SYMBOL         = "BTCUSDT"
LEVERAGE       = int(os.getenv("LEVERAGE", "3"))
DRY_RUN        = os.getenv("DRY_RUN", "true").lower() == "true"
SCALP_CAPITAL  = float(os.getenv("SCALP_CAPITAL", "500"))
CYCLE_SECONDS  = int(os.getenv("SCALP_CYCLE_SECONDS", "30"))
SL_PCT         = 0.004   # 0.4% mínimo
TP_PCT         = 0.008   # 0.8% mínimo
POS_PCT        = 0.10    # 10% del capital por trade

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

# ─── Paper Trading ────────────────────────────────────────────────────────────
from paper_trading import get_scalping_engine, SCALPING_STATE
import json as _json

# ─── Binance Client ───────────────────────────────────────────────────────────
def get_client():
    from binance.client import Client
    return Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)


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
def analyze(ind: dict, current_position: str) -> dict:
    """Decide: LONG / SHORT / FLAT / HOLD según el régimen de mercado."""
    price   = ind["price"]
    atr_pct = ind["atr_pct"]
    rsi     = ind["rsi"]
    vol     = ind["vol_ratio"]
    macd_h  = ind["macd_hist"]

    # SL dinámico basado en ATR (mín 0.4%, máx 0.6%)
    sl_pct = round(max(min(atr_pct * 1.2 / 100, 0.006), SL_PCT), 5)
    tp_pct = round(sl_pct * 2.0, 5)

    def make(decision, confidence, reasoning, signals):
        return {
            "decision": decision, "confidence": confidence,
            "reasoning": reasoning, "key_signals": signals,
            "entry_price": price, "stop_loss_pct": sl_pct,
            "take_profit_pct": tp_pct, "position_size_pct": POS_PCT,
        }

    # ── Filtro horario: no operar 00:00–06:00 UTC ──────────────
    utc_hour = datetime.now(timezone.utc).hour
    if 0 <= utc_hour < 6:
        return make("FLAT", "MEDIUM", f"Sin sesión activa ({utc_hour:02d}:xx UTC) — esperando", ["Horario bajo"])

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

    # ══════════════════════════════════════════════════════════
    # MODO BREAKOUT — volumen explosivo (>2.5x)
    # ══════════════════════════════════════════════════════════
    if regime == "BREAKOUT":
        candle_body = price - ind.get("open", price)  # no lo tenemos fácil, usar MACD
        if macd_h > 0 and ind["cvd_bullish"] and rsi < 75:
            return make("LONG", "HIGH",
                        f"Breakout LONG | Vol:{vol:.1f}x | CVD↑ | MACD:{macd_h:+.1f}",
                        [f"Vol {vol:.1f}x ✓", "CVD alcista ✓", f"MACD {macd_h:+.1f}"])
        if macd_h < 0 and ind["cvd_bearish"] and rsi > 25:
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

        if long_score >= 3 and long_score > short_score:
            if ind["cvd_divergence"] and not ind["cvd_bullish"]:
                return make("FLAT", "MEDIUM", f"LONG bloqueado — CVD diverge (precio ↑ pero ventas dominan)", ["CVD diverge"])
            if macd_h < 0:
                return make("FLAT", "MEDIUM", f"LONG bloqueado — MACD negativo ({macd_h:+.1f})", ["MACD contradice"])
            conf = "HIGH" if long_score >= 5 else "MEDIUM"
            return make("LONG", conf, f"TREND LONG score:{long_score} | {' | '.join(long_sigs[:3])}", long_sigs)

        if short_score >= 3 and short_score > long_score:
            if ind["cvd_divergence"] and not ind["cvd_bearish"]:
                return make("FLAT", "MEDIUM", f"SHORT bloqueado — CVD diverge (precio ↓ pero compras dominan)", ["CVD diverge"])
            if macd_h > 0:
                return make("FLAT", "MEDIUM", f"SHORT bloqueado — MACD positivo ({macd_h:+.1f})", ["MACD contradice"])
            conf = "HIGH" if short_score >= 5 else "MEDIUM"
            return make("SHORT", conf, f"TREND SHORT score:{short_score} | {' | '.join(short_sigs[:3])}", short_sigs)

        return make("FLAT", "MEDIUM", f"TREND sin confluencia (L:{long_score} S:{short_score})", ["Esperando señal"])

    # ══════════════════════════════════════════════════════════
    # MODO RANGE — ADX < 20, mean reversion en Bollinger Bands
    # ══════════════════════════════════════════════════════════
    if regime == "RANGE":
        bb_pct = ind["bb_pct"]
        log.info(f"  [RANGE ADX:{ind['adx']:.0f}] BB%:{bb_pct:.2f} RSI:{rsi:.0f} CVD:{'↑' if ind['cvd_bullish'] else '↓'}")

        # LONG en banda inferior: precio sobrevendido, CVD empieza a acumular
        if bb_pct < 0.15 and rsi < 40 and ind["cvd_bullish"]:
            return make("LONG", "HIGH",
                        f"RANGE reversion LONG | BB:{bb_pct:.2f} RSI:{rsi:.0f} CVD↑",
                        [f"BB lower {bb_pct:.2f} ✓", f"RSI {rsi:.0f} sobrevendido", "CVD acumulando ✓"])

        if bb_pct < 0.25 and rsi < 38:
            return make("LONG", "MEDIUM",
                        f"RANGE reversion LONG | BB:{bb_pct:.2f} RSI:{rsi:.0f}",
                        [f"BB lower {bb_pct:.2f}", f"RSI {rsi:.0f}"])

        # SHORT en banda superior: precio sobrecomprado, CVD empieza a distribuir
        if bb_pct > 0.85 and rsi > 60 and ind["cvd_bearish"]:
            return make("SHORT", "HIGH",
                        f"RANGE reversion SHORT | BB:{bb_pct:.2f} RSI:{rsi:.0f} CVD↓",
                        [f"BB upper {bb_pct:.2f} ✓", f"RSI {rsi:.0f} sobrecomprado", "CVD distribuyendo ✓"])

        if bb_pct > 0.75 and rsi > 62:
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

    if long_score >= 4 and long_score > short_score and not ind["cvd_divergence"]:
        return make("LONG", "MEDIUM", f"MIXED LONG score:{long_score}", [f"ADX:{ind['adx']:.0f}", "CVD alineado"])
    if short_score >= 4 and short_score > long_score and not ind["cvd_divergence"]:
        return make("SHORT", "MEDIUM", f"MIXED SHORT score:{short_score}", [f"ADX:{ind['adx']:.0f}", "CVD alineado"])

    return make("FLAT", "MEDIUM", f"MIXED — esperando definición (ADX:{ind['adx']:.0f})", ["Transición de régimen"])


# ─── Trailing Stop ────────────────────────────────────────────────────────────
def update_trailing_stop(open_trade, price: float) -> Optional[float]:
    """
    Mueve el SL a breakeven (+0.1%) cuando el trade llega al 50% del TP.
    Retorna el nuevo SL si se movió, None si no.
    """
    if not open_trade:
        return None
    entry = open_trade.entry_price
    sl    = open_trade.stop_loss
    tp    = open_trade.take_profit

    if open_trade.side == "LONG":
        half_tp = entry + (tp - entry) * 0.5
        breakeven = entry * 1.001  # entry + 0.1% para cubrir fees
        if price >= half_tp and sl < breakeven:
            return breakeven
    else:
        half_tp = entry - (entry - tp) * 0.5
        breakeven = entry * 0.999
        if price <= half_tp and sl > breakeven:
            return breakeven
    return None


# ─── Ciclo Principal ──────────────────────────────────────────────────────────
def run_cycle(client, paper):
    log.info(f"── CICLO {datetime.now().strftime('%H:%M:%S')} ──────────────────────────")

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
    open_trade = paper.get_scalping_position()
    capital    = paper.state.current_capital + sum(t.size for t in paper.state.open_trades if t.bot == "scalping")
    current_pos = open_trade.side if open_trade else "FLAT"

    log.info(f"  BTC ${price:,.2f} | Regime:{detect_regime(ind)} ADX:{ind['adx']:.0f} | EMA:{ind['ema_trend']} RSI:{ind['rsi']} CVD:{'↑' if ind['cvd_bullish'] else '↓'} Vol:{ind['vol_ratio']:.1f}x | Pos:{current_pos}")

    # 3. Trailing stop
    new_sl = update_trailing_stop(open_trade, price)
    if new_sl:
        log.info(f"  📈 Trailing stop: SL movido a ${new_sl:,.2f} (breakeven)")
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

    # 6. Decisión
    decision = analyze(ind, current_pos)
    action   = decision["decision"]
    conf     = decision["confidence"]
    log.info(f"  → {action} [{conf}] | {decision['reasoning']}")

    # 7. Ejecutar
    if action == "HOLD":
        paper.add_log(f"HOLD — {decision['reasoning'][:60]}")

    elif action == "FLAT":
        if open_trade:
            paper.close_scalping_position(price, "SIGNAL")
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
                    if getattr(t, "exit_reason") == "STOP_LOSS" and getattr(t, "side") == action:
                        consecutive_sl += 1
                    else:
                        break
                # Total SLs del mismo lado en 2h
                sl_same_2h = sum(
                    1 for t in scalp_trades
                    if getattr(t, "exit_reason") == "STOP_LOSS" and getattr(t, "side") == action
                )
                if consecutive_sl >= 2 or sl_same_2h >= 3:
                    pause_min = 90 if sl_same_2h >= 3 else 30  # 90 min o 30 min
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

            if open_trade and open_trade.side != action:
                paper.close_scalping_position(price, "SIGNAL")
            if not paper.get_scalping_position():
                paper.open_scalping_trade(decision, price, capital, LEVERAGE)

    paper.save()
    log.info(f"  Capital: ${paper.state.current_capital:.2f} | P&L: {paper.state.total_pnl:+.2f} | Win: {paper.state.win_rate:.0f}%")


# ─── Run ──────────────────────────────────────────────────────────────────────
def run_forever():
    log.info("🔪 Scalping Bot v2 — BTC 1m — CVD + ADX Regime + Trailing Stop")
    log.info(f"   Modo: {'DRY RUN' if DRY_RUN else '⚠️  REAL'} | Leverage: {LEVERAGE}x | Ciclo: {CYCLE_SECONDS}s")
    log.info(f"   SL: {SL_PCT*100:.1f}%+ (ATR) | TP: SL×2 | Capital: ${SCALP_CAPITAL}")

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
