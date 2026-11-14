# M4 shared interfaces (lead-owned; D-040, D-041)

Binding for every M4 worker. Changes only by the lead. Design background: [M4-START-PLAN](M4-START-PLAN.md) sections 2..6. Status: **specified; contracts (M4-01) in implementation; independent review required before any consumer is integrated.**

## Layering

| Layer | Crate / module | May depend on | Must not contain |
|---|---|---|---|
| Kernel | `writ-kernel` | unchanged | anything M4 |
| Broker logic (storage-independent) | `writ-broker`: new `human` (verifier boundary types and trait), `approval` (records, state machines, checks), `acceptance` (signoff use-time checks), `refusal` (codes) | kernel, existing deps | network, HTTP, Tokio, WebAuthn library, randomness source, SQL |
| Persistence | `writ-ledger-sqlite`: `schema_v6.sql`, `approval_store.rs` | broker, rusqlite | HTTP, WebAuthn |
| Approval channel | new `writ-approval` crate | broker, webauthn-rs (D-041), hyper HTTP/1 + tokio (D-041) | kernel internals beyond broker APIs |

## I-1 Subjects, roles and the verifier boundary (`broker::human`)

- `SubjectId { issuer: String, subject: String }`: stable issuer or realm plus subject; never a display name. `PrincipalKind { Human, Service }` on every subject.
- `Permission { Approver, SignoffAuthority, Operator, Revoker }`, each granted per (tenant, project) scope (`RoleAssignment { subject, kind, permission, tenant_id, project_id, granted_at, revoked_at }`). Services can hold only `Operator` for declared automation operations; never `Approver` or `SignoffAuthority` (O-8).
- `trait HumanVerifier` (implemented in `writ-approval`; test implementations only in non-shipping test code):
  - `begin(&self, subject: &SubjectId, credentials: &[StoredCredential]) -> Result<CeremonyStart, VerifierError>`: `CeremonyStart { client_options: Vec<u8> /* sent to the page */, state: CeremonyState /* opaque bytes, persisted only in the ledger */, challenge_digest: Digest }`.
  - `finish(&self, state: &CeremonyState, response: &[u8], credentials: &[StoredCredential]) -> Result<VerifiedAssertion, VerifierError>`: `VerifiedAssertion { subject, credential_id, user_verified: true /* required */, method: "webauthn-passkey", credential_update: Option<CredentialUpdate> }`.
  - Only active credentials are passed in (revocation is the ledger lookup). Ceremony state never leaves the server and is never accepted from the client (D-041).
- `StoredCredential { credential_id, subject, public_material: Vec<u8> /* library-serialized passkey */, status: Active | Revoked, enrolled_at, enrolled_by }`.

## I-2 Challenges (O-3)

A challenge row binds, server-side: challenge id, `challenge_digest`, ceremony state, subject and allowed credential ids, operation (`Approve | Deny | Revoke | Signoff | Enroll | RoleChange`), verdict, tenant, project, target id (approval request, grant, signoff or subject), complete immutable context fingerprint (D-006 canonical digest of the stored target context as displayed), policy bundle digest and relevant revision, issued_at, expires_at = min(issued_at + 120 s, target expiry), idempotency key, status `issued | consumed | expired`. Consumption is in the same transaction as the durable decision. An identical retry (same idempotency key and same assertion outcome) after an unknown commit replays the original result; anything else with a consumed challenge is refused. Sessions (read-only inbox, at most 15 minutes) never authorize a state change.

## I-3 Approval records and state machine (`broker::approval`)

- `ApprovalRequest` (immutable once stored): request id (kernel `approval_request_id`), tenant, project, requester principal, agent id, delegation chain, action kind, canonical resource id, action digest, capabilities, policy bundle digest, request fingerprint, created_at, expires_at, `required_approvers: 1` (O-9; a policy needing more is refused `APPROVAL_QUORUM_UNSUPPORTED`). Status `pending -> approved | denied | expired`; `denied` and `expired` are terminal; `approved` produces exactly one grant.
- Created by `Broker::execute` when the use-time decision is `require_approval` and no grant is presented (agent still receives `APPROVAL_REQUIRED`; the request id goes only to the human channel).
- `ApprovalDecision { request_id, verdict, subject, credential_id, channel /* from listener config */, decided_at, challenge_id, provenance_version: "writ-provenance-2" }`; room for several decision rows per request, but one suffices and no quorum is implied.
- Separation (O-7): approver subject differs from requester principal, agent and every delegation-chain member; checked by `SubjectId`, and across all credentials and roles of the same subject.

