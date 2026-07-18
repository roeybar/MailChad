-- Cloud settings - key/value config moved out of env vars (v3.1 Q1).
--
-- Per Q1: every operator-facing config moves to UI-editable settings.
-- Only BOOTSTRAP_TOKEN stays env-only (chicken-egg with handshake auth).
-- Tier 2 tuning knobs (poll timeouts, pace, etc.) stay env with optional
-- settings override.
--
-- audit trail: every set() writes updated_by + updated_at so we can see
-- which operator/client/system actor changed what.

CREATE TABLE IF NOT EXISTS settings (
  key         TEXT    PRIMARY KEY,
  value       TEXT    NOT NULL,                -- plaintext for non-secrets; base64(AES-256-GCM(KEK, ...)) for secrets
  is_secret   INTEGER NOT NULL DEFAULT 0,      -- 0/1; if 1, value is encrypted at rest
  updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_by  TEXT    NOT NULL DEFAULT 'system'
);
