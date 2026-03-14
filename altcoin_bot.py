"""
Multi-Altcoin Adaptive Bot
===========================
- Trae top 20 altcoins por volumen de Binance Futures automáticamente
- Claude analiza cada una y elige la estrategia óptima:
    * MEAN_REVERSION: RSI sobrevendido/sobrecomprado
    * MOMENTUM: breakout con volumen alto
    * RANGE: oscilación en canal Bollinger
- Sizing dinámico basado en tamaño de la oportunidad (Kelly fraccionado)
- Opera en paralelo, máx 5 posiciones simultáneas
- Paper trading por defecto

SETUP:
  pip install python-binance anthropic python-dotenv pandas numpy
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
import pandas as pd
import numpy as np

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

LEVERAGE = int(os.getenv("ALTCOIN_LEVERAGE", "3"))
MAX_POSITIONS = int(os.getenv("ALTCOIN_MAX_POSITIONS", "5"))
TOTAL_CAPITAL = float(os.getenv("ALTCOIN_CAPITAL", "500"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
INTERVAL_MINUTES = int(os.getenv("ALTCOIN_INTERVAL", "15"))
MIN_VOLUME_USDT = float(os.getenv("ALTCOIN_MIN_VOLUME", "50000000"))  # $50M diarios

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

DATA_DIR = Path("./altcoin_data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

# Excluir stablecoins y tokens irrelevantes
EXCLUDE = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "USDTUSDT", "BTCUSDT",
           "BTCDOMUSDT", "DEFIUSDT", "BNXUSDT", "1000SHIBUSDT"}

import anthropic as anthropic_lib
ai_client = anthropic_lib.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# ─── Binance Client ───────────────────────────────────────────────────────────

def get_client():
    from binance.client import Client
    return Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)


# ─── Market Scanner ───────────────────────────────────────────────────────────

def get_top_altcoins(client, n: int = 20) -> list[dict]:
    """Trae las top N altcoins por volumen 24h en Binance Futures."""
    try:
        tickers = client.futures_ticker()
        altcoins = []

        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT"):
                continue
            if sym in EXCLUDE:
                continue
            # Solo perps activos
            volume = float(t.get("quoteVolume", 0))
            if volume < MIN_VOLUME_USDT:
                continue

            altcoins.append({
                "symbol": sym,
                "price": float(t["lastPrice"]),
                "volume_24h": volume,
                "change_24h": float(t["priceChangePercent"]),
            })

        altcoins.sort(key=lambda x: x["volume_24h"], reverse=True)
        top = altcoins[:n]
        log.info(f"Top {len(top)} altcoins por volumen:")
        for a in top[:5]:
            log.info(f"  {a['symbol']:12} vol: ${a['volume_24h']/1e6:.0f}M | {a['change_24h']:+.1f}%")
        log.info(f"  ... y {len(top)-5} más")
        return top

    except Exception as e:
        log.error(f"Error trayendo altcoins: {e}")
        return []


# ─── Indicadores ──────────────────────────────────────────────────────────────

def get_indicators(client, symbol: str) -> Optional[dict]:
    """Calcula indicadores técnicos para una altcoin."""
    try:
        klines = client.futures_klines(symbol=symbol, interval="15m", limit=100)
        df = pd.DataFrame(klines, columns=[
            "ts", "open", "high", "low", "close", "volume",
            "ct", "qv", "trades", "tbb", "tbq", "ignore"
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # RSI
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = float((100 - (100 / (1 + rs))).iloc[-1])

        # MACD
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_hist = float((ema12 - ema26 - (ema12 - ema26).ewm(span=9, adjust=False).mean()).iloc[-1])
        prev_macd_hist = float((ema12 - ema26 - (ema12 - ema26).ewm(span=9, adjust=False).mean()).iloc[-2])

        # Bollinger
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        bb_pct = float(((close - (sma20 - 2*std20)) / (4*std20)).iloc[-1])

        # ATR
        tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
        atr_pct = float(tr.rolling(14).mean().iloc[-1] / close.iloc[-1] * 100)

        # Volumen
        vol_ratio = float(volume.iloc[-1] / volume.rolling(20).mean().iloc[-1])

        # Volatilidad histórica (para que Claude elija estrategia)
        returns = close.pct_change().dropna()
        hist_vol = float(returns.rolling(20).std().iloc[-1] * 100)  # % diario
        avg_range = float(((high - low) / close).rolling(20).mean().iloc[-1] * 100)

        # Tendencia
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])

        # Funding rate
        try:
            funding = client.futures_funding_rate(symbol=symbol, limit=1)
            funding_rate = float(funding[0]["fundingRate"]) * 100 if funding else 0
        except Exception:
            funding_rate = 0

        return {
            "symbol": symbol,
            "price": float(close.iloc[-1]),
            "rsi": round(rsi, 2),
            "macd_hist": round(macd_hist, 4),
            "macd_cross": "bullish" if macd_hist > 0 and prev_macd_hist <= 0
                          else "bearish" if macd_hist < 0 and prev_macd_hist >= 0
                          else "neutral",
            "bb_pct": round(bb_pct, 3),
            "atr_pct": round(atr_pct, 3),
            "vol_ratio": round(vol_ratio, 2),
            "hist_vol": round(hist_vol, 3),
            "avg_range_pct": round(avg_range, 3),
            "trend": "bullish" if ema50 > ema200 else "bearish",
            "funding_rate": round(funding_rate, 4),
        }

    except Exception as e:
        log.warning(f"Error calculando indicadores para {symbol}: {e}")
        return None


# ─── Claude Analysis ──────────────────────────────────────────────────────────

ADAPTIVE_PROMPT = """Eres un trader experto en altcoins. Analizá esta altcoin y elegí la estrategia óptima basada en su perfil.

