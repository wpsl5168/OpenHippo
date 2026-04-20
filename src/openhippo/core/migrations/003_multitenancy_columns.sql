-- Migration 003: multi-tenancy columns on cold_memory
-- Reserves agent_id, scope, session_id columns for future F11/F12/F13.
-- All NULL-allowed with sensible defaults for backward compatibility.

ALTER TABLE cold_memory ADD COLUMN agent_id   TEXT;
ALTER TABLE cold_memory ADD COLUMN scope      TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE cold_memory ADD COLUMN session_id TEXT;

CREATE INDEX IF NOT EXISTS idx_cold_memory_agent   ON cold_memory(agent_id);
CREATE INDEX IF NOT EXISTS idx_cold_memory_scope   ON cold_memory(scope);
CREATE INDEX IF NOT EXISTS idx_cold_memory_session ON cold_memory(session_id);

-- Mirror on hot_memory
ALTER TABLE hot_memory ADD COLUMN agent_id   TEXT;
ALTER TABLE hot_memory ADD COLUMN scope      TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE hot_memory ADD COLUMN session_id TEXT;

CREATE INDEX IF NOT EXISTS idx_hot_memory_agent   ON hot_memory(agent_id);
CREATE INDEX IF NOT EXISTS idx_hot_memory_session ON hot_memory(session_id);
