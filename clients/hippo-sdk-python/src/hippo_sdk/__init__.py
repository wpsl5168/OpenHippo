"""hippo-sdk — universal client for the Hippo (海马体) memory engine.

Public API (v0.2, ROI-quick-wins):
    HippoClient    — high-level client
    HippoConfig    — config dataclass
    MemoryEntry    — typed result row
    RecallResult   — typed recall response
    HippoMetrics   — observability counters & latencies
    RecallCache    — LRU+TTL recall cache (auto-attached by client)

Architecture:
    transport.HippoTransport — HTTP transport (urllib, stdlib only)
    wal.WriteAheadLog        — file-backed retry queue
    cache.RecallCache        — local LRU for sub-ms repeat queries
    metrics.HippoMetrics     — process-local observability

Future phases will add:
    P3: outbox.AsyncOutbox   — SQLite-backed async fire-and-forget
    P3: scope/agent_id wire  — multi-tenant filter on server
    P4: SoT mirror           — local jsonl shadow for disaster recovery
"""

from .cache import RecallCache
from .client import HippoClient
from .config import HippoConfig
from .metrics import HippoMetrics
from .types import MemoryEntry, RecallResult

__version__ = "0.2.0"
__all__ = [
    "HippoClient",
    "HippoConfig",
    "MemoryEntry",
    "RecallResult",
    "HippoMetrics",
    "RecallCache",
    "__version__",
]
