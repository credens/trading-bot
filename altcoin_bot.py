"""
Multi-Altcoin Adaptive Bot - v4.0 (BUG FIXES + IMPROVEMENTS)
=============================================================
- PID Lock: Solo 1 instancia puede correr a la vez.
- File Locking: fcntl.flock en state read/write.
- Duplicate Prevention: Re-verifica antes de abrir.
- Dynamic Cooldown: Emergency=30min, SL=10min, default=5min.
- Kelly Sizing: Basado en historial si hay suficientes trades.
- TTP Peak Cap: Limita pico a +15% sobre entry.
- RSI Simplificado: Solo extremos (<25, >75).
- Linear Scoring: Sin multiplicador de volumen.
- Emergency Exit: -10% (era -20%).
- Time Limit: 60min + early exit a 30min si < -5%.
- Long/Short Balance: Hard block a 7 por dirección.
"""

import os
import sys
import json
import time
import fcntl
import atexit
import logging
from datetime import datetime, timedelta, timezone


def _parse_dt(s):
    """Parse ISO datetime string, handling trailing 'Z' for Python 3.10."""
    if s.endswith("Z"):
        s = s[:-1]
    return datetime.fromisoformat(s)
from trade_logger import log_trade as _log_trade
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
import pandas as pd
import numpy as np

load_dotenv(override=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("altcoin.log"),
    ]
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

LEVERAGE         = int(os.getenv("ALTCOIN_LEVERAGE",    "20"))  # base, sobreescrito dinámicamente
MAX_POSITIONS    = int(os.getenv("ALTCOIN_MAX_POSITIONS","10"))
TOTAL_CAPITAL    = float(os.getenv("ALTCOIN_CAPITAL",   "200"))
DRY_RUN          = os.getenv("DRY_RUN", "true").lower() == "true"
INTERVAL_MINUTES = int(os.getenv("ALTCOIN_INTERVAL",   "3"))
MIN_VOLUME_USDT  = float(os.getenv("ALTCOIN_MIN_VOLUME","500000000"))  # $500M mínimo
TOP_N            = int(os.getenv("ALTCOIN_TOP_N",       "30"))
CANDLE_INTERVAL  = os.getenv("ALTCOIN_CANDLE", "5m")
DEFAULT_SL_PCT   = float(os.getenv("ALTCOIN_SL",  "0.012"))     # SL fallback 1.2%
DEFAULT_TP_PCT   = float(os.getenv("ALTCOIN_TP",  "0.040"))     # TP fallback 4.0%
TRAILING_TRIGGER = float(os.getenv("ALTCOIN_TRAIL", "0.008"))   # trailing trigger fallback
TP_CALLBACK_PCT  = float(os.getenv("ALTCOIN_TP_CALLBACK", "0.003"))  # TTP callback fallback
TIME_LIMIT_MIN   = int(os.getenv("ALTCOIN_TIME_LIMIT", "60"))   # cerrar si stale > 60min

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

DATA_DIR = Path(__file__).parent / "altcoin_data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
LOCK_FILE = DATA_DIR / "altcoin_bot.lock"

EXCLUDE = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "USDTUSDT", "BTCUSDT",
           "BTCDOMUSDT", "DEFIUSDT", "BNXUSDT", "1000SHIBUSDT",
           "SIRENUSDT", "LOOMUSDT", "CVPUSDT", "BALUSDT",
           "1000LUNCUSDT", "LUNA2USDT", "ONTUSDT", "RIVERUSDT",
           "HYPEUSDT", "XAGUSDT", "XAUUSDT", "PAXGUSDT", "1000PEPEUSDT",
           # Coins problemáticas: baja liquidez real, gaps enormes
           "BLESSUSDT", "RAVEUSDT", "CLUSDT", "ARIAUSDT", "ENJUSDT",
           "ZECUSDT", "ALPHAUSDT", "XAUTUSDT"}

# ─── Leverage Dinámico ───────────────────────────────────────────────────────

