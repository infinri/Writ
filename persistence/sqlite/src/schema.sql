-- Writ production SQLite ledger, schema writ-ledger-2 (M3-01, M3-02 trusted time).
-- Tables as in the M1 ledger (m1/fixtures/broker/ledger.sql, D-023), plus the single-owner
-- fencing epoch (M3-START-PLAN "Coordination boundary") and, since writ-ledger-2, the
-- trusted-time high-water mark and clock audit records (M3-TRUSTED-TIME revision 2).
-- Applied once to a new database inside one transaction.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

-- Approval grants: bindings fixed at approval time (D-009, BROKER.md approve).
CREATE TABLE grants (
  label          TEXT PRIMARY KEY,
  binding        TEXT NOT NULL,              -- D-006 canonical JSON of the grant binding
  expires_s      INTEGER NOT NULL,           -- seconds since 1970-01-01T00:00:00Z
  expires_ns     INTEGER NOT NULL CHECK (expires_ns BETWEEN 0 AND 999999999),
  max_uses       INTEGER NOT NULL CHECK (max_uses >= 0),
  uses           INTEGER NOT NULL CHECK (uses >= 0 AND uses <= max_uses)
) STRICT;

-- Effect intent and status per (tenant, idempotency key) (D-012).
CREATE TABLE receipts (
  tenant_id       TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  fingerprint     TEXT NOT NULL,             -- D-006 digest of the full request
  grant_label     TEXT,                      -- effective grant; NULL for an allow decision
  status          TEXT NOT NULL CHECK (status IN
                    ('reserved', 'dispatched', 'unknown_reconcile', 'observed_success', 'observed_failure')),
  PRIMARY KEY (tenant_id, idempotency_key)
) STRICT;

-- Append-only history of every receipt status change, including reconciliation.
CREATE TABLE receipt_events (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id       TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  status          TEXT NOT NULL,
  cause           TEXT NOT NULL CHECK (cause IN ('reserve', 'dispatch', 'observe', 'reconcile')),
  FOREIGN KEY (tenant_id, idempotency_key) REFERENCES receipts (tenant_id, idempotency_key)
) STRICT;

-- Approval-use history: one row per consumed grant use.
CREATE TABLE grant_uses (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  label           TEXT NOT NULL REFERENCES grants (label),
  tenant_id       TEXT NOT NULL,
  idempotency_key TEXT NOT NULL
) STRICT;

-- Every refusal is audited (BROKER.md "Results of refusals").
CREATE TABLE audit_refusals (
  seq  INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL
) STRICT;

-- Named counters: 'next_permit' (last issued permit number).
CREATE TABLE counters (
  name  TEXT PRIMARY KEY,
  value INTEGER NOT NULL
) STRICT;

-- SIMULATED effect adapter state (an external system in production): consumed
-- permit ids and the count of applied effects. Kept in the same file so a broker
-- restart cannot forget them; this is not a real external exactly-once guarantee.
CREATE TABLE adapter_consumed_permits (
  permit_id TEXT PRIMARY KEY
) STRICT;

CREATE TABLE adapter_effects (
  seq       INTEGER PRIMARY KEY AUTOINCREMENT,
  permit_id TEXT NOT NULL REFERENCES adapter_consumed_permits (permit_id)
) STRICT;

-- Trusted-time high-water mark H (M3-TRUSTED-TIME rule 4): at most one row, absent until
-- the first observation; only ever raised (fenced `time_floor` transaction).
CREATE TABLE time_floor (
  id       INTEGER PRIMARY KEY CHECK (id = 1),
  floor_s  INTEGER NOT NULL,                 -- seconds since 1970-01-01T00:00:00Z
  floor_ns INTEGER NOT NULL CHECK (floor_ns BETWEEN 0 AND 999999999)
) STRICT;

-- Clock audit records: startup outcome, drift diagnostics, operator acknowledgements.
-- Timestamps in the kernel's RFC 3339 UTC form.
CREATE TABLE clock_events (
  seq       INTEGER PRIMARY KEY AUTOINCREMENT,
  kind      TEXT NOT NULL CHECK (kind IN
              ('startup', 'startup_rollback_absorbed', 'startup_rollback_blocked',
               'rollback_detected', 'forward_jump_detected', 'drift_normal', 'acknowledged',
               'acknowledgement_unauthenticated', 'acknowledgement_unauthorized',
               'acknowledgement_stale')),
  effective TEXT,
  wall      TEXT,
  floor     TEXT,
  operator  TEXT
) STRICT;

INSERT INTO meta (key, value) VALUES ('schema_version', 'writ-ledger-2');
INSERT INTO counters (name, value) VALUES ('next_permit', 0);
INSERT INTO meta (key, value) VALUES ('owner_epoch', '0');
