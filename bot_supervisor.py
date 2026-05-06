"""
Trading Bot Supervisor
======================
Keeps the local state server and trading bots alive.

Intended to be started by macOS launchd via run_supervisor.sh.
If a child process exits, it is restarted after a short delay.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

PYTHON = os.environ.get("PYTHON", sys.executable)
RESTART_DELAY_SECONDS = int(os.environ.get("BOT_RESTART_DELAY", "10"))
CHECK_INTERVAL_SECONDS = int(os.environ.get("BOT_CHECK_INTERVAL", "5"))


SERVICES = [
    {
        "name": "state_server",
        "cmd": [PYTHON, "local_server.py"],
        "log": LOG_DIR / "server.log",
        "port": 8082,
    },
    {
        "name": "btc_scalping",
        "cmd": [PYTHON, "scalping_bot.py"],
        "log": LOG_DIR / "scalp.log",
    },
    {
        "name": "altscalp",
        "cmd": [PYTHON, "altscalp_bot.py"],
        "log": LOG_DIR / "altscalp.log",
    },
]


children: dict[str, subprocess.Popen] = {}
log_handles = {}
stopping = False


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def supervisor_log(message: str) -> None:
    line = f"{ts()} [SUP] {message}"
    print(line, flush=True)
    with (LOG_DIR / "supervisor.log").open("a") as f:
        f.write(line + "\n")


def port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def start_service(service: dict) -> None:
    name = service["name"]
    if service.get("port") and port_is_open(service["port"]):
        supervisor_log(f"{name} already available on port {service['port']}")
        return

    log_handle = service["log"].open("a")
    log_handles[name] = log_handle
    supervisor_log(f"starting {name}: {' '.join(service['cmd'])}")
    children[name] = subprocess.Popen(
        service["cmd"],
        cwd=BASE_DIR,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def stop_children(*_args) -> None:
    global stopping
    stopping = True
    supervisor_log("stopping children")
    for name, proc in list(children.items()):
        if proc.poll() is None:
            proc.terminate()
            supervisor_log(f"terminated {name} pid={proc.pid}")


def close_logs() -> None:
    for handle in log_handles.values():
        try:
            handle.close()
        except Exception:
            pass


def main() -> int:
    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)

    supervisor_log("supervisor started")
    for service in SERVICES:
        start_service(service)

    while not stopping:
        for service in SERVICES:
            name = service["name"]
            proc = children.get(name)

            if service.get("port") and proc is None:
                if not port_is_open(service["port"]):
                    start_service(service)
                continue

            if proc is None:
                start_service(service)
                continue

            exit_code = proc.poll()
            if exit_code is not None:
                supervisor_log(f"{name} exited with code {exit_code}; restarting in {RESTART_DELAY_SECONDS}s")
                handle = log_handles.pop(name, None)
                if handle:
                    handle.close()
                children.pop(name, None)
                if not stopping:
                    time.sleep(RESTART_DELAY_SECONDS)
                    start_service(service)

        time.sleep(CHECK_INTERVAL_SECONDS)

    for proc in children.values():
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    close_logs()
    supervisor_log("supervisor stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
