"""SQLite + FTS5 + sqlite-vec storage backend for OpenHippo.

Threading model:
- One sqlite3.Connection per thread (threading.local pool).
- WAL journal mode for read/write concurrency.
- vec0 extension loaded once per connection.
"""

import hashlib
import logging
import sqlite3
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import sqlite_vec

from .migrations_runner import run_migrations

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".openhippo" / "memory.db"

# ── Initial schema (v2) — historic baseline ──
SCHEMA_SQL = """
-- Hot memory: small, injected every turn
CREATE TABLE IF NOT EXISTS hot_memory (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    target      TEXT NOT NULL CHECK(target IN ('memory', 'user')),
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
    source          TEXT DEFAULT 'manual',
    tags            TEXT DEFAULT '[]',
    access_count    INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL DEFAULT (unixepoch('now')),
    updated_at      REAL NOT NULL DEFAULT (unixepoch('now')),
    last_accessed   REAL,
    archived_from   TEXT,
    metadata        TEXT DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS cold_memory_fts USING fts5(
    content,
    tags,
    content=cold_memory,
    content_rowid=rowid,
    tokenize='unicode61'
);

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

CREATE TABLE IF NOT EXISTS consolidation_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    memory_id   TEXT,
    details     TEXT,
    created_at  REAL NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS cold_embeddings (
    memory_id   TEXT PRIMARY KEY REFERENCES cold_memory(id) ON DELETE CASCADE,
    embedding   BLOB NOT NULL,
    model       TEXT NOT NULL DEFAULT 'nomic-embed-text',
    created_at  REAL NOT NULL DEFAULT (unixepoch('now'))
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version VALUES (2);
"""

