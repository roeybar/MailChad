-- Number of parallel human-emulated senders (v3.10)
ALTER TABLE campaigns ADD COLUMN human_send_count INTEGER NOT NULL DEFAULT 1;
