CREATE TABLE draft_audit (
    store_slot INTEGER PRIMARY KEY CHECK (store_slot BETWEEN 1 AND 8192),
    audit_id INTEGER NOT NULL CHECK (audit_id BETWEEN 1 AND 1024),
    tenant TEXT NOT NULL,
    project TEXT NOT NULL,
    draft TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 1024),
    previous_revision INTEGER NOT NULL CHECK (previous_revision = revision - 1),
    operation TEXT NOT NULL CHECK (operation IN ('create', 'edit', 'clone')),
    subject TEXT NOT NULL,
    key_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tenant, project, audit_id),
    UNIQUE (tenant, project, subject, key_digest),
    UNIQUE (tenant, project, draft, revision),
    FOREIGN KEY (tenant, project, draft, revision)
        REFERENCES draft_versions (tenant, project, draft, revision)
        DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE draft_versions (
    tenant TEXT NOT NULL,
    project TEXT NOT NULL,
    draft TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 1024),
    candidate TEXT NOT NULL CHECK (length(CAST(candidate AS BLOB)) <= 32768),
    source_draft TEXT,
    source_revision INTEGER,
    CHECK ((source_draft IS NULL) = (source_revision IS NULL)),
    CHECK (source_revision IS NULL OR source_revision BETWEEN 1 AND 1024),
    PRIMARY KEY (tenant, project, draft, revision),
    FOREIGN KEY (tenant, project, draft, revision)
        REFERENCES draft_audit (tenant, project, draft, revision),
    FOREIGN KEY (tenant, project, source_draft, source_revision)
        REFERENCES draft_versions (tenant, project, draft, revision)
) STRICT, WITHOUT ROWID;

CREATE TABLE pack_dispatch_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    identity TEXT NOT NULL
) STRICT;

INSERT INTO pack_dispatch_identity VALUES (1, lower(hex(randomblob(16))));
