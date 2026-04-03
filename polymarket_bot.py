"""
Polymarket AI Trading Bot
=========================
Arquitectura completa: lectura de mercados → análisis con Claude AI → ejecución de órdenes

SETUP:
  pip install py-clob-client anthropic python-dotenv requests

CONFIGURAR .env:
  ANTHROPIC_API_KEY=sk-ant-...
  POLYMARKET_PRIVATE_KEY=0x...          # tu private key de wallet EOA
  POLYMARKET_FUNDER=0x...               # address que tiene USDC (si usás Magic/email wallet)
  POLYMARKET_SIGNATURE_TYPE=0           # 0=EOA/MetaMask, 1=Magic/email wallet
"""

import os
import json
import time
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv
import requests
import anthropic
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv(override=True)

# ─── Paper Trading ────────────────────────────────────────────────────────────
from paper_trading import get_polymarket_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Configuración ────────────────────────────────────────────────────────────

HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # Polygon

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
FUNDER = os.getenv("POLYMARKET_FUNDER")          # solo si usás Magic wallet
SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))

# Proxy/VPN — Polymarket bloqueado en AR
# Configurar en .env: HTTPS_PROXY=socks5://127.0.0.1:1080  (o http://...)
PROXY = os.getenv("HTTPS_PROXY") or os.getenv("POLYMARKET_PROXY")
PROXIES = {"https": PROXY, "http": PROXY} if PROXY else None

# Parámetros del bot
MAX_BET_USDC = float(os.getenv("MAX_BET_USDC", "10"))        # máximo por apuesta en USDC
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.05"))               # edge mínimo para entrar (5%)
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"      # True = no ejecuta órdenes reales

# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class Market:
    question: str
    token_id: str
    condition_id: str
    slug: str
    volume: float
    liquidity: float
    yes_price: float
    no_price: float
    end_date: str
    description: str = ""

@dataclass
class TradeSignal:
    market: Market
    side: str          # "YES" o "NO"
    price: float       # precio actual del lado a comprar
    ai_probability: float    # probabilidad estimada por Claude (0-1)
    market_probability: float
    edge: float        # diferencia entre AI prob y market prob
    reasoning: str
    confidence: str    # "HIGH", "MEDIUM", "LOW"

# ─── Clientes ─────────────────────────────────────────────────────────────────

def get_clob_client(authenticated: bool = False) -> ClobClient:
    """Retorna cliente CLOB. authenticated=True requiere private key."""
    if not authenticated:
        return ClobClient(HOST)

    if not PRIVATE_KEY:
        raise ValueError("POLYMARKET_PRIVATE_KEY no configurada en .env")

    kwargs = dict(key=PRIVATE_KEY, chain_id=CHAIN_ID, signature_type=SIGNATURE_TYPE)
    if SIGNATURE_TYPE == 1 and FUNDER:
        kwargs["funder"] = FUNDER

    client = ClobClient(HOST, **kwargs)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


# ─── RAG + Scoring (opcional, requiere setup_rag.py) ─────────────────────────
try:
    from rag_pipeline import find_similar_markets, score_market, build_enriched_prompt
    RAG_AVAILABLE = True
except ImportError:
    RAG_AVAILABLE = False

ai_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# ─── Obtener Mercados ──────────────────────────────────────────────────────────

