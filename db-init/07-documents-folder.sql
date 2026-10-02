-- 07: folder/DFS provenance for documents (folder upload feature 2026-10-02)
ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS folder TEXT,
  ADD COLUMN IF NOT EXISTS rel_path TEXT;

CREATE INDEX IF NOT EXISTS idx_documents_folder ON documents(folder);
