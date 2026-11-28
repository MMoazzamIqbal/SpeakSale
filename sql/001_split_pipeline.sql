BEGIN;

ALTER TABLE recordings
    ADD COLUMN IF NOT EXISTS raw_transcript text;

ALTER TABLE recordings
    ADD COLUMN IF NOT EXISTS error_log jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE recordings
    ADD COLUMN IF NOT EXISTS extracted_json jsonb;

ALTER TABLE recordings
    ADD COLUMN IF NOT EXISTS processed_at timestamptz;

ALTER TABLE sales
    ADD COLUMN IF NOT EXISTS file_id integer;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'sales_file_id_fkey'
    ) THEN
        ALTER TABLE sales
            ADD CONSTRAINT sales_file_id_fkey
            FOREIGN KEY (file_id) REFERENCES recordings(file_id);
    END IF;
END $$;

ALTER TABLE sale_items
    ADD COLUMN IF NOT EXISTS discount_pct numeric(5,2) NOT NULL DEFAULT 0;

COMMIT;