def get_dynamic_leverage(scenario: dict, atr_pct: float, confidence: str) -> int:
    """Leverage según contexto de mercado, volatilidad del coin y confianza de señal."""
    scenario_name = scenario.get("name", "RANGE")

    # Base por escenario de mercado
    base = {
        "TREND_STRONG":   20,
        "TREND_MODERATE": 15,
        "BREAKOUT":       12,
        "RANGE":          10,
        "VOLATILE":        5,
        "CRASH":           3,
    }.get(scenario_name, 10)

    # Reducir por alta volatilidad del coin (ATR)
    if atr_pct > 3.0:
        base = max(3, int(base * 0.40))   # muy volátil → mínimo leverage
    elif atr_pct > 2.0:
        base = max(5, int(base * 0.55))
    elif atr_pct > 1.0:
        base = max(8, int(base * 0.75))

    # Ajuste por confianza de señal
    if confidence == "HIGH":
        base = int(base * 1.25)
    elif confidence == "MEDIUM":
        base = int(base * 0.85)

    return min(20, max(3, base))  # límite: 3x–20x


# ─── PID Lock ────────────────────────────────────────────────────────────────

_lock_fd = None

def acquire_lock():
    """Previene múltiples instancias del bot corriendo simultáneamente."""
    global _lock_fd
    _lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        log.info(f"🔒 Lock adquirido (PID {os.getpid()})")
    except BlockingIOError:
        log.error("⛔ Otra instancia del bot ya está corriendo. Saliendo.")
        sys.exit(1)
    atexit.register(lambda: _lock_fd.close())

# ─── Binance Client ──────────────────────────────────────────────────────────

def get_client():
    from binance.client import Client
    # Aumentar el timeout de 10s (default) a 30s para evitar ReadTimeoutError
    return Client(BINANCE_API_KEY, BINANCE_SECRET_KEY, requests_params={'timeout': 30})

# ─── Market Scanner ──────────────────────────────────────────────────────────

def get_top_altcoins(client, n: int = TOP_N) -> list[dict]:
    try:
        tickers = client.futures_ticker()
        altcoins = []
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT") or sym in EXCLUDE: continue
            volume = float(t.get("quoteVolume", 0))
            if volume < MIN_VOLUME_USDT: continue
            altcoins.append({
                "symbol": sym, "price": float(t["lastPrice"]),
                "volume_24h": volume, "change_24h": float(t["priceChangePercent"]),
            })
        altcoins.sort(key=lambda x: x["volume_24h"], reverse=True)
        return altcoins[:n]
    except Exception as e:
        log.error(f"Error scanner: {e}")
        return []

# ─── Indicadores Expertos ─────────────────────────────────────────────────────

