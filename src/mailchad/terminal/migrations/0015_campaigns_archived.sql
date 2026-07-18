-- Add 'archived' to campaigns status constraint (v3.14)
PRAGMA foreign_keys = OFF;
DROP TABLE IF EXISTS campaigns_new;
CREATE TABLE campaigns_new (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT    NOT NULL,
  template_id       INTEGER NOT NULL REFERENCES templates(id),
  kind              TEXT    NOT NULL CHECK (kind IN ('transactional', 'promotional')),
  entity_id         INTEGER REFERENCES entities(id) ON DELETE SET NULL,
  status            TEXT    NOT NULL DEFAULT 'draft'
                            CHECK (status IN ('draft','tested','scheduled','dispatched',
                                              'sending','sent','cancelled','paused','failed','archived')),
  test_sent_at      TEXT,
  template_hash_at_test TEXT,
  scheduled_for     TEXT,
  dispatched_at     TEXT,
  sent_at           TEXT,
  recipient_count   INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  rate_limit_per_min INTEGER DEFAULT NULL,
  bounce_pause_pct   REAL    DEFAULT 0.10,
  human_send        INTEGER NOT NULL DEFAULT 0,
  human_send_min_s  INTEGER NOT NULL DEFAULT 60,
  human_send_max_s  INTEGER NOT NULL DEFAULT 210,
  human_send_count  INTEGER NOT NULL DEFAULT 1
);
INSERT INTO campaigns_new (
  id, name, template_id, kind, entity_id, status,
  test_sent_at, template_hash_at_test, scheduled_for, dispatched_at, sent_at,
  recipient_count, created_at, updated_at,
  rate_limit_per_min, bounce_pause_pct,
  human_send, human_send_min_s, human_send_max_s, human_send_count
) SELECT
  id, name, template_id, kind, entity_id, status,
  test_sent_at, template_hash_at_test, scheduled_for, dispatched_at, sent_at,
  recipient_count, created_at, updated_at,
  rate_limit_per_min, bounce_pause_pct,
  human_send, human_send_min_s, human_send_max_s, human_send_count
FROM campaigns;
DROP TABLE campaigns;
ALTER TABLE campaigns_new RENAME TO campaigns;
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns (status);
PRAGMA foreign_keys = ON;
