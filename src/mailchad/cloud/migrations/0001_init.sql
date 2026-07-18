-- v3.1 cloud schema - dumb storage + execution loop.
--
-- Per spec §1: cloud holds only what the sync protocol moves through it.
-- Cannot decrypt K_op+K_cl payloads (no private keys). Can decrypt K_temp
-- payloads during TTL (key on filesystem via cloud/app/keys.py).

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- event_log
-- The sync wire (§3.1). Every write from a terminal becomes a row here.
CREATE TABLE IF NOT EXISTS event_log (
  event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name        TEXT    NOT NULL,
  row_id            TEXT    NOT NULL,
  revision          INTEGER NOT NULL,
  actor             TEXT    NOT NULL CHECK (actor IN ('operator', 'client')),
  modified_at       TEXT    NOT NULL,
  key_id            TEXT    NOT NULL,
  encrypted_payload BLOB,
  deleted           INTEGER NOT NULL DEFAULT 0,
  received_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_event_log_row    ON event_log (table_name, row_id, revision);
CREATE INDEX IF NOT EXISTS idx_event_log_actor  ON event_log (actor, received_at);

-- pack
CREATE TABLE IF NOT EXISTS pack (
  pack_id           TEXT    PRIMARY KEY,
  campaign_id       INTEGER NOT NULL,
  recipient_hash    TEXT    NOT NULL,
  content_hash      TEXT    NOT NULL,
  send_at           TEXT    NOT NULL,
  key_id            TEXT    NOT NULL,
  encrypted_payload BLOB,
  status            TEXT    NOT NULL CHECK (status IN
                      ('pending', 'claimed', 'sent', 'failed', 'stuck_no_key', 'cancelled')
                    ) DEFAULT 'pending',
  enqueued_at       TEXT    NOT NULL DEFAULT (datetime('now')),
  claimed_at        TEXT,
  sent_at           TEXT,
  resend_message_id TEXT,
  failure_reason    TEXT,
  pushed_by         TEXT    NOT NULL CHECK (pushed_by IN ('operator', 'client'))
);
CREATE INDEX IF NOT EXISTS idx_pack_pending ON pack (status, send_at);
CREATE INDEX IF NOT EXISTS idx_pack_campaign ON pack (campaign_id);

-- near_conflict_log
CREATE TABLE IF NOT EXISTS near_conflict_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name        TEXT    NOT NULL,
  row_id            TEXT    NOT NULL,
  event_id_a        INTEGER NOT NULL,
  event_id_b        INTEGER NOT NULL,
  actor_a           TEXT    NOT NULL,
  actor_b           TEXT    NOT NULL,
  modified_at_a     TEXT    NOT NULL,
  modified_at_b     TEXT    NOT NULL,
  delta_seconds     REAL    NOT NULL,
  detected_at       TEXT    NOT NULL DEFAULT (datetime('now')),
  acknowledged_at   TEXT,
  acknowledged_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_near_conflict_unack ON near_conflict_log (acknowledged_at);

-- terminal_session
CREATE TABLE IF NOT EXISTS terminal_session (
  bearer_hash       TEXT    PRIMARY KEY,
  actor             TEXT    NOT NULL CHECK (actor IN ('operator', 'client')),
  created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  last_seen_at      TEXT,
  revoked_at        TEXT
);

-- pubkey
CREATE TABLE IF NOT EXISTS pubkey (
  actor             TEXT    PRIMARY KEY CHECK (actor IN ('operator', 'client')),
  kem_pub           BLOB    NOT NULL,
  registered_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- webhook_event_raw
CREATE TABLE IF NOT EXISTS webhook_event_raw (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  svix_id           TEXT    UNIQUE,
  event_type        TEXT,
  message_id        TEXT,
  received_at       TEXT    NOT NULL DEFAULT (datetime('now')),
  forwarded_event_id INTEGER REFERENCES event_log(event_id)
);
