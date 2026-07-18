-- Rate limiting fields on campaigns (v3.10)
ALTER TABLE campaigns ADD COLUMN rate_limit_per_min INTEGER DEFAULT NULL;  -- NULL = unlimited
ALTER TABLE campaigns ADD COLUMN bounce_pause_pct   REAL    DEFAULT 0.10;  -- 0.10 = 10% triggers emergency halving
