-- 04-add-others.sql — preserve unmapped details for every document type
-- Migration-only: no runtime DDL. Safe to apply to existing databases.
ALTER TABLE invoice_rows
  ADD COLUMN IF NOT EXISTS others TEXT;

COMMENT ON COLUMN invoice_rows.others IS
  'Free-text details not represented by the structured columns; retained for every document class.';

-- Backfill rows created before this migration when source JSON already contains notes.
UPDATE invoice_rows r
SET others = COALESCE(
  NULLIF(d.standard_json->'mapped'->>'others',''),
  NULLIF(d.standard_json->'classified'->>'others',''),
  NULLIF(d.standard_json->'mapped'->>'notes',''),
  NULLIF(d.standard_json->'classified'->>'notes','')
)
FROM documents d
WHERE d.id = r.document_id
  AND COALESCE(r.others, '') = '';
