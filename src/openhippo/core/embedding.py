"""Embedding abstraction layer — pluggable backends, local-first."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import struct
import threading
import urllib.error
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


class EmbeddingVector(list):
    """List-compatible vector with the provenance captured BEFORE inference."""

    def __init__(self, values, *, model: str, space_id: str):
        super().__init__(values)
        self.model = model
        self.space_id = space_id

    def copy(self):
        return EmbeddingVector(self, model=self.model, space_id=self.space_id)


def validate_vector(vec, dimension: int = EMBEDDING_DIM) -> list[float]:
    """Reject malformed/non-finite vectors, including float32 overflow."""
    if not isinstance(vec, (list, tuple)) or len(vec) != dimension:
        raise ValueError(f"embedding must contain exactly {dimension} values")
    try:
        if any(isinstance(x, (bool, str, bytes)) for x in vec):
            raise ValueError("embedding values must be numbers")
        values = [float(x) for x in vec]
        if not all(math.isfinite(x) for x in values):
            raise ValueError("embedding contains non-finite values")
        packed = struct.pack(f"<{dimension}f", *values)
        if not all(math.isfinite(x) for x in struct.unpack(f"<{dimension}f", packed)):
            raise ValueError("embedding overflows float32")
        return values
    except (TypeError, OverflowError, struct.error) as exc:
        raise ValueError("embedding values must be finite float32 numbers") from exc


def provider_identity(provider) -> tuple[str, str]:
    model = getattr(provider, "model", None) or getattr(provider, "_model_name", None)
    if not isinstance(model, str) or not model:
        raise ValueError("embedding provider must declare a model")
    # Endpoint, backend implementation and preprocessing are part of the space;
    # identical dimensions/model names alone are NOT compatibility evidence.
    descriptor = {
        "provider": f"{type(provider).__module__}.{type(provider).__qualname__}",
        "model": model,
        "endpoint": getattr(provider, "base_url", "").rstrip("/"),
        "dimension": provider.dimension,
        "preprocessing": getattr(provider, "space_revision", "openhippo-v1"),
        "max_prompt_chars": getattr(provider, "MAX_PROMPT_CHARS", None),
    }
    space_id = "v1:" + hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    return model, space_id


def _cache_key(text: str, space_id: str) -> str:
    return hashlib.sha256((space_id + "\0" + text).encode("utf-8")).hexdigest()


def _cache_get(text: str, space_id: str) -> Optional[list[float]]:
    key = _cache_key(text, space_id)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            _cache_stats["hits"] += 1
            return _cache[key].copy()
        _cache_stats["misses"] += 1
        return None


def _cache_put(text: str, vec: list[float]) -> None:
    key = _cache_key(text, vec.space_id)
    with _cache_lock:
        _cache[key] = vec.copy()
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


class CopilotProvider(EmbeddingProvider):
    """GitHub Copilot embedding backend (text-embedding-3-small @ 768 dims).

    Uses the long-lived ``gho_*`` OAuth token from Hermes' ``~/.hermes/.env``
    directly as a Bearer credential against ``api.githubcopilot.com/embeddings``.
    No token exchange / refresh needed (the raw gho_ token is accepted directly).

    Field-verified quirks (2026-07):
    - ``input`` MUST be a JSON array; a bare string returns HTTP 400.
    - ``dimensions=768`` fits the sqlite-vec float[768] SHAPE only; it does
      not establish compatibility with an existing model's vector space.
    - Editor headers (Copilot-Integration-Id etc.) are required for auth.
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        dimensions: int = EMBEDDING_DIM,  # 768
        base_url: str = "https://api.githubcopilot.com",
        timeout: float = 30.0,
        token: Optional[str] = None,
        env_path: str = "~/.hermes/.env",
    ):
        self.model = model
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._env_path = env_path
        self._token = token or self._load_token()

    def _load_token(self) -> Optional[str]:
        import os
        # Prefer live env, fall back to parsing Hermes .env
        tok = os.environ.get("COPILOT_GITHUB_TOKEN")
        if tok:
            return tok.strip().strip('"').strip("'")
        try:
            p = os.path.expanduser(self._env_path)
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("COPILOT_GITHUB_TOKEN="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception as e:
            logger.warning("CopilotProvider: cannot read token from %s: %s", self._env_path, e)
        return None

    @property
    def dimension(self) -> int:
        return self.dimensions

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Editor-Version": "vscode/1.95.0",
            "Editor-Plugin-Version": "copilot-chat/0.22.0",
            "Copilot-Integration-Id": "vscode-chat",
            "User-Agent": "GitHubCopilotChat/0.22.0",
        }

    def _post_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        if not self._token:
            logger.warning("CopilotProvider: no token available")
            return [None] * len(texts)
        payload = json.dumps({
            "model": self.model,
            "input": texts,               # MUST be array
            "dimensions": self.dimensions,
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
            items = sorted(data.get("data", []), key=lambda d: d["index"])
            if [it["index"] for it in items] != list(range(len(texts))):
                raise ValueError("embedding response has missing/duplicate/out-of-range indices")
            out: list[Optional[list[float]]] = []
            for it in items:
                vec = it.get("embedding")
                out.append(_l2_normalize(validate_vector(vec, self.dimension)))
            return out
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            logger.warning("Copilot embedding HTTP %s: %s", e.code, body)
            return [None] * len(texts)
        except Exception as e:
            logger.warning("Copilot embedding failed: %s", e)
            return [None] * len(texts)

    def embed(self, text: str) -> Optional[list[float]]:
        if not text:
            return None
        return self._post_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[Optional[list[float]]]:
        if not texts:
            return []
        # Copilot accepts multi-input; keep batches modest to bound latency/payload.
        BATCH = 64
        results: list[Optional[list[float]]] = []
        for i in range(0, len(texts), BATCH):
            results.extend(self._post_batch(texts[i:i + BATCH]))
        return results


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

    # nomic-embed-text 默认 ctx ≈ 2048 token；中英混合保守取 ~1800 chars,超出 Ollama 直接返 500。
    MAX_PROMPT_CHARS = 1800

    def _post(self, prompt: str) -> Optional[list[float]]:
        payload = json.dumps({"model": self.model, "prompt": prompt}).encode()
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
            return None

    def embed(self, text: str) -> Optional[list[float]]:
        if not text:
            return None
        # 主截断 + 自适应回退：超长内容截到 MAX_PROMPT_CHARS,如仍 500 则继续减半,直到 500 chars 放弃。
        attempt_lens = [self.MAX_PROMPT_CHARS, 1200, 800, 400]
        attempt_lens = [n for n in attempt_lens if n <= self.MAX_PROMPT_CHARS] or [self.MAX_PROMPT_CHARS]
        for n in attempt_lens:
            prompt = text[:n] if len(text) > n else text
            try:
                return self._post(prompt)
            except urllib.error.HTTPError as e:
                if e.code == 500 and n > 500:
                    logger.warning("Ollama 500 at prompt len=%d, retry shorter", n)
                    continue
                logger.warning("Ollama embedding failed: %s", e)
                return None
            except Exception as e:
                logger.warning("Ollama embedding failed: %s", e)
                return None
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
                return validate_vector(vec, self.dimension)
            logger.warning("Unexpected dim: %d (expected %d)", len(vec), self.dimension)
            return None
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
            return [validate_vector(v.tolist(), self.dimension) for v in vecs]
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

    if provider_type == "copilot":
        return CopilotProvider(
            model=get(cfg, "embedding.copilot.model", "text-embedding-3-small"),
            dimensions=int(get(cfg, "embedding.copilot.dimensions", EMBEDDING_DIM)),
            base_url=get(cfg, "embedding.copilot.base_url", "https://api.githubcopilot.com"),
            env_path=get(cfg, "embedding.copilot.env_path", "~/.hermes/.env"),
        )

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

def get_embedding(text: str, *, provider=None) -> Optional[list[float]]:
    """Get embedding vector with LRU cache. Uses the configured provider."""
    if not text:
        return None
    provider = provider if provider is not None else get_provider()
    model, space_id = provider_identity(provider)
    cached = _cache_get(text, space_id)
    if cached is not None:
        return cached
    vec = provider.embed(text)
    if vec is not None:
        try:
            vec = EmbeddingVector(validate_vector(vec, provider.dimension), model=model, space_id=space_id)
            if provider_identity(provider) != (model, space_id):
                raise ValueError("provider configuration changed during inference")
        except ValueError as exc:
            logger.warning("Invalid embedding: %s", exc)
            return None
        _cache_put(text, vec)
    return vec


def get_embeddings_batch(texts: list[str]) -> list[Optional[list[float]]]:
    """Get embeddings for multiple texts with LRU cache."""
    provider = get_provider()
    model, space_id = provider_identity(provider)
    results: list[Optional[list[float]]] = [None] * len(texts)
    miss_idx: list[int] = []
    miss_texts: list[str] = []
    for i, t in enumerate(texts):
        if not t:
            continue
        cached = _cache_get(t, space_id)
        if cached is not None:
            results[i] = cached
        else:
            miss_idx.append(i)
            miss_texts.append(t)
    if miss_texts:
        fresh = provider.embed_batch(miss_texts)
        if len(fresh) != len(miss_texts) or provider_identity(provider) != (model, space_id):
            logger.warning("Invalid embedding batch size/provider change")
            return results
        for j, vec in zip(miss_idx, fresh):
            if vec is not None:
                try:
                    vec = EmbeddingVector(validate_vector(vec, provider.dimension), model=model, space_id=space_id)
                except ValueError:
                    vec = None
            results[j] = vec
            if vec is not None:
                _cache_put(texts[j], vec)
    return results


# ── Utility ──

def _l2_normalize(vec: list[float]) -> list[float]:
    vec = validate_vector(vec, len(vec))
    norm = math.hypot(*vec)
    if norm > 0:
        return [x / norm for x in vec]
    return vec
