-- 013: Add originator (user/assistant) and channel (weixin/feishu/cli/...) to cold_memory.
-- Backwards compatible: existing rows have NULL for both columns; UI handles null gracefully.

ALTER TABLE cold_memory ADD COLUMN originator TEXT;
ALTER TABLE cold_memory ADD COLUMN channel TEXT;
CREATE INDEX IF NOT EXISTS idx_cold_originator ON cold_memory(originator);
CREATE INDEX IF NOT EXISTS idx_cold_channel ON cold_memory(channel);
