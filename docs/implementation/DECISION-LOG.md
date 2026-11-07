# Writ V2 implementation decision log

Implementation-level decisions. Architectural decisions belong in [ADRs](../decisions/). Do not reverse an entry without new evidence; add a superseding entry instead.

## D-001: M0 harness is a Python reference oracle, not the production kernel

- **Date:** 2026-11-05
- **Context:** M0 needs executable contract validation, simulated providers/hosts and a decision oracle before M1. ADR-0002 keeps Rust vs Go OPEN. The workstation has Python 3.12 and uv; neither cargo nor go is installed.
- **Alternatives:** (a) start a Rust or Go kernel now; (b) write M0 checks only as JSON fixtures with no executable oracle; (c) Python reference harness under `m0/` emitting language-neutral fixtures.
- **Selected:** (c).
- **Rationale:** (a) pre-empts ADR-0002 and cannot be built here; (b) leaves fixture expectations unverified. A small Python oracle makes expected verdicts executable while the fixtures themselves stay JSON, so Rust and Go consume identical inputs in M1.
- **Consequences:** Nothing under `m0/` may be described as the V2 kernel or shipped. Production code must not import `m0/`. The oracle's verdicts become the M1 parity expectations and require independent review for high-risk fixtures.
- **Related:** ADR-0002, MASTER-IMPLEMENTATION-PLAN section 10.

## D-002: Audit reads V1 through Git objects at a pinned SHA

- **Date:** 2026-11-05
- **Context:** V1 files remain in the working tree of this branch and could be edited.
- **Selected:** All audit generators read `git show <sha>:<path>` for main `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`.
- **Rationale:** Reproducible evidence regardless of working-tree edits; matches the baseline recorded in docs/audit/V1-ARCHITECTURE-BASELINE.md.
- **Consequences:** Audit requires a clone containing that commit (shallow clones fail with an explicit error).

## D-003: Generated audit artifacts are committed and drift-checked

- **Date:** 2026-11-05
- **Selected:** Generators write deterministic files to `docs/audit/m0/`; `python -m writ_m0.audit check` fails if any is stale.
- **Rationale:** Reviewers can read evidence without running code; CI can detect hand edits or generator changes.

## D-004: Rule disposition is a documented heuristic first pass

- **Date:** 2026-11-05
- **Context:** M0 asks to classify 288 rules; a full semantic review is a separate human-quality task.
- **Selected:** Deterministic precedence (duplicate, obsolete, candidate_enforced, unverified, advisory) over metadata and statement text, with every row labeled `review_status=automated_first_pass` and `conflict_check=not_performed`.
- **Consequences:** Dispositions are triage, not verdicts. M0-01d must review them before any V2 import. "obsolete" means the V1 mechanism is retired; the rule's intent may still carry forward into a Capability Pack.

## D-005: M0 tests isolated from the V1 test configuration

- **Date:** 2026-11-05
- **Selected:** `m0/pytest.ini` and a dedicated venv at `m0/.venv` (gitignored by the existing `.venv/` pattern).
- **Rationale:** V1's root pyproject addopts and `tests/conftest.py` require a disposable Neo4j; M0 must run with no external services.

## D-006: Canonical JSON for digests admits no floats

- **Date:** 2026-11-05
- **Context:** Approval grants, permits and receipts bind to `action_digest`; Rust and Go must compute byte-identical digests. Float formatting differs across languages unless a full RFC 8785 number serializer is shared.
- **Alternatives:** full RFC 8785 (JCS) including ECMAScript number formatting; ad hoc per-language JSON; restricted domain.
- **Selected:** JCS-style encoding (sorted keys by code point, no whitespace, UTF-8, minimal escaping) over a restricted domain: no floats, integers within +/-(2^53-1), no lone surrogates, no Unicode normalization, depth <= 64. Digest string `sha256:<hex>`.
- **Rationale:** Removes the main cross-language divergence source at the cost of encoding quantities as integers or strings. Unicode normalization is refused because it can alias distinct identifiers and paths.
- **Consequences:** Any digested payload containing a float is rejected. Key ordering is by code point, which differs from JCS UTF-16 ordering only for astral-plane keys; M1 implementations must follow this module. Reference: `m0/writ_m0/canonical.py`, fixed vector in `m0/tests/test_contracts.py`.

## D-007: Evidence carries verifiable claims; evaluators ignore producer status

