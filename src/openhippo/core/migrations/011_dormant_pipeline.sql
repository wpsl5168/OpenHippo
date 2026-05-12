-- Migration 011: F5 unified soft-delete pipeline
-- 1. dream_status 值域 trigger (CHECK constraint via trigger, both INSERT and UPDATE)
-- 2. purge hot-path partial index
-- 3. backfill 23 legacy test residue rows to dormant + audit

-- ─── 1. dream_status value-domain triggers ───
CREATE TRIGGER IF NOT EXISTS cold_memory_bi_dreamstatus
BEFORE INSERT ON cold_memory
WHEN NEW.dream_status NOT IN ('active', 'dormant', 'consolidated')
BEGIN
  SELECT RAISE(ABORT, 'invalid dream_status: ' || COALESCE(NEW.dream_status, 'NULL'));
END;

CREATE TRIGGER IF NOT EXISTS cold_memory_bu_dreamstatus
BEFORE UPDATE OF dream_status ON cold_memory
WHEN NEW.dream_status NOT IN ('active', 'dormant', 'consolidated')
BEGIN
  SELECT RAISE(ABORT, 'invalid dream_status: ' || COALESCE(NEW.dream_status, 'NULL'));
END;

-- ─── 2. partial index for purge job hot path ───
CREATE INDEX IF NOT EXISTS idx_cold_dormant_purge
  ON cold_memory(last_dream_at) WHERE dream_status = 'dormant';

-- ─── 3. backfill legacy test residue rows ───
-- Synthetic dream_run satisfies 008's FOREIGN KEY (dream_run_id) REFERENCES dream_runs(id).
-- INSERT OR IGNORE makes this migration idempotent across re-runs / fixture resets.
-- SCOPE: only '[snapshot ...' rows. The earlier draft also matched '[2026-%'
-- but inspection showed those are real user conversation entries (timestamp
-- prefix on normal notes). Soft-deleting them would violate F5's零信任铁律.
-- Only insert the synthetic dream_run if there's actually something to backfill,
-- otherwise empty/fresh DBs (CI fixtures) get a phantom run that breaks
-- list_runs() == [] assertions.
INSERT OR IGNORE INTO dream_runs
  (id, started_at, finished_at, status, config_snapshot,
   candidates_count, clusters_count, consolidated_count, forgotten_count)
SELECT
  'migration:011:backfill',
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
  'migration',
  '{"migration":"011","reason":"legacy_test_residue","scope":"snapshot_prefix_only"}',
  0, 0, 0, 0
WHERE EXISTS (
  SELECT 1 FROM cold_memory
   WHERE COALESCE(dream_status, 'active') = 'active'
     AND content LIKE '[snapshot %'
);

-- Mark active legacy test residue rows as dormant
UPDATE cold_memory
   SET dream_status = 'dormant',
       last_dream_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
 WHERE COALESCE(dream_status, 'active') = 'active'
   AND content LIKE '[snapshot %';

-- Audit row per backfilled memory; idempotent via NOT EXISTS guard.
-- reason='legacy_test_residue' → purge job uses 0d retention (immediate cleanup eligible)
INSERT INTO dream_actions (dream_run_id, action, memory_id, details, created_at)
SELECT 'migration:011:backfill',
       'mark_dormant',
       cm.id,
       json_object('actor',       'migration',
                   'reason',      'legacy_test_residue',
                   'source_tier', 'cold',
                   'original_id', NULL),
       strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
  FROM cold_memory cm
 WHERE cm.dream_status = 'dormant'
   AND cm.content LIKE '[snapshot %'
   AND NOT EXISTS (
     SELECT 1 FROM dream_actions da
      WHERE da.dream_run_id = 'migration:011:backfill'
        AND da.memory_id    = cm.id
   );