def fetch_active_markets(limit: int = 50, min_volume: float = 10000) -> list[Market]:
    """Trae mercados activos de la Gamma API, filtrados por volumen."""
    log.info(f"Buscando mercados activos (min volumen: ${min_volume:,.0f})...")

    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": limit, "order": "volume", "ascending": "false"},
            headers={"Accept": "application/json"},
            proxies=PROXIES,
            timeout=30
        )
        resp.raise_for_status()
        raw = resp.json()
        log.info(f"  → Gamma API devolvió {len(raw)} registros brutos")
    except Exception as e:
        log.error(f"Error al traer mercados: {e}")
        # Reintentar una vez
        try:
            time.sleep(5)
            resp = requests.get(f"{GAMMA_API}/markets", params={"active": "true", "closed": "false", "limit": limit, "order": "volume", "ascending": "false"}, headers={"Accept": "application/json"}, proxies=PROXIES, timeout=30)
            resp.raise_for_status()
            raw = resp.json()
            log.info(f"  → Reintento exitoso: {len(raw)} registros")
        except Exception as e2:
            log.error(f"Error en reintento: {e2}")
            return []

    # DEBUG: mostrar estructura del primer mercado
    if raw:
        sample = raw[0]
        log.info(f"  DEBUG keys: {list(sample.keys())}")
        log.info(f"  DEBUG tokens: {sample.get('tokens', 'NO TOKENS KEY')}")
        log.info(f"  DEBUG volume keys: volume={sample.get('volume')}, volumeNum={sample.get('volumeNum')}, volume24hr={sample.get('volume24hr')}")

    markets = []
    for m in raw:
        try:
            clob_token_ids = m.get("clobTokenIds", [])
            # clobTokenIds puede ser string JSON o lista
            if isinstance(clob_token_ids, str):
                try:
                    clob_token_ids = json.loads(clob_token_ids)
                except Exception:
                    clob_token_ids = []
            if not clob_token_ids:
                continue

            token_id = clob_token_ids[0]  # YES token

            volume = float(m.get("volumeNum", 0) or 0)
            liquidity = float(m.get("liquidityNum", 0) or 0)

            if volume < min_volume or not token_id:
                continue

            # Precio desde outcomePrices
            try:
                outcome_prices = json.loads(m.get("outcomePrices", "[0.5, 0.5]") or "[0.5, 0.5]")
                yes_price = float(outcome_prices[0])
            except Exception:
                yes_price = float(m.get("lastTradePrice", 0.5) or 0.5)

            no_price = 1.0 - yes_price

            markets.append(Market(
                question=m.get("question", ""),
                token_id=token_id,
                condition_id=m.get("conditionId", ""),
                slug=m.get("slug", ""),
                volume=volume,
                liquidity=liquidity,
                yes_price=yes_price,
                no_price=no_price,
                end_date=m.get("endDate", ""),
                description=m.get("description", "")[:500],
            ))
        except Exception as e:
            log.warning(f"Error procesando mercado: {e}")
            continue

    log.info(f"  → {len(markets)} mercados cargados")
    return markets

# ─── Análisis con Claude AI ────────────────────────────────────────────────────

ANALYSIS_PROMPT = """Eres un analista experto en mercados de predicción. Tu tarea es evaluar la probabilidad real de un evento y detectar si hay ineficiencia en el precio del mercado.

MERCADO:
Pregunta: {question}
Descripción: {description}
Fecha cierre: {end_date}
Precio actual YES: {yes_price:.2%} (mercado asigna {yes_price:.0%} de probabilidad)
Precio actual NO: {no_price:.2%}
Volumen total: ${volume:,.0f}
Liquidez: ${liquidity:,.0f}

INSTRUCCIONES:
1. Analiza el evento basándote en tu conocimiento
2. Estima la probabilidad REAL de que ocurra (YES)
3. Calcula el edge: (tu_prob - precio_mercado)
4. Solo recomienda operar si |edge| > 5% y tenés alta confianza

Responde ÚNICAMENTE con JSON válido, sin texto adicional:
{{
  "ai_probability": 0.XX,
  "reasoning": "explicación breve del análisis",
  "confidence": "HIGH|MEDIUM|LOW",
  "recommended_side": "YES|NO|PASS",
  "key_factors": ["factor1", "factor2"]
}}

Si no tenés suficiente información o el mercado es demasiado eficiente, usa recommended_side: "PASS"."""


def analyze_market_with_claude(market: Market) -> Optional[TradeSignal]:
    """Llama a Claude para analizar un mercado y generar señal de trading."""
    if not ai_client:
        log.error("Claude client no inicializado. Configurá ANTHROPIC_API_KEY.")
        return None

    # Enriquecer con RAG + ML si está disponible
    if RAG_AVAILABLE:
        market_dict = {
            "question": market.question,
            "description": market.description,
            "end_date": market.end_date,
            "yes_price": market.yes_price,
            "no_price": market.no_price,
            "volume": market.volume,
            "liquidity": market.liquidity,
        }
        similar = find_similar_markets(market.question, n=5)
        ml_score = score_market(market_dict)
        prompt = build_enriched_prompt(market_dict, similar, ml_score)
        if similar:
            log.info(f"    RAG: {len(similar)} históricos similares | ML score: {ml_score:.1%}")
    else:
        prompt = ANALYSIS_PROMPT.format(
            question=market.question,
            description=market.description or "Sin descripción",
            end_date=market.end_date,
            yes_price=market.yes_price,
            no_price=market.no_price,
            volume=market.volume,
            liquidity=market.liquidity,
        )

    try:
        response = ai_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()
        # Limpiar posibles backticks
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        data = json.loads(raw)

        ai_prob = float(data["ai_probability"])
        side = data.get("recommended_side", "PASS")
        confidence = data.get("confidence", "LOW")
        reasoning = data.get("reasoning", "")

        if side == "PASS":
            log.info(f"  → PASS: {market.question[:60]}...")
            return None

        # Calcular edge
        if side == "YES":
            market_prob = market.yes_price
            trade_price = market.yes_price
        else:  # NO
            market_prob = market.no_price
            ai_prob = 1.0 - ai_prob  # invertir para calcular edge del NO
            trade_price = market.no_price

        edge = ai_prob - market_prob

        if abs(edge) < MIN_EDGE:
            log.info(f"  → Edge insuficiente ({edge:.1%}): {market.question[:50]}...")
            return None

        return TradeSignal(
            market=market,
            side=side,
            price=trade_price,
            ai_probability=ai_prob,
            market_probability=market_prob,
            edge=edge,
            reasoning=reasoning,
            confidence=confidence,
        )

    except json.JSONDecodeError as e:
        log.warning(f"  Claude devolvió JSON inválido: {e}")
        return None
    except Exception as e:
        log.error(f"  Error analizando con Claude: {e}")
        return None