- **Date:** 2026-11-05
- **Context:** The Evidence draft could not express counterevidence, publication dates or the verbatim text a citation relies on, so a fabricated citation could not be detected mechanically.
- **Selected:** Optional Evidence fields `evidence_kinds`, `stance`, `source_published_at`, `quoted_excerpt` (still contract 2.0 draft). The core evaluator resolves every source through a tenant-scoped SourceCatalog port, verifies digest and excerpt, and delegates kind-specific checks to pack-supplied functions. Producer `verification_status` is never trusted. Model-assisted records cannot raise an aggregate to pass.
- **Consequences:** Packs must register a check for every evidence kind they use; an unregistered kind fails. Related: INV-24, INV-25, P-17.

## D-008: Reference decision semantics, scoped resource ids and use-time re-decision

- **Date:** 2026-11-05
- **Context:** M1 needs one unambiguous semantics for Rust and Go to reproduce; the drafts left precedence, resource identity and permit flow open.
- **Selected:**
  - Evaluation order: schema validity, policy loaded and pin match, principal issuer trusted by the bundle, state revision, all capabilities known, canonical resource id, resource scope, statements.
  - Precedence forbid > require_approval > permit > default deny. Every requested capability must be covered by a matching approval or permit statement.
  - Canonical resource ids are `<scheme>:<tenant>/<project>/<path>` with no empty, `.` or `..` segments; a request may only target its own tenant and project (structural INV-10 check, independent of policy statements).
  - The broker does not trust a previously returned Decision. It re-runs the kernel decision at use time, consumes approval grants inside the same SQLite `BEGIN IMMEDIATE` transaction that reserves the idempotency key, and passes effect adapters an HMAC permit they must verify.
- **Alternatives:** broker trusting decisions handed to it by the host (rejected: stale or forged decisions); separate grant and idempotency transactions (rejected: crash windows that double-consume or strand grants).
- **Consequences:** Policy change invalidates outstanding grants (binding includes bundle sha). Golden cases in `docs/contracts/fixtures/decisions/m0-golden.json` are hand-reasoned and become the M1 parity oracle. Related: INV-01..INV-10, INV-13, INV-14, INV-17, ADR-0003.

## D-009: Grants are per requester principal; malformed grants are refused

- **Date:** 2026-11-05
- **Context:** Porting V1 regressions (M0-01f). V1 let a subagent inherit its parent's manual-test grant; a V2 ledger grant missing a binding field crashed the broker with KeyError.
- **Selected:** No grant inheritance: a subagent (distinct requester principal) needs its own approval. The broker validates a stored grant against the ApprovalGrant schema before use and refuses with `GRANT_MALFORMED`.
- **Rationale:** Inheritance widens a human approval to principals the human never saw. A grant that cannot prove its binding must count as unapproved (V1 `test_gate_approval_plan_binding` lesson).
- **Consequences:** Multi-agent workflows request approval per subagent unless a future, explicitly reviewed delegation contract is added. Pending independent review with the golden cases (M0-05a).

## D-010: Authenticated broker ingress; the kernel stays key-free

- **Date:** 2026-11-05
- **Context:** Independent review SR-01. The broker accepted any ActionRequest dict, and the kernel trusts `principal.verified_by` if it names a bundle-trusted issuer, so a caller bypassing the host adapter could assert any identity, tenant, roles or digest.
- **Alternatives:** (a) kernel verifies a PrincipalProof itself (pulls keys and clock-sensitive proof checks into the pure kernel, and still leaves the operation digest unauthenticated); (b) broker verifies a PrincipalProof passed beside the request (authenticates identity but not that the digest and capabilities came from trusted normalization); (c) adapter seals the complete normalized request, broker verifies the seal.
- **Selected:** (c). `writ_m0/ingress.py`: a trusted adapter seals the canonical ActionRequest with its registered ingress key after verifying the proof and computing the digest; the broker refuses anything else with `INGRESS_UNAUTHENTICATED`, and the seal's adapter id must equal `principal.verified_by`. HostAdapter refuses construction if its sealer identity differs from its proof issuer.
- **Consequences:** Kernel `decide()` remains pure and keyless; its `verified_by` check is sound only behind this ingress, which is now the documented precondition. Production key management, rotation and adapter registration are open (M3/M5). This does not authenticate the host process itself (threat model residual risk).

