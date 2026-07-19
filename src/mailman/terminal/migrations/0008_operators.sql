CREATE TABLE IF NOT EXISTS operators (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  email          TEXT    NOT NULL UNIQUE,
  password_hash  TEXT    NOT NULL,
  role           TEXT    NOT NULL DEFAULT 'operator' CHECK (role IN ('admin', 'operator')),
  created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
  last_login_at  TEXT,
  active         INTEGER NOT NULL DEFAULT 1
);
