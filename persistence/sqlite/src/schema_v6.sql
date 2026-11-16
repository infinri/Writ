-- Writ production SQLite ledger, schema writ-ledger-6 additions (M4: authenticated approval,
-- revocation, signoff and enrollment; D-040, M4-INTERFACES I-7 and I-8), applied after
-- schema_v5.sql to a new ledger, or by the explicit operator upgrade (SqliteLedger::upgrade)
-- from writ-ledger-3, -4 or -5, which keeps every existing row. New tables, plus one rebuilt
-- table (failure_records, at the end: its stage CHECK gains 'effect').
-- Timestamps are the kernel's RFC 3339 UTC text. No credential secret is ever stored: only
-- public passkey material, credential ids and references to consumed challenges.

-- I-8: one ledger-wide monotonic sequence. Every promotion, administrative operation,
-- revocation, approval decision, signoff, enrollment, role change ('administration') and
-- expiry step that changes anything commits exactly one row inside its own transaction, so
-- tests read the actual commit order (D-042 X6). A read-session login (D-044 Z1) commits
-- its own row ('read_session') with its consumed challenge and approval event; the read
-- session itself is that consumed challenge and event, never a separate grant of authority.
CREATE TABLE ledger_sequence (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL CHECK (kind IN ('promotion', 'administration', 'revocation',
               'decision', 'signoff', 'enrollment', 'bootstrap', 'expiry', 'registration',
               'read_session', 'key_revocation')),
  reference  TEXT NOT NULL
) STRICT;

-- Subjects (I-1, R7): stable issuer plus subject, principal kind, revocation time.
CREATE TABLE subjects (
  issuer      TEXT NOT NULL CHECK (issuer <> ''),
  subject     TEXT NOT NULL CHECK (subject <> ''),
  kind        TEXT NOT NULL CHECK (kind IN ('human', 'service')),
  created_at  TEXT NOT NULL,
  revoked_at  TEXT,
  PRIMARY KEY (issuer, subject)
) STRICT;

-- Passkey credentials: library-serialized public material only (D-041).
CREATE TABLE credentials (
  credential_id      TEXT PRIMARY KEY CHECK (credential_id <> ''),
  issuer             TEXT NOT NULL,
  subject            TEXT NOT NULL,
  public_material    BLOB NOT NULL,
  status             TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
  enrolled_at        TEXT NOT NULL,
  enrolled_by_issuer TEXT NOT NULL,
  enrolled_by        TEXT NOT NULL,
  FOREIGN KEY (issuer, subject) REFERENCES subjects (issuer, subject)
) STRICT;

-- Enrollment records (R1), written only by the documented ceremony
-- (docs/implementation/M4-ENROLLMENT-CEREMONY.md); NULL challenge only for the bootstrap.
CREATE TABLE enrollments (
  issuer             TEXT NOT NULL,
  subject            TEXT NOT NULL,
  enrolled_at        TEXT NOT NULL,
  enrolled_by_issuer TEXT NOT NULL,
  enrolled_by        TEXT NOT NULL,
  challenge_id       TEXT UNIQUE,
  PRIMARY KEY (issuer, subject),
  FOREIGN KEY (issuer, subject) REFERENCES subjects (issuer, subject)
) STRICT;

-- The explicit requester identities an enrolled subject controls (R1).
CREATE TABLE enrollment_links (
  issuer         TEXT NOT NULL,
  subject        TEXT NOT NULL,
  linked_issuer  TEXT NOT NULL CHECK (linked_issuer <> ''),
  linked_subject TEXT NOT NULL CHECK (linked_subject <> ''),
  PRIMARY KEY (issuer, subject, linked_issuer, linked_subject),
  FOREIGN KEY (issuer, subject) REFERENCES enrollments (issuer, subject)
) STRICT;

