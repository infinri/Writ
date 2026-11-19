CREATE TABLE pack_owner (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    registry_identity TEXT NOT NULL,
    evaluator_identity TEXT NOT NULL
) STRICT;

CREATE TABLE pack_versions (
    pack_id TEXT NOT NULL,
    pack_version TEXT NOT NULL,
    envelope TEXT NOT NULL,
    envelope_digest TEXT NOT NULL,
    installed_revision INTEGER NOT NULL UNIQUE,
    PRIMARY KEY (pack_id, pack_version),
    FOREIGN KEY (installed_revision, pack_id, pack_version)
        REFERENCES pack_audit (revision, pack_id, pack_version) DEFERRABLE INITIALLY DEFERRED
) STRICT, WITHOUT ROWID;

CREATE TABLE pack_selection (
    pack_id TEXT PRIMARY KEY,
    pack_version TEXT NOT NULL,
    FOREIGN KEY (pack_id, pack_version) REFERENCES pack_versions (pack_id, pack_version)
) STRICT, WITHOUT ROWID;

CREATE TABLE pack_audit (
    revision INTEGER PRIMARY KEY CHECK (revision BETWEEN 1 AND 512),
    pack_id TEXT NOT NULL,
    pack_version TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('install', 'enable', 'disable', 'upgrade')),
    subject TEXT NOT NULL,
    key_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    intent_digest TEXT NOT NULL,
    management_dispatch TEXT UNIQUE,
    ceremony TEXT NOT NULL UNIQUE,
    receipt TEXT NOT NULL,
    receipt_digest TEXT NOT NULL,
    UNIQUE (subject, key_digest),
    UNIQUE (revision, pack_id, pack_version),
    FOREIGN KEY (pack_id, pack_version) REFERENCES pack_versions (pack_id, pack_version)
) STRICT;

CREATE TRIGGER pack_versions_no_update BEFORE UPDATE ON pack_versions
BEGIN SELECT RAISE(ABORT, 'immutable pack version'); END;
CREATE TRIGGER pack_versions_no_delete BEFORE DELETE ON pack_versions
BEGIN SELECT RAISE(ABORT, 'immutable pack version'); END;
CREATE TRIGGER pack_audit_no_update BEFORE UPDATE ON pack_audit
BEGIN SELECT RAISE(ABORT, 'immutable pack audit'); END;
CREATE TRIGGER pack_audit_no_delete BEFORE DELETE ON pack_audit
BEGIN SELECT RAISE(ABORT, 'immutable pack audit'); END;

CREATE TRIGGER pack_owner_no_update BEFORE UPDATE ON pack_owner
BEGIN SELECT RAISE(ABORT, 'immutable evaluator source'); END;
CREATE TRIGGER pack_owner_no_delete BEFORE DELETE ON pack_owner
BEGIN SELECT RAISE(ABORT, 'immutable evaluator source'); END;
