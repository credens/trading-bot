"""
Daily Report — Resumen diario de operaciones
==============================================
Lee el estado de scalping + altcoins, filtra trades del día,
y envía resumen por email (HTML) + Telegram.

Puede ejecutarse como:
  python3 daily_report.py          # envía reporte ahora
  python3 daily_report.py --loop   # corre daemon, envía a las 23:59
"""

import json
import sys
import time
import logging
from datetime import datetime, date
from pathlib import Path
from notifications import send_telegram, send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s [REPORT] %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SCALPING_STATE = BASE_DIR / "paper_trading" / "scalping_state.json"
ALTCOIN_STATE = BASE_DIR / "altcoin_data" / "state.json"


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def get_todays_trades(state: dict, bot_name: str) -> list:
    """Extrae trades cerrados de hoy."""
    today = date.today().isoformat()
    trades = state.get("all_closed_trades") or state.get("closed_trades") or []
    result = []
    for t in trades:
        exit_time = t.get("exit_time", "")
        if exit_time.startswith(today):
            t["_bot"] = bot_name
            result.append(t)
    return result


def format_trade_row(t: dict) -> str:
    """Formatea un trade como fila HTML."""
    symbol = t.get("symbol") or t.get("id", "BTC")
    if symbol.startswith("SC-"):
        symbol = "BTCUSDT"
    side = t.get("side") or t.get("direction", "?")
    entry = t.get("entry_price", 0)
    exit_p = t.get("exit_price", 0)
    size = t.get("size") or t.get("size_usdt", 0)
    pnl = t.get("pnl", 0) or 0
    reason = t.get("exit_reason", "?")
    bot = t.get("_bot", "?")

    pnl_color = "#00cc66" if pnl >= 0 else "#ff4444"
    side_color = "#00cc66" if side == "LONG" else "#ff4444"

    # Format prices based on magnitude
    if entry > 100:
        entry_str = f"${entry:,.2f}"
        exit_str = f"${exit_p:,.2f}"
    else:
        entry_str = f"${entry:.6f}"
        exit_str = f"${exit_p:.6f}"

    return f"""<tr style="border-bottom:1px solid #333;">
        <td style="padding:6px 8px; color:#bbb;">{bot}</td>
        <td style="padding:6px 8px; color:#ccc;">{symbol}</td>
        <td style="padding:6px 8px; color:{side_color}; font-weight:bold;">{side}</td>
        <td style="padding:6px 8px; color:#ccc;">{entry_str}</td>
        <td style="padding:6px 8px; color:#ccc;">{exit_str}</td>
        <td style="padding:6px 8px; color:#ccc;">${size:.0f}</td>
        <td style="padding:6px 8px; color:{pnl_color}; font-weight:bold;">{"+" if pnl>=0 else ""}${pnl:.2f}</td>
        <td style="padding:6px 8px; color:#bbb;">{reason}</td>
    </tr>"""


