-- Writ production SQLite ledger, schema writ-ledger-4 additions (D-035), applied after
-- schema.sql to a new ledger, or by the explicit operator upgrade from writ-ledger-3
-- (SqliteLedger::upgrade_from_v3), which keeps every existing row. Only new tables:
-- no existing table changes, so the exact-schema verification of upgraded and new
-- ledgers compares the same object definitions.

-- Approver provenance per grant (D-035 item 5), from the trusted approval authority and
-- the broker's own decision; never agent-supplied, never a credential. A grant without a
-- row here (carried over from writ-ledger-3) is not executable.
CREATE TABLE grant_provenance (
  label               TEXT PRIMARY KEY REFERENCES grants (label),
  version             TEXT NOT NULL,          -- writ-provenance-1
  approver_id         TEXT NOT NULL,
  approver_issuer     TEXT NOT NULL,
  channel             TEXT NOT NULL,
  approved_at         TEXT NOT NULL,          -- RFC 3339 UTC, trusted time of the approval
  verdict             TEXT NOT NULL,
  auth_evidence_ref   TEXT NOT NULL,          -- the authority's evidence reference
  request_fingerprint TEXT NOT NULL,          -- D-012 fingerprint of the approved request
  policy_bundle_sha   TEXT NOT NULL
) STRICT;

-- The permit number issued at each receipt's dispatch (D-035 item 3): reconciliation looks
-- up the adapter's evidence for exactly this permit. Absent for receipts dispatched under
-- writ-ledger-3 (their uncertainty is retained).
CREATE TABLE receipt_permits (
  tenant_id       TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  permit_number   INTEGER NOT NULL UNIQUE CHECK (permit_number > 0),
  PRIMARY KEY (tenant_id, idempotency_key),
  FOREIGN KEY (tenant_id, idempotency_key) REFERENCES receipts (tenant_id, idempotency_key)
) STRICT;

-- Every receipt change made by reconciliation and the evidence it rested on.
CREATE TABLE reconciliations (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id       TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  permit_number   INTEGER,
  outcome         TEXT NOT NULL CHECK (outcome IN
                    ('observed_success', 'observed_failure', 'unknown_reconcile')),
  evidence        TEXT NOT NULL CHECK (evidence IN ('adapter_record', 'none')),
  FOREIGN KEY (tenant_id, idempotency_key) REFERENCES receipts (tenant_id, idempotency_key)
) STRICT;

-- The failure behind an audited refusal (D-035 item 4): failed transaction, stage,
-- sanitized category and code, durable progress, furthest possibly durable progress, and
-- whether an issued commit has an unknown outcome.
CREATE TABLE failure_records (
  audit_seq         INTEGER PRIMARY KEY REFERENCES audit_refusals (seq),
  transaction_name  TEXT NOT NULL CHECK (transaction_name IN
                      ('reserve', 'dispatch', 'adapter', 'observe')),
  stage             TEXT NOT NULL CHECK (stage IN ('begin', 'body', 'commit')),
  category          TEXT NOT NULL,
  code              TEXT NOT NULL,
  progress          TEXT NOT NULL CHECK (progress IN ('no_change', 'reserved',
                      'dispatched_without_effect', 'dispatched_with_effect', 'observed')),
  possible_progress TEXT NOT NULL CHECK (possible_progress IN ('no_change', 'reserved',
                      'dispatched_without_effect', 'dispatched_with_effect', 'observed')),
  outcome_unknown   INTEGER NOT NULL CHECK (outcome_unknown IN (0, 1))
) STRICT;

UPDATE meta SET value = 'writ-ledger-4' WHERE key = 'schema_version';
