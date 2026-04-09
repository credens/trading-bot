"""
S&P500 + ETFs Trading Bot — Alpaca
====================================
Estrategia: scoring técnico puro
Opera acciones individuales del S&P500 + ETFs (SPY, QQQ, IWM)
Usa Alpaca paper trading por defecto

SETUP:
  pip install alpaca-trade-api yfinance pandas numpy python-dotenv
  
.env:
  ALPACA_API_KEY=...
  ALPACA_SECRET_KEY=...
  ALPACA_BASE_URL=https://paper-api.alpaca.markets
  SP500_CAPITAL=10000
  SP500_MAX_POSITIONS=5
  SP500_INTERVAL=5   # minutos entre ciclos
"""

import os
import json
import time
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

INITIAL_CAPITAL = float(os.getenv("SP500_CAPITAL", "10000"))
MAX_POSITIONS = int(os.getenv("SP500_MAX_POSITIONS", "5"))
INTERVAL_MINUTES = int(os.getenv("SP500_INTERVAL", "5"))
MAX_POSITION_PCT = 0.20  # máx 20% del capital por posición

DATA_DIR = Path("./sp500_data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

# Basket de acciones S&P500 (mismo que el RSI bot + extras)
SP500_BASKET = [
    "VLO", "AMAT", "EOG", "MOS", "COST", "EQIX", "GILD",  # basket original
    "NVDA", "AMD", "META", "GOOGL", "AMZN", "MSFT", "AAPL",  # tech
    "JPM", "BAC", "GS",  # financials
    "XOM", "CVX",  # energy
]

# ETFs
ETFS = ["SPY", "QQQ", "IWM", "GLD", "TLT"]

ALL_SYMBOLS = SP500_BASKET + ETFS

# ─── Alpaca Client ────────────────────────────────────────────────────────────

def get_alpaca():
    import alpaca_trade_api as tradeapi
    api = tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, ALPACA_BASE_URL, api_version="v2")
    return api


# ─── Indicadores Técnicos ─────────────────────────────────────────────────────

def get_indicators(api, symbol: str, timeframe: str = "5Min", limit: int = 100) -> Optional[dict]:
    """Calcula indicadores técnicos desde Alpaca."""
    try:
        from datetime import timezone
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=5)

        bars = api.get_bars(
            symbol,
            timeframe,
            start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            limit=limit,
            adjustment="raw",
        ).df

        if bars.empty or len(bars) < 20:
            return None

        close = bars["close"]
        high = bars["high"]
        low = bars["low"]
        volume = bars["volume"]

        # RSI
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = float((100 - (100 / (1 + rs))).iloc[-1])

        # MACD
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = float((macd_line - signal_line).iloc[-1])
        prev_hist = float((macd_line - signal_line).iloc[-2])

        # Bollinger Bands
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        bb_pct = float(((close - (sma20 - 2*std20)) / (4*std20)).iloc[-1])

        # ATR
        tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        atr_pct = atr / float(close.iloc[-1]) * 100

        # Volumen
        vol_ratio = float(volume.iloc[-1] / volume.rolling(20).mean().iloc[-1])

        # EMAs tendencia
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        trend = "bullish" if ema50 > ema200 else "bearish"

        # Cambio diario
        change_1d = float((close.iloc[-1] - close.iloc[-20]) / close.iloc[-20] * 100)

        # NaN check
        if rsi != rsi or bb_pct != bb_pct:
            return None

        return {
            "symbol": symbol,
            "price": round(float(close.iloc[-1]), 4),
            "rsi": round(rsi, 2),
            "macd_hist": round(macd_hist, 4),
            "macd_cross": "bullish" if macd_hist > 0 and prev_hist <= 0
                          else "bearish" if macd_hist < 0 and prev_hist >= 0
                          else "neutral",
            "bb_pct": round(bb_pct, 3),
            "atr": round(atr, 4),
            "atr_pct": round(atr_pct, 3),
            "vol_ratio": round(vol_ratio, 2),
            "trend": trend,
            "change_1d": round(change_1d, 2),
            "is_etf": symbol in ETFS,
        }

    except Exception as e:
        log.warning(f"Error obteniendo indicadores de {symbol}: {e}")
        return None


