-- v3 vault schema (ported from email-platform-v2/app/migrations/0001_init.sql).
--
-- This is the SOURCE OF TRUTH for everything. Vault is the only place
-- with these tables; front-edge has caches, backup has snapshots.
--
-- Three v3-specific tables added at the end: dispatched_job, drift_report,
-- audit_event. These power the spec's "Queue management and drift detection"
-- section.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- contacts
CREATE TABLE IF NOT EXISTS contacts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  email           TEXT    NOT NULL UNIQUE,
  name            TEXT,
  tags            TEXT,
  consent_ts      TEXT    NOT NULL,
  consent_source  TEXT    NOT NULL,
  external_id     TEXT    UNIQUE,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_contacts_email       ON contacts (email);
CREATE INDEX IF NOT EXISTS idx_contacts_external_id ON contacts (external_id);

-- templates
CREATE TABLE IF NOT EXISTS templates (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT    NOT NULL UNIQUE,
  subject         TEXT    NOT NULL,
  from_name       TEXT    NOT NULL,
  html_body       TEXT    NOT NULL,
  text_body       TEXT,
  template_hash   TEXT    NOT NULL,
  tracking_enabled INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- campaigns
CREATE TABLE IF NOT EXISTS campaigns (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  name                        TEXT    NOT NULL,
  template_id                 INTEGER NOT NULL REFERENCES templates(id),
  kind                        TEXT    NOT NULL CHECK (kind IN ('transactional', 'promotional')),
  status                      TEXT    NOT NULL CHECK (status IN ('draft', 'tested', 'scheduled', 'dispatched', 'sending', 'sent', 'cancelled')) DEFAULT 'draft',
  test_sent_at                TEXT,
  template_hash_at_test       TEXT,
  scheduled_for               TEXT,
  dispatched_at               TEXT,
  sent_at                     TEXT,
  recipient_count             INTEGER NOT NULL DEFAULT 0,
  created_at                  TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at                  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns (status);

-- campaign_recipients
CREATE TABLE IF NOT EXISTS campaign_recipients (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id     INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  contact_id      INTEGER NOT NULL REFERENCES contacts(id),
  message_id      TEXT,
  status          TEXT    NOT NULL CHECK (status IN ('queued', 'sent', 'delivered', 'opened', 'clicked', 'bounced', 'complained', 'failed', 'cancelled')) DEFAULT 'queued',
  sent_at         TEXT,
  delivered_at    TEXT,
  opened_at       TEXT,
  clicked_at      TEXT,
  bounced_at      TEXT,
  complained_at   TEXT,
  failure_reason  TEXT,
  UNIQUE (campaign_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_campaign_recipients_message  ON campaign_recipients (message_id);
CREATE INDEX IF NOT EXISTS idx_campaign_recipients_campaign ON campaign_recipients (campaign_id);

-- events
CREATE TABLE IF NOT EXISTS events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type      TEXT    NOT NULL,
  message_id      TEXT,
  recipient       TEXT,
  payload_json    TEXT    NOT NULL,
  received_at     TEXT    NOT NULL DEFAULT (datetime('now')),
  synced_from_edge_at TEXT  -- when vault pulled this from front-edge cache
);
CREATE INDEX IF NOT EXISTS idx_events_message ON events (message_id);
CREATE INDEX IF NOT EXISTS idx_events_type    ON events (event_type);

-- suppression_hashes - THE legally-binding table
CREATE TABLE IF NOT EXISTS suppression_hashes (
  email_hash      TEXT    PRIMARY KEY,
  reason          TEXT    NOT NULL CHECK (reason IN ('unsubscribe', 'complaint', 'bounce_hard', 'manual', 'erasure_request')),
  added_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  source          TEXT             -- "webhook:email.bounced", "u/<token>", "admin:user@x", etc.
);

-- v3 ADDITIONS

-- dispatched_job: what the vault TOLD front-edge to send. Used for
-- drift detection against front-edge's actual send_queue.
CREATE TABLE IF NOT EXISTS dispatched_job (
  job_id          TEXT    PRIMARY KEY,            -- uuid; matches front-edge's send_queue.job_id
  campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
  recipient_id    INTEGER NOT NULL REFERENCES contacts(id),
  recipient_hash  TEXT    NOT NULL,               -- sha256 for drift-detection without leak
  status          TEXT    NOT NULL CHECK (status IN ('dispatched', 'acked', 'completed', 'failed', 'cancelled')) DEFAULT 'dispatched',
  dispatched_at   TEXT    NOT NULL DEFAULT (datetime('now')),
  acked_at        TEXT,
  completed_at    TEXT,
  content_hash    TEXT    NOT NULL                -- sha256 of (subject + html); used for content-mismatch drift
);
CREATE INDEX IF NOT EXISTS idx_dispatched_job_campaign ON dispatched_job (campaign_id);
CREATE INDEX IF NOT EXISTS idx_dispatched_job_status   ON dispatched_job (status);

-- drift_report: produced by every wake's drift_check. Persisted so the
-- operator can review history.
CREATE TABLE IF NOT EXISTS drift_report (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  detected_at     TEXT    NOT NULL DEFAULT (datetime('now')),
  category        TEXT    NOT NULL CHECK (category IN ('missing_on_edge', 'missing_on_vault', 'status_mismatch', 'content_mismatch')),
  severity        TEXT    NOT NULL CHECK (severity IN ('INFO', 'WARN', 'CRITICAL')),
  job_id          TEXT,
  details_json    TEXT    NOT NULL,
  acknowledged_at TEXT,
  acknowledged_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_drift_report_severity ON drift_report (severity, acknowledged_at);

-- audit_event: append-only log of operator actions + system actions.
-- Critical for after-the-fact investigation of drift / suspected
-- intrusion / compliance proof.
CREATE TABLE IF NOT EXISTS audit_event (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  occurred_at     TEXT    NOT NULL DEFAULT (datetime('now')),
  actor           TEXT    NOT NULL,               -- "operator:<email>" or "system:<component>"
  action          TEXT    NOT NULL,               -- "campaign.launched", "drift.acknowledged", "erasure.propagated", etc.
  target          TEXT,                            -- "campaign:123", "user_hash:abc", etc.
  details_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_occurred ON audit_event (occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_action   ON audit_event (action);