## D-011: Owner policy decisions on cases 01, 08, SR-03 and SR-04

- **Date:** 2026-11-05. **Decided by:** project owner (human). Recorded verbatim in substance; no code change accompanies this entry.
- **Case 01 (workspace writes):** ordinary project-scoped workspace writes stay permitted under an explicitly authorized policy, without human approval per edit, provided trusted identity, canonical paths, project isolation and explicit sensitive-path protection are enforced. The reference fixture's broad, role-free permit is **not** the production default.
- **Case 08 (precedence):** require_approval stays above permit. An operator role does not override an approval requirement; any administrator exemption must be an explicit, separately reviewed policy provision.
- **SR-04:** subagents do not inherit a parent's approval (confirms D-009). Bounded, explicit delegation may be designed later.
- **SR-03:** an expired request is `invalid` / `DEADLINE_EXCEEDED`; every non-allow status must fail closed at every host and adapter.
- **Effect on review status:** these are policy decisions, not independent review verdicts. All golden cases remain PENDING INDEPENDENT REVIEW.

## D-012: Bind effects and receipts to the whole request; scope evidence by project

- **Date:** 2026-11-05
- **Context:** Independent review 2 (SR-10, SR-11) and open SR-05.
- **Selected:**
  - BrokerPermit and EffectReceipt carry `request_fingerprint`, the canonical digest of the complete ActionRequest. The effect adapter refuses a request whose fingerprint differs from the (MAC-protected) permit before consuming the permit. An idempotent duplicate returns the stored receipt only when both fingerprint and the authorizing grant match; otherwise IDEMPOTENCY_CONFLICT.
  - Evidence carries a required `project_id`; SourceCatalog resolution is keyed by tenant and project; evaluators require evidence scope to equal the task's.
- **Alternatives:** compare a list of normative fields (rejected: silently misses fields added later); widen the idempotency namespace to include principal and project (rejected for now: a conflict refusal is fail-closed and simpler; revisit if legitimate cross-principal key reuse appears).
- **Consequences:** Retries must resend the identical request (same request id and idempotency key) to receive the original receipt. Explicitly shared cross-project evidence needs a future contract. Kernel unchanged and key-free.

## D-013: Artifact and signoff scope; scope mismatch is an evaluation failure

- **Date:** 2026-11-05
- **Context:** Independent review 3, SR-12. Evidence was project-scoped (D-012) but artifacts and human signoffs were not.
- **Selected:** Artifact requires `project_id`. `evaluate()` records an `artifact:scope` criterion that fails unless artifact tenant, project and task equal the TaskSpec's. A SignoffGrant counts only when tenant, project, task and artifact digest all match.
- **Malformed vs mismatched scope:** schema-invalid input (missing `project_id`, wrong types) raises ContractError, as before; a schema-valid artifact or evidence item from another scope produces an evaluation `fail`, never `pass`. Both are non-pass and therefore fail closed.
- **Not covered:** SignoffGrant is still a value the caller asserts was verified; in production it must be produced only by the authenticated approval service (as ApprovalGrant is in the broker). Shared cross-project artifacts are not modeled.
- **Related:** INV-10, INV-25, P-17, D-012.

## D-014 (ACCEPTED 2026-11-07): Scoped M1 development GO through owner-led review and explicit risk acceptance

