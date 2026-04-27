"""
AltScalp Bot — High-Frequency Altcoin Scalper
==============================================
Ciclo: 15s | Muchas operaciones pequeñas | Leverage variable
Señales: volume burst + momentum + BB en altcoins top
TP: 0.2-0.4% | SL: 0.15-0.3% | Time limit: 90s
"""

import os, json, time, logging, fcntl
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from binance.client import Client

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ALTSC] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.FileHandler("altscalp.log")],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
DRY_RUN       = os.getenv("DRY_RUN", "true").lower() == "true"
CAPITAL       = float(os.getenv("ALTSCALP_CAPITAL", "200"))
CYCLE_SEC     = int(os.getenv("ALTSCALP_CYCLE", "15"))
MAX_POSITIONS = int(os.getenv("ALTSCALP_MAX_POS", "5"))
SIZE_PCT      = float(os.getenv("ALTSCALP_SIZE", "0.15"))    # 15% por posición
TP_PCT        = float(os.getenv("ALTSCALP_TP", "0.004"))     # 0.4% base (era 0.2%)
SL_PCT        = float(os.getenv("ALTSCALP_SL", "0.002"))     # 0.2% base (era 0.15%) → R:R 2:1
TIME_LIMIT_S  = int(os.getenv("ALTSCALP_TIME", "180"))       # 180s max por trade (era 90s)
MIN_SCORE     = int(os.getenv("ALTSCALP_SCORE", "5"))        # Score mínimo 5 (era 4)

# Leverage conservador — prioridad: no perder capital
HIGH_LIQ = {"ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT",
             "ADAUSDT","AVAXUSDT","DOTUSDT","LTCUSDT","LINKUSDT",
             "MATICUSDT","UNIUSDT","ATOMUSDT","NEARUSDT","APTUSDT"}
# HIGH_LIQ → 15x | top-30 → 10x | resto → 5x (antes: 50x/20x/10x)

MIN_VOLUME_USDT = 200_000_000   # $200M mínimo 24h (era $100M — más liquidez)
TOP_N           = 30
SCORE_THRESHOLD = MIN_SCORE

EXCLUDE = {
    "BUSDUSDT","USDCUSDT","TUSDUSDT","USDTUSDT","BTCUSDT","BTCDOMUSDT",
    "BLESSUSDT","RAVEUSDT","CLUSDT","ARIAUSDT","ENJUSDT","ZECUSDT",
    "ALPHAUSDT","XAUTUSDT","1000SHIBUSDT","1000LUNCUSDT","1000PEPEUSDT",
    "HYPEUSDT","XAGUSDT","XAUUSDT","PAXGUSDT","LUNA2USDT",
    "DEFIUSDT","BNXUSDT","SIRENUSDT","LOOMUSDT","CVPUSDT","BALUSDT",
    "币安人生USDT","ALPACAUSDT",
    # Baja liquidez real — spreads amplios, slippage alto
    "BASEDUSDT","PLAYUSDT","PIPPINUSDT","PORT3USDT","ORDIUSDT","BIOUSDT",
    "NEIROUSDT","RIVERUSDT","TAOUSDT","TRUMPUSDT","TSLAUSDT","LINKUSDT",
    "FILUSDT","DOGEUSDT",
}

# ── State ─────────────────────────────────────────────────────────────────────
STATE_DIR  = Path("./paper_trading")
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "altscalp_state.json"

def _fresh_state():
    return {
        "bot": "altscalp", "initial_capital": CAPITAL, "current_capital": CAPITAL,
        "peak_capital": CAPITAL, "positions": {}, "closed_trades": [],
        "total_pnl": 0.0, "total_pnl_pct": 0.0, "win_rate": 0.0,
        "max_drawdown": 0.0, "trades_today": 0, "cycle_log": [],
        "last_updated": "", "scanner_coins": [], "cooldowns": {}, "manual_close": []
    }

def load_state():
    if not STATE_FILE.exists():
        return _fresh_state()
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
        # Validar que es un estado real, no una respuesta de error del servidor
        if not data.get("bot") or "error" in data:
            log.warning("Estado inválido detectado — reiniciando estado altscalp")
            return _fresh_state()
        data.setdefault("positions", {})
        data.setdefault("cooldowns", {})
        data.setdefault("manual_close", [])
        if data.get("initial_capital", 0) == 0:
            data["initial_capital"] = CAPITAL
        return data
    except Exception as e:
        log.warning(f"Error cargando estado altscalp: {e} — reiniciando")
        return _fresh_state()

