# Writ V2 implementation decision log

Implementation-level decisions. Architectural decisions belong in [ADRs](../decisions/). Do not reverse an entry without new evidence; add a superseding entry instead.

## ENT-OWNER-01: Enterprise security and assurance planning direction

- **Date:** 2026-11-10 (America/Chicago). **Authority:** owner delegated the readiness approach in session after discussing Rust, PostgreSQL and SOC 2. This is planning authorization, not an audit opinion or release approval.
- **Direction:** Rust remains the provisional leader and preferred kernel/broker direction; ADR-0002 stays OPEN until targeted M1-08 evidence and final owner decision. PostgreSQL targets enterprise state with its own future concurrency/recovery gate; SQLite stays local/offline. Preserve current M1 verification and benchmark evidence rather than restarting the bake-off.
- **Readiness:** adopt [ENTERPRISE-READINESS](../security/ENTERPRISE-READINESS.md), ENT-R01..R09 and tasks ENT-00..ENT-08. Design controls into existing milestones and retain actual operational evidence. Self-hosted customers own their deployment controls and examination scope; Writ owns product/release evidence and any controls or services it operates.
- **Assurance scope:** do not label software Type I/II merely because controls are designed. Before a hosted/managed enterprise offering, scope the service, assign operators and obtain CPA/customer input on examination and assurance milestones. Type II for our service is not transferred to customers. No spending, auditor engagement or compliance claim is authorized here.
- **Unchanged gates:** ADR-0008 APPROVED IN PRINCIPLE; production/release NO-GO; independent review and real-host obligations remain. ENT control implementation, operational effectiveness and independent examination are not complete.

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

## D-020: External refusal mapping for request and state errors; transition support versus malformed protection

