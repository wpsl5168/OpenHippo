"""Embedding abstraction layer — pluggable backends, local-first."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import urllib.request
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 768

# ── LRU cache for embeddings (SHA-256 of normalized text → vector) ──
# Avoids redundant Ollama calls for repeated content (very common in dedup checks
# and re-imports). Default 1024 entries ≈ 6MB at 768 floats × 8 bytes.
_CACHE_CAPACITY = 1024
_cache: "OrderedDict[str, list[float]]" = OrderedDict()
_cache_lock = threading.Lock()
_cache_stats = {"hits": 0, "misses": 0}


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_get(text: str) -> Optional[list[float]]:
    key = _cache_key(text)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            _cache_stats["hits"] += 1
            return _cache[key]
        _cache_stats["misses"] += 1
        return None


def _cache_put(text: str, vec: list[float]) -> None:
    key = _cache_key(text)
    with _cache_lock:
        _cache[key] = vec
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_CAPACITY:
            _cache.popitem(last=False)


def cache_stats() -> dict:
    with _cache_lock:
        total = _cache_stats["hits"] + _cache_stats["misses"]
        return {
            "size": len(_cache),
            "capacity": _CACHE_CAPACITY,
            "hits": _cache_stats["hits"],
            "misses": _cache_stats["misses"],
            "hit_rate": round(_cache_stats["hits"] / total, 3) if total else 0.0,
        }


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()
        _cache_stats["hits"] = 0
        _cache_stats["misses"] = 0


class EmbeddingProvider(ABC):
    """Abstract embedding provider interface."""

    @abstractmethod
    def embed(self, text: str) -> Optional[list[float]]:
        """Embed a single text. Returns None on failure."""

    def embed_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        """Embed multiple texts. Default: sequential calls."""
        return [self.embed(t) for t in texts]

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Embedding vector dimension."""


