-- Rename companies -> entities; add CAN-SPAM required fields (v3.11)
ALTER TABLE companies RENAME TO entities;
ALTER TABLE campaigns RENAME COLUMN company_id TO entity_id;

-- CAN-SPAM §5 requires physical mailing address in every commercial email footer
ALTER TABLE entities ADD COLUMN footer_address TEXT DEFAULT NULL;
-- Support contact (optional but shown in compliance UI)
ALTER TABLE entities ADD COLUMN support_email  TEXT DEFAULT NULL;
-- Public host for unsubscribe/erasure URL generation (required for app function)
ALTER TABLE entities ADD COLUMN public_host    TEXT DEFAULT NULL;
