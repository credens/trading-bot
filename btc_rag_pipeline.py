"""
RSI Mean Reversion Bot
======================
Estrategia: cada día comprar la acción más sobrevendida (RSI10 más bajo)
de un basket de 7 acciones del S&P500.
Exit: +3% take profit | -3% stop loss | cierre del día

Basado en estudio con basket: ["VLO", "AMAT", "EOG", "MOS", "COST", "EQIX", "GILD"]
Resultado histórico: +86% en 2.5 meses vs +25% buy & hold

MODOS:
  python rsi_bot.py backtest     → backtest histórico
  python rsi_bot.py paper        → paper trading con Alpaca (datos reales, sin dinero)
  python rsi_bot.py live         → trading real (⚠️ cuidado)

SETUP:
  pip install alpaca-trade-api yfinance pandas numpy python-dotenv
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta, date
from pathlib import Path
import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Configuración ────────────────────────────────────────────────────────────

BASKET = ["VLO", "AMAT", "EOG", "MOS", "COST", "EQIX", "GILD"]
RSI_PERIOD = 10
TAKE_PROFIT = 0.03     # +3%
STOP_LOSS = 0.03       # -3%
CAPITAL = float(os.getenv("CAPITAL", "10000"))  # capital inicial
MAX_POSITION_PCT = 0.95  # usar 95% del capital por trade

ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

DATA_DIR = Path("./rsi_bot_data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

# ─── RSI Calculator ───────────────────────────────────────────────────────────

def calculate_rsi(prices: pd.Series, period: int = 10) -> float:
    """Calcula RSI del período dado. Retorna el último valor."""
    if len(prices) < period + 1:
        return 50.0

    delta = prices.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


# ─── Data Fetcher ─────────────────────────────────────────────────────────────

def fetch_prices_yfinance(symbols: list, period: str = "3mo") -> dict:
    """Descarga precios históricos con yfinance."""
    import yfinance as yf
    data = {}
    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period=period)
            if not hist.empty:
                data[sym] = hist["Close"]
        except Exception as e:
            log.warning(f"Error descargando {sym}: {e}")
    return data


def get_rsi_ranking(price_data: dict, as_of_date=None) -> list[dict]:
    """
    Calcula RSI para cada símbolo y los rankea de menor a mayor.
    El más sobrevendido (RSI más bajo) va primero.
    """
    rankings = []
    for sym, prices in price_data.items():
        if as_of_date:
            prices = prices[prices.index <= as_of_date]
        if len(prices) < RSI_PERIOD + 5:
            continue
        rsi = calculate_rsi(prices, RSI_PERIOD)
        rankings.append({
            "symbol": sym,
            "rsi": round(rsi, 2),
            "price": round(float(prices.iloc[-1]), 2),
        })

    rankings.sort(key=lambda x: x["rsi"])
    return rankings


# ─── Backtester ───────────────────────────────────────────────────────────────

def run_backtest(symbols: list = BASKET, period: str = "6mo", initial_capital: float = CAPITAL):
    """
    Backtest completo de la estrategia.
    Cada día: comprar el más sobrevendido, vender al día siguiente (+3%/-3%/cierre).
    """
    log.info("=" * 60)
    log.info(f"BACKTEST — RSI{RSI_PERIOD} Mean Reversion")
    log.info(f"Basket: {symbols}")
    log.info(f"Capital inicial: ${initial_capital:,.2f}")
    log.info("=" * 60)

    # Descargar datos
    log.info("Descargando datos históricos...")
    price_data = fetch_prices_yfinance(symbols, period=period)

    if not price_data:
        log.error("No se pudieron descargar datos.")
        return None

    # Alinear fechas
    df = pd.DataFrame(price_data)
    df = df.dropna()
    dates = df.index.tolist()

    log.info(f"Período: {dates[0].date()} → {dates[-1].date()} ({len(dates)} días de mercado)")

    # Simular
    capital = initial_capital
    trades = []
    equity_curve = [capital]

    for i in range(RSI_PERIOD + 5, len(dates) - 1):
        entry_date = dates[i]
        exit_date = dates[i + 1]

        # Calcular RSI para cada símbolo hasta entry_date
        rankings = []
        for sym in symbols:
            prices = df[sym].iloc[:i + 1]
            rsi = calculate_rsi(prices, RSI_PERIOD)
            rankings.append({"symbol": sym, "rsi": rsi, "price": float(df[sym].iloc[i])})

        rankings.sort(key=lambda x: x["rsi"])
        target = rankings[0]  # más sobrevendido

        entry_price = target["price"]
        exit_price_raw = float(df[target["symbol"]].iloc[i + 1])

        # Calcular exit (TP/SL/cierre)
        # Nota: en backtest diario no tenemos intraday, simulamos con precio del día siguiente
        change = (exit_price_raw - entry_price) / entry_price

        if change >= TAKE_PROFIT:
            exit_price = entry_price * (1 + TAKE_PROFIT)
            exit_reason = "TAKE_PROFIT"
        elif change <= -STOP_LOSS:
            exit_price = entry_price * (1 - STOP_LOSS)
            exit_reason = "STOP_LOSS"
        else:
            exit_price = exit_price_raw
            exit_reason = "CLOSE"

        # P&L
        shares = (capital * MAX_POSITION_PCT) / entry_price
        pnl = shares * (exit_price - entry_price)
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        capital += pnl

        trade = {
            "date": str(entry_date.date()),
            "symbol": target["symbol"],
            "rsi_entry": round(target["rsi"], 2),
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "exit_reason": exit_reason,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "capital": round(capital, 2),
        }
        trades.append(trade)
        equity_curve.append(capital)

    # ── Métricas ──────────────────────────────────────────────────────────────
    df_trades = pd.DataFrame(trades)
    wins = df_trades[df_trades["pnl"] > 0]
    losses = df_trades[df_trades["pnl"] <= 0]
    win_rate = len(wins) / len(df_trades) * 100 if len(df_trades) > 0 else 0

    total_return = (capital - initial_capital) / initial_capital * 100

    # Max drawdown
    equity = pd.Series(equity_curve)
    peak = equity.cummax()
    drawdown = (equity - peak) / peak * 100
    max_drawdown = float(drawdown.min())

    # Buy & hold del basket (promedio)
    bh_returns = []
    for sym in symbols:
        start_price = float(df[sym].iloc[RSI_PERIOD + 5])
        end_price = float(df[sym].iloc[-1])
        bh_returns.append((end_price - start_price) / start_price * 100)
    buy_hold_avg = np.mean(bh_returns)

    # Profit factor
    gross_profit = float(wins["pnl"].sum()) if len(wins) > 0 else 0
    gross_loss = abs(float(losses["pnl"].sum())) if len(losses) > 0 else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    log.info("\n" + "=" * 60)
    log.info("RESULTADOS DEL BACKTEST")
    log.info("=" * 60)
    log.info(f"  Capital inicial:     ${initial_capital:>12,.2f}")
    log.info(f"  Capital final:       ${capital:>12,.2f}")
    log.info(f"  Retorno total:       {total_return:>+11.2f}%")
    log.info(f"  Buy & Hold promedio: {buy_hold_avg:>+11.2f}%")
    log.info(f"  Outperformance:      {total_return - buy_hold_avg:>+11.2f}%")
    log.info(f"  ─────────────────────────────────────")
    log.info(f"  Total trades:        {len(df_trades):>12}")
    log.info(f"  Ganadores:           {len(wins):>12} ({win_rate:.1f}%)")
    log.info(f"  Perdedores:          {len(losses):>12}")
    log.info(f"  Profit factor:       {profit_factor:>12.2f}")
    log.info(f"  Max drawdown:        {max_drawdown:>+11.2f}%")
    log.info(f"  ─────────────────────────────────────")

    # Por exit reason
    for reason in ["TAKE_PROFIT", "STOP_LOSS", "CLOSE"]:
        subset = df_trades[df_trades["exit_reason"] == reason]
        if len(subset) > 0:
            avg_pnl = subset["pnl_pct"].mean()
            log.info(f"  {reason:<20} {len(subset):>4} trades | avg {avg_pnl:>+.2f}%")

    log.info(f"\n  Símbolo más operado:")
    for sym, count in df_trades["symbol"].value_counts().head(5).items():
        sym_trades = df_trades[df_trades["symbol"] == sym]
        sym_return = sym_trades["pnl"].sum()
        log.info(f"    {sym}: {count} trades | ${sym_return:>+.2f}")

    # Guardar resultados
    results = {
        "total_return_pct": round(total_return, 2),
        "buy_hold_pct": round(buy_hold_avg, 2),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown": round(max_drawdown, 2),
        "total_trades": len(df_trades),
        "final_capital": round(capital, 2),
        "trades": trades[-20:],  # últimos 20
    }

    with open(DATA_DIR / "backtest_results.json", "w") as f:
        json.dump(results, f, indent=2)

    log.info(f"\n  Resultados guardados en {DATA_DIR}/backtest_results.json")
    return results


# ─── Paper / Live Trading con Alpaca ──────────────────────────────────────────

def get_alpaca_client(paper: bool = True):
    """Inicializa cliente Alpaca."""
    try:
        import alpaca_trade_api as tradeapi
        base_url = ALPACA_BASE_URL if paper else "https://api.alpaca.markets"
        api = tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, base_url, api_version="v2")
        account = api.get_account()
        log.info(f"Alpaca conectado — Balance: ${float(account.cash):,.2f} | {'PAPER' if paper else 'LIVE'}")
        return api
    except Exception as e:
        log.error(f"Error conectando Alpaca: {e}")
        return None


def get_current_rsi_ranking(api) -> list[dict]:
    """Calcula RSI actual usando datos de Alpaca."""
    rankings = []
    end = datetime.now()
    start = end - timedelta(days=60)

    for sym in BASKET:
        try:
            bars = api.get_bars(
                sym,
                timeframe="1Day",
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                limit=50,
            ).df

            if bars.empty or len(bars) < RSI_PERIOD + 5:
                continue

            prices = bars["close"]
            rsi = calculate_rsi(prices, RSI_PERIOD)
            current_price = float(prices.iloc[-1])

            rankings.append({
                "symbol": sym,
                "rsi": round(rsi, 2),
                "price": current_price,
            })
        except Exception as e:
            log.warning(f"Error obteniendo datos de {sym}: {e}")

    rankings.sort(key=lambda x: x["rsi"])
    return rankings


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"position": None, "entry_price": None, "entry_date": None, "trades": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def run_trading_cycle(api, paper: bool = True):
    """
    Un ciclo completo de trading:
    1. Si hay posición abierta: verificar TP/SL
    2. Si no hay posición: comprar el más sobrevendido
    """
    mode = "PAPER" if paper else "LIVE"
    log.info(f"\n{'='*55}")
    log.info(f"CICLO RSI BOT [{mode}] — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info(f"{'='*55}")

    state = load_state()
    account = api.get_account()
    cash = float(account.cash)
    portfolio_value = float(account.portfolio_value)

    log.info(f"Cash: ${cash:,.2f} | Portfolio: ${portfolio_value:,.2f}")

    # ── 1. Verificar posición abierta ─────────────────────────────────────────
    if state.get("position"):
        sym = state["position"]
        entry_price = state["entry_price"]
        entry_date = state["entry_date"]

        try:
            position = api.get_position(sym)
            current_price = float(position.current_price)
            change = (current_price - entry_price) / entry_price

            log.info(f"Posición abierta: {sym} | entrada ${entry_price:.2f} | actual ${current_price:.2f} | {change:+.2%}")

            should_exit = False
            exit_reason = ""

            today = str(date.today())
            if change >= TAKE_PROFIT:
                should_exit = True
                exit_reason = "TAKE_PROFIT"
            elif change <= -STOP_LOSS:
                should_exit = True
                exit_reason = "STOP_LOSS"
            elif entry_date != today:
                # Día diferente = vender al cierre
                should_exit = True
                exit_reason = "CLOSE_EOD"

            if should_exit:
                log.info(f"Cerrando posición: {exit_reason}")
                api.submit_order(
                    symbol=sym,
                    qty=int(position.qty),
                    side="sell",
                    type="market",
                    time_in_force="day",
                )
                pnl = (current_price - entry_price) * int(position.qty)
                pnl_pct = change * 100
                log.info(f"  ✅ Vendido | P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")

                # Registrar trade
                state["trades"].append({
                    "symbol": sym,
                    "entry": entry_price,
                    "exit": current_price,
                    "exit_reason": exit_reason,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "date": today,
                })
                state["position"] = None
                state["entry_price"] = None
                state["entry_date"] = None
                save_state(state)

                # Stats rápidas
                trades = state["trades"]
                wins = [t for t in trades if t["pnl"] > 0]
                wr = len(wins) / len(trades) * 100 if trades else 0
                total_pnl = sum(t["pnl"] for t in trades)
                log.info(f"  Stats: {len(trades)} trades | Win rate: {wr:.0f}% | P&L total: ${total_pnl:+.2f}")
            else:
                log.info("  Manteniendo posición.")
                save_state(state)
                return

        except Exception as e:
            log.warning(f"No hay posición activa en Alpaca: {e}")
            state["position"] = None
            save_state(state)

    # ── 2. Buscar nueva entrada ────────────────────────────────────────────────
    if state.get("position"):
        return  # ya hay posición

    log.info("\nBuscando entrada...")
    rankings = get_current_rsi_ranking(api)

    if not rankings:
        log.warning("No se pudo calcular RSI. Saltando ciclo.")
        return

    log.info("RSI Ranking:")
    for r in rankings:
        log.info(f"  {r['symbol']:6} RSI: {r['rsi']:6.2f} | ${r['price']:.2f}")

    target = rankings[0]
    log.info(f"\n→ Target: {target['symbol']} (RSI más bajo: {target['rsi']:.2f})")

    # Calcular shares
    invest = cash * MAX_POSITION_PCT
    shares = int(invest / target["price"])

    if shares < 1:
        log.warning(f"Capital insuficiente para comprar {target['symbol']}")
        return

    cost = shares * target["price"]
    log.info(f"  Comprando {shares} acciones de {target['symbol']} @ ${target['price']:.2f} = ${cost:,.2f}")

    try:
        api.submit_order(
            symbol=target["symbol"],
            qty=shares,
            side="buy",
            type="market",
            time_in_force="day",
        )
        log.info(f"  ✅ Orden enviada")

        state["position"] = target["symbol"]
        state["entry_price"] = target["price"]
        state["entry_date"] = str(date.today())
        save_state(state)

        # Calcular niveles de referencia
        tp_price = round(target["price"] * (1 + TAKE_PROFIT), 2)
        sl_price = round(target["price"] * (1 - STOP_LOSS), 2)
        log.info(f"  Take Profit: ${tp_price:.2f} | Stop Loss: ${sl_price:.2f}")

    except Exception as e:
        log.error(f"  ❌ Error enviando orden: {e}")


def run_paper(check_interval_minutes: int = 30):
    """Loop de paper trading — verifica posiciones y busca entradas."""
    log.info("🤖 RSI Mean Reversion Bot — PAPER TRADING")
    log.info(f"   Basket: {BASKET}")
    log.info(f"   RSI Period: {RSI_PERIOD} | TP: {TAKE_PROFIT:.0%} | SL: {STOP_LOSS:.0%}")

    api = get_alpaca_client(paper=True)
    if not api:
        log.error("No se pudo conectar a Alpaca. Verificá las API keys.")
        return

    while True:
        try:
            # Solo operar en horario de mercado
            clock = api.get_clock()
            if not clock.is_open:
                next_open = clock.next_open
                log.info(f"Mercado cerrado. Próxima apertura: {next_open}")
                time.sleep(60 * 30)
                continue

            run_trading_cycle(api, paper=True)

        except KeyboardInterrupt:
            log.info("\n🛑 Bot detenido.")
            break
        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)

        log.info(f"\n⏰ Próxima verificación en {check_interval_minutes} min...")
        time.sleep(check_interval_minutes * 60)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"

    if mode == "backtest":
        period = sys.argv[2] if len(sys.argv) > 2 else "6mo"
        run_backtest(BASKET, period=period)

    elif mode == "paper":
        if not ALPACA_KEY or not ALPACA_SECRET:
            print("\nConfigurá en .env:")
            print("  ALPACA_API_KEY=tu_key")
            print("  ALPACA_SECRET_KEY=tu_secret")
            print("  ALPACA_BASE_URL=https://paper-api.alpaca.markets")
        else:
            run_paper()

    elif mode == "live":
        confirm = input("⚠️  MODO REAL — ¿Estás seguro? (escribe 'SI' para confirmar): ")
        if confirm == "SI":
            api = get_alpaca_client(paper=False)
            if api:
                run_trading_cycle(api, paper=False)
        else:
            print("Cancelado.")

    else:
        print(f"Modo desconocido: {mode}")
        print("Uso: python rsi_bot.py [backtest|paper|live]")
