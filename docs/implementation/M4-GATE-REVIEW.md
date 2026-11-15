# M4 gate-review package (M4-14; D-040..D-043)

Status: **M4 ACCEPTED as scoped development scope (D-044, conditions met, E-077).** Conditions: authenticated scoped reads (Z1..Z4, independently reviewed), browser suite as a required CI step, required CI on the exact SHA (`8b2057a`, run 43659759755, 6/6). Sections 1 to 7 below are the package as reviewed; their limitations and carried obligations remain in force. ADR-0008 APPROVED IN PRINCIPLE; release and production **NO-GO**. Nothing here claims verified production security or host enforcement.

## 1. What is asked of the owner

1. Accept M4 (authenticated approval and signoff, local WebAuthn profile) as scoped development scope, on the code and evidence below, with the limitations in section 6 and the carried obligations in section 7.
2. Decide whether the browser suite (`approval/browser-tests/run.sh`) becomes a required CI step (proposed text in `approval-server/README.md`; needs Chrome and Node 22 on the runner). Today it is run locally and recorded as evidence; required CI does not run it.
3. Decide LOW-2 (section 6): keep read access of displayed contexts for any loopback client under R6 for the local profile, or add an operator-authenticated page session before `/prepare` (a new ceremony operation).

## 2. Scope delivered (TASKS M4-00..M4-14)

| Task | Status | Delivered |
|---|---|---|
| M4-01 | DONE | Contracts: subjects, roles, verifier boundary, challenges, approval requests and decisions, revocation, signoff object, record `/2`, refusal codes; independently reviewed |
| M4-02 | DONE (local profile) | webauthn-rs 0.6.2 `PasskeyVerifier`; configured `ApprovalChannel` (one verifier, RP id `localhost`, origin `http://localhost:<port>`, recorded on every challenge); challenges single use, at most 120 s, bound to operation, verdict, target, fingerprint, idempotency key |
| M4-03..M4-05 | DONE | Approval requests and state machine (denied and approved terminal), approve and deny with a consumed challenge, revocation serialized against reserve, dispatch and promotion (O-5 matrix tested) |
| M4-06 | DONE | Signoff issuance, exclusions read from the ledger and fingerprinted, server-controlled expiry (at most 600 s, D-043), use-time verification and atomic consumption with promotion |
| M4-07 | DEFERRED to M6 (CO-M6-02) | Waivers (O-10) |
| M4-08 | DONE | Verified-registration enrollment (ES256 only), operator-authenticated `RegistrationStart` ceremony binding rows to operator, page session, subject and relying party (D-043), one-shot `BootstrapSession` through the ownership lock, role change |
| M4-09 | DONE | Provenance v2 events for every ceremony; no credential material stored |
| M4-10 | DONE | `writ-ledger-6`; explicit upgrade from `-3`, `-4`, `-5`; legacy grants non-executable with history kept (O-14) |
| M4-11 | DONE | The 25 carried closure cases resolved by measured results (D-042, D-043): 0 deferred, 0 unexplained |
| M4-12 | DONE | Ordering evidence (`ledger_sequence`) and exact concurrent revocation-versus-promotion ordering tests (CO-PROP-02) |
| M4-13 | DONE (local profile) | Hyper HTTP/1 loopback page: approve, deny, signoff, revoke, role change, enrollment; release binary `writ-approval-server` (configuration required, no test-verifier path, fails closed) |
| M4-14 | this package | |

## 3. Decisions in force

D-040 (M4 authorization O-1..O-14), D-041 (dependency adoption, Hyper and Tokio listener), D-042 (DR-5 licences; S1..S8 remediation), D-043 (`sc-12` approved difference; signoff expiry at most 600 s; worker cleanup; M4 completion requirements). Binding design: [M4-INTERFACES](M4-INTERFACES.md) X1..X8, configured approval channel, Y1..Y3, pages and registration binding.

## 4. Evidence

