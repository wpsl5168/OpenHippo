-- Migration 008: dream_actions audit table
-- Every consolidate/forget/restore action gets a row here for full auditability.

CREATE TABLE IF NOT EXISTS dream_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dream_run_id TEXT NOT NULL,
    action TEXT NOT NULL,                  -- 'consolidate' / 'forget' / 'restore'
    memory_id TEXT NOT NULL,
    details TEXT,                          -- JSON
    created_at TEXT NOT NULL,
    FOREIGN KEY (dream_run_id) REFERENCES dream_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_dream_actions_run ON dream_actions(dream_run_id);
CREATE INDEX IF NOT EXISTS idx_dream_actions_memory ON dream_actions(memory_id);
