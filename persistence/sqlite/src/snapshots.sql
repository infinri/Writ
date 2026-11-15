-- Writ immutable snapshot store, schema writ-snapshots-1 (M5a-06; D-045 M5-D5, M5-D17;
-- M5-INTERFACES J-4; INV-40). A separate database file owned by the service, bound at
-- creation to one ledger (meta 'ledger_id') and fenced by its own owner epoch
-- (meta 'owner_epoch', incremented at every open). Published content (blobs, snapshots,
-- snapshot_files) is never updated or deleted: the triggers below refuse it, and the
-- store's API has no such operation. Staging rows of unfinished ingest sessions are
-- temporary and removed at every open. Applied once to a new database inside one
-- transaction.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

-- File content by the store-computed digest of its bytes (`sha256:<hex>`).
CREATE TABLE blobs (
  digest TEXT PRIMARY KEY,
  size   INTEGER NOT NULL CHECK (size >= 0),
  bytes  BLOB NOT NULL,
  CHECK (length(bytes) = size)
) STRICT;

-- One published snapshot per (tenant, project, snapshot id); the id is derived from the
-- scope and the writ-snapshot-manifest/1 digest.
CREATE TABLE snapshots (
  tenant_id       TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  snapshot_id     TEXT NOT NULL,
  manifest_digest TEXT NOT NULL,
  file_count      INTEGER NOT NULL CHECK (file_count > 0),
  total_size      INTEGER NOT NULL CHECK (total_size >= 0),
  PRIMARY KEY (tenant_id, project_id, snapshot_id)
) STRICT;

CREATE TABLE snapshot_files (
  tenant_id   TEXT NOT NULL,
  project_id  TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  path        TEXT NOT NULL,
  blob_digest TEXT NOT NULL REFERENCES blobs (digest),
  size        INTEGER NOT NULL CHECK (size >= 0),
  PRIMARY KEY (tenant_id, project_id, snapshot_id, path),
  FOREIGN KEY (tenant_id, project_id, snapshot_id)
    REFERENCES snapshots (tenant_id, project_id, snapshot_id)
) STRICT;

-- Completed files of unfinished ingest sessions (temporary; never read as a snapshot).
CREATE TABLE ingest_staging (
  session_id TEXT NOT NULL,
  path       TEXT NOT NULL,
  digest     TEXT NOT NULL,
  size       INTEGER NOT NULL CHECK (size >= 0),
  bytes      BLOB NOT NULL,
  CHECK (length(bytes) = size),
  PRIMARY KEY (session_id, path)
) STRICT;

CREATE TRIGGER blobs_immutable_update BEFORE UPDATE ON blobs
BEGIN SELECT RAISE(ABORT, 'writ-snapshots-1: published content is immutable'); END;
CREATE TRIGGER blobs_immutable_delete BEFORE DELETE ON blobs
BEGIN SELECT RAISE(ABORT, 'writ-snapshots-1: published content is immutable'); END;
CREATE TRIGGER snapshots_immutable_update BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'writ-snapshots-1: published content is immutable'); END;
CREATE TRIGGER snapshots_immutable_delete BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'writ-snapshots-1: published content is immutable'); END;
CREATE TRIGGER snapshot_files_immutable_update BEFORE UPDATE ON snapshot_files
BEGIN SELECT RAISE(ABORT, 'writ-snapshots-1: published content is immutable'); END;
CREATE TRIGGER snapshot_files_immutable_delete BEFORE DELETE ON snapshot_files
BEGIN SELECT RAISE(ABORT, 'writ-snapshots-1: published content is immutable'); END;

INSERT INTO meta (key, value) VALUES ('schema_version', 'writ-snapshots-1');
INSERT INTO meta (key, value) VALUES ('owner_epoch', '0');
