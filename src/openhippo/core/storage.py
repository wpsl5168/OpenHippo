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

    def hot_add(self, target: str, content: str, agent_id: str | None = None,
                session_id: str | None = None, scope: str = "agent") -> dict:
        conn = self._get_conn()
        mid = uuid.uuid4().hex[:16]
        now = time.time()
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM hot_memory WHERE target=?",
            (target,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO hot_memory (id, target, content, created_at, updated_at, sort_order, agent_id, session_id, scope) VALUES (?,?,?,?,?,?,?,?,?)",
            (mid, target, content, now, now, max_order + 1, agent_id, session_id, scope),
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

    def unified_timeline(self, target: str | None = None,
                         agent_id: str | None = None,
                         limit: int = 50, offset: int = 0,
                         date_from: float | None = None,
                         date_to: float | None = None) -> list[dict]:
        """Combined hot+cold timeline ordered by created_at DESC.
        End-user facing — does not distinguish tiers.
        agent_id='__local__' filters to hot + cold rows where agent_id IS NULL.
        date_from/date_to are unix timestamps (inclusive lower, exclusive upper).
        """
        conn = self._get_conn()
        where_hot = ["1=1"]
        where_cold = ["1=1"]
        if target:
            where_hot.append("target = ?")
            where_cold.append("target = ?")
        # agent filter
        if agent_id == "__local__":
            where_cold.append("agent_id IS NULL")
        elif agent_id:
            where_cold.append("agent_id = ?")
            # hot has no agent_id, exclude when filtering by specific agent
            where_hot.append("0=1")
        if date_from is not None:
            where_hot.append("created_at >= ?")
            where_cold.append("created_at >= ?")
        if date_to is not None:
            where_hot.append("created_at < ?")
            where_cold.append("created_at < ?")

        # Build params in execution order (hot params, cold params, limit, offset)
        hot_params: list = []
        cold_params: list = []
        if target:
            hot_params.append(target)
            cold_params.append(target)
        if agent_id and agent_id != "__local__":
            cold_params.append(agent_id)
        if date_from is not None:
            hot_params.append(date_from)
            cold_params.append(date_from)
        if date_to is not None:
            hot_params.append(date_to)
            cold_params.append(date_to)

        sql = f"""
            SELECT id, target, content, created_at, updated_at,
                   NULL as source, NULL as agent_id, NULL as scope, NULL as session_id, 'hot' as tier
            FROM hot_memory WHERE {' AND '.join(where_hot)}
            UNION ALL
            SELECT id, target, content, created_at, updated_at,
                   source, agent_id, scope, session_id, 'cold' as tier
            FROM cold_memory WHERE {' AND '.join(where_cold)}
            ORDER BY created_at DESC LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, (*hot_params, *cold_params, limit, offset)).fetchall()
        return [dict(r) for r in rows]

    def unified_count(self, target: str | None = None,
                      agent_id: str | None = None,
                      date_from: float | None = None,
                      date_to: float | None = None) -> int:
        """Total memories matching filters (hot+cold combined)."""
        conn = self._get_conn()

        def _build(table: str, is_cold: bool) -> tuple[str, list]:
            where = ["1=1"]
            params: list = []
            if target:
                where.append("target = ?"); params.append(target)
            if is_cold:
                if agent_id == "__local__":
                    where.append("agent_id IS NULL")
                elif agent_id:
                    where.append("agent_id = ?"); params.append(agent_id)
            else:
                # hot has no agent_id; only matches when not filtering by specific agent
                if agent_id and agent_id != "__local__":
                    return "", []
            if date_from is not None:
                where.append("created_at >= ?"); params.append(date_from)
            if date_to is not None:
                where.append("created_at < ?"); params.append(date_to)
            return f"SELECT COUNT(*) FROM {table} WHERE {' AND '.join(where)}", params

        total = 0
        for table, is_cold in [("hot_memory", False), ("cold_memory", True)]:
            sql, params = _build(table, is_cold)
            if not sql:
                continue
            total += conn.execute(sql, params).fetchone()[0]
        return total

    def daily_calendar(self, target: str | None = None,
                       agent_id: str | None = None,
                       days: int = 365) -> list[dict]:
        """Per-day memory counts over the past N days (UTC dates).
        Returns [{date: 'YYYY-MM-DD', count: N}] sorted oldest→newest,
        with zero-count days included so the UI can render a continuous strip.
        """
        import datetime as _dt
        conn = self._get_conn()
        now = _dt.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        start = now - _dt.timedelta(days=days - 1)
        start_ts = start.timestamp()

        def _build(table: str, is_cold: bool) -> tuple[str, list]:
            where = ["created_at >= ?"]
            params: list = [start_ts]
            if target:
                where.append("target = ?"); params.append(target)
            if is_cold:
                if agent_id == "__local__":
                    where.append("agent_id IS NULL")
                elif agent_id:
                    where.append("agent_id = ?"); params.append(agent_id)
            else:
                if agent_id and agent_id != "__local__":
                    return "", []
            return (
                f"SELECT created_at FROM {table} WHERE {' AND '.join(where)}",
                params,
            )

        bucket: dict[str, int] = {}
        for table, is_cold in [("hot_memory", False), ("cold_memory", True)]:
            sql, params = _build(table, is_cold)
            if not sql:
                continue
            for (ts,) in conn.execute(sql, params).fetchall():
                d = _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                bucket[d] = bucket.get(d, 0) + 1

        out = []
        for i in range(days):
            d = (start + _dt.timedelta(days=i)).strftime("%Y-%m-%d")
            out.append({"date": d, "count": bucket.get(d, 0)})
        return out

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

    def overview(self) -> dict:
        """User-facing aggregate stats. Combines hot+cold (no tier distinction)
        for end users who don't care about internal tiering.

        Returns:
            total: combined hot+cold memory count
            earliest_at / latest_at: epoch seconds across all memories
            by_target: [{key, label, count}] grouped by target (memory/user)
            by_agent: [{key, label, count}] grouped by agent_id (cold only;
                      hot has no agent_id concept)
            daily_counts_30d: [{date: YYYY-MM-DD, count}] for last 30 days,
                              zero-filled, based on created_at of all memories
        """
        import datetime as _dt
        conn = self._get_conn()
        # totals
        hot_rows = conn.execute(
            "SELECT target, COUNT(*) c, MIN(created_at) mn, MAX(created_at) mx "
            "FROM hot_memory GROUP BY target"
        ).fetchall()
        cold_rows = conn.execute(
            "SELECT target, COUNT(*) c, MIN(created_at) mn, MAX(created_at) mx "
            "FROM cold_memory GROUP BY target"
        ).fetchall()

        by_target_map: dict[str, int] = {}
        mins: list[float] = []
        maxs: list[float] = []
        for r in list(hot_rows) + list(cold_rows):
            by_target_map[r["target"]] = by_target_map.get(r["target"], 0) + r["c"]
            if r["mn"] is not None: mins.append(r["mn"])
            if r["mx"] is not None: maxs.append(r["mx"])

        target_labels = {"memory": "工作笔记", "user": "关于你"}
        by_target = [
            {"key": k, "label": target_labels.get(k, k), "count": v}
            for k, v in sorted(by_target_map.items(), key=lambda x: -x[1])
        ]

        # by agent (cold only — hot has no agent_id)
        agent_rows = conn.execute(
            "SELECT COALESCE(agent_id, '__local__') as a, COUNT(*) c "
            "FROM cold_memory GROUP BY a ORDER BY c DESC"
        ).fetchall()
        # Add hot rows under '__local__' bucket
        hot_total = sum(r["c"] for r in hot_rows)
        agent_map: dict[str, int] = {r["a"]: r["c"] for r in agent_rows}
        if hot_total:
            agent_map["__local__"] = agent_map.get("__local__", 0) + hot_total
        by_agent = [
            {"key": k, "label": "本地" if k == "__local__" else k, "count": v}
            for k, v in sorted(agent_map.items(), key=lambda x: -x[1])
        ]

        # daily counts last 30 days (zero-filled)
        today = _dt.datetime.now().date()
        start = today - _dt.timedelta(days=29)
        start_epoch = _dt.datetime.combine(start, _dt.time.min).timestamp()
        day_rows = conn.execute(
            "SELECT date(created_at, 'unixepoch', 'localtime') d, COUNT(*) c "
            "FROM (SELECT created_at FROM hot_memory UNION ALL "
            "      SELECT created_at FROM cold_memory) "
            "WHERE created_at >= ? GROUP BY d",
            (start_epoch,),
        ).fetchall()
        day_map = {r["d"]: r["c"] for r in day_rows}
        daily = []
        for i in range(30):
            d = start + _dt.timedelta(days=i)
            ds = d.isoformat()
            daily.append({"date": ds, "count": day_map.get(ds, 0)})

        return {
            "total": sum(by_target_map.values()),
            "earliest_at": min(mins) if mins else None,
            "latest_at": max(maxs) if maxs else None,
            "by_target": by_target,
            "by_agent": by_agent,
            "daily_counts_30d": daily,
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
