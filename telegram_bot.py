"""
Telegram Bot — Comandos interactivos
=====================================
Bot de Telegram para monitorear y controlar los trading bots.

Comandos:
  /status     — Estado general de los bots
  /positions  — Posiciones abiertas con P&L actual
  /pnl        — P&L hoy y total por bot
  /report     — Generar reporte diario on-demand
  /pause      — Pausar bot (ej: /pause altcoin)
  /resume     — Reanudar bot (ej: /resume altcoin)
  /closeall   — Cerrar todas las posiciones (requiere confirmación)
  /help       — Mostrar ayuda

Uso:
  python3 telegram_bot.py
"""

import os
import json
import time
import logging
from datetime import datetime, date
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [TG-BOT] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BASE_DIR = Path(__file__).parent
SCALPING_STATE = BASE_DIR / "paper_trading" / "scalping_state.json"
ALTCOIN_STATE = BASE_DIR / "altcoin_data" / "state.json"

BOTS = {
    "scalping": {"name": "Scalping BTC", "state": SCALPING_STATE},
    "altcoin":  {"name": "Altcoins",     "state": ALTCOIN_STATE},
}

# Confirmación pendiente para /closeall
_pending_closeall = {}  # {chat_id: timestamp}


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def send_reply(chat_id, text, parse_mode="HTML"):
    """Envía respuesta por Telegram, chunkeando si excede 4096 chars."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        try:
            requests.post(url, json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": parse_mode,
            }, timeout=10)
        except Exception as e:
            log.warning(f"Error enviando reply: {e}")


def get_binance_price(symbol: str) -> float:
    """Obtiene precio actual de Binance Futures."""
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params={"symbol": symbol}, timeout=5
        )
        return float(r.json()["price"])
    except Exception:
        return 0.0


# ─── Comandos ────────────────────────────────────────────────────────────────

def cmd_help(chat_id, args):
    send_reply(chat_id, """<b>Trading Bot HQ — Comandos</b>

