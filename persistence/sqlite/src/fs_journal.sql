-- writ-fseffects-1: the per-root journal of the fs.write adapter (M5a-09; D-045 M5-D8;
-- M5-INTERFACES J-7.4, J-11.9). One file per configured root, bound to one ledger and one
-- root identity; WAL with synchronous=FULL; owner-epoch fencing.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

-- One row per consumed permit. `state` moves intent -> (abandoned | staged) -> resolved, or
-- intent -> resolved; `resolved` is terminal (N2). Identities are opaque canonical text
-- written by the adapter ({fs_uuid, ino, btime, size, digest}) or 'absent'.
CREATE TABLE ops (
  op_id               TEXT PRIMARY KEY,
  permit_id           TEXT NOT NULL UNIQUE,
  tenant_id           TEXT NOT NULL,
  idempotency_key     TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  root_id             TEXT NOT NULL,
  target              TEXT NOT NULL,
  mode                TEXT NOT NULL CHECK (mode IN ('create_only', 'replace_if')),
  expected_prior      TEXT NOT NULL,
  content_digest      TEXT NOT NULL,
  snapshot_id         TEXT NOT NULL,
  staging_name        TEXT NOT NULL UNIQUE,
  state               TEXT NOT NULL CHECK (state IN ('intent', 'abandoned', 'staged', 'resolved')),
  staged_identity     TEXT,
  owner_epoch         INTEGER NOT NULL,
  created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

-- At most one unresolved op per target.
CREATE UNIQUE INDEX ops_one_unresolved_per_target ON ops (target) WHERE state <> 'resolved';

-- The recorded outcome of a resolved op; never updated or deleted.
CREATE TABLE outcomes (
  op_id                 TEXT PRIMARY KEY REFERENCES ops (op_id),
  operation_evidence    TEXT NOT NULL CHECK (operation_evidence IN ('completed', 'not_completed', 'unknown')),
  desired_state         TEXT NOT NULL CHECK (desired_state IN ('verified', 'differs', 'absent', 'unknown')),
  result                TEXT NOT NULL CHECK (result IN ('applied', 'failed', 'unknown')),
  reason                TEXT NOT NULL,
  target_identity_after TEXT,
  displaced_identity    TEXT,
  recorded_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  boot_id               TEXT NOT NULL
) STRICT;

CREATE TRIGGER outcomes_no_update BEFORE UPDATE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes are immutable'); END;
CREATE TRIGGER outcomes_no_delete BEFORE DELETE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes are immutable'); END;

-- Per-target state: the op blocking it, taint, and the last identity (with digest) the
-- adapter established.
CREATE TABLE targets (
  target        TEXT PRIMARY KEY,
  blocked_by    TEXT,
  tainted       INTEGER NOT NULL CHECK (tainted IN (0, 1)),
  taint_reason  TEXT,
  last_identity TEXT
) STRICT;

-- Unrecoverable marks (N3, F12): one row per marked boot, kept as history.
CREATE TABLE root_state (
  seq                   INTEGER PRIMARY KEY AUTOINCREMENT,
  unrecoverable_boot_id TEXT NOT NULL,
  reason                TEXT NOT NULL,
  recorded_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

-- Applied fs_target_release dispositions (J-11.9 step 2): unique disposition id, idempotent.
CREATE TABLE target_dispositions (
  disposition_id     TEXT PRIMARY KEY,
  target             TEXT NOT NULL,
  state_digest       TEXT NOT NULL,
  prev_blocked_by    TEXT,
  prev_tainted       INTEGER NOT NULL CHECK (prev_tainted IN (0, 1)),
  prev_taint_reason  TEXT,
  prev_last_identity TEXT,
  new_last_identity  TEXT NOT NULL,
  applied_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

INSERT INTO meta (key, value) VALUES ('schema_version', 'writ-fseffects-1');
INSERT INTO meta (key, value) VALUES ('owner_epoch', '0');
INSERT INTO meta (key, value) VALUES ('change_counter', '0');