## I-4 Revocation (O-5)

`Revocation { target: Grant(id) | Signoff(id) | Credential(id) | Subject(SubjectId) | Role(assignment id), by: SubjectId, at, reason, challenge_id }`. Ledger status is authoritative and rechecked at T-reserve, T-dispatch and promotion: before reserve refuse; after reserve before dispatch refuse with the receipt kept `reserved` and its grant use kept; after dispatch nothing is undone and the revocation is recorded as after-dispatch. Never refund, replenish or substitute.

## I-5 Signoff (O-4) and the amended record contract

- `Signoff { signoff_id, tenant, project, task, workflow, transition (from, to), acceptance_record_id, criterion_id, artifact_snapshot_id, artifact_digest, acceptance_policy_id, version, bundle digest, state_revision, issued_at, expires_at, subject, credential_id, challenge_id, provenance_version }`, stored in the ledger; consumed single-use ledger-wide atomically with the promotion.
- `authenticated-acceptance-record/2`: as `/1`, plus per-evaluation `signoff_id`, required exactly for trust `H` (and `attestation_id` exactly for trust `M`); `/1` records keep refusing human criteria (`ACCEPTANCE_HUMAN_UNAVAILABLE`), never reinterpreted; mechanical-only `/1` records stay valid.
- Use time (`check_use`, inside the promotion transaction): signoff exists in scope, provenance intact, binding equals record and requirements and current state, not revoked (signoff, credential, subject, role), fresh at trusted time, signer holds `SignoffAuthority` for the scope, signer is not the requester, agent, delegation chain, nor the subject who changed the artifact or acceptance policy for this revision, never consumed.

## I-6 Refusal codes (O-6; internal, external mapping generic)

`APPROVAL_REQUEST_UNKNOWN`, `APPROVAL_REQUEST_CLOSED`, `APPROVAL_REQUEST_EXPIRED`, `APPROVAL_QUORUM_UNSUPPORTED`, `CHALLENGE_INVALID` (unknown, consumed, expired or binding mismatch; diagnostic says which), `ASSERTION_INVALID` (verifier refusal; diagnostic from the library category only), `CREDENTIAL_REVOKED`, `SUBJECT_REVOKED`, `PERMISSION_MISSING`, `SEPARATION_VIOLATED`, `SERVICE_NOT_PERMITTED`, `ENROLLMENT_UNSUPPORTED`, `GRANT_REVOKED`, `SIGNOFF_UNKNOWN`, `SIGNOFF_REVOKED`, `SIGNOFF_MISMATCH`, `SIGNOFF_EXPIRED`, `SIGNOFF_CONSUMED`. Every new code maps to the kernel's generic external refusal.

## I-7 Persistence (`writ-ledger-6`, tables only)

`subjects`, `credentials`, `role_assignments`, `approval_requests`, `approval_decisions`, `challenges`, `revocations`, `signoffs`, `consumed_signoffs`, `approval_events` (provenance v2, no credential material). `SqliteLedger::open` accepts only `writ-ledger-6`; `SqliteLedger::upgrade` from exact `-3`, `-4`, `-5` keeps every row; at upgrade every pre-M4 grant becomes non-executable (O-14) by a recorded marker (no row rewritten or deleted; reservation and use history kept; dispatched or completed receipts keep reconciliation and replay). Enrollment and recovery: only the documented administrative ceremony (M4-08); otherwise `ENROLLMENT_UNSUPPORTED`.

## I-8 Ordering evidence (CO-PROP-02)

Every promotion, administrative operation and revocation commits a row in one ledger-wide monotonic sequence (`ledger_seq`, assigned inside the transaction), so tests establish the actual order of concurrent invalidations and commits and check each exactly.