def get_indicators(client, symbol: str) -> Optional[dict]:
    try:
        klines = client.futures_klines(symbol=symbol, interval=CANDLE_INTERVAL, limit=120)
        df = pd.DataFrame(klines, columns=["ts","open","high","low","close","volume","ct","qv","trades","tbb","tbq","ignore"])
        for col in ["open","high","low","close","volume"]: df[col] = df[col].astype(float)

        close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]

        # EMA Setup
        ema9  = close.ewm(span=9,  adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        cross_bull = ema9.iloc[-2] <= ema21.iloc[-2] and ema9.iloc[-1] > ema21.iloc[-1]
        cross_bear = ema9.iloc[-2] >= ema21.iloc[-2] and ema9.iloc[-1] < ema21.iloc[-1]

        # VWAP
        tp = (high + low + close) / 3
        vol_sum = volume.rolling(78).sum().iloc[-1]
        vwap = float((tp * volume).rolling(78).sum().iloc[-1] / vol_sum) if vol_sum > 0 else float(close.iloc[-1])
        p_vs_v = ((close.iloc[-1] - vwap) / vwap * 100) if vwap > 0 else 0

        # RSI 14
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rsi = float((100 - (100 / (1 + gain / loss.replace(0, np.nan)))).iloc[-1])

        # MACD Scalper (3,10,5)
        macd = close.ewm(span=3, adjust=False).mean() - close.ewm(span=10, adjust=False).mean()
        signal = macd.ewm(span=5, adjust=False).mean()
        macd_h = float((macd - signal).iloc[-1])
        macd_c = "bullish" if macd_h > 0 and (macd-signal).iloc[-2] <= 0 else "bearish" if macd_h < 0 and (macd-signal).iloc[-2] >= 0 else "neutral"

        # Bollinger & ATR
        sma20, std20 = close.rolling(20).mean(), close.rolling(20).std()
        std_val = std20.iloc[-1]
        bb_pct = float(((close.iloc[-1] - (sma20.iloc[-1] - 2*std_val)) / (4*std_val))) if std_val > 0 else 0.5

        tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
        atr_pct = float(tr.rolling(14).mean().iloc[-1] / close.iloc[-1] * 100)

        v_mean = volume.rolling(20).mean().iloc[-1]
        vol_ratio = float(volume.iloc[-1] / v_mean) if v_mean > 0 else 1.0

        ema50, ema200 = close.ewm(span=50).mean().iloc[-1], close.ewm(span=200).mean().iloc[-1]

        try:
            funding = client.futures_funding_rate(symbol=symbol, limit=1)
            funding_rate = float(funding[0]["fundingRate"]) * 100 if funding else 0
        except Exception:
            funding_rate = 0

        return {
            "symbol": symbol, "price": close.iloc[-1],
            "ema_trend": "bullish" if ema9.iloc[-1] > ema21.iloc[-1] else "bearish",
            "cross_bullish": cross_bull, "cross_bearish": cross_bear,
            "vwap": round(vwap, 6), "price_vs_vwap": round(p_vs_v, 3),
            "rsi": round(rsi, 2), "macd_hist": round(macd_h, 6), "macd_cross": macd_c,
            "bb_pct": round(bb_pct, 3), "atr_pct": round(atr_pct, 3),
            "vol_ratio": round(vol_ratio, 2),
            "trend": "bullish" if ema50 > ema200 else "bearish",
            "funding_rate": funding_rate
        }
    except Exception as e:
        log.warning(f"Error indicators {symbol}: {e}")
        return None

# ─── Scoring Técnico ─────────────────────────────────────────────────────────

def analyze_altcoin(indicators, market_data, capital, open_pos, open_l, open_s, closed_trades=None) -> Optional[dict]:
    score = 0
    sigs = []

    # ── Señales primarias: EMA cross + VWAP ──
    if indicators["cross_bullish"]: score += 3; sigs.append("EMA Cross Up")
    elif indicators["ema_trend"] == "bullish": score += 1

    if indicators["cross_bearish"]: score -= 3; sigs.append("EMA Cross Down")
    elif indicators["ema_trend"] == "bearish": score -= 1

    pvwap = indicators.get("price_vs_vwap", 0)
    if pvwap > 0.1: score += 1; sigs.append(f"VWAP +{pvwap:.2f}%")
    elif pvwap < -0.1: score -= 1; sigs.append(f"VWAP {pvwap:.2f}%")

    # ── Confirmadores: MACD ──
    if indicators["macd_cross"] == "bullish": score += 2; sigs.append("MACD bull")
    elif indicators["macd_cross"] == "bearish": score -= 2; sigs.append("MACD bear")
    elif indicators["macd_hist"] > 0: score += 1
    elif indicators["macd_hist"] < 0: score -= 1

    # ── RSI — solo extremos (simplificado) ──
    rsi = indicators.get("rsi", 50)
    if rsi < 25: score += 2; sigs.append(f"RSI sobrevendido {rsi:.0f}")
    elif rsi > 75: score -= 2; sigs.append(f"RSI sobrecomprado {rsi:.0f}")

    # ── Volumen — aditivo, no multiplicativo (linear scoring) ──
    vol_ratio = indicators.get("vol_ratio", 1)
    if vol_ratio > 1.8:
        score += 1 if score > 0 else -1
        sigs.append(f"Vol alto {vol_ratio:.1f}x")

    # ── Funding rate ──
    funding = indicators.get("funding_rate", 0)
    if funding > 0.03: score -= 1; sigs.append("Funding alto")
    elif funding < -0.03: score += 1; sigs.append("Funding neg")

    # ── Sobreextensión 24h ──
    change_24h = market_data.get("change_24h", 0)
    if change_24h > 12: score -= 2; sigs.append(f"Pump +{change_24h:.1f}%")
    elif change_24h < -12: score += 2; sigs.append(f"Dump {change_24h:.1f}%")

    # ── Balance soft penalty ──
    if open_l > open_s + 1: score -= 1
    elif open_s > open_l + 1: score += 1

    # ── Hard block: máximo 7 posiciones por dirección ──
    if open_l >= 7 and score > 0: return None
    if open_s >= 7 and score < 0: return None

    # ── Threshold mínimo ──
    if abs(score) < 2: return None

    # FIX 5: Respetar bias del escenario - no abrir contra tendencia
    # Para bots multi-asset, simulamos bias bearish como scaler negativo
    direction = "LONG" if score > 0 else "SHORT"
    conf = "HIGH" if abs(score) >= 4 else "MEDIUM"

    # ── Kelly Criterion sizing ──
    trades = (closed_trades or [])[-50:]
    if len(trades) >= 10:
        wins = [t for t in trades if (t.get("pnl") or 0) > 0]
        losses = [t for t in trades if (t.get("pnl") or 0) < 0]
        win_rate = len(wins) / len(trades)
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 1
        avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 1
        b = avg_win / avg_loss if avg_loss > 0 else 1
        kelly = max(0.08, min(0.35, win_rate - (1 - win_rate) / b))  # clamped 8%-35%
        max_position = capital * kelly
    else:
        max_position = capital / 10  # fallback

    size = round(max_position * (1.0 if conf == "HIGH" else 0.7), 2)
    if size < 15: size = 15

    strategy = "EMA_CROSS" if indicators["cross_bullish"] or indicators["cross_bearish"] else \
               "MEAN_REVERSION" if rsi < 30 or rsi > 70 else \
               "MOMENTUM" if indicators["macd_cross"] in ("bullish","bearish") and vol_ratio > 1.5 else "RANGE"

    # ── TP/SL dinámicos basados en ATR ──
    atr_pct = indicators.get("atr_pct", 0) / 100  # viene en %, convertir a decimal
    if atr_pct > 0.003:  # solo si ATR es razonable (>0.3%)
        tp_pct = round(min(max(atr_pct * 3.0, 0.02), 0.12), 4)   # 3x ATR, clamp 2%-12%
        sl_pct = round(min(max(atr_pct * 1.2, 0.005), 0.04), 4)  # 1.2x ATR, clamp 0.5%-4%
        trail_trigger = round(atr_pct * 0.6, 4)                    # 0.6x ATR
        tp_callback = round(min(max(atr_pct * 0.4, 0.002), 0.015), 4)  # 0.4x ATR, clamp 0.2%-1.5%
    else:
        tp_pct = DEFAULT_TP_PCT
        sl_pct = DEFAULT_SL_PCT
        trail_trigger = TRAILING_TRIGGER
        tp_callback = TP_CALLBACK_PCT

    return {
        "strategy": strategy, "direction": direction, "confidence": conf,
        "reasoning": f"Score {score:+d} | {' | '.join(sigs[:3])}",
        "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
        "trailing_trigger": trail_trigger, "tp_callback": tp_callback,
        "position_size_usdt": size,
        "atr_pct": indicators.get("atr_pct", 1.0),
    }

# ─── State Management ────────────────────────────────────────────────────────

def load_state() -> dict:
    default = {
        "initial_capital": TOTAL_CAPITAL, "capital": TOTAL_CAPITAL, "positions": {},
        "closed_trades": [], "total_pnl": 0.0, "cooldowns": {}, "cycle_log": [],
        "manual_close": [], "last_scan": [], "scanning": False,
        "next_liquidity_check": None
    }
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                data = json.load(f)
                fcntl.flock(f, fcntl.LOCK_UN)
            if not isinstance(data.get("positions"), dict): data["positions"] = {}
            if not data.get("closed_trades"): data["capital"] = TOTAL_CAPITAL
            return data
        except:
            return default
    return default

def save_state(state: dict):
    def clean(obj):
        if isinstance(obj, float) and (obj != obj): return 0.0
        if isinstance(obj, dict): return {k: clean(v) for k, v in obj.items()}
        if isinstance(obj, list): return [clean(v) for v in obj]
        return obj

    closed = state.get("closed_trades", [])
    state["total_pnl"] = round(sum(t.get("pnl", 0) for t in closed), 2)
    current_cap = round(TOTAL_CAPITAL + state["total_pnl"] - sum(p["size_usdt"] for p in state["positions"].values()), 2)

    dashboard = {
        **state,
        "bot": "altcoins",
        "capital": current_cap,
        "current_capital": current_cap,
        "initial_capital": TOTAL_CAPITAL,
        "total_pnl": state["total_pnl"],
        "total_pnl_raw": state["total_pnl"],
        "total_pnl_pct": round(state["total_pnl"] / TOTAL_CAPITAL * 100, 2),
        "win_rate": round(len([t for t in closed if t.get("pnl",0) > 0]) / max(1, len(closed)) * 100, 1),
        "total_trades": len(closed),
        "open_positions": list(state["positions"].values()),
        "all_closed_trades": closed,
        "closed_trades": closed[-30:],
        "last_updated": datetime.now().isoformat()
    }

    try:
        tmp = STATE_FILE.parent / f".state_tmp_{os.getpid()}.json"
        with open(tmp, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(clean(dashboard), f, indent=2, default=str)
            fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error(f"Error saving state: {e}")

def add_log(state, msg):
    state.setdefault("cycle_log", []).insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg})
    state["cycle_log"] = state["cycle_log"][:50]

