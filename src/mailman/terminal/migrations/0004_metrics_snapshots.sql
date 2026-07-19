CREATE TABLE IF NOT EXISTS metrics_snapshots (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT    NOT NULL DEFAULT (datetime('now')),
  contacts      INTEGER NOT NULL DEFAULT 0,
  campaigns     INTEGER NOT NULL DEFAULT 0,
  dispatched    INTEGER NOT NULL DEFAULT 0,
  suppression   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics_snapshots (ts);
