-- Track Resend message ID returned by dispatcher once a pack is resolved.
ALTER TABLE dispatched_job ADD COLUMN resend_message_id TEXT;