def generate_report():
    """Genera y envía el reporte diario."""
    log.info("Generando reporte diario...")

    sc_state = load_state(SCALPING_STATE)
    alt_state = load_state(ALTCOIN_STATE)

    sc_trades = get_todays_trades(sc_state, "Scalping")
    alt_trades = get_todays_trades(alt_state, "Altcoin")
    all_trades = sc_trades + alt_trades

    # Stats
    total_pnl_day = sum(t.get("pnl", 0) or 0 for t in all_trades)
    wins = sum(1 for t in all_trades if (t.get("pnl", 0) or 0) > 0)
    losses = sum(1 for t in all_trades if (t.get("pnl", 0) or 0) <= 0)
    wr = (wins / len(all_trades) * 100) if all_trades else 0

    sc_capital = sc_state.get("current_capital", 0)
    alt_capital = alt_state.get("capital") or alt_state.get("current_capital", 0)
    total_capital = sc_capital + alt_capital

    sc_total_pnl = sc_state.get("total_pnl", 0)
    alt_total_pnl = alt_state.get("total_pnl", 0)
    total_pnl_all = sc_total_pnl + alt_total_pnl

    sc_dd = sc_state.get("max_drawdown", 0)
    alt_dd = alt_state.get("max_drawdown", 0)

    today_str = date.today().strftime("%d/%m/%Y")

    # ── Trade rows HTML ────────────────────���────────────────────────��────────
    if all_trades:
        rows = "\n".join(format_trade_row(t) for t in all_trades)
    else:
        rows = '<tr><td colspan="8" style="padding:20px; text-align:center; color:#888;">Sin operaciones hoy</td></tr>'

    pnl_color = "#00cc66" if total_pnl_day >= 0 else "#ff4444"

    html = f"""
    <div style="font-family:'Courier New',monospace; background:#0a0a1a; color:#ccc; padding:24px; border-radius:12px; max-width:800px;">
        <h2 style="color:#00ff88; margin:0 0 4px;">Trading Bot HQ — Resumen Diario</h2>
        <p style="color:#888; margin:0 0 20px;">{today_str}</p>

        <div style="display:flex; gap:20px; margin-bottom:20px; flex-wrap:wrap;">
            <div style="background:#111; padding:12px 18px; border-radius:8px; border:1px solid #222;">
                <div style="color:#888; font-size:11px;">CAPITAL TOTAL</div>
                <div style="color:#ccc; font-size:20px; font-weight:bold;">${total_capital:.0f}</div>
            </div>
            <div style="background:#111; padding:12px 18px; border-radius:8px; border:1px solid #222;">
                <div style="color:#888; font-size:11px;">P&L HOY</div>
                <div style="color:{pnl_color}; font-size:20px; font-weight:bold;">{"+" if total_pnl_day>=0 else ""}${total_pnl_day:.2f}</div>
            </div>
            <div style="background:#111; padding:12px 18px; border-radius:8px; border:1px solid #222;">
                <div style="color:#888; font-size:11px;">P&L TOTAL</div>
                <div style="color:{"#00cc66" if total_pnl_all>=0 else "#ff4444"}; font-size:20px; font-weight:bold;">{"+" if total_pnl_all>=0 else ""}${total_pnl_all:.2f}</div>
            </div>
            <div style="background:#111; padding:12px 18px; border-radius:8px; border:1px solid #222;">
                <div style="color:#888; font-size:11px;">TRADES HOY</div>
                <div style="color:#ccc; font-size:20px; font-weight:bold;">{len(all_trades)} ({wins}W/{losses}L)</div>
            </div>
        </div>

        <div style="display:flex; gap:20px; margin-bottom:20px; flex-wrap:wrap;">
            <div style="background:#111; padding:10px 16px; border-radius:8px; border:1px solid #222;">
                <span style="color:#888; font-size:11px;">SCALPING</span>
                <span style="color:#ff9933; font-weight:bold; margin-left:8px;">${sc_capital:.0f}</span>
                <span style="color:{"#00cc66" if sc_total_pnl>=0 else "#ff4444"}; margin-left:8px;">{"+" if sc_total_pnl>=0 else ""}${sc_total_pnl:.0f}</span>
                <span style="color:#888; margin-left:8px;">DD:{sc_dd:.1f}%</span>
            </div>
            <div style="background:#111; padding:10px 16px; border-radius:8px; border:1px solid #222;">
                <span style="color:#888; font-size:11px;">ALTCOINS</span>
                <span style="color:#cc88ff; font-weight:bold; margin-left:8px;">${alt_capital:.0f}</span>
                <span style="color:{"#00cc66" if alt_total_pnl>=0 else "#ff4444"}; margin-left:8px;">{"+" if alt_total_pnl>=0 else ""}${alt_total_pnl:.0f}</span>
                <span style="color:#888; margin-left:8px;">DD:{alt_dd:.1f}%</span>
            </div>
        </div>

        <table style="width:100%; border-collapse:collapse; background:#111; border-radius:8px; overflow:hidden;">
            <thead>
                <tr style="background:#1a1a2e; color:#888; font-size:11px; text-transform:uppercase; letter-spacing:1px;">
                    <th style="padding:8px; text-align:left;">Bot</th>
                    <th style="padding:8px; text-align:left;">Instrumento</th>
                    <th style="padding:8px; text-align:left;">Lado</th>
                    <th style="padding:8px; text-align:left;">Entrada</th>
                    <th style="padding:8px; text-align:left;">Salida</th>
                    <th style="padding:8px; text-align:left;">Size</th>
                    <th style="padding:8px; text-align:left;">P&L</th>
                    <th style="padding:8px; text-align:left;">Razón</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>

        <p style="color:#555; font-size:10px; margin-top:16px; text-align:center;">
            Trading Bot HQ — Reporte automático · Win Rate hoy: {wr:.0f}%
        </p>
    </div>
    """

    # ── Send email ───────────────────────────���───────────────────────────────
    subject = f"Trading Bot HQ — {today_str} | {'+'if total_pnl_day>=0 else ''}${total_pnl_day:.2f}"
    email_ok = send_email(subject, html)

    # ── Send Telegram summary ────────────────────────────────────────────────
    tg_lines = [f"📊 <b>Resumen {today_str}</b>"]
    tg_lines.append(f"Capital: <b>${total_capital:.0f}</b>")
    tg_lines.append(f"P&L hoy: <b>{'+'if total_pnl_day>=0 else ''}${total_pnl_day:.2f}</b>")
    tg_lines.append(f"P&L total: {'+'if total_pnl_all>=0 else ''}${total_pnl_all:.2f}")
    tg_lines.append(f"Trades: {len(all_trades)} ({wins}W/{losses}L) WR:{wr:.0f}%")
    tg_lines.append("")
    for t in all_trades[:10]:
        sym = t.get("symbol") or "BTCUSDT"
        pnl = t.get("pnl", 0) or 0
        side = t.get("side") or t.get("direction", "?")
        tg_lines.append(f"  {'🟢' if pnl>=0 else '🔴'} {sym} {side} {'+'if pnl>=0 else ''}${pnl:.2f}")
    if len(all_trades) > 10:
        tg_lines.append(f"  ...y {len(all_trades)-10} más")

    tg_ok = send_telegram("\n".join(tg_lines))

    log.info(f"Reporte enviado — Email: {'OK' if email_ok else 'FAIL'} | Telegram: {'OK' if tg_ok else 'FAIL'}")
    log.info(f"  Trades hoy: {len(all_trades)} | P&L: {'+'if total_pnl_day>=0 else ''}${total_pnl_day:.2f}")


def run_daemon():
    """Corre como daemon, envía reporte a las 23:59."""
    log.info("Daily report daemon iniciado — envía a las 23:59")
    last_sent = None
    while True:
        now = datetime.now()
        if now.hour == 23 and now.minute == 59 and last_sent != now.date():
            generate_report()
            last_sent = now.date()
        time.sleep(30)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        run_daemon()
    else:
        generate_report()
