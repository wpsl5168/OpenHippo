-- Migration 009: Performance indexes for common access patterns.
--
-- Adds composite (agent_id, created_at DESC) and (created_at DESC) indexes
-- to eliminate TEMP B-TREE sorts found in EXPLAIN QUERY PLAN. Critical once
-- cold_memory exceeds a few hundred rows.

CREATE INDEX IF NOT EXISTS idx_hot_memory_agent_time
    ON hot_memory(agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_cold_memory_time
    ON cold_memory(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_cold_memory_agent_time
    ON cold_memory(agent_id, created_at DESC);

-- Backfill statistics so the planner picks the new indexes immediately.
ANALYZE;
