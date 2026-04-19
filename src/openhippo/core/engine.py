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

    def add(self, target: str, content: str) -> dict:
        """Add a memory entry to hot storage with dedup.

        Dedup pipeline:
        1. Exact match (SHA-256 of normalized content) → skip
        2. Semantic match (cosine > 0.92 against cold) → skip
        3. Otherwise → create new entry

        Returns:
            {"id": str, "status": "created"/"duplicate"/"similar", ...}
        """
        self._validate_target(target)
        normalized = self._normalize_content(content)

        # ── Exact dedup: check hot + cold ──
        if self.EXACT_DEDUP:
            content_hash = hashlib.sha256(normalized.encode()).hexdigest()
            # Check hot
            for entry in self.storage.hot_list(target):
                if hashlib.sha256(self._normalize_content(entry["content"]).encode()).hexdigest() == content_hash:
                    return {"id": entry["id"], "status": "duplicate", "reason": "exact_hot"}
            # Check cold
            conn = self.storage._get_conn()
            # Use LIKE as fast pre-filter, then exact hash
            rows = conn.execute(
                "SELECT id, content FROM cold_memory WHERE target=?", (target,)
            ).fetchall()
            for row in rows:
                if hashlib.sha256(self._normalize_content(row["content"]).encode()).hexdigest() == content_hash:
                    return {"id": row["id"], "status": "duplicate", "reason": "exact_cold"}

        # ── Semantic dedup: check cold embeddings ──
        vec = get_embedding(normalized)
        if vec:
            similar = self.storage.vec_search(vec, target, limit=1)
            if similar:
                distance = similar[0].get("vec_distance", 999)
                # sqlite-vec returns L2 distance; cosine similarity ≈ 1 - distance/2 for normalized vectors
                # For nomic-embed-text (normalized), distance < 0.4 ≈ cosine > 0.92
                if distance < 0.4:
                    return {
                        "id": similar[0]["id"],
                        "status": "similar",
                        "reason": "semantic_cold",
                        "distance": round(distance, 4),
                    }

        result = self.storage.hot_add(target, content)

        # ── Auto-eviction: archive oldest entries when over capacity ──
        limit = self.HOT_MEMORY_LIMIT if target == "memory" else self.HOT_USER_LIMIT
        current_chars = self.storage.hot_chars(target)
        if current_chars > limit:
            self._evict_hot(target, current_chars, limit)

        return result

    def _evict_hot(self, target: str, current_chars: int, limit: int) -> None:
        """Archive oldest hot entries until under capacity (with 10% headroom)."""
        target_chars = int(limit * 0.9)  # evict to 90% to avoid thrashing
        entries = self.storage.hot_list(target)  # sorted by sort_order, created_at
        evicted = 0
        for entry in entries:  # oldest first
            if current_chars <= target_chars:
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
                    self._embed_cold_entry(cold_result["id"])
                    self.storage._log("evict", removed["id"], {"cold_id": cold_result["id"]})
                    current_chars -= entry_len
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
            self._embed_cold_entry(result["cold_id"])
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

    # ── Logs ──

    def get_logs(self, limit: int = 50) -> list[dict]:
        """Get consolidation/operation logs."""
        return self.storage.get_logs(limit)

    # ── Helpers ──

    @staticmethod
    def _validate_target(target: str) -> None:
        if target not in ("memory", "user"):
            raise ValueError(f"target must be 'memory' or 'user', got '{target}'")
