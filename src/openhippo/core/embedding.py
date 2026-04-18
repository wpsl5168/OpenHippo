"""Embedding client — local-first via Ollama, zero external API calls."""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "nomic-embed-text"
EMBEDDING_DIM = 768


def get_embedding(
    text: str,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
    timeout: float = 30.0,
) -> Optional[list[float]]:
    """Get embedding vector from Ollama. Returns None on failure."""
    try:
        payload = json.dumps({"model": model, "prompt": text}).encode()
        req = urllib.request.Request(
            f"{base_url}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            vec = data.get("embedding")
            if vec and len(vec) == EMBEDDING_DIM:
                return vec
            logger.warning("Unexpected embedding dim: %d (expected %d)", len(vec) if vec else 0, EMBEDDING_DIM)
            return vec if vec else None
    except Exception as e:
        logger.warning("Embedding failed: %s", e)
        return None


def get_embeddings_batch(
    texts: list[str],
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_OLLAMA_URL,
) -> list[Optional[list[float]]]:
    """Get embeddings for multiple texts. Returns list aligned with input."""
    return [get_embedding(t, model, base_url) for t in texts]
