# Writ V2 data ownership, records and graph schema

**Status:** target specification; schema migrations not yet implemented. Do not mistake the V1 graph dump for a V2 import.

## Storage boundaries

| Owner | Local | Enterprise | Purpose | Explicitly NOT |
|---|---|---|---|---|
| Operational ledger | SQLite WAL, secure file permissions | PostgreSQL transactional cluster | Sessions, state revision, policy pins, approval lifecycle, idempotency and effect receipts | Vector search and general graph traversal |
| Graph intelligence | Optional locally provisioned Neo4j | Authenticated Neo4j service | Knowledge, provenance, decisions, memory and typed edges | Authoritative tool permission or approval nonce |
| Retrieval indexes | Local immutable generation directories | Sharded/replicated read-only indexes | BM25, vector, metadata and bounded snapshots | Canonical approvals or policy |
| Audit | Local append-only hash-linked records + ledger pointers | Signed/append-only stream with retention controls | Evidence of decision, actor, policy, executed effect and recovery | A best-effort debug log alone |
| Optional workflow | No remote requirement | Temporal/Restate or equivalent only after need demonstrated | Long-lived scheduling, retries and distributed reconciliation | Shortcut around broker |

## Operational relational schema (logical)

~~~sql
CREATE TABLE tenants (
  tenant_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE principals (
  tenant_id TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  identity_provider TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY (tenant_id, principal_id)
);
CREATE TABLE sessions (
  tenant_id TEXT NOT NULL, session_id TEXT NOT NULL,
  project_id TEXT NOT NULL, revision BIGINT NOT NULL,
  state_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (tenant_id, session_id)
);
CREATE TABLE policy_bundles (
  tenant_id TEXT NOT NULL, bundle_sha TEXT NOT NULL,
  version TEXT NOT NULL, publisher TEXT NOT NULL,
  signature BYTEA, published_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (tenant_id, bundle_sha)
);
CREATE TABLE approval_requests (
  tenant_id TEXT NOT NULL, request_id TEXT NOT NULL,
  principal_id TEXT NOT NULL, action_digest TEXT NOT NULL,
  resource_digest TEXT NOT NULL, policy_bundle_sha TEXT NOT NULL,
  state TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
  revision BIGINT NOT NULL,
  PRIMARY KEY (tenant_id, request_id)
);
CREATE TABLE approval_grants (
  tenant_id TEXT NOT NULL, grant_id TEXT NOT NULL,
  request_id TEXT NOT NULL, trusted_approver_id TEXT NOT NULL,
  max_uses INT NOT NULL, used_count INT NOT NULL DEFAULT 0,
  nonce_hash TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  PRIMARY KEY (tenant_id, grant_id)
);
CREATE TABLE effect_attempts (
  tenant_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, action_digest TEXT NOT NULL,
  decision_id TEXT NOT NULL, status TEXT NOT NULL,
  resource_id TEXT NOT NULL, receipt_json JSONB,
  created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (tenant_id, attempt_id),
  UNIQUE (tenant_id, idempotency_key)
);
CREATE TABLE audit_events (
  tenant_id TEXT NOT NULL, event_id TEXT NOT NULL,
  subject_id TEXT NOT NULL, sequence BIGINT NOT NULL,
  event_json JSONB NOT NULL, previous_hash TEXT,
  event_hash TEXT NOT NULL,
  PRIMARY KEY (tenant_id, event_id),
  UNIQUE (tenant_id, subject_id, sequence)
);
~~~

**Important:** This is a logical PostgreSQL example, not executable migration code for SQLite. Migrations must also add FKs, query indexes, retention partitions, row-level/DB privilege enforcement, envelope encryption and migration safety. The prototype's SQLite schema uses equivalent semantics but compatible types and locking.

### Transaction protocols

**Approval consume:** lock request and grant (or CAS revision); verify all bindings; increment use count; create effect attempt/idempotency reservation atomically; commit before effect. **Effect completion:** finalize receipt or leave UNKNOWN for reconciliation; never auto-replay an ambiguous irreversible external action. **Policy publish:** verify signatures/tests and atomic active-bundle pointer swap. **State transitions:** expected_revision compare-and-swap, append event, update materialization in one transaction.

## Neo4j logical graph

Node labels: Tenant, Project, KnowledgeRule, Playbook, Evidence, Source, Claim, Decision, Memory, File, Commit, Change, ActorRef, PolicyRef (read-only evidence link), ContextSnapshot. Relationships: APPLIES_TO, SUPPORTS, CONTRADICTS, DERIVED_FROM, SUPERSEDES, RELATED_TO, AFFECTS, OBSERVED_IN, RETRIEVED_FOR, RECORDED_BY, SCOPED_TO.

Every tenant-private node carries tenant_id and visibility scope. Shared doctrine is explicitly published under a distinct trusted/shared namespace, never inferred because the project field equals a special value. All graph traversals enforce tenant/visibility for **each node and edge**, including neighbor enrichment and path expansion.

~~~cypher
CREATE CONSTRAINT memory_identity IF NOT EXISTS
FOR (m:Memory) REQUIRE (m.tenant_id, m.memory_id) IS UNIQUE;
CREATE CONSTRAINT decision_identity IF NOT EXISTS
FOR (d:Decision) REQUIRE (d.tenant_id, d.decision_id) IS UNIQUE;
~~~

These Cypher examples are starting points only; indexes, property types and supported uniqueness constraints must be validated on the target Neo4j edition/version. Neo4j licenses and operations are deployment decisions, not free assumptions.

## Evidence and temporal truth

Every claim has origin, source digest, recorded_at, valid_from/valid_to if known, confidence and supersession link. A contradicted claim is retained as history but should not be returned as uncontested current truth. Retrieval returns evidence references and explicit conflicts. A model-authored memory cannot become an authorization policy via a graph edge.

## Import rules

V1 Cypher dump is read-only input. Validate labels and schema against an allowlist, convert to V2 IDs, classify rules as advisory vs enforceable, map project scope to tenant scope, deduplicate by provenance and record an import receipt. Require human review for ambiguous canonical facts or access policies. Never execute untrusted V1 Cypher in a privileged production DB as the migration mechanism.

## Backups, retention and encryption

Encrypted backups of both stores, separate key management, tenant-specific retention, deletion/tombstone propagation into search indexes, periodic restore drills and audit chain verification. Ensure ordinary telemetry is not used as a substitute for durable effect records.

See [architecture](architecture.md), [Recall](retrieval-and-memory.md), and [operations](../operations/deployment.md).
