-- 06-invoice-line-items.sql — one normalized row per OCR-mapped table item
-- Migration-only: no runtime DDL. Keeps invoice_rows as document header/audit row.
CREATE TABLE IF NOT EXISTS invoice_line_items (
  id               BIGSERIAL PRIMARY KEY,
  document_id      TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  invoice_row_id   BIGINT REFERENCES invoice_rows(id) ON DELETE CASCADE,
  line_no          INTEGER NOT NULL,
  source_page      INTEGER,
  item_code        TEXT,
  description      TEXT,
  quantity         NUMERIC(16,4),
  uom              TEXT,
  unit_price       NUMERIC(16,2),
  amount           NUMERIC(16,2),
  raw_item         JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (document_id, line_no)
);
CREATE INDEX IF NOT EXISTS invoice_line_items_document_idx
  ON invoice_line_items (document_id, source_page, line_no);
CREATE INDEX IF NOT EXISTS invoice_line_items_code_idx
  ON invoice_line_items (item_code);