/status — Estado general
/positions — Posiciones abiertas
/pnl — P&amp;L hoy y total
/report — Reporte diario ahora
/weekly — Análisis semanal
/pause [bot] — Pausar (scalping/altcoin/all)
/resume [bot] — Reanudar
/closeall — Cerrar todo (requiere confirm)
/help — Esta ayuda""")


def cmd_status(chat_id, args):
    lines = ["<b>── STATUS ──</b>\n"]
    for key, cfg in BOTS.items():
        s = load_state(cfg["state"])
        capital = s.get("current_capital") or s.get("capital", 0)
        pnl = s.get("total_pnl", 0)
        paused = s.get("paused", False)

        if key == "scalping":
            positions = s.get("open_trades", [])
            n_pos = len(positions) if isinstance(positions, list) else (1 if positions else 0)
        else:
            positions = s.get("positions", {})
            n_pos = len(positions)

        status = "⛔ PAUSADO" if paused else "🟢 OPERANDO"
        pnl_sign = "+" if pnl >= 0 else ""

        lines.append(f"<b>{cfg['name']}</b>")
        lines.append(f"  Capital: <b>${capital:,.2f}</b>")
        lines.append(f"  P&amp;L: <b>{pnl_sign}${pnl:,.2f}</b>")
        lines.append(f"  Posiciones: {n_pos}")
        lines.append(f"  Estado: {status}\n")

    send_reply(chat_id, "\n".join(lines))


def cmd_positions(chat_id, args):
    lines = ["<b>── POSICIONES ABIERTAS ──</b>\n"]
    total_unrealized = 0

    for key, cfg in BOTS.items():
        s = load_state(cfg["state"])

        if key == "scalping":
            pos_list = s.get("open_trades", [])
            if not pos_list:
                lines.append(f"<b>{cfg['name']}</b>: sin posiciones\n")
                continue
            lines.append(f"<b>{cfg['name']}</b>")
            for p in (pos_list if isinstance(pos_list, list) else [pos_list]):
                symbol = "BTCUSDT"
                side = p.get("side", "?")
                entry = p.get("entry_price", 0)
                size = p.get("size", 0)
                price = get_binance_price(symbol)
                if price and entry:
                    raw = (price - entry) / entry if side == "LONG" else (entry - price) / entry
                    pnl = round(size * raw * p.get("leverage", 10), 2)
                    total_unrealized += pnl
                    pnl_str = f"{'+'if pnl>=0 else ''}${pnl:.2f}"
                    emoji = "🟢" if pnl >= 0 else "🔴"
                else:
                    pnl_str = "?"
                    emoji = "⚪"
                lines.append(f"  {emoji} {symbol} {side} @ ${entry:,.2f} | ${size:.0f} | {pnl_str}")
            lines.append("")

        else:
            positions = s.get("positions", {})
            if not positions:
                lines.append(f"<b>{cfg['name']}</b>: sin posiciones\n")
                continue
            lines.append(f"<b>{cfg['name']}</b> ({len(positions)})")
            for sym, p in positions.items():
                side = p.get("direction", "?")
                entry = p.get("entry_price", 0)
                size = p.get("size_usdt", 0)
                lev = p.get("leverage", 20)
                price = get_binance_price(sym)
                if price and entry:
                    raw = (price - entry) / entry if side == "LONG" else (entry - price) / entry
                    pnl = round(size * raw * lev, 2)
                    total_unrealized += pnl
                    pnl_str = f"{'+'if pnl>=0 else ''}${pnl:.2f}"
                    emoji = "🟢" if pnl >= 0 else "🔴"
                else:
                    pnl_str = "?"
                    emoji = "⚪"
                lines.append(f"  {emoji} {sym} {side} @ ${entry:.4f} | ${size:.0f} | {pnl_str}")
            lines.append("")

    lines.append(f"<b>P&amp;L no realizado: {'+'if total_unrealized>=0 else ''}${total_unrealized:.2f}</b>")
    send_reply(chat_id, "\n".join(lines))


def cmd_pnl(chat_id, args):
    from daily_report import get_todays_trades, load_state as dr_load

    lines = ["<b>── P&amp;L ──</b>\n"]
    total_today = 0
    total_all = 0

    sc_state = dr_load(SCALPING_STATE)
    alt_state = dr_load(ALTCOIN_STATE)

    for key, cfg, state in [("scalping", BOTS["scalping"], sc_state),
                             ("altcoin", BOTS["altcoin"], alt_state)]:
        today_trades = get_todays_trades(state, key)
        pnl_today = sum(t.get("pnl", 0) or 0 for t in today_trades)
        pnl_total = state.get("total_pnl", 0)
        n_today = len(today_trades)
        wins = sum(1 for t in today_trades if (t.get("pnl", 0) or 0) > 0)

        total_today += pnl_today
        total_all += pnl_total

        lines.append(f"<b>{cfg['name']}</b>")
        lines.append(f"  Hoy: {'+'if pnl_today>=0 else ''}${pnl_today:.2f} ({n_today} trades, {wins}W)")
        lines.append(f"  Total: {'+'if pnl_total>=0 else ''}${pnl_total:.2f}\n")

    lines.append(f"<b>TOTAL HOY: {'+'if total_today>=0 else ''}${total_today:.2f}</b>")
    lines.append(f"<b>TOTAL ALL: {'+'if total_all>=0 else ''}${total_all:.2f}</b>")
    send_reply(chat_id, "\n".join(lines))


def cmd_report(chat_id, args):
    send_reply(chat_id, "⏳ Generando reporte...")
    try:
        from daily_report import generate_report
        generate_report()
        send_reply(chat_id, "✅ Reporte enviado por Telegram y Email.")
    except Exception as e:
        send_reply(chat_id, f"❌ Error: {e}")


def cmd_weekly(chat_id, args):
    send_reply(chat_id, "⏳ Generando análisis semanal...")
    try:
        from daily_report import generate_weekly_analysis
        generate_weekly_analysis()
        send_reply(chat_id, "✅ Análisis semanal enviado.")
    except Exception as e:
        send_reply(chat_id, f"❌ Error: {e}")


def cmd_pause(chat_id, args):
    target = args[0].lower() if args else "all"
    targets = list(BOTS.keys()) if target == "all" else [target]

    for key in targets:
        if key not in BOTS:
            send_reply(chat_id, f"❌ Bot '{key}' no existe. Usa: scalping, altcoin, all")
            return
        path = BOTS[key]["state"]
        try:
            s = load_state(path)
            s["paused"] = True
            s["pause_reason"] = "Manual pause via Telegram"
            path.write_text(json.dumps(s, indent=2, default=str))
            send_reply(chat_id, f"⛔ <b>{BOTS[key]['name']}</b> pausado.")
        except Exception as e:
            send_reply(chat_id, f"❌ Error pausando {key}: {e}")


def cmd_resume(chat_id, args):
    target = args[0].lower() if args else "all"
    targets = list(BOTS.keys()) if target == "all" else [target]

    for key in targets:
        if key not in BOTS:
            send_reply(chat_id, f"❌ Bot '{key}' no existe. Usa: scalping, altcoin, all")
            return
        path = BOTS[key]["state"]
        try:
            s = load_state(path)
            s["paused"] = False
            s.pop("pause_reason", None)
            path.write_text(json.dumps(s, indent=2, default=str))
            send_reply(chat_id, f"🟢 <b>{BOTS[key]['name']}</b> reanudado.")
        except Exception as e:
            send_reply(chat_id, f"❌ Error reanudando {key}: {e}")


def cmd_closeall(chat_id, args):
    global _pending_closeall

    # Check confirmation
    if args and args[0].lower() == "confirm":
        if chat_id in _pending_closeall and time.time() - _pending_closeall[chat_id] < 60:
            del _pending_closeall[chat_id]
            closed = []

            # Scalping
            try:
                s = load_state(SCALPING_STATE)
                if s.get("open_trades"):
                    s["manual_close"] = True
                    SCALPING_STATE.write_text(json.dumps(s, indent=2, default=str))
                    closed.append("Scalping: 1 posición")
            except Exception as e:
                closed.append(f"Scalping: error ({e})")

            # Altcoin
            try:
                s = load_state(ALTCOIN_STATE)
                positions = s.get("positions", {})
                if positions:
                    s["manual_close"] = list(positions.keys())
                    ALTCOIN_STATE.write_text(json.dumps(s, indent=2, default=str))
                    closed.append(f"Altcoin: {len(positions)} posiciones")
            except Exception as e:
                closed.append(f"Altcoin: error ({e})")

            msg = "🛑 <b>CLOSEALL ejecutado</b>\n" + "\n".join(closed)
            msg += "\n\nSe cerrarán en el próximo ciclo de cada bot."
            send_reply(chat_id, msg)
            return
        else:
            send_reply(chat_id, "⏰ Confirmación expirada. Enviá /closeall de nuevo.")
            return

    # Count positions
    sc = load_state(SCALPING_STATE)
    alt = load_state(ALTCOIN_STATE)
    sc_n = len(sc.get("open_trades", [])) if isinstance(sc.get("open_trades"), list) else (1 if sc.get("open_trades") else 0)
    alt_n = len(alt.get("positions", {}))

    if sc_n + alt_n == 0:
        send_reply(chat_id, "No hay posiciones abiertas.")
        return

    _pending_closeall[chat_id] = time.time()
    send_reply(chat_id, f"⚠️ <b>¿Cerrar TODAS las posiciones?</b>\n"
               f"  Scalping: {sc_n}\n  Altcoin: {alt_n}\n\n"
               f"Enviá <code>/closeall confirm</code> en 60 segundos para confirmar.")


# ─── Dispatcher ──────────────────────────────────────────────────────────────

COMMANDS = {
    "/help": cmd_help,
    "/start": cmd_help,
    "/status": cmd_status,
    "/positions": cmd_positions,
    "/pos": cmd_positions,
    "/pnl": cmd_pnl,
    "/report": cmd_report,
    "/weekly": cmd_weekly,
    "/pause": cmd_pause,
    "/resume": cmd_resume,
    "/closeall": cmd_closeall,
}


def dispatch(chat_id, text):
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]  # Remove @botname suffix
    args = parts[1:]

    handler = COMMANDS.get(cmd)
    if handler:
        try:
            handler(chat_id, args)
        except Exception as e:
            log.error(f"Error en {cmd}: {e}", exc_info=True)
            send_reply(chat_id, f"❌ Error: {e}")
    else:
        send_reply(chat_id, f"Comando no reconocido. Usá /help")


# ─── Main Loop ───────────────────────────────────────────────────────────────

def main():
    if not TOKEN or not CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID requeridos en .env")
        return

    log.info(f"Telegram bot iniciado — esperando comandos de chat {CHAT_ID}")
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

    # Skip old messages on startup
    try:
        r = requests.get(url, params={"offset": -1, "timeout": 0}, timeout=10)
        updates = r.json().get("result", [])
        offset = updates[-1]["update_id"] + 1 if updates else 0
    except Exception:
        offset = 0

    while True:
        try:
            r = requests.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
            updates = r.json().get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat = msg.get("chat", {})
                text = msg.get("text", "")

                if str(chat.get("id")) != str(CHAT_ID):
                    continue

                if text.startswith("/"):
                    log.info(f"Comando: {text}")
                    dispatch(chat["id"], text)

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log.warning(f"Error en polling: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
