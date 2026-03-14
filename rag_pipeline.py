"""
Polymarket RAG + Scoring Pipeline
==================================
1. Descarga mercados históricos resueltos de Polymarket
2. Genera embeddings y los guarda en ChromaDB (base vectorial local)
3. Modelo de scoring con features extraídas de los históricos
4. Enriquece el prompt de Claude con contexto histórico relevante

SETUP:
  pip install chromadb sentence-transformers scikit-learn pandas numpy requests python-dotenv
"""

import os
import json
import time
import logging
import sqlite3
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_DIR = Path("./polymarket_data")
DATA_DIR.mkdir(exist_ok=True)

CHROMA_DIR = str(DATA_DIR / "chroma_db")
HISTORICAL_DB = DATA_DIR / "historical.db"
SCORING_MODEL_PATH = DATA_DIR / "scoring_model.pkl"

# ─── 1. DESCARGA DE HISTÓRICOS ────────────────────────────────────────────────

def download_historical_markets(pages: int = 20) -> list[dict]:
    """
    Descarga mercados YA RESUELTOS de Polymarket.
    Estos son los datos de entrenamiento — sabemos qué pasó realmente.
    """
    log.info(f"Descargando históricos ({pages} páginas)...")
    all_markets = []

    for page in range(pages):
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "closed": "true",
                    "active": "false",
                    "limit": 100,
                    "offset": page * 100,
                    "order": "volume",
                    "ascending": "false",
                },
                timeout=20
            )
            resp.raise_for_status()
            data = resp.json()

            if not data:
                break

            # Solo mercados binarios resueltos con resultado claro
            for m in data:
                outcomes = m.get("outcomes", "[]")
                try:
                    outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                except Exception:
                    continue

                prices = m.get("outcomePrices", "[0.5,0.5]")
                try:
                    prices = json.loads(prices) if isinstance(prices, str) else prices
                    yes_final_price = float(prices[0]) if prices else 0.5
                except Exception:
                    yes_final_price = 0.5

                # El precio final resuelto es ~1.0 (YES ganó) o ~0.0 (NO ganó)
                if yes_final_price > 0.95:
                    resolved_outcome = "YES"
                elif yes_final_price < 0.05:
                    resolved_outcome = "NO"
                else:
                    continue  # No resuelto claramente, saltear

                all_markets.append({
                    "id": m.get("id"),
                    "question": m.get("question", ""),
                    "description": (m.get("description") or "")[:300],
                    "category": _extract_category(m),
                    "volume": float(m.get("volumeNum", 0) or 0),
                    "liquidity": float(m.get("liquidityNum", 0) or 0),
                    "start_date": m.get("startDateIso", ""),
                    "end_date": m.get("endDateIso", ""),
                    "resolved_outcome": resolved_outcome,
                    "yes_final_price": yes_final_price,
                    # Precio en algún momento intermedio (usamos lastTradePrice como proxy)
                    "mid_market_price": float(m.get("lastTradePrice", 0.5) or 0.5),
                })

            log.info(f"  Página {page+1}: {len(data)} mercados, acumulado: {len(all_markets)}")
            time.sleep(0.3)

        except Exception as e:
            log.error(f"Error en página {page}: {e}")
            break

    log.info(f"Total históricos descargados: {len(all_markets)}")
    return all_markets


def _extract_category(market: dict) -> str:
    """Intenta extraer categoría del mercado."""
    events = market.get("events", [])
    if events and isinstance(events, list):
        for event in events:
            if isinstance(event, dict):
                tag = event.get("tag", "") or event.get("category", "")
                if tag:
                    return tag
    q = market.get("question", "").lower()
    if any(w in q for w in ["bitcoin", "eth", "crypto", "btc", "sol"]):
        return "crypto"
    if any(w in q for w in ["election", "vote", "president", "senate"]):
        return "politics"
    if any(w in q for w in ["nba", "nfl", "soccer", "world cup", "championship"]):
        return "sports"
    if any(w in q for w in ["fed", "rate", "inflation", "gdp", "recession"]):
        return "economics"
    return "general"


