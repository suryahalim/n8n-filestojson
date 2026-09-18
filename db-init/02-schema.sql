-- Document pipeline schema (states mirror the flow diagram)
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime TEXT,
    size INTEGER,
    stored_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'UPLOADED',  -- UPLOADED->EXTRACTED->VALIDATED|FLAGGED->PUBLISHED->DELIVERED|FAILED
    standard_json JSONB,
    validation JSONB,
    review_loop INTEGER NOT NULL DEFAULT 0,
    created TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    id SERIAL PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    kind TEXT NOT NULL,             -- extract | deliver
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING->RUNNING->DONE|FAILED
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (kind, status);

CREATE TABLE IF NOT EXISTS delivery_tasks (
    id TEXT PRIMARY KEY,            -- uuid = correlation id on the service bus
    document_id TEXT NOT NULL REFERENCES documents(id),
    target_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED',   -- QUEUED->IN_FLIGHT->DELIVERED|RETRY_WAIT|FAILED
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT,
    delivered_at TIMESTAMPTZ,
    created TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON delivery_tasks (status, next_attempt_at);
