"""SQLite + FTS5 storage backend for OpenHippo."""

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".openhippo" / "memory.db"

SCHEMA_SQL = """
-- Hot memory: small, injected every turn
CREATE TABLE IF NOT EXISTS hot_memory (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    target      TEXT NOT NULL CHECK(target IN ('memory', 'user')),  -- memory=agent notes, user=profile
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL DEFAULT (unixepoch('now')),
    updated_at  REAL NOT NULL DEFAULT (unixepoch('now')),
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- Cold memory: searchable archive
CREATE TABLE IF NOT EXISTS cold_memory (
    id              TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    target          TEXT NOT NULL CHECK(target IN ('memory', 'user')),
    content         TEXT NOT NULL,
    source          TEXT DEFAULT 'manual',   -- manual, archived, extracted
    tags            TEXT DEFAULT '[]',        -- JSON array
    access_count    INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL DEFAULT (unixepoch('now')),
    updated_at      REAL NOT NULL DEFAULT (unixepoch('now')),
    last_accessed   REAL,
    archived_from   TEXT,  -- hot_memory.id if archived
    metadata        TEXT DEFAULT '{}'  -- JSON
);

-- FTS5 index on cold memory
CREATE VIRTUAL TABLE IF NOT EXISTS cold_memory_fts USING fts5(
    content,
    tags,
    content=cold_memory,
    content_rowid=rowid,
    tokenize='unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS cold_memory_ai AFTER INSERT ON cold_memory BEGIN
    INSERT INTO cold_memory_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS cold_memory_ad AFTER DELETE ON cold_memory BEGIN
    INSERT INTO cold_memory_fts(cold_memory_fts, rowid, content, tags) VALUES('delete', old.rowid, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS cold_memory_au AFTER UPDATE ON cold_memory BEGIN
    INSERT INTO cold_memory_fts(cold_memory_fts, rowid, content, tags) VALUES('delete', old.rowid, old.content, old.tags);
    INSERT INTO cold_memory_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;

-- Consolidation log
CREATE TABLE IF NOT EXISTS consolidation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,  -- archive, promote, merge, forget, sweep
    memory_id   TEXT,
    details     TEXT,  -- JSON
    created_at  REAL NOT NULL DEFAULT (unixepoch('now'))
);

-- Schema version
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version VALUES (1);
"""


