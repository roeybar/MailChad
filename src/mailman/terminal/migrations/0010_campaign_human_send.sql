-- Human-send emulator: randomises send interval between min_gap_s and max_gap_s (v3.10)
ALTER TABLE campaigns ADD COLUMN human_send        INTEGER NOT NULL DEFAULT 0;    -- 1 = enabled
ALTER TABLE campaigns ADD COLUMN human_send_min_s  INTEGER NOT NULL DEFAULT 60;   -- 1 min
ALTER TABLE campaigns ADD COLUMN human_send_max_s  INTEGER NOT NULL DEFAULT 210;  -- 3.5 mins
