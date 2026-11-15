# writ-approval-server: production entry point and browser coverage (Y3, D-043)

Status: implemented on a worker branch, not yet integrated or independently reviewed. Release and production stay NO-GO (ADR-0008 approved in principle).

## Entry point `writ-approval-server`

Release binary of the workspace crate `writ-approval-server` (`approval-server/`: `[[bin]]` in `src/main.rs`, configuration in `src/lib.rs`). It is a separate crate so that the `writ-approval` library (verifier, listener, page) never links the SQLite ledger.

```
writ-approval-server --ledger <path> --port <1..65535> --rp-id localhost \
  --origin http://localhost:<port> --channel <identity> --subject-issuer <issuer>
```

Every setting is required and given exactly once on the command line. There is no default, no configuration file, no environment variable and no verifier choice.

What it does, in order (each step fails closed before the next):

1. Validates the configuration. `--rp-id` must be `localhost`; `--origin` must be exactly `http://localhost:<port>` for the given port; port 0 is refused (the origin must be known before binding); channel identity and issuer are 1 to 128 characters from `[A-Za-z0-9._:-]`.
2. Builds the reviewed `webauthn-rs` `PasskeyVerifier` for that RP id and origin and one `ApprovalChannel` over it. This is the only verifier the binary can build: there is no test verifier, switch or fallback.
3. Opens the existing ledger with `SqliteLedger::open`, the normal ownership path (canonical identity, sidecar ownership lock, schema verification `writ-ledger-6`, owner epoch), and attaches the channel once (`with_approval_channel`). The ledger is never created here, and bootstrap stays the separate `BootstrapSession` ceremony (M4-ENROLLMENT-CEREMONY).
4. Starts the trusted host clock (`HostClock`, wall plus `CLOCK_BOOTTIME`) against the ledger's persisted floor. A blocked startup rollback is refused.
5. Binds the loopback listener on `127.0.0.1:<port>` (the listener has no address setting), checks that its fixed origin and RP id equal the configuration, prints one readiness line on standard output and serves until it is terminated. The operating system releases the ownership lock at exit.

Fails closed, non-zero exit, nothing bound:

| Condition | Exit |
|---|---|
| A setting missing, repeated, empty, unknown (including `--test-verifier` or any verifier switch), non-UTF-8 or invalid | 2, before the ledger is touched |
| Ledger absent (never created), not a `writ-ledger-6` ledger, or owned by another process (`OwnedElsewhere`) | 1 |
| Verifier or channel cannot be built; clock floor unreadable or rollback unacknowledged | 1 |
| Port in use, or bound origin differs from the configuration | 1 |

Release-build contents: `cargo tree -p writ-approval-server -e normal,build` has no `webauthn-authenticator-rs` (the software passkey is a dev-dependency); the verifier tests, `m4_support` and the ledger's `*_for_tests` functions are `#[cfg(test)]` and are never compiled into the binary. `tests/server_entry.rs` checks the configuration and ledger failures against the real binary and scans it for test-verifier symbols with a positive control (`PasskeyVerifier`); `approval/browser-tests/check-release-binary.sh` repeats the scan on the release binary.

Dependencies: workspace path crates only (`writ-approval`, `writ-broker`, `writ-ledger-sqlite`); no registry package of its own. It is gated by `approval-server/deps-allowlist.json` (empty `packages`; `forbidden` and `forbidden_anywhere` keep TLS stacks, OpenSSL, `reqwest`, HTTP/2 and the software test authenticator out of its complete closure). `writ-approval` has no SQLite dependency, and its `forbidden_anywhere` list proves it.

Trust boundary (M4-INTERFACES, configured approval channel): any code that links these crates and opens the ledger is inside the trust boundary. Deploying only this reviewed binary is the operational requirement of the local profile.

## Routes of the loopback listener (D-043; D-044 item 3, M4-INTERFACES Z1..Z4)

Every route keeps exact `Host`, `Origin` (required on every `POST`), form bodies within the bounds, Fetch Metadata (LOW-1: pages `same-origin` or `none`, every `POST` and `GET /view` exactly `same-origin`), the per-session CSRF token, the `HttpOnly` `SameSite=Strict` cookie `writ_session` and the strict Content-Security-Policy. Authentication is a purpose-separated passkey login (`ReadSession` ceremony, purpose `read`) that opens a read session of at most 900 s; a read session never authorizes a change.

