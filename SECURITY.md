# Writ V2 security policy and trust model

> **Specification, not certification.** No implementation of the V2 security model has yet been released from this branch.

The most important distinction: **host hooks can refuse selected host tool calls, but do not automatically sandbox a process or control every side effect.** Writ may claim prevention only where it owns or reliably mediates the execution path.

## Trusted base

The trusted base comprises the kernel, policy evaluator, capability broker, durable approval/receipt store, host's authenticated identity binding and any configured host sandbox. Model output, prompts, transcripts, retrieved documents, shell strings and graph content are untrusted input.

## Protected capabilities

Policy evaluation applies to normalized actions (for example file.write, process.execute, network.egress, repository.publish, authority.promote) regardless of the host-specific tool that originated the request. Unsupported host operations must be explicitly disclosed; do not claim all Bash or external-process operations are mediated by a hook.

## Failures

Policy or broker uncertainty on privileged operations defaults to **deny/error**, not silent allow. Advisory context retrieval may degrade to empty/abstain without blocking work. A local personal-use mode may permit clearly labeled ungoverned fallback only if it is outside enterprise enforcement claims, enabled by a trusted operator and auditable.

## Approvals

A human approval is bound to principal, tenant, action, canonical target, content/action digest, policy version, expiry and nonce. It is durable and consumed atomically. A transcript line or an LLM claim cannot mint, expand or renew one.

## Reporting

Open a GitHub security advisory/private report when available. Avoid filing exploitable secrets or full exploit payloads in public issues. For this planning branch, refer to [threat model](docs/security/threat-model.md) and [invariants](docs/security/invariants.md). All actual security guarantees require implementation evidence and adversarial verification.
