"""HippoEngine — main entry point for all memory operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .storage import Storage, DEFAULT_DB_PATH


class HippoEngine:
    """Core memory engine. All protocols (REST/MCP/CLI) delegate to this."""

    # Hot memory capacity defaults (chars)
    HOT_MEMORY_LIMIT = 4400
    HOT_USER_LIMIT = 2750

    def __init__(self, db_path: str | Path | None = None):
        self.storage = Storage(db_path or DEFAULT_DB_PATH)

    def close(self) -> None:
        self.storage.close()

    # ── F1: Memory Write ──

    def add(self, target: str, content: str) -> dict:
        """Add a memory entry to hot storage.
        
        Args:
            target: 'memory' or 'user'
            content: memory content text
        Returns:
            {"id": str, "status": "created"}
        """
        self._validate_target(target)
        result = self.storage.hot_add(target, content)
        return result

    # ── F2: Memory Search ──

    def search(self, query: str, target: str | None = None,
               source: str = "all", limit: int = 20) -> dict:
        """Search memories across hot and cold storage.
        
        Args:
            query: search keywords
            target: filter by 'memory' or 'user', or None for both
            source: 'all', 'hot', 'cold'
            limit: max results
        """
        results = {"query": query, "hot": [], "cold": []}

        if source in ("all", "hot"):
            hot_entries = self.storage.hot_list(target)
            query_lower = query.lower()
            keywords = query_lower.split()
            for entry in hot_entries:
                content_lower = entry["content"].lower()
                if any(kw in content_lower for kw in keywords):
                    results["hot"].append(entry)

        if source in ("all", "cold"):
            results["cold"] = self.storage.cold_search(query, target, limit)

        results["total"] = len(results["hot"]) + len(results["cold"])
        return results

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
        return self.storage.archive(target, old_text)

    # ── Promote (cold→hot) ──

    def promote(self, memory_id: str) -> dict:
        """Move a cold memory back to hot storage."""
        return self.storage.promote(memory_id)

    # ── Cold operations ──

    def cold_search(self, query: str, target: str | None = None, limit: int = 20) -> list[dict]:
        """Search cold memory only."""
        return self.storage.cold_search(query, target, limit)

    def cold_delete(self, memory_id: str) -> dict:
        """Permanently delete a cold memory."""
        return self.storage.cold_delete(memory_id)

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
