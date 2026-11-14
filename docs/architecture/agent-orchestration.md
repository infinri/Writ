# Optional agent orchestration, evidence and evaluation

**Status:** target design; the deterministic kernel does not depend on an AgentRunner or a model API.

## Two operational modes

External-agent mode: Claude Code, Codex, Gemini CLI, another agent framework or an MCP-compatible host keeps its own agent loop. Writ receives authenticated host events, supplies scoped evidence and enforces only tool capabilities it actually mediates.

Writ-managed mode: an optional AgentRunner invokes a ModelAdapter, consumes model events, proposes broker-mediated actions and persists TaskEvents and artifacts. It is unprivileged relative to the kernel and the broker.

Both modes share TaskSpec, Artifact, Evidence and EvaluationRecord contracts so they can execute the same domain pack without forcing users to switch model or agent host.

## Task lifecycle

1. Intake authenticated actor, pack, goal, input schema, privacy requirements and limits.
2. Plan scoped workflow with evidence requirements and human gates.
3. Retrieve/observe sources and record provenance; treat external content as untrusted.
4. Produce typed artifacts, not just chat text.
5. Validate through registered evaluators; explicitly identify unknowns and contradictions.
6. Require authenticated human approval for governed effects or designated high-stakes final signoff.
7. Persist legitimate observations and approved memories; never auto-promote an agent claim to policy.
8. Return final artifacts, evaluation details, incomplete criteria and receipts.

## Evaluation taxonomy

| Type | Examples | Authority |
|---|---|---|
| Mechanical | Schema, test, deterministic static validator | Can prove its stated narrow property |
| Evidence-based | Citation resolution, source consistency, independent corroboration | Limited to available sources |
| Model-assisted | Review hypothesis, synthesis, usefulness | Advisory, not independently verified |
| Human | Expert approval, high-stakes interpretation | Trust derives from authenticated reviewer and defined scope |

Every validator has a versioned rubric and declared failure mode. An unavailable verification step yields incomplete/needs_review rather than automatically passing.

## Budget and safety

Per-task limits include model calls, total tokens, cost, tool calls, graph expansion, wall time and retries. Cancellation stops work not yet executed; it cannot undo an irreversible external effect. Evidence is tenant scoped. Provider fallback must respect residency and privacy. No mandatory multi-agent swarm or remote workflow engine for local tasks.

See [Model Runtime](model-agnostic-runtime.md), [Capability Packs](capability-packs.md), [security invariants](../security/invariants.md).
