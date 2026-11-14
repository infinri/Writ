# Rule review batch-11: scaling (10 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-11.csv](batch-11.csv).

## SCALE-CONFIG-001

- **Domain / severity / mandatory:** scaling / medium / false
- **Statement:** Configuration supports environment-based overrides without code changes. Defaults live in code; per-environment values come from env vars, config files, or a secrets manager. New deployment targets do not require code edits.
- **Trigger:** When introducing configurable behavior.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-ENV-001, DOC-CONFIG-001, ERR-TIMEOUT-002, SEC-CRYPTO-KEY-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SCALE-DB-001

- **Domain / severity / mandatory:** scaling / high / false
- **Statement:** Database connections are pooled with size limits. Per-request connection creation is forbidden; the pool is initialized at startup. Pool size is bounded so that the application cannot exhaust the database's connection limit.
- **Trigger:** When establishing database connections from application code.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SCALE-DB-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SCALE-DB-002

- **Domain / severity / mandatory:** scaling / medium / false
- **Statement:** Read replicas serve read-heavy queries where eventual consistency is acceptable (analytics, dashboards, list pages). Writes go to the primary; reads route to the closest healthy replica.
- **Trigger:** When implementing read-heavy queries against a database that supports replication.
- **V1 enforcement text:** Code review. ORM router config (Django DATABASES + DATABASE_ROUTERS, SQLAlchemy multiple binds).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SCALE-DB-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SCALE-HEALTH-001

- **Domain / severity / mandatory:** scaling / high / false
- **Statement:** Health-check endpoints verify actual service readiness: DB connection, critical downstream dependencies, ability to serve a request. A 200 OK that doesn't check downstream dependencies is a violation.
- **Trigger:** When implementing health-check endpoints for orchestration systems (Kubernetes, ECS, load balancers).
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SCALE-HEALTH-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SCALE-HEALTH-002

- **Domain / severity / mandatory:** scaling / medium / false
- **Statement:** Readiness (`/ready`) and liveness (`/live`) probes are distinct. Ready = can serve traffic now (deps healthy, warmup done). Live = process is responsive but not necessarily ready. A liveness failure restarts the pod; a readiness failure removes it from rotation.
- **Trigger:** When implementing health endpoints alongside Kubernetes-style readiness/liveness probes.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** SCALE-HEALTH-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SCALE-MIGRATE-001

- **Domain / severity / mandatory:** scaling / high / false
- **Statement:** Database migrations are backwards-compatible across the deployment window: old code can run against the new schema until the deploy completes. Destructive changes (drop column, rename column) are split into multiple deploys: 1) deploy code that stops using the column; 2) drop in a later deploy.
- **Trigger:** When deploying schema changes alongside rolling code deploys.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** API-BREAKING-001, ARCH-MIGRATION-001, ARCH-MIGRATION-002, PROC-DEPLOY-001, PROC-ROLLBACK-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SCALE-QUEUE-001

- **Domain / severity / mandatory:** scaling / high / false
- **Statement:** Long-running operations are dispatched to a background queue (Celery, Sidekiq, RQ, BullMQ, SQS+worker), not executed inside the request cycle. The request returns a job ID; status is polled or pushed.
- **Trigger:** When implementing operations that take longer than the request timeout: video transcoding, large reports, batch emails, ML inference, external-API fan-out.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ENF-OPS-002, SCALE-QUEUE-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SCALE-QUEUE-002

- **Domain / severity / mandatory:** scaling / medium / false
- **Statement:** Queue consumers are idempotent: receiving the same message twice does not corrupt state. Messages may be redelivered (worker crash mid-job, broker retry). Use idempotency keys, conditional writes, or upserts to absorb duplicates.
- **Trigger:** When implementing background-queue consumers.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** ARCH-EVENT-001, ARCH-IDEMPOTENT-001, ERR-GRACEFUL-002, SCALE-QUEUE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SCALE-STATELESS-001

- **Domain / severity / mandatory:** scaling / high / true
- **Statement:** Application processes are stateless. Session data, user state, in-progress workflows, and any data that must survive across requests is stored in an external store (Redis, database, cache service), not in process memory. Module-level mutable globals that hold per-user state are violations.
- **Trigger:** When implementing application processes that handle multiple requests (web servers, API handlers, queue workers).
- **V1 enforcement text:** Code review. Look for module-level mutable dicts/lists that grow with users.
- **V1 mechanical path:** bin/run-analysis.sh::analyze_scaling_stateless (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `bin/lib/analyzers-regex.sh`
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SCALE-STATELESS-002, SEC-AUTH-LOGOUT-001
- **Proposed disposition:** candidate_enforced (V1 binding resolves at baseline; must be rebound to a V2 validator); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_

## SCALE-STATELESS-002

- **Domain / severity / mandatory:** scaling / medium / false
- **Statement:** File storage uses object storage (S3, GCS, Azure Blob, MinIO), not the local filesystem. Files written to local disk are not durable across deploys and are not shared across workers.
- **Trigger:** When persisting user-uploaded files or generated artifacts.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** SCALE-STATELESS-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: software-engineering
- **Reviewer verdict:** _not reviewed_
