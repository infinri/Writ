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
