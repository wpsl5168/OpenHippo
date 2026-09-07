"""Async embedding job queue.

SQLite-backed lightweight queue that decouples embedding generation from the
write path. Single-worker design (SQLite has one writer; multiple workers would
just contend on locks).

Lifecycle: pending → running → done|failed (capped retries).

See skill `sqlite-async-embedding-queue` for the design rationale.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from typing import Optional

from .embedding import get_embedding

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
POLL_INTERVAL_SECONDS = 0.5
IDLE_BACKOFF_SECONDS = 2.0  # back off when queue is empty


# ── Queue ops (sync, called from request path or worker) ──

def enqueue(conn: sqlite3.Connection, target_table: str, target_id: str, content: str) -> int:
    """Enqueue with a durable content-version snapshot in the same transaction."""
    if target_table != "cold_memory":
        raise ValueError("unsupported embedding target_table")
    from .storage import atomic
    with atomic(conn):
        row = conn.execute(
            "SELECT content, created_at, updated_at FROM cold_memory WHERE id=?", (target_id,),
        ).fetchone()
        if row is not None and row["content"] != content:
            raise ValueError("enqueue content is stale")
        cur = conn.execute(
            "INSERT INTO embedding_jobs (target_table, target_id, content) VALUES (?,?,?)",
            (target_table, target_id, content),
        )
        job_id = int(cur.lastrowid)
        if row is not None:
            conn.execute(
                "INSERT INTO embedding_job_versions (job_id, created_at, updated_at) VALUES (?,?,?)",
                (job_id, row["created_at"], row["updated_at"]),
            )
    return job_id


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _record_completion_receipt(conn, job_id, target_id, expected):
    """Bind a CAS-checked completion to exact stored bytes/revision/space.

    Called ONLY after vec_store inside complete_job's transaction. Historical
    vectors must never be backfilled with guessed receipts. No FK/cascade: this
    is durable evidence, not a disposable queue implementation detail.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS embedding_job_receipts (
            job_id INTEGER PRIMARY KEY,
            target_id TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_embedding_job_receipts_target ON embedding_job_receipts(target_id,job_id)")
    row = conn.execute("""
        SELECT e.embedding, e.model, e.created_at, s.space_id, s.model AS space_model
        FROM cold_embeddings e JOIN embedding_spaces s ON s.memory_id=e.memory_id
        WHERE e.memory_id=?
    """, (target_id,)).fetchone()
    indexed = conn.execute(
        "SELECT embedding FROM cold_memory_vec WHERE memory_id=?", (target_id,),
    ).fetchone()
    if row is None or indexed is None or row['embedding'] != indexed[0] or row['model'] != row['space_model']:
        raise ValueError('completion receipt requires consistent stored vector and space')
    evidence = {
        'receipt_version': 1,
        'content_sha256': hashlib.sha256(expected['content'].encode('utf-8')).hexdigest(),
        'created_at': expected['created_at'], 'updated_at': expected['updated_at'],
        'vector_sha256': hashlib.sha256(row['embedding']).hexdigest(),
        'vector_created_at': row['created_at'],
        'model': row['model'], 'space_id': row['space_id'],
    }
    conn.execute(
        'INSERT INTO embedding_job_receipts(job_id,target_id,evidence_json) VALUES (?,?,?)',
        (job_id, target_id, json.dumps(evidence, sort_keys=True, separators=(',', ':'))),
    )


def fetch_one_pending(conn: sqlite3.Connection) -> Optional[dict]:
    """Atomically claim one pending job by flipping it to 'running'.

    Returns the claimed job dict, or None if queue is empty.
    """
    # Use an immediate transaction so the SELECT+UPDATE pair is atomic w.r.t.
    # other writers (we only have one worker, but request-path enqueues still
    # touch the table).
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Reconciliation never rewrites historical status/error/attempts. Any
        # audited failed row (including needs_confirmation) is explicitly held;
        # resolving a hold requires a separate operator decision, not a retry.
        hold = (
            "AND NOT EXISTS (SELECT 1 FROM embedding_job_reconciliation a "
            "WHERE a.job_id=embedding_jobs.id) "
            if _table_exists(conn, "embedding_job_reconciliation") else ""
        )
        row = conn.execute(
            "SELECT id, target_table, target_id, content, attempts "
            "FROM embedding_jobs "
            "WHERE status IN ('pending', 'failed') AND attempts < ? " + hold +
            "ORDER BY id ASC LIMIT 1",
            (MAX_ATTEMPTS,),
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE embedding_jobs SET status='running', updated_at=datetime('now') WHERE id=?",
            (row["id"],),
        )
        conn.execute("COMMIT")
        return {
            "id": row["id"],
            "target_table": row["target_table"],
            "target_id": row["target_id"],
            "content": row["content"],
            "attempts": row["attempts"],
        }
    except Exception:
        conn.execute("ROLLBACK")
        raise


def mark_done(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE embedding_jobs SET status='done', updated_at=datetime('now') WHERE id=?",
        (job_id,),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, job_id: int, error: str) -> None:
    """Bump attempts; status returns to 'failed' for retry until cap."""
    conn.execute(
        "UPDATE embedding_jobs "
        "SET status='failed', attempts=attempts+1, last_error=?, updated_at=datetime('now') "
        "WHERE id=? AND status='running'",
        (error[:500], job_id),
    )
    conn.commit()


def reset_running_on_startup(conn: sqlite3.Connection) -> int:
    """If the worker died mid-job, the row is stuck in 'running'.

    Reset such rows back to 'pending' on startup. Returns count reset.
    """
    cur = conn.execute(
        "UPDATE embedding_jobs SET status='pending', updated_at=datetime('now') "
        "WHERE status='running'"
    )
    conn.commit()
    return cur.rowcount


def cleanup_done(conn: sqlite3.Connection, older_than_days: int = 7) -> int:
    """Delete completed jobs older than N days. Returns deleted count."""
    keep = ""
    for table in ("embedding_job_receipts", "embedding_job_reconciliation"):
        if _table_exists(conn, table):
            keep += f" AND NOT EXISTS (SELECT 1 FROM {table} a WHERE a.job_id=embedding_jobs.id)"
    cur = conn.execute(
        "DELETE FROM embedding_jobs WHERE status='done' "
        "AND updated_at < datetime('now', ?)" + keep,
        (f"-{older_than_days} days",),
    )
    conn.commit()
    return cur.rowcount


def queue_stats(conn: sqlite3.Connection) -> dict:
    """Raw status counts plus historical audit outcomes, in one read snapshot.

    ``failed`` remains the original counter for compatibility. Subtract only
    explicitly resolved/superseded historical audits in ``failed_unresolved``;
    needs_confirmation is still unresolved, never an automatic retry request.
    """
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        result = _queue_stats_snapshot(conn)
        if own:
            conn.commit()
        return result
    except BaseException:
        if own:
            conn.rollback()
        raise


def _queue_stats_snapshot(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM embedding_jobs GROUP BY status"
    ).fetchall()
    result = {row["status"]: row["n"] for row in rows}
    if _table_exists(conn, "embedding_job_reconciliation"):
        # Preserve the established raw status counts. Additional keys describe
        # historical audit outcomes, not a claim that failed jobs ran to done.
        counts = {r[0]: r[1] for r in conn.execute("""
            SELECT a.decision, count(*) FROM embedding_job_reconciliation a
            JOIN embedding_jobs j ON j.id=a.job_id AND j.status='failed'
            WHERE a.id=(SELECT max(b.id) FROM embedding_job_reconciliation b WHERE b.job_id=a.job_id)
            GROUP BY a.decision
        """)}
        for decision in ("resolved", "superseded", "needs_confirmation"):
            result["historical_" + decision] = counts.get(decision, 0)
        result["failed_unresolved"] = result.get("failed", 0) - counts.get("resolved", 0) - counts.get("superseded", 0)
    return result


def complete_job(storage, job: dict, vec: list[float]) -> bool:
    """CAS validation, vector replacement and job completion are ONE commit.

    Missing legacy version evidence is stale, not permission to overwrite.
    Returns False for discarded jobs; failures roll back before mark_failed.
    """
    from .storage import atomic
    conn = storage._get_conn()
    with atomic(conn):
        current = conn.execute("SELECT * FROM embedding_jobs WHERE id=?", (job["id"],)).fetchone()
        if current is None or current["status"] != "running":
            return False
        version = conn.execute(
            "SELECT created_at, updated_at FROM embedding_job_versions WHERE job_id=?", (job["id"],),
        ).fetchone()
        valid = current["target_table"] == "cold_memory" and version is not None and all(
            current[k] == job[k] for k in ("target_table", "target_id", "content", "attempts")
        )
        expected = dict(version) if version else {}
        expected["content"] = current["content"]
        valid = valid and storage._entry_matches(current["target_id"], expected)
        newer = conn.execute(
            "SELECT 1 FROM embedding_jobs WHERE target_table=? AND target_id=? AND id>? LIMIT 1",
            (current["target_table"], current["target_id"], job["id"]),
        ).fetchone()
        if valid and not newer:
            storage.vec_store(current["target_id"], vec, expected=expected)
            _record_completion_receipt(conn, job["id"], current["target_id"], expected)
        else:
            valid = False
        conn.execute(
            "UPDATE embedding_jobs SET status='done', last_error=?, updated_at=datetime('now') WHERE id=?",
            (None if valid else "stale job: target/content/version superseded or unverified", job["id"]),
        )
        return bool(valid)


# ── Worker (async) ──

def is_async_enabled() -> bool:
    """Master switch. Default ON; set OPENHIPPO_ASYNC_EMBED=0 to disable."""
    return os.environ.get("OPENHIPPO_ASYNC_EMBED", "1").lower() not in {"0", "false", "no"}


async def embedding_worker(engine, stop_event: asyncio.Event) -> None:
    """Drain pending embedding jobs in a loop until stop_event is set.

    Runs in the FastAPI event loop. Embedding (Ollama HTTP call) is sync and
    blocks the loop briefly per job, but with one worker and ~130ms/job that's
    acceptable (~7 jobs/sec). If this becomes a bottleneck, wrap get_embedding
    in `asyncio.to_thread`.
    """
    storage = engine.storage
    logger.info("embedding worker started")
    while not stop_event.is_set():
        try:
            conn = storage._get_conn()
            job = fetch_one_pending(conn)
        except Exception as e:
            logger.error("worker: fetch failed: %s", e)
            await asyncio.sleep(IDLE_BACKOFF_SECONDS)
            continue

        if not job:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=IDLE_BACKOFF_SECONDS)
            except asyncio.TimeoutError:
                pass
            continue

        try:
            vec = await asyncio.to_thread(get_embedding, job["content"])
            if not vec:
                raise RuntimeError("get_embedding returned None/empty")
            complete_job(storage, job, vec)
        except Exception as e:
            logger.warning(
                "worker: job %d (target=%s) failed (attempt %d): %s",
                job["id"], job["target_id"], job["attempts"] + 1, e,
            )
            try:
                mark_failed(storage._get_conn(), job["id"], str(e))
            except Exception as e2:
                logger.error("worker: mark_failed itself failed: %s", e2)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        # Yield briefly to keep the loop responsive even under heavy queue load.
        await asyncio.sleep(0)

    logger.info("embedding worker stopped")
