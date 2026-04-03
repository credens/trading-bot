"""
Paper Trading Engine
====================
Registra operaciones simuladas con datos reales de mercado.
Calcula P&L, win rate, drawdown en tiempo real.
Comparte estado con el dashboard via JSON.
"""

import json
import time
import logging
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger(__name__)

STATE_DIR = Path("./paper_trading")
STATE_DIR.mkdir(exist_ok=True)

BINANCE_STATE = STATE_DIR / "binance_state.json"


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    id: str
    bot: str                    # "binance" | "altcoin"
    side: str                   # "LONG" | "SHORT"
    entry_price: float
    entry_time: str
    size: float                 # USDT
    stop_loss: float
    take_profit: float
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None  # "STOP_LOSS" | "TAKE_PROFIT" | "SIGNAL" | "MANUAL"
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    status: str = "OPEN"        # "OPEN" | "CLOSED"
    reasoning: str = ""
    confidence: str = ""
    leverage: int = 1
    best_price: Optional[float] = None


@dataclass
class BotState:
    bot: str
    initial_capital: float
    current_capital: float
    open_trades: list = field(default_factory=list)
    closed_trades: list = field(default_factory=list)
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    win_rate: float = 0.0
    max_drawdown: float = 0.0
    peak_capital: float = 0.0
    trades_today: int = 0
    last_updated: str = ""
    cycle_log: list = field(default_factory=list)
    # Binance extra
    btc_price: float = 0.0
    rsi: float = 50.0
    trend: str = "neutral"
    macd_cross: str = "neutral"
    funding_rate: float = 0.0
    vol_ratio: float = 1.0


# ─── Paper Trading Engine ──────────────────────────────────────────────────────