| Route | Needs | Purpose |
|---|---|---|
| `GET /` | nothing | landing page: the login form only (no record, no session) |
| `GET /static/approval.js`, `GET /static/approval.css` | nothing | page script and style |
| `POST /login/begin` | nothing (`subject`); own bound (the oldest login is evicted) and rate limit (429 `rate`); 503 for every subject during a verifier outage | login challenge in a new pre-authentication session; a presented session is ended; a subject the ledger refuses gets a decoy of identical shape (4 stable `allowCredentials` with enrolled lengths, review M1); held to a 100 ms latency floor |
| `POST /login/finish` | the login session (`csrf`, `challenge_id`, `response`) | verifies the assertion, binds the read session (subject, credential) to the page session, rotates the cookie and CSRF token; every failure is one 403 `refused` and ends the login session; held to a 100 ms latency floor |
| `GET /request/<id>` | read session; `approver` over the request's scope | approval request page (approve or deny) |
| `GET /signoff/<id>` | read session; `signoff_authority` over the signoff's scope | pending signoff page (exclusions, `delegation_chain: unavailable`, pinned effective expiry) |
| `GET /revoke`, `GET /role`, `GET /enroll` | read session; live `revoker` (revoke) or `operator` (role, enroll) over some scope | operator forms (no record) |
| `POST /prepare` | read session; the operation's permission (`revoker`, or `operator` for roles, enrollment and registration start) | the form's context through the ledger's scoped read; answers `{"view":"/view"}` |
| `GET /view` | read session (re-read at every display) | the prepared context |
| `POST /ceremony/begin`, `POST /ceremony/finish` | read session; a fresh operation-bound ceremony whose actor is the read session's subject | approve, deny, signoff, revocation, role change, registration start, enrollment |
| `POST /registration/finish` | read session of the operator who started the row | the new person's passkey registration (D-043 Y2) |

Every protected read is the ledger's one read API (`ApprovalLedger::read_record`), which rechecks the session, the subject, the credential and the scoped permission inside its read transaction. Anonymous, unauthenticated, expired, revoked, out-of-scope, unknown and guessed targets all receive one identical answer: the same 404 page and no session (`POST /prepare`: 403 `refused`). There is no unauthenticated status or context endpoint. The fixture's `status` command reads the disposable ledger in process after the server exited; it is not a route.

## End-to-end browser suite (`approval/browser-tests/`)

Real path: headless Google Chrome, then the real `writ-approval-server` binary, then the production `webauthn-rs` verifier and the SQLite ledger. User verification is simulated: the authenticator is a CDP virtual authenticator (`WebAuthn.enable`, `WebAuthn.addVirtualAuthenticator` with `hasUserVerification: true, isUserVerified: true`; `WebAuthn.setUserVerified` turns it off for the negative cases). No real user or credential is enrolled. Nothing binds beyond `127.0.0.1`. The suite uses no npm package: Node 22's built-in `WebSocket` drives CDP, and `node:http` serves a placeholder page. The temporary directory (ledger, Chrome `--user-data-dir`, Chrome's own temporary files) is created with `mkdtemp`, and the suite removes it after closing the browser.

Setup: the non-shipping example `approval-server/examples/browser_fixture.rs` creates the ledger. It then bootstraps the administrator through `BootstrapSession`; the creation options go to the browser, whose virtual authenticator creates the credential on a placeholder page served at the server's future origin. Next it reopens the ledger normally, registers the approver's credential in the browser the same way, and enrolls the approver (`approver` in every scope) through the administrator's `Enroll` ceremony, also signed in the browser. Last, it records the pending approval requests the flows need and exits. After the run, the fixture's `status` command reads the final ledger state.

Every flow signs in first through the landing page or the login routes (`flows/login.mjs`, D-044 Z1). Flows (`approval/browser-tests/flows/approve-deny.mjs`, which also carries `flows/read-sessions.mjs`, and `flows/operator-pages.mjs`):