# ─── Trade Engine ────────────────────────────────────────────────────────────

def open_position(client, state, symbol, analysis, indicators, scenario=None):
    price, direction = indicators["price"], analysis["direction"]
    sl_pct = float(analysis.get("stop_loss_pct", DEFAULT_SL_PCT))
    tp_pct = float(analysis.get("take_profit_pct", DEFAULT_TP_PCT))
    sl = round(price * (1 - sl_pct) if direction == "LONG" else price * (1 + sl_pct), 8)
    tp = round(price * (1 + tp_pct) if direction == "LONG" else price * (1 - tp_pct), 8)

    leverage = get_dynamic_leverage(
        scenario or {}, analysis.get("atr_pct", 1.0), analysis["confidence"]
    )
    log.info(f"  📐 Leverage dinámico: {leverage}x (escenario:{(scenario or {}).get('name','?')} ATR:{analysis.get('atr_pct',0):.2f}% conf:{analysis['confidence']})")

    pos = {
        "symbol": symbol, "direction": direction, "strategy": analysis["strategy"],
        "entry_price": price, "entry_time": datetime.now().isoformat(),
        "size_usdt": analysis["position_size_usdt"], "stop_loss": sl, "take_profit": tp,
        "tp_trailing_active": False, "tp_peak_price": price, "best_price": price,
        "leverage": leverage, "trailing_activated": False, "confidence": analysis["confidence"],
        "reasoning": analysis["reasoning"],
        "trailing_trigger": analysis.get("trailing_trigger", TRAILING_TRIGGER),
        "tp_callback": analysis.get("tp_callback", TP_CALLBACK_PCT)
    }

    if DRY_RUN:
        state["positions"][symbol] = pos
        msg = f"✓ PAPER {direction} {symbol} @ ${price:.4f} | {analysis['strategy']} | SL {sl_pct*100:.1f}% | TP {tp_pct*100:.1f}% | ${analysis['position_size_usdt']:.0f}"
        add_log(state, msg)
        log.info(f"  [PAPER] {msg}")
    else:
        try:
            client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
            side = "BUY" if direction == "LONG" else "SELL"
            qty = round(pos["size_usdt"] * LEVERAGE / price, 3)
            client.futures_create_order(symbol=symbol, side=side, type="MARKET", quantity=qty)
            state["positions"][symbol] = pos
            msg = f"✅ REAL {direction} {symbol} @ ${price:.4f} | ${analysis['position_size_usdt']:.0f}"
            add_log(state, msg)
            log.info(f"  [REAL] {msg}")
        except Exception as e:
            log.error(f"Error opening real {symbol}: {e}")

