-- Writ production SQLite ledger, schema writ-ledger-6 additions (M4: authenticated approval,
-- revocation, signoff and enrollment; D-040, M4-INTERFACES I-7 and I-8), applied after
-- schema_v5.sql to a new ledger, or by the explicit operator upgrade (SqliteLedger::upgrade)
-- from writ-ledger-3, -4 or -5, which keeps every existing row. Only new tables.
-- Timestamps are the kernel's RFC 3339 UTC text. No credential secret is ever stored: only
-- public passkey material, credential ids and references to consumed challenges.

-- I-8: one ledger-wide monotonic sequence. Every promotion, administrative operation,
-- revocation, approval decision, signoff, enrollment, role change ('administration') and
-- expiry step that changes anything commits exactly one row inside its own transaction, so
-- tests read the actual commit order (D-042 X6).
CREATE TABLE ledger_sequence (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL CHECK (kind IN ('promotion', 'administration', 'revocation',
               'decision', 'signoff', 'enrollment', 'bootstrap', 'expiry')),
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
                        ('approve', 'deny', 'revoke', 'signoff', 'enroll', 'role_change')),
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
  status              TEXT NOT NULL CHECK (status IN ('issued', 'consumed', 'expired',
                        'refused')),
  refusal_code        TEXT,
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
  provenance_version           TEXT NOT NULL
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
                        'role_changed')),
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

UPDATE meta SET value = 'writ-ledger-6' WHERE key = 'schema_version';