- **approve via page:** the page shows the stored fingerprint and escapes the untrusted requester text. Approving answers `approved`, and the ledger status is `approved`.
- **deny via page:** answers `denied`, and the ledger status is `denied`.
- **UV false, unmodified page:** with `isUserVerified=false`, the page result is not `approved`, and the request stays pending.
- **UV false, server refusal:** the client options are tampered with through CDP `Fetch` (`userVerification: discouraged`), so an assertion with UV=0 reaches the server. The server answers `ASSERTION_INVALID`, and the request stays pending.
- **wrong origin, HTTP level:** a begin with `Origin: http://localhost:<other port>` answers 403 `origin`, and `Host: 127.0.0.1:<port>` answers 421.
- **wrong origin, relayed assertion:** the challenge is begun on the real origin and signed in the browser on another port (same RP id). The finish answers `ASSERTION_INVALID`, and the request stays pending.
- **stale display, fingerprint:** a displayed fingerprint other than the stored one answers `stale display`.
- **stale display, decided:** the page was shown while the request was pending and the request was then denied. Approving from that page answers `refused`, and the ledger status is `denied`.
- **the actor is the signed-in subject:** a begin naming another subject than the signed-in one answers `refused`; the request stays pending.
- **anonymous direct URLs (D-044):** every page and direct URL (`/request/<id>`, guessed ids, `/signoff/<id>`, the operator forms, `/view`) answers the same 404 body without a session; `POST /prepare` answers 403 `refused`; the browser without cookies sees the login page and the same refusal.
- **valid scoped read:** the target subject (approver in `t-browser`/`p-browser`) signs in with its passkey and reads a request of its scope.
- **cross-scope reads:** a request of tenant `t-other` for that approver, a request for the signoff reviewer and for the operator, the signoff for the approver, and an approver's role `prepare` all answer the one refusal.
- **replayed login assertion:** the same login finish again (old and rotated cookie) is refused; the old assertion on a new login challenge answers `ASSERTION_INVALID` and leaves that session unauthenticated; a login challenge finished as a ceremony is refused.
- **session invalidated after revocation:** the target's open session reads a request; the administrator revokes its credential through the revoke page's ceremony; the open session (raw and in the browser) then gets the one refusal and a new login is refused.
- **login begin indistinguishable (review M1):** an enrolled and an unknown subject get the same status, header names, body shape and four stable credential ids; both finishes answer 403 `refused`.
- **operator forms need the live permission (review LOW-3):** the reviewer's and the approver's sessions get the one 404 on `/revoke`, `/role` and `/enroll`; the administrator gets the forms.
- **operator pages:** revoke, role change, enrollment (the operator is the signed-in subject), signoff and their refusals, each after signing in; a registration start prepared by an approver is refused before anything is displayed.

Run locally (pinned toolchain, Chrome at `/usr/bin/google-chrome`, Node 22):

```
approval/browser-tests/run.sh
```

It builds the release binary and the fixture (`--locked`), runs the release-binary check, and then runs `node approval/browser-tests/run.mjs` under `m3/tools/run-with-clean-fds.sh`. Every result line is `PASS` or `FAIL` and carries the label `[CDP virtual authenticator, simulated UV]`. `--only <substring>` selects flows locally; CI mode refuses it.

Adding flows (signoff, revoke, enrollment and role-change pages): add a module under `flows/` exporting `flows`, each `{ name, requests, expect, run(t) }`, and list it in `FLOW_MODULES` in `run.mjs`. Any new fixture state goes into `approval-server/examples/browser_fixture.rs` as further JSON-line steps.

Required CI (D-044 item 2). The complete suite is a step of the required job "Production Rust workspace" in `.github/workflows/v2-docs.yml`; it is a step rather than a new job because only existing job names are required checks, and branch protection is not changed. The step reuses the job's pinned Rust 1.99.1 (`rust-toolchain.toml`) and pinned SQLite 3.54.1 (D-023); in CI mode `run.sh` uses that environment as is (it checks `rustc 1.99.1` and the SQLite library instead of sourcing `m1/tools/env.sh`). Tool versions:

- Node: installed by `actions/setup-node@v4` with `node-version: '22.22.3'` (exact pin). The job checks `node --version` is `v22.*`, and `run.mjs` refuses any other major than `node_major` in `approval/browser-tests/ci-manifest.json`.
- Chrome: the runner image's Google Chrome (`ubuntu-latest`), verified with `command -v google-chrome` and `google-chrome --version`. `run.mjs` refuses a missing Chrome or a major below `chrome_min_major` in the manifest. Chrome is not pinned to an exact version; the version that ran is recorded.
- The job log prints both versions, and the results file records them (`tools.node`, `tools.chrome`, `tools.chrome_cdp` from CDP `Browser.getVersion`).

