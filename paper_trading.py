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
import os
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger(__name__)

STATE_DIR = Path("./paper_trading")
STATE_DIR.mkdir(exist_ok=True)


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    id: str
    bot: str
    side: str
    entry_price: float
    entry_time: str
    size: float
    stop_loss: float
    take_profit: float
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    status: str = "OPEN"
    reasoning: str = ""
    confidence: str = ""
    leverage: int = 1
    best_price: Optional[float] = None
    entry_adx: Optional[float] = None
    breakeven_activated: bool = False  # Añadido para evitar errores de reconstrucción
    symbol: Optional[str] = None


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
    next_liquidity_check: Optional[str] = None
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
                content = self.state_file.read_text()
                if not content.strip():
                    raise ValueError("Archivo de estado vacío")
                
                data = json.loads(content)
                state = BotState(**{k: v for k, v in data.items() if k in BotState.__dataclass_fields__})
                
                # Reconstrucción robusta de trades (filtrando campos desconocidos)
                def make_trade(t_dict):
                    valid_fields = {k: v for k, v in t_dict.items() if k in Trade.__dataclass_fields__}
                    return Trade(**valid_fields)

                state.open_trades = [make_trade(t) for t in data.get("open_trades", [])]
                state.closed_trades = [make_trade(t) for t in data.get("closed_trades", [])[-50:]]
                return state
            except Exception as e:
                log.error(f"[{self.bot}] Error cargando estado: {e}")

        state = BotState(
            bot=self.bot,
            initial_capital=initial_capital,
            current_capital=initial_capital,
            peak_capital=initial_capital,
        )
        return state

    def save(self):
        """Guarda el estado en disco de forma segura."""
        try:
            data = asdict(self.state)
            temp_path = self.state_file.with_suffix(".tmp")
            
            # Escribir en archivo temporal primero para evitar corrupción
            temp_path.write_text(json.dumps(data, indent=2, default=str))
            
            # Renombrar de forma atómica
            temp_path.replace(self.state_file)
        except Exception as e:
            log.error(f"Error guardando estado: {e}")

    def add_log(self, msg: str):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg}
        self.state.cycle_log = [entry] + self.state.cycle_log[:49]  # últimos 50 logs
        self.state.last_updated = datetime.now().isoformat()

    # ─── Trade Operations ────────────────────────────────────────────────────

    def _close_trade(self, trade: Trade, exit_price: float, reason: str):
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

    def update_market_data(self, btc_price: float, **kwargs):
        """Actualiza datos de mercado y recalcula P&L de posiciones abiertas."""
        self.state.btc_price = btc_price
        for k, v in kwargs.items():
            if hasattr(self.state, k):
                setattr(self.state, k, v)
        
        # Recalcular P&L flotante para que el capital refleje la realidad
        for trade in self.state.open_trades:
            if trade.side == "LONG":
                trade.pnl_pct = round((btc_price - trade.entry_price) / trade.entry_price * 100 * trade.leverage, 2)
            else:
                trade.pnl_pct = round((trade.entry_price - btc_price) / trade.entry_price * 100 * trade.leverage, 2)
            trade.pnl = round(trade.pnl_pct / 100 * trade.size, 2)

        self.state.last_updated = datetime.now().isoformat()
        self.save()

    def open_trade(self, bot: str, symbol: str, side: str, size: float,
                   leverage: int, entry_price: float, sl_pct: float, tp_pct: float):
        """Open a paper trade. Deducts size from current_capital."""
        if side == "LONG":
            sl = round(entry_price * (1 - sl_pct), 8)
            tp = round(entry_price * (1 + tp_pct), 8)
        else:
            sl = round(entry_price * (1 + sl_pct), 8)
            tp = round(entry_price * (1 - tp_pct), 8)
        tid = f"{bot[:3].upper()}-{datetime.now().strftime('%H%M%S%f')[:12]}"
        trade = Trade(
            id=tid, bot=bot, symbol=symbol, side=side,
            entry_price=entry_price, entry_time=datetime.now().isoformat(),
            size=size, stop_loss=sl, take_profit=tp, leverage=leverage,
        )
        self.state.open_trades.append(trade)
        self.state.current_capital -= size
        self.state.trades_today += 1
        self.add_log(f"📈 {side} {symbol} | {leverage}x | ${size:.0f} | TP:{tp_pct*100:.2f}% SL:{sl_pct*100:.2f}%")
        log.info(f"  [PAPER] OPEN {side} {symbol} @ {entry_price} size:{size} lev:{leverage}x")
        return trade

    def close_by_symbol(self, symbol: str, exit_price: float, reason: str, bot: str = None) -> float:
        """Close open trade by symbol. Returns PnL."""
        for trade in list(self.state.open_trades):
            if trade.symbol == symbol and (bot is None or trade.bot == bot):
                self._close_trade(trade, exit_price, reason)
                self.state.open_trades.remove(trade)
                self.save()
                return trade.pnl or 0.0
        return 0.0

    def close_by_id(self, trade_id: str, exit_price: float, reason: str, bot: str = None) -> float:
        """Close an open trade by id. Returns PnL."""
        for trade in list(self.state.open_trades):
            if trade.id == trade_id and (bot is None or trade.bot == bot):
                self._close_trade(trade, exit_price, reason)
                self.state.open_trades.remove(trade)
                self.save()
                return trade.pnl or 0.0
        return 0.0

    def check_stops(self, prices: dict, bot: str = None) -> list:
        """Check SL/TP for all open trades. Returns list of closed Trade objects."""
        closed = []
        for trade in list(self.state.open_trades):
            if bot and trade.bot != bot:
                continue
            sym = getattr(trade, "symbol", None) or ""
            price = prices.get(sym)
            if not price:
                continue
            if trade.side == "LONG":
                hit_sl = price <= trade.stop_loss
                hit_tp = price >= trade.take_profit
            else:
                hit_sl = price >= trade.stop_loss
                hit_tp = price <= trade.take_profit
            if hit_sl or hit_tp:
                reason = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"
                exit_px = trade.stop_loss if hit_sl else trade.take_profit
                self._close_trade(trade, exit_px, reason)
                self.state.open_trades.remove(trade)
                closed.append(trade)
        if closed:
            self.save()
        return closed


