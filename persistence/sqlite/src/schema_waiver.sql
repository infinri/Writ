-- D-053, opt-in disposable ledger profile.
CREATE TABLE waiver_requests (
  waiver_id         TEXT PRIMARY KEY,
  intent_digest     TEXT NOT NULL UNIQUE,
  context           TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  ledger_seq        INTEGER NOT NULL UNIQUE REFERENCES ledger_sequence (seq)
) STRICT;

CREATE TABLE waivers (
  waiver_id      TEXT PRIMARY KEY REFERENCES waiver_requests (waiver_id),
  result         TEXT NOT NULL CHECK (result = 'waived'),
  issuer         TEXT NOT NULL,
  subject        TEXT NOT NULL,
  credential_id  TEXT NOT NULL,
  challenge_id   TEXT NOT NULL UNIQUE REFERENCES challenges (challenge_id),
  recorded_at    TEXT NOT NULL,
  ledger_seq     INTEGER NOT NULL UNIQUE REFERENCES ledger_sequence (seq),
  FOREIGN KEY (issuer, subject) REFERENCES subjects (issuer, subject),
  FOREIGN KEY (credential_id) REFERENCES credentials (credential_id)
) STRICT;

CREATE UNIQUE INDEX waiver_request_audit_once ON approval_events (target_id)
WHERE kind = 'waiver_requested';
CREATE UNIQUE INDEX waiver_commit_audit_once ON approval_events (challenge_id)
WHERE kind = 'waived';

CREATE TRIGGER waiver_requests_no_update BEFORE UPDATE ON waiver_requests
BEGIN SELECT RAISE(ABORT, 'waiver_requests is append-only'); END;
CREATE TRIGGER waiver_requests_no_delete BEFORE DELETE ON waiver_requests
BEGIN SELECT RAISE(ABORT, 'waiver_requests is append-only'); END;
CREATE TRIGGER waivers_no_update BEFORE UPDATE ON waivers
BEGIN SELECT RAISE(ABORT, 'waivers is append-only'); END;
CREATE TRIGGER waivers_no_delete BEFORE DELETE ON waivers
BEGIN SELECT RAISE(ABORT, 'waivers is append-only'); END;

CREATE TRIGGER waiver_audit_no_update BEFORE UPDATE ON approval_events
WHEN OLD.kind IN ('waiver_requested', 'waived') OR
     (OLD.kind = 'revoked' AND OLD.target_id LIKE 'waiver:%')
BEGIN SELECT RAISE(ABORT, 'waiver audit is append-only'); END;
CREATE TRIGGER waiver_audit_no_delete BEFORE DELETE ON approval_events
WHEN OLD.kind IN ('waiver_requested', 'waived') OR
     (OLD.kind = 'revoked' AND OLD.target_id LIKE 'waiver:%')
BEGIN SELECT RAISE(ABORT, 'waiver audit is append-only'); END;

CREATE TRIGGER waiver_revocation_no_update BEFORE UPDATE ON revocations
WHEN OLD.target_kind = 'waiver'
BEGIN SELECT RAISE(ABORT, 'waiver revocation is append-only'); END;
CREATE TRIGGER waiver_revocation_no_delete BEFORE DELETE ON revocations
WHEN OLD.target_kind = 'waiver'
BEGIN SELECT RAISE(ABORT, 'waiver revocation is append-only'); END;

CREATE TRIGGER waiver_challenge_no_update BEFORE UPDATE ON challenges
WHEN OLD.operation = 'waive' AND OLD.status = 'consumed'
BEGIN SELECT RAISE(ABORT, 'consumed waiver challenge is append-only'); END;
CREATE TRIGGER waiver_challenge_no_delete BEFORE DELETE ON challenges
WHEN OLD.operation = 'waive' AND OLD.status = 'consumed'
BEGIN SELECT RAISE(ABORT, 'consumed waiver challenge is append-only'); END;
