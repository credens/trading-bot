"""
Local State Server
==================
Servidor minimalista que permite al dashboard leer y escribir
los archivos JSON de estado directamente.
Corre en localhost:8082 junto al dashboard de Vite.

Uso:
  python3 local_server.py
"""

import json
import sys
import logging
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

STATE_FILES = {
    "altcoins":  BASE_DIR / "altcoin_data" / "state.json",
    "rsi":       BASE_DIR / "rsi_bot_data" / "state.json",
    "scalping":  BASE_DIR / "paper_trading" / "scalping_state.json",
    "altscalp":  BASE_DIR / "paper_trading" / "altscalp_state.json",
}

EDITABLE_FILES = [
    "altcoin_bot.py",
    "scalping_bot.py",
    "market_scenario.py",
    "notifications.py",
    "trade_logger.py",
    "daily_report.py",
    "local_server.py"
]


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # silenciar logs de cada request

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")

        # GET /files — listar archivos editables
        if parts[0] == "files" and len(parts) == 1:
            self._send(200, {"files": EDITABLE_FILES})
            return

        # GET /file?name=xxx — leer contenido de un archivo
        if parts[0] == "file" and len(parts) == 1:
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(urlparse(self.path).query)
            filename = query.get("name", [None])[0]
            if filename in EDITABLE_FILES:
                path = BASE_DIR / filename
                if path.exists():
                    self._send(200, {"content": path.read_text(), "name": filename})
                else:
                    self._send(404, {"error": "file not found"})
            else:
                self._send(403, {"error": "forbidden"})
            return

        # GET /stats  — estadísticas globales de todos los bots
        if parts[0] == "stats" and len(parts) == 1:
            try:
                from trade_logger import generate_stats
                self._send(200, generate_stats())
            except Exception as e:
                self._send(500, {"error": str(e)})
            return

        # GET /state/binance  GET /state/altcoins  GET /state/rsi
        if len(parts) == 2 and parts[0] == "state":
            bot = parts[1]
            path = STATE_FILES.get(bot)
            if path and path.exists():
                try:
                    data = json.loads(path.read_text())
                    self._send(200, data)
                except Exception as e:
                    self._send(500, {"error": str(e)})
            else:
                self._send(404, {"error": "no data"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        # POST /file  body={"name": "xxx", "content": "..."}
        parts = self.path.split("?")[0].strip("/").split("/")
        if parts[0] == "file" and len(parts) == 1:
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body)
                filename = data.get("name")
                content = data.get("content")
                if filename in EDITABLE_FILES:
                    path = BASE_DIR / filename
                    path.write_text(content)
                    log.info(f"Archivo {filename} actualizado desde dashboard")
                    self._send(200, {"status": "ok"})
                else:
                    self._send(403, {"error": "forbidden"})
            except Exception as e:
                self._send(500, {"error": str(e)})
            return

        # POST /state/binance  body=JSON completo del estado
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) == 2 and parts[0] == "state":
            bot = parts[1]
            path = STATE_FILES.get(bot)
            if not path:
                self._send(404, {"error": "bot not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, indent=2))
                log.info(f"Estado {bot} actualizado desde dashboard")
                self._send(200, {"status": "ok"})
            except Exception as e:
                self._send(500, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})


if __name__ == "__main__":
    port = 8082
    server = HTTPServer(("0.0.0.0", port), Handler)
    log.info(f"Local state server corriendo en http://localhost:{port}")
    log.info("  GET  /state/altcoins  — leer estado Altcoins")
    log.info("  POST /state/altcoins  — escribir estado Altcoins")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Servidor detenido.")