# ─── Factory ──────────────────────────────────────────────────────────────────

SCALPING_STATE = STATE_DIR / "scalping_state.json"
BTC_STATE      = STATE_DIR / "btc_state.json"
ALT_STATE      = STATE_DIR / "alt_state.json"

def get_scalping_engine(initial_capital: float = 200.0) -> PaperTradingEngine:
    return PaperTradingEngine("scalping", initial_capital, SCALPING_STATE)

def get_btc_engine(initial_capital: float = 200.0) -> PaperTradingEngine:
    return PaperTradingEngine("btc_scalp", initial_capital, BTC_STATE)

def get_alt_engine(initial_capital: float = 200.0) -> PaperTradingEngine:
    return PaperTradingEngine("alt_scalp", initial_capital, ALT_STATE)


# ─── Scalping-specific methods (monkey-patched onto PaperTradingEngine) ──────

def _open_scalping_trade(self, decision: dict, current_price: float, capital: float, leverage: int):
    side = decision["decision"]
    max_pos_pct = float(os.getenv("SCALP_MAX_POS_PCT", "0.50"))
    pos_pct = min(float(decision.get("position_size_pct", 0.15)), max_pos_pct)
    sl_pct  = float(decision.get("stop_loss_pct", 0.004))
    tp_pct  = float(decision.get("take_profit_pct", 0.008))
    size    = round(capital * pos_pct, 2)
    tid = f"SC-{datetime.now().strftime('%H%M%S')}"
    if side == "LONG":
        sl = round(current_price * (1 - sl_pct), 2)
        tp = round(current_price * (1 + tp_pct), 2)
    else:
        sl = round(current_price * (1 + sl_pct), 2)
        tp = round(current_price * (1 - tp_pct), 2)
    trade = Trade(
        id=tid, bot="scalping", side=side,
        entry_price=current_price, entry_time=datetime.now().isoformat(),
        size=size, stop_loss=sl, take_profit=tp,
        reasoning=decision.get("reasoning", ""),
        confidence=decision.get("confidence", ""),
        leverage=leverage,
        entry_adx=decision.get("entry_adx"),
    )
    self.state.open_trades.append(trade)
    self.state.current_capital -= size
    self.state.trades_today += 1
    self.save()
    msg = f"SCALP {side} | entrada ${current_price:,.0f} | SL ${sl:,.0f} | TP ${tp:,.0f} | size ${size:.0f}"
    self.add_log(msg)
    log.info(f"  [SCALP] {msg}")
    return trade

