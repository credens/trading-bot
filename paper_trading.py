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

BINANCE_STATE  = STATE_DIR / "binance_state.json"
SCALPING_STATE = STATE_DIR / "scalping_state.json"
TRADING2_STATE = STATE_DIR / "trading2_state.json"


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
    # Trading2 extra
    active_strategy: str = "VOTE"
    last_vote: dict = field(default_factory=dict)


# ─── Paper Trading Engine ──────────────────────────────────────────────────────

class PaperTradingEngine:
    def __init__(self, bot: str, initial_capital: float, state_file: Path):
        self.bot = bot
        self.state_file = state_file
        self._total_wins = 0
        self._total_closed = 0
        self.state = self._load_or_create(initial_capital)

    def _load_or_create(self, initial_capital: float) -> BotState:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                state = BotState(
                    bot=self.bot,
                    initial_capital=data.get("initial_capital", initial_capital),
                    current_capital=data.get("current_capital", initial_capital),
                    peak_capital=data.get("current_capital", initial_capital),
                    total_pnl=data.get("total_pnl", 0.0),
                    total_pnl_pct=data.get("total_pnl_pct", 0.0),
                    win_rate=data.get("win_rate", 0.0),
                    max_drawdown=data.get("max_drawdown", 0.0),
                    cycle_log=data.get("cycle_log", []),
                    btc_price=data.get("btc_price", 0.0),
                    rsi=data.get("rsi", 50.0),
                    trend=data.get("trend", "neutral"),
                    macd_cross=data.get("macd_cross", "neutral"),
                    funding_rate=data.get("funding_rate", 0.0),
                    vol_ratio=data.get("vol_ratio", 1.0),
                )
                # Reconstruir trades desde all_closed_trades (lista más completa)
                all_closed = data.get("all_closed_trades", data.get("closed_trades", []))
                state.open_trades = [Trade(**{k: v for k, v in t.items() if k in Trade.__dataclass_fields__}) for t in data.get("open_trades", [])]
                state.closed_trades = [Trade(**{k: v for k, v in t.items() if k in Trade.__dataclass_fields__}) for t in all_closed]
                # Inicializar contadores históricos (para win_rate correcto tras truncamiento)
                total_on_disk = data.get("total_trades", len(all_closed))
                wr_on_disk    = data.get("win_rate", 0.0)
                if total_on_disk > len(all_closed) and wr_on_disk > 0:
                    self._total_closed = total_on_disk
                    self._total_wins   = round(wr_on_disk / 100 * total_on_disk)
                else:
                    self._total_closed = len(all_closed)
                    self._total_wins   = sum(1 for t in all_closed if (t.get("pnl") or 0) > 0)
                log.info(f"[{self.bot}] Estado cargado: ${state.current_capital:.2f} | {len(state.open_trades)} abiertos | {len(state.closed_trades)} cerrados ({self._total_closed} hist)")
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
        closed = [t for t in self.state.closed_trades if t.pnl is not None]
        wins = [t for t in closed if t.pnl > 0]
        open_trades = self.state.open_trades

        # Usar total_pnl del estado en memoria (actualizado por eventos de cierre),
        # NO recalcular desde la lista truncada de trades que pierde el historial.
        total_pnl = round(self.state.total_pnl, 2)
        reserved = sum(t.size for t in open_trades)
        current_capital = round(self.state.initial_capital - reserved + total_pnl, 2)
        # Win rate: usar el del estado (actualizado por close methods desde historial completo)
        win_rate = self.state.win_rate

        # Formato compatible con el dashboard
        dashboard = {
            "bot": "binance",
            "initial_capital": self.state.initial_capital,
            "current_capital": current_capital,
            "total_pnl": total_pnl,
            "total_pnl_raw": total_pnl,
            "total_pnl_pct": round(total_pnl / self.state.initial_capital * 100, 2),
            "win_rate": win_rate,
            "max_drawdown": self.state.max_drawdown,
            "open_trades": [asdict(t) for t in open_trades],
            "positions": {t.id: asdict(t) for t in open_trades},
            "closed_trades": [asdict(t) for t in closed[-30:]],
            "all_closed_trades": [asdict(t) for t in closed[-100:]],
            "total_trades": self._total_closed,
            "btc_price": self.state.btc_price,
            "rsi": self.state.rsi,
            "trend": self.state.trend,
            "macd_cross": self.state.macd_cross,
            "funding_rate": self.state.funding_rate,
            "vol_ratio": self.state.vol_ratio,
            "cycle_log": self.state.cycle_log or [],
            "last_updated": datetime.now().isoformat(),
        }

        # Merge con disco — respetar cierres manuales del dashboard
        try:
            if self.state_file.exists():
                disk = json.loads(self.state_file.read_text())
                disk_closed = disk.get("all_closed_trades", disk.get("closed_trades", []))[-100:]
                bot_closed = dashboard["all_closed_trades"]
                if len(disk_closed) > len(bot_closed):
                    dashboard["all_closed_trades"] = disk_closed
                    dashboard["closed_trades"] = disk_closed[-30:]
                    dashboard["total_pnl"] = round(sum(t.get("pnl", 0) for t in disk_closed), 2)
                    dashboard["total_pnl_raw"] = dashboard["total_pnl"]
                    dashboard["total_pnl_pct"] = round(dashboard["total_pnl"] / self.state.initial_capital * 100, 2)
                    d_wins = [t for t in disk_closed if t.get("pnl", 0) > 0]
                    dashboard["win_rate"] = round(len(d_wins) / len(disk_closed) * 100, 1) if disk_closed else 0
                    dashboard["total_trades"] = len(disk_closed)
                    # Filtrar open_trades/positions para excluir trades cerrados manualmente
                    closed_ids = {t.get("id") for t in disk_closed if t.get("id")}
                    dashboard["open_trades"] = [t for t in dashboard["open_trades"] if t.get("id") not in closed_ids]
                    dashboard["positions"] = {k: v for k, v in dashboard["positions"].items()
                                              if k not in closed_ids and v.get("id") not in closed_ids}
                    # Sincronizar en memoria para el próximo ciclo
                    self.state.open_trades = [t for t in self.state.open_trades if t.id not in closed_ids]
                # Cooldowns
                dashboard["cooldowns"] = {**disk.get("cooldowns", {}), **dashboard.get("cooldowns", {})}
        except Exception:
            pass

        self.state_file.write_text(json.dumps(dashboard, indent=2, default=str))

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

        # Win rate con contadores históricos (no se pierde en truncamiento)
        self._total_closed += 1
        if pnl > 0:
            self._total_wins += 1
        self.state.win_rate = round(self._total_wins / self._total_closed * 100, 1) if self._total_closed else 0.0

        emoji = "✅" if pnl > 0 else "❌"
        msg = f"{emoji} CERRADO {reason} | exit ${exit_price:,.0f} | P&L {'+' if pnl >= 0 else ''}{pnl:.2f} ({pnl_pct:+.1f}%)"
        self.add_log(msg)
        log.info(f"  [PAPER] {msg}")

        # Cooldown de 10 minutos después de stop loss para evitar re-entradas inmediatas
        if reason == "STOP_LOSS":
            import json as _json
            from pathlib import Path as _Path
            from datetime import timedelta as _td
            state_file = _Path(__file__).parent / "paper_trading" / "binance_state.json"
            try:
                raw = _json.loads(state_file.read_text()) if state_file.exists() else {}
                raw["cooldown_until"] = (datetime.now() + _td(minutes=10)).isoformat()
                state_file.write_text(_json.dumps(raw, indent=2))
                log.info(f"  ⏸ Cooldown 10min activado tras STOP_LOSS")
            except Exception as e:
                log.warning(f"Error escribiendo cooldown: {e}")

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
        self.state.win_rate = round(self._total_wins / self._total_closed * 100, 1) if self._total_closed else 0.0
        self.state.total_pnl = round(sum(t.pnl for t in closed), 2)  # NOTE: solo in-memory, save() usa state.total_pnl
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


