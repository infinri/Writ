# Contributing to Writ V2

Writ V2 is being rebuilt on a clean architecture while V1 remains on main. Before writing production code, read [the implementation plan](docs/implementation/MASTER-IMPLEMENTATION-PLAN.md), [architecture](docs/architecture/architecture.md), and [security invariants](docs/security/invariants.md).

## Development rules

- Treat the current V1 source as **legacy reference only**, not a template for new boundaries or hook proliferation.
- New behavior must have a stable action contract, a named decision owner and a negative test.
- A change to enforcement requires a test that attempts to bypass the intended boundary.
- Distinguish **deny** from **requires approval**; never convert an authorization denial into an approval prompt after retries.
- Do not use user/session identifiers from an untrusted text field as proof of identity.
- Reject missing or stale policy versions for privileged effects according to the applicable failure policy.
- Do not add stores, queues, services, or policy languages without a measured need and ADR.
- Use feature flags with an owner, default, expiry and removal plan; never make a privileged bypass implicit.
- All new architecture documentation is under docs/; update the HTML site when topology or contracts change.

## PR expectations

Provide: scope and milestone, design references, concrete tests, benchmark implications, failure handling, security review notes, and compatibility/rollback. Prefer small slices that can be reverted in isolation. No speculative claims of enterprise readiness or universal tool mediation.

## V1 artifacts

The old docs are accessible through main and Git history. Do not copy V1 acceptance criteria blindly; classify each as retain, redesign, discard or research before bringing it forward.

## Domain-neutral requirements

New core contracts must work for coding, research and noncode document review. Add or revise the matching Capability Pack and evaluation fixtures, including a negative test for unsupported model features and unauthorized tool effects. A provider adapter cannot bypass broker authorization, and an adapter's advertised feature list must be validated with integration tests before marking it supported.
