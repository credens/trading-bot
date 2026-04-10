"""
Daily Report — Resumen diario + snapshots para análisis semanal
================================================================
Lee el estado de scalping + altcoins, filtra trades del día,
guarda snapshot diario, y envía resumen por email + Telegram.

Puede ejecutarse como:
  python3 daily_report.py              # envía reporte ahora
  python3 daily_report.py --loop       # corre daemon, envía a las 23:59
  python3 daily_report.py --weekly     # genera análisis semanal
"""

import json
import sys
import time
import logging
import math
from datetime import datetime, date, timedelta
from pathlib import Path
from notifications import send_telegram, send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s [REPORT] %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SCALPING_STATE = BASE_DIR / "paper_trading" / "scalping_state.json"
ALTCOIN_STATE = BASE_DIR / "altcoin_data" / "state.json"
SNAPSHOTS_DIR = BASE_DIR / "analytics"


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


def save_daily_snapshot():
    """Guarda snapshot del día para análisis posterior."""
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    today = date.today().isoformat()

    sc_state = load_state(SCALPING_STATE)
    alt_state = load_state(ALTCOIN_STATE)

    sc_trades = get_todays_trades(sc_state, "scalping")
    alt_trades = get_todays_trades(alt_state, "altcoin")
    all_trades = sc_trades + alt_trades

    def bot_stats(trades, state, bot_name):
        pnls = [(t.get("pnl", 0) or 0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        capital = state.get("current_capital") or state.get("capital", 0)
        initial = state.get("initial_capital", 500 if bot_name == "altcoin" else 1000)
        peak = state.get("peak_capital", capital)
        dd_from_peak = (peak - capital) / peak * 100 if peak > 0 else 0

        # Duración promedio de trades (minutos)
        durations = []
        for t in trades:
            try:
                entry_t = datetime.fromisoformat(t["entry_time"])
                exit_t = datetime.fromisoformat(t["exit_time"])
                durations.append((exit_t - entry_t).total_seconds() / 60)
            except Exception:
                pass

        # Exit reasons breakdown
        reasons = {}
        for t in trades:
            r = t.get("exit_reason", "UNKNOWN")
            reasons[r] = reasons.get(r, 0) + 1

        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "pnl": round(sum(pnls), 2),
            "pnl_total": round(state.get("total_pnl", 0), 2),
            "best_trade": round(max(pnls), 2) if pnls else 0,
            "worst_trade": round(min(pnls), 2) if pnls else 0,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else 999,
            "capital": round(capital, 2),
            "initial_capital": initial,
            "peak_capital": round(peak, 2),
            "drawdown_from_peak": round(dd_from_peak, 2),
            "avg_duration_min": round(sum(durations) / len(durations), 1) if durations else 0,
            "exit_reasons": reasons,
        }

    snapshot = {
        "date": today,
        "scalping": bot_stats(sc_trades, sc_state, "scalping"),
        "altcoin": bot_stats(alt_trades, alt_state, "altcoin"),
        "combined": {
            "trades": len(all_trades),
            "pnl": round(sum((t.get("pnl", 0) or 0) for t in all_trades), 2),
            "capital": round(
                (sc_state.get("current_capital", 0) or 0) +
                (alt_state.get("capital") or alt_state.get("current_capital", 0) or 0), 2
            ),
        },
    }

    snap_file = SNAPSHOTS_DIR / f"{today}.json"
    snap_file.write_text(json.dumps(snapshot, indent=2))
    log.info(f"Snapshot guardado: {snap_file}")
    return snapshot


def load_snapshots(days=7) -> list:
    """Carga los últimos N días de snapshots."""
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    snapshots = []
    for i in range(days):
        d = (date.today() - timedelta(days=i)).isoformat()
        f = SNAPSHOTS_DIR / f"{d}.json"
        if f.exists():
            try:
                snapshots.append(json.loads(f.read_text()))
            except Exception:
                pass
    return list(reversed(snapshots))  # cronológico


def generate_weekly_analysis():
    """Genera análisis semanal a partir de los snapshots diarios."""
    snaps = load_snapshots(7)
    if not snaps:
        msg = "No hay snapshots para analizar. Esperá al menos 1 día."
        send_telegram(msg)
        log.info(msg)
        return

    days = len(snaps)
    log.info(f"Generando análisis semanal con {days} días de datos...")

    # Agregar métricas
    for bot in ["scalping", "altcoin"]:
        total_trades = sum(s[bot]["trades"] for s in snaps)
        total_pnl = sum(s[bot]["pnl"] for s in snaps)
        total_wins = sum(s[bot]["wins"] for s in snaps)
        total_losses = sum(s[bot]["losses"] for s in snaps)
        daily_pnls = [s[bot]["pnl"] for s in snaps]
        capitals = [s[bot]["capital"] for s in snaps]
        win_rate = round(total_wins / total_trades * 100, 1) if total_trades else 0

        # Profit factor
        all_wins_sum = sum(s[bot]["avg_win"] * s[bot]["wins"] for s in snaps)
        all_losses_sum = abs(sum(s[bot]["avg_loss"] * s[bot]["losses"] for s in snaps))
        profit_factor = round(all_wins_sum / all_losses_sum, 2) if all_losses_sum > 0 else 999

        # Sharpe ratio (diario, anualizado)
        if len(daily_pnls) >= 2:
            avg_daily = sum(daily_pnls) / len(daily_pnls)
            std_daily = math.sqrt(sum((p - avg_daily) ** 2 for p in daily_pnls) / (len(daily_pnls) - 1))
            sharpe = round(avg_daily / std_daily * math.sqrt(365), 2) if std_daily > 0 else 0
        else:
            sharpe = 0
            avg_daily = daily_pnls[0] if daily_pnls else 0

        # Max drawdown en el periodo
        max_dd = max(s[bot]["drawdown_from_peak"] for s in snaps) if snaps else 0

        # Días positivos vs negativos
        green_days = sum(1 for p in daily_pnls if p > 0)
        red_days = sum(1 for p in daily_pnls if p <= 0)

        # ROI
        initial = snaps[0][bot].get("initial_capital", 500)
        roi = round(total_pnl / initial * 100, 1) if initial else 0

        # Consistencia: desviación estándar del P&L diario
        avg_trade_duration = sum(s[bot]["avg_duration_min"] for s in snaps) / days if days else 0

        # Guardar en variable para formato
        if bot == "scalping":
            sc = {
                "trades": total_trades, "pnl": total_pnl, "win_rate": win_rate,
                "profit_factor": profit_factor, "sharpe": sharpe, "max_dd": max_dd,
                "green_days": green_days, "red_days": red_days, "roi": roi,
                "avg_daily": avg_daily, "avg_duration": avg_trade_duration,
                "capital_start": snaps[0][bot]["capital"], "capital_end": snaps[-1][bot]["capital"],
                "daily_pnls": daily_pnls,
            }
        else:
            alt = {
                "trades": total_trades, "pnl": total_pnl, "win_rate": win_rate,
                "profit_factor": profit_factor, "sharpe": sharpe, "max_dd": max_dd,
                "green_days": green_days, "red_days": red_days, "roi": roi,
                "avg_daily": avg_daily, "avg_duration": avg_trade_duration,
                "capital_start": snaps[0][bot]["capital"], "capital_end": snaps[-1][bot]["capital"],
                "daily_pnls": daily_pnls,
            }

    total_pnl = sc["pnl"] + alt["pnl"]
    total_trades = sc["trades"] + alt["trades"]

    # ── Veredicto: ¿listo para real? ──
    checks = []
    checks.append(("Win Rate > 50%", sc["win_rate"] > 50 and alt["win_rate"] > 50))
    checks.append(("Profit Factor > 1.5", sc["profit_factor"] > 1.5 and alt["profit_factor"] > 1.5))
    checks.append(("Sharpe > 1.0", (sc["sharpe"] > 1.0 or sc["trades"] == 0) and (alt["sharpe"] > 1.0 or alt["trades"] == 0)))
    checks.append(("Max DD < 15%", sc["max_dd"] < 15 and alt["max_dd"] < 15))
    checks.append((f"Días verdes >= {days//2+1}/{days}", (sc["green_days"] + alt["green_days"]) / 2 >= days / 2))
    checks.append((f"Min {days * 10} trades/semana", total_trades >= days * 10))
    passed = sum(1 for _, ok in checks if ok)

    if passed == len(checks):
        verdict = "🟢 LISTO PARA REAL — Todos los criterios superados"
    elif passed >= len(checks) - 1:
        verdict = "🟡 CASI LISTO — Revisar criterios fallidos"
    else:
        verdict = "🔴 NO RECOMENDADO — Seguir en paper"

    # ── Telegram ──
    def spark(pnls):
        if not pnls: return ""
        return " ".join("🟢" if p > 0 else "🔴" for p in pnls)

    period = f"{snaps[0]['date']} → {snaps[-1]['date']}"
    tg = [f"📈 <b>ANÁLISIS SEMANAL</b>", f"<i>{period} ({days} días)</i>\n"]

    for name, s in [("SCALPING", sc), ("ALTCOINS", alt)]:
        tg.append(f"<b>{name}</b>")
        tg.append(f"  Trades: {s['trades']} | WR: {s['win_rate']}%")
        tg.append(f"  P&amp;L: {'+'if s['pnl']>=0 else ''}${s['pnl']:.2f} | ROI: {s['roi']}%")
        tg.append(f"  PF: {s['profit_factor']} | Sharpe: {s['sharpe']}")
        tg.append(f"  Max DD: {s['max_dd']:.1f}% | Avg trade: {s['avg_duration']:.0f}min")
        tg.append(f"  {spark(s['daily_pnls'])}\n")

    tg.append(f"<b>TOTAL: {'+'if total_pnl>=0 else ''}${total_pnl:.2f} | {total_trades} trades</b>\n")

    tg.append("<b>── CHECKLIST PARA REAL ──</b>")
    for label, ok in checks:
        tg.append(f"  {'✅' if ok else '❌'} {label}")
    tg.append(f"\n<b>{verdict}</b>")

    send_telegram("\n".join(tg))

    # ── Email HTML ──
    rows_html = ""
    for s in snaps:
        d = s["date"]
        sc_pnl = s["scalping"]["pnl"]
        alt_pnl = s["altcoin"]["pnl"]
        total = sc_pnl + alt_pnl
        col = "#00cc66" if total >= 0 else "#ff4444"
        rows_html += f"""<tr style="border-bottom:1px solid #333;">
            <td style="padding:6px 10px; color:#ccc;">{d}</td>
            <td style="padding:6px 10px; color:{'#00cc66' if sc_pnl>=0 else '#ff4444'};">{'+'if sc_pnl>=0 else ''}${sc_pnl:.2f}</td>
            <td style="padding:6px 10px; color:{'#00cc66' if alt_pnl>=0 else '#ff4444'};">{'+'if alt_pnl>=0 else ''}${alt_pnl:.2f}</td>
            <td style="padding:6px 10px; color:{col}; font-weight:bold;">{'+'if total>=0 else ''}${total:.2f}</td>
            <td style="padding:6px 10px; color:#bbb;">{s['scalping']['trades']+s['altcoin']['trades']}</td>
            <td style="padding:6px 10px; color:#bbb;">{s['combined']['capital']:.0f}</td>
        </tr>"""

    checks_html = "".join(
        f'<div style="padding:4px 0; color:{"#00cc66" if ok else "#ff4444"};">{"✅" if ok else "❌"} {label}</div>'
        for label, ok in checks
    )
    verdict_color = "#00cc66" if passed == len(checks) else "#ffcc00" if passed >= len(checks) - 1 else "#ff4444"

    html = f"""
    <div style="font-family:'Courier New',monospace; background:#0a0a1a; color:#ccc; padding:24px; border-radius:12px; max-width:800px;">
        <h2 style="color:#00ff88; margin:0 0 4px;">Trading Bot HQ — Análisis Semanal</h2>
        <p style="color:#888; margin:0 0 20px;">{period} ({days} días)</p>

        <div style="display:flex; gap:20px; margin-bottom:20px; flex-wrap:wrap;">
            <div style="background:#111; padding:12px 18px; border-radius:8px; border:1px solid #222;">
                <div style="color:#888; font-size:11px;">P&amp;L SEMANA</div>
                <div style="color:{'#00cc66' if total_pnl>=0 else '#ff4444'}; font-size:20px; font-weight:bold;">{'+'if total_pnl>=0 else ''}${total_pnl:.2f}</div>
            </div>
            <div style="background:#111; padding:12px 18px; border-radius:8px; border:1px solid #222;">
                <div style="color:#888; font-size:11px;">TRADES</div>
                <div style="color:#ccc; font-size:20px; font-weight:bold;">{total_trades}</div>
            </div>
            <div style="background:#111; padding:12px 18px; border-radius:8px; border:1px solid #222;">
                <div style="color:#888; font-size:11px;">SCALPING WR</div>
                <div style="color:#ccc; font-size:20px; font-weight:bold;">{sc['win_rate']}%</div>
            </div>
            <div style="background:#111; padding:12px 18px; border-radius:8px; border:1px solid #222;">
                <div style="color:#888; font-size:11px;">ALTCOIN WR</div>
                <div style="color:#ccc; font-size:20px; font-weight:bold;">{alt['win_rate']}%</div>
            </div>
        </div>

        <table style="width:100%; border-collapse:collapse; background:#111; border-radius:8px; overflow:hidden; margin-bottom:20px;">
            <thead>
                <tr style="background:#1a1a2e; color:#888; font-size:11px; text-transform:uppercase;">
                    <th style="padding:8px; text-align:left;">Fecha</th>
                    <th style="padding:8px; text-align:left;">Scalping</th>
                    <th style="padding:8px; text-align:left;">Altcoins</th>
                    <th style="padding:8px; text-align:left;">Total</th>
                    <th style="padding:8px; text-align:left;">Trades</th>
                    <th style="padding:8px; text-align:left;">Capital</th>
                </tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table>

        <div style="background:#111; padding:16px; border-radius:8px; border:1px solid #222; margin-bottom:20px;">
            <h3 style="color:#ffcc00; margin:0 0 10px;">Checklist para dinero real</h3>
            {checks_html}
            <div style="margin-top:12px; padding:10px; background:#0a0a1a; border-radius:6px; border:1px solid {verdict_color};">
                <span style="color:{verdict_color}; font-weight:bold; font-size:14px;">{verdict}</span>
            </div>
        </div>

        <div style="display:flex; gap:20px; flex-wrap:wrap;">
            <div style="background:#111; padding:12px; border-radius:8px; border:1px solid #222; flex:1;">
                <div style="color:#ff9933; font-weight:bold; margin-bottom:8px;">SCALPING</div>
                <div style="font-size:12px; line-height:1.8;">
                    PF: {sc['profit_factor']} | Sharpe: {sc['sharpe']}<br>
                    Max DD: {sc['max_dd']:.1f}% | Avg: {sc['avg_duration']:.0f}min<br>
                    Días: {sc['green_days']}🟢 {sc['red_days']}🔴
                </div>
            </div>
            <div style="background:#111; padding:12px; border-radius:8px; border:1px solid #222; flex:1;">
                <div style="color:#cc88ff; font-weight:bold; margin-bottom:8px;">ALTCOINS</div>
                <div style="font-size:12px; line-height:1.8;">
                    PF: {alt['profit_factor']} | Sharpe: {alt['sharpe']}<br>
                    Max DD: {alt['max_dd']:.1f}% | Avg: {alt['avg_duration']:.0f}min<br>
                    Días: {alt['green_days']}🟢 {alt['red_days']}🔴
                </div>
            </div>
        </div>
    </div>
    """

    subject = f"Trading Bot HQ — Análisis Semanal | {'+'if total_pnl>=0 else ''}${total_pnl:.2f} | {verdict.split('—')[0].strip()}"
    send_email(subject, html)
    log.info(f"Análisis semanal enviado — {days} días, {total_trades} trades, P&L: {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")
    log.info(f"  Veredicto: {verdict}")


def run_daemon():
    """Corre como daemon, envía reporte a las 23:59 y análisis semanal los domingos."""
    log.info("Daily report daemon iniciado — reporte 23:59, análisis semanal domingos")
    last_sent = None
    last_weekly = None
    while True:
        now = datetime.now()
        if now.hour == 23 and now.minute == 59 and last_sent != now.date():
            generate_report()
            save_daily_snapshot()
            last_sent = now.date()
            # Análisis semanal los domingos
            if now.weekday() == 6 and last_weekly != now.date():
                generate_weekly_analysis()
                last_weekly = now.date()
        time.sleep(30)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        run_daemon()
    elif "--weekly" in sys.argv:
        generate_weekly_analysis()
    elif "--snapshot" in sys.argv:
        save_daily_snapshot()
    else:
        generate_report()
