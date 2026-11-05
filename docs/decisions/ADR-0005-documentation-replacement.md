# ADR-0005: Replace the entire active V1 documentation set

**Status:** ACCEPTED · 2026-11-05

## Context

V1 documentation was distributed among more than 150 files in docs/, including legacy ADRs, generated reference text, pressure-run artifacts, an HTML visualization with a large generated graph payload, and several contradictory or stale enforcement claims. Keeping them beside a clean V2 design would create two competing sources of truth.

## Decision

On the v2/architecture-rebuild branch, remove all tracked old docs/ files and build a new authoritative Markdown and HTML set. Re-author the root README, handbook, contribution guide, security/governance guidance and changelog. Replace .claude/CODEBASE.md with a pointer to V2 contracts. Preserve CODE_OF_CONDUCT.md, LICENSE and SPONSORSHIP.md as nonarchitecture/legal files.

Old V1 source remains on the branch solely as reference until code migration is separately approved. V1 docs and reports remain accessible on main and in Git history. The Makefile docs target now runs the V2 integrity checker rather than regenerating V1 reference pages. A separate v2/** workflow checks documentation links and initial JSON contracts; it does **not** certify a runtime.

## Consequences

The V2 branch is intentionally not a release and the historical V1 tests may refer to deleted pages. Tests and generators must be rebuilt as part of implementation milestones. No production data is deleted; Git history retains all removed documentation. Architecture pages explicitly label the design as not implemented.

## Exit

When V2 code is running, update and generate the HTML website from one manifest and add actual schema, host-integration and security tests. Do not accept a static website as evidence of enforcement.