def get_scalping_engine(initial_capital: float = 500.0) -> "PaperTradingEngine":
    """Crea engine de paper trading para el bot de scalping."""
    engine = PaperTradingEngine("scalping", initial_capital, SCALPING_STATE)
    # Agregar métodos específicos de scalping
    return engine


# Parchar PaperTradingEngine con métodos de scalping (para no crear subclase)
def _open_scalping_trade(self, decision: dict, current_price: float, capital: float, leverage: int):
    side = decision["decision"]
    pos_pct = min(float(decision.get("position_size_pct", 0.08)), 0.10)
    sl_pct  = float(decision.get("stop_loss_pct", 0.004))
    tp_pct  = float(decision.get("take_profit_pct", 0.008))
    size    = round(capital * pos_pct, 2)
    ts = int(time.time())
    from datetime import datetime as _dt
    tid = f"SC-{_dt.now().strftime('%H%M%S')}"
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
    )
    self.state.open_trades.append(trade)
    self.state.current_capital -= size
    self.state.trades_today += 1
    self.save()
    msg = f"✓ SCALP {side} | entrada ${current_price:,.0f} | SL ${sl:,.0f} | TP ${tp:,.0f} | size ${size:.0f}"
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
            self._close_binance_trade(trade, trade.stop_loss if hit_sl else trade.take_profit, reason)
            closed.append(trade)
    for t in closed:
        self.state.open_trades.remove(t)
    if closed:
        self.save()