def save_state(state):
    """Guarda el estado usando truncate para no romper el descriptor de archivo."""
    try:
        state["last_updated"] = datetime.now().isoformat()
        with open(STATE_FILE, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                disk_data = json.load(f)
                # Sincronizar cierres manuales del dashboard
                for sym in disk_data.get("manual_close", []):
                    if sym not in state["manual_close"]:
                        state["manual_close"].append(sym)
            except: pass
            
            f.seek(0)
            f.truncate()
            json.dump(state, f, indent=2, default=str)
            f.flush()
            import os
            os.fsync(f.fileno())
    except Exception as e:
        log.error(f"Error guardando estado AltScalp: {e}")

def add_log(state, msg):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg}
    state["cycle_log"] = [entry] + state["cycle_log"][:49]

# ── Coin Selection ─────────────────────────────────────────────────────────────
_coin_cache = {"coins": [], "ts": 0}

def get_volatile_coins(client):
    """Altcoins con alto volumen Y alta variación reciente."""
    try:
        tickers = client.futures_ticker()
        coins = []
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT") or sym in EXCLUDE:
                continue
            vol  = float(t.get("quoteVolume", 0))
            chg  = abs(float(t.get("priceChangePercent", 0)))
            price = float(t.get("lastPrice", 0))
            if vol < MIN_VOLUME_USDT or price <= 0:
                continue
            # Score = volumen * variación (máximo momentum + liquidez)
            coins.append({
                "symbol": sym, "volume": vol, "change_pct": chg,
                "price": price, "score_vol": vol * chg,
            })
        coins.sort(key=lambda x: x["score_vol"], reverse=True)
        top = coins[:TOP_N]
        log.info(f"  Scanner: {len(top)} coins | top: {', '.join(c['symbol'] for c in top[:5])}")
        return top
    except Exception as e:
        log.error(f"Scanner error: {e}")
        return []

def get_leverage(symbol, rank):
    if symbol in HIGH_LIQ:
        return 15
    if rank < 15:
        return 10
    return 5

# ── Indicadores Rápidos ────────────────────────────────────────────────────────
def get_indicators(client, symbol):
    try:
        klines = client.futures_klines(symbol=symbol, interval="1m", limit=30)
        df = pd.DataFrame(klines, columns=[
            "ts","open","high","low","close","vol","ct","qv","trades","tbb","tbq","ig"])
        for c in ["open","high","low","close","vol"]:
            df[c] = df[c].astype(float)

        close = df["close"]
        vol   = df["vol"]
        high  = df["high"]
        low   = df["low"]
        price = float(close.iloc[-1])

        # Volume burst: candle actual vs promedio 20 periodos
        vol_avg  = float(vol.iloc[:-1].rolling(20).mean().iloc[-1])
        vol_curr = float(vol.iloc[-1])
        vol_ratio = round(vol_curr / vol_avg, 2) if vol_avg > 0 else 1.0

        # Momentum: cambio % en últimas 3 velas
        price_3m  = float(close.iloc[-4])
        velocity  = round((price - price_3m) / price_3m * 100, 4)

        # RSI 14
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = float((100 - 100 / (1 + gain / loss.replace(0, np.nan))).iloc[-1])

        # Bollinger Bands 20
        sma20    = close.rolling(20).mean()
        std20    = close.rolling(20).std()
        bb_upper = float((sma20 + 2 * std20).iloc[-1])
        bb_lower = float((sma20 - 2 * std20).iloc[-1])
        bb_pct   = (price - bb_lower) / (bb_upper - bb_lower) if bb_upper != bb_lower else 0.5

        # CVD aproximado (vol * dirección)
        direction_ser = (close - close.shift()).apply(lambda x: 1 if x > 0 else -1)
        cvd = float((vol * direction_ser).rolling(5).sum().iloc[-1])

        # ATR 14
        tr    = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr_pct = float(tr.rolling(14).mean().iloc[-1] / price * 100)

        # EMA trend
        ema9  = float(close.ewm(span=9,  adjust=False).mean().iloc[-1])
        ema21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])

        return {
            "price": price, "vol_ratio": vol_ratio,
            "velocity": velocity, "rsi": round(rsi, 1),
            "bb_pct": round(bb_pct, 3), "cvd_bull": cvd > 0,
            "atr_pct": round(atr_pct, 4),
            "ema_bull": ema9 > ema21,
        }
    except Exception as e:
        log.debug(f"Indicators {symbol}: {e}")
        return None