class Storage:
    """SQLite + FTS5 storage layer."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), timeout=10)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Hot Memory ──

    def hot_list(self, target: str | None = None) -> list[dict]:
        conn = self._get_conn()
        if target:
            rows = conn.execute(
                "SELECT * FROM hot_memory WHERE target=? ORDER BY sort_order, created_at",
                (target,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM hot_memory ORDER BY target, sort_order, created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def hot_add(self, target: str, content: str) -> dict:
        conn = self._get_conn()
        mid = uuid.uuid4().hex[:16]
        now = time.time()
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM hot_memory WHERE target=?",
            (target,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO hot_memory (id, target, content, created_at, updated_at, sort_order) VALUES (?,?,?,?,?,?)",
            (mid, target, content, now, now, max_order + 1),
        )
        conn.commit()
        return {"id": mid, "status": "created"}

    def hot_replace(self, target: str, old_text: str, new_content: str) -> dict:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, content FROM hot_memory WHERE target=? AND content LIKE ?",
            (target, f"%{old_text}%"),
        ).fetchall()
        if not rows:
            return {"error": f"No entry matching '{old_text[:50]}...'"}
        if len(rows) > 1:
            return {"error": f"Multiple entries match '{old_text[:50]}...', be more specific"}
        row = rows[0]
        conn.execute(
            "UPDATE hot_memory SET content=?, updated_at=? WHERE id=?",
            (new_content, time.time(), row["id"]),
        )
        conn.commit()
        return {"id": row["id"], "status": "replaced"}

    def hot_remove(self, target: str, old_text: str) -> dict:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, content FROM hot_memory WHERE target=? AND content LIKE ?",
            (target, f"%{old_text}%"),
        ).fetchall()
        if not rows:
            return {"error": f"No entry matching '{old_text[:50]}...'"}
        row = rows[0]
        conn.execute("DELETE FROM hot_memory WHERE id=?", (row["id"],))
        conn.commit()
        return {"id": row["id"], "status": "removed", "content": row["content"]}

    def hot_count(self, target: str) -> int:
        conn = self._get_conn()
        return conn.execute(
            "SELECT COUNT(*) FROM hot_memory WHERE target=?", (target,)
        ).fetchone()[0]

    def hot_chars(self, target: str) -> int:
        conn = self._get_conn()
        result = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM hot_memory WHERE target=?",
            (target,),
        ).fetchone()[0]
        return result

    # ── Cold Memory ──

    def cold_add(self, target: str, content: str, source: str = "manual",
                 tags: list[str] | None = None, metadata: dict | None = None,
                 archived_from: str | None = None) -> dict:
        conn = self._get_conn()
        mid = uuid.uuid4().hex[:16]
        now = time.time()
        conn.execute(
            """INSERT INTO cold_memory
               (id, target, content, source, tags, created_at, updated_at, archived_from, metadata)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (mid, target, content, source, 
             __import__("json").dumps(tags or []),
             now, now, archived_from,
             __import__("json").dumps(metadata or {})),
        )
        conn.commit()
        return {"id": mid, "status": "created"}

    def cold_search(self, query: str, target: str | None = None, limit: int = 20) -> list[dict]:
        conn = self._get_conn()
        try:
            # FTS5 search
            if target:
                rows = conn.execute(
                    """SELECT cm.* FROM cold_memory_fts fts
                       JOIN cold_memory cm ON cm.rowid = fts.rowid
                       WHERE cold_memory_fts MATCH ? AND cm.target = ?
                       ORDER BY rank LIMIT ?""",
                    (query, target, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT cm.* FROM cold_memory_fts fts
                       JOIN cold_memory cm ON cm.rowid = fts.rowid
                       WHERE cold_memory_fts MATCH ?
                       ORDER BY rank LIMIT ?""",
                    (query, limit),
                ).fetchall()
        except Exception:
            # Fallback to LIKE
            if target:
                rows = conn.execute(
                    "SELECT * FROM cold_memory WHERE content LIKE ? AND target=? LIMIT ?",
                    (f"%{query}%", target, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cold_memory WHERE content LIKE ? LIMIT ?",
                    (f"%{query}%", limit),
                ).fetchall()

        # Update access stats
        results = []
        for r in rows:
            d = dict(r)
            conn.execute(
                "UPDATE cold_memory SET access_count=access_count+1, last_accessed=? WHERE id=?",
                (time.time(), d["id"]),
            )
            results.append(d)
        if results:
            conn.commit()
        return results

    def cold_get(self, memory_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM cold_memory WHERE id=?", (memory_id,)).fetchone()
        return dict(row) if row else None

    def cold_delete(self, memory_id: str) -> dict:
        conn = self._get_conn()
        row = conn.execute("SELECT id FROM cold_memory WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return {"error": f"Memory {memory_id} not found"}
        conn.execute("DELETE FROM cold_memory WHERE id=?", (memory_id,))
        conn.commit()
        return {"id": memory_id, "status": "deleted"}

    def cold_count(self, target: str | None = None) -> int:
        conn = self._get_conn()
        if target:
            return conn.execute(
                "SELECT COUNT(*) FROM cold_memory WHERE target=?", (target,)
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM cold_memory").fetchone()[0]

    # ── Archive (hot→cold) ──

    def archive(self, target: str, old_text: str) -> dict:
        """Move a hot memory entry to cold storage."""
        removed = self.hot_remove(target, old_text)
        if "error" in removed:
            return removed
        result = self.cold_add(
            target=target,
            content=removed["content"],
            source="archived",
            archived_from=removed["id"],
        )
        self._log("archive", removed["id"], {"cold_id": result["id"]})
        return {"status": "archived", "hot_id": removed["id"], "cold_id": result["id"]}

    # ── Promote (cold→hot) ──

    def promote(self, memory_id: str) -> dict:
        """Move a cold memory entry back to hot storage."""
        entry = self.cold_get(memory_id)
        if not entry:
            return {"error": f"Cold memory {memory_id} not found"}
        hot = self.hot_add(entry["target"], entry["content"])
        self.cold_delete(memory_id)
        self._log("promote", memory_id, {"hot_id": hot["id"]})
        return {"status": "promoted", "cold_id": memory_id, "hot_id": hot["id"]}

    # ── Stats ──

    def stats(self) -> dict:
        conn = self._get_conn()
        return {
            "hot_memory_count": self.hot_count("memory"),
            "hot_memory_chars": self.hot_chars("memory"),
            "hot_user_count": self.hot_count("user"),
            "hot_user_chars": self.hot_chars("user"),
            "cold_count": self.cold_count(),
            "cold_memory_count": self.cold_count("memory"),
            "cold_user_count": self.cold_count("user"),
            "db_size_kb": self.db_path.stat().st_size // 1024 if self.db_path.exists() else 0,
        }

    # ── Logging ──

    def _log(self, action: str, memory_id: str | None, details: dict | None = None) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO consolidation_log (action, memory_id, details) VALUES (?,?,?)",
            (action, memory_id, __import__("json").dumps(details or {})),
        )
        conn.commit()

    def get_logs(self, limit: int = 50) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM consolidation_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