def _close_position(state, symbol, pos, exit_price, exit_reason, note=""):
    entry, direction, lev = pos["entry_price"], pos["direction"], pos.get("leverage", LEVERAGE)
    pnl_pct = ((exit_price - entry) / entry if direction == "LONG" else (entry - exit_price) / entry) * lev
    pnl = round(pos["size_usdt"] * pnl_pct, 2)

    trade = {
        **pos, "exit_price": exit_price, "exit_time": datetime.now().isoformat(),
        "pnl": pnl, "pnl_pct": round(pnl_pct * 100, 2), "exit_reason": exit_reason
    }
    state["closed_trades"].append(trade)

    # Cooldown post-cierre negativo
    if pnl < 0:
        if exit_reason == "EMERGENCY":
            cd_min = 90   # 90 min por EMERGENCY (pump/dump protection)
        elif exit_reason in ("STOP_LOSS", "EARLY_EXIT"):
            cd_min = 10
        else:
            cd_min = 5
        state.setdefault("cooldowns", {})[symbol] = (datetime.now() + timedelta(minutes=cd_min)).isoformat()

    if symbol in state["positions"]:
        del state["positions"][symbol]

    emoji = "✅" if pnl > 0 else "❌"
    msg = f"{emoji} {exit_reason} {symbol}{' '+note if note else ''} | exit ${exit_price:.6f} | P&L {'+' if pnl>=0 else ''}${pnl:.2f} ({pnl_pct*100:+.1f}%)"
    add_log(state, msg)
    log.info(f"  [CLOSE] {msg}")
    try:
        _log_trade({**trade, "bot": "altcoins", "symbol": symbol,
                    "size": trade.get("size_usdt", 0), "leverage": lev})
    except:
        pass

