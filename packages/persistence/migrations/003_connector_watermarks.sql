ALTER TABLE connector_offsets ALTER COLUMN cursor_value DROP NOT NULL;
ALTER TABLE connector_offsets ADD COLUMN IF NOT EXISTS watermark timestamptz;
