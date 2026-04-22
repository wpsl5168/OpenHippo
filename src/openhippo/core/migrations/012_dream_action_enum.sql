-- Migration 012: dream_actions action enum constraint
-- White-list contains BOTH legacy values (consolidate_member/seed/forget/restore)
-- AND new F5 values (mark_dormant/restore_dormant/purge_dormant/consolidate_promoted)
-- to avoid breaking existing dream pipeline. Legacy values will be migrated to
-- new ones in a follow-up PR, then removed from the white-list.
-- dream_actions is append-only by contract → no UPDATE trigger needed.

CREATE TRIGGER IF NOT EXISTS dream_actions_bi_action
BEFORE INSERT ON dream_actions
WHEN NEW.action NOT IN (
  -- Legacy (compat with current dream pipeline)
  'consolidate_member', 'consolidate_seed', 'forget', 'restore',
  -- F5 v0.3 unified soft-delete
  'mark_dormant', 'restore_dormant', 'purge_dormant', 'consolidate_promoted'
)
BEGIN
  SELECT RAISE(ABORT, 'invalid dream_action: ' || NEW.action);
END;
