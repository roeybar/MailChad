-- v3.22 - batched, windowed sending + seed addresses.

-- Ordered 1000-contact batches per campaign. The terminal computes a send_at per
-- recipient (window + 3-sender + rush) and loads them; the cloud fires by send_at.
CREATE TABLE IF NOT EXISTS campaign_batches (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id       INTEGER NOT NULL,
  batch_no          INTEGER NOT NULL,           -- 1-based
  size              INTEGER NOT NULL DEFAULT 0,  -- real recipients (excl. seeds)
  status            TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','scheduled','sending','drained','cooldown','approved','done','cancelled')),
  window_start      TEXT,                        -- first send_at (UTC)
  window_end        TEXT,                        -- last send_at (UTC)
  loaded_at         TEXT,                        -- when packs were pushed to cloud
  drained_at        TEXT,                        -- when the last pack's send_at passed
  approve_unlock_at TEXT,                        -- cooldown gate: approve-next unlocks at/after this
  approved_at       TEXT,
  -- rolling outcome counts (filled by analytics)
  sent INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0,
  opened INTEGER NOT NULL DEFAULT 0, clicked INTEGER NOT NULL DEFAULT 0,
  bounced INTEGER NOT NULL DEFAULT 0, complained INTEGER NOT NULL DEFAULT 0,
  unsub INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (campaign_id, batch_no)
);
CREATE INDEX IF NOT EXISTS idx_campaign_batches_campaign ON campaign_batches(campaign_id);

ALTER TABLE campaign_recipients ADD COLUMN batch_no INTEGER;

-- Seed / monitor addresses salted into every batch for inbox-placement checks.
-- is_seed recipients are excluded from real analytics, suppression and the lock.
CREATE TABLE IF NOT EXISTS seed_addresses (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  email       TEXT    NOT NULL UNIQUE,
  provider    TEXT,                              -- 'gmail','outlook','yahoo','icloud','corporate'…
  active      INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Manual placement log: one row per (batch, seed) the operator checks.
CREATE TABLE IF NOT EXISTS seed_placements (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id    INTEGER NOT NULL,
  seed_email  TEXT    NOT NULL,
  placement   TEXT    CHECK (placement IN ('inbox','spam','promotions','missing','unknown')),
  checked_at  TEXT,
  note        TEXT
);

-- Mark seed recipients so analytics/lock skip them.
ALTER TABLE campaign_recipients ADD COLUMN is_seed INTEGER NOT NULL DEFAULT 0;

-- v3.22 send-strategy settings (defaults; editable in admin).
INSERT OR IGNORE INTO settings (key, value) VALUES ('send_window_start_hour', '9');
INSERT OR IGNORE INTO settings (key, value) VALUES ('send_window_hours', '4');
INSERT OR IGNORE INTO settings (key, value) VALUES ('send_window_tz', 'America/Los_Angeles');
INSERT OR IGNORE INTO settings (key, value) VALUES ('send_sender_count', '3');
INSERT OR IGNORE INTO settings (key, value) VALUES ('send_rush_tail_minutes', '30');
INSERT OR IGNORE INTO settings (key, value) VALUES ('send_batch_size', '1000');
INSERT OR IGNORE INTO settings (key, value) VALUES ('send_jitter_min_s', '60');
INSERT OR IGNORE INTO settings (key, value) VALUES ('send_jitter_max_s', '210');
INSERT OR IGNORE INTO settings (key, value) VALUES ('send_rush_jitter_s', '15');
