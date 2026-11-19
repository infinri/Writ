CREATE TABLE publications (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    document TEXT NOT NULL,
    publication_digest TEXT NOT NULL UNIQUE
) STRICT;

CREATE TABLE publication_audit (
    sequence INTEGER PRIMARY KEY CHECK (sequence > 0),
    revision INTEGER NOT NULL CHECK (revision > 0),
    operation TEXT NOT NULL CHECK (operation IN ('publish', 'rollback', 'retire')),
    publication_version INTEGER NOT NULL REFERENCES publications(version),
    publication_digest TEXT NOT NULL,
    intent_digest TEXT NOT NULL,
    human TEXT NOT NULL,
    ceremony TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    at TEXT NOT NULL
) STRICT;

CREATE TABLE registry_head (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version TEXT NOT NULL CHECK (schema_version = 'writ-policy-registry/1'),
    tenant TEXT NOT NULL,
    project TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    publication_version INTEGER REFERENCES publications(version),
    audit_sequence INTEGER NOT NULL CHECK (audit_sequence >= 0),
    audit_digest TEXT,
    CHECK ((revision = 0) = (publication_version IS NULL)),
    CHECK ((audit_sequence = 0) = (audit_digest IS NULL)),
    CHECK (revision <= audit_sequence)
) STRICT;
