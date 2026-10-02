-- 09-map-tasks.sql — mapping queue (flow: upload -> OCR -> map -> monitor)
-- DB is the queue of record (restart-safe, same pattern as OCR recovery).
CREATE TABLE IF NOT EXISTS map_tasks (
  id            BIGSERIAL PRIMARY KEY,
  document_id   TEXT NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
  folder        TEXT,
  rel_path      TEXT,
  tab_guess     TEXT,              -- from folder/filename hint: faktur_pajak|faktur_penjualan|po_customer|tanda_terima|other
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending -> claimed -> mapped | failed
  engine        TEXT,              -- hermes | api | hybrid (set on claim)
  attempts      INTEGER NOT NULL DEFAULT 0,
  error         TEXT,
  rows_summary  JSONB DEFAULT '{}'::jsonb,        -- {"po_customer": 3, "faktur_pajak": 1, ...}
  claimed_at    TIMESTAMPTZ,
  mapped_at     TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS map_tasks_status_idx ON map_tasks (status, created_at);
CREATE INDEX IF NOT EXISTS map_tasks_folder_idx ON map_tasks (folder);
