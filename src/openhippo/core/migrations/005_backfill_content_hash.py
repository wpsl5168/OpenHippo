"""Migration 005: backfill content_hash for existing cold_memory rows.

Computes SHA-256 of normalized content for every row where content_hash IS NULL.
Idempotent: skips rows already populated.
"""

from __future__ import annotations

import hashlib
import sqlite3


# Mirror engine._normalize_content to avoid circular import at migration time.
_PREFIXES = ("[hermes-mirror] ", "[migrated-cold] ", "[migrated-hot] ")


def _normalize(s: str) -> str:
    s = s.strip()
    for p in _PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
    return s


def upgrade(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, content FROM cold_memory WHERE content_hash IS NULL"
    ).fetchall()
    for row in rows:
        mid = row[0] if not hasattr(row, "keys") else row["id"]
        content = row[1] if not hasattr(row, "keys") else row["content"]
        h = hashlib.sha256(_normalize(content).encode()).hexdigest()
        conn.execute(
            "UPDATE cold_memory SET content_hash=? WHERE id=?", (h, mid)
        )
