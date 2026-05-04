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
    "btc":       BASE_DIR / "paper_trading" / "btc_state.json",
    "alt":       BASE_DIR / "paper_trading" / "alt_state.json",
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
        try:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        except Exception as e:
            log.error(f"Error enviando respuesta: {e}")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

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
        
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                self._send(400, {"error": "Content-Length is 0"})
                return
            body = self.rfile.read(length)
            if not body:
                self._send(400, {"error": "Empty body"})
                return
            data = json.loads(body)
        except Exception as e:
            log.error(f"Error leyendo POST body: {e}")
            self._send(400, {"error": "invalid json"})
            return

        if parts[0] == "file" and len(parts) == 1:
            filename = data.get("name")
            content = data.get("content")
            if filename in EDITABLE_FILES:
                path = BASE_DIR / filename
                path.write_text(content)
                log.info(f"Archivo {filename} actualizado desde dashboard")
                self._send(200, {"status": "ok"})
            else:
                self._send(403, {"error": "forbidden"})
            return

        # POST /state/binance  body=JSON completo del estado
        if len(parts) == 2 and parts[0] == "state":
            bot = parts[1]
            path = STATE_FILES.get(bot)
            if not path:
                self._send(404, {"error": "bot not found"})
                return
            try:
                import fcntl
                import os
                # Bloqueo real sobre el archivo para que el bot no lo pise
                with open(path, "a+") as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    f.seek(0)
                    f.truncate()
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                
                log.info(f"Estado {bot} actualizado desde dashboard")
                self._send(200, {"status": "ok"})
            except Exception as e:
                log.error(f"Error guardando estado {bot}: {e}")
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
