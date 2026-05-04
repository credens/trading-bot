"""
BTC Scalping Bot
================
Strategy : VWAP + EMA(9/21) + RSI + Order Book Imbalance
Capital  : $200  |  Size: 25% ($50)  |  Leverage: 3x
TP       : 0.20%  |  SL: 0.12%  |  Timeout: 120s
Hours    : 09:00-15:00 Argentina (UTC-3)
"""
import os, time, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from binance.client import Client

from paper_trading import get_btc_engine, BTC_STATE
from signals import evaluate
from drawdown_monitor import check_drawdown, is_paused

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BTC] %(message)s",
    handlers=[logging.FileHandler("btc_scalp.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
SYMBOL       = "BTCUSDT"
CAPITAL      = float(os.getenv("BTC_CAPITAL", "200"))
SIZE_PCT     = 0.25          # 25% of capital
LEVERAGE     = 3
TP_PCT       = 0.0020        # 0.20%
SL_PCT       = 0.0012        # 0.12%
TIMEOUT_SECS = 120
CYCLE_SECS   = 30
COOLDOWN_SECS   = 300        # 5 min after SL
MAX_DAILY_LOSS  = 0.02       # 2% of capital

SIG_CFG = dict(
    max_spread_pct=0.05,
    min_atr_pct=0.08,
    max_spike_mult=2.5,
    rsi_long_lo=52, rsi_long_hi=68,
    rsi_short_lo=32, rsi_short_hi=48,
    ob_threshold=1.3,
)

# ── Runtime state ───────────────────────────────────────────────────────────────
_cooldown_until = None
_daily_loss     = 0.0
_last_day       = None


def _trading_hours():
    h = (datetime.now(timezone.utc) + timedelta(hours=-3)).hour
    return 9 <= h < 15


def _reset_daily():
    global _daily_loss, _last_day
    today = datetime.now().date()
    if _last_day != today:
        _daily_loss = 0.0
        _last_day = today


def run_cycle(client, paper):
    global _cooldown_until, _daily_loss

    log.info(f"── BTC {datetime.now().strftime('%H:%M:%S')} | ${paper.state.current_capital:.2f} ──")

    if not _trading_hours():
        return

    if is_paused(BTC_STATE):
        log.warning("⛔ PAUSADO por drawdown")
        return

    _reset_daily()
    if _daily_loss <= -(CAPITAL * MAX_DAILY_LOSS):
        log.warning(f"🛑 Daily loss limit ({_daily_loss:.2f})")
        return

    if _cooldown_until and datetime.now() < _cooldown_until:
        secs = int((_cooldown_until - datetime.now()).total_seconds())
        log.info(f"  ⏳ Cooldown {secs}s")
        return
    _cooldown_until = None

    # ── Monitor open position ────────────────────────────────────────────────────
    open_trades = [t for t in paper.state.open_trades if t.bot == "btc_scalp"]
    if open_trades:
        trade = open_trades[0]
        try:
            price = float(client.futures_symbol_ticker(symbol=SYMBOL)["price"])
        except Exception as e:
            log.warning(f"  price fetch error: {e}")
            return

        # Timeout
        entry_dt = datetime.fromisoformat(trade.entry_time)
        age = (datetime.now() - entry_dt.replace(tzinfo=None)).total_seconds()
        if age >= TIMEOUT_SECS:
            pnl = paper.close_by_symbol(SYMBOL, price, "TIMEOUT", bot="btc_scalp")
            _daily_loss += pnl
            log.info(f"  ⏰ TIMEOUT @ ${price:.2f}  PnL {pnl:+.2f}")
            return

        # SL / TP
        closed = paper.check_stops({SYMBOL: price}, bot="btc_scalp")
        for t in closed:
            _daily_loss += t.pnl or 0
            emoji = "✅" if (t.pnl or 0) >= 0 else "❌"
            log.info(f"  {emoji} {t.exit_reason} @ ${price:.2f}  PnL {t.pnl:+.2f}")
            if t.exit_reason == "STOP_LOSS":
                _cooldown_until = datetime.now() + timedelta(seconds=COOLDOWN_SECS)
        return

    # ── Evaluate signal ──────────────────────────────────────────────────────────
    try:
        action, reason, ctx = evaluate(client, SYMBOL, SIG_CFG)
    except Exception as e:
        log.warning(f"  signal error: {e}")
        return

    log.info(f"  ${ctx['price']:.2f} VWAP:{ctx['vwap']:.2f} RSI:{ctx['rsi']:.1f} "
             f"OB:{ctx['ob']:.2f} ATR:{ctx['atr_pct']:.3f}% Spr:{ctx['spread_pct']:.3f}%")
    log.info(f"  → {action} | {reason}")

    if action not in ("LONG", "SHORT"):
        paper.save()
        return

    # ── Open position ────────────────────────────────────────────────────────────
    check_drawdown("btc_scalp", paper.state.current_capital, CAPITAL,
                   paper.state.peak_capital, BTC_STATE)
    size = round(CAPITAL * SIZE_PCT, 2)
    paper.open_trade("btc_scalp", SYMBOL, action, size, LEVERAGE,
                     ctx["price"], SL_PCT, TP_PCT)
    paper.save()


if __name__ == "__main__":
    log.info(f"🚀 BTC Scalp | ${CAPITAL} | {SIZE_PCT*100:.0f}% size | {LEVERAGE}x lev | "
             f"TP:{TP_PCT*100:.2f}% SL:{SL_PCT*100:.2f}%")
    client = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"))
    paper  = get_btc_engine(CAPITAL)
    while True:
        try:
            run_cycle(client, paper)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
        time.sleep(CYCLE_SECS)