ALTCOIN: {symbol}
Precio: ${price:.6f}
RSI(14): {rsi:.1f}
MACD histograma: {macd_hist:.4f} | Cruz: {macd_cross}
Bollinger %B: {bb_pct:.2f} (0=lower, 1=upper)
ATR volatilidad: {atr_pct:.2f}%
Volumen relativo: {vol_ratio:.1f}x
Volatilidad histórica: {hist_vol:.2f}% (20 períodos)
Rango promedio vela: {avg_range_pct:.2f}%
Tendencia EMA50/200: {trend}
Funding rate: {funding_rate:+.4f}%
Cambio 24h: {change_24h:+.1f}%
Volumen 24h: ${volume_24h_m:.0f}M

ESTRATEGIAS DISPONIBLES:
- MEAN_REVERSION: RSI extremo (<30 o >70), esperar rebote. Mejor en alta volatilidad.
- MOMENTUM: MACD bullish/bearish + volumen alto. Seguir tendencia fuerte.
- RANGE: BB%B en extremos, mercado lateral. Comprar lower band, vender upper band.
- SKIP: No hay oportunidad clara ahora.

Capital disponible: ${capital:.2f} USDT | Leverage: {leverage}x | Posiciones abiertas: {open_positions}/{max_positions}

Respondé ÚNICAMENTE con JSON válido:
{{
  "strategy": "MEAN_REVERSION|MOMENTUM|RANGE|SKIP",
  "direction": "LONG|SHORT|SKIP",
  "confidence": "HIGH|MEDIUM|LOW",
  "reasoning": "1-2 oraciones explicando por qué esta estrategia para esta coin",
  "stop_loss_pct": 0.02,
  "take_profit_pct": 0.04,
  "position_size_usdt": 50,
  "key_levels": {{"entry": {price:.6f}, "sl": 0.0, "tp": 0.0}}
}}

