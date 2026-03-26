"""
Test Bot — Prueba de Estrategias BTC
=====================================
3 estrategias nuevas votando 2/3 para entrar:

  1. FUNDING_EXTREME — funding rate > +0.08% → SHORT, < -0.03% → LONG
  2. BB_SQUEEZE      — Bollinger Bands comprimidas + breakout + volumen
  3. RSI_DIV_1H      — Divergencia RSI en 1h (precio vs momentum)

Leverage: x10 | Capital inicial: $500 | Ciclo: 5 min
"""

import os, time, json, logging
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from binance.client import Client
from dotenv import load_dotenv

from paper_trading import PaperTradingEngine, Trade
import time as _time

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
SYMBOL         = "BTCUSDT"
LEVERAGE       = 10
INITIAL_CAP    = 500.0
CYCLE_MIN      = 5
STATE_FILE     = Path(__file__).parent / "paper_trading" / "test_bot_state.json"
LOG_FILE       = "/tmp/test_bot.log"

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TEST] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ]
)
log = logging.getLogger("test_bot")

# ─── Helpers ──────────────────────────────────────────────────────────────────
def fetch_ohlcv(client, interval="15m", limit=200) -> pd.DataFrame:
    klines = client.futures_klines(symbol=SYMBOL, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_vol","trades","taker_buy_base","taker_buy_quote","ignore"])
    for col in ["open","high","low","close","volume","taker_buy_base"]:
        df[col] = df[col].astype(float)
    return df


def get_funding_rate(client) -> float:
    try:
        data = client.futures_funding_rate(symbol=SYMBOL, limit=1)
        return float(data[-1]["fundingRate"]) * 100  # en %
    except Exception:
        return 0.0


def _strat(name, decision, confidence, reasoning, signals, price, sl_pct, tp_pct):
    log.info(f"  [{name}] → {decision} ({confidence}) | {reasoning}")
    return {
        "decision": decision, "confidence": confidence,
        "reasoning": f"[{name}] {reasoning}", "key_signals": signals,
        "entry_price": price, "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
        "position_size_pct": 0.10 if confidence == "HIGH" else 0.07,
        "_strategy": name,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA 1 — FUNDING RATE EXTREME
# Cuando el mercado está muy sesgado en una dirección, operar en contra.
# Funding > +0.08%  → longs pagan mucho → SHORT
# Funding < -0.03%  → shorts pagan mucho → LONG
# ══════════════════════════════════════════════════════════════════════════════
def strategy_funding_extreme(client, current_position: str) -> dict:
    name = "FUNDING_EXT"
    df = fetch_ohlcv(client, "15m", 50)
    price = float(df["close"].iloc[-1])
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = float((100 - (100 / (1 + gain / loss.replace(0, np.nan)))).iloc[-1])
    sl_pct, tp_pct = 0.015, 0.035  # SL 1.5% TP 3.5%

    funding = get_funding_rate(client)

    if current_position == "LONG":
        if funding > 0.05 or rsi > 65:
            return _strat(name, "FLAT", "HIGH", f"Funding giró positivo ({funding:+.4f}%) → cierre", ["Salida funding"], price, sl_pct, tp_pct)
        return _strat(name, "HOLD", "MEDIUM", f"LONG activo | Funding {funding:+.4f}%", [f"Funding {funding:+.4f}%"], price, sl_pct, tp_pct)

    if current_position == "SHORT":
        if funding < 0.01 or rsi < 35:
            return _strat(name, "FLAT", "HIGH", f"Funding bajó ({funding:+.4f}%) → cierre", ["Salida funding"], price, sl_pct, tp_pct)
        return _strat(name, "HOLD", "MEDIUM", f"SHORT activo | Funding {funding:+.4f}%", [f"Funding {funding:+.4f}%"], price, sl_pct, tp_pct)

    if funding > 0.08:
        conf = "HIGH" if funding > 0.12 else "MEDIUM"
        signals = [f"Funding extremo +{funding:.4f}%", "Mercado sobrecargado long"]
        if rsi > 55: signals.append(f"RSI {rsi:.0f} — no sobrevendido ✓")
        return _strat(name, "SHORT", conf, f"Funding extremo largo ({funding:+.4f}%)", signals, price, sl_pct, tp_pct)

    if funding < -0.03:
        conf = "HIGH" if funding < -0.06 else "MEDIUM"
        signals = [f"Funding negativo {funding:.4f}%", "Short squeeze potencial"]
        if rsi < 45: signals.append(f"RSI {rsi:.0f} — no sobrecomprado ✓")
        return _strat(name, "LONG", conf, f"Funding extremo corto ({funding:+.4f}%)", signals, price, sl_pct, tp_pct)

    return _strat(name, "FLAT", "MEDIUM", f"Funding neutral ({funding:+.4f}%)", ["Funding sin extremo"], price, sl_pct, tp_pct)


# ══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA 2 — BB SQUEEZE BREAKOUT (15m)
# Cuando las Bollinger Bands se comprimen (baja volatilidad), el siguiente
# movimiento suele ser explosivo. Entrar en la dirección del breakout.
# ══════════════════════════════════════════════════════════════════════════════
def strategy_bb_squeeze(client, current_position: str) -> dict:
    name = "BB_SQUEEZE"
    df = fetch_ohlcv(client, "15m", 100)
    price = float(df["close"].iloc[-1])
    close = df["close"]; volume = df["volume"]
    sl_pct, tp_pct = 0.012, 0.028  # SL 1.2% TP 2.8%

    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_width = (bb_upper - bb_lower) / sma20

    # ¿Hay squeeze? BB width en el percentil 20 de los últimos 50 candles
    width_pct20 = float(bb_width.iloc[-51:-1].quantile(0.20)) if len(bb_width) > 51 else 0.01
    squeeze = float(bb_width.iloc[-2]) < width_pct20  # la vela anterior estaba en squeeze

    vol_sma = volume.rolling(20).mean()
    vol_ratio = float(volume.iloc[-1] / vol_sma.iloc[-1])

    # Breakout: cierre actual fuera de las bandas
    above_upper = price > float(bb_upper.iloc[-1])
    below_lower = price < float(bb_lower.iloc[-1])

    # RSI para dirección
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = float((100 - (100 / (1 + gain / loss.replace(0, np.nan)))).iloc[-1])

    if current_position == "LONG":
        if below_lower or rsi > 72:
            return _strat(name, "FLAT", "HIGH", "Precio rompe banda inferior → cierre LONG", ["Salida BB"], price, sl_pct, tp_pct)
        return _strat(name, "HOLD", "MEDIUM", f"LONG BB activo | Precio: {'UPPER' if above_upper else 'MIDDLE'}", ["Manteniendo"], price, sl_pct, tp_pct)

    if current_position == "SHORT":
        if above_upper or rsi < 28:
            return _strat(name, "FLAT", "HIGH", "Precio rompe banda superior → cierre SHORT", ["Salida BB"], price, sl_pct, tp_pct)
        return _strat(name, "HOLD", "MEDIUM", f"SHORT BB activo | Precio: {'LOWER' if below_lower else 'MIDDLE'}", ["Manteniendo"], price, sl_pct, tp_pct)

    if squeeze and above_upper and vol_ratio > 1.5 and rsi < 78:
        conf = "HIGH" if vol_ratio > 2.0 else "MEDIUM"
        signals = [f"Squeeze → LONG breakout ✓", f"Vol {vol_ratio:.1f}x ✓", f"RSI {rsi:.0f}"]
        return _strat(name, "LONG", conf, f"BB squeeze breakout LONG | vol {vol_ratio:.1f}x", signals, price, sl_pct, tp_pct)

    if squeeze and below_lower and vol_ratio > 1.5 and rsi > 22:
        conf = "HIGH" if vol_ratio > 2.0 else "MEDIUM"
        signals = [f"Squeeze → SHORT breakout ✓", f"Vol {vol_ratio:.1f}x ✓", f"RSI {rsi:.0f}"]
        return _strat(name, "SHORT", conf, f"BB squeeze breakout SHORT | vol {vol_ratio:.1f}x", signals, price, sl_pct, tp_pct)

    reason = f"Sin squeeze+breakout (BB_width={float(bb_width.iloc[-1]):.4f}, squeeze={'Sí' if squeeze else 'No'}, vol={vol_ratio:.1f}x)"
    return _strat(name, "FLAT", "MEDIUM", reason, ["Esperando squeeze+breakout"], price, sl_pct, tp_pct)


# ══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA 3 — RSI DIVERGENCE (1h)
# Precio hace nuevo mínimo pero RSI no lo confirma → momentum cambiando.
# Señal más fiable que RSI puro porque detecta cambio estructural.
# ══════════════════════════════════════════════════════════════════════════════
def strategy_rsi_divergence(client, current_position: str) -> dict:
    name = "RSI_DIV_1H"
    df = fetch_ohlcv(client, "1h", 60)
    price = float(df["close"].iloc[-1])
    close = df["close"]; high = df["high"]; low = df["low"]
    sl_pct, tp_pct = 0.018, 0.038  # SL 1.8% TP 3.8%

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi_s = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    rsi = float(rsi_s.iloc[-1])

    # Buscar divergencia en las últimas 15 velas
    window = 15
    lows_price = low.iloc[-window:].values
    highs_price = high.iloc[-window:].values
    rsi_vals = rsi_s.iloc[-window:].values

    # Divergencia alcista: precio hace lower low, RSI hace higher low
    # Buscar dos mínimos locales en el precio
    bullish_div = False
    bearish_div = False
    div_strength = 0

    for i in range(2, window - 2):
        for j in range(i + 2, window):
            # Mínimo local en i y j (precio)
            is_low_i = lows_price[i] < lows_price[i-1] and lows_price[i] < lows_price[i+1]
            is_low_j = lows_price[j] < lows_price[j-1] and lows_price[j] < lows_price[j+1] if j < window-1 else False
            if is_low_i and is_low_j:
                # Precio lower low pero RSI higher low → divergencia alcista
                if lows_price[j] < lows_price[i] and rsi_vals[j] > rsi_vals[i] + 3:
                    if lows_price[i] < np.mean(lows_price):  # el precio está en zona baja
                        bullish_div = True
                        div_strength = rsi_vals[j] - rsi_vals[i]

            # Máximo local
            is_high_i = highs_price[i] > highs_price[i-1] and highs_price[i] > highs_price[i+1]
            is_high_j = highs_price[j] > highs_price[j-1] and highs_price[j] > highs_price[j+1] if j < window-1 else False
            if is_high_i and is_high_j:
                # Precio higher high pero RSI lower high → divergencia bajista
                if highs_price[j] > highs_price[i] and rsi_vals[j] < rsi_vals[i] - 3:
                    if highs_price[i] > np.mean(highs_price):  # el precio está en zona alta
                        bearish_div = True
                        div_strength = rsi_vals[i] - rsi_vals[j]

    if current_position == "LONG":
        if bearish_div or rsi > 70:
            return _strat(name, "FLAT", "HIGH", f"Divergencia bearish o RSI alto → cierre", ["Salida divergencia"], price, sl_pct, tp_pct)
        return _strat(name, "HOLD", "MEDIUM", f"LONG activo | RSI {rsi:.0f}", [f"RSI {rsi:.0f}"], price, sl_pct, tp_pct)

    if current_position == "SHORT":
        if bullish_div or rsi < 30:
            return _strat(name, "FLAT", "HIGH", f"Divergencia bullish o RSI bajo → cierre", ["Salida divergencia"], price, sl_pct, tp_pct)
        return _strat(name, "HOLD", "MEDIUM", f"SHORT activo | RSI {rsi:.0f}", [f"RSI {rsi:.0f}"], price, sl_pct, tp_pct)

    if bullish_div and rsi < 50:
        conf = "HIGH" if div_strength > 8 else "MEDIUM"
        signals = [f"Divergencia alcista 1h ✓ (Δ{div_strength:.0f})", f"RSI {rsi:.0f}", "Precio lower low, RSI higher low"]
        return _strat(name, "LONG", conf, f"RSI bullish divergence 1h (Δ{div_strength:.0f})", signals, price, sl_pct, tp_pct)

    if bearish_div and rsi > 50:
        conf = "HIGH" if div_strength > 8 else "MEDIUM"
        signals = [f"Divergencia bajista 1h ✓ (Δ{div_strength:.0f})", f"RSI {rsi:.0f}", "Precio higher high, RSI lower high"]
        return _strat(name, "SHORT", conf, f"RSI bearish divergence 1h (Δ{div_strength:.0f})", signals, price, sl_pct, tp_pct)

    return _strat(name, "FLAT", "MEDIUM", f"Sin divergencia RSI 1h (RSI={rsi:.0f})", ["Esperando divergencia"], price, sl_pct, tp_pct)


# ══════════════════════════════════════════════════════════════════════════════
# SISTEMA DE VOTACIÓN — 2/3 para entrar
# ══════════════════════════════════════════════════════════════════════════════
def run_strategies(client, current_position: str) -> dict:
    r1 = strategy_funding_extreme(client, current_position)
    r2 = strategy_bb_squeeze(client, current_position)
    r3 = strategy_rsi_divergence(client, current_position)

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
                "position_size_pct": 0.10 if conf == "HIGH" else 0.07}

    if short_v >= 2:
        conf = "HIGH" if short_v == 3 else "MEDIUM"
        return {"decision": "SHORT", "confidence": conf,
                "reasoning": f"Voto {short_v}/3 SHORT", "key_signals": all_signals,
                "entry_price": price, "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct,
                "position_size_pct": 0.10 if conf == "HIGH" else 0.07}

    hold_rs = [r for r in [r1, r2, r3] if r["decision"] == "HOLD"]
    if len(hold_rs) >= 2:
        return {**hold_rs[0], "decision": "HOLD", "confidence": "MEDIUM", "reasoning": "Mayoría HOLD"}

    return {"decision": "FLAT", "confidence": "MEDIUM",
            "reasoning": f"Sin mayoría (L:{long_v} S:{short_v} F:{votes['FLAT']})",
            "key_signals": ["Señales divididas"], "entry_price": price,
            "stop_loss_pct": sl_pct, "take_profit_pct": tp_pct, "position_size_pct": 0.07}


# ─── Ciclo principal ──────────────────────────────────────────────────────────
def run_cycle(client, paper: PaperTradingEngine):
    log.info("")
    log.info("=" * 55)
    log.info(f"CICLO — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 55)

    state = paper.state
    open_trade = state.open_trades[0] if state.open_trades else None

    current_position = open_trade.side if open_trade else "FLAT"
    capital = state.current_capital
    log.info(f"Capital: ${capital:.2f} | Posición: {current_position}")

    def _close_trade(trade, exit_price, reason):
        """Cierra un trade en el engine."""
        side = trade.side
        if side == "LONG":
            raw_pnl = (exit_price - trade.entry_price) / trade.entry_price
        else:
            raw_pnl = (trade.entry_price - exit_price) / trade.entry_price
        pnl = round(raw_pnl * trade.size * trade.leverage, 2)
        pnl_pct = round(raw_pnl * trade.leverage * 100, 2)
        trade.exit_price = exit_price; trade.exit_time = datetime.now().isoformat()
        trade.exit_reason = reason; trade.pnl = pnl; trade.pnl_pct = pnl_pct; trade.status = "CLOSED"
        paper.state.current_capital += trade.size + pnl
        paper.state.total_pnl += pnl
        paper.state.total_pnl_pct = round((paper.state.current_capital - paper.state.initial_capital) / paper.state.initial_capital * 100, 2)
        paper.state.closed_trades.append(trade)
        if paper.state.current_capital > paper.state.peak_capital:
            paper.state.peak_capital = paper.state.current_capital
        paper._total_closed += 1
        if pnl > 0: paper._total_wins += 1
        paper.state.win_rate = round(paper._total_wins / paper._total_closed * 100, 1) if paper._total_closed else 0.0
        paper.state.open_trades.remove(trade)
        emoji = "✅" if pnl > 0 else "❌"
        log.info(f"  [PAPER] {emoji} {reason} | ${exit_price:,.0f} | PnL {'+' if pnl>=0 else ''}{pnl:.2f} ({pnl_pct:+.1f}%)")
        paper.add_log(f"{emoji} {reason} @ ${exit_price:,.0f} | PnL {'+' if pnl>=0 else ''}{pnl:.2f}")

    # ── Chequear SL/TP de posición abierta ────────────────────────────────────
    if open_trade:
        df = fetch_ohlcv(client, "1m", 5)
        price = float(df["close"].iloc[-1])
        side = open_trade.side
        if side == "LONG":
            if price <= open_trade.stop_loss:
                _close_trade(open_trade, open_trade.stop_loss, "STOP_LOSS"); open_trade = None; current_position = "FLAT"; paper.save()
            elif price >= open_trade.take_profit:
                _close_trade(open_trade, open_trade.take_profit, "TAKE_PROFIT"); open_trade = None; current_position = "FLAT"; paper.save()
        else:
            if price >= open_trade.stop_loss:
                _close_trade(open_trade, open_trade.stop_loss, "STOP_LOSS"); open_trade = None; current_position = "FLAT"; paper.save()
            elif price <= open_trade.take_profit:
                _close_trade(open_trade, open_trade.take_profit, "TAKE_PROFIT"); open_trade = None; current_position = "FLAT"; paper.save()

    # ── Evaluar estrategias ────────────────────────────────────────────────────
    log.info("Evaluando estrategias...")
    decision = run_strategies(client, current_position)
    action = decision.get("decision", "FLAT")
    conf = decision.get("confidence", "MEDIUM")
    log.info(f"Decisión: {action} | Confianza: {conf}")

    # ── Ejecutar decisión ──────────────────────────────────────────────────────
    if action in ("LONG", "SHORT") and current_position == "FLAT":
        price = decision["entry_price"]
        sl_pct = decision["stop_loss_pct"]
        tp_pct = decision["take_profit_pct"]
        pos_pct = decision["position_size_pct"]
        size = round(capital * pos_pct, 2)
        sl = round(price * (1 - sl_pct) if action == "LONG" else price * (1 + sl_pct), 2)
        tp = round(price * (1 + tp_pct) if action == "LONG" else price * (1 - tp_pct), 2)
        trade = Trade(
            id=f"TB-{int(_time.time())}",
            bot="testbot", side=action,
            entry_price=price, entry_time=datetime.now().isoformat(),
            size=size, stop_loss=sl, take_profit=tp,
            reasoning=decision.get("reasoning", ""),
            confidence=conf, leverage=LEVERAGE,
        )
        paper.state.open_trades.append(trade)
        paper.state.current_capital -= size
        paper.state.trades_today += 1
        paper.add_log(f"✓ {action} @ ${price:,.0f} | SL ${sl:,.0f} | TP ${tp:,.0f} | ${size:.0f}")
        log.info(f"  ✓ ABIERTO {action} @ ${price:,.2f} | SL ${sl:,.2f} | TP ${tp:,.2f} | Size ${size:.2f}")
        paper.save()

    elif action == "FLAT" and current_position != "FLAT" and open_trade:
        df = fetch_ohlcv(client, "1m", 2)
        price = float(df["close"].iloc[-1])
        _close_trade(open_trade, price, "SIGNAL")
        paper.save()
        log.info(f"  ○ CERRADO por señal @ ${price:,.2f}")

    s = paper.state
    # Siempre loguear estado del ciclo para que el dashboard se actualice
    pos_str = f"FLAT" if not s.open_trades else f"{s.open_trades[0].side} @ ${s.open_trades[0].entry_price:,.0f}"
    paper.add_log(f"FLAT — {action} | BTC ${decision['entry_price']:,.0f} | {pos_str}")
    paper.save()
    log.info(f"  [PAPER] Capital: ${s.current_capital:.2f} | P&L: {s.total_pnl:+.2f} | Win: {s.win_rate:.0f}%")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    api_key    = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client     = Client(api_key, api_secret)

    paper = PaperTradingEngine(bot="testbot", initial_capital=INITIAL_CAP, state_file=STATE_FILE)
    paper.state.leverage = LEVERAGE
    paper.save()

    log.info(f"Test Bot — BTC/USDT · x{LEVERAGE} · ${INITIAL_CAP} inicial")
    log.info(f"Estrategias: FUNDING_EXT + BB_SQUEEZE + RSI_DIV_1H")
    log.info(f"Ciclo: {CYCLE_MIN} minutos")

    while True:
        try:
            run_cycle(client, paper)
        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)
        log.info(f"\n⏰ Próximo ciclo en {CYCLE_MIN} minutos...")
        time.sleep(CYCLE_MIN * 60)


if __name__ == "__main__":
    main()
