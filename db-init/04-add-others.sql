-- 04-add-others.sql — preserve unmapped details for every document type
-- Migration-only: no runtime DDL. Safe to apply to existing databases.
ALTER TABLE invoice_rows
  ADD COLUMN IF NOT EXISTS others TEXT;

COMMENT ON COLUMN invoice_rows.others IS
  'Free-text details not represented by the structured columns; retained for every document class.';
