-- Writ production SQLite ledger, schema writ-ledger-5 additions (M3-05 transition service,
-- D-036), applied after schema_v4.sql to a new ledger, or by the explicit operator upgrade
-- (SqliteLedger::upgrade), which keeps every existing row. Only new tables.

-- Workflows: the transition service is the only writer of state and revision (INV-32).
CREATE TABLE workflows (
  tenant_id            TEXT NOT NULL,
  project_id           TEXT NOT NULL,
  workflow_id          TEXT NOT NULL,
  task_id              TEXT NOT NULL,
  state                TEXT NOT NULL,
  revision             INTEGER NOT NULL CHECK (revision >= 0),
  artifact_snapshot_id TEXT NOT NULL,
  artifact_digest      TEXT NOT NULL,
  -- The latest protected promotion (all NULL before the first one).
  last_receipt_id      TEXT,
  last_record_id       TEXT,
  last_snapshot_id     TEXT,
  last_artifact_digest TEXT,
  last_acceptance_sha  TEXT,
  last_authorization_sha TEXT,
  PRIMARY KEY (tenant_id, project_id, workflow_id)
) STRICT;

-- The active acceptance policy of each project: trusted requirement set (canonical JSON of
-- the kernel contract) and effect prerequisites.
CREATE TABLE acceptance_policies (
  tenant_id    TEXT NOT NULL,
  project_id   TEXT NOT NULL,
  requirements TEXT NOT NULL,
  bundle_sha   TEXT NOT NULL,
  PRIMARY KEY (tenant_id, project_id)
) STRICT;

CREATE TABLE effect_prerequisites (
  tenant_id   TEXT NOT NULL,
  project_id  TEXT NOT NULL,
  effect_kind TEXT NOT NULL,
  state       TEXT NOT NULL,
  PRIMARY KEY (tenant_id, project_id, effect_kind, state)
) STRICT;

-- Evaluator registry (current entry per evaluator and project scope).
CREATE TABLE evaluator_registry (
  tenant_id         TEXT NOT NULL,
  project_id        TEXT NOT NULL,
  evaluator_id      TEXT NOT NULL,
  registry_revision INTEGER NOT NULL CHECK (registry_revision >= 0),
  entry             TEXT NOT NULL,          -- canonical JSON, evaluator-registry-entry/1
  status            TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
  PRIMARY KEY (tenant_id, project_id, evaluator_id)
) STRICT;

-- Promotion receipts per (tenant, idempotency key): an identical retry replays.
CREATE TABLE promotion_receipts (
  tenant_id       TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  fingerprint     TEXT NOT NULL,
  receipt_id      TEXT NOT NULL UNIQUE,
  project_id      TEXT NOT NULL,
  workflow_id     TEXT NOT NULL,
  revision        INTEGER NOT NULL,
  PRIMARY KEY (tenant_id, idempotency_key)
) STRICT;

-- Single use, ledger-wide (D-034): acceptance records and runner attestations.
CREATE TABLE consumed_records (
  acceptance_record_id TEXT PRIMARY KEY,
  tenant_id            TEXT NOT NULL,
  idempotency_key      TEXT NOT NULL
) STRICT;

CREATE TABLE consumed_attestations (
  attestation_id       TEXT PRIMARY KEY,
  acceptance_record_id TEXT NOT NULL REFERENCES consumed_records (acceptance_record_id),
  tenant_id            TEXT NOT NULL,
  idempotency_key      TEXT NOT NULL
) STRICT;

-- Workflow history (creation, artifact updates, promotions).
CREATE TABLE workflow_events (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id   TEXT NOT NULL,
  project_id  TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  cause       TEXT NOT NULL CHECK (cause IN ('create', 'artifact', 'promote')),
  from_state  TEXT,
  to_state    TEXT NOT NULL,
  revision    INTEGER NOT NULL,
  receipt_id  TEXT,
  record_id   TEXT
) STRICT;

-- Administrative operations with operator provenance (D-036 item 5).
CREATE TABLE admin_events (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  operation       TEXT NOT NULL CHECK (operation IN ('create_workflow', 'update_artifact',
                    'activate_acceptance_policy', 'register_evaluator', 'revoke_evaluator')),
  tenant_id       TEXT NOT NULL,
  project_id      TEXT NOT NULL,
  target          TEXT NOT NULL,
  operator_id     TEXT NOT NULL,
  operator_issuer TEXT NOT NULL,
  channel         TEXT NOT NULL,
  evidence_ref    TEXT NOT NULL,
  at              TEXT NOT NULL
) STRICT;

UPDATE meta SET value = 'writ-ledger-5' WHERE key = 'schema_version';
