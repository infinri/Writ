-- Writ mechanical runner store, schema writ-runner-1 (M3-07; INV-35; D-035 items 2 and 3).
-- The runner adapter's own store, separate from the broker ledger and from the controlled
-- effect store: the broker reaches it only through the runner adapter
-- (writ_broker::runner::RunnerAdapter); the transition service reads attestations from it
-- by id. Bound at creation to one ledger (meta 'ledger_id'). Applied once to a new
-- database inside one transaction.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

-- Every permit id the runner accepted (T-start, state 'started'), and its outcome (T-finish):
-- 'applied' with exactly one attestation, or 'failed' with an operator diagnostic. A
-- 'started' run without an outcome is never run again and is no completion evidence.
CREATE TABLE runs (
  permit_id           TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  idempotency_key     TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  state               TEXT NOT NULL CHECK (state IN ('started', 'applied', 'failed')),
  diagnostic          TEXT,
  CHECK ((state = 'failed') = (diagnostic IS NOT NULL))
) STRICT;

-- Authenticated, verified runner attestations (canonical JSON), at most one per run.
CREATE TABLE attestations (
  attestation_id TEXT PRIMARY KEY,
  permit_id      TEXT NOT NULL UNIQUE REFERENCES runs (permit_id),
  tenant_id      TEXT NOT NULL,
  project_id     TEXT NOT NULL,
  document       TEXT NOT NULL
) STRICT;

INSERT INTO meta (key, value) VALUES ('schema_version', 'writ-runner-1');
