-- Per-day single-campaign contact lock (v3.21).
-- A dispatched campaign locks its contacts until its confirmation window elapses
-- AND a post-send unsub pull has run (Option B). Prevents a contact being on
-- more than one running campaign per day (anti-spam + compliance + no info loss).
ALTER TABLE campaigns ADD COLUMN lock_until          TEXT;          -- set at dispatch
ALTER TABLE campaigns ADD COLUMN unsubs_confirmed_at TEXT;          -- set when window elapsed + pull ran
ALTER TABLE campaigns ADD COLUMN lock_duration_s     INTEGER NOT NULL DEFAULT 86400;  -- default 1 day

-- Tracks the wall-clock of the last successful unsub pull, so the scheduler can
-- prove a pull ran *after* a campaign dispatched before confirming its lock.
INSERT OR IGNORE INTO sync_state (key, value) VALUES ('last_pull_ok_at', '');