class PaperTradingEngine:
    def __init__(self, bot: str, initial_capital: float, state_file: Path):
        self.bot = bot
        self.state_file = state_file
        self.state = self._load_or_create(initial_capital)

    def _load_or_create(self, initial_capital: float) -> BotState:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                state = BotState(**{k: v for k, v in data.items() if k in BotState.__dataclass_fields__})
                # Reconstruir trades
                state.open_trades = [Trade(**t) for t in data.get("open_trades", [])]
                state.closed_trades = [Trade(**t) for t in data.get("closed_trades", [])[-50:]]  # últimos 50
                log.info(f"[{self.bot}] Estado cargado: ${state.current_capital:.2f} | {len(state.open_trades)} trades abiertos")
                return state
            except Exception as e:
                log.warning(f"Error cargando estado: {e} — creando nuevo")

        state = BotState(
            bot=self.bot,
            initial_capital=initial_capital,
            current_capital=initial_capital,
            peak_capital=initial_capital,
        )
        return state

    def save(self):
        # Respetar cierres manuales: si el disco tiene más trades cerrados o el flag manual_close,
        # filtrar open_trades para no sobrescribir trades ya cerrados manualmente.
        try:
            if self.state_file.exists():
                disk = json.loads(self.state_file.read_text())
                disk_closed = disk.get("closed_trades", [])
                mem_closed = self.state.closed_trades
                if len(disk_closed) > len(mem_closed):
                    closed_ids = {t.get("id") for t in disk_closed if t.get("id")}
                    self.state.open_trades = [t for t in self.state.open_trades if t.id not in closed_ids]
                    # Reconstruir closed_trades desde disco
                    self.state.closed_trades = [
                        Trade(**{k: v for k, v in t.items() if k in Trade.__dataclass_fields__})
                        for t in disk_closed
                    ]
                if disk.get("manual_close"):
                    # Limpiar el flag en el objeto que vamos a guardar
                    disk["manual_close"] = False
        except Exception:
            pass

        data = asdict(self.state)
        self.state_file.write_text(json.dumps(data, indent=2, default=str))

    def add_log(self, msg: str):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg}
        self.state.cycle_log = [entry] + self.state.cycle_log[:49]  # últimos 50 logs
        self.state.last_updated = datetime.now().isoformat()

    # ─── Binance Operations ───────────────────────────────────────────────────

    def open_binance_trade(self, decision: dict, current_price: float, capital: float, leverage: int) -> Optional[Trade]:
        """Abre trade simulado de Binance."""
        side = decision["decision"]
        pos_pct = min(float(decision.get("position_size_pct", 0.05)), 0.10)
        sl_pct = float(decision.get("stop_loss_pct", 0.015))
        tp_pct = float(decision.get("take_profit_pct", 0.03))
        size = round(capital * pos_pct, 2)

        if side == "LONG":
            sl = round(current_price * (1 - sl_pct), 2)
            tp = round(current_price * (1 + tp_pct), 2)
        else:
            sl = round(current_price * (1 + sl_pct), 2)
            tp = round(current_price * (1 - tp_pct), 2)

        trade = Trade(
            id=f"bn_{int(time.time())}",
            bot="binance",
            side=side,
            entry_price=current_price,
            entry_time=datetime.now().isoformat(),
            size=size,
            stop_loss=sl,
            take_profit=tp,
            reasoning=decision.get("reasoning", ""),
            confidence=decision.get("confidence", ""),
            leverage=leverage,
        )

        self.state.open_trades.append(trade)
        self.state.current_capital -= size  # reservar capital
        self.state.trades_today += 1
        self.save()

        msg = f"✓ PAPER {side} | entrada ${current_price:,.0f} | SL ${sl:,.0f} | TP ${tp:,.0f} | size ${size:.0f}"
        self.add_log(msg)
        log.info(f"  [PAPER] {msg}")
        return trade

    def check_binance_stops(self, current_price: float):
        """Verifica si algún trade abierto tocó SL o TP."""
        closed = []
        for trade in self.state.open_trades:
            if trade.bot != "binance":
                continue

            hit_sl = hit_tp = False
            if trade.side == "LONG":
                hit_sl = current_price <= trade.stop_loss
                hit_tp = current_price >= trade.take_profit
            else:
                hit_sl = current_price >= trade.stop_loss
                hit_tp = current_price <= trade.take_profit

            if hit_sl or hit_tp:
                exit_price = trade.stop_loss if hit_sl else trade.take_profit
                reason = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"
                self._close_binance_trade(trade, exit_price, reason)
                closed.append(trade)

        for t in closed:
            self.state.open_trades.remove(t)

        if closed:
            self.save()

    def _close_binance_trade(self, trade: Trade, exit_price: float, reason: str):
        """Cierra un trade y calcula P&L."""
        if trade.side == "LONG":
            raw_pnl = (exit_price - trade.entry_price) / trade.entry_price
        else:
            raw_pnl = (trade.entry_price - exit_price) / trade.entry_price

        pnl = round(raw_pnl * trade.size * trade.leverage, 2)
        pnl_pct = round(raw_pnl * trade.leverage * 100, 2)

        trade.exit_price = exit_price
        trade.exit_time = datetime.now().isoformat()
        trade.exit_reason = reason
        trade.pnl = pnl
        trade.pnl_pct = pnl_pct
        trade.status = "CLOSED"

        # Devolver capital + P&L
        self.state.current_capital += trade.size + pnl
        self.state.total_pnl += pnl
        self.state.total_pnl_pct = round((self.state.current_capital - self.state.initial_capital) / self.state.initial_capital * 100, 2)
        self.state.closed_trades.append(trade)

        # Actualizar peak y drawdown
        if self.state.current_capital > self.state.peak_capital:
            self.state.peak_capital = self.state.current_capital
        drawdown = (self.state.peak_capital - self.state.current_capital) / self.state.peak_capital * 100
        self.state.max_drawdown = max(self.state.max_drawdown, drawdown)

        # Win rate
        closed = [t for t in self.state.closed_trades if t.pnl is not None]
        wins = [t for t in closed if t.pnl > 0]
        self.state.win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0

        emoji = "✅" if pnl > 0 else "❌"
        msg = f"{emoji} CERRADO {reason} | exit ${exit_price:,.0f} | P&L {'+' if pnl >= 0 else ''}{pnl:.2f} ({pnl_pct:+.1f}%)"
        self.add_log(msg)
        log.info(f"  [PAPER] {msg}")

    def close_binance_position(self, current_price: float, reason: str = "SIGNAL"):
        """Cierra todas las posiciones abiertas de Binance."""
        for trade in list(self.state.open_trades):
            if trade.bot == "binance":
                self._close_binance_trade(trade, current_price, reason)
                self.state.open_trades.remove(trade)
        self.save()

    def get_binance_position(self) -> Optional[Trade]:
        """Retorna la posición abierta de Binance si existe."""
        for t in self.state.open_trades:
            if t.bot == "binance":
                return t
        return None

    def _recalc_stats(self):
        closed = [t for t in self.state.closed_trades if t.pnl is not None]
        wins = [t for t in closed if t.pnl > 0]
        self.state.win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
        self.state.total_pnl = round(sum(t.pnl for t in closed), 2)
        self.state.total_pnl_pct = round((self.state.current_capital - self.state.initial_capital) / self.state.initial_capital * 100, 2)
        if self.state.current_capital > self.state.peak_capital:
            self.state.peak_capital = self.state.current_capital
        dd = (self.state.peak_capital - self.state.current_capital) / self.state.peak_capital * 100
        self.state.max_drawdown = max(self.state.max_drawdown, dd)

    def update_market_data(self, **kwargs):
        """Actualiza datos de mercado en el estado (para el dashboard)."""
        for k, v in kwargs.items():
            if hasattr(self.state, k):
                setattr(self.state, k, v)
        self.state.last_updated = datetime.now().isoformat()
        self.save()


