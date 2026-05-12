"""HippoMetrics — process-local observability for the SDK.

Tracks recall/write counts, cache hit-rate, latency percentiles, and WAL
fallbacks. Zero external dependencies, thread-safe via lock.

Exposed via HippoClient.stats() — agents can render to dashboards / logs.
"""
from __future__ import annotations

import bisect
import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _LatencyBuffer:
    """Bounded ring buffer of latencies (ms) for percentile calc."""
    cap: int = 500
    samples: deque = field(default_factory=lambda: deque(maxlen=500))

    def add(self, ms: float) -> None:
        self.samples.append(ms)

    def percentile(self, p: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        k = int(round((p / 100.0) * (len(ordered) - 1)))
        return round(ordered[k], 2)

    def avg(self) -> float:
        if not self.samples:
            return 0.0
        return round(sum(self.samples) / len(self.samples), 2)


class HippoMetrics:
    """Process-local counters & latency tracking.

    Use via context manager:
        with metrics.timer('recall'):
            ...

    Or manually:
        metrics.incr('recall_count')
        metrics.observe('recall_latency_ms', 12.3)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {
            "recall_count": 0,
            "recall_cache_hit": 0,
            "recall_cache_miss": 0,
            "recall_timeout": 0,
            "recall_empty": 0,
            "write_count": 0,
            "write_wal_fallback": 0,
            "write_failed": 0,
            "memories_injected_total": 0,
        }
        self._latencies: dict[str, _LatencyBuffer] = {
            "recall_latency_ms": _LatencyBuffer(),
            "write_latency_ms": _LatencyBuffer(),
        }
        self._started_at = time.time()

    # ── recording ────────────────────────────────────────────────────────

    def incr(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            buf = self._latencies.get(name)
            if buf is None:
                buf = self._latencies[name] = _LatencyBuffer()
            buf.add(value)

    def timer(self, name: str):
        """Context manager: with metrics.timer('recall'): ..."""
        return _TimerCtx(self, f"{name}_latency_ms")

    # ── reading ──────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return a JSON-friendly snapshot of all metrics."""
        with self._lock:
            counters = dict(self._counters)
            latencies = {
                name: {
                    "count": len(buf.samples),
                    "avg": buf.avg(),
                    "p50": buf.percentile(50),
                    "p95": buf.percentile(95),
                    "p99": buf.percentile(99),
                }
                for name, buf in self._latencies.items()
            }
        # Derived
        total_recalls = counters["recall_cache_hit"] + counters["recall_cache_miss"]
        cache_hit_rate = (
            round(counters["recall_cache_hit"] / total_recalls, 3)
            if total_recalls else 0.0
        )
        return {
            "uptime_sec": round(time.time() - self._started_at, 1),
            "counters": counters,
            "cache_hit_rate": cache_hit_rate,
            "latencies": latencies,
        }

    def reset(self) -> None:
        with self._lock:
            for k in self._counters:
                self._counters[k] = 0
            for buf in self._latencies.values():
                buf.samples.clear()
            self._started_at = time.time()


class _TimerCtx:
    def __init__(self, metrics: HippoMetrics, name: str):
        self.metrics = metrics
        self.name = name
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed_ms = (time.perf_counter() - self.t0) * 1000
        self.metrics.observe(self.name, elapsed_ms)
        return False  # don't suppress