- **Test evidence:** [TEST-EVIDENCE](TEST-EVIDENCE.md) E-075 (D-042 remediation) and E-076 (D-043 completion).
- **Closure gate (required CI):** 127 cases: exact 17, adapted 72, approved production differences 38, deferred 0, unexplained 0, unclassified 0; companions 33/33; report verification current. Not 127/127 exact parity; every difference is an approved decision (D-036..D-043).
- **Property gate (required CI):** broker property run 19,796 checks, 0 failed.
- **Dependency gate (required CI):** 0 problems with OSV; `rsa` 0.10.0-rc.19 (RUSTSEC-2023-0071) under the DEVELOPMENT-ONLY exception of D-041 DR-3, visible in every run; `--release` fails on it.
- **Browser suite (local, not required CI):** headless Chrome, disposable profile, CDP virtual authenticator with **simulated user verification**; every page flow through the real HTTP and verification paths, positive and negative.
- **Exact-SHA CI:** recorded in E-076 and SESSION-HANDOFF for the final commit of this package.

## 5. Security reviews

| Review | Scope | Result |
|---|---|---|
| Contract review (M4-01) | contracts | mediums fixed (R1..R8), lows as consumer obligations C1..C4 |
| Approval-ledger review | ledger after integration | S1 HIGH, S2/S3 MEDIUM, S4..S8 LOW: all fixed (D-042, E-075) |
| Independent review of `5b1376b` and re-review of `fd5444a` | S1..S8, public API, release/test separation, enrollment, HTTP boundary | no CRITICAL/HIGH; M1 MEDIUM fixed (configured channel); L1..L8 and N1..N3 fixed or documented |
| Independent re-review of `041e52e` and of `041e52e..398b8f1` | new routes, registration start, signoff expiry, release executable, gate changes | no CRITICAL/HIGH/MEDIUM; LOW-1 fixed and tightened (`fadb0c7`), LOW-2 and LOW-3 documented, LOW-4 test gaps closed |

## 6. Limitations (accepted or to be decided; never claimed otherwise)

- **Trust boundary:** code that links these crates in process and opens the ledger is inside the trust boundary (equivalent to ledger-file access); protection is the sidecar ownership lock, X1/X8 (the ledger accepts only its own rows) and deploying only reviewed binaries. `LedgerCapability`'s constructor stays public, confined by the source scanner.
- **Simulated user verification in tests:** no real authenticator or real user has been used; no real user is enrolled.
- **Delegation chain:** sealed requests carry no trusted chain; separation covers requester, agent and linked identities (recorded as `delegation_chain: unavailable`).
- **LOW-2 (owner decision 3):** any loopback client can read the displayed context of named records through `/prepare` and `/view` (existence and content oracle; no credential material or authority).
- **LOW-3 / N2:** hostile loopback requests can grow the ledger without bound and can sustain a denial of service of the page (120 s bounds one challenge, not the overall outage).
- **L7:** a legacy grant label equal to `approval/<request id>` blocks that approval (fail closed); an explicit migration and recovery obligation.
- **No graceful shutdown** of `writ-approval-server` (Tokio `signal` feature not adopted); the ownership lock is released at process exit.
- **Library behaviour:** webauthn-rs accepts a device-bound credential becoming backup eligible (passkey flow default); followed per D-040 O-1.
- **Dependency risks:** `crypto-glue` single maintainer (DR-4); `rsa` development-only exception (DR-3); release needs explicit dispositions.

## 7. Carried obligations after M4

CO-M5-01 (contained runner), CO-M5-02 (host enforcement, immutable snapshots, SR-06), CO-M5M9-01 (production key management), CO-M6-01 (registry publication, signed policies), CO-M6-02 (waivers), CO-M7-01 (evidence catalog), CO-PROP-01 (effect-store faults), CO-HR-01 (harness-review limits), plus from M4: ledger retention and compaction of audit, challenge and registration history (LOW-3), legacy-label collision recovery (L7), enterprise OIDC/SSO profile and IdP offboarding policy (O-1, O-5), N-of-M approvals (O-9), browser suite in required CI (decision 2), dispositions of the `rsa` exception and `crypto-glue` concentration before any release.
