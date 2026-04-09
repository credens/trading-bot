"""
Drawdown Monitor
=================
Monitorea drawdown de cada bot y:
- 25%: alerta por Telegram + Email (una vez)
- 50%: alerta CRÍTICA + pausa automática del bot
"""

import json
import logging
from pathlib import Path
from notifications import send_alert

log = logging.getLogger(__name__)

# Tracker para no enviar alertas repetidas
_alerted = {}  # key: "bot_level" → True


def check_drawdown(bot_name: str, current_capital: float, initial_capital: float,
                   peak_capital: float, state_file: Path = None):
    """
    Verifica drawdown y envía alertas si corresponde.
    Retorna True si el bot debe pausarse (drawdown >= 50%).
    """
    if peak_capital <= 0 or initial_capital <= 0:
        return False

    # Drawdown desde el peak
    dd_from_peak = (peak_capital - current_capital) / peak_capital * 100
    # Drawdown desde el capital inicial
    dd_from_initial = (initial_capital - current_capital) / initial_capital * 100

    # Usar el mayor de los dos
    drawdown = max(dd_from_peak, dd_from_initial)

    if drawdown < 25:
        # Reset alerts si se recupera
        _alerted.pop(f"{bot_name}_25", None)
        _alerted.pop(f"{bot_name}_50", None)
        return False

    # ── 50% Drawdown: CRITICAL + PAUSE ──────────────────────────────────────
    if drawdown >= 50 and f"{bot_name}_50" not in _alerted:
        _alerted[f"{bot_name}_50"] = True
        msg = (
            f"Bot: <b>{bot_name.upper()}</b>\n"
            f"Drawdown: <b>{drawdown:.1f}%</b>\n"
            f"Capital: ${current_capital:.2f} (inicio: ${initial_capital:.2f})\n"
            f"Peak: ${peak_capital:.2f}\n\n"
            f"<b>BOT PAUSADO AUTOMÁTICAMENTE</b>"
        )
        send_alert(f"DRAWDOWN CRÍTICO — {bot_name.upper()}", msg, level="CRITICAL")
        log.warning(f"🚨 DRAWDOWN CRÍTICO {bot_name}: {drawdown:.1f}% — PAUSANDO BOT")

        # Escribir pausa en state file
        if state_file and state_file.exists():
            try:
                raw = json.loads(state_file.read_text())
                raw["paused"] = True
                raw["pause_reason"] = f"Drawdown {drawdown:.1f}% >= 50%"
                state_file.write_text(json.dumps(raw, indent=2))
            except Exception as e:
                log.warning(f"Error escribiendo pausa en {state_file}: {e}")

        return True

    # ── 25% Drawdown: WARNING ───────────────────────────────────────────────
    if drawdown >= 25 and f"{bot_name}_25" not in _alerted:
        _alerted[f"{bot_name}_25"] = True
        msg = (
            f"Bot: <b>{bot_name.upper()}</b>\n"
            f"Drawdown: <b>{drawdown:.1f}%</b>\n"
            f"Capital: ${current_capital:.2f} (inicio: ${initial_capital:.2f})\n"
            f"Peak: ${peak_capital:.2f}\n\n"
            f"El bot sigue operando. Se pausará automáticamente si llega al 50%."
        )
        send_alert(f"Drawdown alto — {bot_name.upper()}", msg, level="WARNING")
        log.warning(f"⚠️ DRAWDOWN WARNING {bot_name}: {drawdown:.1f}%")

    return False


def is_paused(state_file: Path) -> bool:
    """Verifica si un bot está pausado por drawdown."""
    try:
        if state_file.exists():
            raw = json.loads(state_file.read_text())
            return raw.get("paused", False)
    except Exception:
        pass
    return False
