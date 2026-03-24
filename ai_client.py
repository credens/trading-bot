"""
AI Client — Anthropic o Ollama
================================
Usa Anthropic si tiene API key, sino usa Ollama local.
Compatible con ambos bots (Binance y Altcoins).
"""

import os
import json
import requests
import logging
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

# Detectar qué usar
USE_ANTHROPIC = bool(ANTHROPIC_KEY and not ANTHROPIC_KEY.startswith("sk-ant-xxx"))
USE_OLLAMA = not USE_ANTHROPIC

if USE_ANTHROPIC:
    import anthropic as anthropic_lib
    _anthropic_client = anthropic_lib.Anthropic(api_key=ANTHROPIC_KEY)
    log.info("AI: usando Anthropic Claude")
else:
    _anthropic_client = None
    log.info(f"AI: usando Ollama ({OLLAMA_MODEL}) en {OLLAMA_HOST}")


def call_ai(prompt: str, max_tokens: int = 600) -> str:
    """
    Llama al LLM disponible y retorna el texto de respuesta.
    Compatible con Anthropic y Ollama.
    """
    if USE_ANTHROPIC:
        return _call_anthropic(prompt, max_tokens)
    else:
        return _call_ollama(prompt)


def _call_anthropic(prompt: str, max_tokens: int) -> str:
    response = _anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


def _call_ollama(prompt: str) -> str:
    """Llama a Ollama local via HTTP."""
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,      # baja temperatura para decisiones consistentes
                    "top_p": 0.9,
                    "num_predict": 600,
                }
            },
            timeout=120  # Ollama puede tardar más que Claude
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.exceptions.ConnectionError:
        log.error("Ollama no está corriendo. Ejecutá: ollama serve")
        raise
    except Exception as e:
        log.error(f"Error llamando Ollama: {e}")
        raise


def parse_json_response(text: str) -> dict:
    """Extrae JSON de la respuesta del LLM (maneja markdown code blocks)."""
    # Limpiar markdown
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            if part.startswith("json"):
                text = part[4:]
                break
            elif "{" in part:
                text = part
                break

    # Encontrar el JSON
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]

    return json.loads(text)


def is_available() -> bool:
    """Verifica si hay algún LLM disponible."""
    if USE_ANTHROPIC:
        return True
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def get_model_info() -> str:
    if USE_ANTHROPIC:
        return "Anthropic Claude Sonnet"
    return f"Ollama {OLLAMA_MODEL} (local)"