-- Server-held WebAuthn registration ceremonies (S5, X8): bound to the subject, RP id, origin,
-- operation and expiry; single use (issued -> registered -> enrolled, or issued -> refused).
-- The verified credential is recorded at `registered`; an enrollment copies it from here.
-- D-043 Y2: an `enroll` row is created only by a consumed `registration_start` ceremony of the
-- operator (`started_by`) and records the operator and the approval listener's page session; its
-- finish and the consuming Enroll ceremony must come from both. `bootstrap` has none.
CREATE TABLE registrations (
  registration_id TEXT PRIMARY KEY CHECK (registration_id <> ''),
  operation       TEXT NOT NULL CHECK (operation IN ('bootstrap', 'enroll')),
  issuer          TEXT NOT NULL CHECK (issuer <> ''),
  subject         TEXT NOT NULL CHECK (subject <> ''),
  rp_id           TEXT NOT NULL CHECK (rp_id <> ''),
  origin          TEXT NOT NULL CHECK (origin <> ''),
  ceremony_state  BLOB NOT NULL,
  issued_at       TEXT NOT NULL,
  expires_at      TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('issued', 'registered', 'enrolled', 'refused')),
  credential_id   TEXT UNIQUE,
  public_material BLOB,
  algorithm       TEXT,
  consumed_by     TEXT,                       -- enrolling challenge id, or 'bootstrap'
  operator_issuer  TEXT CHECK (operator_issuer <> ''),
  operator_subject TEXT CHECK (operator_subject <> ''),
  operator_session TEXT CHECK (operator_session <> ''),
  started_by       TEXT UNIQUE,                -- the consumed registration_start challenge id
  client_options   BLOB,                       -- creation options (replay of the start)
  CHECK ((operation = 'enroll') = (started_by IS NOT NULL)),
  CHECK ((operation = 'enroll') = (operator_session IS NOT NULL)),
  CHECK ((operator_session IS NULL) = (operator_issuer IS NULL)),
  CHECK ((operator_session IS NULL) = (operator_subject IS NULL)),
  CHECK ((status IN ('issued', 'refused')) = (credential_id IS NULL)),
  CHECK ((credential_id IS NULL) = (public_material IS NULL)),
  CHECK (algorithm IS NULL OR algorithm = 'ES256')
) STRICT;

-- Permissions per (tenant, project) scope (O-7); services hold only operator (O-8).
CREATE TABLE role_assignments (
  assignment_id TEXT PRIMARY KEY CHECK (assignment_id <> ''),
  issuer        TEXT NOT NULL,
  subject       TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('human', 'service')),
  permission    TEXT NOT NULL CHECK (permission IN
                  ('approver', 'signoff_authority', 'operator', 'revoker')),
  tenant_id     TEXT NOT NULL CHECK (tenant_id <> ''),
  project_id    TEXT NOT NULL CHECK (project_id <> ''),
  granted_at    TEXT NOT NULL,
  revoked_at    TEXT,
  CHECK (kind = 'human' OR permission = 'operator'),
  FOREIGN KEY (issuer, subject) REFERENCES subjects (issuer, subject)
) STRICT;

-- Stored approval requests (I-3): immutable context (D-006 canonical JSON of the displayed
-- context) and its fingerprint; only the status changes, pending to a terminal status.
CREATE TABLE approval_requests (
  request_id          TEXT PRIMARY KEY CHECK (request_id <> ''),
  tenant_id           TEXT NOT NULL,
  project_id          TEXT NOT NULL,
  context             TEXT NOT NULL,
  context_fingerprint TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  expires_at          TEXT NOT NULL,
  status              TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'expired'))
) STRICT;

-- Server-side challenges (I-2, R2, C1): ids and digests unique ledger-wide, single use. A
-- terminal refusal of an identified ceremony burns its challenge: status 'refused' with the
-- refusal code, in the refusing transaction (D-042 X4).
CREATE TABLE challenges (
  challenge_id        TEXT PRIMARY KEY CHECK (challenge_id <> ''),
  challenge_digest    TEXT NOT NULL UNIQUE,
  ceremony_state      BLOB NOT NULL,
  issuer              TEXT NOT NULL,
  subject             TEXT NOT NULL,
  allowed_credentials TEXT NOT NULL,          -- canonical JSON array of credential ids
  operation           TEXT NOT NULL CHECK (operation IN
                        ('approve', 'deny', 'revoke', 'signoff', 'enroll', 'role_change',
                         'registration_start', 'read_session')),
  verdict             TEXT NOT NULL,
  tenant_id           TEXT NOT NULL,
  project_id          TEXT NOT NULL,
  target_id           TEXT NOT NULL,
  context_fingerprint TEXT NOT NULL,
  policy_bundle_sha   TEXT NOT NULL,
  revision            INTEGER NOT NULL,
  issued_at           TEXT NOT NULL,
  expires_at          TEXT NOT NULL,
  idempotency_key     TEXT NOT NULL,
  rp_id               TEXT NOT NULL CHECK (rp_id <> ''),  -- configured channel at issue (M1)
  origin              TEXT NOT NULL CHECK (origin <> ''),
  status              TEXT NOT NULL CHECK (status IN ('issued', 'consumed', 'expired',
                        'refused')),
  refusal_code        TEXT,
  -- D-043 Y1: a signoff challenge stores the effective signoff expiry the ledger computed at
  -- begin (displayed and fingerprinted); finish takes exactly this value. Other operations
  -- store none. A signoff challenge without it is refused at finish (fail closed).
  signoff_expires_at  TEXT CHECK (operation = 'signoff' OR signoff_expires_at IS NULL),
  CHECK ((status = 'refused') = (refusal_code IS NOT NULL))
) STRICT;

