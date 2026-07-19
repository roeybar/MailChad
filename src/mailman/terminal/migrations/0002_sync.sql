-- v3.1 terminal sync tables (§3.3 + §13.4).
--
-- inbox        - events pulled from cloud, awaiting local materialisation
-- outbox       - local writes awaiting push to cloud
-- sync_state   - key-value: last_pulled_event_id cursor, etc.

CREATE TABLE IF NOT EXISTS inbox (
  -- The event_id assigned by cloud is the natural PK; idempotent on dupes.
  event_id          INTEGER PRIMARY KEY,
  table_name        TEXT    NOT NULL,
  row_id            TEXT    NOT NULL,
  revision          INTEGER NOT NULL,
  actor             TEXT    NOT NULL,
  modified_at       TEXT    NOT NULL,
  key_id            TEXT    NOT NULL,
  encrypted_payload BLOB,
  deleted           INTEGER NOT NULL DEFAULT 0,
  received_at       TEXT    NOT NULL DEFAULT (datetime('now')),
  materialised_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbox_row ON inbox (table_name, row_id, revision);
CREATE INDEX IF NOT EXISTS idx_inbox_unmaterialised ON inbox (materialised_at);

CREATE TABLE IF NOT EXISTS outbox (
  outbox_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name        TEXT    NOT NULL,
  row_id            TEXT    NOT NULL,
  revision          INTEGER NOT NULL,
  actor             TEXT    NOT NULL,
  modified_at       TEXT    NOT NULL,
  key_id            TEXT    NOT NULL,
  encrypted_payload BLOB,
  deleted           INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  pushed_at         TEXT,
  assigned_event_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpushed ON outbox (pushed_at, outbox_id);

CREATE TABLE IF NOT EXISTS sync_state (
  key        TEXT    PRIMARY KEY,
  value      TEXT    NOT NULL,
  updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
