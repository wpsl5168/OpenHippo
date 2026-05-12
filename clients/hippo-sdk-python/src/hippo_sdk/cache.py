"""Recall LRU cache with TTL.

Keyed by (query, source, mode, limit, target). Returns cached RecallResult
dataclass (immutable enough for our purposes — no in-place mutation in plugin).

Default: 256 entries, 60s TTL. Tunable via HippoConfig.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class RecallCache:
    def __init__(self, capacity: int = 256, ttl_sec: float = 60.0):
        self.capacity = max(1, capacity)
        self.ttl_sec = ttl_sec
        self._store: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def make_key(query: str, source: str, mode: str, limit: int, target: str | None) -> tuple:
        # Truncate query to 256 chars for key stability
        return (query[:256], source, mode, limit, target or "")

    def get(self, key: tuple) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.time() - ts > self.ttl_sec:
                # Expired — evict
                self._store.pop(key, None)
                return None
            # LRU touch
            self._store.move_to_end(key)
            return value

    def set(self, key: tuple, value: Any) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (time.time(), value)
            while len(self._store) > self.capacity:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)
