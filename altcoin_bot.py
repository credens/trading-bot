"""
Multi-Altcoin Adaptive Bot - v3.0 (FULL EXPERT VERSION)
======================================================
- Capital Reset: $200 (Configurable vía .env)
- Anti-Revenge Trading: Cooldown de 15 min y bloqueo de duplicados.
- Expert Scoring: EMA Cross, VWAP, RSI, MACD (3,10,5), Bollinger, ATR.
- Trailing: SL dinámico + TP dinámico (TTP).
- Dashboard: Full logs, win rate, PnL raw y capital dinámico.
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from trade_logger import log_trade as _log_trade
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
import pandas as pd
import numpy as np

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

LEVERAGE         = int(os.getenv("ALTCOIN_LEVERAGE",    "20"))   
MAX_POSITIONS    = int(os.getenv("ALTCOIN_MAX_POSITIONS","10"))   
TOTAL_CAPITAL    = float(os.getenv("ALTCOIN_CAPITAL",   "200"))  # Capital de inicio
DRY_RUN          = os.getenv("DRY_RUN", "true").lower() == "true"
INTERVAL_MINUTES = int(os.getenv("ALTCOIN_INTERVAL",   "3"))
MIN_VOLUME_USDT  = float(os.getenv("ALTCOIN_MIN_VOLUME","300000000"))  
TOP_N            = int(os.getenv("ALTCOIN_TOP_N",       "20"))   
CANDLE_INTERVAL  = os.getenv("ALTCOIN_CANDLE", "5m")            
DEFAULT_SL_PCT   = float(os.getenv("ALTCOIN_SL",  "0.006"))     
DEFAULT_TP_PCT   = float(os.getenv("ALTCOIN_TP",  "0.025"))     
TRAILING_TRIGGER = float(os.getenv("ALTCOIN_TRAIL", "0.004"))   
TP_CALLBACK_PCT  = float(os.getenv("ALTCOIN_TP_CALLBACK", "0.003")) 
TIME_LIMIT_MIN   = int(os.getenv("ALTCOIN_TIME_LIMIT", "120"))   

LOSS_COOLDOWN_MIN = 15 # Bloqueo tras pérdida

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

DATA_DIR = Path(__file__).parent / "altcoin_data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

EXCLUDE = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "USDTUSDT", "BTCUSDT",
           "BTCDOMUSDT", "DEFIUSDT", "BNXUSDT", "1000SHIBUSDT",
           "SIRENUSDT", "LOOMUSDT", "CVPUSDT", "BALUSDT",
           "1000LUNCUSDT", "LUNA2USDT", "ONTUSDT", "RIVERUSDT", 
           "HYPEUSDT", "XAGUSDT", "XAUUSDT", "PAXGUSDT", "1000PEPEUSDT"}

# ─── Binance Client ───────────────────────────────────────────────────────────

def get_client():
    from binance.client import Client
    return Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

# ─── Market Scanner ───────────────────────────────────────────────────────────

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

# ─── Indicadores Expertos ──────────────────────────────────────────────────────

def get_indicators(client, symbol: str) -> Optional[dict]:
    try:
        klines = client.futures_klines(symbol=symbol, interval=CANDLE_INTERVAL, limit=120)
        df = pd.DataFrame(klines, columns=["ts", "open", "high", "low", "close", "volume", "ct", "qv", "trades", "tbb", "tbq", "ignore"])
        for col in ["open", "high", "low", "close", "volume"]: df[col] = df[col].astype(float)

        close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]

        # EMA Scalping Setup
        ema9  = close.ewm(span=9,  adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        cross_bull = ema9.iloc[-2] <= ema21.iloc[-2] and ema9.iloc[-1] > ema21.iloc[-1]
        cross_bear = ema9.iloc[-2] >= ema21.iloc[-2] and ema9.iloc[-1] < ema21.iloc[-1]

        # VWAP (78 velas de 5m = 6.5h mercado)
        tp = (high + low + close) / 3
        vwap = float((tp * volume).rolling(78).sum().iloc[-1] / volume.rolling(78).sum().iloc[-1])
        p_vs_v = (close.iloc[-1] - vwap) / vwap * 100

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

        # Bollinger & Volatilidad
        sma20, std20 = close.rolling(20).mean(), close.rolling(20).std()
        bb_pct = float(((close.iloc[-1] - (sma20.iloc[-1] - 2*std20.iloc[-1])) / (4*std20.iloc[-1])))
        tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
        atr_pct = float(tr.rolling(14).mean().iloc[-1] / close.iloc[-1] * 100)

        vol_ratio = float(volume.iloc[-1] / volume.rolling(20).mean().iloc[-1])
        hist_vol = float(close.pct_change().rolling(20).std().iloc[-1] * 100)
        ema50, ema200 = close.ewm(span=50).mean().iloc[-1], close.ewm(span=200).mean().iloc[-1]

        return {
            "symbol": symbol, "price": close.iloc[-1], "ema_trend": "bullish" if ema9.iloc[-1] > ema21.iloc[-1] else "bearish",
            "cross_bullish": cross_bull, "cross_bearish": cross_bear, "vwap": round(vwap, 6), "price_vs_vwap": round(p_vs_v, 3),
            "rsi": round(rsi, 2), "macd_hist": round(macd_h, 6), "macd_cross": macd_c, "bb_pct": round(bb_pct, 3),
            "atr_pct": round(atr_pct, 3), "vol_ratio": round(vol_ratio, 2), "hist_vol": round(hist_vol, 3),
            "trend": "bullish" if ema50 > ema200 else "bearish"
        }
    except Exception as e:
        log.warning(f"Error indicators {symbol}: {e}")
        return None

# ─── Scoring Técnico ──────────────────────────────────────────────────────────

def analyze_altcoin(indicators, market_data, capital, open_pos, open_l, open_s) -> Optional[dict]:
    score = 0
    sigs = []
    
    # 1. EMA Cross & Trend (Primario)
    if indicators["cross_bullish"]: score += 3; sigs.append("EMA Cross Up ↑")
    elif indicators["ema_trend"] == "bullish": score += 1
    
    if indicators["cross_bearish"]: score -= 3; sigs.append("EMA Cross Down ↓")
    elif indicators["ema_trend"] == "bearish": score -= 1

    # 2. VWAP
    if indicators["price_vs_vwap"] > 0.1: score += 1
    elif indicators["price_vs_vwap"] < -0.1: score -= 1

    # 3. MACD & RSI
    if indicators["macd_cross"] == "bullish": score += 2; sigs.append("MACD Bullish")
    elif indicators["macd_cross"] == "bearish": score -= 2; sigs.append("MACD Bearish")
    
    if indicators["rsi"] < 30: score += 2; sigs.append(f"RSI OS {indicators['rsi']}")
    elif indicators["rsi"] > 70: score -= 2; sigs.append(f"RSI OB {indicators['rsi']}")

    # 4. Volumen
    if indicators["vol_ratio"] > 1.8: score = int(score * 1.3); sigs.append("High Vol")

    if abs(score) < 2: return None

    direction = "LONG" if score > 0 else "SHORT"
    conf = "HIGH" if abs(score) >= 4 else "MEDIUM"
    
    # Sizing Dinámico para $200
    size = round((capital / 10) * (1.0 if conf == "HIGH" else 0.7), 2)
    if size < 15: size = 15 # Margen mínimo operativo

    return {
        "strategy": "MOMENTUM" if indicators["vol_ratio"] > 1.4 else "RANGE",
        "direction": direction, "confidence": conf, "reasoning": f"Score {score} | {', '.join(sigs[:2])}",
        "stop_loss_pct": DEFAULT_SL_PCT, "take_profit_pct": DEFAULT_TP_PCT, "position_size_usdt": size
    }

# ─── State Management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    default = {
        "initial_capital": TOTAL_CAPITAL, "capital": TOTAL_CAPITAL, "positions": {}, 
        "closed_trades": [], "total_pnl": 0.0, "cooldowns": {}, "cycle_log": [], 
        "manual_close": [], "last_scan": [], "scanning": False
    }
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            # Fix: Asegurar que posiciones sea dict
            if not isinstance(data.get("positions"), dict): data["positions"] = {}
            # Sync capital si está vacío
            if not data.get("closed_trades") and data.get("capital") != TOTAL_CAPITAL:
                data["capital"] = TOTAL_CAPITAL
            return data
        except: return default
    return default

def save_state(state: dict):
    def clean(obj):
        if isinstance(obj, float) and (obj != obj): return 0.0
        if isinstance(obj, dict): return {k: clean(v) for k, v in obj.items()}
        if isinstance(obj, list): return [clean(v) for v in obj]
        return obj
    
    closed = state.get("closed_trades", [])
    state["total_pnl"] = round(sum(t.get("pnl", 0) for t in closed), 2)
    # Capital Actual = Capital Inicial + PNL Realizado - Capital Comprometido
    current_cap = round(TOTAL_CAPITAL + state["total_pnl"] - sum(p["size_usdt"] for p in state["positions"].values()), 2)
    
    dashboard = {
        **state,
        "bot": "altcoins",
        "capital": current_cap,
        "current_capital": current_cap,
        "total_pnl_pct": round((state["total_pnl"] / TOTAL_CAPITAL) * 100, 2),
        "win_rate": round(len([t for t in closed if t.get("pnl",0) > 0]) / max(1, len(closed)) * 100, 1),
        "total_trades": len(closed),
        "all_closed_trades": closed,
        "closed_trades": closed[-30:],
        "last_updated": datetime.now().isoformat()
    }
    
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(clean(dashboard), f, indent=2)
    except Exception as e: log.error(f"Error saving state: {e}")

def add_log(state, msg):
    state.setdefault("cycle_log", []).insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg})
    state["cycle_log"] = state["cycle_log"][:50]

# ─── Trade Engine ─────────────────────────────────────────────────────────────

def open_position(client, state, symbol, analysis, indicators):
    price, direction = indicators["price"], analysis["direction"]
    sl = round(price * (1 - DEFAULT_SL_PCT) if direction == "LONG" else price * (1 + DEFAULT_SL_PCT), 8)
    tp = round(price * (1 + DEFAULT_TP_PCT) if direction == "LONG" else price * (1 - DEFAULT_TP_PCT), 8)
    
    pos = {
        "symbol": symbol, "direction": direction, "strategy": analysis["strategy"],
        "entry_price": price, "entry_time": datetime.now().isoformat(),
        "size_usdt": analysis["position_size_usdt"], "stop_loss": sl, "take_profit": tp,
        "tp_trailing_active": False, "tp_peak_price": price, "best_price": price, 
        "leverage": LEVERAGE, "trailing_activated": False, "confidence": analysis["confidence"],
        "reasoning": analysis["reasoning"]
    }

    if DRY_RUN:
        state["positions"][symbol] = pos
        add_log(state, f"✓ PAPER {direction} {symbol} @ ${price:.4f}")
    else:
        try:
            client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
            side = "BUY" if direction == "LONG" else "SELL"
            qty = round(pos["size_usdt"] * LEVERAGE / price, 3)
            client.futures_create_order(symbol=symbol, side=side, type="MARKET", quantity=qty)
            state["positions"][symbol] = pos
            add_log(state, f"✅ REAL {direction} {symbol} @ ${price:.4f}")
        except Exception as e: log.error(f"Error opening {symbol}: {e}")

def _close_position(state, symbol, pos, exit_price, exit_reason):
    entry, direction, lev = pos["entry_price"], pos["direction"], pos["leverage"]
    pnl_pct = ((exit_price - entry)/entry if direction=="LONG" else (entry-exit_price)/entry) * lev
    pnl = round(pos["size_usdt"] * pnl_pct, 2)
    
    trade = {**pos, "exit_price": exit_price, "exit_time": datetime.now().isoformat(), "pnl": pnl, "pnl_pct": round(pnl_pct*100, 2), "exit_reason": exit_reason}
    state["closed_trades"].append(trade)
    
    if pnl < 0:
        state["cooldowns"][symbol] = (datetime.now() + timedelta(minutes=LOSS_COOLDOWN_MIN)).isoformat()
        log.warning(f"  🚫 {symbol} bloqueado 15m por pérdida.")

    if symbol in state["positions"]: del state["positions"][symbol]
    
    msg = f"{'✅' if pnl>0 else '❌'} {exit_reason} {symbol} | PnL ${pnl:+.2f} ({trade['pnl_pct']}%)"
    add_log(state, msg); log.info(f"  [CLOSE] {msg}")
    try: _log_trade({**trade, "bot": "altcoins"})
    except: pass

def check_positions(client, state, scenario=None):
    now = datetime.now()
    to_close = []
    
    for symbol, pos in list(state["positions"].items()):
        try:
            ticker = client.futures_ticker(symbol=symbol)
            curr = float(ticker["lastPrice"])
            entry, direction, sl, tp_act = pos["entry_price"], pos["direction"], pos["stop_loss"], pos["take_profit"]
            lev = pos["leverage"]

            # 1. Trailing Stop Loss (Piso dinámico)
            if direction == "LONG": pos["best_price"] = max(pos.get("best_price", curr), curr)
            else: pos["best_price"] = min(pos.get("best_price", curr), curr)
            
            move = abs(curr - entry) / entry
            if move >= TRAILING_TRIGGER:
                dist = 0.008 if move < TRAILING_TRIGGER*2 else 0.005 if move < TRAILING_TRIGGER*4 else 0.003
                new_sl = round(pos["best_price"]*(1-dist) if direction=="LONG" else pos["best_price"]*(1+dist), 8)
                if (direction=="LONG" and new_sl > sl) or (direction=="SHORT" and new_sl < sl):
                    pos["stop_loss"] = sl = new_sl
                    pos["trailing_activated"] = True

            # 2. Trailing Take Profit (TTP)
            if (direction=="LONG" and curr >= tp_act) or (direction=="SHORT" and curr <= tp_act):
                if not pos.get("tp_trailing_active"):
                    pos["tp_trailing_active"] = True
                    pos["tp_peak_price"] = curr
                    log.info(f"  🚀 TTP Activado en {symbol}")

            if pos.get("tp_trailing_active"):
                if direction == "LONG":
                    pos["tp_peak_price"] = max(pos["tp_peak_price"], curr)
                    if curr <= pos["tp_peak_price"] * (1 - TP_CALLBACK_PCT):
                        to_close.append((symbol, curr, "TRAILING_TP"))
                        continue
                else:
                    pos["tp_peak_price"] = min(pos["tp_peak_price"], curr)
                    if curr >= pos["tp_peak_price"] * (1 + TP_CALLBACK_PCT):
                        to_close.append((symbol, curr, "TRAILING_TP"))
                        continue

            # 3. SL / Emergency / Time
            pnl_pct = ((curr - entry)/entry if direction=="LONG" else (entry-curr)/entry) * lev
            hit_sl = (direction=="LONG" and curr <= sl) or (direction=="SHORT" and curr >= sl)
            time_exp = (now - datetime.fromisoformat(pos["entry_time"])).total_seconds()/60 >= TIME_LIMIT_MIN
            
            if hit_sl: to_close.append((symbol, curr, "STOP_LOSS"))
            elif pnl_pct <= -0.22: to_close.append((symbol, curr, "EMERGENCY"))
            elif time_exp: to_close.append((symbol, curr, "TIME_LIMIT"))

        except Exception as e: log.error(f"Error check {symbol}: {e}")

    for s, p, r in to_close:
        if s in state["positions"]: _close_position(state, s, state["positions"][s], p, r)

# ─── Main Cycle ───────────────────────────────────────────────────────────────

def run_cycle(client):
    log.info(f"\n{'='*40}\nALTCOIN CYCLE - {datetime.now().strftime('%H:%M:%S')}\n{'='*40}")
    state = load_state()
    now = datetime.now()

    # 1. Cooldown Cleanup
    state["cooldowns"] = {s: t for s, t in state.get("cooldowns", {}).items() if datetime.fromisoformat(t) > now}

    # 2. Drawdown & Manual Close
    from drawdown_monitor import is_paused
    if is_paused(STATE_FILE): return

    for sym in list(state.get("manual_close", [])):
        if sym in state["positions"]:
            t = client.futures_ticker(symbol=sym)
            _close_position(state, sym, state["positions"][sym], float(t["lastPrice"]), "MANUAL")
    state["manual_close"] = []

    # 3. Position Monitoring
    from market_scenario import detect_scenario
    scenario = detect_scenario(client)
    check_positions(client, state, scenario)

    # 4. Market Scanning
    slots = MAX_POSITIONS - len(state["positions"])
    if slots > 0:
        state["scanning"] = True
        state["last_scan"] = []
        save_state(state)
        
        coins = get_top_altcoins(client)
        # SEGURIDAD CRÍTICA: Filtrar antes de analizar para evitar bugs de ENJ
        candidates = [c for c in coins if c["symbol"] not in state["positions"] and c["symbol"] not in state["cooldowns"]]
        
        opps = []
        for c in candidates[:slots*3]:
            inds = get_indicators(client, c["symbol"])
            if not inds: continue
            
            open_l = sum(1 for p in state["positions"].values() if p["direction"]=="LONG")
            open_s = sum(1 for p in state["positions"].values() if p["direction"]=="SHORT")
            
            ana = analyze_altcoin(inds, c, state["capital"], len(state["positions"]), open_l, open_s)
            
            # Log scan para el Dashboard
            scan_data = {
                "symbol": c["symbol"], "rsi": inds["rsi"], "strategy": ana["strategy"] if ana else "SKIP",
                "direction": ana["direction"] if ana else "SKIP", "confidence": ana["confidence"] if ana else "LOW",
                "reasoning": ana["reasoning"] if ana else "No signal", "scanned_at": now.strftime("%H:%M:%S")
            }
            state["last_scan"].append(scan_data)
            
            if ana: opps.append((inds, c, ana))
            time.sleep(0.2) # Throttle API
        
        opps.sort(key=lambda x: x[2]["position_size_usdt"], reverse=True)
        for inds, coin, ana in opps[:slots]:
            open_position(client, state, coin["symbol"], ana, inds)

    state["scanning"] = False
    save_state(state)
    from drawdown_monitor import check_drawdown
    check_drawdown("altcoins", state["capital"], TOTAL_CAPITAL, state.get("peak_capital", state["capital"]), STATE_FILE)

if __name__ == "__main__":
    client = get_client()
    while True:
        try: run_cycle(client)
        except Exception as e: log.error(f"Fatal error: {e}", exc_info=True)
        time.sleep(INTERVAL_MINUTES * 60)