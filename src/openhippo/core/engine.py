"""HippoEngine — main entry point for all memory operations."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from .embedding import get_embedding, EMBEDDING_DIM
from .storage import Storage, DEFAULT_DB_PATH

logger = logging.getLogger(__name__)


class HippoEngine:
    """Core memory engine. All protocols (REST/MCP/CLI) delegate to this."""

    # Hot memory capacity defaults (chars)
    HOT_MEMORY_LIMIT = 4400
    HOT_USER_LIMIT = 2750
    # Hard cap on entry count regardless of chars (prevents pathological
    # tiny-entry floods that bypass char-based eviction).
    HOT_MAX_ENTRIES = 100

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            from .config import get_config, get
            db_path = get(get_config(), "storage.db_path")
        self.storage = Storage(db_path or DEFAULT_DB_PATH)

    def close(self) -> None:
        self.storage.close()

    # ── F1: Memory Write ──

    # Dedup thresholds
    EXACT_DEDUP = True
    SEMANTIC_DEDUP_THRESHOLD = 0.92

    @staticmethod
    def _normalize_content(content: str) -> str:
        """Normalize content for dedup: strip whitespace and known prefixes."""
        s = content.strip()
        for prefix in ("[hermes-mirror] ", "[migrated-cold] ", "[migrated-hot] "):
            if s.startswith(prefix):
                s = s[len(prefix):]
        return s

    # Track consecutive embedding failures for health monitoring
    _embed_fail_count = 0
    EMBED_FAIL_WARN_THRESHOLD = 3

    def add(self, target: str, content: str, agent_id: str | None = None,
            session_id: str | None = None, scope: str = "agent") -> dict:
        """Add a memory entry to hot storage with dedup.

        Dedup pipeline:
        1. Exact match via content_hash index (hot + cold) → skip
        2. Semantic match (L2 < 0.4 against cold OR hot) → skip
        3. Otherwise → create new entry

        Embedding failures are logged + counted; a warning is emitted on
        consecutive failures so silent dedup degradation is visible.
        """
        self._validate_target(target)
        normalized = self._normalize_content(content)

        # ── Exact dedup: O(1) via content_hash index ──
        if self.EXACT_DEDUP:
            content_hash = hashlib.sha256(normalized.encode()).hexdigest()
            # Check hot (small set, full scan is fine; ~tens of entries)
            for entry in self.storage.hot_list(target):
                if hashlib.sha256(self._normalize_content(entry["content"]).encode()).hexdigest() == content_hash:
                    return {"id": entry["id"], "status": "duplicate", "reason": "exact_hot"}
            # Check cold via indexed lookup
            cold_dup = self.storage.cold_find_by_hash(target, content)
            if cold_dup:
                return {"id": cold_dup["id"], "status": "duplicate", "reason": "exact_cold"}

        # ── Semantic dedup: cold + hot ──
        vec = get_embedding(normalized)
        if vec:
            HippoEngine._embed_fail_count = 0  # reset
            similar = self.storage.vec_search(vec, target, limit=1)
            if similar:
                distance = similar[0].get("vec_distance", 999)
                # nomic-embed-text (normalized): L2 distance < 0.4 ≈ cosine > 0.92
                if distance < 0.4:
                    return {
                        "id": similar[0]["id"],
                        "status": "similar",
                        "reason": "semantic_cold",
                        "distance": round(distance, 4),
                    }
            # Hot semantic dedup (opt-in: O(N) ollama calls per write).
            # Disabled by default to honor F1 <50ms target. Enable via
            # config.semantic_dedup_hot=true when accuracy > latency.
            hot_match = None
            if getattr(getattr(self, "config", None), "semantic_dedup_hot", False):
                hot_match = self._semantic_match_hot(target, vec)
            if hot_match:
                return hot_match
        else:
            HippoEngine._embed_fail_count += 1
            if HippoEngine._embed_fail_count >= self.EMBED_FAIL_WARN_THRESHOLD:
                logger.warning(
                    "Embedding backend has failed %d consecutive times — "
                    "semantic dedup is currently DEGRADED. Check Ollama / SentenceTransformer.",
                    HippoEngine._embed_fail_count,
                )

        result = self.storage.hot_add(target, content, agent_id=agent_id, session_id=session_id, scope=scope)

        # ── Auto-eviction: archive oldest entries when over capacity ──
        # Dual constraint: by chars (context-window aware) AND by entry count
        # (defense against many tiny entries).
        limit = self.HOT_MEMORY_LIMIT if target == "memory" else self.HOT_USER_LIMIT
        current_chars = self.storage.hot_chars(target)
        current_count = len(self.storage.hot_list(target))
        if current_chars > limit or current_count > self.HOT_MAX_ENTRIES:
            self._evict_hot(target, current_chars, limit)

        return result

    def _semantic_match_hot(self, target: str, query_vec: list[float],
                            threshold: float = 0.4) -> dict | None:
        """Check semantic similarity against hot entries (compute embeddings on-the-fly).

        Hot is small (tens of entries), so on-demand embedding is acceptable.
        Returns a duplicate-style dict if a near match is found, else None.
        """
        if not query_vec:
            return None
        # Cosine similarity between two unit vectors u, v: cos = sum(u_i * v_i)
        # L2 distance d = sqrt(2 - 2*cos), so cos = 1 - d^2/2.
        # Threshold 0.4 ≈ cos > 0.92.
        for entry in self.storage.hot_list(target):
            evec = get_embedding(self._normalize_content(entry["content"]))
            if not evec or len(evec) != len(query_vec):
                continue
            cos = sum(a * b for a, b in zip(query_vec, evec))
            d = (2.0 - 2.0 * cos) ** 0.5 if cos < 1 else 0.0
            if d < threshold:
                return {
                    "id": entry["id"],
                    "status": "similar",
                    "reason": "semantic_hot",
                    "distance": round(d, 4),
                }
        return None

    def _evict_hot(self, target: str, current_chars: int, limit: int) -> None:
        """Archive oldest hot entries until under capacity (with 10% headroom).

        Stops when BOTH chars-budget AND entry-count are within target.
        """
        target_chars = int(limit * 0.9)  # evict to 90% to avoid thrashing
        target_entries = int(self.HOT_MAX_ENTRIES * 0.9)
        entries = self.storage.hot_list(target)  # sorted by sort_order, created_at
        current_count = len(entries)
        evicted = 0
        for entry in entries:  # oldest first
            if current_chars <= target_chars and current_count <= target_entries:
                break
            entry_len = len(entry["content"])
            # Archive to cold storage
            try:
                removed = self.storage.hot_remove(target, entry["content"][:60])
                if "error" not in removed:
                    cold_result = self.storage.cold_add(
                        target=target,
                        content=removed["content"],
                        source="evicted",
                        archived_from=removed["id"],
                    )
                    self._embed_or_enqueue(cold_result["id"], cold_result.get("content") or removed["content"])
                    self.storage._log("evict", removed["id"], {"cold_id": cold_result["id"]})
                    current_chars -= entry_len
                    current_count -= 1
                    evicted += 1
            except Exception as e:
                logger.warning("Eviction failed for entry %s: %s", entry["id"], e)
                break
        if evicted:
            logger.info("Evicted %d entries from hot/%s, chars: %d→%d (limit=%d)",
                       evicted, target, current_chars + sum(len(e["content"]) for e in entries[:evicted]), current_chars, limit)

    # ── F2: Memory Search (hybrid: FTS5 + vector + RRF) ──

    def search(self, query: str, target: str | None = None,
               source: str = "all", limit: int = 20,
               mode: str = "hybrid") -> dict:
        """Search memories across hot and cold storage.
        
        Args:
            query: search keywords
            target: filter by 'memory' or 'user', or None for both
            source: 'all', 'hot', 'cold'
            limit: max results
            mode: 'hybrid' (FTS+vec RRF), 'fts', 'vector'
        """
        results = {"query": query, "hot": [], "cold": [], "mode": mode}

        if source in ("all", "hot"):
            hot_entries = self.storage.hot_list(target)
            query_lower = query.lower()
            keywords = query_lower.split()
            for entry in hot_entries:
                content_lower = entry["content"].lower()
                if any(kw in content_lower for kw in keywords):
                    results["hot"].append(entry)

        if source in ("all", "cold"):
            if mode == "fts":
                results["cold"] = self.storage.cold_search(query, target, limit)
            elif mode == "vector":
                results["cold"] = self._vec_search(query, target, limit)
            else:  # hybrid
                results["cold"] = self._hybrid_search(query, target, limit)

        results["total"] = len(results["hot"]) + len(results["cold"])
        return results

    def _vec_search(self, query: str, target: str | None, limit: int) -> list[dict]:
        """Pure vector search."""
        vec = get_embedding(query)
        if not vec:
            logger.warning("Embedding failed, falling back to FTS")
            return self.storage.cold_search(query, target, limit)
        return self.storage.vec_search(vec, target, limit)

    def _hybrid_search(self, query: str, target: str | None, limit: int,
                       rrf_k: int = 60) -> list[dict]:
        """Hybrid search: FTS5 + vector, fused with RRF (Reciprocal Rank Fusion).
        
        Score = Σ 1/(k + rank_i) for each retrieval path.
        """
        # FTS path
        fts_results = self.storage.cold_search(query, target, limit)
        
        # Vector path
        vec = get_embedding(query)
        vec_results = self.storage.vec_search(vec, target, limit) if vec else []

        if not vec_results:
            # No embeddings available, return FTS only
            return fts_results

        # RRF fusion
        scores: dict[str, float] = {}
        entries: dict[str, dict] = {}

        for rank, r in enumerate(fts_results):
            mid = r["id"]
            scores[mid] = scores.get(mid, 0) + 1.0 / (rrf_k + rank)
            entries[mid] = r

        for rank, r in enumerate(vec_results):
            mid = r["id"]
            scores[mid] = scores.get(mid, 0) + 1.0 / (rrf_k + rank)
            if mid not in entries:
                entries[mid] = r

        # Sort by RRF score descending
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
        result = []
        for mid, score in ranked:
            entry = entries[mid]
            entry["rrf_score"] = round(score, 6)
            result.append(entry)
        return result

    # ── F3: Memory Replace ──

    def replace(self, target: str, old_text: str, new_content: str) -> dict:
        """Replace a hot memory entry identified by old_text."""
        self._validate_target(target)
        return self.storage.hot_replace(target, old_text, new_content)

    # ── F4: Memory Remove ──

    def remove(self, target: str, old_text: str) -> dict:
        """Remove a hot memory entry."""
        self._validate_target(target)
        return self.storage.hot_remove(target, old_text)

    # ── F5: Archive (hot→cold) ──

    def archive(self, target: str, old_text: str) -> dict:
        """Move a hot memory to cold storage."""
        self._validate_target(target)
        result = self.storage.archive(target, old_text)
        # Auto-embed the archived entry
        if "cold_id" in result:
            self._embed_or_enqueue(result["cold_id"])
        return result

    def cold_add(self, target: str, content: str, source: str = "manual",
                 tags: list[str] | None = None, metadata: dict | None = None,
                 agent_id: str | None = None, scope: str = "agent",
                 session_id: str | None = None, dedup: bool = True) -> dict:
        """Insert directly into cold storage (e.g. session snapshots, audit records)
        without going through hot. Auto-embeds for semantic search.

        With dedup=True (default), returns existing entry on exact content match.
        """
        self._validate_target(target)
        normalized = self._normalize_content(content)
        if dedup:
            existing = self.storage.cold_find_by_hash(target, normalized)
            if existing:
                return {"id": existing["id"], "status": "duplicate", "reason": "exact_cold"}
        result = self.storage.cold_add(
            target=target, content=content, source=source,
            tags=tags, metadata=metadata,
            agent_id=agent_id, scope=scope, session_id=session_id,
        )
        embed_status = self._embed_or_enqueue(result["id"], content)
        result["embedding_status"] = embed_status
        return result

    def _embed_cold_entry(self, memory_id: str) -> bool:
        """Generate and store embedding for a cold memory entry. Returns success."""
        entry = self.storage.cold_get(memory_id)
        if not entry:
            return False
        vec = get_embedding(entry["content"])
        if vec:
            self.storage.vec_store(memory_id, vec)
            return True
        logger.warning("Failed to embed cold entry %s", memory_id)
        return False

    def _embed_or_enqueue(self, memory_id: str, content: str | None = None) -> str:
        """Embed a cold entry now, or enqueue for the async worker.

        Behavior controlled by env OPENHIPPO_ASYNC_EMBED (default ON).
        Returns 'sync' or 'pending'. Tests can call embed_drain_now() to flush.
        """
        from . import embed_queue
        if not embed_queue.is_async_enabled():
            self._embed_cold_entry(memory_id)
            return "sync"
        if content is None:
            entry = self.storage.cold_get(memory_id)
            if not entry:
                return "sync"  # nothing to enqueue
            content = entry["content"]
        try:
            embed_queue.enqueue(self.storage._get_conn(), "cold_memory", memory_id, content)
            return "pending"
        except Exception as e:
            # Queue failure must not break writes — fall back to sync.
            logger.warning("enqueue failed for %s, falling back to sync embed: %s", memory_id, e)
            self._embed_cold_entry(memory_id)
            return "sync"

    def embed_drain_now(self, max_jobs: int = 1000) -> dict:
        """Synchronously drain the embedding job queue.

        Test/CLI helper. Production traffic uses the async worker started in
        the FastAPI lifespan. Returns counts.
        """
        from . import embed_queue
        conn = self.storage._get_conn()
        done, failed = 0, 0
        for _ in range(max_jobs):
            job = embed_queue.fetch_one_pending(conn)
            if not job:
                break
            try:
                vec = get_embedding(job["content"])
                if not vec:
                    raise RuntimeError("empty embedding")
                self.storage.vec_store(job["target_id"], vec)
                embed_queue.mark_done(conn, job["id"])
                done += 1
            except Exception as e:
                embed_queue.mark_failed(conn, job["id"], str(e))
                failed += 1
        return {"done": done, "failed": failed}

    def embed_all_cold(self) -> dict:
        """Backfill embeddings for all cold memories missing them."""
        conn = self.storage._get_conn()
        rows = conn.execute(
            "SELECT id, content FROM cold_memory WHERE id NOT IN (SELECT memory_id FROM cold_embeddings)"
        ).fetchall()
        success, failed = 0, 0
        for row in rows:
            vec = get_embedding(row["content"])
            if vec:
                self.storage.vec_store(row["id"], vec)
                success += 1
            else:
                failed += 1
        return {"embedded": success, "failed": failed, "skipped_existing": self.storage.vec_count() - success}

    # ── Promote (cold→hot) ──

    def promote(self, memory_id: str) -> dict:
        """Move a cold memory back to hot storage."""
        return self.storage.promote(memory_id)

    # ── Cold operations ──

    def cold_search(self, query: str, target: str | None = None, limit: int = 20) -> list[dict]:
        """Search cold memory only."""
        return self.storage.cold_search(query, target, limit)

    def cold_get(self, memory_id: str) -> dict | None:
        """Get a single cold memory by ID."""
        return self.storage.cold_get(memory_id)

    def cold_update(self, memory_id: str, content: str) -> dict:
        """Update a cold memory's content and refresh embedding."""
        return self.storage.cold_update(memory_id, content)

    def cold_delete(self, memory_id: str) -> dict:
        """Permanently delete a cold memory."""
        return self.storage.cold_delete(memory_id)

    def cold_timeline(self, target: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        """Browse cold memories by time (newest first)."""
        return self.storage.cold_timeline(target, limit, offset)

    # ── Hot memory bulk read (for injection) ──

    def get_hot(self, target: str) -> list[dict]:
        """Get all hot memory entries for a target."""
        self._validate_target(target)
        return self.storage.hot_list(target)

    def get_hot_text(self, target: str, delimiter: str = "\n§\n") -> str:
        """Get hot memory as delimited text (for context injection)."""
        entries = self.get_hot(target)
        return delimiter.join(e["content"] for e in entries)

    # ── Stats ──

    def stats(self) -> dict:
        """Get memory statistics."""
        s = self.storage.stats()
        s["hot_memory_usage"] = f"{s['hot_memory_chars']}/{self.HOT_MEMORY_LIMIT}"
        s["hot_user_usage"] = f"{s['hot_user_chars']}/{self.HOT_USER_LIMIT}"
        return s

    def overview(self) -> dict:
        """End-user facing aggregate dashboard data."""
        return self.storage.overview()

    def unified_timeline(self, target: str | None = None,
                         agent_id: str | None = None,
                         limit: int = 50, offset: int = 0,
                         date_from: float | None = None,
                         date_to: float | None = None) -> list[dict]:
        """Combined hot+cold timeline for end-user UI."""
        return self.storage.unified_timeline(target, agent_id, limit, offset, date_from, date_to)

    def unified_count(self, target: str | None = None,
                      agent_id: str | None = None,
                      date_from: float | None = None,
                      date_to: float | None = None) -> int:
        return self.storage.unified_count(target, agent_id, date_from, date_to)

    def daily_calendar(self, target: str | None = None,
                       agent_id: str | None = None,
                       days: int = 365) -> list[dict]:
        return self.storage.daily_calendar(target, agent_id, days)

    # ── Logs ──

    def get_logs(self, limit: int = 50) -> list[dict]:
        """Get consolidation/operation logs."""
        return self.storage.get_logs(limit)

    # ── Helpers ──

    @staticmethod
    def _validate_target(target: str) -> None:
        if target not in ("memory", "user"):
            raise ValueError(f"target must be 'memory' or 'user', got '{target}'")
