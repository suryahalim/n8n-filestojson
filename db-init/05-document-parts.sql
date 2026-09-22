-- 05-document-parts.sql — parent PDF to child-document relationships
ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS parent_document_id TEXT REFERENCES documents(id),
  ADD COLUMN IF NOT EXISTS page_start INTEGER,
  ADD COLUMN IF NOT EXISTS page_end INTEGER;

CREATE INDEX IF NOT EXISTS idx_documents_parent ON documents(parent_document_id);

CREATE TABLE IF NOT EXISTS document_parts (
    id BIGSERIAL PRIMARY KEY,
    parent_document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    child_document_id TEXT NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    detected_class TEXT,
    confidence TEXT,
    status TEXT NOT NULL DEFAULT 'CREATED',
    created TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_document_parts_parent ON document_parts(parent_document_id);