- **Status:** ACCEPTED 2026-11-07. Proposed 2026-11-06; decision recorded in commit `38e2d1f` (CI run 42319056546); owner attestation by Lucio Saldivar posted personally in the GitHub web UI ([comment](https://github.com/infinri/Writ/commit/38e2d1f31fb11a145cb7a109934e85b018dd1d60#commitcomment-208565071)) and verified read-only (TEST-EVIDENCE E-015). Scope: M1 development only; release and production NO-GO; 16 risks open.
- **Context:** M0 gates G1 (independent human golden review), G2 (288-rule semantic review) and G3 (live host verification, SR-06) depend on external people or on a credential and live trials. The owner intends to proceed through owner-led technical investigation, AI-assisted reviews, deterministic testing and explicit risk acceptance rather than wait indefinitely. SR-06 status at this point: SANDBOX PREPARED / LIVE VERIFICATION NOT EXECUTED.
- **Proposed:** (1) For M1 development only, replace G1 with owner-led technical review plus recorded AI-assisted reviews and risk acceptance; golden statuses stay PENDING INDEPENDENT REVIEW and no human signoff is fabricated. (2) Defer G2 to the migration milestones; no unreviewed V1 rule may become authoritative or enforced V2 policy. (3) Defer G3 to M5; SR-06 stays NOT VERIFIED and no host-enforcement claim is allowed before the planned tests. (4) A development GO permits M1 development only; release and privileged production workloads remain NO-GO. (5) The GO requires green CI, complete technical review evidence, no unresolved critical harness defect, a decided risk table and an attributable owner decision.
- **Alternatives:** wait for external reviewers (blocks indefinitely); declare M0 complete (would misrepresent unverified gates); proceed without recorded risk acceptance (loses accountability).
- **Consequences if accepted:** M1 parity rests on a provisional oracle (risk R-ORACLE); independent human review moves to the release gate; host enforcement remains unproven until M5.
- **Status note 2026-11-06:** amendment A-1 APPROVED IN PRINCIPLE by owner instruction as a development-only framework, with recommended risk dispositions from an AI-assisted technical assessment recorded in M0-GO-NO-GO (owner signature pending). D-014 stays PROPOSED until the owner's exact policy approval is attributable; the GO Decision table stays blank.
- **Status note 2026-11-06 (later):** the owner instructed adoption of A-1 and M1 development under the 16 risk dispositions. The decision commit records this in M0-GO-NO-GO as RECORDED, PENDING OWNER ATTESTATION. D-014 stays PROPOSED until (a) the owner's personal attestation is posted and verified read-only and (b) CI on the decision commit is green ([signoff package](M0-GO-SIGNOFF-PACKAGE.md) section 3). The acceptance commit then sets this entry ACCEPTED.

## D-015: Writ Control Center is a first-class product requirement and a separate management plane

- **Date:** 2026-11-06. **Decided by:** project owner (instruction in session 2026-11-06); recorded by Claude. Independent of D-014 and of the M0 development-GO signoff.
- **Context:** V2 needs a way for authorized people to understand, configure, monitor, troubleshoot and administer Writ without routine direct edits of JSON, YAML, scripts or Neo4j.
- **Selected:** add Writ Control Center ([specification](../control-center/README.md), [ADR-0007](../decisions/ADR-0007-control-center-management-plane.md)): navigation Overview, Activity & Audit, Rules & Policies, Knowledge & Memory, Integrations, Administration; separate management service with versioned authenticated APIs outside the decision path; work distributed across M2 to M10+; minimum usable Control Center for the first local beta; enterprise set separately justified; not a prerequisite for M1.
- **Consequences:** invariants INV-27 to INV-31; product requirements P-20 to P-24; CC tasks in TASKS. Status of every Control Center requirement: planned.

## D-016: Acceptance Gate Integrity is a core requirement (design proposed)

- **Date:** 2026-11-06. **Decided by:** project owner (direction in session 2026-11-06); recorded by Claude. Independent of D-014 and D-015.
- **Decision (requirement, accepted):** Writ must make authorization and acceptance requirements enforceable outside the model's reasoning, with two mandatory boundaries in Governed Execution: the Action Authorization Gate and the Acceptance and Promotion Gate. Advisory Integration and Governed Execution are distinct, labelled modes; hook behavior is never advertised as complete containment.
- **Design:** [ADR-0008](../decisions/ADR-0008-acceptance-gate-integrity.md) PROPOSED; [specification](../architecture/acceptance-gate-integrity.md). Invariants INV-32..INV-39 PROPOSED.
- **Constraint:** the decision kernel remains the only permission authority; evaluators establish satisfaction; the transition service enforces promotion. Capability Packs may define rubrics and requirements but cannot mint trusted verification.
- **Status note 2026-11-07:** ACCEPTED. Attestation verified: author `infinri` (repository OWNER), created 2026-11-07T00:44:26Z after the decision commit's CI completed (00:34:59Z), names the decision SHA and run 42319056546, scope text matches the template. Limitations: the date placeholder in the text was left unfilled (GitHub's timestamp is the date of record); a web UI comment cannot be distinguished cryptographically from an API comment under the same account, so the owner's in-session statement that it was posted personally is part of the record.
- **Status note 2026-11-07:** owner approved ADR-0008 in principle, with conditions: immutable snapshots or equivalent use-time mediation; designed vs tested vs verified guarantee levels; no adversarial bypass-resistance claim before integration and threat tests pass. Drafted in specification sections 6a and 12a and INV-40 (proposed). ADR-0008 not ACCEPTED until reviewed.
- **Status note 2026-11-07 (correction):** owner approved the immutable snapshot model and corrected section 6a: an open descriptor, file lock or single digest comparison is not an immutability guarantee. Section 6a now requires a content-addressed snapshot in agent-unwritable storage with digest verification at creation and every consumption, an ADR-justified equivalent only as an exception, and fail-closed otherwise; adversarial tests mutate the artifact from a separate process in every phase. ADR-0008 remains APPROVED IN PRINCIPLE pending review.

## D-017: M1-02 semantic decisions (strict JSON, identifiers, exact transitions, promotion permits, requirement completeness, model-assisted evaluation)

- **Date:** 2026-11-07. **Decided by:** project owner (technical review of M1-02 baseline `4a66364`, instruction in session 2026-11-07); recorded by Claude. Amends the M1 drafts only; M0 decisions D-001..D-016 unchanged except where noted.
- **Duplicate JSON keys:** rejected at every nesting level before building any map, including names equal after escape decoding. No first-wins or last-wins. (Tightens D-006 parsing; the M0 harness, which uses Python's last-wins parser, is not conformant and stays non-normative.)
- **Integer range and spellings:** revisions are nonnegative integers up to 2^53-1 in schemas, parsing, canonicalization and both prototypes; fractions, exponents, `-0`, leading zeros and out-of-range values are rejected, never converted.
- **Identifiers:** one explicit, case-sensitive representation; restricted lowercase ASCII for Writ-defined ids and state names; no automatic case folding or Unicode normalization of D-006/D-008 resource paths; internal ids distinct from display names.
- **Workflow transitions:** resource `workflow:<tenant>/<project>/<workflow_id>`; typed `from_state` and `to_state` attributes in a closed attribute set; the decision covers the exact transition (`to_states`, `from_states` statement conditions; unscoped transition permits rejected at bundle load); duplicate or conflicting representations refused; the broker independently checks stored state, requested transition, expected revision and full request fingerprint at use time.
- **Promotion permits:** bound to the complete canonical ActionRequest (D-012 fingerprint) and to the presented acceptance record (digest, id, snapshot id, artifact digest); changing actor, scope, workflow, target state, artifact digest, snapshot id, record or expected revision invalidates the permit.
- **Composite actions:** promotion-plus-effect requests are prohibited structurally (kernel and broker) while RM-01 is unresolved, not by an editable policy statement.
- **Trusted time:** the agent never supplies authoritative time; M1 uses deterministic injected clocks; production trusted clocks and rollback protection remain M3 requirements (risk 14).
- **Requirement completeness:** the acceptance check compares the record with a trusted, version-bound requirement set from the policy registry (bound to policy id, version, bundle digest), never one from the agent or the record; missing, downgraded, re-classed, substituted or extra mandatory criteria refuse. A record digest is not proof of issuance; the pure M1 checker claims no signature or provenance verification.
- **Model-assisted evaluation:** advisory only; may inform assessment or trigger human review; never establishes a mandatory pass for protected promotion (`model_assisted` cannot be a required class).
- **Also:** `expires_at` must be later than `issued_at`; revoked evaluators refuse; promotion is atomic only for Writ's workflow state, never for later external effects; external responses use `PROMOTION_REFUSED` while detailed codes stay in access-controlled audit.
- **Fixture handling:** amendment record `m1/fixtures/AMENDMENT-1.md` lists every changed expected result with its reason; all cases remain PENDING INDEPENDENT REVIEW. ADR-0008 remains APPROVED IN PRINCIPLE.

## D-018: M1-02 final semantic decisions A1 to A8

- **Date:** 2026-11-07. **Decided by:** project owner (review of amendment-1 baseline `a97e8f1`, instruction in session 2026-11-07); recorded by Claude. Applied in `m1/fixtures/AMENDMENT-2.md`.
- **A1 depth:** D-006 exactly: root depth 0, children (array elements, object member names and values) at parent plus 1, depth greater than 64 rejected; traversal depth, not container count. D-006 unchanged; boundary vectors cj-19, cj-20, cj-23..cj-34 agree with the D-006 reference module.
- **A2 protected transitions:** the authoritative workflow and acceptance policy decides which transitions are protected (M1: by target state). Protected transitions require the complete acceptance binding and broker validation; unprotected transitions still require identity, kernel authorization, revision checks and audit, and are not blocked by absent acceptance fields. Omitting attributes or taking an unprotected route cannot downgrade a protected transition.
- **A3 separate digests:** the authorization bundle digest and the acceptance-policy bundle digest are distinct, each resolved from trusted state, verified and pinned; a change to either invalidates bindings under its own lifecycle (stale permit refused; record refused on policy mismatch).
- **A4 requested capabilities:** `action.kind` is the operation identity; `requested_capabilities` lists only additional distinct capabilities, never the kind; for `workflow.transition` it must be empty.
- **A5 trust and time:** strict class matching; a trust label is not proof of evaluator identity or registration; injected trusted clock with exact boundaries; `issued_at` equal to `now` valid; future issuance, malformed timestamps and reversed validity refuse; production clock integrity, skew and rollback remain M3.
- **A6 idempotency:** refusals before durable reservation, grant consumption or dispatch leave the key reusable; a reserved key is bound to its fingerprint; interrupted or uncertain effects require reconciliation and are never treated as pre-dispatch refusals; refusals are audited and never consume grants or create effects.
- **A7 refusal information:** agents receive sanitized responses (`PROMOTION_REFUSED` for security-related acceptance and permit failures; safe request-validation codes in detail); internal codes stay with authorized auditors and test harnesses; no leak of cross-tenant existence, policy predicates, evaluator identities or resource secrets.
- **A8 composite invalidity:** structurally unsupported composite transition-plus-effect requests are INVALID and fail before any effect; a policy prohibition on a structurally valid action is DENY.
- **Strict parsing:** every command input path rejects `NaN`, `Infinity`, duplicate and escape-equivalent duplicate keys, malformed and alternate numeric spellings, unsafe integers and excessive depth; Python's permissive default parser is not normative; Rust and Go must follow identical input semantics.
- **Divergence from M0:** recorded in AMENDMENT-2; the M0 oracle is not altered.

## D-019: Final M1-02 security amendment (N-1 to N-4) and M1 prototype start

- **Date:** 2026-11-07. **Decided by:** project owner (review of revision 3 `226e4d2`, instruction in session 2026-11-07); recorded by Claude. Applied in `m1/fixtures/AMENDMENT-3.md`.
- **N-1 protection at both boundaries:** the authorization bundle carries the authoritative `workflow_protection` classification (protected states, protected edges, compatible acceptance-policy bundle digest). The kernel refuses a protected transition without the complete acceptance binding (`ACCEPTANCE_BINDING_REQUIRED`) and a binding on an unprotected one; it checks structure only, no evidence and no I/O. The broker independently checks the same version-pinned classification, permit and request binding, record, policy and requirement versions, criteria and trust, snapshot and digest, state and revision, replay and expiry, and never trusts a client-supplied flag. Missing, ambiguous or inconsistent classification fails closed. Authorization and acceptance-policy digests stay distinct with an explicit compatibility binding; neither may silently weaken the other. No second permission authority.
- **N-2 broker fixtures:** language-neutral `broker-scenario` fixtures (BROKER.md) cover sealed ingress, permits, approval grants, idempotency, crashes and reconciliation, written before prototypes and compared with M0 only after freezing. Simulated signatures, storage and crashes are labelled as such.
- **N-3 downstream effects:** promotion and external effects stay separate transactions. A governed publish or deploy requires the trusted prerequisite (workflow state and current promotion receipt) and independently verifies state, receipt, exact snapshot and digest, target (permit fingerprint), active authorization and acceptance policies, principal, approvals, revision, freshness and revocation. An accepted state for another artifact or under obsolete authorization is insufficient. The adapter consumes the verified snapshot. No claim that an external effect rolls back atomically with workflow state.
- **N-4 protected states and edges:** both supported; effective requirements are the union; destination protection applies from any source; source-specific protection only adds. Definitions are checked deterministically at load: duplicates, self edges, missing classification and any protected edge reachable by an unprotected permitted route are rejected.
- **M1 start:** once these fixtures pass integrity checks and are committed with no unresolved security-semantic contradiction, M1-03 (Rust) and M1-04 (Go) may begin under D-014, against identical fixture sets, without choosing a winner before correctness gates and reproducible benchmarks. Simulated mechanisms in prototypes are labelled simulated. ADR-0008 remains APPROVED IN PRINCIPLE.