# ─── Análisis Híbrido ─────────────────────────────────────────────────────────

def analyze_symbol(indicators: dict, capital: float, open_longs: int, open_shorts: int) -> Optional[dict]:
    """Scoring técnico para decidir si operar."""

    symbol = indicators["symbol"]
    rsi = indicators["rsi"]
    bb_pct = indicators["bb_pct"]
    macd_hist = indicators["macd_hist"]
    macd_cross = indicators["macd_cross"]
    vol_ratio = indicators["vol_ratio"]
    atr_pct = indicators["atr_pct"]
    trend = indicators["trend"]
    change_1d = indicators["change_1d"]
    price = indicators["price"]
    is_etf = indicators["is_etf"]

    if atr_pct < 0.05:
        return None

    # ── Scoring técnico ──────────────────────────────────────────────────────
    score = 0
    signals = []

    if rsi < 25: score += 3; signals.append(f"RSI sobrevendido ({rsi:.0f})")
    elif rsi < 35: score += 2; signals.append(f"RSI bajo ({rsi:.0f})")
    elif rsi > 75: score -= 3; signals.append(f"RSI sobrecomprado ({rsi:.0f})")
    elif rsi > 65: score -= 2; signals.append(f"RSI alto ({rsi:.0f})")

    if bb_pct < 0.1: score += 3; signals.append("BB lower band")
    elif bb_pct < 0.2: score += 1; signals.append("BB cerca lower")
    elif bb_pct > 0.9: score -= 3; signals.append("BB upper band")
    elif bb_pct > 0.8: score -= 1; signals.append("BB cerca upper")

    if macd_cross == "bullish": score += 2; signals.append("MACD bullish cross")
    elif macd_cross == "bearish": score -= 2; signals.append("MACD bearish cross")
    elif macd_hist > 0: score += 1
    elif macd_hist < 0: score -= 1

    if trend == "bullish": score += 1
    elif trend == "bearish": score -= 1

    if vol_ratio > 2:
        score = int(score * 1.5)
        signals.append(f"Vol alto ({vol_ratio:.1f}x)")

    if change_1d > 8: score -= 2; signals.append(f"Sobreextendido +{change_1d:.1f}%")
    elif change_1d < -8: score += 2; signals.append(f"Caída extrema {change_1d:.1f}%")

    # ETFs: señales más conservadoras (mercado entero)
    if is_etf:
        if abs(score) < 2:
            return None
    else:
        if abs(score) < 1:
            return None

    technical_direction = "LONG" if score > 0 else "SHORT"
    log.info(f"    {symbol} Score: {score:+d} → {technical_direction} | {' | '.join(signals[:3])}")

    # ── Decisión técnica ───────────────────────────────────────────────────────
    direction = technical_direction
    confidence = "HIGH" if abs(score) >= 3 else "MEDIUM"
    reasoning = f"Score {score:+d} | {' | '.join(signals[:2])}"
    sl_pct = round(max(atr_pct * 1.5 / 100, 0.01), 4)
    tp_pct = round(sl_pct * 2.5, 4)

    max_pos = capital * MAX_POSITION_PCT
    size_usd = round(max_pos * (1.0 if confidence == "HIGH" else 0.6), 2)
    shares = max(1, int(size_usd / price))

    return {
        "symbol": symbol,
        "direction": direction,
        "confidence": confidence,
        "reasoning": reasoning,
        "stop_loss_pct": round(sl_pct, 4),
        "take_profit_pct": round(tp_pct, 4),
        "size_usd": size_usd,
        "shares": shares,
        "price": price,
        "is_etf": is_etf,
    }


# ─── Estado ───────────────────────────────────────────────────────────────────

def load_state() -> dict:
    default = {
        "capital": INITIAL_CAPITAL,
        "positions": {},
        "closed_trades": [],
        "total_pnl": 0.0,
        "cycle_log": [],
        "cooldowns": {},
    }
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            state = dict(default)
            state["capital"] = data.get("capital", INITIAL_CAPITAL)
            state["positions"] = data.get("positions", {})
            state["closed_trades"] = data.get("all_closed_trades", data.get("closed_trades", []))
            state["total_pnl"] = sum(t.get("pnl", 0) for t in state["closed_trades"])
            state["cycle_log"] = data.get("cycle_log", [])
            state["cooldowns"] = data.get("cooldowns", {})
            log.info(f"[SP500] Estado: ${state['capital']:.2f} | {len(state['positions'])} posiciones | {len(state['closed_trades'])} trades")
            return state
        except Exception as e:
            log.warning(f"Error cargando estado SP500: {e}")
    return default


