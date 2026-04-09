"""
API Server
==========
Servidor FastAPI que:
1. Sirve los archivos de estado JSON de los bots
2. Sirve el dashboard React como archivos estáticos
3. Corre los bots en background threads

Railway lo usa como punto de entrada único.
"""

import os
import json
import asyncio
import logging
import threading
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "dashboard_dist"  # build del React
DATA_DIRS = {
    "binance": BASE_DIR / "paper_trading" / "binance_state.json",
    "altcoins": BASE_DIR / "altcoin_data" / "state.json",
    "sp500": BASE_DIR / "sp500_data" / "state.json",
    "rsi": BASE_DIR / "rsi_bot_data" / "state.json",
}

# ─── Bot Runners ──────────────────────────────────────────────────────────────

def run_binance_bot():
    """DEPRECATED — Binance swing bot removed."""
    log.info("⚠️ Binance swing bot removed — skipping")


def run_altcoin_bot():
    """Corre el bot de altcoins en thread separado."""
    try:
        import time
        from altcoin_bot import get_client, run_cycle, INTERVAL_MINUTES

        log.info("🤖 Altcoin bot thread iniciado")
        client = get_client()

        while True:
            try:
                run_cycle(client)
            except Exception as e:
                log.error(f"Error en ciclo Altcoin: {e}")
            time.sleep(INTERVAL_MINUTES * 60)
    except Exception as e:
        log.error(f"Error fatal Altcoin bot: {e}")


def run_rsi_bot():
    """Corre el RSI bot en thread separado."""
    try:
        import time
        from rsi_bot import get_alpaca_client, run_trading_cycle

        ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
        ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")

        if not ALPACA_KEY or not ALPACA_SECRET:
            log.warning("RSI bot: ALPACA keys no configuradas, saltando")
            return

        log.info("🤖 RSI bot thread iniciado")
        api = get_alpaca_client(paper=True)
        if not api:
            return

        while True:
            try:
                clock = api.get_clock()
                if clock.is_open:
                    run_trading_cycle(api, paper=True)
                    time.sleep(30 * 60)  # cada 30 min cuando el mercado está abierto
                else:
                    time.sleep(5 * 60)  # cada 5 min cuando está cerrado
            except Exception as e:
                log.error(f"Error en ciclo RSI: {e}")
                time.sleep(5 * 60)
    except Exception as e:
        log.error(f"Error fatal RSI bot: {e}")


# ─── App Startup ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lanza los bots en threads al iniciar el servidor."""
    log.info("Iniciando bots en background...")

    threads = [
        threading.Thread(target=run_binance_bot, daemon=True, name="BinanceBot"),
        threading.Thread(target=run_altcoin_bot, daemon=True, name="AltcoinBot"),
        threading.Thread(target=run_rsi_bot, daemon=True, name="RSIBot"),
    ]

    for t in threads:
        t.start()
        log.info(f"  ✓ {t.name} iniciado")

    yield  # servidor corriendo

    log.info("Servidor detenido.")


app = FastAPI(title="Trading Bot HQ", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── API Endpoints ────────────────────────────────────────────────────────────

@app.get("/api/state/{bot}")
async def get_bot_state(bot: str):
    """Retorna el estado actual de un bot."""
    if bot not in DATA_DIRS:
        return JSONResponse({"error": f"Bot '{bot}' no encontrado"}, status_code=404)

    path = DATA_DIRS[bot]
    if not path.exists():
        return JSONResponse({"error": "Sin datos aún", "bot": bot}, status_code=200)

    try:
        data = json.loads(path.read_text())
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/state/all")
async def get_all_states():
    """Retorna el estado de todos los bots de una vez."""
    result = {}
    for bot, path in DATA_DIRS.items():
        if path.exists():
            try:
                result[bot] = json.loads(path.read_text())
            except Exception:
                result[bot] = {"error": "parse error"}
        else:
            result[bot] = {"status": "no data"}
    return JSONResponse(result)


@app.get("/api/health")
async def health():
    return {"status": "ok", "bots": list(DATA_DIRS.keys())}


@app.post("/api/close/binance")
async def close_binance():
    """Marca la posición de Binance para cierre manual."""
    path = BASE_DIR / "paper_trading" / "binance_state.json"
    if path.exists():
        data = json.loads(path.read_text())
        data["manual_close"] = True
        path.write_text(json.dumps(data, indent=2))
        return {"status": "ok", "message": "Posición marcada para cierre"}
    return JSONResponse({"error": "Sin estado"}, status_code=404)


@app.post("/api/close/altcoin")
async def close_altcoin(body: dict):
    """Cierra posición de altcoin — acepta estado completo del dashboard o solo symbol."""
    symbol = body.get("symbol")
    new_state = body.get("state")
    path = BASE_DIR / "altcoin_data" / "state.json"

    if new_state:
        # Dashboard envió el estado ya calculado — guardar directamente
        path.write_text(json.dumps(new_state, indent=2))
        return {"status": "ok", "message": f"{symbol} cerrado instantáneamente"}

    # Fallback: marcar para cierre en próximo ciclo
    if path.exists():
        data = json.loads(path.read_text())
        closes = data.get("manual_close", [])
        if symbol not in closes:
            closes.append(symbol)
        data["manual_close"] = closes
        path.write_text(json.dumps(data, indent=2))
        return {"status": "ok", "message": f"{symbol} marcado para cierre"}
    return JSONResponse({"error": "Sin estado"}, status_code=404)


# ─── Servir Dashboard React ───────────────────────────────────────────────────

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Sirve el dashboard React (SPA)."""
        index = STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return JSONResponse({"error": "Dashboard no buildeado"}, status_code=404)
else:
    @app.get("/")
    async def root():
        return {"message": "Trading Bot HQ API", "endpoints": ["/api/state/binance", "/api/state/altcoins", "/api/state/rsi", "/api/health"]}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
