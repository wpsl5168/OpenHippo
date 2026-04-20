-- Migration 004: precise dedup via content_hash
-- Adds content_hash column on cold_memory + (target, content_hash) UNIQUE index.
-- Replaces O(n) Python-loop SHA-256 scan with O(log n) DB index lookup.

ALTER TABLE cold_memory ADD COLUMN content_hash TEXT;

-- Backfill existing rows: SHA-256 of content (matches engine._normalize_content
-- enough for legacy entries; new writes will normalize first then hash).
-- We cannot call sha256() in pure SQLite without extension, so backfill is
-- handled by the Python migration 005 below. This file just adds the column.

CREATE INDEX IF NOT EXISTS idx_cold_memory_hash ON cold_memory(target, content_hash);