-- Human decisions on approval requests (I-3; several rows possible, no quorum implied).
CREATE TABLE approval_decisions (
  seq                INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id         TEXT NOT NULL REFERENCES approval_requests (request_id),
  verdict            TEXT NOT NULL CHECK (verdict IN ('approved', 'denied')),
  issuer             TEXT NOT NULL,
  subject            TEXT NOT NULL,
  credential_id      TEXT NOT NULL,
  channel            TEXT NOT NULL,
  decided_at         TEXT NOT NULL,
  challenge_id       TEXT NOT NULL UNIQUE REFERENCES challenges (challenge_id),
  provenance_version TEXT NOT NULL,
  grant_label        TEXT REFERENCES grants (label)
) STRICT;

-- The single grant an approved request produced (I-3; exactly one per request). Written only
-- inside the ledger's own approval ceremony; on writ-ledger-6 a grant is executable only with
-- this link to its approved request, decision and consumed challenge (D-042 X1).
CREATE TABLE approval_grants (
  label        TEXT PRIMARY KEY REFERENCES grants (label),
  request_id   TEXT NOT NULL UNIQUE REFERENCES approval_requests (request_id),
  challenge_id TEXT NOT NULL UNIQUE REFERENCES challenges (challenge_id)
) STRICT;

-- O-14: grants that existed before writ-ledger-6 are non-executable. The marker is the only
-- change: the grant row, its provenance, uses, reservations and receipts are kept.
CREATE TABLE legacy_grants (
  label         TEXT PRIMARY KEY REFERENCES grants (label),
  upgraded_from TEXT NOT NULL CHECK (upgraded_from IN
                  ('writ-ledger-3', 'writ-ledger-4', 'writ-ledger-5'))
) STRICT;

-- Committed revocations (I-4). `use_state` records where the target's use stood when the
-- revocation committed (serialized with T-reserve, T-dispatch and promotion, O-5).
CREATE TABLE revocations (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  target_kind  TEXT NOT NULL CHECK (target_kind IN
                 ('grant', 'signoff', 'credential', 'subject', 'role')),
  target_id    TEXT NOT NULL,
  by_issuer    TEXT NOT NULL,
  by_subject   TEXT NOT NULL,
  at           TEXT NOT NULL,
  reason       TEXT NOT NULL,
  challenge_id TEXT NOT NULL UNIQUE REFERENCES challenges (challenge_id),
  use_state    TEXT NOT NULL CHECK (use_state IN
                 ('unused', 'reserved', 'dispatched', 'consumed', 'not_applicable')),
  ledger_seq   INTEGER NOT NULL UNIQUE REFERENCES ledger_sequence (seq),
  UNIQUE (target_kind, target_id)
) STRICT;

-- Human signoffs (I-5) with provenance v2.
CREATE TABLE signoffs (
  signoff_id                   TEXT PRIMARY KEY CHECK (signoff_id <> ''),
  tenant_id                    TEXT NOT NULL,
  project_id                   TEXT NOT NULL,
  task_id                      TEXT NOT NULL,
  workflow_id                  TEXT NOT NULL,
  from_state                   TEXT NOT NULL,
  to_state                     TEXT NOT NULL,
  acceptance_record_id         TEXT NOT NULL,
  criterion_id                 TEXT NOT NULL,
  artifact_snapshot_id         TEXT NOT NULL,
  artifact_digest              TEXT NOT NULL,
  acceptance_policy_id         TEXT NOT NULL,
  acceptance_policy_version    TEXT NOT NULL,
  acceptance_policy_bundle_sha TEXT NOT NULL,
  state_revision               INTEGER NOT NULL,
  issued_at                    TEXT NOT NULL,
  expires_at                   TEXT NOT NULL,
  issuer                       TEXT NOT NULL,
  subject                      TEXT NOT NULL,
  credential_id                TEXT NOT NULL,
  challenge_id                 TEXT NOT NULL UNIQUE REFERENCES challenges (challenge_id),
  provenance_version           TEXT NOT NULL,
  -- X7: the exclusions read from the ledger at signing (canonical JSON), displayed and
  -- fingerprinted with the binding.
  exclusions                   TEXT NOT NULL
) STRICT;

