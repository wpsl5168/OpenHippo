"""WriteAheadLog — file-backed retry queue for failed writes.

Lifted as-is (semantics) from the legacy plugin's _wal_* helpers and
hardened a bit:
  - All paths configurable via HippoConfig
  - Rotation moves expired entries to a dead-letter file
  - Replay drains entries; per-entry retry count caps at config.wal_max_retries

Threadsafety: file locking is intentionally NOT used — the legacy plugin
ran single-threaded inside hooks, and P2 will replace WAL with a SQLite
outbox that handles concurrency natively. Keep this file simple.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import HippoConfig

logger = logging.getLogger("hippo_sdk.wal")


class WriteAheadLog:
    """File-backed JSONL queue of failed writes for later retry.

    Entry shape: {"path": str, "payload": dict, "ts": iso, "retries": int}
    """

    def __init__(self, config: HippoConfig):
        self.config = config
        self.dir: Path = config.wal_dir
        self.file: Path = self.dir / "wal.jsonl"
        self.dead: Path = self.dir / "wal.dead.jsonl"
        self.max_bytes: int = config.wal_max_bytes
        self.max_retries: int = config.wal_max_retries

    # ── append ────────────────────────────────────────────────────────────

    def append(self, path: str, payload: dict) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            entry = {
                "path": path,
                "payload": payload,
                "ts": datetime.now().isoformat(),
                "retries": 0,
            }
            with open(self.file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if self.file.exists() and self.file.stat().st_size > self.max_bytes:
                self._rotate()
        except Exception as e:
            logger.debug("WAL append failed: %s", e)

    # ── rotation ──────────────────────────────────────────────────────────

    def _rotate(self) -> None:
        try:
            lines = self.file.read_text(encoding="utf-8").splitlines()
            if len(lines) <= 100:
                return
            old, keep = lines[:-100], lines[-100:]
            with open(self.dead, "a", encoding="utf-8") as f:
                for line in old:
                    f.write(line + "\n")
            self.file.write_text("\n".join(keep) + "\n", encoding="utf-8")
        except Exception as e:
            logger.debug("WAL rotate failed: %s", e)

    # ── replay ────────────────────────────────────────────────────────────

    def replay(self, sender: Callable[[str, dict], dict | None]) -> int:
        """Drain pending entries by calling sender(path, payload).

        Returns the number of entries successfully delivered.
        Entries whose retries exceed max_retries are moved to the dead file.
        """
        if not self.file.exists() or self.file.stat().st_size == 0:
            return 0

        try:
            lines = self.file.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            logger.debug("WAL read failed: %s", e)
            return 0

        delivered = 0
        remaining: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # Drop malformed line silently
                continue

            retries = int(entry.get("retries", 0))
            if retries >= self.max_retries:
                self._move_to_dead(line)
                continue

            try:
                result = sender(entry["path"], entry["payload"])
            except Exception as e:
                logger.debug("WAL sender raised: %s", e)
                result = None

            if result is None:
                entry["retries"] = retries + 1
                remaining.append(json.dumps(entry, ensure_ascii=False))
            else:
                delivered += 1

        try:
            if remaining:
                self.file.write_text("\n".join(remaining) + "\n", encoding="utf-8")
            else:
                self.file.write_text("", encoding="utf-8")
        except Exception as e:
            logger.debug("WAL truncate failed: %s", e)

        return delivered

    def _move_to_dead(self, line: str) -> None:
        try:
            with open(self.dead, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    # ── introspection ─────────────────────────────────────────────────────

    def pending_count(self) -> int:
        """Return number of entries currently queued (not perfectly accurate
        if rotated mid-call but good enough for monitoring)."""
        if not self.file.exists():
            return 0
        try:
            return sum(1 for line in self.file.read_text(encoding="utf-8").splitlines() if line.strip())
        except Exception:
            return 0