CI mode (`WRIT_BROWSER_CI=1`) fails the step when Node or Chrome is missing or of the wrong major, when setup fails, when `--only` or any other reduced selection is given, when the flows loaded from `FLOW_MODULES` do not number exactly `expected_flow_count` in `approval/browser-tests/ci-manifest.json`, when any loaded flow did not run, or when any flow or final ledger check fails. There is no `continue-on-error` and no skip. When a flow is added or removed, `expected_flow_count` must change in the same commit.

Results: `--results` and `--diagnostics` (both required in CI mode) write `browser-suite-results.json` (flow names, executed list, pass or fail, expected and actual ledger statuses, tool versions, and a failed flow's reason) and `browser-suite-diagnostics.log` (harness log, server and fixture stderr, Chrome console messages and page exceptions). Both are written on failure too, and `actions/upload-artifact@v4` uploads them as `approval-browser-suite-results` with `if: always()`. Every string passes through `approval/browser-tests/lib/sanitize.mjs`, which redacts session cookies, CSRF tokens, ceremony ids and state, challenges, credential ids, assertion and attestation fields, keys, tokens, and long random base64, base64url or hex runs (over-redaction is accepted). The results file holds no flow detail text and no stored record contents. `approval/browser-tests/sanitize-selftest.mjs` runs as its own CI step before the suite and proves the known secret shapes are redacted. Cleanup of Chrome, the server, the fixture and the temporary directory runs on failure as well as on success.

Local CI-mode run (pinned environment sourced first, as the CI job provides it):

```
source m1/tools/env.sh
WRIT_BROWSER_CI=1 NODE=<node 22> TMPDIR=/tmp approval/browser-tests/run.sh --results results.json --diagnostics diagnostics.log
```

Notes: Chrome's singleton socket path must be short, so `TMPDIR` must not be a deeply nested directory. The suite points Chrome's `TMPDIR` at its own temporary directory, and on SIGINT or SIGTERM it closes the browser and removes that directory.

## Results (worker W-G, 2026-11-14, Chrome 150.0.7871.46, Node 22.22.3)

- `approval/browser-tests/run.sh`: `check-release-binary: PASS`, and the browser suite reported 16 passed, 0 failed (8 flows plus 8 final ledger-status checks), all labelled CDP virtual authenticator, simulated UV. The temporary directory was removed.
- `cargo test --locked --workspace`, run under `run-with-clean-fds.sh`, passed, including `approval-server/tests/server_entry.rs` (5 tests). `cargo clippy --workspace --all-targets -D warnings` and `cargo fmt --check` were clean. `check_deps.py` reported 0 problems, and `check-v2-docs.py` passed.

## Results (worker W-H, D-044 item 3, review and re-review fixes; 2026-11-15, Chrome 150.0.7871.46, Node 22.22.3)

Local results on the worker branch, not yet run in CI on the resulting commit.

- `WRIT_BROWSER_CI=1 approval/browser-tests/run.sh --results .. --diagnostics ..` (after `source m1/tools/env.sh`), run twice: `check-release-binary: PASS`, and each run reported 40 passed, 0 failed, 25 of 25 flows executed (25 flows, 14 final ledger-status checks, 1 stored-signoff check), all labelled CDP virtual authenticator, simulated UV; the temporary directory was removed each time. Login finishes measured 102 to 104 ms for both the real refusal and the decoy (the 100 ms floor). The flows retry a login begin answered `rate`.
- `cargo test --locked --workspace` under `run-with-clean-fds.sh`: 572 passed, 0 failed, 4 ignored (the kill tests, then run alone: 4 passed). Property gate (CI profile): 19,796 checks, 0 failed. `cargo fmt --check` and `cargo clippy --workspace --all-targets -D warnings` clean. Closure gate `--repeat 2`: exact 17, adapted 72, approved differences 38, deferred 0, GATE PASS, report verifies. `check_deps.py`: 0 problems. `check-v2-docs.py` passed.
