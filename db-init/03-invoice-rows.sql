-- 03-invoice-rows.sql — the STANDARD vendor-invoice table (v1.5)
-- Populated automatically after OCR+mapping (extractor upserts).
-- RPA lifecycle: status 'extracted' -> pull GET /invoices?status=extracted
--                -> PATCH /invoices/{document_id} {"status":"pending_review"}
--                -> human/review -> {"status":"mapped", rpa_* final values}
CREATE TABLE IF NOT EXISTS invoice_rows (
  id                     BIGSERIAL PRIMARY KEY,
  document_id            TEXT NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
  filename               TEXT,
  -- extraction/mapping result
  vendor_name            TEXT,
  vendor_npwp            TEXT,
  vendor_address         TEXT,
  doc_type_label         TEXT,
  invoice_number         TEXT,
  invoice_number_source  TEXT,           -- printed|handwritten|stamp|inferred
  invoice_number_confidence TEXT,        -- high|medium|low
  invoice_date           DATE,
  ref_po                 TEXT,
  currency               TEXT DEFAULT 'IDR',
  subtotal               NUMERIC(16,2),
  tax                    NUMERIC(16,2),
  total                  NUMERIC(16,2),
  total_as_written       TEXT,
  line_items             JSONB DEFAULT '[]'::jsonb,
  handwritten            JSONB DEFAULT '[]'::jsonb,
  confidence             TEXT,
  missing                TEXT,           -- ';' joined
  notes                  TEXT,
  mapper_model           TEXT,
  mapped_at              TIMESTAMPTZ DEFAULT now(),
  -- workflow: extracted -> pending_review -> mapped
  status                 TEXT NOT NULL DEFAULT 'extracted',
  -- RPA review write-back (final ledger values; fall back to extracted when unchanged)
  rpa_vendor             TEXT,
  rpa_invoice_number     TEXT,
  rpa_date               DATE,
  rpa_total              NUMERIC(16,2),
  rpa_note               TEXT,
  doc_class              TEXT,
  money_doc              BOOLEAN DEFAULT true,
  rpa_status             TEXT,          -- ok|corrected|rejected
  rpa_reviewed_at        TIMESTAMPTZ,
  created_at             TIMESTAMPTZ DEFAULT now(),
  updated_at             TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS invoice_rows_status_idx ON invoice_rows (status, invoice_date);
