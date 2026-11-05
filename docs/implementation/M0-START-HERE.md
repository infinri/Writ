# M0 — implementation start checklist

**Status:** executable work plan, not completed implementation. **Branch:** v2/architecture-rebuild. **No Rust/Go choice committed yet.**

## Goal

Preserve V1's useful evidence and define the cross-model, cross-domain contracts that Rust and Go must both implement. Do not port V1 shell gates directly or begin with distributed infrastructure.

## M0-01: Audit and fixtures

- [ ] Pin main's V1 SHA and export check-summed, read-only baseline evidence.
- [ ] Enumerate all 52 V1 hook command registrations with normalized capabilities, preventive/advisory coverage and failure posture.
- [ ] Classify 288 rule entries: advisory, obsolete, duplicate/conflicting, candidate enforced, unverified.
- [ ] Extract golden tests for retrieval, approval replays, cross-project isolation and effect gaps.
- [ ] Do not run graph migrations or destructive tests against non-disposable Neo4j.

Target outputs: audit coverage CSV, rule disposition CSV, V1 corpus manifest, threat cases and golden fixtures. Their absence means M0 has not passed.

## M0-02: Cross-domain contract fixtures

- [ ] Draft TaskSpec, Artifact and Evidence schemas with no mandatory file/PR fields.
- [ ] Create 3 positive tasks: code review with line-linked evidence, research with dated sources and counterevidence, noncode document review against a rubric.
- [ ] Create 3 negative tasks: fabricated citation, insufficient source evidence and unapproved tool effect.
- [ ] Two stub ModelAdapters with dissimilar streaming/tool-call features; one simulated external host.
- [ ] Assert identical policy verdicts independent of model and pack selection.

## M0-03: Security boundary and pack registry

- [ ] Trusted principal proof, broker permit and approval-grant contracts.
- [ ] Normalize file/process/network/repo actions without relying on the host's tool names.
- [ ] Validate CapabilityPack manifest against schema, signer/trust policy, required model features, semantic version and evaluator rubric.
- [ ] Prove a pack cannot authorize its requested capabilities or change tenant scope.
- [ ] Enumerate unmediated tools explicitly.

## M0-04: Benchmark/runbook handoff

- [ ] Define the exact Rust/Go M1 prototype parity suite and workload distribution.
- [ ] Include a noncoding research/review fixture alongside coding and policy decisions.
- [ ] Record hardware, compiler versions, resource budgets and results with no invented numbers.
- [ ] Choose primary runtime only after security and correctness gates pass.
- [ ] Add CI checks for JSON schemas, examples, Markdown/HTML links, plus the new fixture suite.

## First PR definition of done

- [ ] Documentation checker passes in CI.
- [ ] Draft contract schemas and all pack examples validate.
- [ ] Research and Review tasks require no repository path or source-code phase.
- [ ] Model output cannot mint approval; external host calls need no ModelAdapter.
- [ ] Findings and every benchmark distinguish planned vs actually executed.

**Start with M0-01 and M0-02**, not a full platform rewrite. [Master plan](MASTER-IMPLEMENTATION-PLAN.md).