def save_state(state: dict):
    closed = state.get("closed_trades", [])
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    total_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)

    dashboard = {
        "bot": "sp500",
        "initial_capital": INITIAL_CAPITAL,
        "current_capital": round(state["capital"], 2),
        "capital": round(state["capital"], 2),
        "total_pnl": total_pnl,
        "total_pnl_pct": round(total_pnl / INITIAL_CAPITAL * 100, 2),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_trades": len(closed),
        "positions": state["positions"],
        "open_positions": list(state["positions"].values()),
        "closed_trades": closed[-30:],
        "all_closed_trades": closed,
        "cooldowns": state.get("cooldowns", {}),
        "cycle_log": state.get("cycle_log", [])[-50:],
        "last_updated": datetime.now().isoformat(),
    }

    def clean_nan(obj):
        if isinstance(obj, float) and obj != obj: return None
        if isinstance(obj, dict): return {k: clean_nan(v) for k, v in obj.items()}
        if isinstance(obj, list): return [clean_nan(v) for v in obj]
        return obj

    STATE_FILE.write_text(json.dumps(clean_nan(dashboard), indent=2, default=str))


def add_log(state: dict, msg: str):
    logs = state.get("cycle_log", [])
    logs = [{"time": datetime.now().strftime("%H:%M:%S"), "msg": msg}] + logs[:49]
    state["cycle_log"] = logs


# ─── Ejecución ────────────────────────────────────────────────────────────────

def check_positions(api, state: dict):
    """Verifica SL/TP de posiciones abiertas."""
    to_close = []
    for symbol, pos in state["positions"].items():
        try:
            bars = api.get_bars(symbol, "1Min", limit=1).df
            if bars.empty: continue
            current_price = float(bars["close"].iloc[-1])

            direction = pos["direction"]
            entry = pos["entry_price"]
            sl = pos["stop_loss"]
            tp = pos["take_profit"]

            hit_tp = (direction == "LONG" and current_price >= tp) or \
                     (direction == "SHORT" and current_price <= tp)
            hit_sl = (direction == "LONG" and current_price <= sl) or \
                     (direction == "SHORT" and current_price >= sl)

            if hit_tp or hit_sl:
                reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"
                exit_price = tp if hit_tp else sl
                pnl_pct = (exit_price - entry) / entry if direction == "LONG" else (entry - exit_price) / entry
                pnl = round(pos["size_usd"] * pnl_pct, 2)

                trade = {**pos, "exit_price": exit_price, "exit_time": datetime.now().isoformat(),
                         "exit_reason": reason, "pnl": pnl, "pnl_pct": round(pnl_pct*100, 2)}
                state["closed_trades"].append(trade)
                state["capital"] += pos["size_usd"] + pnl
                state["total_pnl"] += pnl
                to_close.append(symbol)

                emoji = "✅" if pnl > 0 else "❌"
                add_log(state, f"{emoji} {reason} {symbol} @ ${exit_price:.2f} | P&L {'+' if pnl>=0 else ''}${pnl:.2f}")
                log.info(f"  {emoji} {reason} {symbol} | P&L ${pnl:+.2f}")

        except Exception as e:
            log.warning(f"Error verificando {symbol}: {e}")

    for sym in to_close:
        del state["positions"][sym]