# ── Señal de Entrada ───────────────────────────────────────────────────────────
def analyze_entry(ind):
    """
    Score LONG/SHORT híbrido: Mean-Reversion + Momentum Breakout.
    Busca rebotes en extremos O seguir la fuerza en explosiones.
    """
    long_score = short_score = 0
    rsi = ind["rsi"]
    bb  = ind["bb_pct"]
    vr  = ind["vol_ratio"]
    vel = ind["velocity"]
    
    # --- MODO 1: MEAN REVERSION (Rebotes) ---
    if rsi < 25: long_score += 4
    elif rsi < 35: long_score += 2
    
    if rsi > 75: short_score += 4
    elif rsi > 65: short_score += 2
    
    if bb < 0.05: long_score += 3
    elif bb > 0.95: short_score += 3

    # --- MODO 2: MOMENTUM BREAKOUT (Máxima Ganancia) ---
    # Si el volumen es masivo (>3x) y el precio rompe con fuerza, seguimos el movimiento
    if vr > 3.0:
        if vel > 0.5: # Subida explosiva
            long_score += 5
            log.debug("  🚀 Momentum LONG detectado")
        elif vel < -0.5: # Caída explosiva
            short_score += 5
            log.debug("  📉 Momentum SHORT detectado")

    # Confirmación por EMA y CVD
    if ind["ema_bull"] and vel > 0: long_score += 1
    if not ind["ema_bull"] and vel < 0: short_score += 1
    
    if ind["cvd_bull"]: long_score += 1
    else: short_score += 1

    # Gate de seguridad para Mean Reversion (no atrapar cuchillos cayendo)
    # Solo aplica si la señal principal NO es de momentum explosivo
    if vr < 3.0:
        if long_score > short_score and vel < -0.3: return "FLAT", 0
        if short_score > long_score and vel > 0.3: return "FLAT", 0

    if long_score >= SCORE_THRESHOLD and long_score > short_score + 1:
        return "LONG", long_score
    if short_score >= SCORE_THRESHOLD and short_score > long_score + 1:
        return "SHORT", short_score
    
    return "FLAT", 0

# ── Gestión de Posiciones ──────────────────────────────────────────────────────
def open_position(state, symbol, direction, price, size_usdt, leverage, tp_pct, sl_pct, score):
    if direction == "LONG":
        tp = round(price * (1 + tp_pct), 8)
        sl = round(price * (1 - sl_pct), 8)
    else:
        tp = round(price * (1 - tp_pct), 8)
        sl = round(price * (1 + sl_pct), 8)

    state["positions"][symbol] = {
        "symbol": symbol, "direction": direction,
        "entry_price": price, "entry_time": datetime.now().isoformat(),
        "size_usdt": size_usdt, "leverage": leverage,
        "take_profit": tp, "stop_loss": sl,
        "best_price": price, "breakeven": False,
        "score": score,
    }
    state["current_capital"] -= size_usdt
    state["trades_today"] = state.get("trades_today", 0) + 1
    msg = f"↗ {direction} {symbol} | {leverage}x | TP {tp_pct*100:.2f}% SL {sl_pct*100:.2f}% | sc:{score}"
    add_log(state, msg)
    log.info(f"  📈 {direction} {symbol} @ ${price:.4f} | lev:{leverage}x size:${size_usdt:.0f} score:{score}")


