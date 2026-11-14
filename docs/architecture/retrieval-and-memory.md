# JIT retrieval, graph intelligence and Recall

## Separate intelligence plane from authority plane

Writ V2 preserves the product's distinctive context behavior: relevant rules near the action, a connected knowledge graph, evidence-backed decisions and durable memory. But all retrieved text, graph facts and memories are **untrusted data**. A retrieved 'you may do X' is never a broker permit.

## Retrieval stages

1. **Resolve caller:** verified tenant/principal/project, workload intent, confidentiality boundary and query budget.
2. **Intent and scope:** classify needed evidence type and likely relevant files or domains; abstain when context is insufficient.
3. **Candidate retrieval:** lexical and vector indexes, ideally warmed and read-only in the local process. Compare ONNX-HNSW-Tantivy V1 behavior rather than blindly replacing it.
4. **Graph expansion:** bounded multi-hop relationships (e.g., rule-to-exception, decision-to-evidence, file-to-change), with per-node authorization and edge provenance.
5. **Conflict/staleness:** filter invalid, revoked, stale or contradictory claims; surface conflicts rather than merging incompatible instructions.
6. **Rank and budget:** deterministic score, importance and recency/authority weighting, diversity, token/character cap, dedup and citation references.
7. **Deliver:** explicit evidence/guide classes, source IDs, confidence, version and exclusions; abstain on insufficient evidence.
8. **Feedback:** separate user-confirmed outcomes from model-generated speculation. Update indexes asynchronously with generation swaps and rollback.

## Data model

Doctrine (guidance/policy description), Evidence, Decision, FileChange, Memory, Source, Entity, Relation, Claim, Project, Tenant, Principal and ProvenanceEvent. Only policy registry controls enforcement. Graph queries receive caller scope; no 'project=None means all tenant records' behavior. Shared doctrine is explicitly published into a shared namespace, never inferred from a field named project.

All relationships have type, source, evidence, valid time and visibility policy. Graph working sets have upper bounds and revisions. Snapshot/cache invalidation is generation based; graph unavailability may degrade retrieval but cannot relax security decisions.

## Recall: what persists

- Provenance-backed **decision**: rationale, affected artifacts, evidence links, approval status and supersession.
- **Observation**: tool result or externally verified claim, timestamp, expiry and confidence.
- **Preference**: user or tenant provided with consent and revocation semantics.
- **Project constraint**: scoped instruction, version and owner; if it is an actual enforcement rule it must be separately published through policy.
- **Ephemeral context**: short-retention recent activity, not silently promoted to permanent fact.

Memory merging needs contradiction and temporal semantics; don't keep a second mutable 'truth' in session cache without reconciliation. Principal access must be checked at query time and embedding/graph-neighbor expansion time.

## Evaluation program

Freeze golden V1 query sets covering general coding, Magento/opaque SDKs, permissions, multi-project isolation, near misses, abstentions and conflicting evidence. Compare precision/recall@k, mean reciprocal rank, relevant tokens per turn, memory RSS, refresh time and p95. Re-run with 10x corpus growth. Preserve strong V1 ranking where it wins; replace only with measured evidence.

See [data model](data-model.md) and [benchmark plan](../testing/verification-and-benchmarks.md).

## Cross-domain evidence

JIT retrieval is not code-only. A Research pack may prioritize dated source provenance and contradictory claims; a Review pack may prioritize document ranges, rubric criteria and verification status; a Coding pack may prioritize diffs and tests. All share the same scoped Evidence/Artifact contracts, graph/visibility protections and abstention behavior. Model outputs may create observations marked unverified but cannot promote themselves into policy. [Capability Pack design](capability-packs.md).