def open_position(api, state: dict, analysis: dict):
    """Abre posición paper en Alpaca."""
    symbol = analysis["symbol"]
    direction = analysis["direction"]
    shares = analysis["shares"]
    price = analysis["price"]
    sl_pct = analysis["stop_loss_pct"]
    tp_pct = analysis["take_profit_pct"]

    sl = round(price * (1 - sl_pct) if direction == "LONG" else price * (1 + sl_pct), 4)
    tp = round(price * (1 + tp_pct) if direction == "LONG" else price * (1 - tp_pct), 4)
    size_usd = round(shares * price, 2)

    try:
        side = "buy" if direction == "LONG" else "sell"
        api.submit_order(symbol=symbol, qty=shares, side=side, type="market", time_in_force="day")

        pos = {
            "symbol": symbol,
            "direction": direction,
            "entry_price": price,
            "entry_time": datetime.now().isoformat(),
            "shares": shares,
            "size_usd": size_usd,
            "stop_loss": sl,
            "take_profit": tp,
            "confidence": analysis["confidence"],
            "reasoning": analysis["reasoning"],
            "is_etf": analysis["is_etf"],
        }
        state["positions"][symbol] = pos
        state["capital"] -= size_usd

        msg = f"✓ {direction} {symbol} @ ${price:.2f} | {shares} shares | SL ${sl:.2f} | TP ${tp:.2f}"
        add_log(state, msg)
        log.info(f"  [SP500] {msg}")

    except Exception as e:
        log.error(f"  Error abriendo {symbol}: {e}")


# ─── Ciclo Principal ──────────────────────────────────────────────────────────

def run_cycle(api):
    log.info(f"\n{'='*55}")
    log.info(f"SP500 CYCLE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"{'='*55}")

    # Verificar horario de mercado
    clock = api.get_clock()
    if not clock.is_open:
        log.info(f"Mercado cerrado. Próxima apertura: {clock.next_open}")
        return

    state = load_state()

    # 1. Verificar posiciones
    if state["positions"]:
        log.info(f"Verificando {len(state['positions'])} posiciones...")
        check_positions(api, state)

    capital = state["capital"]
    open_count = len(state["positions"])
    log.info(f"Capital: ${capital:.2f} | Posiciones: {open_count}/{MAX_POSITIONS} | P&L: ${state['total_pnl']:+.2f}")

    if open_count >= MAX_POSITIONS:
        log.info("Máximo de posiciones alcanzado.")
        save_state(state)
        return

    # 2. Escanear símbolos
    now = datetime.now()
    cooldowns = state.get("cooldowns", {})
    cooldowns = {s: t for s, t in cooldowns.items()
                 if datetime.fromisoformat(t.replace("Z", "")) > now}
    state["cooldowns"] = cooldowns

    candidates = [s for s in ALL_SYMBOLS
                  if s not in state["positions"] and s not in cooldowns]

    log.info(f"Analizando {len(candidates)} símbolos...")

    open_longs = sum(1 for p in state["positions"].values() if p["direction"] == "LONG")
    open_shorts = sum(1 for p in state["positions"].values() if p["direction"] == "SHORT")

    opportunities = []
    for symbol in candidates:
        indicators = get_indicators(api, symbol)
        if not indicators:
            continue

        analysis = analyze_symbol(indicators, capital, open_longs, open_shorts)
        if analysis:
            opportunities.append(analysis)
        time.sleep(0.2)

    # 3. Ordenar por confianza y ejecutar
    conf_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    opportunities.sort(key=lambda x: conf_order.get(x["confidence"], 0), reverse=True)

    slots = MAX_POSITIONS - open_count
    executed = 0
    for analysis in opportunities[:slots]:
        if capital < analysis["size_usd"]:
            break
        open_position(api, state, analysis)
        open_count += 1
        executed += 1

    log.info(f"Ciclo completo: {executed} nuevas posiciones")
    save_state(state)


def run_forever():
    log.info("🤖 SP500 + ETFs Bot iniciado")
    log.info(f"   Capital: ${INITIAL_CAPITAL:,.0f} | Max posiciones: {MAX_POSITIONS} | Intervalo: {INTERVAL_MINUTES}min")
    log.info(f"   Acciones: {len(SP500_BASKET)} | ETFs: {len(ETFS)}")

    api = get_alpaca()
    account = api.get_account()
    log.info(f"   Balance Alpaca: ${float(account.cash):,.2f}")

    while True:
        try:
            run_cycle(api)
        except KeyboardInterrupt:
            log.info("🛑 Bot detenido.")
            break
        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)

        log.info(f"⏰ Próximo ciclo en {INTERVAL_MINUTES} min...")
        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    import sys
    api = get_alpaca()
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_cycle(api)
    else:
        run_forever()
