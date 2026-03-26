"""
Trade Logger
============
Log persistente de todos los trades cerrados de los 3 bots.
Escribe en trades.jsonl (una línea JSON por trade) y
genera stats.json con estadísticas agregadas.

Uso:
  from trade_logger import log_trade, generate_stats
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

LOG_FILE   = Path(__file__).parent / "trades.jsonl"
STATS_FILE = Path(__file__).parent / "stats.json"


# ─── Core ─────────────────────────────────────────────────────────────────────

def log_trade(trade: dict):
    """
    Registra un trade cerrado en trades.jsonl.
    trade debe tener al menos: bot, side, entry_price, exit_price,
    entry_time, exit_time, exit_reason, pnl, pnl_pct, size, leverage.
    """
    record = {
        "id":           trade.get("id", ""),
        "bot":          trade.get("bot", ""),
        "symbol":       trade.get("symbol", "BTCUSDT"),
        "side":         trade.get("side", ""),
        "leverage":     trade.get("leverage", 1),
        "size_usdt":    trade.get("size", trade.get("size_usdt", 0)),
        "entry_price":  trade.get("entry_price", 0),
        "exit_price":   trade.get("exit_price", 0),
        "stop_loss":    trade.get("stop_loss", None),
        "take_profit":  trade.get("take_profit", None),
        "entry_time":   trade.get("entry_time", ""),
        "exit_time":    trade.get("exit_time", datetime.now().isoformat()),
        "exit_reason":  trade.get("exit_reason", ""),
        "pnl":          round(float(trade.get("pnl", 0)), 4),
        "pnl_pct":      round(float(trade.get("pnl_pct", 0)), 4),
        "duration_min": _duration_min(trade.get("entry_time"), trade.get("exit_time")),
        "reasoning":    trade.get("reasoning", ""),
        "confidence":   trade.get("confidence", ""),
        "logged_at":    datetime.now().isoformat(),
    }

    with LOG_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")

    _update_stats()


def generate_stats() -> dict:
    """Lee trades.jsonl y devuelve estadísticas completas."""
    trades = _load_trades()
    if not trades:
        return {"total_trades": 0, "message": "Sin trades aún"}

    stats = {
        "generated_at": datetime.now().isoformat(),
        "total_trades": len(trades),
        "overall": _calc_stats(trades),
        "by_bot": {},
        "by_side": {},
        "by_exit_reason": {},
        "best_trade": None,
        "worst_trade": None,
        "recent_10": [],
    }

    # Por bot
    bots = {t["bot"] for t in trades}
    for bot in sorted(bots):
        bot_trades = [t for t in trades if t["bot"] == bot]
        stats["by_bot"][bot] = _calc_stats(bot_trades)

    # Por lado
    for side in ["LONG", "SHORT"]:
        side_trades = [t for t in trades if t["side"] == side]
        if side_trades:
            stats["by_side"][side] = _calc_stats(side_trades)

    # Por razón de salida
    reasons = {t["exit_reason"] for t in trades}
    for reason in sorted(reasons):
        r_trades = [t for t in trades if t["exit_reason"] == reason]
        stats["by_exit_reason"][reason] = {
            "count": len(r_trades),
            "total_pnl": round(sum(t["pnl"] for t in r_trades), 2),
        }

    # Mejor y peor trade
    by_pnl = sorted(trades, key=lambda t: t["pnl"])
    stats["worst_trade"] = _trade_summary(by_pnl[0])
    stats["best_trade"]  = _trade_summary(by_pnl[-1])

    # Últimos 10
    stats["recent_10"] = [_trade_summary(t) for t in trades[-10:]]

    return stats


# ─── Internal ─────────────────────────────────────────────────────────────────

def _load_trades() -> list:
    if not LOG_FILE.exists():
        return []
    trades = []
    with LOG_FILE.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return trades


def _calc_stats(trades: list) -> dict:
    if not trades:
        return {}

    pnls       = [t["pnl"] for t in trades]
    wins       = [p for p in pnls if p > 0]
    losses     = [p for p in pnls if p <= 0]
    durations  = [t["duration_min"] for t in trades if t.get("duration_min") is not None]

    total_pnl      = round(sum(pnls), 2)
    avg_win        = round(sum(wins) / len(wins), 2)    if wins   else 0
    avg_loss       = round(sum(losses) / len(losses), 2) if losses else 0
    profit_factor  = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None
    expectancy     = round(total_pnl / len(trades), 2)
    win_rate       = round(len(wins) / len(trades) * 100, 1)
    avg_pnl_pct    = round(sum(t["pnl_pct"] for t in trades) / len(trades), 2)
    avg_duration   = round(sum(durations) / len(durations), 1) if durations else None

    # Drawdown máximo sobre serie de P&L acumulado
    equity = 0
    peak   = 0
    max_dd = 0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 2)

    return {
        "count":          len(trades),
        "win_rate_pct":   win_rate,
        "total_pnl":      total_pnl,
        "avg_pnl_per_trade": expectancy,
        "avg_pnl_pct":    avg_pnl_pct,
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
        "profit_factor":  profit_factor,
        "max_drawdown":   max_dd,
        "avg_duration_min": avg_duration,
        "wins":           len(wins),
        "losses":         len(losses),
    }


def _trade_summary(t: dict) -> dict:
    return {
        "id":          t.get("id"),
        "bot":         t.get("bot"),
        "symbol":      t.get("symbol"),
        "side":        t.get("side"),
        "exit_reason": t.get("exit_reason"),
        "pnl":         t.get("pnl"),
        "pnl_pct":     t.get("pnl_pct"),
        "duration_min":t.get("duration_min"),
        "entry_time":  t.get("entry_time"),
    }


def _duration_min(entry_time: Optional[str], exit_time: Optional[str]) -> Optional[float]:
    try:
        entry = datetime.fromisoformat(entry_time)
        exit_ = datetime.fromisoformat(exit_time) if exit_time else datetime.now()
        return round((exit_ - entry).total_seconds() / 60, 1)
    except Exception:
        return None


def _update_stats():
    try:
        stats = generate_stats()
        STATS_FILE.write_text(json.dumps(stats, indent=2))
    except Exception:
        pass


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--reset" in sys.argv:
        if LOG_FILE.exists():
            LOG_FILE.unlink()
        if STATS_FILE.exists():
            STATS_FILE.unlink()
        print("Log reseteado.")
        sys.exit(0)

    stats = generate_stats()
    if stats.get("total_trades", 0) == 0:
        print("Sin trades registrados aún.")
        sys.exit(0)

    ov = stats["overall"]
    print(f"\n{'='*55}")
    print(f"  TRADING STATS — {stats['generated_at'][:16]}")
    print(f"{'='*55}")
    print(f"  Total trades : {stats['total_trades']}")
    print(f"  Win rate     : {ov['win_rate_pct']}%  ({ov['wins']}W / {ov['losses']}L)")
    print(f"  Total P&L    : ${ov['total_pnl']:+.2f}")
    print(f"  Avg/trade    : ${ov['avg_pnl_per_trade']:+.2f}  ({ov['avg_pnl_pct']:+.2f}%)")
    print(f"  Avg win      : ${ov['avg_win']:+.2f}")
    print(f"  Avg loss     : ${ov['avg_loss']:+.2f}")
    print(f"  Profit factor: {ov['profit_factor']}")
    print(f"  Max drawdown : ${ov['max_drawdown']:.2f}")
    print(f"  Avg duration : {ov['avg_duration_min']} min")

    print(f"\n  {'─'*20} Por bot {'─'*20}")
    for bot, s in stats["by_bot"].items():
        print(f"  {bot:12} | {s['count']:3} trades | WR {s['win_rate_pct']:5.1f}% | P&L ${s['total_pnl']:+.2f} | PF {s['profit_factor']}")

    print(f"\n  {'─'*20} Por lado {'─'*19}")
    for side, s in stats.get("by_side", {}).items():
        print(f"  {side:6} | {s['count']:3} trades | WR {s['win_rate_pct']:5.1f}% | P&L ${s['total_pnl']:+.2f}")

    print(f"\n  {'─'*18} Por razón de salida {'─'*15}")
    for reason, s in stats["by_exit_reason"].items():
        print(f"  {reason:20} | {s['count']:3} trades | P&L ${s['total_pnl']:+.2f}")

    if stats["best_trade"]:
        bt = stats["best_trade"]
        print(f"\n  ✅ Mejor trade : {bt['symbol']} {bt['side']} | ${bt['pnl']:+.2f} ({bt['pnl_pct']:+.2f}%) | {bt['duration_min']} min")
    if stats["worst_trade"]:
        wt = stats["worst_trade"]
        print(f"  ❌ Peor trade  : {wt['symbol']} {wt['side']} | ${wt['pnl']:+.2f} ({wt['pnl_pct']:+.2f}%) | {wt['duration_min']} min")

    print(f"\n  {'─'*20} Últimos 10 {'─'*21}")
    for t in reversed(stats["recent_10"]):
        emoji = "✅" if (t["pnl"] or 0) > 0 else "❌"
        print(f"  {emoji} {t['bot']:10} {t['symbol']:12} {t['side']:5} | {t['exit_reason']:20} | ${t['pnl']:+.2f} ({t['pnl_pct']:+.2f}%)")

    print(f"{'='*55}\n")