def _check_scalping_stops(self, current_price: float):
    closed = []
    for trade in self.state.open_trades:
        if trade.bot != "scalping":
            continue
        hit_sl = hit_tp = False
        if trade.side == "LONG":
            hit_sl = current_price <= trade.stop_loss
            hit_tp = current_price >= trade.take_profit
        else:
            hit_sl = current_price >= trade.stop_loss
            hit_tp = current_price <= trade.take_profit
        if hit_sl or hit_tp:
            reason = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"
            self._close_trade(trade, trade.stop_loss if hit_sl else trade.take_profit, reason)
            closed.append(trade)
    for t in closed:
        self.state.open_trades.remove(t)
    if closed:
        self.save()

def _update_breakeven_stop(self, current_price: float):
    """MEJORA 1: Mueve SL a breakeven cuando el trade gana +0.5%"""
    for trade in self.state.open_trades:
        if trade.bot != "scalping":
            continue
        if trade.side == "LONG":
            current_profit = (current_price - trade.entry_price) / trade.entry_price
        else:
            current_profit = (trade.entry_price - current_price) / trade.entry_price
        
        # Si estamos en profit >= 0.5% y no hemos activado breakeven
        if current_profit >= 0.005 and not getattr(trade, 'breakeven_activated', False):
            breakeven_price = trade.entry_price + (0.001 if trade.side == "LONG" else -0.001)
            trade.stop_loss = breakeven_price
            trade.breakeven_activated = True
            emoji = "🛡️" if trade.side == "LONG" else "🛡️"
            log.info(f"  {emoji} Breakeven Stop activado {trade.id}: SL -> {breakeven_price:.0f}")
            self.save()

def _apply_profit_locking(self, current_price: float):
    """MEJORA 2: Reduce TP cuando el trade está muy en ganancia (>10%-20%)"""
    for trade in self.state.open_trades:
        if trade.bot != "scalping":  # Solo para altcoins en state dict
            continue
        if trade.side == "LONG":
            current_pnl = (current_price - trade.entry_price) / trade.entry_price
        else:
            current_pnl = (trade.entry_price - current_price) / trade.entry_price
        
        # Si ganancia > 20%, bajar TP a +5%
        if current_pnl >= 0.20:
            if trade.side == "LONG":
                trade.take_profit = min(trade.take_profit, trade.entry_price * 1.05)
            else:
                trade.take_profit = max(trade.take_profit, trade.entry_price * 0.95)
        # Si ganancia > 10%, bajar TP a +3%
        elif current_pnl >= 0.10:
            if trade.side == "LONG":
                trade.take_profit = min(trade.take_profit, trade.entry_price * 1.03)
            else:
                trade.take_profit = max(trade.take_profit, trade.entry_price * 0.97)
        self.save()

def _get_scalping_position(self):
    for t in self.state.open_trades:
        if t.bot == "scalping":
            return t
    return None

def _close_scalping_position(self, current_price: float, reason: str = "SIGNAL"):
    for trade in list(self.state.open_trades):
        if trade.bot == "scalping":
            self._close_trade(trade, current_price, reason)
            self.state.open_trades.remove(trade)
    self.save()

PaperTradingEngine.open_scalping_trade   = _open_scalping_trade
PaperTradingEngine.check_scalping_stops  = _check_scalping_stops
PaperTradingEngine.update_breakeven_stop = _update_breakeven_stop
PaperTradingEngine.apply_profit_locking  = _apply_profit_locking
PaperTradingEngine.get_scalping_position = _get_scalping_position
PaperTradingEngine.close_scalping_position = _close_scalping_position
