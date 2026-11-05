# V1 baseline, verified observations and disposition

**Source revision:** main ebdafe5450b201339f81e717ed104e6bcfd3fe3e (2026-11-05 audit). This page records evidence, not a completed line-by-line runtime certification.

## Observed inventory

- 1,139 tracked files in the source tree, with approximately 647 under tests/, 146 under writ/, 150 under docs/.
- 52 hook command registrations across 12 Claude Code lifecycle events in hooks/hooks.json.
- Large central files include hooks/scripts/writ-bash-write-gate.sh (~215 KB), bin/lib/common.sh (~148 KB), writ/cli.py (~155 KB), writ/session/gates.py (~67 KB) and writ/retrieval/pipeline.py (~70 KB).
- Shipped graph corpus contains 288 Rule entries and 32 mandatory flags. Mechanical-path metadata has historical/stale references, including old .claude/hooks/... locations.
- V1 architecture: FastAPI and local transport, shell hooks, file session state, Neo4j canonical graph, hybrid vector/keyword retrieval and multiple authorization/fallback paths.
- Prior CI jobs passed on inspected revision; this is not proof of complete mediation or independent local tests.

## Architectural findings

| ID | Finding | Evidence | Disposition |
|---|---|---|---|
| A-01 | Repeated write denials may become 'ask' | writ/server/routes/gate.py pre_write_check | REDESIGN decision classes and precedence |
| A-02 | Default daemon+fallback outage can allow | bin/lib/common.sh; tests/test_gates_fail_open.py | REPLACE for strict governed execution; separate advisory profile |
| A-03 | Shell command parser acknowledges bypass limits | hooks/scripts/writ-bash-write-gate.sh; docs/reference/architecture.md on main | REPLACE with broker/sandbox boundary |
| A-04 | Some safeguards run only for Write, not Edit/NotebookEdit | hooks/hooks.json registration filters | REPLACE with normalized capability selectors |
| A-05 | PostToolUse validator exit 1 is nonblocking | hooks/scripts/validate-handoff.sh; historic docs/reference/session-and-gates.md | RENAME as advisory or move to pre-effect validator |
| A-06 | Approval evidence relies partly on editable transcript | historic docs/adr/ADR-approval-integrity.md | REPLACE with authenticated human approval |
| A-07 | Rule metadata references historic paths | writ-corpus.cypher mechanical_enforcement_path | IMPORT as unverified until rebound |
| A-08 | Single project tag previously treated doctrine as globally visible | historic ADR session-and-project-isolation | REDESIGN explicit scope and tenant labels |
| A-09 | Retrieval pipeline uses ONNX/HNSW/Tantivy and benchmarks | writ/retrieval/pipeline.py; SCALE_BENCHMARK_RESULTS.md on main | BENCHMARK BEFORE PORT |
| A-10 | V1 fixes and tests encode valuable failure lessons | tests/, ADRs on main | MINE GOLDEN FIXTURES, not literal port |

## Preserve

Knowledge corpus with provenance, ranking/golden queries, useful decision-memory records, approvals as a **domain requirement** (not their file token protocol), negative and differential tests, idempotency/race test data, graph integrity tests, and detailed observed failure cases.

## Rebuild

Execution authority, policy registry and rule lifecycle, host adapter normalization, authorization and approval flow, local ledger, graph schema isolation, retrieval-to-enforcement split, observability contracts, installation/deployment, and executable extension API.

## Discard / archive on main

Shell command interception as a claim of general security, redundant host-specific hook scripts, implicit global session pointers, historic generated V1 docs as normative design, fragile line/filename enforcement references, ambient fallback allows in enterprise mode, mutable approvals inferred from natural language, and automatic policy power from context text.

## Evidence limitations

This baseline came from repository inspection and prior documented tests. It does not assert that every rule and source line was audited, or that all bypasses are exploitable on the current host. Before M0 concludes, export a complete 52-registration-to-action matrix, manually inspect all 288 rule semantics and run isolated host-level negative tests. Preserve source links to the original commit in the resulting report.
