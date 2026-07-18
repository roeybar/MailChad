-- Multi-stage (drip) campaign support (v3.13)
CREATE TABLE IF NOT EXISTS campaign_stages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    stage_number  INTEGER NOT NULL DEFAULT 1,
    template_id   INTEGER NOT NULL REFERENCES templates(id),
    scheduled_for TEXT NOT NULL,          -- ISO datetime, required
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | dispatched | paused | failed
    dispatched_at TEXT,
    note          TEXT,                   -- operator label e.g. "Day 3 follow-up"
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(campaign_id, stage_number)
);
CREATE INDEX IF NOT EXISTS idx_campaign_stages_sched ON campaign_stages (status, scheduled_for);