def check_positions(client, state, scenario=None):
    now = datetime.now()
    to_close = []

    for symbol, pos in list(state["positions"].items()):
        try:
            ticker = client.futures_ticker(symbol=symbol)
            curr = float(ticker["lastPrice"])
            entry_price = pos["entry_price"]
            direction = pos["direction"]
            sl = pos["stop_loss"]
            tp_act = pos["take_profit"]
            lev = pos.get("leverage", LEVERAGE)

            # ── 1. TRAILING STOP LOSS ──
            if direction == "LONG":
                pos["best_price"] = max(pos.get("best_price", curr), curr)
            else:
                pos["best_price"] = min(pos.get("best_price", curr), curr)

            pos_trail_trigger = pos.get("trailing_trigger", TRAILING_TRIGGER)
            move = abs(curr - entry_price) / entry_price
            if move >= pos_trail_trigger:
                dist = 0.008 if move < pos_trail_trigger*2 else 0.005 if move < pos_trail_trigger*4 else 0.003
                new_sl = round(pos["best_price"]*(1-dist) if direction=="LONG" else pos["best_price"]*(1+dist), 8)
                if (direction=="LONG" and new_sl > sl) or (direction=="SHORT" and new_sl < sl):
                    pos["stop_loss"] = sl = new_sl
                    pos["trailing_activated"] = True

            # ── 2. TRAILING TAKE PROFIT (TTP) ──
            hit_tp = (direction=="LONG" and curr >= tp_act) or (direction=="SHORT" and curr <= tp_act)
            if hit_tp and not pos.get("tp_trailing_active"):
                pos["tp_trailing_active"] = True
                pos["tp_peak_price"] = curr
                log.info(f"  🚀 TTP ACTIVADO en {symbol}: Precio alcanzó TP")

            pos_tp_callback = pos.get("tp_callback", TP_CALLBACK_PCT)
            if pos.get("tp_trailing_active"):
                if direction == "LONG":
                    pos["tp_peak_price"] = max(pos["tp_peak_price"], curr)
                    # TTP Peak Cap: máximo +15% sobre entry
                    pos["tp_peak_price"] = min(pos["tp_peak_price"], entry_price * 1.15)
                    if curr <= pos["tp_peak_price"] * (1 - pos_tp_callback):
                        to_close.append((symbol, curr, "TRAILING_TP", f"Pico: ${pos['tp_peak_price']:.4f}"))
                        continue
                else:
                    pos["tp_peak_price"] = min(pos["tp_peak_price"], curr)
                    # TTP Peak Cap: máximo -15% bajo entry
                    pos["tp_peak_price"] = max(pos["tp_peak_price"], entry_price * 0.85)
                    if curr >= pos["tp_peak_price"] * (1 + pos_tp_callback):
                        to_close.append((symbol, curr, "TRAILING_TP", f"Pico: ${pos['tp_peak_price']:.4f}"))
                        continue

            # ── 2b. MEJORA 2: Profit Locking ──
            # Si estamos muy en ganancia, reducir TP para asegurar ganancias
            unrealized_pct_for_lock = ((curr - entry_price)/entry_price if direction=="LONG" else (entry_price-curr)/entry_price) * lev
            if unrealized_pct_for_lock >= 0.20:  # 20% de ganancia
                tp = round(entry_price * (1.05 if direction == "LONG" else 0.95), 8)
                if direction == "LONG" and tp < tp_act:
                    pos["take_profit"] = tp
                    log.info(f"  💰 Profit Lock {symbol}: TP bajado a +5% (ganancia asegurada)")
                elif direction == "SHORT" and tp > tp_act:
                    pos["take_profit"] = tp
                    log.info(f"  💰 Profit Lock {symbol}: TP bajado a +5%")
            elif unrealized_pct_for_lock >= 0.10:  # 10% de ganancia
                tp = round(entry_price * (1.03 if direction == "LONG" else 0.97), 8)
                if direction == "LONG" and tp < tp_act:
                    pos["take_profit"] = tp
                elif direction == "SHORT" and tp > tp_act:
                    pos["take_profit"] = tp

            # ── 3. CONDICIONES DE CIERRE ──
            unrealized_pct = ((curr - entry_price)/entry_price if direction=="LONG" else (entry_price-curr)/entry_price) * lev
            hit_sl = (direction=="LONG" and curr <= sl) or (direction=="SHORT" and curr >= sl)
            emergency = unrealized_pct < -0.08  # -8% levered (era -5%) — más espacio al trade

            entry_dt = _parse_dt(pos["entry_time"])
            minutes_open = (now - entry_dt).total_seconds() / 60
            time_expired = minutes_open >= TIME_LIMIT_MIN
            early_exit = minutes_open >= 30 and unrealized_pct < -0.08  # 30min + perdiendo >8%

            if hit_sl or emergency or time_expired or early_exit:
                if emergency:
                    exit_reason = "EMERGENCY"
                elif early_exit:
                    exit_reason = "EARLY_EXIT"
                elif time_expired:
                    exit_reason = "TIME_LIMIT"
                else:
                    exit_reason = "STOP_LOSS"
                to_close.append((symbol, curr, exit_reason, ""))
            else:
                state["positions"][symbol] = pos  # guardar updates de best_price/peak

        except Exception as e:
            log.error(f"Error check {symbol}: {e}")

    for s, p, r, note in to_close:
        if s in state["positions"]:
            _close_position(state, s, state["positions"][s], p, r, note)

