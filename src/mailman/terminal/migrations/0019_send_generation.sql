-- send generations. relaunch_gen is the campaign's current send generation (0 = the
-- batched main send; k = the k-th Re-launch); relaunch_open is 1 until that Re-launch has
-- recorded all of its packs.
ALTER TABLE campaigns ADD COLUMN relaunch_gen INTEGER NOT NULL DEFAULT 0;
ALTER TABLE campaigns ADD COLUMN relaunch_open INTEGER NOT NULL DEFAULT 0;
-- relaunch_seq counts Re-launch presses; the page sends the value it showed, so a press from
-- a stale page (or a second tab) is refused instead of finishing or resending twice.
ALTER TABLE campaigns ADD COLUMN relaunch_seq INTEGER NOT NULL DEFAULT 0;
-- Set just before a batch's packs are pushed, cleared when they are recorded: non-NULL
-- means the dispatch was interrupted and its packs may be live on the cloud.
ALTER TABLE campaign_batches ADD COLUMN dispatch_started_at TEXT;
