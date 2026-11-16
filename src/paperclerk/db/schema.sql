-- paperclerk schema. Applied idempotently on startup.

PRAGMA foreign_keys = ON;

-- One row per unique file content (local file or email attachment).
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT    NOT NULL UNIQUE,      -- exact-duplicate detection
    path          TEXT,                         -- current local path (NULL for email-only)
    source        TEXT    NOT NULL CHECK (source IN ('local', 'gmail')),
    source_ref    TEXT,                         -- e.g. Gmail message id
    mime_type     TEXT,
    size_bytes    INTEGER,
    mtime         REAL,                         -- for incremental rescans
    text          TEXT,                         -- extracted text
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- LLM classification + structured fields for a document.
CREATE TABLE IF NOT EXISTS extractions (
    id             INTEGER PRIMARY KEY,
    document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    doc_type       TEXT    NOT NULL,           -- see extract/schemas.py DocType
    vendor         TEXT,
    amount         REAL,
    currency       TEXT,
    issue_date     TEXT,
    due_date       TEXT,
    is_recurring   INTEGER,                    -- 0/1/NULL
    period         TEXT,                       -- monthly / yearly / ...
    confidence     REAL    NOT NULL,
    needs_review   INTEGER NOT NULL DEFAULT 0,
    human_verified INTEGER NOT NULL DEFAULT 0, -- corrected/approved by the user
    model          TEXT,                       -- which model produced it
    raw_json       TEXT,                       -- full model output, for debugging/evals
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Recurring payments aggregated from extractions.
CREATE TABLE IF NOT EXISTS subscriptions (
    id             INTEGER PRIMARY KEY,
    vendor         TEXT    NOT NULL,
    amount         REAL,
    currency       TEXT,
    period         TEXT,
    next_due_date  TEXT,
    status         TEXT    NOT NULL DEFAULT 'active',
    last_document_id INTEGER REFERENCES documents(id),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (vendor, currency, period)
);

-- Append-only log of file operations, grouped by batch, for undo.
CREATE TABLE IF NOT EXISTS operations (
    id          INTEGER PRIMARY KEY,
    batch_id    TEXT    NOT NULL,
    op          TEXT    NOT NULL CHECK (op IN ('move', 'rename', 'mkdir')),
    src_path    TEXT,
    dst_path    TEXT,
    document_id INTEGER REFERENCES documents(id),
    status      TEXT    NOT NULL DEFAULT 'applied' CHECK (status IN ('applied', 'undone', 'failed')),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_extractions_document ON extractions(document_id);
CREATE INDEX IF NOT EXISTS idx_operations_batch ON operations(batch_id);