# ─── Ciclo Principal ─────────────────────────────────────────────────────────

def run_cycle(client):
    log.info(f"\n{'='*40}\nALTCOIN CYCLE - {datetime.now().strftime('%H:%M:%S')}\n{'='*40}")
    
    state = load_state()
    now_utc = datetime.now(timezone.utc)

    if state.get("next_liquidity_check"):
        state["next_liquidity_check"] = None
        save_state(state)

    now = datetime.now()

    # Daily loss limit: si el P&L del día supera -5% del capital, pausar hasta mañana
    today_str = now.strftime("%Y-%m-%d")
    today_trades = [t for t in state.get("closed_trades", [])
                    if (t.get("exit_time") or t.get("entry_time") or "").startswith(today_str)]
    today_pnl = sum(t.get("pnl") or 0 for t in today_trades)
    capital = state.get("current_capital", TOTAL_CAPITAL)
    if today_pnl < -(capital * 0.05):
        log.warning(f"  🛑 Daily loss limit alcanzado: {today_pnl:.2f} ({today_pnl/capital*100:.1f}%) — pausando hasta mañana")
        return

    # Cooldown cleanup
    state["cooldowns"] = {s: t for s, t in state.get("cooldowns", {}).items()
                          if _parse_dt(t) > now}

    from drawdown_monitor import is_paused
    if is_paused(STATE_FILE):
        log.warning("⛔ BOT PAUSADO por drawdown")
        return

    # Manual closes
    for sym in list(state.get("manual_close", [])):
        if sym in state["positions"]:
            try:
                t = client.futures_ticker(symbol=sym)
                _close_position(state, sym, state["positions"][sym], float(t["lastPrice"]), "MANUAL")
            except Exception as e:
                log.error(f"Error cerrando {sym}: {e}")
    state["manual_close"] = []

    from market_scenario import detect_scenario, is_with_trend
    scenario = detect_scenario(client)
    check_positions(client, state, scenario)

    capital = state.get("capital", TOTAL_CAPITAL)
    slots = MAX_POSITIONS - len(state["positions"])

    log.info(f"Capital: ${capital:.2f} | Posiciones: {len(state['positions'])}/{MAX_POSITIONS} | Cooldowns: {len(state['cooldowns'])}")
    log.info(f"P&L total: ${state.get('total_pnl', 0):+.2f}")

    if slots <= 0:
        log.info("Máximo posiciones alcanzado. Esperando...")
        save_state(state)
        return

    # Scan
    state["scanning"] = True
    state["last_scan"] = []
    save_state(state)

    coins = get_top_altcoins(client)
    # FILTRO: no abierta ni en cooldown
    candidates = [c for c in coins if c["symbol"] not in state["positions"] and c["symbol"] not in state["cooldowns"]]

    # Blacklist dinámica: symbols con <40% win rate después de 8+ trades
    sym_stats = {}
    for t in state.get("closed_trades", []):
        s = t.get("symbol", "")
        if not s: continue
        st = sym_stats.setdefault(s, {"wins": 0, "total": 0})
        st["total"] += 1
        if t.get("pnl", 0) > 0: st["wins"] += 1
    blacklist = {s for s, st in sym_stats.items() if st["total"] >= 8 and (st["wins"] / st["total"]) < 0.40}
    if blacklist:
        log.info(f"  🚫 Blacklist ({len(blacklist)}): {sorted(blacklist)}")
    candidates = [c for c in candidates if c["symbol"] not in blacklist]

    log.info(f"  Escenario: {scenario['name']} | bias={scenario['direction']}")
    state["btc_macro"] = scenario["direction"]
    log.info(f"\nAnalizando {min(len(candidates), slots*3)} candidatos...")

    opps = []
    for c in candidates[:slots*3]:
        inds = get_indicators(client, c["symbol"])
        if not inds: continue

        open_l = sum(1 for p in state["positions"].values() if p["direction"]=="LONG")
        open_s = sum(1 for p in state["positions"].values() if p["direction"]=="SHORT")

        ana = analyze_altcoin(inds, c, capital, len(state["positions"]), open_l, open_s,
                              closed_trades=state.get("closed_trades", []))
        if ana:
            # Filtro por escenario
            allowed = scenario.get("alt_strategies", ["RANGE","MOMENTUM","MEAN_REVERSION"])
            if ana["strategy"] not in allowed and ana["strategy"] not in ("EMA_CROSS",):
                continue
            
            # FIX 5: Respetar bias del escenario - bloquear SHORTs en bullish, LONGs en bearish (excepto en RANGE)
            scenario_name = scenario.get("name", "RANGE")
            if scenario_name in ("TREND_STRONG", "TREND_MODERATE"):
                if not is_with_trend(ana["direction"], scenario):
                    log.info(f"    ⛔ {c['symbol']} {ana['direction']} bloqueado: contra tendencia en {scenario_name}")
                    continue
            
            if scenario.get("alt_block_counter") and not is_with_trend(ana["direction"], scenario):
                continue

            # Aplicar multiplicadores del escenario
            ana["take_profit_pct"] = ana["take_profit_pct"] * scenario.get("alt_tp_mult", 1.0)
            ana["position_size_usdt"] = ana["position_size_usdt"] * scenario.get("alt_size_mult", 1.0)

            opps.append((inds, c, ana))
            state["last_scan"].append({
                "symbol": c["symbol"], "strategy": ana["strategy"],
                "direction": ana["direction"], "confidence": ana["confidence"],
                "scanned_at": datetime.now().strftime("%H:%M:%S")
            })
        time.sleep(0.2)

    # Ejecutar mejores oportunidades
    conf_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    opps.sort(key=lambda x: conf_order.get(x[2]["confidence"], 0), reverse=True)

    executed = 0
    for inds, coin, ana in opps[:slots]:
        symbol = coin["symbol"]
        if capital < 20: break
        # BF-3: Re-verificar que no se abrió ya en esta iteración
        if symbol in state["positions"]:
            continue
        # Re-verificar cooldown (pudo haber cambiado durante el scan)
        cd = state.get("cooldowns", {}).get(symbol)
        if cd and _parse_dt(cd) > datetime.now():
            continue
        open_position(client, state, symbol, ana, inds, scenario)
        capital = state.get("capital", capital)
        executed += 1

    state["scanning"] = False
    save_state(state)

    log.info(f"Ciclo completado: {executed} nuevas posiciones abiertas")

    # Drawdown check — usar capital + valor en posiciones abiertas
    from drawdown_monitor import check_drawdown
    pos_value = sum(abs(p.get("size_usdt", 0)) for p in state.get("positions", {}).values())
    effective_capital = state.get("capital", TOTAL_CAPITAL) + pos_value
    check_drawdown("altcoins", effective_capital, TOTAL_CAPITAL,
                   state.get("peak_capital", TOTAL_CAPITAL), STATE_FILE)


def run_forever():
    log.info("🤖 Multi-Altcoin Bot v4.0 — scoring técnico + Kelly sizing")
    acquire_lock()
    client = get_client()

    while True:
        try:
            run_cycle(client)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)

        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    run_forever()