def close_position(state, symbol, price, reason):
    pos = state["positions"].pop(symbol, None)
    if not pos:
        return

    # AÑADIR COOLDOWN: 10 min para no re-abrir inmediatamente
    state.setdefault("cooldowns", {})[symbol] = (datetime.now() + timedelta(minutes=10)).isoformat()

    entry     = pos["entry_price"]
    size      = pos["size_usdt"]
    lev       = pos["leverage"]
    direction = pos["direction"]

    raw = (price - entry) / entry if direction == "LONG" else (entry - price) / entry
    pnl     = round(raw * lev * size, 4)
    pnl_pct = round(raw * lev * 100, 2)

    state["current_capital"] += size + pnl
    state["total_pnl"] = round(state.get("total_pnl", 0) + pnl, 4)
    state["total_pnl_pct"] = round(
        (state["current_capital"] - state["initial_capital"]) / state["initial_capital"] * 100, 2)

    if state["current_capital"] > state.get("peak_capital", CAPITAL):
        state["peak_capital"] = state["current_capital"]
    dd = (state["peak_capital"] - state["current_capital"]) / state["peak_capital"] * 100
    state["max_drawdown"] = max(state.get("max_drawdown", 0), dd)

    trade = {
        "id": f"AS-{datetime.now().strftime('%H%M%S%f')[:10]}",
        "bot": "altscalp", "symbol": symbol,
        "side": direction, "entry_price": entry, "exit_price": price,
        "entry_time": pos["entry_time"], "exit_time": datetime.now().isoformat(),
        "size": size, "leverage": lev, "pnl": pnl, "pnl_pct": pnl_pct,
        "exit_reason": reason, "status": "CLOSED",
    }
    state.setdefault("closed_trades", []).append(trade)
    if len(state["closed_trades"]) > 300:
        state["closed_trades"] = state["closed_trades"][-300:]

    closed = state["closed_trades"]
    wins   = [t for t in closed if t["pnl"] > 0]
    state["win_rate"] = round(len(wins) / len(closed) * 100, 1) if closed else 0.0

    emoji = "✅" if pnl > 0 else "❌"
    add_log(state, f"{emoji} {reason} {symbol} {pnl:+.2f} ({pnl_pct:+.1f}%)")
    log.info(f"  {emoji} {reason} {symbol} exit ${price:.4f} PnL {pnl:+.4f} ({pnl_pct:+.1f}%)")


def monitor_positions(client, state):
    to_close = []
    now = datetime.now()

    for symbol, pos in list(state["positions"].items()):
        try:
            ticker = client.futures_symbol_ticker(symbol=symbol)
            price  = float(ticker["price"])
        except Exception as e:
            log.warning(f"  ⚠ {symbol} ticker error: {e}")
            continue

        direction = pos["direction"]
        entry     = pos["entry_price"]
        lev       = pos["leverage"]
        size      = pos["size_usdt"]
        
        # Calcular P&L flotante para el dashboard
        raw = (price - entry) / entry if direction == "LONG" else (entry - price) / entry
        pos["pnl_pct"] = round(raw * lev * 100, 2)
        pos["pnl"] = round(raw * lev * size, 2)
        pos["current_price"] = price

        tp        = pos["take_profit"]
        sl        = pos["stop_loss"]

        # Trailing: mover SL a breakeven cuando ganamos 0.2% (era 0.1%)
        if not pos.get("breakeven"):
            if direction == "LONG" and price >= entry * 1.002:
                pos["stop_loss"] = sl = entry * 1.0005  # breakeven + pequeño buffer
                pos["breakeven"] = True
            elif direction == "SHORT" and price <= entry * 0.998:
                pos["stop_loss"] = sl = entry * 0.9995
                pos["breakeven"] = True

        # Actualizar best price
        if direction == "LONG":
            pos["best_price"] = max(pos.get("best_price", entry), price)
        else:
            pos["best_price"] = min(pos.get("best_price", entry), price)

        hit_tp = (direction == "LONG" and price >= tp) or (direction == "SHORT" and price <= tp)
        hit_sl = (direction == "LONG" and price <= sl) or (direction == "SHORT" and price >= sl)

        entry_dt  = datetime.fromisoformat(pos["entry_time"])
        secs_open = (now - entry_dt).total_seconds()
        time_out  = secs_open >= TIME_LIMIT_S

        unrealized = ((price - entry) / entry if direction == "LONG" else (entry - price) / entry) * lev
        emergency  = unrealized < -0.08  # -8% levered (era -5%)

        reason = None
        exit_px = price
        if hit_tp:
            reason, exit_px = "TAKE_PROFIT", tp
        elif hit_sl:
            # Usar el precio actual (peor caso real), no el SL teórico
            reason, exit_px = "STOP_LOSS", min(price, sl) if direction == "LONG" else max(price, sl)
        elif emergency:
            reason, exit_px = "EMERGENCY", price
        elif time_out:
            reason, exit_px = "TIME_LIMIT", price

        if reason:
            log.info(f"  → {reason} {symbol} @ ${exit_px:.4f} (unreal: {unrealized*100:.1f}%)")
            to_close.append((symbol, exit_px, reason))

    for symbol, price, reason in to_close:
        close_position(state, symbol, price, reason)

