# Writ V2 codebase guide

**Branch:** v2/architecture-rebuild  
**Status:** design and documentation only; V2 code has not yet been implemented.

This guide supersedes the V1 architecture notes formerly stored here. The old notes remain in main branch Git history. The existing writ/, hooks/, bin/, agents/, rules/, tests/ and deployment scripts currently on this branch are **legacy V1 source reference**. Do not use them as the architecture template or assume they implement the new security invariants.

## Start with these sources

- [Documentation index](../docs/README.md)
- [Master implementation plan](../docs/implementation/MASTER-IMPLEMENTATION-PLAN.md)
- [Boundaries and owners](../docs/architecture/architecture.md)
- [Target repository layout](../docs/architecture/repository-layout.md)
- [Security invariants](../docs/security/invariants.md)
- [Runtime language benchmark ADR](../docs/decisions/ADR-0002-runtime-bakeoff.md)
- [HTML architecture atlas](../docs/architecture/index.html)

## Contributor instruction

No new V1-style hook policy copies, implicit current-session pointers, natural-language approval tokens, graph-as-permission checks or recursive shell parsing as an enterprise security guarantee. Work against normalized contracts and one capability broker. Classify code and tests as keep/reimplement/discard before porting. Always distinguish **planned**, **implemented** and **verified**.

Use python3 scripts/check-v2-docs.py (or make docs-check on this branch) for documentation integrity. V1's scripts/render-docs.py is intentionally not the renderer for the replacement documentation.
