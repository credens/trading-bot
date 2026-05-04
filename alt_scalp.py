"""
Altcoin Scalping Bot
====================
Strategy : VWAP + EMA(9/21) + RSI + Order Book Imbalance
Capital  : $200  |  Size: 25% ($50)  |  Leverage: 2x
TP       : 0.35%  |  SL: 0.20%  |  Timeout: 90s
Universe : Top liquid alts + spread filter
Hours    : 09:00-15:00 Argentina (UTC-3)
"""
import os, time, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from binance.client import Client

from paper_trading import get_alt_engine, ALT_STATE
from signals import evaluate, spread_pct as get_spread
from drawdown_monitor import check_drawdown, is_paused

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ALT] %(message)s",
    handlers=[logging.FileHandler("alt_scalp.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
CAPITAL       = float(os.getenv("ALT_CAPITAL", "200"))
SIZE_PCT      = 0.25
LEVERAGE      = 2
TP_PCT        = 0.0035       # 0.35%
SL_PCT        = 0.0020       # 0.20%
TIMEOUT_SECS  = 90
CYCLE_SECS    = 30
MAX_POSITIONS = 2
COOLDOWN_SECS    = 300
MAX_DAILY_LOSS   = 0.02
MIN_VOLUME_USDT  = 50_000_000   # 50M 24h volume
MAX_SPREAD_PCT   = 0.08

# Core liquid universe — extended by dynamic scan
CORE_SYMBOLS = [
    "ETHUSDT","SOLUSDT","BNBUSDT","AVAXUSDT","DOGEUSDT",
    "LINKUSDT","MATICUSDT","ARBUSDT","OPUSDT","APTUSDT",
    "LTCUSDT","XRPUSDT","ADAUSDT","DOTUSDT","NEARUSDT",
]

SIG_CFG = dict(
    max_spread_pct=MAX_SPREAD_PCT,
    min_atr_pct=0.10,
    max_spike_mult=2.5,
    rsi_long_lo=52, rsi_long_hi=68,
    rsi_short_lo=32, rsi_short_hi=48,
    ob_threshold=1.3,
)

_cooldown: dict = {}   # symbol → datetime
_daily_loss = 0.0
_last_day   = None


def _trading_hours():
    h = (datetime.now(timezone.utc) + timedelta(hours=-3)).hour
    return 9 <= h < 15


def _reset_daily():
    global _daily_loss, _last_day
    today = datetime.now().date()
    if _last_day != today:
        _daily_loss = 0.0
        _last_day = today


def get_universe(client):
    """Return symbols passing volume and spread filters."""
    try:
        tickers = client.futures_ticker()
    except Exception:
        return CORE_SYMBOLS[:5]

    liquid = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        if float(t.get("quoteVolume", 0)) < MIN_VOLUME_USDT:
            continue
        liquid.append(sym)

    # Merge core + high-volume, deduplicated, limit 20
    universe = list(dict.fromkeys(CORE_SYMBOLS + liquid))[:20]
    return universe


def monitor_open(client, paper):
    """Check timeouts and SL/TP on all open alt positions."""
    global _daily_loss
    closed_syms = []
    for trade in list(paper.state.open_trades):
        if trade.bot != "alt_scalp":
            continue
        sym = trade.symbol
        try:
            price = float(client.futures_symbol_ticker(symbol=sym)["price"])
        except Exception:
            continue

        entry_dt = datetime.fromisoformat(trade.entry_time)
        age = (datetime.now() - entry_dt.replace(tzinfo=None)).total_seconds()

        if age >= TIMEOUT_SECS:
            pnl = paper.close_by_symbol(sym, price, "TIMEOUT", bot="alt_scalp")
            _daily_loss += pnl
            log.info(f"  ⏰ TIMEOUT {sym} @ ${price:.4f}  PnL {pnl:+.2f}")
            closed_syms.append(sym)
            continue

        closed = paper.check_stops({sym: price}, bot="alt_scalp")
        for t in closed:
            _daily_loss += t.pnl or 0
            emoji = "✅" if (t.pnl or 0) >= 0 else "❌"
            log.info(f"  {emoji} {t.exit_reason} {sym} @ ${price:.4f}  PnL {t.pnl:+.2f}")
            if t.exit_reason == "STOP_LOSS":
                _cooldown[sym] = datetime.now() + timedelta(seconds=COOLDOWN_SECS)
            closed_syms.append(sym)

    return closed_syms


def run_cycle(client, paper):
    global _daily_loss

    open_alts = [t for t in paper.state.open_trades if t.bot == "alt_scalp"]
    n_open = len(open_alts)

    log.info(f"── ALT {datetime.now().strftime('%H:%M:%S')} | "
             f"${paper.state.current_capital:.2f} | {n_open}/{MAX_POSITIONS} pos ──")

    if not _trading_hours():
        return

    if is_paused(ALT_STATE):
        log.warning("⛔ PAUSADO por drawdown")
        return

    _reset_daily()
    if _daily_loss <= -(CAPITAL * MAX_DAILY_LOSS):
        log.warning(f"🛑 Daily loss limit ({_daily_loss:.2f})")
        return

    # Monitor existing positions
    monitor_open(client, paper)
    open_alts = [t for t in paper.state.open_trades if t.bot == "alt_scalp"]
    n_open = len(open_alts)

    if n_open >= MAX_POSITIONS:
        return

    # Scan for new setups
    check_drawdown("alt_scalp", paper.state.current_capital, CAPITAL,
                   paper.state.peak_capital, ALT_STATE)

    universe = get_universe(client)
    open_syms = {t.symbol for t in open_alts}
    now = datetime.now()

    for sym in universe:
        if sym in open_syms:
            continue
        if _cooldown.get(sym) and now < _cooldown[sym]:
            continue
        if n_open >= MAX_POSITIONS:
            break

        try:
            action, reason, ctx = evaluate(client, sym, SIG_CFG)
        except Exception as e:
            log.debug(f"  {sym} eval error: {e}")
            continue

        if action not in ("LONG", "SHORT"):
            continue

        size = round(CAPITAL * SIZE_PCT, 2)
        if size > paper.state.current_capital:
            log.warning("  Insufficient capital")
            break

        paper.open_trade("alt_scalp", sym, action, size, LEVERAGE,
                         ctx["price"], SL_PCT, TP_PCT)
        log.info(f"  📈 {action} {sym} @ ${ctx['price']:.4f} | "
                 f"RSI:{ctx['rsi']:.0f} OB:{ctx['ob']:.2f} | {reason}")
        n_open += 1
        open_syms.add(sym)

    paper.save()


if __name__ == "__main__":
    log.info(f"🚀 Alt Scalp | ${CAPITAL} | {SIZE_PCT*100:.0f}% size | {LEVERAGE}x lev | "
             f"TP:{TP_PCT*100:.2f}% SL:{SL_PCT*100:.2f}% | max {MAX_POSITIONS} pos")
    client = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))
    paper  = get_alt_engine(CAPITAL)
    while True:
        try:
            run_cycle(client, paper)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
        time.sleep(CYCLE_SECS)