VECTOR_TABLE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS cold_memory_vec USING vec0(
    memory_id TEXT PRIMARY KEY,
    embedding float[768]
);
"""


# ── Helpers ──

_PREFIXES = ("[hermes-mirror] ", "[migrated-cold] ", "[migrated-hot] ")


def _normalize_content(s: str) -> str:
    s = s.strip()
    for p in _PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
    return s


def _content_hash(content: str) -> str:
    return hashlib.sha256(_normalize_content(content).encode()).hexdigest()


class Storage:
    """SQLite + FTS5 + sqlite-vec storage layer (thread-safe via per-thread conns)."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        # Run init/migrations on a one-shot connection so they happen exactly once
        init_conn = self._make_conn()
        try:
            self._init_db(init_conn)
        finally:
            init_conn.close()

    # ── Connection management ──

    def _make_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=True)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = self._make_conn()
            self._tls.conn = conn
        return conn

    def _init_db(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(VECTOR_TABLE_SQL)
        conn.commit()
        applied = run_migrations(conn)
        if applied:
            logger.info("Applied %d schema migration(s)", applied)

    def close(self) -> None:
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            conn.close()
            self._tls.conn = None

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
                 archived_from: str | None = None,
                 agent_id: str | None = None,
                 scope: str = "agent",
                 session_id: str | None = None) -> dict:
        conn = self._get_conn()
        mid = uuid.uuid4().hex[:16]
        now = time.time()
        chash = _content_hash(content)
        conn.execute(
            """INSERT INTO cold_memory
               (id, target, content, source, tags, created_at, updated_at,
                archived_from, metadata, content_hash, agent_id, scope, session_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, target, content, source,
             __import__("json").dumps(tags or []),
             now, now, archived_from,
             __import__("json").dumps(metadata or {}),
             chash, agent_id, scope, session_id),
        )
        conn.commit()
        return {"id": mid, "status": "created"}

    def cold_find_by_hash(self, target: str, content: str) -> dict | None:
        """Return the cold row matching the normalized content hash, if any."""
        conn = self._get_conn()
        chash = _content_hash(content)
        row = conn.execute(
            "SELECT id, content FROM cold_memory WHERE target=? AND content_hash=? LIMIT 1",
            (target, chash),
        ).fetchone()
        return dict(row) if row else None

    def cold_search(self, query: str, target: str | None = None, limit: int = 20,
                    include_consolidated: bool = False,
                    include_dormant: bool = False) -> list[dict]:
        """Search cold memory with FTS5 fallback to LIKE.

        By default, rows merged into a seed by Dream (dream_status='consolidated')
        AND rows soft-forgotten (dream_status='dormant') are hidden — only
        active memories surface. Pass the corresponding flag(s) for audit views.
        """
        conn = self._get_conn()
        # Build dream-status filter as SQL fragment we can append to either branch.
        excluded = []
        if not include_consolidated:
            excluded.append("'consolidated'")
        if not include_dormant:
            excluded.append("'dormant'")
        if excluded:
            in_clause = "(" + ",".join(excluded) + ")"
            status_clause = f" AND COALESCE(cm.dream_status, 'active') NOT IN {in_clause}"
            like_status_clause = f" AND COALESCE(dream_status, 'active') NOT IN {in_clause}"
        else:
            status_clause = ""
            like_status_clause = ""
        try:
            if target:
                rows = conn.execute(
                    f"""SELECT cm.* FROM cold_memory_fts fts
                       JOIN cold_memory cm ON cm.rowid = fts.rowid
                       WHERE cold_memory_fts MATCH ? AND cm.target = ?{status_clause}
                       ORDER BY rank LIMIT ?""",
                    (query, target, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT cm.* FROM cold_memory_fts fts
                       JOIN cold_memory cm ON cm.rowid = fts.rowid
                       WHERE cold_memory_fts MATCH ?{status_clause}
                       ORDER BY rank LIMIT ?""",
                    (query, limit),
                ).fetchall()
        except Exception:
            if target:
                rows = conn.execute(
                    f"SELECT * FROM cold_memory WHERE content LIKE ? AND target=?{like_status_clause} LIMIT ?",
                    (f"%{query}%", target, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM cold_memory WHERE content LIKE ?{like_status_clause} LIMIT ?",
                    (f"%{query}%", limit),
                ).fetchall()

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
        self.vec_delete(memory_id)
        conn.commit()
        return {"id": memory_id, "status": "deleted"}

    def cold_update(self, memory_id: str, content: str) -> dict:
        conn = self._get_conn()
        row = conn.execute("SELECT id FROM cold_memory WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return {"error": f"Memory {memory_id} not found"}
        chash = _content_hash(content)
        conn.execute(
            "UPDATE cold_memory SET content=?, content_hash=?, updated_at=unixepoch('now') WHERE id=?",
            (content, chash, memory_id),
        )
        conn.commit()
        from .embedding import get_embedding
        vec = get_embedding(content)
        if vec:
            self.vec_delete(memory_id)
            self.vec_store(memory_id, vec)
        return {"id": memory_id, "status": "updated"}

    def cold_timeline(self, target: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        conn = self._get_conn()
        if target:
            rows = conn.execute(
                "SELECT * FROM cold_memory WHERE target=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (target, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM cold_memory ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def cold_count(self, target: str | None = None) -> int:
        conn = self._get_conn()
        if target:
            return conn.execute(
                "SELECT COUNT(*) FROM cold_memory WHERE target=?", (target,)
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM cold_memory").fetchone()[0]

    # ── Archive (hot→cold) ──

    def archive(self, target: str, old_text: str) -> dict:
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
        entry = self.cold_get(memory_id)
        if not entry:
            return {"error": f"Cold memory {memory_id} not found"}
        hot = self.hot_add(entry["target"], entry["content"])
        self.cold_delete(memory_id)
        self._log("promote", memory_id, {"hot_id": hot["id"]})
        return {"status": "promoted", "cold_id": memory_id, "hot_id": hot["id"]}

    # ── Vector Operations ──

    @staticmethod
    def _serialize_vec(vec: list[float]) -> bytes:
        return struct.pack(f"<{len(vec)}f", *vec)

    def vec_store(self, memory_id: str, embedding: list[float]) -> None:
        conn = self._get_conn()
        blob = self._serialize_vec(embedding)
        conn.execute(
            "INSERT OR REPLACE INTO cold_embeddings (memory_id, embedding, created_at) VALUES (?,?,?)",
            (memory_id, blob, time.time()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO cold_memory_vec (memory_id, embedding) VALUES (?,?)",
            (memory_id, blob),
        )
        conn.commit()

    def vec_delete(self, memory_id: str) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM cold_embeddings WHERE memory_id=?", (memory_id,))
        conn.execute("DELETE FROM cold_memory_vec WHERE memory_id=?", (memory_id,))
        conn.commit()

    # L2 distance threshold for relevance filtering.
    # For nomic-embed-text (768d, normalized), empirically:
    #   < 1.0 = highly relevant, 1.0-1.3 = relevant, 1.3-1.5 = borderline
    #   > 1.5 = likely irrelevant.
    VEC_DISTANCE_THRESHOLD = 1.0

    def vec_search(self, query_embedding: list[float], target: str | None = None,
                   limit: int = 20) -> list[dict]:
        conn = self._get_conn()
        # sqlite-vec quirk: KNN MATCH on empty vec0 table raises "unknown error".
        # Guard with a fast count check.
        cnt = conn.execute("SELECT COUNT(*) FROM cold_memory_vec").fetchone()[0]
        if cnt == 0:
            return []
        blob = self._serialize_vec(query_embedding)
        fetch_k = max(limit * 3, 30)
        try:
            rows = conn.execute(
                """SELECT v.memory_id, v.distance, cm.*
                   FROM cold_memory_vec v
                   JOIN cold_memory cm ON cm.id = v.memory_id
                   WHERE v.embedding MATCH ? AND k = ?""",
                (blob, fetch_k),
            ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("vec_search failed (%s); returning empty result", e)
            return []

        results = []
        for r in rows:
            d = dict(r)
            distance = d.pop("distance", None)
            if target and d.get("target") != target:
                continue
            if distance is not None and distance > self.VEC_DISTANCE_THRESHOLD:
                continue
            d["vec_distance"] = distance
            results.append(d)
            if len(results) >= limit:
                break
        return results

    def vec_count(self) -> int:
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) FROM cold_embeddings").fetchone()[0]

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
            "vec_count": self.vec_count(),
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