def save_to_sqlite(markets: list[dict]):
    """Guarda históricos en SQLite para consultas rápidas."""
    conn = sqlite3.connect(HISTORICAL_DB)
    df = pd.DataFrame(markets)
    df.to_sql("markets", conn, if_exists="replace", index=False)
    conn.close()
    log.info(f"Guardados {len(markets)} mercados en SQLite: {HISTORICAL_DB}")


# ─── 2. BASE VECTORIAL CON CHROMADB ───────────────────────────────────────────

def build_vector_db(markets: list[dict]):
    """
    Genera embeddings de las preguntas de mercado y los indexa en ChromaDB.
    Usa sentence-transformers local (sin costo, sin API).
    """
    log.info("Construyendo base vectorial con ChromaDB...")

    try:
        import chromadb
        from sentence_transformers import SentenceTransformer
    except ImportError:
        log.error("Instalá: pip install chromadb sentence-transformers")
        return None

    # Modelo de embeddings local — corre en tu Mac sin internet
    log.info("Cargando modelo de embeddings local (primera vez puede tardar)...")
    embedder = SentenceTransformer("all-MiniLM-L6-v2")

    client = chromadb.PersistentClient(path=CHROMA_DIR)

    # Recrear colección
    try:
        client.delete_collection("polymarket_historical")
    except Exception:
        pass
    collection = client.create_collection("polymarket_historical")

    # Procesar en batches
    batch_size = 50
    valid = [m for m in markets if m.get("question")]

    for i in range(0, len(valid), batch_size):
        batch = valid[i:i+batch_size]
        texts = [f"{m['question']} {m.get('description', '')}" for m in batch]
        embeddings = embedder.encode(texts).tolist()

        collection.add(
            ids=[str(m["id"]) for m in batch],
            embeddings=embeddings,
            documents=[m["question"] for m in batch],
            metadatas=[{
                "resolved_outcome": m["resolved_outcome"],
                "yes_final_price": m["yes_final_price"],
                "volume": m["volume"],
                "category": m["category"],
                "end_date": m["end_date"],
            } for m in batch]
        )
        log.info(f"  Indexados {min(i+batch_size, len(valid))}/{len(valid)}")

    log.info(f"✅ Base vectorial lista: {len(valid)} mercados indexados en {CHROMA_DIR}")
    return collection


def find_similar_markets(question: str, n: int = 5) -> list[dict]:
    """
    Dado un mercado nuevo, encuentra los N históricos más similares.
    Retorna contexto para enriquecer el prompt de Claude.
    """
    try:
        import chromadb
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return []

    try:
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        client = chromadb.PersistentClient(path=CHROMA_DIR)
        collection = client.get_collection("polymarket_historical")

        query_embedding = embedder.encode([question]).tolist()
        results = collection.query(query_embeddings=query_embedding, n_results=n)

        similar = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i]
            distance = results["distances"][0][i] if "distances" in results else 1.0
            similarity = max(0, 1 - distance)

            similar.append({
                "question": doc,
                "resolved_outcome": meta["resolved_outcome"],
                "yes_final_price": meta["yes_final_price"],
                "volume": meta["volume"],
                "category": meta["category"],
                "similarity": round(similarity, 3),
            })

        return similar

    except Exception as e:
        log.warning(f"RAG no disponible: {e}")
        return []


# ─── 3. MODELO DE SCORING LOCAL ───────────────────────────────────────────────

