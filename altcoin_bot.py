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

LEVERAGE         = int(os.getenv("ALTCOIN_LEVERAGE",    "20"))   # x20
MAX_POSITIONS    = int(os.getenv("ALTCOIN_MAX_POSITIONS","5"))    # máx 5 posiciones simultáneas
TOTAL_CAPITAL    = float(os.getenv("ALTCOIN_CAPITAL",   "500"))
DRY_RUN          = os.getenv("DRY_RUN", "true").lower() == "true"
INTERVAL_MINUTES = int(os.getenv("ALTCOIN_INTERVAL",   "3"))
MIN_VOLUME_USDT  = float(os.getenv("ALTCOIN_MIN_VOLUME","300000000"))  # $300M — suficiente liquidez
TOP_N            = int(os.getenv("ALTCOIN_TOP_N",       "20"))   # escanear top 20 por volumen
CANDLE_INTERVAL  = os.getenv("ALTCOIN_CANDLE", "5m")            # velas de 5m
DEFAULT_SL_PCT   = float(os.getenv("ALTCOIN_SL",  "0.008"))     # SL 0.8%
DEFAULT_TP_PCT   = float(os.getenv("ALTCOIN_TP",  "0.025"))     # TP 2.5% (R:R 3.1:1)
TRAILING_TRIGGER = float(os.getenv("ALTCOIN_TRAIL", "0.005"))   # mover SL a BE cuando +0.5%
TIME_LIMIT_MIN   = int(os.getenv("ALTCOIN_TIME_LIMIT", "90"))    # cerrar si stale > 90min

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

DATA_DIR = Path(__file__).parent / "altcoin_data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

# Excluir stablecoins y tokens con historial de problemas de liquidez
EXCLUDE = {"BUSDUSDT", "USDCUSDT", "TUSDUSDT", "USDTUSDT", "BTCUSDT",
           "BTCDOMUSDT", "DEFIUSDT", "BNXUSDT", "1000SHIBUSDT",
           "SIRENUSDT", "LOOMUSDT", "CVPUSDT", "BALUSDT",  # historial flash crash
           "1000LUNCUSDT", "LUNA2USDT",  # tokens volátiles extremos
           # Baja liquidez o WR consistentemente bajo:
           "ONTUSDT", "RIVERUSDT", "HYPEUSDT", "XAGUSDT", "XAUUSDT",
           "PAXGUSDT", "1000PEPEUSDT"}

from ai_client import call_ai, parse_json_response, is_available, get_model_info
log.info(f"AI Engine: {get_model_info()}")

# ─── Binance Client ───────────────────────────────────────────────────────────

def get_client():
    from binance.client import Client
    return Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)


# ─── Market Scanner ───────────────────────────────────────────────────────────

def get_top_altcoins(client, n: int = TOP_N) -> list[dict]:
    """Top N altcoins por volumen 24h — solo los más líquidos."""
    try:
        tickers = client.futures_ticker()
        altcoins = []

        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT"):
                continue
            if sym in EXCLUDE:
                continue
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
        log.info(f"Top {len(top)} altcoins (>${MIN_VOLUME_USDT/1e6:.0f}M vol):")
        for a in top:
            log.info(f"  {a['symbol']:12} vol: ${a['volume_24h']/1e6:.0f}M | {a['change_24h']:+.1f}%")
        return top

    except Exception as e:
        log.error(f"Error trayendo altcoins: {e}")
        return []


# ─── Indicadores ──────────────────────────────────────────────────────────────