-- X7: stored promotion contexts. Who asked for the promotion a signoff is for: one row per
-- signoff id and sealed request fingerprint (Broker::request_signoff). Every row of a
-- signoff id excludes its requester and agent from signing and using it. Authorizes nothing.
CREATE TABLE signoff_requests (
  signoff_id           TEXT NOT NULL CHECK (signoff_id <> ''),
  request_fingerprint  TEXT NOT NULL,
  tenant_id            TEXT NOT NULL,
  project_id           TEXT NOT NULL,
  workflow_id          TEXT NOT NULL,
  acceptance_record_id TEXT NOT NULL,
  criterion_id         TEXT NOT NULL,
  from_state           TEXT NOT NULL,
  to_state             TEXT NOT NULL,
  state_revision       INTEGER NOT NULL,
  requester_issuer     TEXT NOT NULL CHECK (requester_issuer <> ''),
  requester_subject    TEXT NOT NULL CHECK (requester_subject <> ''),
  agent_issuer         TEXT,
  agent_subject        TEXT,
  created_at           TEXT NOT NULL,
  -- D-043 Y1: the trusted deadline of this promotion context: the earliest of the verified
  -- record's expiry, the human criterion's freshness and the sealed request's deadline_at.
  -- A row without it bounds nothing and makes signoff begin refuse (fail closed).
  deadline_at          TEXT,
  CHECK ((agent_issuer IS NULL) = (agent_subject IS NULL)),
  PRIMARY KEY (signoff_id, request_fingerprint)
) STRICT;

-- Single consumption ledger-wide (I-5), atomic with the promotion that consumes it.
CREATE TABLE consumed_signoffs (
  signoff_id      TEXT PRIMARY KEY REFERENCES signoffs (signoff_id),
  tenant_id       TEXT NOT NULL,
  idempotency_key TEXT NOT NULL
) STRICT;

-- Approval-channel events with provenance v2 (M4-09): never credential material.
CREATE TABLE approval_events (
  seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
  kind                TEXT NOT NULL CHECK (kind IN ('request_created', 'approved', 'denied',
                        'expired', 'revoked', 'signed_off', 'enrolled', 'bootstrap',
                        'role_changed', 'registration_started', 'read_session')),
  target_id           TEXT NOT NULL,
  tenant_id           TEXT NOT NULL,
  project_id          TEXT NOT NULL,
  actor_issuer        TEXT,
  actor_subject       TEXT,
  credential_id       TEXT,
  channel             TEXT,
  at                  TEXT NOT NULL,
  challenge_id        TEXT,
  request_fingerprint TEXT,
  policy_bundle_sha   TEXT,
  provenance_version  TEXT NOT NULL,
  ledger_seq          INTEGER REFERENCES ledger_sequence (seq)
) STRICT;

-- M5 J-3 and J-11.8 (D-045 M5-D6, D-046 Q-3): the key registry and key revocations.
-- `key_registry` records every key id ever loaded with its purpose and the digest of its
-- material (`sha256:` over a domain-separation prefix, never the material). Ids are never
-- reused: an id presented again with other material or purpose, a material digest already
-- registered under another id or purpose, and an id or digest that was ever revoked are
-- refused at load. Both tables are append-only (triggers refuse UPDATE and DELETE).
CREATE TABLE key_registry (
  key_id          TEXT PRIMARY KEY CHECK (key_id <> '' AND length(key_id) <= 64),
  purpose         TEXT NOT NULL CHECK (purpose IN
                    ('ingress', 'permit', 'acceptance-record', 'runner-attestation')),
  material_digest TEXT NOT NULL UNIQUE CHECK (length(material_digest) = 71
                    AND substr(material_digest, 1, 7) = 'sha256:'),
  CHECK (substr(key_id, 1, length(purpose) + 1) = purpose || '/')
) STRICT;

