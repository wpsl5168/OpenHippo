-- Migration 006: dream_runs table — track every consolidation cycle
CREATE TABLE IF NOT EXISTS dream_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,                  -- 'running' / 'completed' / 'failed' / 'preview'
    candidates_count INTEGER DEFAULT 0,
    clusters_count INTEGER DEFAULT 0,
    consolidated_count INTEGER DEFAULT 0,
    forgotten_count INTEGER DEFAULT 0,
    config_snapshot TEXT,                  -- JSON: thresholds & version at run time
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_dream_runs_status ON dream_runs(status, started_at DESC);
