-- Migration 007: dream-related columns on cold_memory
-- Note: access_count and last_accessed already exist in the base schema; reuse them.
-- All NULL-allowed / sensible defaults for backward compat.

ALTER TABLE cold_memory ADD COLUMN consolidated_into TEXT;     -- points to seed id when merged
ALTER TABLE cold_memory ADD COLUMN merged_from TEXT;            -- JSON array of merged source ids
ALTER TABLE cold_memory ADD COLUMN importance REAL DEFAULT 0.5; -- 0~1
ALTER TABLE cold_memory ADD COLUMN dream_status TEXT DEFAULT 'active';
-- 'active' | 'dormant' | 'consolidated'
ALTER TABLE cold_memory ADD COLUMN last_dream_at TEXT;

CREATE INDEX IF NOT EXISTS idx_cold_dream_status ON cold_memory(dream_status, last_dream_at);
CREATE INDEX IF NOT EXISTS idx_cold_consolidated ON cold_memory(consolidated_into);
