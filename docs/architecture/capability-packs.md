# Domain-independent Capability Packs

**Status:** DECIDED interface; pack activation and executable modules are not implemented.

Coding is a first use case, not the platform's identity. A **Capability Pack** is a versioned, scoped and eventually signed declaration of task types, workflows, tool requirements, artifact schemas, evaluation rubrics, retrieval/evidence strategies and memory rules. Packs configure core services; they never fork the kernel or grant permissions.

## Manifest

- Identity: pack_id, version, publisher, signature reference, domain, compatibility range.
- Tasks and workflows: typed tasks, required inputs, ordered/branching steps, review and completion gates.
- Tool requirements: symbolic capability names; broker independently authorizes them.
- Artifacts: output types and schema references, provenance and scope.
- Evaluation: rubric IDs/versions and mechanical, source-based, model-assisted or human evaluators.
- Retrieval/Recall: evidence expectations, graph scope, allowed memory record types, retention.
- Models: optional required features, never a hard-coded model provider.
- Limits: duration, tool calls, model calls, token/cost budget and cancellation policy.

The pack manifest itself is untrusted data until verified against schema, publisher policy and dependencies; a valid manifest still cannot authorize effects. Optional executable plugin code requires a separately approved sandbox or sidecar trust tier.

## Reference packs

| Pack | Example tasks | Artifacts | Evidence |
|---|---|---|---|
| Software Engineering | Code investigation, PR review, implementation validation | Findings, patch, tests, change plan | Diff/line references, test evidence |
| Research & Investigation | Source discovery, verification, counterevidence, synthesis | Cited report, claim ledger | Dated sources, claim provenance, uncertainty |
| Review & Audit | Document review, rubric assessment, compliance issue spotting | Findings, disposition, rubric result | Exact document locations, verification and human signoff |
| Business Operations | Data intake, reconciliation, approvals, workflows | Operational records, receipts | Source systems, authorization and quality checks |

Research and Review are **not restricted to repositories or coding**. Higher-risk domain packs require domain-specific safeguards and qualified human review.

## Workflow and evaluation

~~~text
PackRegistry.resolve(pack_id, version, scope) -> VerifiedPack
TaskPlanner.instantiate(pack, task_input, actor) -> TaskSpec
AgentRunner.execute(task, model_or_host, budget) -> TaskEvents
Evaluator.verify(artifact, rubric, evidence) -> EvaluationRecord
MemoryPort.record(verified_observation, consent_and_scope) -> MemoryReceipt
~~~

An evaluator distinguishes: deterministic checks, source-backed evidence, model-assisted hypotheses and authenticated human signoff. A model labeling a finding "verified" does not make it independently verified. Unknown or contradictory evidence must be surfaced, not silently incorporated into durable Recall as fact.

Packs may compose, but must declare dependency/version and artifact conflicts; security policy is not last-writer-wins. A pack cannot substitute a workflow phase for an approval grant.

## M0 demonstration scenarios

1. **Coding:** inspect a diff and produce source-linked review findings.
2. **Research:** investigate an unfamiliar question, cross-check sources, record counterevidence and return a cited synthesis with uncertainty.
3. **Document Review:** evaluate a noncode document against a versioned rubric with exact locations and human-signoff needs.

All three must be representable in identical task/evidence contracts and runnable by stub models without external credentials. Example manifests are [here](../packs/README.md); the proposed contract is [CapabilityPack](../contracts/capability-pack.schema.json).

## Security and release criteria

Pack must be signed/verified before execution; unsupported requirements reject activation. A pack requesting network access does not grant network access. Memory reads/writes are scoped and consent-aware. Tests must prove forged citations, missing evidence, conflicting sources, cross-tenant data and unauthorized tool calls cannot be silently accepted.

Executable WASM packs and distributed orchestrators are deferred until the capability/broker contracts are stable.
