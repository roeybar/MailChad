CREATE TABLE IF NOT EXISTS companies (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT    NOT NULL UNIQUE,
  domain         TEXT    NOT NULL,
  from_name      TEXT    NOT NULL DEFAULT '',
  from_email     TEXT    NOT NULL DEFAULT '',
  resend_key_enc TEXT,              -- AES-GCM encrypted via settings KEK; NULL = not set
  created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- optional company association for campaigns
ALTER TABLE campaigns ADD COLUMN company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL;
