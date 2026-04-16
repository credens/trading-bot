"""
Telegram Commander
==================
Escucha comandos de Telegram y responde con datos de los bots en tiempo real.

Comandos:
  /status     — resumen rápido de los 3 bots
  /positions  — posiciones abiertas en los 3 bots
  /trades     — trades de hoy (todos los bots)
  /scalping   — detalle del bot Scalping BTC
  /altcoin    — detalle del bot Altcoin
  /altscalp   — detalle del bot AltScalp HFT
  /report     — reporte completo (igual al diario)
  /help       — lista de comandos

Uso:
  python3 telegram_commander.py
"""

import json
import time
import logging
from datetime import datetime, date
from pathlib import Path

import requests
from dotenv import load_dotenv
import os

load_dotenv()
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [TG] %(message)s")

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

BASE_DIR        = Path(__file__).parent
SCALPING_STATE  = BASE_DIR / "paper_trading" / "scalping_state.json"
ALTCOIN_STATE   = BASE_DIR / "altcoin_data"  / "state.json"
ALTSCALP_STATE  = BASE_DIR / "paper_trading" / "altscalp_state.json"

POLL_TIMEOUT = 30   # long-polling seconds
API          = f"https://api.telegram.org/bot{TOKEN}"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def send(text: str, chat_id: str = CHAT_ID):
    try:
        requests.post(f"{API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception as e:
        log.warning(f"send error: {e}")


def pnl_fmt(v):
    return f"{'+'if v>=0 else ''}{v:.2f}"


def today_trades(state: dict) -> list:
    today = date.today().isoformat()
    trades = state.get("all_closed_trades") or state.get("closed_trades") or []
    return [t for t in trades if (t.get("exit_time") or "").startswith(today)]


def trade_line(t: dict) -> str:
    sym    = t.get("symbol", "?")
    side   = t.get("side") or t.get("direction", "?")
    pnl    = t.get("pnl", 0) or 0
    lev    = t.get("leverage", "")
    reason = t.get("exit_reason", "")
    hr     = (t.get("exit_time") or "")[11:16]
    em     = "🟢" if pnl >= 0 else "🔴"
    return f"  {em} {hr} {sym} {side}{f' {lev}x' if lev else ''} {reason} {pnl_fmt(pnl)}$"


# ─── Comando handlers ──────────────────────────────────────────────────────────

def cmd_status(chat_id):
    sc  = load(SCALPING_STATE)
    alt = load(ALTCOIN_STATE)
    als = load(ALTSCALP_STATE)

    def row(label, s, open_key="open_positions"):
        cap  = s.get("current_capital") or s.get("capital") or 0
        pnl  = s.get("total_pnl", 0)
        wr   = s.get("win_rate", 0)
        ops  = s.get(open_key) or s.get("positions") or []
        n_op = len(ops) if isinstance(ops, list) else len(ops)
        td   = today_trades(s)
        td_pnl = sum(t.get("pnl", 0) or 0 for t in td)
        return (f"<b>{label}</b>\n"
                f"  Capital: ${cap:.0f}  |  WR: {wr:.0f}%\n"
                f"  P&L total: {pnl_fmt(pnl)}$  |  Hoy: {pnl_fmt(td_pnl)}$\n"
                f"  Abiertas: {n_op}  |  Trades hoy: {len(td)}")

    lines = ["📊 <b>STATUS — Trading Bot HQ</b>", ""]
    lines.append(row("⚡ SCALPING BTC",   sc,  "open_positions"))
    lines.append("")
    lines.append(row("🌐 ALTCOIN",        alt, "open_positions"))
    lines.append("")
    lines.append(row("🔥 ALTSCALP HFT",   als, "positions"))

    total_cap = ((sc.get("current_capital") or 0) +
                 (alt.get("capital") or alt.get("current_capital") or 0) +
                 (als.get("current_capital") or 0))
    total_pnl = (sc.get("total_pnl", 0) + alt.get("total_pnl", 0) + als.get("total_pnl", 0))
    lines.append(f"\n💰 <b>Total capital: ${total_cap:.0f}  |  P&L: {pnl_fmt(total_pnl)}$</b>")
    send("\n".join(lines), chat_id)


def cmd_positions(chat_id):
    sc  = load(SCALPING_STATE)
    alt = load(ALTCOIN_STATE)
    als = load(ALTSCALP_STATE)

    lines = ["📌 <b>POSICIONES ABIERTAS</b>", ""]

    # Scalping BTC
    sc_pos = sc.get("open_positions") or []
    lines.append("<b>— SCALPING BTC —</b>")
    if sc_pos:
        for p in sc_pos:
            sym  = p.get("symbol", "BTCUSDT")
            side = p.get("direction") or p.get("side", "?")
            ep   = p.get("entry_price", 0)
            sl   = p.get("stop_loss", 0)
            tp   = p.get("take_profit", 0)
            sz   = p.get("size_usdt") or p.get("size", 0)
            lev  = p.get("leverage", 1)
            lines.append(f"  {sym} {side} {lev}x | ${sz:.0f}")
            lines.append(f"  entrada ${ep:.2f}  SL ${sl:.2f}  TP ${tp:.2f}")
    else:
        lines.append("  Sin posiciones")

    # Altcoin
    alt_pos = alt.get("open_positions") or []
    lines.append("\n<b>— ALTCOIN —</b>")
    if alt_pos:
        for p in alt_pos:
            sym  = p.get("symbol", "?")
            side = p.get("direction") or p.get("side", "?")
            ep   = p.get("entry_price", 0)
            sz   = p.get("size_usdt") or p.get("size", 0)
            lev  = p.get("leverage", 1)
            strat = p.get("strategy", "")
            lines.append(f"  {sym} {side} {lev}x {strat} | ${sz:.0f} @ ${ep:.4f}")
    else:
        lines.append("  Sin posiciones")

    # AltScalp
    as_pos = list((als.get("positions") or {}).values())
    lines.append("\n<b>— ALTSCALP HFT —</b>")
    if as_pos:
        for p in as_pos:
            sym  = p.get("symbol", "?")
            side = p.get("direction", "?")
            ep   = p.get("entry_price", 0)
            sl   = p.get("stop_loss", 0)
            tp   = p.get("take_profit", 0)
            sz   = p.get("size_usdt", 0)
            lev  = p.get("leverage", 1)
            sc_v = p.get("score", "")
            lines.append(f"  {sym} {side} {lev}x sc:{sc_v} | ${sz:.0f} @ ${ep:.4f}")
            lines.append(f"  SL ${sl:.4f}  TP ${tp:.4f}")
    else:
        lines.append("  Sin posiciones")

    send("\n".join(lines), chat_id)


def cmd_trades(chat_id):
    sc  = load(SCALPING_STATE)
    alt = load(ALTCOIN_STATE)
    als = load(ALTSCALP_STATE)

    sc_t  = today_trades(sc)
    alt_t = today_trades(alt)
    as_t  = today_trades(als)
    all_t = sc_t + alt_t + as_t

    today_str = date.today().strftime("%d/%m/%Y")
    wins  = sum(1 for t in all_t if (t.get("pnl", 0) or 0) > 0)
    total_pnl = sum(t.get("pnl", 0) or 0 for t in all_t)
    wr    = wins / len(all_t) * 100 if all_t else 0

    lines = [f"📋 <b>TRADES HOY — {today_str}</b>",
             f"Total: {len(all_t)} ({wins}W/{len(all_t)-wins}L) WR:{wr:.0f}%  P&L: {pnl_fmt(total_pnl)}$",
             ""]

    def section(label, trades):
        if not trades:
            return [f"<b>{label}</b>: sin trades"]
        td_pnl = sum(t.get("pnl", 0) or 0 for t in trades)
        t_wins = sum(1 for t in trades if (t.get("pnl", 0) or 0) > 0)
        rows   = [f"<b>{label}</b> ({len(trades)} trades, {pnl_fmt(td_pnl)}$, WR:{t_wins/len(trades)*100:.0f}%)"]
        for t in trades:
            rows.append(trade_line(t))
        return rows

    lines += section("SCALPING BTC", sc_t)
    lines.append("")
    lines += section("ALTCOIN", alt_t)
    lines.append("")
    lines += section("ALTSCALP HFT", as_t)

    # Telegram tiene límite de 4096 chars; partir si hace falta
    msg = "\n".join(lines)
    if len(msg) > 4000:
        send(msg[:4000] + "\n... (continúa)", chat_id)
        send(msg[4000:], chat_id)
    else:
        send(msg, chat_id)


def _bot_detail(label, s, open_key="open_positions"):
    cap    = s.get("current_capital") or s.get("capital") or 0
    init   = s.get("initial_capital", 200)
    pnl    = s.get("total_pnl", 0)
    wr     = s.get("win_rate", 0)
    dd     = s.get("max_drawdown", 0)
    total  = s.get("total_trades", 0) or len(s.get("closed_trades") or [])
    td     = today_trades(s)
    td_pnl = sum(t.get("pnl", 0) or 0 for t in td)
    td_wins= sum(1 for t in td if (t.get("pnl", 0) or 0) > 0)
    td_wr  = td_wins / len(td) * 100 if td else 0

    ops  = s.get(open_key) or s.get("positions") or []
    n_op = len(ops) if isinstance(ops, list) else len(ops)

    lines = [
        f"<b>{label}</b>",
        f"Capital: ${cap:.2f} (inicial ${init:.0f})",
        f"P&L total: {pnl_fmt(pnl)}$  |  Max DD: {dd:.1f}%",
        f"Win Rate: {wr:.0f}%  |  Trades total: {total}",
        f"Abiertas: {n_op}",
        f"",
        f"<b>Hoy:</b> {len(td)} trades ({td_wins}W/{len(td)-td_wins}L) WR:{td_wr:.0f}%  {pnl_fmt(td_pnl)}$",
    ]
    for t in td:
        lines.append(trade_line(t))
    return lines


def cmd_scalping(chat_id):
    lines = ["⚡ <b>SCALPING BTC — Detalle</b>", ""] + _bot_detail("", load(SCALPING_STATE), "open_positions")
    send("\n".join(lines), chat_id)


def cmd_altcoin(chat_id):
    lines = ["🌐 <b>ALTCOIN — Detalle</b>", ""] + _bot_detail("", load(ALTCOIN_STATE), "open_positions")
    send("\n".join(lines), chat_id)


def cmd_altscalp(chat_id):
    lines = ["🔥 <b>ALTSCALP HFT — Detalle</b>", ""] + _bot_detail("", load(ALTSCALP_STATE), "positions")
    send("\n".join(lines), chat_id)


def cmd_report(chat_id):
    send("📊 Generando reporte completo...", chat_id)
    try:
        from daily_report import generate_report
        generate_report()
        send("✅ Reporte enviado por email + Telegram.", chat_id)
    except Exception as e:
        send(f"❌ Error generando reporte: {e}", chat_id)


def cmd_help(chat_id):
    send(
        "🤖 <b>Trading Bot HQ — Comandos</b>\n\n"
        "/status    — resumen rápido de los 3 bots\n"
        "/positions — posiciones abiertas\n"
        "/trades    — todos los trades de hoy\n"
        "/scalping  — detalle Scalping BTC\n"
        "/altcoin   — detalle Altcoin\n"
        "/altscalp  — detalle AltScalp HFT\n"
        "/report    — reporte completo (email + Telegram)\n"
        "/help      — esta ayuda",
        chat_id
    )


COMMANDS = {
    "/status":    cmd_status,
    "/positions": cmd_positions,
    "/trades":    cmd_trades,
    "/scalping":  cmd_scalping,
    "/altcoin":   cmd_altcoin,
    "/altscalp":  cmd_altscalp,
    "/report":    cmd_report,
    "/help":      cmd_help,
}


# ─── Poll loop ────────────────────────────────────────────────────────────────

def run():
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN no configurado")
        return

    log.info("Telegram Commander iniciado — escuchando comandos")
    send("🤖 <b>Trading Bot HQ online</b>\nEnviá /help para ver los comandos disponibles.")

    offset = None
    while True:
        try:
            params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            resp = requests.get(f"{API}/getUpdates", params=params, timeout=POLL_TIMEOUT + 5)
            updates = resp.json().get("result", [])

            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip().lower().split()[0]  # primer palabra

                if text in COMMANDS:
                    log.info(f"Comando recibido: {text} de chat {chat_id}")
                    try:
                        COMMANDS[text](chat_id)
                    except Exception as e:
                        log.error(f"Error en {text}: {e}")
                        send(f"❌ Error: {e}", chat_id)

        except requests.exceptions.Timeout:
            pass  # normal en long-polling
        except Exception as e:
            log.warning(f"Poll error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    run()