def extract_features(market: dict, similar_markets: list[dict]) -> np.ndarray:
    """
    Extrae features numéricas de un mercado para el modelo de scoring.
    Combina features del mercado actual + señales de los históricos similares.
    """
    features = []

    # Features del mercado actual
    features.append(float(market.get("yes_price", 0.5)))           # precio YES actual
    features.append(float(market.get("no_price", 0.5)))            # precio NO actual
    features.append(min(np.log1p(market.get("volume", 0)), 15))    # log volumen
    features.append(min(np.log1p(market.get("liquidity", 0)), 12)) # log liquidez

    # Días hasta cierre
    try:
        end = datetime.fromisoformat(market.get("end_date", "").replace("Z", "+00:00"))
        days_left = max(0, (end - datetime.now(end.tzinfo)).days)
    except Exception:
        days_left = 30
    features.append(min(days_left, 365))

    # Features de mercados similares (si hay RAG disponible)
    if similar_markets:
        yes_wins = sum(1 for m in similar_markets if m["resolved_outcome"] == "YES")
        avg_similarity = np.mean([m["similarity"] for m in similar_markets])
        avg_yes_price = np.mean([m["yes_final_price"] for m in similar_markets])
        base_rate_yes = yes_wins / len(similar_markets)

        features.append(base_rate_yes)          # tasa histórica de YES en mercados similares
        features.append(avg_similarity)          # qué tan similares son los históricos
        features.append(avg_yes_price)           # precio final promedio histórico
        features.append(float(yes_wins))         # conteo absoluto de YES
    else:
        features.extend([0.5, 0.0, 0.5, 0.0])

    return np.array(features).reshape(1, -1)


def train_scoring_model(markets_db_path: Path = HISTORICAL_DB):
    """
    Entrena un modelo simple (Random Forest) para predecir el outcome.
    Usa los históricos de SQLite como datos de entrenamiento.
    """
    import pickle
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.model_selection import cross_val_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline

    log.info("Entrenando modelo de scoring...")

    conn = sqlite3.connect(markets_db_path)
    df = pd.read_sql("SELECT * FROM markets WHERE volume > 1000", conn)
    conn.close()

    if len(df) < 50:
        log.warning(f"Pocos datos para entrenar ({len(df)}). Necesitás más históricos.")
        return None

    log.info(f"Entrenando con {len(df)} mercados históricos...")

    # Features básicas (sin RAG, para el modelo base)
    X = df[["mid_market_price", "volume", "liquidity"]].copy()
    X["log_volume"] = np.log1p(X["volume"])
    X["log_liquidity"] = np.log1p(X["liquidity"])
    X = X[["mid_market_price", "log_volume", "log_liquidity"]].values

    y = (df["resolved_outcome"] == "YES").astype(int).values

    # Pipeline: scaler + gradient boosting
    model = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", GradientBoostingClassifier(n_estimators=100, max_depth=3, random_state=42))
    ])

    # Cross-validation
    scores = cross_val_score(model, X, y, cv=5, scoring="accuracy")
    log.info(f"Accuracy CV: {scores.mean():.3f} ± {scores.std():.3f}")

    model.fit(X, y)

    # Guardar modelo
    with open(SCORING_MODEL_PATH, "wb") as f:
        pickle.dump(model, f)

    log.info(f"✅ Modelo guardado en {SCORING_MODEL_PATH}")
    return model


def score_market(market: dict) -> float:
    """
    Puntúa un mercado con el modelo local.
    Retorna probabilidad estimada de YES (0-1).
    """
    import pickle

    if not SCORING_MODEL_PATH.exists():
        return 0.5  # sin modelo, retornar 50%

    try:
        with open(SCORING_MODEL_PATH, "rb") as f:
            model = pickle.load(f)

        X = np.array([[
            market.get("yes_price", 0.5),
            np.log1p(market.get("volume", 0)),
            np.log1p(market.get("liquidity", 0)),
        ]])

        prob = model.predict_proba(X)[0][1]
        return float(prob)

    except Exception as e:
        log.warning(f"Error en scoring: {e}")
        return 0.5


# ─── 4. PROMPT ENRIQUECIDO PARA CLAUDE ────────────────────────────────────────

