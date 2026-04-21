-- Migration 010: Async embedding job queue.
--
-- Decouples embedding generation from the write path. cold_add inserts a
-- pending job; a single FastAPI background worker drains it. See skill
-- `sqlite-async-embedding-queue` for design rationale.
--
-- Status lifecycle: pending → running → done|failed
-- The partial index keeps the worker poll cheap even when `done` accumulates.

CREATE TABLE IF NOT EXISTS embedding_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    target_table TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    content      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_embedding_jobs_pending
    ON embedding_jobs(status, created_at)
    WHERE status IN ('pending', 'failed');
