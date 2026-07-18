-- Scope-aware suppression (v3.21).
-- 'all' = unsubscribed from everything; 'promotional' = marketing only
-- (transactional sends bypass promotional-scope unsubs).
ALTER TABLE suppression_hashes ADD COLUMN scope TEXT NOT NULL DEFAULT 'all';

-- Cursor for the unsub-cache pull that rides /sync/pull.
INSERT OR IGNORE INTO sync_state (key, value) VALUES ('last_pulled_unsub_at', '');
