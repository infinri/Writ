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
