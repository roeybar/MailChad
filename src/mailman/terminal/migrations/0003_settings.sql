-- Terminal settings - key/value config moved out of env vars (v3.1 Q1).
-- Mirror of cloud/app/migrations/0002_settings.sql.

CREATE TABLE IF NOT EXISTS settings (
  key         TEXT    PRIMARY KEY,
  value       TEXT    NOT NULL,                -- plaintext for non-secrets; base64(AES-256-GCM(KEK, ...)) for secrets
  is_secret   INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_by  TEXT    NOT NULL DEFAULT 'system'
);
