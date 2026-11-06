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

## D-014 (PROPOSED, not accepted): Scoped M1 development GO through owner-led review and explicit risk acceptance

- **Status:** PROPOSED 2026-11-06. Not accepted. Becomes ACCEPTED only when the owner records a decision in [M0-GO-NO-GO](M0-GO-NO-GO.md) under the criteria in amendment A-1.
- **Context:** M0 gates G1 (independent human golden review), G2 (288-rule semantic review) and G3 (live host verification, SR-06) depend on external people or on a credential and live trials. The owner intends to proceed through owner-led technical investigation, AI-assisted reviews, deterministic testing and explicit risk acceptance rather than wait indefinitely. SR-06 status at this point: SANDBOX PREPARED / LIVE VERIFICATION NOT EXECUTED.
- **Proposed:** (1) For M1 development only, replace G1 with owner-led technical review plus recorded AI-assisted reviews and risk acceptance; golden statuses stay PENDING INDEPENDENT REVIEW and no human signoff is fabricated. (2) Defer G2 to the migration milestones; no unreviewed V1 rule may become authoritative or enforced V2 policy. (3) Defer G3 to M5; SR-06 stays NOT VERIFIED and no host-enforcement claim is allowed before the planned tests. (4) A development GO permits M1 development only; release and privileged production workloads remain NO-GO. (5) The GO requires green CI, complete technical review evidence, no unresolved critical harness defect, a decided risk table and an attributable owner decision.
- **Alternatives:** wait for external reviewers (blocks indefinitely); declare M0 complete (would misrepresent unverified gates); proceed without recorded risk acceptance (loses accountability).
- **Consequences if accepted:** M1 parity rests on a provisional oracle (risk R-ORACLE); independent human review moves to the release gate; host enforcement remains unproven until M5.
- **Status note 2026-11-06:** amendment A-1 APPROVED IN PRINCIPLE by owner instruction as a development-only framework, with recommended risk dispositions from an independent technical review recorded in M0-GO-NO-GO (owner signature pending). D-014 stays PROPOSED until the owner's exact policy approval is attributable; the GO Decision table stays blank.
