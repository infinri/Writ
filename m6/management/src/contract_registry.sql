CREATE TABLE contract_owner (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    tenant TEXT NOT NULL,
    project TEXT NOT NULL,
    dependency_count INTEGER CHECK (dependency_count BETWEEN 0 AND 128)
) STRICT;

CREATE TABLE contract_packs (
    name TEXT PRIMARY KEY
) STRICT, WITHOUT ROWID;

CREATE TABLE contract_dependencies (
    contract_id TEXT NOT NULL,
    version TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('pack', 'policy', 'integration')),
    owner TEXT NOT NULL,
    PRIMARY KEY (contract_id, version, kind, owner)
) STRICT, WITHOUT ROWID;

CREATE TABLE contract_versions (
    contract_id TEXT NOT NULL,
    version TEXT NOT NULL,
    major INTEGER NOT NULL,
    minor INTEGER NOT NULL,
    patch INTEGER NOT NULL,
    schema TEXT NOT NULL CHECK (length(CAST(schema AS BLOB)) <= 32768),
    contract_digest TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_revision INTEGER NOT NULL UNIQUE,
    PRIMARY KEY (contract_id, version),
    UNIQUE (contract_id, major, minor, patch),
    FOREIGN KEY (created_revision, contract_id, version)
        REFERENCES contract_audit (revision, contract_id, version)
        DEFERRABLE INITIALLY DEFERRED
) STRICT, WITHOUT ROWID;

CREATE TABLE contract_audit (
    revision INTEGER PRIMARY KEY CHECK (revision BETWEEN 1 AND 512),
    contract_id TEXT NOT NULL,
    version TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'publish', 'deprecate', 'retire')),
    subject TEXT NOT NULL,
    key_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    intent_digest TEXT,
    ceremony TEXT UNIQUE,
    replacement_version TEXT,
    end_date TEXT,
    receipt TEXT NOT NULL,
    receipt_digest TEXT NOT NULL,
    CHECK ((operation = 'create') = (intent_digest IS NULL)),
    CHECK ((operation = 'create') = (ceremony IS NULL)),
    CHECK ((operation = 'deprecate') = (replacement_version IS NOT NULL)),
    CHECK ((operation = 'deprecate') = (end_date IS NOT NULL)),
    UNIQUE (revision, contract_id, version),
    UNIQUE (subject, key_digest),
    UNIQUE (contract_id, version, operation),
    FOREIGN KEY (contract_id, version) REFERENCES contract_versions (contract_id, version),
    FOREIGN KEY (contract_id, replacement_version) REFERENCES contract_versions (contract_id, version)
) STRICT;

CREATE INDEX contract_audit_version ON contract_audit (contract_id, version, revision);

CREATE TRIGGER contract_versions_immutable_update BEFORE UPDATE ON contract_versions
BEGIN
    SELECT RAISE(ABORT, 'immutable contract version');
END;

CREATE TRIGGER contract_versions_immutable_delete BEFORE DELETE ON contract_versions
BEGIN
    SELECT RAISE(ABORT, 'immutable contract version');
END;

CREATE TRIGGER contract_audit_immutable_update BEFORE UPDATE ON contract_audit
BEGIN
    SELECT RAISE(ABORT, 'immutable contract audit');
END;

CREATE TRIGGER contract_audit_immutable_delete BEFORE DELETE ON contract_audit
BEGIN
    SELECT RAISE(ABORT, 'immutable contract audit');
END;