class OllamaProvider(EmbeddingProvider):
    """Ollama-based embedding (requires running Ollama service)."""

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 30.0,
    ):
        self.model = model
        self.base_url = base_url
        self.timeout = timeout

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM

    def embed(self, text: str) -> Optional[list[float]]:
        try:
            payload = json.dumps({"model": self.model, "prompt": text}).encode()
            req = urllib.request.Request(
                f"{self.base_url}/api/embeddings",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
                vec = data.get("embedding")
                if vec and len(vec) == self.dimension:
                    return _l2_normalize(vec)
                logger.warning("Unexpected dim: %d (expected %d)", len(vec) if vec else 0, self.dimension)
                return vec if vec else None
        except Exception as e:
            logger.warning("Ollama embedding failed: %s", e)
            return None


class SentenceTransformerProvider(EmbeddingProvider):
    """SentenceTransformers-based embedding (pure Python, no external service)."""

    def __init__(self, model_name: str = "nomic-ai/nomic-embed-text-v1.5", device: str = "cpu"):
        self._model_name = model_name
        self._device = device
        self._model = None  # lazy load

    def _get_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                logger.info("Loading SentenceTransformer model: %s", self._model_name)
                self._model = SentenceTransformer(self._model_name, trust_remote_code=True, device=self._device)
                logger.info("Model loaded, dimension=%d", self._model.get_embedding_dimension())
            except ImportError:
                raise ImportError(
                    "sentence-transformers is required for local embedding. "
                    "Install with: pip install sentence-transformers"
                )
        return self._model

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM  # nomic-embed-text-v1.5 = 768

    def embed(self, text: str) -> Optional[list[float]]:
        try:
            model = self._get_model()
            # nomic-embed-text requires task prefix
            prefixed = f"search_document: {text}" if "nomic" in self._model_name else text
            vec = model.encode(prefixed, normalize_embeddings=True).tolist()
            if len(vec) == self.dimension:
                return vec
            logger.warning("Unexpected dim: %d (expected %d)", len(vec), self.dimension)
            return vec
        except ImportError:
            raise
        except Exception as e:
            logger.warning("SentenceTransformer embedding failed: %s", e)
            return None

    def embed_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        try:
            model = self._get_model()
            prefix = "search_document: " if "nomic" in self._model_name else ""
            prefixed = [f"{prefix}{t}" for t in texts]
            vecs = model.encode(prefixed, normalize_embeddings=True, batch_size=32)
            return [v.tolist() for v in vecs]
        except Exception as e:
            logger.warning("Batch embedding failed: %s", e)
            return [self.embed(t) for t in texts]


# ── Factory ──

_provider: Optional[EmbeddingProvider] = None


def get_provider() -> EmbeddingProvider:
    """Get the current embedding provider (singleton)."""
    global _provider
    if _provider is None:
        _provider = _create_default_provider()
    return _provider


def set_provider(provider: EmbeddingProvider) -> None:
    """Override the embedding provider (for testing or config)."""
    global _provider
    _provider = provider


def _create_default_provider() -> EmbeddingProvider:
    """Create provider based on config. provider=auto: Ollama → SentenceTransformer."""
    from .config import get_config, get

    cfg = get_config()
    provider_type = get(cfg, "embedding.provider", "auto")

    if provider_type == "ollama":
        return OllamaProvider(
            model=get(cfg, "embedding.ollama.model", "nomic-embed-text"),
            base_url=get(cfg, "embedding.ollama.base_url", "http://127.0.0.1:11434"),
        )

    if provider_type == "local":
        return SentenceTransformerProvider(
            model_name=get(cfg, "embedding.local.model", "nomic-ai/nomic-embed-text-v1.5"),
            device=get(cfg, "embedding.local.device", "cpu"),
        )

    if provider_type == "auto":
        # Try Ollama first (if running)
        ollama_url = get(cfg, "embedding.ollama.base_url", "http://127.0.0.1:11434")
        try:
            req = urllib.request.Request(f"{ollama_url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=2):
                logger.info("Ollama detected, using OllamaProvider")
                return OllamaProvider(
                    model=get(cfg, "embedding.ollama.model", "nomic-embed-text"),
                    base_url=ollama_url,
                )
        except Exception:
            pass

        # Fall back to SentenceTransformer
        try:
            import sentence_transformers  # noqa: F401
            logger.info("Using SentenceTransformerProvider (local)")
            return SentenceTransformerProvider(
                model_name=get(cfg, "embedding.local.model", "nomic-ai/nomic-embed-text-v1.5"),
                device=get(cfg, "embedding.local.device", "cpu"),
            )
        except ImportError:
            pass

    # Last resort: Ollama (will fail at embed time with clear error)
    logger.warning("No embedding backend available. Install sentence-transformers or start Ollama.")
    return OllamaProvider()


# ── Backward-compatible convenience functions ──

def get_embedding(text: str) -> Optional[list[float]]:
    """Get embedding vector with LRU cache. Uses the configured provider."""
    if not text:
        return None
    cached = _cache_get(text)
    if cached is not None:
        return cached
    vec = get_provider().embed(text)
    if vec is not None:
        _cache_put(text, vec)
    return vec


def get_embeddings_batch(texts: list[str]) -> list[Optional[list[float]]]:
    """Get embeddings for multiple texts with LRU cache."""
    results: list[Optional[list[float]]] = [None] * len(texts)
    miss_idx: list[int] = []
    miss_texts: list[str] = []
    for i, t in enumerate(texts):
        if not t:
            continue
        cached = _cache_get(t)
        if cached is not None:
            results[i] = cached
        else:
            miss_idx.append(i)
            miss_texts.append(t)
    if miss_texts:
        fresh = get_provider().embed_batch(miss_texts)
        for j, vec in zip(miss_idx, fresh):
            results[j] = vec
            if vec is not None:
                _cache_put(texts[j], vec)
    return results


# ── Utility ──

def _l2_normalize(vec: list[float]) -> list[float]:
    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        return [x / norm for x in vec]
    return vec