def _get_scalping_position(self):
    for t in self.state.open_trades:
        if t.bot == "scalping":
            return t
    return None

def _close_scalping_position(self, current_price: float, reason: str = "SIGNAL"):
    for trade in list(self.state.open_trades):
        if trade.bot == "scalping":
            self._close_binance_trade(trade, current_price, reason)
            self.state.open_trades.remove(trade)
    self.save()

PaperTradingEngine.open_scalping_trade   = _open_scalping_trade
PaperTradingEngine.check_scalping_stops  = _check_scalping_stops
PaperTradingEngine.get_scalping_position = _get_scalping_position
PaperTradingEngine.close_scalping_position = _close_scalping_position


# ═══════════════════════════════════════════════════════════════════════════════
# Trading2 Engine
# ═══════════════════════════════════════════════════════════════════════════════

class Trading2Engine(PaperTradingEngine):
    """
    Paper trading engine para Trading2 (MACD + RSI+VWAP + CVD).
    Hereda toda la lógica de PaperTradingEngine y sobreescribe:
      - save()          → escribe trading2_state.json con campos extra
      - _load_or_create → carga los campos extra de Trading2
      - open_t2_trade   → abre un trade con metadata de estrategia y voto
      - check_t2_stops  → verifica SL/TP de posiciones Trading2
      - close_t2_position → cierra posición abierta
      - get_t2_position → retorna posición abierta si existe
      - update_vote     → guarda el último voto de las 3 estrategias
    """

    def _load_or_create(self, initial_capital: float) -> BotState:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                state = BotState(
                    bot=self.bot,
                    initial_capital=data.get("initial_capital", initial_capital),
                    current_capital=data.get("current_capital", initial_capital),
                    peak_capital=data.get("current_capital", initial_capital),
                    total_pnl=data.get("total_pnl", 0.0),
                    total_pnl_pct=data.get("total_pnl_pct", 0.0),
                    win_rate=data.get("win_rate", 0.0),
                    max_drawdown=data.get("max_drawdown", 0.0),
                    cycle_log=data.get("cycle_log", []),
                    btc_price=data.get("btc_price", 0.0),
                    active_strategy=data.get("active_strategy", "VOTE"),
                    last_vote=data.get("last_vote", {}),
                )
                all_closed = data.get("all_closed_trades", data.get("closed_trades", []))
                state.open_trades   = [Trade(**{k: v for k, v in t.items() if k in Trade.__dataclass_fields__}) for t in data.get("open_trades", [])]
                state.closed_trades = [Trade(**{k: v for k, v in t.items() if k in Trade.__dataclass_fields__}) for t in all_closed]
                total_on_disk = data.get("total_trades", len(all_closed))
                wr_on_disk    = data.get("win_rate", 0.0)
                if total_on_disk > len(all_closed) and wr_on_disk > 0:
                    self._total_closed = total_on_disk
                    self._total_wins   = round(wr_on_disk / 100 * total_on_disk)
                else:
                    self._total_closed = len(all_closed)
                    self._total_wins   = sum(1 for t in all_closed if (t.get("pnl") or 0) > 0)
                log.info(f"[trading2] Estado cargado: ${state.current_capital:.2f} | {len(state.open_trades)} abiertos | {len(state.closed_trades)} cerrados ({self._total_closed} hist)")
                return state
            except Exception as e:
                log.warning(f"[trading2] Error cargando estado: {e} — creando nuevo")

        return BotState(
            bot=self.bot,
            initial_capital=initial_capital,
            current_capital=initial_capital,
            peak_capital=initial_capital,
        )

    def save(self):
        closed     = [t for t in self.state.closed_trades if t.pnl is not None]
        wins       = [t for t in closed if t.pnl > 0]
        open_trades = self.state.open_trades

        total_pnl       = round(self.state.total_pnl, 2)
        reserved        = sum(t.size for t in open_trades)
        current_capital = round(self.state.initial_capital - reserved + total_pnl, 2)
        win_rate        = self.state.win_rate

        dashboard = {
            "bot":             "trading2",
            "initial_capital": self.state.initial_capital,
            "current_capital": current_capital,
            "total_pnl":       total_pnl,
            "total_pnl_raw":   total_pnl,
            "total_pnl_pct":   round(total_pnl / self.state.initial_capital * 100, 2),
            "win_rate":        win_rate,
            "max_drawdown":    self.state.max_drawdown,
            "open_trades":     [asdict(t) for t in open_trades],
            "positions":       {t.id: asdict(t) for t in open_trades},
            "closed_trades":   [asdict(t) for t in closed[-30:]],
            "all_closed_trades": [asdict(t) for t in closed[-100:]],
            "total_trades":    self._total_closed,
            "btc_price":       self.state.btc_price,
            "active_strategy": self.state.active_strategy,
            "last_vote":       self.state.last_vote,
            "cycle_log":       self.state.cycle_log or [],
            "last_updated":    datetime.now().isoformat(),
        }

        # Merge con disco — respetar cierres manuales del dashboard
        try:
            if self.state_file.exists():
                disk = json.loads(self.state_file.read_text())
                disk_closed = disk.get("all_closed_trades", disk.get("closed_trades", []))[-100:]
                bot_closed  = dashboard["all_closed_trades"]
                if len(disk_closed) > len(bot_closed):
                    dashboard["all_closed_trades"] = disk_closed
                    dashboard["closed_trades"]      = disk_closed[-30:]
                    dashboard["total_pnl"]          = round(sum(t.get("pnl", 0) for t in disk_closed), 2)
                    dashboard["total_pnl_raw"]      = dashboard["total_pnl"]
                    dashboard["total_pnl_pct"]      = round(dashboard["total_pnl"] / self.state.initial_capital * 100, 2)
                    d_wins = [t for t in disk_closed if t.get("pnl", 0) > 0]
                    dashboard["win_rate"]    = round(len(d_wins) / len(disk_closed) * 100, 1) if disk_closed else 0
                    dashboard["total_trades"] = len(disk_closed)
                    closed_ids = {t.get("id") for t in disk_closed if t.get("id")}
                    dashboard["open_trades"] = [t for t in dashboard["open_trades"] if t.get("id") not in closed_ids]
                    dashboard["positions"]   = {k: v for k, v in dashboard["positions"].items()
                                                if k not in closed_ids and v.get("id") not in closed_ids}
                    self.state.open_trades = [t for t in self.state.open_trades if t.id not in closed_ids]
                dashboard["cooldowns"] = {**disk.get("cooldowns", {}), **dashboard.get("cooldowns", {})}
        except Exception:
            pass

        self.state_file.write_text(json.dumps(dashboard, indent=2, default=str))

    # ── Trading2-specific helpers ──────────────────────────────────────────────

    def update_vote(self, macd: str, rsi_vwap: str, cvd: str, result: str):
        """Guarda el resultado del último ciclo de votación."""
        self.state.last_vote = {"macd": macd, "rsi_vwap": rsi_vwap, "cvd": cvd, "result": result}

    def open_t2_trade(self, decision: dict, current_price: float, capital: float, leverage: int) -> Optional[Trade]:
        """Abre un trade simulado de Trading2 con metadata de estrategia."""
        side    = decision["decision"]
        pos_pct = min(float(decision.get("position_size_pct", 0.06)), 0.10)
        sl_pct  = float(decision.get("stop_loss_pct", 0.02))
        tp_pct  = float(decision.get("take_profit_pct", 0.05))
        size    = round(capital * pos_pct, 2)

        if side == "LONG":
            sl = round(current_price * (1 - sl_pct), 2)
            tp = round(current_price * (1 + tp_pct), 2)
        else:
            sl = round(current_price * (1 + sl_pct), 2)
            tp = round(current_price * (1 - tp_pct), 2)

        trade = Trade(
            id=f"t2_{int(time.time())}",
            bot="trading2",
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
        # Guardar qué estrategia lo abrió (campo extra, no rompe dataclass)
        trade.__dict__["_strategy"] = decision.get("_strategy", self.state.active_strategy)

        self.state.open_trades.append(trade)
        self.state.current_capital -= size
        self.state.trades_today    += 1
        self.save()

        msg = f"✓ T2 {side} [{decision.get('_strategy','?')}] | ${current_price:,.0f} | SL ${sl:,.0f} | TP ${tp:,.0f} | ${size:.0f}"
        self.add_log(msg)
        log.info(f"  [T2 PAPER] {msg}")
        return trade

    def check_t2_stops(self, current_price: float):
        """Verifica SL/TP de posiciones Trading2."""
        closed = []
        for trade in self.state.open_trades:
            if trade.bot != "trading2":
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
                reason     = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"
                self._close_t2_trade(trade, exit_price, reason)
                closed.append(trade)

        for t in closed:
            self.state.open_trades.remove(t)
        if closed:
            self.save()

    def _close_t2_trade(self, trade: Trade, exit_price: float, reason: str):
        """Cierra un trade Trading2 y calcula P&L."""
        if trade.side == "LONG":
            raw_pnl = (exit_price - trade.entry_price) / trade.entry_price
        else:
            raw_pnl = (trade.entry_price - exit_price) / trade.entry_price

        pnl     = round(raw_pnl * trade.size * trade.leverage, 2)
        pnl_pct = round(raw_pnl * trade.leverage * 100, 2)

        trade.exit_price  = exit_price
        trade.exit_time   = datetime.now().isoformat()
        trade.exit_reason = reason
        trade.pnl         = pnl
        trade.pnl_pct     = pnl_pct
        trade.status      = "CLOSED"

        self.state.current_capital += trade.size + pnl
        self.state.total_pnl       += pnl
        self.state.total_pnl_pct    = round((self.state.current_capital - self.state.initial_capital) / self.state.initial_capital * 100, 2)
        self.state.closed_trades.append(trade)

        if self.state.current_capital > self.state.peak_capital:
            self.state.peak_capital = self.state.current_capital
        dd = (self.state.peak_capital - self.state.current_capital) / self.state.peak_capital * 100
        self.state.max_drawdown = max(self.state.max_drawdown, dd)

        self._total_closed += 1
        if pnl > 0:
            self._total_wins += 1
        self.state.win_rate = round(self._total_wins / self._total_closed * 100, 1) if self._total_closed else 0.0

        emoji = "✅" if pnl > 0 else "❌"
        msg   = f"{emoji} T2 CERRADO {reason} | ${exit_price:,.0f} | P&L {'+' if pnl >= 0 else ''}{pnl:.2f} ({pnl_pct:+.1f}%)"
        self.add_log(msg)
        log.info(f"  [T2 PAPER] {msg}")

        if reason == "STOP_LOSS":
            try:
                raw = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
                from datetime import timedelta
                raw["cooldown_until"] = (datetime.now() + timedelta(minutes=10)).isoformat()
                self.state_file.write_text(json.dumps(raw, indent=2))
                log.info("  ⏸ Cooldown 10min activado tras STOP_LOSS")
            except Exception as e:
                log.warning(f"Error escribiendo cooldown: {e}")

    def close_t2_position(self, current_price: float, reason: str = "SIGNAL"):
        """Cierra todas las posiciones abiertas de Trading2."""
        for trade in list(self.state.open_trades):
            if trade.bot == "trading2":
                self._close_t2_trade(trade, current_price, reason)
                self.state.open_trades.remove(trade)
        self.save()

    def get_t2_position(self) -> Optional[Trade]:
        """Retorna la posición abierta de Trading2 si existe."""
        for t in self.state.open_trades:
            if t.bot == "trading2":
                return t
        return None


def get_trading2_engine(initial_capital: float = 500.0) -> Trading2Engine:
    return Trading2Engine("trading2", initial_capital, TRADING2_STATE)
