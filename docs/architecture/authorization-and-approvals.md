# Authorization, policy registry and human approvals

## Four separate concepts

**Guidance:** text retrieved into model context; never enforces.  
**Validation:** checks result or intended change; may produce warnings or blockers only at a real controlled boundary.  
**Authorization:** deterministic allow/deny/approval-required decisions over trusted identity and normalized capability.  
**Execution:** mediated action performed by broker with a recorded effect receipt.

The V1 ambiguity between 'rule injected' and 'rule enforced' must not survive.

## Decision lattice

1. Invalid request/identity -> INVALID; no effect.
2. Policy bundle missing/stale/unverified -> UNAVAILABLE or DENY according to strict security profile; no privileged effect.
3. Any applicable hard prohibition -> DENY; overrides cannot turn it into approval.
4. Applicable policy that requires a human grant -> REQUIRE_APPROVAL with typed request.
5. Only explicitly allowed capability -> ALLOW with bounded permit.
6. Unknown capability -> DENY by default on governed profile.

Order and scope are part of the policy specification and independent of retry counts, UI text and host event ordering.

## Policy binding and conflict control

Each policy has: id/version, publisher/owner, scope, action selector, normalized resource constraints, precedence, advisory or enforced classification, effect/denial category, failure mode, implementation binding, tests, issue/expiry metadata, and signed bundle digest. Publishing rejects missing validators, ambiguous precedence, invalid schema or failing negative controls.

A rule that says 'mechanical enforcement' requires a registry binding to a kernel policy or broker validator with a verified executable entrypoint; a Markdown path is insufficient. Rule changes and authority promotion require a human/organization-controlled release event; agent-inferred memories cannot publish policy.

## Human approval protocol

The human approves **the exact requested operation**, not an ambient session mode or an arbitrary future command. Request includes authenticated principal/tenant, operation, target, content digest, relevant plan/effect summary, policy version, constraints, expiry and a trusted request identifier. The model can describe it but cannot mint the authorization.

Approval service verifies the human through a trusted channel, records principal and action binding, and writes a single-use (or explicitly limited-count) capability transactionally. Broker consumes it at effect time, checking target canonicalization, policy freshness, operation digest, nonce, scope and expiry. Approval is revoked or requires reapproval when any bound field materially changes.

A user replying 'approved' in a normal transcript is at most conversational context, **never** the security credential. No global 'last session' pointer and no reapplication of an approval from another project.

## Failure policy

- Strict/enterprise: deny privileged effects if kernel, policy validation, audit receipt store or authenticated identity is unavailable.
- Advisory/local: retrieval may degrade, but no false claim of enforcement. If an operator elects to run ungoverned, that is a separately displayed, audited outside-Writ mode, not a permission verdict.
- Post-action detection must never claim to prevent a completed action.

## Tests

Replay approval; forged transcript; copied token between sessions; policy update after approval; symlink path switch; file content changes after checking; duplicate request and concurrent consumption; ambiguous targets; denial repeated 10 times; untrusted subagent claiming parent identity; alternate host adapter path to same effect. See [invariants](../security/invariants.md).
