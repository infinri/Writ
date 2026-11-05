# Capability Pack design fixtures

These are **unsigned, inactive manifests**, used only to validate the new domain-neutral design:

- [Software Engineering](examples/software-engineering.json)
- [Research & Investigation](examples/research-investigation.json)
- [Review & Audit](examples/review-audit.json)

Manifests request symbolic tool capabilities but do not grant execution permissions. The `writ://` schema refs are proposed identifiers and have no active resolver yet. Activation later requires publisher verification, trusted evaluator implementations, artifact-schema availability and broker policy.

See [Capability Packs](../architecture/capability-packs.md), [Pack Schema](../contracts/capability-pack.schema.json) and [M0 checklist](../implementation/M0-START-HERE.md).