# ── Ciclo Principal ────────────────────────────────────────────────────────────
def run_cycle(client):
    state = load_state()

    now_local = datetime.now()
    cap   = state.get("current_capital", CAPITAL)
    n_pos = len(state.get("positions", {}))
    log.info(f"── ALTSCALP {now_local.strftime('%H:%M:%S')} | ${cap:.1f} | {n_pos}/{MAX_POSITIONS} pos ──")

    # 0. Cierres manuales (del dashboard)
    for sym in list(state.get("manual_close", [])):
        if sym in state["positions"]:
            log.info(f"  🖐 Cierre manual: {sym}")
            try:
                price = float(client.futures_symbol_ticker(symbol=sym)["price"])
            except Exception:
                price = state["positions"][sym].get("entry_price", 0)
            close_position(state, sym, price, "MANUAL")
    state["manual_close"] = []

    # 1. Monitorear SL/TP/emergency de posiciones abiertas
    if state["positions"]:
        monitor_positions(client, state)

    # 2. Refresh scanner cada 5 min
    global _coin_cache
    if time.time() - _coin_cache["ts"] > 300:
        _coin_cache["coins"] = get_volatile_coins(client)
        _coin_cache["ts"]    = time.time()
        state["scanner_coins"] = [
            {"symbol": c["symbol"], "volume": round(c["volume"] / 1e6, 0),
             "change_pct": round(c["change_pct"], 2)}
            for c in _coin_cache["coins"][:15]
        ]

    # 3. Entradas si hay slots
    slots = MAX_POSITIONS - len(state["positions"])
    
    # Limpiar cooldowns viejos
    now = datetime.now()
    state["cooldowns"] = {s: t for s, t in state.get("cooldowns", {}).items() 
                          if datetime.fromisoformat(t) > now}

    if slots > 0 and _coin_cache["coins"]:
        analyzed = 0
        entries  = []
        for rank, coin in enumerate(_coin_cache["coins"]):
            if analyzed >= slots * 5:
                break
            sym = coin["symbol"]
            
            # FILTRO: No estar en posición NI en cooldown
            if sym in state["positions"] or sym in state["cooldowns"] or coin["change_pct"] < 0.5:
                continue
            ind = get_indicators(client, sym)
            if not ind or ind["vol_ratio"] < 1.5:  # era 1.3 — señales más limpias
                continue
            analyzed += 1
            direction, score = analyze_entry(ind)
            if direction != "FLAT":
                entries.append((score, rank, direction, coin, ind))

        entries.sort(key=lambda x: x[0], reverse=True)
        cap = state["current_capital"]   # actualizado tras cierres
        open_pos_value = sum(p.get("size_usdt", 0) for p in state["positions"].values())
        effective_cap = cap + open_pos_value  # capital total (cash + posiciones abiertas)
        if not entries:
            log.debug(f"  Sin entradas (analizados:{analyzed} slots:{slots})")

        for score, rank, direction, coin, ind in entries[:slots]:
            sym  = coin["symbol"]
            size = round(effective_cap * SIZE_PCT, 2)  # tamaño fijo del capital total
            if size < 5 or cap < size:
                log.warning(f"  ⚠ Capital insuficiente para entrada: ${cap:.1f} cash < ${size:.1f} (15%×${effective_cap:.0f})")
                continue
            lev  = get_leverage(sym, rank)
            atr  = ind["atr_pct"] / 100
            # SL dinámico: 1.5x ATR, mín 0.3%, máx 0.6% — da espacio real al trade
            sl   = round(max(0.003, min(atr * 1.5, 0.006)), 5)
            # TP: R:R 2.5:1 sobre el SL real
            tp   = round(sl * 2.5, 5)
            open_position(state, sym, direction, ind["price"], size, lev, tp, sl, score)
            log.info(f"    vol:{ind['vol_ratio']}x vel:{ind['velocity']:+.2f}% RSI:{ind['rsi']} BB:{ind['bb_pct']:.2f}")

    save_state(state)


def main():
    client = Client(
        os.getenv("BINANCE_API_KEY"),
        os.getenv("BINANCE_SECRET_KEY"),
    )

    init = load_state()
    log.info(f"🚀 AltScalp Bot | Capital: ${init['current_capital']:.2f} | Ciclo: {CYCLE_SEC}s | Max pos: {MAX_POSITIONS}")
    log.info(f"   TP: {TP_PCT*100:.2f}% | SL: {SL_PCT*100:.2f}% | Time: {TIME_LIMIT_S}s | Score≥{SCORE_THRESHOLD}")

    while True:
        try:
            run_cycle(client)
        except Exception as e:
            log.error(f"Error ciclo: {e}", exc_info=True)
        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    main()