def get_indicators(client, symbol: str) -> Optional[dict]:
    """Indicadores técnicos en velas de 5m — EMA 9/21, VWAP, RSI, MACD, BB, ATR."""
    try:
        klines = client.futures_klines(symbol=symbol, interval=CANDLE_INTERVAL, limit=120)
        df = pd.DataFrame(klines, columns=[
            "ts", "open", "high", "low", "close", "volume",
            "ct", "qv", "trades", "tbb", "tbq", "ignore"
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)

        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]

        # EMA 9 / 21 (señal principal, como scalping)
        ema9  = close.ewm(span=9,  adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        ema9_cur  = float(ema9.iloc[-1])
        ema21_cur = float(ema21.iloc[-1])
        ema9_prev = float(ema9.iloc[-2])
        ema21_prev = float(ema21.iloc[-2])
        cross_bullish = ema9_prev <= ema21_prev and ema9_cur > ema21_cur
        cross_bearish = ema9_prev >= ema21_prev and ema9_cur < ema21_cur
        ema_trend = "bullish" if ema9_cur > ema21_cur else "bearish"

        # VWAP (sesión — últimas 78 velas de 5m ≈ 6.5h de mercado)
        typ_price = (high + low + close) / 3
        vol_sum = volume.rolling(78).sum().iloc[-1]
        vwap = float((typ_price * volume).rolling(78).sum().iloc[-1] / vol_sum) if vol_sum > 0 else float(close.iloc[-1])
        price_vs_vwap = (float(close.iloc[-1]) - vwap) / vwap * 100  # % sobre/bajo VWAP

        # RSI 14
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = float((100 - (100 / (1 + rs))).iloc[-1])

        # MACD rápido (3,10,5) — igual que scalping
        macd_line  = close.ewm(span=3,  adjust=False).mean() - close.ewm(span=10, adjust=False).mean()
        signal     = macd_line.ewm(span=5, adjust=False).mean()
        macd_hist  = float((macd_line - signal).iloc[-1])
        prev_hist  = float((macd_line - signal).iloc[-2])
        macd_cross = ("bullish" if macd_hist > 0 and prev_hist <= 0
                      else "bearish" if macd_hist < 0 and prev_hist >= 0
                      else "neutral")

        # Bollinger
        sma20  = close.rolling(20).mean()
        std20  = close.rolling(20).std()
        bb_pct = float(((close - (sma20 - 2*std20)) / (4*std20)).iloc[-1])

        # ATR
        tr     = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
        atr_pct = float(tr.rolling(14).mean().iloc[-1] / close.iloc[-1] * 100)

        # Volumen relativo
        vol_mean = volume.rolling(20).mean().iloc[-1]
        vol_ratio = float(volume.iloc[-1] / vol_mean) if vol_mean > 0 else 1.0

        # Volatilidad histórica
        returns   = close.pct_change().dropna()
        hist_vol  = float(returns.rolling(20).std().iloc[-1] * 100)
        avg_range = float(((high - low) / close).rolling(20).mean().iloc[-1] * 100)

        # EMA 50/200 para tendencia macro
        ema50  = float(close.ewm(span=50,  adjust=False).mean().iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])

        # Funding rate
        try:
            funding = client.futures_funding_rate(symbol=symbol, limit=1)
            funding_rate = float(funding[0]["fundingRate"]) * 100 if funding else 0
        except Exception:
            funding_rate = 0

        # NaN check
        if any(v != v for v in [rsi, bb_pct, vol_ratio, atr_pct, vwap]):
            return None

        return {
            "symbol":        symbol,
            "price":         float(close.iloc[-1]),
            "ema9":          round(ema9_cur, 6),
            "ema21":         round(ema21_cur, 6),
            "ema_trend":     ema_trend,
            "cross_bullish": cross_bullish,
            "cross_bearish": cross_bearish,
            "vwap":          round(vwap, 6),
            "price_vs_vwap": round(price_vs_vwap, 3),
            "rsi":           round(rsi, 2),
            "macd_hist":     round(macd_hist, 6),
            "macd_cross":    macd_cross,
            "bb_pct":        round(bb_pct, 3),
            "atr_pct":       round(atr_pct, 3),
            "vol_ratio":     round(vol_ratio, 2),
            "hist_vol":      round(hist_vol, 3),
            "avg_range_pct": round(avg_range, 3),
            "trend":         "bullish" if ema50 > ema200 else "bearish",
            "funding_rate":  round(funding_rate, 4),
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

REGLAS DE DIRECCIÓN — CRÍTICO:
1. MOMENTUM bullish (MACD+, vol alto, trend bullish) → LONG
2. MOMENTUM bearish (MACD-, vol alto, trend bearish) → SHORT  
3. MEAN_REVERSION: RSI<25 → LONG | RSI>75 → SHORT (independiente de tendencia)
4. RANGE: BB%B<0.1 → LONG | BB%B>0.9 → SHORT
5. Cambio 24h negativo grande (<-3%) + RSI bajo → LONG (rebote)
6. Cambio 24h positivo grande (>+5%) + RSI alto → SHORT (corrección)
7. NO tenés sesgo alcista. SHORT es igual de válido que LONG.
8. Si ya hay {open_positions} LONGs abiertos → priorizar SHORT si hay señal

SEÑALES PARA SHORT (buscalas activamente):
- RSI > 70 → sobrecomprado, SHORT
- BB%B > 0.85 → precio en banda superior, SHORT
- MACD bearish cross (histograma cruzó a negativo) → SHORT
- Cambio 24h > +8% → posible corrección, SHORT
- funding_rate > 0.02% → longs pagando, favorece SHORT

SEÑALES PARA LONG:
- RSI < 30 → sobrevendido, LONG
- BB%B < 0.15 → precio en banda inferior, LONG  
- MACD bullish cross (histograma cruzó a positivo) → LONG
- Cambio 24h < -8% → posible rebote, LONG

ESTRATEGIAS:
- MEAN_REVERSION: RSI extremo (<30=LONG, >70=SHORT) sin importar tendencia
- MOMENTUM: seguir MACD cross con volumen alto
- RANGE: BB%B extremos (<0.1=LONG, >0.9=SHORT)
- SKIP: sin señales claras

BALANCE OBLIGATORIO: Si {open_long_count} posiciones son LONG y {open_short_count} son SHORT,
priorizar la dirección con menos posiciones para diversificar riesgo.

Capital disponible: ${capital:.2f} USDT | Leverage: {leverage}x | Posiciones abiertas: {open_positions}/{max_positions}

Respondé ÚNICAMENTE con JSON válido:
{{
  "strategy": "MEAN_REVERSION|MOMENTUM|RANGE|SKIP",
  "direction": "LONG|SHORT|SKIP",
  "confidence": "HIGH|MEDIUM|LOW",
  "reasoning": "1-2 oraciones. Mencioná explícitamente la tendencia y por qué SHORT o LONG",
  "stop_loss_pct": 0.03,
  "take_profit_pct": 0.07,
  "position_size_usdt": 50,
  "key_levels": {{"entry": {price:.6f}, "sl": 0.0, "tp": 0.0}}
}}

IMPORTANTE: SHORT es tan válido como LONG. En mercado bearish SIEMPRE SHORT.
position_size_usdt: máx {max_position_usdt:.0f} USDT.
Si confidence LOW → SKIP obligatorio."""

# ─── Paper Trading State ──────────────────────────────────────────────────────

def load_state() -> dict:
    default = {
        "capital": TOTAL_CAPITAL,
        "positions": {},
        "closed_trades": [],
        "total_pnl": 0.0,
        "cycle_log": [],
        "last_scan": [],
        "scanning": False,
        "cooldowns": {},
        "manual_close": [],
    }
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            state = dict(default)
            state["capital"] = data.get("capital", data.get("current_capital", TOTAL_CAPITAL))
            state["total_pnl"] = sum((t.get("pnl") or 0) for t in data.get("all_closed_trades", data.get("closed_trades", [])))
            positions = data.get("positions", {})
            if isinstance(positions, list):
                positions = {}
            state["positions"] = positions
            all_closed = data.get("all_closed_trades", data.get("closed_trades", []))
            state["closed_trades"] = all_closed
            state["cycle_log"] = data.get("cycle_log", [])
            state["last_scan"] = data.get("last_scan", [])
            state["scanning"] = data.get("scanning", False)
            state["cooldowns"] = data.get("cooldowns", {})
            state["manual_close"] = data.get("manual_close", [])
            log.info(f"Estado cargado: ${state['capital']:.2f} capital | {len(state['positions'])} posiciones | {len(state['closed_trades'])} trades cerrados | {len(state['cooldowns'])} cooldowns")
            return state
        except Exception as e:
            log.warning(f"Error cargando estado: {e} — iniciando fresh")
    return default


def save_state(state: dict):
    closed = state.get("closed_trades", [])
    state["total_pnl"] = round(sum((t.get("pnl") or 0) for t in closed), 2)
    state["capital"] = round(TOTAL_CAPITAL - sum(p.get("size_usdt", 0) for p in state.get("positions", {}).values()) + state["total_pnl"], 2)
    wins = [t for t in closed if t.get("pnl", 0) > 0]
    dashboard = {
        "bot": "altcoins",
        "initial_capital": TOTAL_CAPITAL,
        "current_capital": round(state["capital"], 2),
        "total_pnl": round(state["total_pnl"], 2),
        "total_pnl_raw": round(state["total_pnl"], 2),
        "total_pnl_pct": round(state["total_pnl"] / TOTAL_CAPITAL * 100, 2),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "open_positions": list(state["positions"].values()),
        "positions": state["positions"],
        "closed_trades": closed[-30:],
        "all_closed_trades": state.get("closed_trades", []),
        "total_trades": len(closed),
        "capital": round(state["capital"], 2),
        "total_pnl_raw": round(state["total_pnl"], 2),
        "cycle_log": state.get("cycle_log", [])[-50:],
        "last_scan": state.get("last_scan", []),
        "scanning": state.get("scanning", False),
        "cooldowns": state.get("cooldowns", {}),
        "manual_close": state.get("manual_close", []),
        "last_updated": datetime.now().isoformat(),
    }
    def clean_nan(obj):
        if isinstance(obj, float) and (obj != obj):
            return None
        if isinstance(obj, dict):
            return {k: clean_nan(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [clean_nan(v) for v in obj]
        return obj
    try:
        if STATE_FILE.exists():
            disk = json.loads(STATE_FILE.read_text())
            disk_closed = disk.get("all_closed_trades", disk.get("closed_trades", []))
            bot_closed = dashboard["all_closed_trades"]
            if len(disk_closed) > len(bot_closed):
                dashboard["all_closed_trades"] = disk_closed
                dashboard["closed_trades"] = disk_closed[-30:]
                dashboard["total_pnl"] = round(sum(t.get("pnl",0) for t in disk_closed), 2)
                dashboard["total_pnl_raw"] = dashboard["total_pnl"]
                dashboard["total_pnl_pct"] = round(dashboard["total_pnl"]/TOTAL_CAPITAL*100, 2)
                d_wins = [t for t in disk_closed if t.get("pnl",0)>0]
                dashboard["win_rate"] = round(len(d_wins)/len(disk_closed)*100,1) if disk_closed else 0
                dashboard["total_trades"] = len(disk_closed)
            dashboard["cooldowns"] = {**disk.get("cooldowns",{}), **dashboard.get("cooldowns",{})}
            current_cooldowns = dashboard.get("cooldowns", {})
            disk_positions = disk.get("positions", {})
            bot_positions = dashboard.get("positions", {})
            disk_positions_valid = {k: v for k, v in disk_positions.items() if k not in current_cooldowns}
            if disk_positions_valid and not bot_positions:
                dashboard["positions"] = disk_positions_valid
                dashboard["open_positions"] = list(disk_positions_valid.values())
    except Exception as e:
        log.warning(f"Error en merge de estado: {e}")
    import os as _os
    tmp = STATE_FILE.parent / f".state_tmp_{_os.getpid()}.json"
    tmp.write_text(json.dumps(clean_nan(dashboard), indent=2, default=str))
    _os.replace(tmp, STATE_FILE)


def add_log(state: dict, msg: str):
    logs = state.get("cycle_log", [])
    logs = [{"time": datetime.now().strftime("%H:%M:%S"), "msg": msg}] + logs[:49]
    state["cycle_log"] = logs



def analyze_altcoin(indicators: dict, market_data: dict, capital: float, open_positions: int, open_longs: int = 0, open_shorts: int = 0) -> Optional[dict]:
    """
    Análisis híbrido: scoring técnico filtra primero, Claude decide en los mejores candidatos.
    """
    from ai_client import call_ai, parse_json_response, is_available

    rsi          = indicators.get("rsi", 50)
    bb_pct       = indicators.get("bb_pct", 0.5)
    macd_hist    = indicators.get("macd_hist", 0)
    macd_cross   = indicators.get("macd_cross", "neutral")
    vol_ratio    = indicators.get("vol_ratio", 1)
    atr_pct      = indicators.get("atr_pct", 1)
    trend        = indicators.get("trend", "neutral")
    ema_trend    = indicators.get("ema_trend", "neutral")
    cross_bull   = indicators.get("cross_bullish", False)
    cross_bear   = indicators.get("cross_bearish", False)
    price_vs_vwap = indicators.get("price_vs_vwap", 0)
    funding      = indicators.get("funding_rate", 0)
    change_24h   = market_data.get("change_24h", 0)
    price        = indicators.get("price", 0)
    symbol       = indicators.get("symbol", "")

    # Datos inválidos
    if atr_pct < 0.05 or rsi != rsi or bb_pct != bb_pct:
        return None

    # ── Scoring: EMA cross + VWAP como señales primarias (estilo scalping) ──
    score = 0
    signals = []

    # ── Señales primarias (EMA 9/21 cross + VWAP) ───────────────────────────
    if cross_bull:
        score += 3; signals.append("EMA9 cruzó arriba EMA21 ↑")
    elif ema_trend == "bullish":
        score += 1; signals.append("EMA bullish")

    if cross_bear:
        score -= 3; signals.append("EMA9 cruzó abajo EMA21 ↓")
    elif ema_trend == "bearish":
        score -= 1; signals.append("EMA bearish")

    if price_vs_vwap > 0.1:
        score += 1; signals.append(f"Sobre VWAP +{price_vs_vwap:.2f}%")
    elif price_vs_vwap < -0.1:
        score -= 1; signals.append(f"Bajo VWAP {price_vs_vwap:.2f}%")

    # ── Señales confirmadoras ────────────────────────────────────────────────
    if macd_cross == "bullish":   score += 2; signals.append("MACD bullish cross")
    elif macd_cross == "bearish": score -= 2; signals.append("MACD bearish cross")
    elif macd_hist > 0:           score += 1
    elif macd_hist < 0:           score -= 1

    # RSI — zona operativa (no sobreextendido)
    if 35 <= rsi <= 55:   score += 1 if score > 0 else -1   # confirma dirección
    elif rsi < 25:        score += 2; signals.append(f"RSI sobrevendido {rsi:.0f}")
    elif rsi > 75:        score -= 2; signals.append(f"RSI sobrecomprado {rsi:.0f}")
    elif rsi > 65 and score > 0:   score -= 1   # momentum LONG sobreextendido
    elif rsi < 35 and score < 0:   score += 1   # momentum SHORT sobreextendido

    # Volumen confirma
    if vol_ratio > 1.8:
        score = int(score * 1.3); signals.append(f"Vol alto {vol_ratio:.1f}x")

    # Filtros de riesgo
    if funding > 0.03:   score -= 1; signals.append("Funding alto")
    elif funding < -0.03: score += 1; signals.append("Funding negativo")

    # Evitar entrar en tendencias muy extendidas
    if change_24h > 12:  score -= 2; signals.append(f"Sobreextendido +{change_24h:.1f}%")
    elif change_24h < -12: score += 2; signals.append(f"Caída extrema {change_24h:.1f}%")

    # Balance de posiciones
    if open_longs > open_shorts + 1: score -= 1
    elif open_shorts > open_longs + 1: score += 1

    # Necesita señal mínima de 2 (antes era 1 — más selectivo)
    if abs(score) < 2:
        return None

    technical_direction = "LONG" if score > 0 else "SHORT"
    log.info(f"    Score técnico: {score:+d} → {technical_direction} | {' | '.join(signals[:3])}")

    # ── Paso 2: Claude confirma o descarta ───────────────────────────────────
    if not is_available():
        # Fallback a scoring puro si Claude no está disponible
        if abs(score) >= 4:   confidence = "HIGH"
        elif abs(score) >= 2: confidence = "MEDIUM"
        else: return None
        direction = technical_direction
        sl_pct = DEFAULT_SL_PCT
        tp_pct = DEFAULT_TP_PCT
        reasoning = f"Score {score:+d} | {' | '.join(signals[:3])}"
    else:
        prompt = f"""Trader experto en crypto futuros, velas 5m. Confirmá o descartá la señal.

{symbol} | Precio: ${price:.6f} | Cambio 24h: {change_24h:+.1f}%
EMA trend: {ema_trend} | Cross: {'↑ BULLISH' if cross_bull else '↓ BEARISH' if cross_bear else 'ninguno'}
VWAP: {price_vs_vwap:+.2f}% | RSI: {rsi:.0f} | MACD: {macd_cross}
Vol: {vol_ratio:.1f}x | ATR: {atr_pct:.2f}% | Funding: {funding:+.4f}%

Score: {score:+d} → {technical_direction} | Señales: {', '.join(signals[:4])}
Posiciones: {open_longs}L / {open_shorts}S abiertas

Respondé SOLO JSON (SL máx 1.5%, TP máx 5%):
{{
  "direction": "{technical_direction}|SKIP",
  "confidence": "HIGH|MEDIUM|LOW",
  "reasoning": "1 oración",
  "stop_loss_pct": 0.008,
  "take_profit_pct": 0.025
}}
SKIP solo si la señal es claramente inválida."""

        try:
            raw = call_ai(prompt, max_tokens=150)
            result = parse_json_response(raw)
            direction = result.get("direction", technical_direction)
            confidence = result.get("confidence", "MEDIUM")
            if direction == "SKIP" or confidence == "LOW":
                log.info(f"    Claude descartó la señal")
                return None
            reasoning = result.get("reasoning", f"Score {score:+d}")
            sl_pct = min(float(result.get("stop_loss_pct",  DEFAULT_SL_PCT)), 0.015)
            tp_pct = min(float(result.get("take_profit_pct", DEFAULT_TP_PCT)), 0.05)
            log.info(f"    Claude confirma: {direction} | {confidence} | {reasoning}")
        except Exception as e:
            log.warning(f"    Error Claude: {e} — usando scoring técnico")
            direction = technical_direction
            confidence = "MEDIUM" if abs(score) >= 3 else "LOW"
            if confidence == "LOW": return None
            sl_pct = DEFAULT_SL_PCT
            tp_pct = DEFAULT_TP_PCT
            reasoning = f"Score {score:+d} | {' | '.join(signals[:3])}"

    # Estrategia basada en señal dominante
    if cross_bull or cross_bear:
        strategy = "EMA_CROSS"
    elif rsi < 30 or rsi > 70:
        strategy = "MEAN_REVERSION"
    elif macd_cross in ("bullish", "bearish") and vol_ratio > 1.5:
        strategy = "MOMENTUM"
    else:
        strategy = "RANGE"

    # Sizing: capital dividido en MAX_POSITIONS partes iguales
    max_position = TOTAL_CAPITAL / MAX_POSITIONS
    size = round(max_position * (1.0 if confidence == "HIGH" else 0.7), 2)

    return {
        "strategy": strategy,
        "direction": direction,
        "confidence": confidence,
        "reasoning": reasoning if 'reasoning' in dir() else f"Score {score:+d}",
        "stop_loss_pct": sl_pct,
        "take_profit_pct": tp_pct,
        "position_size_usdt": size,
        "key_levels": {"entry": price, "sl": 0.0, "tp": 0.0}
    }


def open_position(client, state: dict, symbol: str, analysis: dict, indicators: dict):
    """Abre posición paper o real."""
    direction = analysis["direction"]
    size_usdt = float(analysis.get("position_size_usdt", 30))
    sl_pct = float(analysis.get("stop_loss_pct", 0.03))   # default 3%
    tp_pct = float(analysis.get("take_profit_pct", 0.07))  # default 7% (R:R 2.3:1)
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


def get_btc_macro(client) -> str:
    """BTC 1h macro trend: bullish / bearish / soft_bearish / soft_bullish.
    Solo bloquea entradas en tendencias fuertes (gap > 1.5%)."""
    try:
        klines = client.futures_klines(symbol="BTCUSDT", interval="1h", limit=210)
        closes = pd.Series([float(k[4]) for k in klines])
        ema50  = closes.ewm(span=50,  adjust=False).mean().iloc[-1]
        ema200 = closes.ewm(span=200, adjust=False).mean().iloc[-1]
        gap_pct = abs(ema50 - ema200) / ema200 * 100
        if ema50 > ema200:
            return "bullish" if gap_pct > 1.5 else "soft_bullish"
        else:
            return "bearish" if gap_pct > 1.5 else "soft_bearish"
    except Exception:
        return "neutral"


def _close_position(state, symbol, pos, exit_price, exit_reason, note=""):
    """Helper: cierra una posición, actualiza estado y loguea."""
    entry = pos["entry_price"]
    direction = pos["direction"]
    lev = pos.get("leverage", LEVERAGE)

    pnl_pct = (exit_price - entry) / entry * lev
    if direction == "SHORT":
        pnl_pct = -pnl_pct
    pnl = max(round(pos["size_usdt"] * pnl_pct, 2), -pos["size_usdt"])
    pnl_pct = pnl / pos["size_usdt"]

    trade = {**pos, "exit_price": exit_price, "exit_time": datetime.now().isoformat(),
             "exit_reason": exit_reason, "pnl": pnl, "pnl_pct": round(pnl_pct * 100, 2)}
    state["closed_trades"].append(trade)
    state["capital"]   += pos["size_usdt"] + pnl
    state["total_pnl"] += pnl

    emoji = "✅" if pnl > 0 else "❌"
    msg = f"{emoji} {exit_reason} {symbol}{' '+note if note else ''} | exit ${exit_price:.6f} | P&L {'+' if pnl>=0 else ''}${pnl:.2f} ({pnl_pct*100:+.1f}%)"
    add_log(state, msg)
    log.info(f"  [PAPER] {msg}")
    try:
        _log_trade({**trade, "bot": "altcoins", "symbol": symbol,
                    "size": trade.get("size_usdt", 0), "leverage": lev})
    except Exception:
        pass
    return pnl


def check_positions(client, state: dict, scenario=None):
    """Verifica posiciones abiertas: SL/TP, trailing stop, time limit, emergency."""
    to_close = []
    now = datetime.now()

    for symbol, pos in list(state["positions"].items()):
        try:
            if DRY_RUN:
                ticker = client.futures_ticker(symbol=symbol)
                current_price = float(ticker["lastPrice"])
            else:
                position_info = client.futures_position_information(symbol=symbol)
                current_price = float(position_info[0]["markPrice"])

            entry     = pos["entry_price"]
            direction = pos["direction"]
            lev       = pos.get("leverage", LEVERAGE)

            if current_price <= 0 or current_price < entry * 0.01:
                log.warning(f"  ⚠️  {symbol}: precio sospechoso — cierre de emergencia")
                current_price = entry * 0.01

            sl = pos["stop_loss"]
            tp = pos["take_profit"]

            raw_change    = (current_price - entry) / entry
            unrealized_pct = (raw_change if direction == "LONG" else -raw_change) * lev

            # ── Trailing stop dinámico (ATR-based — acompaña el movimiento) ──
            raw_move = abs(raw_change)

            # Actualizar best_price en la posición
            if "best_price" not in pos or pos["best_price"] is None:
                pos["best_price"] = current_price
            if direction == "LONG":
                pos["best_price"] = max(pos["best_price"], current_price)
            else:
                pos["best_price"] = min(pos["best_price"], current_price)

            best = pos["best_price"]
            profit_raw = (best - entry) / entry if direction == "LONG" else (entry - best) / entry

            base_trigger = scenario.get("alt_trailing_trigger", TRAILING_TRIGGER) if scenario else TRAILING_TRIGGER

            if profit_raw >= base_trigger:  # mover SL cuando profit >= trigger base
                # Distancia dinámica: % del precio que se reduce con el profit
                if profit_raw < base_trigger * 2:
                    trail_pct = 0.008     # 0.8% — zona breakeven
                elif profit_raw < base_trigger * 4:
                    trail_pct = 0.005     # 0.5% — lock profit
                else:
                    trail_pct = 0.003     # 0.3% — trailing ajustado

                if direction == "LONG":
                    new_sl = round(best * (1 - trail_pct), 8)
                    if new_sl > pos["stop_loss"]:
                        pos["stop_loss"] = new_sl
                        pos["trailing_activated"] = True
                        state["positions"][symbol] = pos
                        log.info(f"  📍 TRAILING {symbol}: SL → ${new_sl:.6f} (profit +{profit_raw*100:.2f}%, best ${best:.4f}, dist {trail_pct*100:.1f}%)")
                else:
                    new_sl = round(best * (1 + trail_pct), 8)
                    if new_sl < pos["stop_loss"]:
                        pos["stop_loss"] = new_sl
                        pos["trailing_activated"] = True
                        state["positions"][symbol] = pos
                        log.info(f"  📍 TRAILING {symbol}: SL → ${new_sl:.6f} (profit +{profit_raw*100:.2f}%, best ${best:.4f}, dist {trail_pct*100:.1f}%)")

            # ── Condiciones de cierre ─────────────────────────────────────────
            hit_tp = (direction == "LONG" and current_price >= tp) or \
                     (direction == "SHORT" and current_price <= tp)
            hit_sl = (direction == "LONG" and current_price <= pos["stop_loss"]) or \
                     (direction == "SHORT" and current_price >= pos["stop_loss"])
            emergency = unrealized_pct < -0.20

            # Time limit: cerrar posición stale después de TIME_LIMIT_MIN
            entry_dt = datetime.fromisoformat(pos["entry_time"])
            time_expired = (now - entry_dt).total_seconds() / 60 >= TIME_LIMIT_MIN

            if hit_tp or hit_sl or emergency or time_expired:
                if emergency and not hit_sl:
                    exit_reason = "EMERGENCY_EXIT"
                    exit_price  = current_price
                    log.warning(f"  🚨 EMERGENCY EXIT {symbol}: {unrealized_pct*100:.1f}%")
                elif time_expired and not hit_tp and not hit_sl:
                    exit_reason = "TIME_LIMIT"
                    exit_price  = current_price
                    log.info(f"  ⏰ TIME_LIMIT {symbol}: {TIME_LIMIT_MIN}min expirado")
                else:
                    exit_reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS"
                    exit_price  = tp if hit_tp else pos["stop_loss"]

                _close_position(state, symbol, pos, exit_price, exit_reason)
                to_close.append(symbol)

                if exit_reason in ("STOP_LOSS", "EMERGENCY_EXIT"):
                    cooldown_until = (now + timedelta(minutes=5)).isoformat()
                    state.setdefault("cooldowns", {})[symbol] = cooldown_until

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

    # 0. Verificar cierres manuales solicitados desde dashboard
    manual_closes = state.get("manual_close", [])
    if manual_closes:
        log.info(f"🛑 Cierres manuales solicitados: {manual_closes}")
        for sym in list(manual_closes):
            if sym in state["positions"]:
                try:
                    ticker = client.futures_ticker(symbol=sym)
                    price = float(ticker["lastPrice"])
                    pos = state["positions"][sym]
                    direction = pos["direction"]
                    entry = pos["entry_price"]
                    pnl_pct = ((price-entry)/entry if direction=="LONG" else (entry-price)/entry) * pos.get("leverage",3)
                    pnl = round(pos["size_usdt"] * pnl_pct, 2)
                    trade = {**pos, "exit_price": price, "exit_time": datetime.now().isoformat(),
                             "exit_reason": "MANUAL", "pnl": pnl, "pnl_pct": round(pnl_pct*100,2), "status":"CLOSED"}
                    state["closed_trades"].append(trade)
                    state["capital"] += pos["size_usdt"] + pnl
                    state["total_pnl"] += pnl
                    del state["positions"][sym]
                    add_log(state, f"🛑 MANUAL CLOSE {sym} @ ${price:.4f} | P&L {'+' if pnl>=0 else ''}${pnl:.2f}")
                    log.info(f"  ✅ {sym} cerrado manualmente")
                    try:
                        _log_trade({**trade, "bot": "altcoins", "symbol": sym,
                                    "size": pos["size_usdt"], "leverage": LEVERAGE})
                    except Exception:
                        pass
                except Exception as e:
                    log.error(f"Error cerrando {sym}: {e}")
        state["manual_close"] = []
        save_state(state)

    # 1. Detectar escenario temprano para trailing stop
    from market_scenario import detect_scenario, is_with_trend
    scenario = detect_scenario(client)

    # 2. Verificar posiciones abiertas
    if state["positions"]:
        log.info(f"Verificando {len(state['positions'])} posiciones abiertas...")
        check_positions(client, state, scenario=scenario)

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
    altcoins = get_top_altcoins(client, n=TOP_N)
    if not altcoins:
        save_state(state)
        return

    # Filtrar las que ya tienen posición o están en cooldown
    now = datetime.now()
    cooldowns = state.get("cooldowns", {})
    # Limpiar cooldowns expirados (manejar timezones)
    def parse_dt(s):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except Exception:
            return datetime.min
    cooldowns = {s: t for s, t in cooldowns.items() if parse_dt(t) > now}  # limpia expirados
    state["cooldowns"] = cooldowns

    # Blacklist dinámica: símbolos con WR < 40% después de ≥8 trades
    sym_stats: dict[str, dict] = {}
    for t in state.get("closed_trades", []):
        s = t.get("symbol", "")
        if not s:
            continue
        st = sym_stats.setdefault(s, {"wins": 0, "total": 0})
        st["total"] += 1
        if t.get("pnl", 0) > 0:
            st["wins"] += 1
    dynamic_blacklist = {
        s for s, st in sym_stats.items()
        if st["total"] >= 8 and (st["wins"] / st["total"]) < 0.40
    }
    if dynamic_blacklist:
        log.info(f"  🚫 Blacklist dinámica ({len(dynamic_blacklist)}): {sorted(dynamic_blacklist)}")

    candidates = [
        a for a in altcoins
        if a["symbol"] not in state["positions"]
        and a["symbol"] not in cooldowns
        and a["symbol"] not in dynamic_blacklist
    ]
    if len(candidates) < len(altcoins):
        skipped = [s for s in cooldowns]
        log.info(f"  En cooldown (cierre manual): {skipped}")
    # Escenario ya detectado arriba (cached 5 min)
    log.info(f"  Escenario: {scenario['name']} | bias={scenario['direction']} | strategies={scenario.get('alt_strategies', [])}")
    state["btc_macro"] = scenario["direction"]  # compat dashboard

    log.info(f"\nAnalizando {min(len(candidates), slots*2)} candidatos (5m candles)...")
    state["scanning"] = True
    state["last_scan"] = []
    save_state(state)

    opportunities = []
    analyzed = 0

    for coin in candidates:
        if analyzed >= slots * 2:  # analizar hasta 2x los slots disponibles
            break

        symbol = coin["symbol"]
        indicators = get_indicators(client, symbol)
        if not indicators:
            continue

        analyzed += 1
        log.info(f"  {symbol}: EMA={indicators['ema_trend']} cross={'↑' if indicators['cross_bullish'] else '↓' if indicators['cross_bearish'] else '-'} | VWAP{indicators['price_vs_vwap']:+.2f}% | RSI={indicators['rsi']:.0f} | Vol={indicators['vol_ratio']:.1f}x")

        # Contar longs y shorts abiertos para pasar al prompt
        open_longs = sum(1 for p in state["positions"].values() if p.get("direction") == "LONG")
        open_shorts = sum(1 for p in state["positions"].values() if p.get("direction") == "SHORT")
        analysis = analyze_altcoin(indicators, coin, capital, open_count, open_longs, open_shorts)
        if not analysis:
            continue

        strategy = analysis.get("strategy", "SKIP")
        direction = analysis.get("direction", "SKIP")
        confidence = analysis.get("confidence", "LOW")

        # ── Filtro por escenario: estrategia + dirección ──
        allowed_strats = scenario.get("alt_strategies", ["RANGE", "MOMENTUM", "MEAN_REVERSION"])
        if strategy not in allowed_strats and strategy not in ("EMA_CROSS", "SKIP"):
            log.info(f"    🚫 {strategy} desactivada en {scenario['name']} (permitidas: {allowed_strats})")
            continue
        # Bloqueo de dirección contra-tendencia
        if scenario.get("alt_block_counter") and not is_with_trend(direction, scenario):
            log.info(f"    🚫 {direction} bloqueado — contra-tendencia en {scenario['name']}")
            continue

        # Aplicar multiplicadores del escenario
        analysis["take_profit_pct"] = analysis["take_profit_pct"] * scenario.get("alt_tp_mult", 1.0)
        analysis["position_size_usdt"] = analysis["position_size_usdt"] * scenario.get("alt_size_mult", 1.0)

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

        # Log de dirección vs tendencia (informativo)
        trend = indicators.get("trend", "neutral")
        if trend == "bullish" and direction == "SHORT":
            log.info(f"    → SHORT contra tendencia bullish (mean reversion)")
        elif trend == "bearish" and direction == "LONG":
            log.info(f"    → LONG contra tendencia bearish (mean reversion)")

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
        # Safety: recheck volume just before opening (avoid tokens that lost liquidity)
        try:
            fresh_ticker = client.futures_ticker(symbol=coin["symbol"])
            fresh_vol = float(fresh_ticker.get("quoteVolume", 0))
            if fresh_vol < MIN_VOLUME_USDT * 0.5:
                log.warning(f"  ⚠️  {coin['symbol']}: volumen caído a ${fresh_vol/1e6:.1f}M — SKIP")
                continue
        except Exception:
            pass
        open_position(client, state, coin["symbol"], analysis, indicators)
        open_count += 1
        executed += 1

    state["scanning"] = False
    log.info(f"\nCiclo completo: {executed} nuevas posiciones | Total abiertas: {open_count}")
    save_state(state)


def run_forever():
    log.info("🤖 Multi-Altcoin Bot — estilo scalping (5m)")
    log.info(f"   Modo: {'DRY RUN' if DRY_RUN else '⚠️  REAL'}")
    log.info(f"   Capital: ${TOTAL_CAPITAL} | Leverage: {LEVERAGE}x | Max posiciones: {MAX_POSITIONS}")
    log.info(f"   Candles: {CANDLE_INTERVAL} | SL: {DEFAULT_SL_PCT*100:.1f}% | TP: {DEFAULT_TP_PCT*100:.1f}%")
    log.info(f"   Top {TOP_N} altcoins (>${MIN_VOLUME_USDT/1e6:.0f}M vol) | Ciclo: {INTERVAL_MINUTES} min")

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