-- One row per revocation (J-11.8): explicit scope only (one key id, or one material digest,
-- of one purpose). For scope `key_id` the digest is the registered one, never a request
-- value; for scope `material` the key id is the registered id of that digest, or NULL for a
-- pre-emptive revocation of an unregistered digest. `actor` is the canonical JSON of the
-- authenticated OS peer ({principal_kind, os_principal, verified_by, endpoint, pid}); the
-- reason is bounded untrusted text. Each revocation commits one `ledger_sequence` row
-- (kind `key_revocation`) in the same BEGIN IMMEDIATE transaction; a repeated identical
-- revocation (same scope, same digest) returns the existing row.
CREATE TABLE key_revocations (
  revocation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  scope           TEXT NOT NULL CHECK (scope IN ('key_id', 'material')),
  key_id          TEXT CHECK (key_id IS NULL OR (key_id <> '' AND length(key_id) <= 64)),
  material_digest TEXT NOT NULL CHECK (length(material_digest) = 71
                    AND substr(material_digest, 1, 7) = 'sha256:'),
  purpose         TEXT NOT NULL CHECK (purpose IN
                    ('ingress', 'permit', 'acceptance-record', 'runner-attestation')),
  revoked_at      TEXT NOT NULL,
  actor           TEXT NOT NULL CHECK (actor <> ''),
  reason          TEXT NOT NULL CHECK (reason <> '' AND length(CAST(reason AS BLOB)) <= 512),
  ledger_seq      INTEGER NOT NULL UNIQUE REFERENCES ledger_sequence (seq),
  CHECK (scope = 'material' OR key_id IS NOT NULL),
  UNIQUE (scope, material_digest)
) STRICT;

CREATE INDEX key_revocations_by_id ON key_revocations (key_id);

CREATE TRIGGER key_registry_no_update BEFORE UPDATE ON key_registry
BEGIN SELECT RAISE(ABORT, 'key_registry is append-only'); END;
CREATE TRIGGER key_registry_no_delete BEFORE DELETE ON key_registry
BEGIN SELECT RAISE(ABORT, 'key_registry is append-only'); END;
CREATE TRIGGER key_revocations_no_update BEFORE UPDATE ON key_revocations
BEGIN SELECT RAISE(ABORT, 'key_revocations is append-only'); END;
CREATE TRIGGER key_revocations_no_delete BEFORE DELETE ON key_revocations
BEGIN SELECT RAISE(ABORT, 'key_revocations is append-only'); END;

-- M5a-09 / M5a-11 (lead schema change, writ-ledger-6 unreleased, amended in place): an
-- effect-stage failure (a non-transactional effect failed after its durable intent, outcome
-- unknown) is stored with stage 'effect' instead of 'body'. SQLite cannot alter a CHECK, so
-- the table is rebuilt with every row kept; the definition is otherwise the writ-ledger-4 one.
CREATE TABLE failure_records_v6 (
  audit_seq         INTEGER PRIMARY KEY REFERENCES audit_refusals (seq),
  transaction_name  TEXT NOT NULL CHECK (transaction_name IN
                      ('reserve', 'dispatch', 'adapter', 'observe')),
  stage             TEXT NOT NULL CHECK (stage IN ('begin', 'body', 'commit', 'effect')),
  category          TEXT NOT NULL,
  code              TEXT NOT NULL,
  progress          TEXT NOT NULL CHECK (progress IN ('no_change', 'reserved',
                      'dispatched_without_effect', 'dispatched_with_effect', 'observed')),
  possible_progress TEXT NOT NULL CHECK (possible_progress IN ('no_change', 'reserved',
                      'dispatched_without_effect', 'dispatched_with_effect', 'observed')),
  outcome_unknown   INTEGER NOT NULL CHECK (outcome_unknown IN (0, 1))
) STRICT;
INSERT INTO failure_records_v6 (audit_seq, transaction_name, stage, category, code, progress,
  possible_progress, outcome_unknown)
  SELECT audit_seq, transaction_name, stage, category, code, progress, possible_progress,
    outcome_unknown FROM failure_records ORDER BY audit_seq;
DROP TABLE failure_records;
ALTER TABLE failure_records_v6 RENAME TO failure_records;

UPDATE meta SET value = 'writ-ledger-6' WHERE key = 'schema_version';
