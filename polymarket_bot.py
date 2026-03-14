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

load_dotenv()

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

@dataclass
class BotState:
    open_positions: list = field(default_factory=list)
    trades_executed: int = 0
    total_pnl: float = 0.0
    last_run: str = ""

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
            resp = requests.get(f"{GAMMA_API}/markets", params={"active": "true", "closed": "false", "limit": limit, "order": "volume", "ascending": "false"}, headers={"Accept": "application/json"}, timeout=30)
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

# ─── Loop Principal ────────────────────────────────────────────────────────────

# Paper engine global (persiste entre ciclos)
_paper_pm = None

def get_paper_engine():
    global _paper_pm
    if _paper_pm is None:
        _paper_pm = get_polymarket_engine(initial_capital=300.0)
    return _paper_pm

def run_bot_cycle(state: BotState) -> BotState:
    """Un ciclo completo del bot."""
    log.info(f"\n{'#'*60}")
    log.info(f"BOT CYCLE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"{'#'*60}")

    # 1. Verificar balance
    try:
        auth_client = get_clob_client(authenticated=not DRY_RUN)
        if not DRY_RUN:
            balance_info = auth_client.get_balance_allowance()
            balance = float(balance_info.get("balance", 0))
        else:
            balance = 1000.0  # balance ficticio en dry run
        log.info(f"Balance disponible: ${balance:.2f} USDC")
    except Exception as e:
        log.error(f"Error al obtener balance: {e}")
        balance = 0
        auth_client = get_clob_client(authenticated=False)

    if balance < 5 and not DRY_RUN:
        log.warning("Balance insuficiente (<$5). Abortando ciclo.")
        return state

    # 2. Verificar límite de posiciones abiertas
    if len(state.open_positions) >= MAX_OPEN_POSITIONS:
        log.info(f"Máximo de posiciones abiertas alcanzado ({MAX_OPEN_POSITIONS}). Saltando.")
        return state

    # 3. Traer mercados
    markets = fetch_active_markets(limit=50, min_volume=1000)
    if not markets:
        log.warning("No se encontraron mercados.")
        return state

    # 4. Analizar con Claude y filtrar señales
    signals = []
    log.info(f"\nAnalizando {len(markets)} mercados con Claude...")

    for market in markets[:20]:  # analizar máx 20 por ciclo para no gastar tokens
        # Saltear si ya tenemos posición abierta en este mercado
        if any(p.get("token_id") == market.token_id for p in state.open_positions):
            continue

        log.info(f"  Analizando: {market.question[:70]}...")
        signal = analyze_market_with_claude(market)

        if signal:
            signals.append(signal)
            log.info(f"    ✓ SEÑAL: {signal.side} | Edge: {signal.edge:+.1%} | Confianza: {signal.confidence}")

        time.sleep(0.5)  # throttle para no abusar la API

    # 5. Rankear señales por edge * confianza
    confidence_weight = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.2}
    signals.sort(
        key=lambda s: abs(s.edge) * confidence_weight.get(s.confidence, 0.1),
        reverse=True
    )

    log.info(f"\n{len(signals)} señales detectadas")

    # 6. Ejecutar las mejores señales
    slots_available = MAX_OPEN_POSITIONS - len(state.open_positions)
    executed = 0

    for signal in signals[:slots_available]:
        if signal.confidence == "LOW":
            log.info(f"  Saltando señal LOW confidence: {signal.market.question[:50]}...")
            continue

        result = execute_trade(signal, auth_client, balance)

        if result.get("dry_run") or result.get("success"):
            state.open_positions.append({
                "token_id": signal.market.token_id,
                "question": signal.market.question,
                "side": signal.side,
                "price": signal.price,
                "size": result.get("size", 0),
                "timestamp": datetime.now().isoformat(),
            })
            state.trades_executed += 1
            executed += 1
            balance -= result.get("size", 0)

            # Registrar en paper trading
            if DRY_RUN:
                paper = get_paper_engine()
                paper.open_polymarket_trade(signal)
                paper.update_market_data()
                log.info(f"  [PAPER] Capital: ${paper.state.current_capital:.2f} | P&L: {paper.state.total_pnl:+.2f} | Win: {paper.state.win_rate:.0f}%")

    log.info(f"\nCiclo completo. Ejecutadas: {executed} órdenes. Total histórico: {state.trades_executed}")
    state.last_run = datetime.now().isoformat()
    return state

def run_forever(interval_minutes: int = 30):
    """Corre el bot en loop cada N minutos."""
    state = BotState()
    log.info(f"🤖 Polymarket AI Bot iniciado")
    log.info(f"   Modo: {'DRY RUN (simulación)' if DRY_RUN else '⚠️  REAL — cuidado con tu dinero'}")
    log.info(f"   Intervalo: {interval_minutes} min | Max bet: ${MAX_BET_USDC} | Min edge: {MIN_EDGE:.0%}")

    while True:
        try:
            state = run_bot_cycle(state)
        except KeyboardInterrupt:
            log.info("\n🛑 Bot detenido por el usuario")
            break
        except Exception as e:
            log.error(f"Error inesperado en ciclo: {e}", exc_info=True)

        log.info(f"\n⏰ Próximo ciclo en {interval_minutes} minutos...")
        time.sleep(interval_minutes * 60)

# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    # Modo: python polymarket_bot.py          → loop continuo
    # Modo: python polymarket_bot.py once     → un solo ciclo
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        state = BotState()
        run_bot_cycle(state)
    else:
        interval = int(os.getenv("INTERVAL_MINUTES", "30"))
        run_forever(interval)