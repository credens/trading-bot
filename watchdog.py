#!/usr/bin/env python3
"""
Watchdog — reinicia bots si llevan más de N minutos sin loggear.
Corre como proceso independiente.
"""
import os
import time
import subprocess
import logging
from pathlib import Path
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "logs" / "watchdog.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

BASE = Path(__file__).parent

BOTS = {
    "scalping_bot": {
        "script": BASE / "scalping_bot.py",
        "log":    BASE / "logs" / "scalping_bot.log",
        "timeout_min": 2,
    },
    "altcoin_bot": {
        "script": BASE / "altcoin_bot.py",
        "log":    BASE / "logs" / "altcoin_bot.log",
        "timeout_min": 3,
    },
}

CHECK_INTERVAL = 60  # segundos entre checks


def get_log_age_minutes(log_path: Path) -> float:
    """Retorna minutos desde la última línea del log."""
    if not log_path.exists():
        return 999
    mtime = log_path.stat().st_mtime
    return (time.time() - mtime) / 60


def is_running(script_name: str) -> bool:
    result = subprocess.run(
        ["pgrep", "-f", script_name],
        capture_output=True, text=True
    )
    return result.returncode == 0


def kill_bot(script_name: str):
    subprocess.run(["pkill", "-f", script_name], capture_output=True)
    time.sleep(2)


def start_bot(script: Path, log_path: Path):
    with open(log_path, "a") as logfile:
        proc = subprocess.Popen(
            ["python3", str(script)],
            stdout=logfile,
            stderr=logfile,
            start_new_session=True,
        )
    return proc.pid


def check_and_restart():
    for name, cfg in BOTS.items():
        age = get_log_age_minutes(cfg["log"])
        running = is_running(cfg["script"].name)

        if not running:
            log.warning(f"{name} NO está corriendo — iniciando...")
            pid = start_bot(cfg["script"], cfg["log"])
            log.info(f"{name} iniciado con PID {pid}")

        elif age > cfg["timeout_min"]:
            log.warning(f"{name} colgado ({age:.1f} min sin loggear) — reiniciando...")
            kill_bot(cfg["script"].name)
            time.sleep(1)
            pid = start_bot(cfg["script"], cfg["log"])
            log.info(f"{name} reiniciado con PID {pid}")

        else:
            log.info(f"{name} OK (último log hace {age:.1f} min)")


if __name__ == "__main__":
    log.info("Watchdog iniciado")
    while True:
        try:
            check_and_restart()
        except Exception as e:
            log.error(f"Error en watchdog: {e}")
        time.sleep(CHECK_INTERVAL)
