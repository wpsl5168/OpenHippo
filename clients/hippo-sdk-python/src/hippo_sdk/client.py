"""HippoClient — high-level entry point for any agent.

Design:
  * ZERO non-stdlib dependencies.
  * NEVER raises into agent code — every public method swallows errors
    and returns a sensible default (None / empty list / no-op).
  * Backward compatible with the legacy plugin's HTTP shape; once the
    server gains agent_id/scope fields (P3) we extend payloads here.

Public API (P1):
  remember(content, target='memory', target_kind='hot') -> bool
  recall(query, source='cold', mode='hybrid', limit=5) -> RecallResult
  replace(target, old_text, new_content) -> bool
  remove(target, old_text) -> bool
  archive(target, old_text) -> bool
  promote(old_text) -> bool
  cold_add(content, source='manual', tags=None, metadata=None,
           scope='agent', session_id=None, dedup=True) -> bool
  drain_wal() -> int   # explicit retry trigger for hooks
  health() -> bool
"""

from __future__ import annotations

import logging
from typing import Any

from .cache import RecallCache
from .config import HippoConfig
from .metrics import HippoMetrics
from .transport import HippoTransport
from .types import RecallResult
from .wal import WriteAheadLog

logger = logging.getLogger("hippo_sdk.client")


class HippoClient:
    """High-level client. Construct once per agent process, reuse forever."""

    def __init__(
        self,
        config: HippoConfig | None = None,
        *,
        transport: HippoTransport | None = None,
        wal: WriteAheadLog | None = None,
        cache: RecallCache | None = None,
        metrics: HippoMetrics | None = None,
    ):
        self.config = config or HippoConfig()
        self.transport = transport or HippoTransport(self.config)
        self.wal = wal or WriteAheadLog(self.config)
        self.metrics = metrics or HippoMetrics()
        if cache is not None:
            self.cache = cache
        elif self.config.cache_capacity > 0:
            self.cache = RecallCache(
                capacity=self.config.cache_capacity,
                ttl_sec=self.config.cache_ttl_sec,
            )
        else:
            self.cache = None

    # ── internal: post with WAL fallback ──────────────────────────────────

    def _post_or_wal(self, path: str, payload: dict, timeout: float | None = None) -> dict | None:
        with self.metrics.timer("write"):
            result = self.transport.post(path, payload, timeout=timeout)
        if result is None:
            self.wal.append(path, payload)
            self.metrics.incr("write_wal_fallback")
            return None
        self.metrics.incr("write_count")
        return result

    # ── public API ────────────────────────────────────────────────────────

    def remember(
        self,
        content: str,
        *,
        target: str = "memory",
        agent_id: str | None = None,
    ) -> bool:
        """Add a hot memory entry.

        Returns True if the server acked, False if queued to WAL or rejected.
        """
        if not content or not content.strip():
            return False
        payload: dict[str, Any] = {
            "target": target,
            "content": content,
            "agent_id": agent_id or self.config.agent_id,
        }
        return self._post_or_wal("/v1/memories", payload) is not None

    def recall(
        self,
        query: str,
        *,
        source: str = "cold",
        mode: str = "hybrid",
        limit: int = 5,
        target: str | None = None,
        timeout: float | None = None,
    ) -> RecallResult:
        """Semantic recall. Returns RecallResult; never raises.

        Behavior:
        - LRU cache hit → return immediately (sub-ms)
        - Cache miss → POST with hard timeout (default 500ms)
        - Timeout / network error → return empty RecallResult, increment metric
        - Successful response → cache for ttl_sec
        """
        self.metrics.incr("recall_count")
        if not query or not query.strip():
            return RecallResult()

        # Cache lookup
        cache_key = None
        if self.cache is not None:
            cache_key = RecallCache.make_key(query, source, mode, limit, target)
            cached = self.cache.get(cache_key)
            if cached is not None:
                self.metrics.incr("recall_cache_hit")
                self.metrics.observe("recall_latency_ms", 0.05)  # cache hit ~ negligible
                return cached
            self.metrics.incr("recall_cache_miss")

        payload: dict[str, Any] = {
            "query": query[:500],
            "source": source,
            "mode": mode,
            "limit": limit,
        }
        if target:
            payload["target"] = target

        effective_timeout = timeout if timeout is not None else self.config.search_timeout
        with self.metrics.timer("recall"):
            resp = self.transport.post(
                "/v1/memories/search",
                payload,
                timeout=effective_timeout,
            )

        if resp is None:
            # Network error or timeout — degrade gracefully
            self.metrics.incr("recall_timeout")
            return RecallResult()

        result = RecallResult.from_response(resp)
        if not result.cold and not result.hot:
            self.metrics.incr("recall_empty")
        else:
            # Track inject volume — useful for measuring "memory utility"
            self.metrics.incr("memories_injected_total",
                              n=len(result.cold) + len(result.hot))
        # Cache successful response (including empty — avoid hammering on
        # rare-but-known empty queries)
        if cache_key is not None:
            self.cache.set(cache_key, result)
        return result

    def replace(self, target: str, old_text: str, new_content: str) -> bool:
        if not old_text or not new_content:
            return False
        payload = {"target": target, "old_text": old_text[:500], "new_content": new_content}
        return self._post_or_wal("/v1/memories/replace", payload) is not None

    def remove(self, target: str, old_text: str) -> bool:
        if not old_text:
            return False
        # Note: legacy plugin sends only old_text (no target). Keep wire compat.
        payload = {"old_text": old_text[:200]}
        return self._post_or_wal("/v1/memories/remove", payload) is not None

    def archive(self, target: str, old_text: str) -> bool:
        if not old_text:
            return False
        payload = {"old_text": old_text[:200]}
        return self._post_or_wal("/v1/memories/archive", payload) is not None

    def promote(self, memory_id: str) -> bool:
        """Promote a cold memory to hot. Server expects a cold memory_id."""
        if not memory_id:
            return False
        payload = {"memory_id": memory_id}
        return self._post_or_wal("/v1/memories/promote", payload) is not None

    def cold_add(
        self,
        content: str,
        *,
        source: str = "manual",
        tags: list[str] | None = None,
        metadata: dict | None = None,
        scope: str = "agent",
        session_id: str | None = None,
        dedup: bool = True,
        agent_id: str | None = None,
        target: str = "memory",
        originator: str | None = None,
        channel: str | None = None,
    ) -> bool:
        """Insert directly into cold storage (used for session snapshots)."""
        if not content or not content.strip():
            return False
        payload: dict[str, Any] = {
            "target": target,
            "content": content,
            "source": source,
            "scope": scope,
            "dedup": dedup,
            "agent_id": agent_id or self.config.agent_id,
        }
        if tags:
            payload["tags"] = tags
        if metadata:
            payload["metadata"] = metadata
        if session_id:
            payload["session_id"] = session_id
        if originator:
            payload["originator"] = originator
        if channel:
            payload["channel"] = channel
        return self._post_or_wal("/v1/cold/memories", payload) is not None

    # ── maintenance ───────────────────────────────────────────────────────

    def drain_wal(self) -> int:
        """Replay pending WAL entries. Hook callers should invoke this at
        the start of each hook to opportunistically catch up."""
        return self.wal.replay(self.transport.post)

    def health(self) -> bool:
        return self.transport.healthy()

    def wal_pending(self) -> int:
        return self.wal.pending_count()

    # ── observability ─────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return SDK metrics snapshot. Safe to call any time, no I/O."""
        snap = self.metrics.snapshot()
        if self.cache is not None:
            snap["cache_size"] = self.cache.size()
            snap["cache_capacity"] = self.cache.capacity
            snap["cache_ttl_sec"] = self.cache.ttl_sec
        snap["wal_pending"] = self.wal.pending_count()
        snap["agent_id"] = self.config.agent_id
        return snap

    def reset_stats(self) -> None:
        """Reset metrics counters (cache untouched)."""
        self.metrics.reset()
