"""HippoConfig — single source of truth for SDK configuration.

Reads sensible defaults from environment variables but accepts overrides via
constructor. Designed so the same env vars used by the legacy plugin keep
working — backward compatible during P1 migration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class HippoConfig:
    """Configuration for HippoClient.

    All fields can be overridden via env vars (legacy-compatible names).
    """

    # Server
    base_url: str = field(
        default_factory=lambda: os.environ.get("HIPPO_BASE_URL", "http://127.0.0.1:8200")
    )
    bearer_token: str = field(default_factory=lambda: os.environ.get("HIPPO_TOKEN", ""))

    # Identity
    agent_id: str = field(
        default_factory=lambda: os.environ.get("HIPPO_AGENT_ID", "default-agent")
    )

    # Timeouts (seconds) — tightened defaults for "near-zero perceived latency"
    # search_timeout is hard cap; cache miss + slow server = recall returns empty.
    search_timeout: float = field(default_factory=lambda: _env_float("HIPPO_SEARCH_TIMEOUT", 0.5))
    write_timeout: float = field(default_factory=lambda: _env_float("HIPPO_WRITE_TIMEOUT", 5.0))

    # Recall cache (LRU + TTL). Set capacity=0 to disable.
    cache_capacity: int = field(default_factory=lambda: int(os.environ.get("HIPPO_CACHE_CAPACITY", "256")))
    cache_ttl_sec: float = field(default_factory=lambda: _env_float("HIPPO_CACHE_TTL_SEC", 60.0))

    # WAL paths (file-backed retry queue)
    wal_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "HIPPO_WAL_DIR",
                str(Path.home() / ".hermes" / "plugins" / "openhippo"),
            )
        )
    )
    wal_max_bytes: int = 1_000_000  # 1MB rotate threshold
    wal_max_retries: int = 3

    def headers(self) -> dict:
        """Build common request headers including auth + agent identity."""
        h = {"Content-Type": "application/json"}
        if self.bearer_token:
            h["Authorization"] = f"Bearer {self.bearer_token}"
        # P3 will add: h["X-Agent-ID"] = self.agent_id
        # For P1 we keep wire compatibility with current server.
        return h
