-- Composite index to speed up the materialiser poll:
-- SELECT ... FROM inbox WHERE materialised_at IS NULL AND table_name IN (...)
CREATE INDEX IF NOT EXISTS idx_inbox_unmaterialised_type
  ON inbox (table_name, materialised_at);