- **Date:** 2026-11-07. **Decided by:** project owner (review of the M1-03 and M1-04 results, instruction in session 2026-11-07); recorded by Claude. Applied in `m1/fixtures/AMENDMENT-4.md`.
- **Refusal mapping:** `REQUEST_SCHEMA_INVALID` and `DEADLINE_EXCEEDED` pass through (safe diagnostics about the caller's own request). `STATE_REVISION_CONFLICT` maps to `PROMOTION_REFUSED`; for consistency the broker's `REVISION_CONFLICT` changes from pass-through to `PROMOTION_REFUSED`. Unknown codes default to `PROMOTION_REFUSED`. No external message, field or timing assumption may reveal protected state, resource existence or cross-tenant information; detailed reasons stay in scoped audit.
- **Missing protection classification:** a bundle declaring `workflow.transition` support must carry a valid protection configuration, else it is rejected at load (`POLICY_BUNDLE_REJECTED`). A bundle without transition support stays valid for other capabilities; transition requests against it are denied as unsupported. Valid support and protection with no authorizing rule is `DEFAULT_DENY`. Protection metadata unavailable at execution time fails closed with an internal protection-unavailable condition. Malformed configuration is never reported as ordinary denial.
- **Interpretation recorded by Claude (for owner confirmation):** "declares support" means any statement covers `workflow.transition` or `workflow_protection` is present. Listing `workflow.transition` only in `known_capabilities` (as the M0 reference bundle used by the 26 frozen golden cases does) is not a declaration of support; such a bundle stays valid and transition requests are `deny` / `UNSUPPORTED_CAPABILITY`. The literal alternative (known capability implies support) would reject the M0 reference bundle and change all 26 frozen golden outcomes, which needs a separate decision.
- **Also:** the broker model gains `restart_broker` (ledger persists across a simulated restart) to test replay after restart.
- **M1-05 authorized:** property, metamorphic, concurrency and differential testing with recorded seeds; no oracle built from either prototype.

## D-021: Strict protocol response semantics

- **Date:** 2026-11-07. **Decided by:** project owner (instruction in session 2026-11-07); recorded by Claude. Normative text: `docs/implementation/M1-PARITY-SUITE.md`, "Protocol semantics (D-021)"; fixtures: `protocol-semantics` set.
- **Decisions:** (1) a decide strict-parse failure returns exactly `status` invalid, `reason_code` REQUEST_JSON_INVALID, `matched_rule_ids` [], with no placeholder fields; (2) malformed envelopes return `ENVELOPE_INVALID`, preserving the id only when recovered from a strictly parsed envelope with exactly one string `id`, otherwise `id` null; never a fabricated identity; (3) unusable scenario input returns `INPUT_MALFORMED` with no state change, grant use or effect, never a crash; (4) unknown operations return `OPERATION_UNKNOWN`; (5) reused grant references return `GRANT_REF_IN_USE` and never reset or replenish the grant; (6) never-issued permits return `PERMIT_MISSING`; (7) JSON member order is never significant; (8) envelopes are closed structures, unexpected fields are `ENVELOPE_INVALID`; (9) unknown or undeclared dependency names are `INPUT_MALFORMED`.
- **Conflict recorded and resolved:** the three-field failed-parse response is not a `decision.schema.json` instance; it is defined as a protocol refusal. Valid-input decisions keep the Decision contract.
- **Independence:** the Go prototype's earlier alignment with Rust on items 8 and 9 counts as agreement only; tests derive from this decision.


## D-022: Decide input validation before any Decision

- **Date:** 2026-11-07. **Decided by:** project owner (instruction in session 2026-11-07, resolving E-026 finding 5); recorded by Claude. Normative text: `docs/implementation/M1-PARITY-SUITE.md`, "Decide input validation (D-022)"; fixtures: `protocol-decide` cases `p22-*` ([AMENDMENT-6](../../m1/fixtures/AMENDMENT-6.md)).
- **Decision:** a `decide` input that cannot safely produce a valid Decision is answered with exactly `{"status": "invalid", "reason_code": <code>, "matched_rule_ids": []}`: no timestamps, zero dates, placeholder policy hashes, request ids or other invented metadata. Raw text that fails D-017 strict parsing is `REQUEST_JSON_INVALID` (D-021); strictly valid JSON is `REQUEST_SCHEMA_INVALID` when the input is not an object, `request` is absent, `now` is absent or not the strict form, or `state_revision` is absent, not an integer, or outside 0 to 2^53-1.
- **`now`:** exactly `YYYY-MM-DDTHH:MM:SSZ` with a valid proleptic Gregorian calendar time (years 0000 to 9999, hour at most 23, minute and second at most 59; no leap second, offset, fraction or lowercase `t`/`z`), the same form as acceptance timestamps. `now` is test-harness-supplied trusted state in the M1 protocol; it never authorizes an untrusted agent to supply production time.
- **`state_revision`:** an integer from 0 to 2^53-1 (D-017). Negative values, including -1, are `REQUEST_SCHEMA_INVALID`. Values beyond 2^53-1 and alternate spellings (`7.0`, `7e0`, `07`, `-0`) fail D-017 strict parsing first and are therefore `REQUEST_JSON_INVALID` with the same three-field shape (interpretation recorded by Claude: under D-017 they are not syntactically valid JSON).
- **Unchanged:** a present but malformed `request` still yields a complete Decision contract instance (existing fixtures, e.g. `missing-principal`, `tx-35-revision-negative`); the distinction between pre-decision refusals and Decision instances is preserved.

## D-023: SQLite-backed ledger required for official M1-06 broker measurements

- **Date:** 2026-11-07. **Decided by:** project owner (instruction in session 2026-11-07, resolving E-026 item 6); recorded by Claude.
- **Decision:** implement SQLite-backed ledger semantics in both prototypes before official M1-06 workload B measurements, as ADR-0002 and the parity protocol require. Same database schema and logical transactions in both; real file-backed databases; equivalent WAL, synchronous, busy-timeout and transaction settings fixed and documented before measuring; `BEGIN IMMEDIATE`-equivalent atomic reservation and approval consumption; exact request-fingerprint and idempotency bindings; persisted approval use, effect intent, status and reconciliation records; restart tested with a new process or connection on the same database; an uncertain effect is never re-dispatched because of a restart; concurrent approvals and reservations tested under real transaction contention; the effect adapter stays simulated (no claim of real external exactly-once execution); fresh separate database files per run; engine version, driver, dependency versions, build configuration and transaction settings recorded; only necessary dependencies, with licence, maintenance and transitive-footprint review. Same requirements for both candidates; no transaction model chosen for speed.
- **Kept:** the in-memory ledger remains for isolated kernel or simulation measurements and is never a substitute for the SQLite workload. Measure separately: in-process kernel latency; adapter and protocol overhead; broker authorization and transaction latency; SQLite-backed concurrent workload; recovery behaviour and correctness; memory, startup and binary size.

## D-024: Build, platform, storage-failure and cleanup decisions (owner, 2026-11-10)

- **Decided by:** project owner (instruction in session 2026-11-10); recorded by Claude.
- **Decisions:** (1) D-022 behaviour approved as reported. (2) Go release and benchmark builds use `-trimpath`, with reproducibility independently verified (the benchmark guard refuses a Go binary without `-trimpath=true` and the D-023 tags in its build info). (3) M1 targets Linux x86_64; cross-compiler installation and other platforms are deferred. (4) SQLite storage failures fail closed with consistent, sanitized external responses; language-neutral tests added. Normative semantics written by Claude from this instruction: LEDGER-SQLITE.md "Storage failures" (open or initialization failure answers the whole scenario `{"error": "LEDGER_UNAVAILABLE"}` with no step run and no file modified; an operation failure rolls back that transaction and refuses the operation `LEDGER_UNAVAILABLE` while earlier commits stay; stdout carries codes only; external mapping `PROMOTION_REFUSED`). (5) Remove Go `ReadID` after confirming it is unused (confirmed 2026-11-10: only its own test referenced it; no interface). (6) The dependency-default question and assumptions C-1..C-6 stay open with exact wording and proposed resolutions prepared. (7) The independent fixture-review requirement is preserved.

## D-025: M1 open assumptions resolved; timing-related behaviour approved; SQLite corrections (owner, 2026-11-10)

- **Decided by:** project owner (instruction in session 2026-11-10); recorded by Claude. Background: [M1-OPEN-ASSUMPTIONS](M1-OPEN-ASSUMPTIONS.md). Normative text: BROKER.md and LEDGER-SQLITE.md as amended 2026-11-10; fixtures AMENDMENT-7.
- **Approved behaviour (M1-06d):** (A) a write-free idempotent retry transaction may end with `COMMIT`, provided it makes no write, no additional grant use and no dispatch (tests prove durable state unchanged). (B) Permit verification runs inside the adapter transaction; if that transaction cannot begin, `LEDGER_UNAVAILABLE` takes precedence over an invalid permit; execution stays fail-closed and a permit is fully verified before any effect is applied (precedence documented and tested in both prototypes).
- **DEP-1:** a declared broker dependency (`policy`, `ledger`) missing from `initial.dependencies` is `INPUT_MALFORMED`; never a default of available.
- **C-1:** generic external security refusals kept (`PROMOTION_REFUSED`); protected-policy details never revealed.
- **C-2:** in protected-edge bypass analysis a statement without `from_states` may match any source state; conservative bundle validation preserved.
- **C-3:** a durable `reserved` receipt resumes without another grant use only when dispatch has definitely not begun; the `dispatched` marker commits durably before the effect adapter is invoked; a dispatched or ambiguous effect requires reconciliation.
- **C-4:** an unparsable or schema-invalid stored grant is a ledger-integrity failure: refused with the internal `LEDGER_UNAVAILABLE` behaviour, external response sanitized, with direct corruption tests.
- **C-5:** M1 broker comparison with M0 stays by test correspondence; documented divergences preserved.
- **C-6:** serial-equivalent aggregate concurrency outcomes are the M1 fixture criterion; no claim of full linearizability, process-kill safety or power-loss durability.
- **SQLite corrections:** S-1 validate an existing ledger's schema and metadata before changing its WAL mode or any other persistent setting; S-2 remove a database file the run itself created when initialization fails, never a pre-existing file or another process's file; busy timeout stays 5 s; remaining fault-injection limits documented; stderr diagnostics must not reveal database paths or credentials.

## D-026: SQLite lock contention and concurrency correctness (owner, 2026-11-10)

- **Decided by:** project owner (instruction in session 2026-11-10, option A of E-034 with additional requirements); recorded by Claude. Normative text: LEDGER-SQLITE.md "Contention and the concurrency oracle (D-026)" and the `error` member of timing transactions; checker: `m1/props/wprops/c_broker.py`.
- **Unchanged:** busy timeout 5 s; the 64-way contention campaign stays required; no weakened concurrency, longer timeout or suppressed failure.
- **Decision:** a lock-contention refusal is an allowed fail-closed outcome only when verified: the operation's first transaction failed at `BEGIN IMMEDIATE` with SQLite busy (busy timeout); it acquired no reservation, consumed no grant, consumed no permit and applied no effect; no unintended change to authorization or effect state; a separate refusal-audit entry is allowed. Such an operation is an aborted no-op in the concurrency model. Strict serial equivalence is retained for every successful and committed operation; `LEDGER_UNAVAILABLE` is not generally a harmless no-op. A failure after an earlier transaction of the same operation committed is checked against the persisted receipt, grant and effect state under the existing crash-recovery semantics; such an operation is never excluded from the state model.
- **Checker categories:** (1) successful; (2) permitted ordinary refusal; (3) verified lock-contention abort with no governed state change (only category allowed the aborted-no-op exception); (4) failure after partial durable progress (modelled as the equivalent crash state); (5) unexpected storage or integrity failure (always a failed check). Verification uses operation-specific evidence (each operation's transaction outcomes and, where the idempotency key is the operation's own, its rows in the ledger file), not a global before/after comparison; the final state must still equal a serial order of the modelled operations.
- **Availability evidence:** timeout refusals are reported, never hidden: per prototype and run, total operations, successes, lock-timeout refusals by transaction stage, other storage refusals, timeout percentage, concurrency level, lock-wait distribution and maximum, engine and busy-timeout settings, seed and environment. The preliminary 89 (Rust) versus 6 (Go) timeouts in 12,800 operations each (E-034) stay recorded; the cause is investigated without tuning either prototype to erase it; one timing-dependent sample is not a language result. Official benchmarks report successful throughput and refusal rates separately; refusals never count as work. Correctness and availability are separate evaluation dimensions.

## D-027: Standardized SQLite concurrent-worker start (owner, 2026-11-10) — IMPLEMENTED (`52b31fe`, refined by D-028 in `c5effec`; CI green E-044)

- **Decided by:** project owner (instruction in session 2026-11-10, resolving the E-035 finding); recorded by Claude. **Status: APPROVED; IMPLEMENTED LOCALLY** in `52b31fe` (spec amendment `m1-ledger-1a`, `m1/ledger_tests/test_d027.py`, both prototypes, harness; E-038). Published 2026-11-10 (HEAD `41529d1`); CI run 42915265108 failed on a Rust own-code test that hit the concurrent reconcile interleaving (E-039); not marked IMPLEMENTED.
- **Primary SQLite concurrency profile (both prototypes):** for a `concurrent` group, open, configure (D-025 connection settings and `journal_mode=WAL`, `synchronous=FULL` read back) and verify every dedicated worker connection **before** releasing the common start barrier; release the barrier only when all are ready; then start the operations concurrently. No write transaction may begin before the barrier is released. Connection setup is then outside each operation's measured `total_ns` and is reported separately.
- **Supplemental cold-connection profile:** a separate, explicitly selected profile creates each worker connection **after** barrier release (connection creation measured inside the operation), reported separately from the primary profile.
- **Unchanged:** busy timeout 5 s; 64-way stress tests; D-026 fail-closed timeout checks.
- **Earlier evidence:** timeout counts before D-027 (E-034: Rust 89, Go 6 of 12,800; E-035 diagnostic; E-036 CI: Rust 2 of 1,408) stay recorded, labelled **preliminary and not directly comparable**.
- **Correction recorded by Claude (2026-11-10, after the decision):** the premise given to the owner (E-035: "Rust opens worker connections before the barrier, Go after it") was wrong. Both prototypes already open worker connections before release (Go sequentially in one goroutine, Rust in parallel in the worker threads) and both count setup inside `total_ns`; they differ in how release happens (Rust `std::sync::Barrier` over OS threads, Go `close(start)` over goroutines). Consequence for implementation: the D-027 work is verifying each worker connection before release, excluding setup from `total_ns` and reporting it separately, adding the cold-connection profile, and the spec and tests that prove no write before release. **Open for the owner:** whether release simultaneity must also be made equivalent (and how), since that, not connection order, is the remaining candidate explanation (unverified).

## D-028: Equivalent concurrency release protocol (owner, 2026-11-10) — IMPLEMENTED (`c5effec`; CI green E-044)

- **Decided by:** project owner (instruction in session 2026-11-10, answering the D-027 open question on release simultaneity); recorded by Claude. **Status: APPROVED; NOT IMPLEMENTED** (mark implemented only when code, tests and CI exist).
- **Logical start conditions (both prototypes, every `concurrent` group):** create every worker thread or goroutine before release; in the primary profile each worker opens, configures and verifies its own dedicated SQLite connection, concurrently with the other workers, before reporting ready (standardizes D-027 connection preparation: no sequential setup in a coordinator); each worker reaches a ready state and waits at a common start gate; the gate is released only after every worker is ready; governed operations begin only after release; record per-worker release-to-execution delay and group start skew. Synchronization primitive is each language's own; identical OS scheduling is neither possible nor required. Go: goroutines must exist and be waiting at the gate before release (never launched one by one after the signal).
- **Cold profile:** workers still wait at the common gate; each opens and configures its connection only after release.
- **Timing:** connection-preparation wall time recorded separately; pre-barrier setup never in primary operation latency; startup timing kept as a separate engineering metric.
- **Required tests:** every worker exists before release; every worker reaches the gate; every primary connection is fully configured before release; no governed operation begins before release; no write transaction held across the barrier; connection failures do not deadlock the group; actual start delay captured; transaction correctness and failure handling unchanged.
- **Unchanged:** D-026 timeout classification; 64-way contention coverage; 5 s busy timeout; no tuning away of legitimate contention, no reduced concurrency, no hidden timeout refusals. Historical availability results (E-034..E-038) stay recorded as preliminary evidence under different start conditions.
- **Benchmarks:** no official M1-06 or M1-07 timing until the full-power charger is connected and the battery is not discharging, the governor is `performance`, load and temperature pass the guards, D-028 is implemented and verified, and required CI is green.

## D-029: Reconciliation coordination gate, state-conflict diagnostics, D-026 contention test acceptance (owner, 2026-11-10) — IMPLEMENTED (`c5effec`; CI green E-044; process-local only)

- **Decided by:** project owner (decisions A and B in session 2026-11-10, after E-039 and E-040); recorded by Claude.
- **A. Coordination gate (process-local):** a broker-level shared/exclusive gate. `execute` (and `present_permit`, which reaches the adapter) hold it shared from before the protected lifecycle (before the per-key lock) through reserve, dispatch, adapter and observe until the operation returns; `reconcile` holds it exclusive before scanning or updating `dispatched` receipts. Independent executions stay concurrent; no global exclusive lock on ordinary executions. Lock order: gate, per-key lock, SQLite transaction; never wait for the gate while holding a database write transaction; locks released on failure and crash paths. SQLite transaction boundaries, fail-closed behaviour and post-return reconciliation semantics unchanged. **Limitation (must stay explicit):** the gate does not protect against a `reconcile` in another process on the same file; no multi-process reconciliation safety is claimed without durable database-level leases or fencing; a cross-process demonstration test records what is and is not protected.
- **SQLITE_CORRUPT investigation (E-041):** the CI error was a synthetic code from a zero-row receipt transition (Rust `SQLITE_CORRUPT`, Go `io`), not file corruption (`integrity_check` ok). Corrected classification: application `STATE_CONFLICT` on stderr, still fail closed `LEDGER_UNAVAILABLE`, D-026 category 5. Genuine SQLite corruption is never relabelled.
- **B. D-026 contention tests:** each prototype's 64-way tests accept `LEDGER_UNAVAILABLE` only for a proven initial-transaction busy abort (first transaction `busy` at `begin`; no grant use, receipt, dispatch, consumed permit or effect from that operation; only the refusal audit may be recorded), keep the original invariants, fail on any other error, and add negative tests showing the rule cannot hide an unauthorized effect or committed receipt. A timeout after durable progress stays in the recovery-state model. Busy timeout 5 s and 64-way concurrency unchanged; genuine timeouts recorded as availability.
- Specification: LEDGER-SQLITE.md amendment `m1-ledger-1c`; tests `m1/ledger_tests/test_d029.py`.

## D-030: Rust selected for the trusted kernel and broker; ADR-0002 ACCEPTED (owner, 2026-11-12)

- **Decided by:** project owner (selection of Rust conditional on verified published evidence, CI and a bounded correction of the process-concurrency workload); recorded by Claude after those conditions were met (E-054, E-055).
- **Decision:** [ADR-0002](../decisions/ADR-0002-runtime-bakeoff.md) ACCEPTED: Rust for the tested trusted kernel and broker. The Go prototype and benchmark evidence are kept as comparison material; no second production core.
- **Not decided:** PostgreSQL performance or design, Neo4j, orchestration, adapters, Control Center technologies; no production security certification. ADR-0008 APPROVED IN PRINCIPLE; release and production NO-GO.
- **Carried risks:** cross-process reconciliation (M3, M10), real-host enforcement (M5, SR-06), power-loss durability (M9), single-host measurements and no throttle counter (M10).

## D-031: Kernel readings for unspecified inputs (owner, 2026-11-12)

- **Decided by:** project owner (approval of the three M2 readings, with limits); recorded by Claude. Normative for `writ-kernel`; tested in `kernel/tests/readings.rs` without changing frozen fixture expectations.
- **1. `current.revoked_evaluator_ids`:** every entry must be a valid evaluator name (`^[a-z][a-z0-9_-]{0,63}$`); an array with any malformed entry (empty, wrong case or leading digit, spaces, over 64 characters, a non-string), or a value that is not an array, is `CURRENT_STATE_MALFORMED`.
- **2. `external-refusal`:** input that fails strict parsing keeps the documented response `{"input_error": "INPUT_JSON_INVALID", "json_error": <code>}`; a missing, non-string or unknown `internal_code` maps to `PROMOTION_REFUSED` (no disclosure).
- **3. Extra members:** ignored **only** at the top level of the `decide` and `check-acceptance` command wrappers; ignored values must still pass strict JSON parsing (a duplicate key or non-integer number anywhere in the input is the command's strict-parse refusal). Closed envelopes (D-021) and every nested contract (request, bundle, record, current state, requirements) remain strict.
- **Protection reachability (recorded with D-031):** `transition::check_transition` with no protection classification returns `PROTECTION_UNAVAILABLE` through the public library API; through validated command inputs it is unreachable, because a bundle whose statements cover `workflow.transition` without `workflow_protection` is rejected at load time and `decide` reaches the transition check only for bundles that carry a classification.

## D-032: M3-01 ownership code, acceptance-contract carry-over (owner, 2026-11-12)

- **Decided by:** project owner (review of the M3-01 checkpoint); recorded by Claude.
- **Ownership refusal:** `LEDGER_UNAVAILABLE` stays the only external broker and storage code; `OWNED_ELSEWHERE` (and `AMBIGUOUS_IDENTITY`) are sanitized operator diagnostics only; the agent-facing refusal mapping is unchanged.
- **Scope amendment:** the accepted M2 delivered its documented kernel scope but omitted the acceptance-gate obligations assigned to M2 by acceptance-gate-integrity section 13 (evaluator registry entry, runner attestation and EvaluationRecord scope contracts; protected-state machine; INV-32..INV-40 contract tests). They are open as M3-06 and are not described as completed; consumers that depend on them (M3-05 evaluator and attestation checks, M3-07) wait for review of [M3-ACCEPTANCE-CONTRACTS](M3-ACCEPTANCE-CONTRACTS.md).
- **Dependency policy:** a release-age preference never delays a necessary security fix; a fix younger than 14 days is adopted under a documented review exception.
- **Recorded deviation for review (Claude, M3-01):** the production SQLite profile refuses existing ledgers with any schema object beyond the exact expected set (tables, indexes, triggers, views), unlike the M1 rule "extra schema objects are not an error" (LEDGER-SQLITE.md Storage failures rule 1), because an extra trigger could rewrite ledger state. Consequence: M1 trigger-based fault-injection tests cannot be reused unchanged against the production broker (M3-08 uses a fault-injection mechanism instead, S-7).

## D-033: M3-02 dependencies, input strictness, stale permits, trusted-time direction, contract amendments, schema strictness (owner, 2026-11-12)

- **Decided by:** project owner (review of the held M3-02 core); recorded by Claude.
- **1. Dependencies and strictness:** BSD-3-Clause approved for `subtle` 2.6.1 only (package-specific approval in `broker/deps-allowlist.json`; notice and disclaimer in `THIRD-PARTY-NOTICES.md`; licence recorded in the SBOM). Exact-field validation of seals and permits and a 32-byte minimum key length approved; length does not establish key quality: production keys come only from the trusted key provider, with separate purposes for ingress, permits and runner authentication; test keys stay test-only. The dependency gate keeps both views: each crate's standalone closure, and the effective graph of the supported combined release (normal, build) and test (plus dev) builds; the kernel's "10 packages" is its standalone closure, not a claim that combined builds compile only those (in a combined build feature unification adds `subtle` to the shared `digest`).
- **2. Stale permits:** a permit bound to a bundle digest other than the currently active one is refused with internal code `PERMIT_STALE` (agent-facing mapping unchanged: generic `PROMOTION_REFUSED`). After strict parsing the order is fingerprint, MAC, expiry, then the active-bundle check, before any consumption or effect; missing or unverifiable active policy fails closed. Policy activation is coordinated with use-time authorization and dispatch so a policy change cannot slip between the check and effect authorization. **Production difference from M1:** the M1 `BROKER.md` adapter order had no active-bundle check; frozen M1 expectations are unchanged and no M1 fixture presents a permit after `activate_bundle`.
- **3. Trusted time (direction approved, mechanism to be revised):** broker-owned host clock for the local profile with a persisted high-water mark; agent-supplied time never authoritative. Required revisions: `max(clock, high_water)` alone is not sufficient (it can freeze time during rollback); monotonic elapsed-time accounting within a running broker with defined suspend behaviour; a detected rollback must not pause existing lifetimes; 120 s is a diagnostic threshold, never permission to extend; fail closed where trustworthy expiry cannot be established; forward-jump detection against an elapsed-time baseline with explicit startup and restart behaviour; the time floor that established an expiry persists even when the governed operation is refused or rolled back; acknowledgement is authenticated, authorized and audited and never lowers the watermark or revives credentials; residual dependence on host-clock integrity across downtime documented.
- **4. Acceptance contracts (approved with amendments):** explicit registered command identity and digest; closed versioned field types; exact signed or MACed bytes with purpose and domain separation, encoding, algorithm policy and key resolution (unknown algorithms or keys fail closed; HMAC described as a MAC, not a signature); attestation bound to the exact criterion and acceptance context (tenant, project, task, workflow, transition, snapshot and digest, canonical target, acceptance policy, state revision) and retaining evaluator implementation, version, environment, evidence and output digests; ledger-wide single consumption per attestation id, atomic with the protected promotion, identical retry idempotent, use for another record or criterion refused, verification alone never consumes; registry scope defaults to an explicit tenant and project (global marker deferred); INV-40 corrected: governed consumers use one service-controlled immutable snapshot, otherwise `ARTIFACT_INTEGRITY_UNAVAILABLE` (no interim mutable-path shortcut); amended schemas, examples and negative tests for review before M3-05 and M3-07 integration; human mandatory criteria stay unavailable until authenticated signoff exists.
- **5. Schema strictness and fault tests:** rejecting extra schema objects (including triggers) approved; production fault tests use test-only injection at the storage boundary, never weakened schema validation.