# ─── Ejecución de Órdenes ──────────────────────────────────────────────────────

def calculate_position_size(signal: TradeSignal, balance: float) -> float:
    """Kelly Criterion simplificado para sizing."""
    p = signal.ai_probability
    q = 1 - p
    b = (1 / signal.price) - 1  # odds decimales menos 1

    kelly = (p * b - q) / b
    # Usar Kelly fraccionado al 25% por seguridad
    kelly_fraction = kelly * 0.25
    kelly_fraction = max(0, min(kelly_fraction, 0.2))  # máximo 20% del balance

    bet = min(
        kelly_fraction * balance,
        MAX_BET_USDC,
    )
    return round(max(bet, 1.0), 2)  # mínimo $1

def execute_trade(signal: TradeSignal, client: ClobClient, balance: float) -> dict:
    """Ejecuta una orden en Polymarket."""
    size = calculate_position_size(signal, balance)
    side_const = BUY  # siempre compramos (YES o NO token)

    log.info(f"\n{'='*60}")
    log.info(f"SEÑAL DE TRADE")
    log.info(f"  Mercado: {signal.market.question}")
    log.info(f"  Lado: {signal.side}")
    log.info(f"  Precio: {signal.price:.3f} | AI prob: {signal.ai_probability:.1%} | Edge: {signal.edge:+.1%}")
    log.info(f"  Confianza: {signal.confidence}")
    log.info(f"  Reasoning: {signal.reasoning}")
    log.info(f"  Size calculado: ${size:.2f} USDC")

    if DRY_RUN:
        log.info("  [DRY RUN] Orden NO ejecutada (simulación)")
        return {"dry_run": True, "signal": signal, "size": size}

    try:
        order = OrderArgs(
            token_id=signal.market.token_id,
            price=round(signal.price, 4),
            size=size,
            side=side_const,
        )
        signed = client.create_order(order)
        resp = client.post_order(signed, OrderType.GTC)
        log.info(f"  ✅ Orden ejecutada: {resp}")
        return {"success": True, "response": resp, "signal": signal, "size": size}
    except Exception as e:
        log.error(f"  ❌ Error ejecutando orden: {e}")
        return {"success": False, "error": str(e)}

# ─── Fetch current prices for open positions ─────────────────────────────────

def fetch_position_prices(engine) -> dict:
    """Fetch current YES prices for all open positions via Gamma API."""
    price_map = {}
    open_pos = engine.state.get("open_positions", [])
    if not open_pos:
        return price_map

    token_ids = [p["token_id"] for p in open_pos]
    try:
        # Gamma API: fetch markets and match by clobTokenIds
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"active": "true", "closed": "false", "limit": 100, "order": "volume", "ascending": "false"},
            headers={"Accept": "application/json"},
            proxies=PROXIES,
            timeout=30
        )
        resp.raise_for_status()
        raw = resp.json()

        for m in raw:
            clob_ids = m.get("clobTokenIds", [])
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except Exception:
                    clob_ids = []
            for tid in clob_ids:
                if tid in token_ids:
                    try:
                        outcome_prices = json.loads(m.get("outcomePrices", "[0.5, 0.5]") or "[0.5, 0.5]")
                        yes_price = float(outcome_prices[0])
                    except Exception:
                        yes_price = float(m.get("lastTradePrice", 0.5) or 0.5)

                    # If position is YES, price is yes_price; if NO, price is 1 - yes_price
                    pos = next((p for p in open_pos if p["token_id"] == tid), None)
                    if pos:
                        if pos["side"] == "YES":
                            price_map[tid] = yes_price
                        else:
                            price_map[tid] = 1.0 - yes_price
    except Exception as e:
        log.warning(f"Error fetching position prices: {e}")

    return price_map


# ─── Loop Principal ────────────────────────────────────────────────────────────

# Engine global (persiste entre ciclos)
_engine = None

def get_engine():
    global _engine
    if _engine is None:
        _engine = get_polymarket_engine(initial_capital=300.0)
    return _engine

