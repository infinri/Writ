# Control Center: lifecycle semantics for policies, contracts, knowledge and memory

**Status: PLANNED.** Semantics that the owning services must enforce; the Control Center only invokes them. Nothing here is implemented.

## Common rules

- Every mutation is versioned, audited and reversible where the object type allows (rollback or restore), except hard deletion after its grace window.
- Every mutation supports preview. Destructive operations require typed confirmation.
- Eligibility checks run in the owning service, not the UI.
- Deletion never removes security audit records ([audit model](audit-and-observability.md)).

## Policies

States: `draft` > `validated` > `published` > `active` > `inactive` > `retired`.

| Operation | Semantics |
|---|---|
| Create, edit | Only drafts are editable. Editing a validated draft returns it to `draft` |
| Clone | New draft copied from any version, with lineage |
| Validate | Grammar, schema, conflict analysis, rule tests, invariant check; result stored with the draft revision |
| Simulate | Evaluate against recorded or sample requests; report every changed decision; no effects |
| Compare | Structural diff and decision diff between any two versions |
| Publish | Produces an immutable signed bundle (signing from M6); requires validation pass and publisher role; the bundle sha never changes |
| Activate | Selects which published bundle the kernel pulls for a scope; pending approvals bound to the old policy version are invalidated or re-evaluated (existing policy lifecycle) |
| Deactivate | Allowed only for non-mandatory policies; mandatory policies refuse (CC-R03) |
| Roll back | Activate an earlier published bundle; recorded as a new activation event, never as an edit of history |
| Retire | Bundle can no longer be activated; kept for audit and explanation |

Advisory guidance lives in a separate registry with the same versioning; it can never be activated into the enforced bundle without an enforcement binding and tests (INV-20).

## Contracts

States: `draft` > `published` > `deprecated` > `retired`.

- Core security contracts are read-only through the management plane (CC-C06).
- Custom contracts live in a tenant or pack namespace and cannot reuse a core id.
- Publishing a new version runs breaking-change detection: removing a field, narrowing a type, adding a required field or changing semantics marked in the schema is breaking and requires a major version (CC-C04).
- Deprecation records a replacement and an end date; retiring is refused while any active pack, policy or integration depends on the version (CC-C05).

## Knowledge sources

States: `registered` > `importing` > `indexed` > (`stale` | `failed`) > `archived` > `deleted`.

| Operation | Semantics |
|---|---|
| Import | Job with size and type limits; content stored as untrusted data with provenance (source, version, importer, digest) |
| Refresh | Re-import from origin; new version; old chunks superseded |
| Reindex | Rebuild index entries without re-import |
| Edit metadata | Versioned |
| Archive | Excluded from retrieval; content and index retained; reversible |
| Delete | Two phases. (1) Soft delete: excluded from retrieval immediately, grace window (default 7 days) for undo. (2) Hard delete job: removes content, chunks, embeddings and index entries; removes graph nodes and edges derived only from this source; marks memories that cite it as `needs_review` (they are not silently deleted); writes a reconciliation report |

Eligibility: refused while under legal hold, while referenced as required evidence by an open task or approval, or while the caller lacks the curator role in that scope.

Reconciliation verification (CC-SEC-10): after hard delete, index lookups by source id, chunk id and content digest return zero results; graph queries for nodes and edges with that source provenance return zero; the security audit chain still verifies and still contains the records that referenced the source (by id and digest).

## Memories

States: `active` > (`superseded` | `archived`) > `deleted`.

| Operation | Semantics |
|---|---|
| Correct, supersede | New version with a `supersedes` edge; the old version is excluded from retrieval but kept for provenance |
| Archive | Excluded from retrieval; reversible |
| Delete | Soft delete with grace window, then hard delete of content and derived edges; retention and consent rules may force deletion at expiry |
| Consent withdrawal | Memories that depended on consent are scheduled for deletion; retrieval excludes them immediately |

Rules: every memory keeps scope (tenant, project), origin, evidence links, confidence, consent status and retention. Cross-tenant or cross-project moves are not supported; a copy into another scope is a new memory with explicit provenance and requires authorization in both scopes. Graph relationships never grant rights.

## Tenants and projects

Archive makes a scope read-only. Deletion of a scope deletes its knowledge and memory under the rules above and leaves security audit records until their retention expires.