# ─── Factory ──────────────────────────────────────────────────────────────────

def get_binance_engine(initial_capital: float = 500.0) -> PaperTradingEngine:
    return PaperTradingEngine("binance", initial_capital, BINANCE_STATE)


# ─── Polymarket Paper Trading Engine ─────────────────────────────────────────

POLY_DIR = Path("./polymarket_data")
POLY_DIR.mkdir(exist_ok=True)
POLYMARKET_STATE = POLY_DIR / "state.json"


class PolymarketEngine:
    """Paper trading engine for Polymarket prediction markets."""

    def __init__(self, initial_capital: float = 300.0):
        self.state_file = POLYMARKET_STATE
        self.state = self._load(initial_capital)

    def _load(self, initial_capital: float) -> dict:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                log.info(f"[polymarket] Estado cargado: ${data.get('current_capital', 0):.2f} | {len(data.get('open_positions', []))} posiciones abiertas")
                return data
            except Exception as e:
                log.warning(f"[polymarket] Error cargando estado: {e} — creando nuevo")

        return {
            "bot": "polymarket",
            "initial_capital": initial_capital,
            "current_capital": initial_capital,
            "total_pnl": 0.0,
            "total_pnl_pct": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "peak_capital": initial_capital,
            "max_drawdown": 0.0,
            "open_positions": [],
            "closed_trades": [],
            "cycle_log": [],
            "markets_scanned": 0,
            "signals_found": 0,
            "last_updated": datetime.now().isoformat(),
        }

    def save(self):
        self.state["last_updated"] = datetime.now().isoformat()
        self.state_file.write_text(json.dumps(self.state, indent=2, default=str))

    def add_log(self, msg: str):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg}
        self.state["cycle_log"] = [entry] + self.state.get("cycle_log", [])[:49]

    def open_trade(self, signal) -> dict:
        """Open a paper trade from a TradeSignal object."""
        # Kelly sizing
        p = signal.ai_probability
        q = 1 - p
        b = (1 / signal.price) - 1 if signal.price > 0 else 1
        kelly = (p * b - q) / b if b > 0 else 0
        kelly_frac = max(0, min(kelly * 0.25, 0.20))
        size_usdc = min(kelly_frac * self.state["current_capital"], 10.0)
        size_usdc = round(max(size_usdc, 1.0), 2)

        if size_usdc > self.state["current_capital"]:
            self.add_log(f"Capital insuficiente para {signal.market.question[:40]}...")
            return {}

        shares = round(size_usdc / signal.price, 4) if signal.price > 0 else 0

        position = {
            "token_id": signal.market.token_id,
            "question": signal.market.question,
            "side": signal.side,
            "entry_price": round(signal.price, 4),
            "current_price": round(signal.price, 4),
            "size_usdc": size_usdc,
            "shares": shares,
            "edge": round(signal.edge, 4),
            "ai_probability": round(signal.ai_probability, 4),
            "confidence": signal.confidence,
            "reasoning": signal.reasoning[:200],
            "entry_time": datetime.now().isoformat(),
            "unrealized_pnl": 0.0,
            "unrealized_pnl_pct": 0.0,
        }

        self.state["open_positions"].append(position)
        self.state["current_capital"] -= size_usdc
        self.state["total_trades"] = self.state.get("total_trades", 0) + 1

        self.add_log(f"OPEN {signal.side} {signal.market.question[:40]}... @ {signal.price:.2f} | ${size_usdc:.2f} | edge {signal.edge:+.1%}")
        self.save()
        return position

    def update_prices(self, price_map: dict):
        """Update current prices for open positions. price_map: {token_id: new_price}"""
        for pos in self.state["open_positions"]:
            tid = pos["token_id"]
            if tid in price_map:
                new_price = price_map[tid]
                pos["current_price"] = round(new_price, 4)
                # P&L: bought shares at entry_price, now worth current_price each
                cost = pos["size_usdc"]
                current_value = pos["shares"] * new_price
                pos["unrealized_pnl"] = round(current_value - cost, 2)
                pos["unrealized_pnl_pct"] = round((current_value - cost) / cost * 100, 2) if cost > 0 else 0

    def close_trade(self, token_id: str, exit_price: float, reason: str):
        """Close a position by token_id."""
        pos = None
        for p in self.state["open_positions"]:
            if p["token_id"] == token_id:
                pos = p
                break
        if not pos:
            return

        self.state["open_positions"].remove(pos)

        cost = pos["size_usdc"]
        exit_value = pos["shares"] * exit_price
        pnl = round(exit_value - cost, 2)
        pnl_pct = round((exit_value - cost) / cost * 100, 2) if cost > 0 else 0

        closed = {
            **pos,
            "exit_price": round(exit_price, 4),
            "exit_time": datetime.now().isoformat(),
            "exit_reason": reason,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "status": "CLOSED",
        }

        self.state["closed_trades"].append(closed)
        # Keep last 50
        self.state["closed_trades"] = self.state["closed_trades"][-50:]

        # Return capital + P&L
        self.state["current_capital"] += cost + pnl

        # Update stats
        self._recalc_stats()

        emoji = "+" if pnl >= 0 else ""
        self.add_log(f"CLOSE {reason} {pos['question'][:35]}... | P&L {emoji}${pnl:.2f} ({pnl_pct:+.1f}%)")
        self.save()

    def _recalc_stats(self):
        closed = self.state["closed_trades"]
        if closed:
            wins = [t for t in closed if t.get("pnl", 0) > 0]
            self.state["win_rate"] = round(len(wins) / len(closed) * 100, 1)
        self.state["total_pnl"] = round(
            self.state["current_capital"] - self.state["initial_capital"]
            + sum(p["size_usdc"] for p in self.state["open_positions"]), 2)
        self.state["total_pnl_pct"] = round(
            self.state["total_pnl"] / self.state["initial_capital"] * 100, 2)
        if self.state["current_capital"] > self.state.get("peak_capital", 0):
            self.state["peak_capital"] = self.state["current_capital"]
        peak = self.state.get("peak_capital", self.state["initial_capital"])
        if peak > 0:
            dd = (peak - self.state["current_capital"]) / peak * 100
            self.state["max_drawdown"] = max(self.state.get("max_drawdown", 0), dd)

    def check_exits(self, price_map: dict):
        """Check exit conditions for all open positions."""
        self.update_prices(price_map)
        to_close = []
        for pos in self.state["open_positions"]:
            tid = pos["token_id"]
            if tid not in price_map:
                continue

            current = price_map[tid]
            cost = pos["size_usdc"]
            value = pos["shares"] * current
            pnl_pct = (value - cost) / cost * 100 if cost > 0 else 0

            # TP: +15%
            if pnl_pct >= 15:
                to_close.append((tid, current, "TAKE_PROFIT"))
                continue

            # SL: -25%
            if pnl_pct <= -25:
                to_close.append((tid, current, "STOP_LOSS"))
                continue

            # Edge reversal: if our side moved against us past break-even
            entry = pos["entry_price"]
            if pos["side"] == "YES" and current < entry * 0.85:
                to_close.append((tid, current, "EDGE_REVERSAL"))
            elif pos["side"] == "NO" and current > entry * 1.15:
                to_close.append((tid, current, "EDGE_REVERSAL"))

        for tid, price, reason in to_close:
            self.close_trade(tid, price, reason)


def get_polymarket_engine(initial_capital: float = 300.0) -> PolymarketEngine:
    return PolymarketEngine(initial_capital)