def build_enriched_prompt(market: dict, similar_markets: list[dict], ml_score: float) -> str:
    """
    Construye un prompt para Claude que incluye:
    - Datos del mercado actual
    - Contexto histórico de mercados similares (RAG)
    - Score del modelo ML local
    """

    # Contexto histórico
    if similar_markets:
        historical_context = "\n".join([
            f"  - '{m['question']}' → {m['resolved_outcome']} "
            f"(precio final: {m['yes_final_price']:.2f}, similitud: {m['similarity']:.2f})"
            for m in similar_markets[:5]
        ])
        yes_rate = sum(1 for m in similar_markets if m["resolved_outcome"] == "YES") / len(similar_markets)
        historical_section = f"""
CONTEXTO HISTÓRICO (mercados similares ya resueltos):
{historical_context}
→ En mercados similares, YES ganó el {yes_rate:.0%} de las veces.
"""
    else:
        historical_section = "\nCONTEXTO HISTÓRICO: No disponible (ejecutá setup_rag.py primero)\n"

    prompt = f"""Eres un analista experto en mercados de predicción con acceso a datos históricos.

MERCADO A ANALIZAR:
Pregunta: {market.get('question', '')}
Descripción: {market.get('description', 'Sin descripción')[:400]}
Fecha cierre: {market.get('end_date', '')}
Precio actual YES: {market.get('yes_price', 0.5):.2%}
Precio actual NO: {market.get('no_price', 0.5):.2%}
Volumen: ${market.get('volume', 0):,.0f}
Liquidez: ${market.get('liquidity', 0):,.0f}

MODELO ML LOCAL (trained on {Path(HISTORICAL_DB).stat().st_size // 1024 if HISTORICAL_DB.exists() else 0}KB de históricos):
→ Probabilidad estimada YES: {ml_score:.1%}
→ Este score combina precio de mercado, volumen y patrones históricos.
{historical_section}
INSTRUCCIONES:
1. Analizá el mercado considerando TODOS los datos anteriores
2. El modelo ML y el contexto histórico son señales, no verdades absolutas
3. Estimá tu probabilidad real de YES combinando todo
4. Solo recomendá operar si tenés convicción genuina

Respondé ÚNICAMENTE con JSON válido:
{{
  "ai_probability": 0.XX,
  "ml_model_weight": 0.X,
  "historical_weight": 0.X,
  "reasoning": "análisis considerando ML + históricos + conocimiento propio",
  "confidence": "HIGH|MEDIUM|LOW",
  "recommended_side": "YES|NO|PASS",
  "key_factors": ["factor1", "factor2", "factor3"]
}}"""

    return prompt


# ─── 5. SETUP INICIAL ─────────────────────────────────────────────────────────

def full_setup(pages: int = 20):
    """
    Corre el setup completo:
    1. Descarga históricos
    2. Guarda en SQLite
    3. Construye base vectorial
    4. Entrena modelo de scoring
    """
    log.info("=" * 60)
    log.info("SETUP RAG + SCORING — Polymarket Bot")
    log.info("=" * 60)

    # 1. Descargar históricos
    markets = download_historical_markets(pages=pages)

    if not markets:
        log.error("No se pudieron descargar históricos. Verificá tu conexión.")
        return

    # 2. Guardar en SQLite
    save_to_sqlite(markets)

    # 3. Construir base vectorial
    try:
        build_vector_db(markets)
    except Exception as e:
        log.warning(f"ChromaDB/embeddings no disponible: {e}")
        log.warning("Instalá: pip install chromadb sentence-transformers")

    # 4. Entrenar modelo
    try:
        train_scoring_model()
    except Exception as e:
        log.warning(f"Error entrenando modelo: {e}")
        log.warning("Instalá: pip install scikit-learn pandas numpy")

    log.info("\n✅ Setup completo!")
    log.info(f"   SQLite: {HISTORICAL_DB}")
    log.info(f"   ChromaDB: {CHROMA_DIR}")
    log.info(f"   Modelo: {SCORING_MODEL_PATH}")
    log.info("\nAhora el bot usará contexto histórico + ML para mejorar predicciones.")


def refresh_data():
    """Re-descarga y actualiza todo (correr semanalmente)."""
    log.info("Actualizando datos históricos...")
    full_setup(pages=10)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        refresh_data()
    else:
        pages = int(sys.argv[1]) if len(sys.argv) > 1 else 20
        full_setup(pages=pages)