def run_bot_cycle():
    """Un ciclo completo del bot."""
    engine = get_engine()

    log.info(f"\n{'#'*60}")
    log.info(f"POLYMARKET CYCLE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Capital: ${engine.state['current_capital']:.2f} | Posiciones: {len(engine.state['open_positions'])}")
    log.info(f"{'#'*60}")

    # 1. Monitor existing positions — check exits
    open_pos = engine.state.get("open_positions", [])
    if open_pos:
        log.info(f"\nMonitoreando {len(open_pos)} posiciones abiertas...")
        price_map = fetch_position_prices(engine)
        if price_map:
            engine.check_exits(price_map)
            for pos in engine.state["open_positions"]:
                tid = pos["token_id"]
                if tid in price_map:
                    log.info(f"  {pos['question'][:50]}... | {pos['side']} @ {pos['entry_price']:.2f} → {pos['current_price']:.2f} | P&L {pos['unrealized_pnl_pct']:+.1f}%")

    # 2. Check if we can open new positions
    open_count = len(engine.state.get("open_positions", []))
    if open_count >= MAX_OPEN_POSITIONS:
        log.info(f"Max posiciones alcanzado ({MAX_OPEN_POSITIONS}). Solo monitoreo.")
        engine.add_log(f"Ciclo: monitoreo | {open_count} posiciones abiertas")
        engine.save()
        return

    # 3. Fetch markets
    markets = fetch_active_markets(limit=50, min_volume=1000)
    if not markets:
        log.warning("No se encontraron mercados.")
        engine.add_log("Ciclo: 0 mercados encontrados")
        engine.save()
        return

    engine.state["markets_scanned"] = len(markets)

    # 4. Filter: skip markets where we already have a position
    open_tokens = {p["token_id"] for p in engine.state.get("open_positions", [])}
    markets_to_analyze = [m for m in markets if m.token_id not in open_tokens][:20]

    # 5. Analyze with Claude
    signals = []
    log.info(f"\nAnalizando {len(markets_to_analyze)} mercados con Claude...")

    for market in markets_to_analyze:
        log.info(f"  Analizando: {market.question[:70]}...")
        signal = analyze_market_with_claude(market)
        if signal:
            signals.append(signal)
            log.info(f"    SENAL: {signal.side} | Edge: {signal.edge:+.1%} | Confianza: {signal.confidence}")
        time.sleep(0.5)

    # 6. Rank signals
    confidence_weight = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.2}
    signals.sort(
        key=lambda s: abs(s.edge) * confidence_weight.get(s.confidence, 0.1),
        reverse=True
    )

    engine.state["signals_found"] = len(signals)
    log.info(f"\n{len(signals)} senales detectadas")

    # 7. Execute best signals via paper engine
    slots = MAX_OPEN_POSITIONS - open_count
    executed = 0

    for signal in signals[:slots]:
        if signal.confidence == "LOW":
            continue

        # Paper trade via engine
        pos = engine.open_trade(signal)
        if pos:
            executed += 1
            log.info(f"  [PAPER] {signal.side} {signal.market.question[:45]}... @ {signal.price:.2f} | ${pos['size_usdc']:.2f}")

        # Real trade (if not DRY_RUN)
        if not DRY_RUN:
            try:
                auth_client = get_clob_client(authenticated=True)
                execute_trade(signal, auth_client, engine.state["current_capital"])
            except Exception as e:
                log.error(f"Error en trade real: {e}")

    # 8. Log cycle summary
    summary = f"Ciclo: {len(markets)} mercados | {len(signals)} senales | {executed} trades | Capital ${engine.state['current_capital']:.2f}"
    engine.add_log(summary)
    engine.save()

    log.info(f"\n{summary}")

def run_forever(interval_minutes: int = 10):
    """Corre el bot en loop cada N minutos."""
    log.info(f"Polymarket AI Bot iniciado")
    log.info(f"   Modo: {'DRY RUN (simulacion)' if DRY_RUN else 'REAL'}")
    log.info(f"   Intervalo: {interval_minutes} min | Max bet: ${MAX_BET_USDC} | Min edge: {MIN_EDGE:.0%}")

    while True:
        try:
            run_bot_cycle()
        except KeyboardInterrupt:
            log.info("\nBot detenido por el usuario")
            break
        except Exception as e:
            log.error(f"Error inesperado en ciclo: {e}", exc_info=True)

        log.info(f"\nProximo ciclo en {interval_minutes} minutos...")
        time.sleep(interval_minutes * 60)

# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        run_bot_cycle()
    else:
        interval = int(os.getenv("INTERVAL_MINUTES", "10"))
        run_forever(interval)