position_size_usdt: cuánto USDT usar (máx {max_position_usdt:.0f} según oportunidad)
Si confidence es LOW, usá position_size_usdt pequeño o SKIP."""


def analyze_altcoin(indicators: dict, market_data: dict, capital: float, open_positions: int) -> Optional[dict]:
    """Claude analiza la altcoin y elige estrategia."""
    if not ai_client:
        return None

    max_position = min(capital * 0.20, TOTAL_CAPITAL / MAX_POSITIONS)

    prompt = ADAPTIVE_PROMPT.format(
        **indicators,
        change_24h=market_data.get("change_24h", 0),
        volume_24h_m=market_data.get("volume_24h", 0) / 1e6,
        capital=capital,
        leverage=LEVERAGE,
        open_positions=open_positions,
        max_positions=MAX_POSITIONS,
        max_position_usdt=max_position,
    )

    try:
        response = ai_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())

    except Exception as e:
        log.warning(f"Error analizando {indicators.get('symbol')}: {e}")
        return None


# ─── Paper Trading State ──────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "capital": TOTAL_CAPITAL,
        "positions": {},
        "closed_trades": [],
        "total_pnl": 0.0,
        "cycle_log": [],
    }


def save_state(state: dict):
    closed = state.get("closed_trades", [])
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    dashboard = {
        "bot": "altcoins",
        "initial_capital": TOTAL_CAPITAL,
        "current_capital": round(state["capital"], 2),
        "total_pnl": round(state["total_pnl"], 2),
        "total_pnl_pct": round((state["capital"] - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100, 2),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "open_positions": list(state["positions"].values()),
        "closed_trades": closed[-30:],
        "total_trades": len(closed),
        "cycle_log": state.get("cycle_log", [])[-50:],
        "last_scan": state.get("last_scan", []),       # análisis en vivo
        "scanning": state.get("scanning", False),       # si está escaneando ahora
        "last_updated": datetime.now().isoformat(),
    }
    STATE_FILE.write_text(json.dumps(dashboard, indent=2, default=str))


def add_log(state: dict, msg: str):
    logs = state.get("cycle_log", [])
    logs = [{"time": datetime.now().strftime("%H:%M:%S"), "msg": msg}] + logs[:49]
    state["cycle_log"] = logs


# ─── Order Execution ──────────────────────────────────────────────────────────

def open_position(client, state: dict, symbol: str, analysis: dict, indicators: dict):
    """Abre posición paper o real."""
    direction = analysis["direction"]
    size_usdt = float(analysis.get("position_size_usdt", 30))
    sl_pct = float(analysis.get("stop_loss_pct", 0.02))
    tp_pct = float(analysis.get("take_profit_pct", 0.04))
    price = indicators["price"]

    sl = round(price * (1 - sl_pct) if direction == "LONG" else price * (1 + sl_pct), 8)
    tp = round(price * (1 + tp_pct) if direction == "LONG" else price * (1 - tp_pct), 8)

    position = {
        "symbol": symbol,
        "direction": direction,
        "strategy": analysis["strategy"],
        "entry_price": price,
        "entry_time": datetime.now().isoformat(),
        "size_usdt": size_usdt,
        "stop_loss": sl,
        "take_profit": tp,
        "confidence": analysis["confidence"],
        "reasoning": analysis.get("reasoning", ""),
        "leverage": LEVERAGE,
    }

    if DRY_RUN:
        state["positions"][symbol] = position
        state["capital"] -= size_usdt
        msg = f"✓ PAPER {direction} {symbol} @ ${price:.4f} | {analysis['strategy']} | SL ${sl:.4f} | TP ${tp:.4f} | ${size_usdt:.0f}"
        add_log(state, msg)
        log.info(f"  [PAPER] {msg}")
    else:
        try:
            client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
            side = "BUY" if direction == "LONG" else "SELL"
            qty = round(size_usdt * LEVERAGE / price, 3)
            client.futures_create_order(symbol=symbol, side=side, type="MARKET", quantity=qty)
            client.futures_create_order(symbol=symbol, side="SELL" if side=="BUY" else "BUY",
                                        type="STOP_MARKET", stopPrice=sl, closePosition=True)
            client.futures_create_order(symbol=symbol, side="SELL" if side=="BUY" else "BUY",
                                        type="TAKE_PROFIT_MARKET", stopPrice=tp, closePosition=True)
            state["positions"][symbol] = position
            state["capital"] -= size_usdt
            msg = f"✅ REAL {direction} {symbol} @ ${price:.4f} | ${size_usdt:.0f}"
            add_log(state, msg)
            log.info(f"  [REAL] {msg}")
        except Exception as e:
            log.error(f"  Error abriendo {symbol}: {e}")


def check_positions(client, state: dict):
    """Verifica posiciones abiertas y cierra las que tocaron SL/TP."""
    to_close = []

    for symbol, pos in state["positions"].items():
        try:
            if DRY_RUN:
                # Obtener precio actual
                ticker = client.futures_ticker(symbol=symbol)
                current_price = float(ticker["lastPrice"])
            else:
                position_info = client.futures_position_information(symbol=symbol)
                current_price = float(position_info[0]["markPrice"])

            direction = pos["direction"]
            entry = pos["entry_price"]
            sl = pos["stop_loss"]
            tp = pos["take_profit"]
            change = (current_price - entry) / entry

            hit_tp = (direction == "LONG" and current_price >= tp) or \
                     (direction == "SHORT" and current_price <= tp)
            hit_sl = (direction == "LONG" and current_price <= sl) or \
                     (direction == "SHORT" and current_price >= sl)

            if hit_tp or hit_sl:
                exit_reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"
                exit_price = tp if hit_tp else sl

                pnl_pct = (exit_price - entry) / entry * LEVERAGE
                if direction == "SHORT":
                    pnl_pct = -pnl_pct
                pnl = round(pos["size_usdt"] * pnl_pct, 2)

                trade = {**pos, "exit_price": exit_price, "exit_time": datetime.now().isoformat(),
                         "exit_reason": exit_reason, "pnl": pnl, "pnl_pct": round(pnl_pct*100, 2)}
                state["closed_trades"].append(trade)
                state["capital"] += pos["size_usdt"] + pnl
                state["total_pnl"] += pnl
                to_close.append(symbol)

                emoji = "✅" if pnl > 0 else "❌"
                msg = f"{emoji} {exit_reason} {symbol} | exit ${exit_price:.4f} | P&L {'+' if pnl>=0 else ''}${pnl:.2f} ({pnl_pct*100:+.1f}%)"
                add_log(state, msg)
                log.info(f"  [PAPER] {msg}")

        except Exception as e:
            log.warning(f"Error verificando {symbol}: {e}")

    for sym in to_close:
        del state["positions"][sym]


# ─── Main Loop ────────────────────────────────────────────────────────────────

def run_cycle(client):
    log.info(f"\n{'#'*55}")
    log.info(f"ALTCOIN CYCLE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"{'#'*55}")

    state = load_state()

    # 1. Verificar posiciones abiertas
    if state["positions"]:
        log.info(f"Verificando {len(state['positions'])} posiciones abiertas...")
        check_positions(client, state)

    capital = state["capital"]
    open_count = len(state["positions"])

    log.info(f"Capital: ${capital:.2f} | Posiciones: {open_count}/{MAX_POSITIONS}")
    log.info(f"P&L total: ${state['total_pnl']:+.2f}")

    # 2. Buscar nuevas oportunidades si hay slots
    slots = MAX_POSITIONS - open_count
    if slots <= 0:
        log.info("Máximo de posiciones alcanzado. Esperando...")
        save_state(state)
        return

    # 3. Escanear mercado
    altcoins = get_top_altcoins(client, n=20)
    if not altcoins:
        save_state(state)
        return

    # Filtrar las que ya tienen posición
    candidates = [a for a in altcoins if a["symbol"] not in state["positions"]]
    log.info(f"\nAnalizando {min(len(candidates), slots*3)} candidatos con Claude...")
    state["scanning"] = True
    state["last_scan"] = []
    save_state(state)

    opportunities = []
    analyzed = 0

    for coin in candidates:
        if analyzed >= slots * 3:  # analizar hasta 3x los slots disponibles
            break

        symbol = coin["symbol"]
        indicators = get_indicators(client, symbol)
        if not indicators:
            continue

        analyzed += 1
        log.info(f"  {symbol}: RSI={indicators['rsi']:.0f} | BB={indicators['bb_pct']:.2f} | Vol={indicators['vol_ratio']:.1f}x | HistVol={indicators['hist_vol']:.2f}%")

        analysis = analyze_altcoin(indicators, coin, capital, open_count)
        if not analysis:
            continue

        strategy = analysis.get("strategy", "SKIP")
        direction = analysis.get("direction", "SKIP")
        confidence = analysis.get("confidence", "LOW")

        scan_entry = {
            "symbol": symbol,
            "rsi": indicators["rsi"],
            "bb_pct": indicators["bb_pct"],
            "vol_ratio": indicators["vol_ratio"],
            "hist_vol": indicators["hist_vol"],
            "trend": indicators["trend"],
            "macd_cross": indicators["macd_cross"],
            "change_24h": coin.get("change_24h", 0),
            "strategy": strategy,
            "direction": direction,
            "confidence": confidence,
            "reasoning": analysis.get("reasoning", "") if analysis else "",
            "size_usdt": analysis.get("position_size_usdt", 0) if analysis else 0,
            "scanned_at": datetime.now().strftime("%H:%M:%S"),
        }
        state["last_scan"].append(scan_entry)
        save_state(state)  # guardar en tiempo real

        if strategy == "SKIP" or direction == "SKIP" or confidence == "LOW":
            log.info(f"    → SKIP ({strategy}, {confidence})")
            continue

        log.info(f"    → {strategy} {direction} | {confidence} | {analysis.get('reasoning', '')[:60]}")
        opportunities.append((indicators, coin, analysis))

        time.sleep(0.3)  # throttle API

    # 4. Ejecutar mejores oportunidades
    # Ordenar por confidence y size
    conf_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    opportunities.sort(key=lambda x: (conf_order.get(x[2].get("confidence","LOW"), 0),
                                       x[2].get("position_size_usdt", 0)), reverse=True)

    executed = 0
    for indicators, coin, analysis in opportunities[:slots]:
        if capital < 20:
            log.warning("Capital insuficiente.")
            break
        open_position(client, state, coin["symbol"], analysis, indicators)
        open_count += 1
        executed += 1

    state["scanning"] = False
    log.info(f"\nCiclo completo: {executed} nuevas posiciones | Total abiertas: {open_count}")
    save_state(state)


def run_forever():
    log.info("🤖 Multi-Altcoin Adaptive Bot iniciado")
    log.info(f"   Modo: {'DRY RUN' if DRY_RUN else '⚠️  REAL'}")
    log.info(f"   Capital: ${TOTAL_CAPITAL} | Leverage: {LEVERAGE}x | Max posiciones: {MAX_POSITIONS}")
    log.info(f"   Intervalo: {INTERVAL_MINUTES} min")

    client = get_client()

    while True:
        try:
            run_cycle(client)
        except KeyboardInterrupt:
            log.info("\n🛑 Bot detenido.")
            break
        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)

        log.info(f"\n⏰ Próximo ciclo en {INTERVAL_MINUTES} minutos...")
        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    import sys
    client = get_client()
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_cycle(client)
    else:
        run_forever()
