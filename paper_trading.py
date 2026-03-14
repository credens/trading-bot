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
POLYMARKET_STATE = STATE_DIR / "polymarket_state.json"


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    id: str
    bot: str                    # "binance" | "polymarket"
    side: str                   # "LONG" | "SHORT" | "YES" | "NO"
    entry_price: float
    entry_time: str
    size: float                 # USDT para binance, USDC para polymarket
    stop_loss: float
    take_profit: float
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None  # "STOP_LOSS" | "TAKE_PROFIT" | "SIGNAL" | "EXPIRED"
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    status: str = "OPEN"        # "OPEN" | "CLOSED"
    reasoning: str = ""
    confidence: str = ""
    leverage: int = 1
    # Polymarket extra
    market_question: str = ""
    market_probability: float = 0.5
    ai_probability: float = 0.5
    edge: float = 0.0


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

    # ─── Polymarket Operations ────────────────────────────────────────────────

    def open_polymarket_trade(self, signal) -> Optional[Trade]:
        """Registra apuesta simulada de Polymarket."""
        size = min(signal.edge * 50, 15.0)  # sizing basado en edge, máx $15
        size = max(size, 2.0)

        trade = Trade(
            id=f"pm_{int(time.time())}",
            bot="polymarket",
            side=signal.side,
            entry_price=signal.price,
            entry_time=datetime.now().isoformat(),
            size=round(size, 2),
            stop_loss=0.0,
            take_profit=1.0 if signal.side in ("YES", "LONG") else 0.0,
            reasoning=signal.reasoning,
            confidence=signal.confidence,
            market_question=signal.market.question,
            market_probability=signal.market_probability,
            ai_probability=signal.ai_probability,
            edge=signal.edge,
        )

        self.state.open_trades.append(trade)
        self.state.current_capital -= size
        self.state.trades_today += 1
        self.save()

        msg = f"✓ PAPER {signal.side} | {signal.market.question[:50]}... | edge {signal.edge:+.1%} | ${size:.2f}"
        self.add_log(msg)
        log.info(f"  [PAPER] {msg}")
        return trade

    def resolve_polymarket_trade(self, trade_id: str, resolved_outcome: str, final_price: float):
        """Resuelve un mercado de Polymarket con el resultado real."""
        for trade in self.state.open_trades:
            if trade.id == trade_id and trade.bot == "polymarket":
                won = (trade.side == "YES" and resolved_outcome == "YES") or \
                      (trade.side == "NO" and resolved_outcome == "NO")

                pnl = trade.size * (1 / trade.entry_price - 1) if won else -trade.size
                pnl = round(pnl, 2)

                trade.exit_price = final_price
                trade.exit_time = datetime.now().isoformat()
                trade.exit_reason = "RESOLVED"
                trade.pnl = pnl
                trade.pnl_pct = round(pnl / trade.size * 100, 1)
                trade.status = "CLOSED"

                self.state.current_capital += trade.size + pnl
                self.state.total_pnl += pnl
                self.state.closed_trades.append(trade)
                self.state.open_trades.remove(trade)

                self._recalc_stats()
                self.save()

                emoji = "✅" if won else "❌"
                msg = f"{emoji} RESUELTO {resolved_outcome} | {trade.market_question[:40]}... | P&L ${pnl:+.2f}"
                self.add_log(msg)
                log.info(f"  [PAPER] {msg}")
                break

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

def get_polymarket_engine(initial_capital: float = 300.0) -> PaperTradingEngine:
    return PaperTradingEngine("polymarket", initial_capital, POLYMARKET_STATE)
