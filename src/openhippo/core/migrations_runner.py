"""Schema migration runner.

Idempotent, version-tracked schema migrations for OpenHippo.
Each migration is a numbered SQL or Python file in `migrations/`.

Migration discovery:
- Files named `NNN_description.sql` or `NNN_description.py`
- NNN is the target schema_version (zero-padded 3-digit)
- Runner applies all migrations with version > current schema_version

SQL migrations: executed via `connection.executescript()`.
Python migrations: must define `def upgrade(conn: sqlite3.Connection) -> None`.

Safety:
- Wrapped in transaction per migration (auto-rollback on error)
- Bumps schema_version table after success
- Re-running is a no-op
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.(sql|py)$")


def get_current_version(conn: sqlite3.Connection) -> int:
    """Return current schema version. 0 if table missing."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0


def discover_migrations() -> list[tuple[int, str, Path]]:
    """Return sorted list of (version, name, path) for available migrations."""
    if not MIGRATIONS_DIR.exists():
        return []
    out: list[tuple[int, str, Path]] = []
    for p in MIGRATIONS_DIR.iterdir():
        m = MIGRATION_RE.match(p.name)
        if m:
            out.append((int(m.group(1)), m.group(2), p))
    out.sort(key=lambda t: t[0])
    return out


def _apply_sql(conn: sqlite3.Connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    conn.executescript(sql)


def _apply_py(conn: sqlite3.Connection, path: Path) -> None:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load migration {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "upgrade"):
        raise RuntimeError(f"Python migration {path} must define upgrade(conn)")
    module.upgrade(conn)


def run_migrations(conn: sqlite3.Connection) -> int:
    """Apply all pending migrations. Returns number applied."""
    # Ensure schema_version table exists
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    current = get_current_version(conn)
    pending = [m for m in discover_migrations() if m[0] > current]

    if not pending:
        logger.debug("Schema up to date (version=%d)", current)
        return 0

    applied = 0
    for version, name, path in pending:
        logger.info("Applying migration %03d_%s (%s)", version, name, path.suffix)
        try:
            conn.execute("BEGIN")
            if path.suffix == ".sql":
                _apply_sql(conn, path)
            else:
                _apply_py(conn, path)
            conn.execute("INSERT OR REPLACE INTO schema_version VALUES (?)", (version,))
            conn.commit()
            applied += 1
            logger.info("✓ Migration %03d_%s applied", version, name)
        except Exception as e:
            conn.rollback()
            logger.error("✗ Migration %03d_%s failed: %s", version, name, e)
            raise
    return applied
