<!-- GENERATED FILE - do not edit. Source: writ-corpus.cypher. Regenerate with `make docs` (scripts/render-docs.py). -->


# Rulebook inventory

288 rules in the shipped corpus dump, 32 mandatory, 6 always-on. Generated from `writ-corpus.cypher`; the live graph may differ if rules were authored since the last `writ export-cypher`. Full rule text: `writ query`, `GET /rule/{id}`, or `writ export`.

| Domain | Rules | Mandatory |
|---|---:|---:|
| api-design | 12 | 0 |
| architecture | 28 | 0 |
| code-quality | 45 | 0 |
| communication | 2 | 0 |
| database | 2 | 0 |
| documentation | 8 | 0 |
| enforcement | 19 | 7 |
| frameworks | 12 | 0 |
| languages | 8 | 0 |
| meta-authoring | 3 | 0 |
| performance | 19 | 1 |
| process | 19 | 5 |
| research | 4 | 0 |
| scaling | 10 | 1 |
| security | 76 | 18 |
| testing | 21 | 0 |

## api-design

| Rule | Severity | Flags |
|---|---|---|
| `API-BREAKING-001` | high |  |
| `API-CONTRACT-001` | high |  |
| `API-ERROR-001` | high |  |
| `API-ERROR-002` | medium |  |
| `API-IDEMPOTENT-001` | medium |  |
| `API-PAGINATION-001` | high |  |
| `API-PAGINATION-002` | medium |  |
| `API-REST-001` | medium |  |
| `API-REST-002` | medium |  |
| `API-STATUS-001` | high |  |
| `API-STATUS-002` | medium |  |
| `API-VERSION-001` | medium |  |

## architecture

| Rule | Severity | Flags |
|---|---|---|
| `ARCH-ASYNC-001` | high |  |
| `ARCH-ASYNC-002` | medium |  |
| `ARCH-BOUNDARY-001` | high |  |
| `ARCH-BOUNDARY-002` | medium |  |
| `ARCH-COMP-001` | high |  |
| `ARCH-DTO-001` | medium |  |
| `ARCH-ENV-001` | medium |  |
| `ARCH-EVENT-001` | medium |  |
| `ARCH-FEATURE-001` | medium |  |
| `ARCH-IDEMPOTENT-001` | high |  |
| `ARCH-LAYER-001` | high |  |
| `ARCH-LAYER-002` | medium |  |
| `ARCH-MIGRATION-001` | high |  |
| `ARCH-MIGRATION-002` | medium |  |
| `ARCH-STATE-001` | high |  |
| `ARCH-STATE-002` | medium |  |
| `SOLID-DIP-001` | high |  |
| `SOLID-DIP-002` | critical |  |
| `SOLID-DIP-003` | medium |  |
| `SOLID-ISP-001` | medium |  |
| `SOLID-ISP-002` | medium |  |
| `SOLID-LSP-001` | high |  |
| `SOLID-LSP-002` | medium |  |
| `SOLID-OCP-001` | high |  |
| `SOLID-OCP-002` | medium |  |
| `SOLID-SRP-001` | high |  |
| `SOLID-SRP-002` | medium |  |
| `SOLID-SRP-003` | medium |  |

## code-quality

| Rule | Severity | Flags |
|---|---|---|
| `CLEAN-ASSERT-001` | medium |  |
| `CLEAN-BOOL-001` | medium |  |
| `CLEAN-COMMENT-001` | medium |  |
| `CLEAN-COMMENT-002` | medium |  |
| `CLEAN-COUPLING-001` | high |  |
| `CLEAN-COUPLING-002` | medium |  |
| `CLEAN-DEAD-001` | medium |  |
| `CLEAN-ERR-001` | high |  |
| `CLEAN-ERR-002` | high |  |
| `CLEAN-ERR-003` | medium |  |
| `CLEAN-FORMAT-001` | low |  |
| `CLEAN-FUNC-001` | high |  |
| `CLEAN-FUNC-002` | medium |  |
| `CLEAN-FUNC-003` | medium |  |
| `CLEAN-FUNC-004` | medium |  |
| `CLEAN-LOG-001` | medium |  |
| `CLEAN-LOG-002` | medium |  |
| `CLEAN-MAGIC-001` | high |  |
| `CLEAN-NAME-001` | medium |  |
| `CLEAN-NAME-002` | medium |  |
| `CLEAN-NEST-001` | high |  |
| `CLEAN-RETURN-001` | medium |  |
| `CLEAN-SIDE-001` | high |  |
| `CLEAN-TERNARY-001` | low |  |
| `CLEAN-TODO-001` | low |  |
| `DRY-CONFIG-001` | high |  |
| `DRY-CONFIG-002` | medium |  |
| `DRY-DUP-001` | high |  |
| `DRY-DUP-002` | medium |  |
| `DRY-DUP-003` | medium |  |
| `DRY-QUERY-001` | medium |  |
| `DRY-TEMPLATE-001` | medium |  |
| `DRY-TYPE-001` | medium |  |
| `ERR-CIRCUIT-001` | medium |  |
| `ERR-FALLBACK-001` | medium |  |
| `ERR-GRACEFUL-001` | high |  |
| `ERR-GRACEFUL-002` | medium |  |
| `ERR-HANDLE-001` | high |  |
| `ERR-HANDLE-002` | high |  |
| `ERR-HANDLE-003` | medium |  |
| `ERR-RETRY-001` | high |  |
| `ERR-RETRY-002` | medium |  |
| `ERR-TIMEOUT-001` | high |  |
| `ERR-TIMEOUT-002` | medium |  |
| `ERR-VALIDATION-001` | high |  |

## communication

| Rule | Severity | Flags |
|---|---|---|
| `ENF-COMMS-001` | high | always-on |
| `ENF-COMMS-OUTPUT-001` | medium | always-on |

## database

| Rule | Severity | Flags |
|---|---|---|
| `DB-SQL-002` | medium |  |
| `DB-SQL-003` | medium |  |

## documentation

| Rule | Severity | Flags |
|---|---|---|
| `DOC-API-001` | high |  |
| `DOC-ARCH-001` | medium |  |
| `DOC-CONFIG-001` | medium |  |
| `DOC-INLINE-001` | medium |  |
| `DOC-ONBOARD-001` | low |  |
| `DOC-README-001` | medium |  |
| `DOC-TYPE-001` | high |  |
| `DOC-TYPE-002` | medium |  |

## enforcement

| Rule | Severity | Flags |
|---|---|---|
| `ENF-CTX-003` | high | mandatory |
| `ENF-GATE-006` | high | mandatory |
| `ENF-GATE-007` | critical | mandatory |
| `ENF-OPS-001` | critical |  |
| `ENF-OPS-002` | high |  |
| `ENF-POST-003` | critical | mandatory |
| `ENF-POST-004` | critical |  |
| `ENF-POST-005` | high |  |
| `ENF-POST-006` | high | mandatory |
| `ENF-POST-007` | critical | mandatory |
| `ENF-PRE-001` | critical |  |
| `ENF-PRE-002` | critical |  |
| `ENF-PRE-003` | critical |  |
| `ENF-PRE-004` | critical |  |
| `ENF-SYS-002` | critical |  |
| `ENF-SYS-003` | critical |  |
| `ENF-SYS-005` | critical |  |
| `ENF-SYS-006` | critical |  |
| `ENF-TEST-001` | critical | mandatory |

## frameworks

| Rule | Severity | Flags |
|---|---|---|
| `FW-M2-001` | critical |  |
| `FW-M2-002` | critical |  |
| `FW-M2-003` | critical |  |
| `FW-M2-004` | high |  |
| `FW-M2-005` | critical |  |
| `FW-M2-006` | high |  |
| `FW-M2-RT-001` | critical |  |
| `FW-M2-RT-002` | critical |  |
| `FW-M2-RT-003` | high |  |
| `FW-M2-RT-004` | critical |  |
| `FW-M2-RT-005` | high |  |
| `FW-M2-RT-006` | high |  |

## languages

| Rule | Severity | Flags |
|---|---|---|
| `PHP-ERR-001` | high |  |
| `PHP-ERR-002` | medium |  |
| `PHP-TRY-001` | high |  |
| `PHP-TYPE-001` | low |  |
| `PY-ASYNC-001` | critical |  |
| `PY-IMPORT-001` | high |  |
| `PY-PROTO-001` | medium |  |
| `PY-PYDANTIC-001` | high |  |

## meta-authoring

| Rule | Severity | Flags |
|---|---|---|
| `ENF-META-CONCISE-001` | low |  |
| `META-AUTH-001` | high |  |
| `META-AUTH-002` | high |  |

## performance

| Rule | Severity | Flags |
|---|---|---|
| `PERF-ASYNC-001` | high |  |
| `PERF-BATCH-001` | medium |  |
| `PERF-BIGO-001` | high |  |
| `PERF-BUNDLE-001` | medium |  |
| `PERF-CACHE-001` | high |  |
| `PERF-CACHE-002` | high |  |
| `PERF-CACHE-003` | medium |  |
| `PERF-CACHE-004` | medium |  |
| `PERF-IMAGE-001` | low |  |
| `PERF-IO-001` | critical |  |
| `PERF-LAZY-001` | medium |  |
| `PERF-MEM-001` | high |  |
| `PERF-MEM-002` | medium |  |
| `PERF-OPT-001` | medium |  |
| `PERF-QBUDGET-001` | critical |  |
| `PERF-QUERY-001` | critical | mandatory |
| `PERF-QUERY-002` | high |  |
| `PERF-QUERY-003` | medium |  |
| `PERF-QUERY-004` | medium |  |

## process

| Rule | Severity | Flags |
|---|---|---|
| `ENF-PROC-BRAIN-001` | critical | mandatory |
| `ENF-PROC-DEBUG-001` | high | always-on |
| `ENF-PROC-FIXLOOP-001` | high |  |
| `ENF-PROC-PLAN-001` | high | mandatory, always-on |
| `ENF-PROC-PRIORITY-001` | high |  |
| `ENF-PROC-SDD-001` | high |  |
| `ENF-PROC-TDD-001` | critical | mandatory, always-on |
| `ENF-PROC-VERIFY-001` | critical | mandatory, always-on |
| `ENF-PROC-WORKTREE-001` | high | mandatory |
| `PROC-BRANCH-001` | low |  |
| `PROC-CHANGELOG-001` | medium |  |
| `PROC-COMMIT-001` | medium |  |
| `PROC-DEPLOY-001` | high |  |
| `PROC-ENV-001` | medium |  |
| `PROC-INCIDENT-001` | medium |  |
| `PROC-PLAN-001` | high |  |
| `PROC-REVIEW-001` | medium |  |
| `PROC-ROLLBACK-001` | high |  |
| `PROC-TEST-001` | high |  |

## research

| Rule | Severity | Flags |
|---|---|---|
| `RESEARCH-CITE-001` | high |  |
| `RESEARCH-CORROBORATE-001` | high |  |
| `RESEARCH-SOURCE-001` | high |  |
| `RESEARCH-STALENESS-001` | medium |  |

## scaling

| Rule | Severity | Flags |
|---|---|---|
| `SCALE-CONFIG-001` | medium |  |
| `SCALE-DB-001` | high |  |
| `SCALE-DB-002` | medium |  |
| `SCALE-HEALTH-001` | high |  |
| `SCALE-HEALTH-002` | medium |  |
| `SCALE-MIGRATE-001` | high |  |
| `SCALE-QUEUE-001` | high |  |
| `SCALE-QUEUE-002` | medium |  |
| `SCALE-STATELESS-001` | high | mandatory |
| `SCALE-STATELESS-002` | medium |  |

## security

| Rule | Severity | Flags |
|---|---|---|
| `ENF-SEC-001` | critical | mandatory |
| `SEC-AUTH-BRUTE-001` | high |  |
| `SEC-AUTH-ENUM-001` | medium |  |
| `SEC-AUTH-HASH-001` | critical | mandatory |
| `SEC-AUTH-HASH-002` | high |  |
| `SEC-AUTH-LOGOUT-001` | medium |  |
| `SEC-AUTH-MFA-001` | medium |  |
| `SEC-AUTH-RESET-001` | high |  |
| `SEC-AUTH-TIMING-001` | high |  |
| `SEC-AUTH-TOKEN-001` | critical | mandatory |
| `SEC-AUTH-TOKEN-002` | high |  |
| `SEC-AUTHZ-DEFAULT-001` | critical | mandatory |
| `SEC-AUTHZ-ENFORCE-001` | critical | mandatory |
| `SEC-AUTHZ-FUNC-001` | high |  |
| `SEC-AUTHZ-IDOR-001` | critical | mandatory |
| `SEC-AUTHZ-MASS-001` | critical | mandatory |
| `SEC-AUTHZ-PRIV-001` | high |  |
| `SEC-AUTHZ-RBAC-001` | medium |  |
| `SEC-AUTHZ-SCOPE-001` | high |  |
| `SEC-AUTHZ-TENANT-001` | critical |  |
| `SEC-CRYPTO-ALGO-001` | critical |  |
| `SEC-CRYPTO-ALGO-002` | high |  |
| `SEC-CRYPTO-CERT-001` | medium |  |
| `SEC-CRYPTO-IV-001` | high |  |
| `SEC-CRYPTO-KEY-001` | critical | mandatory |
| `SEC-CRYPTO-KEY-002` | high |  |
| `SEC-CRYPTO-RAND-001` | critical | mandatory |
| `SEC-CRYPTO-TLS-001` | high |  |
| `SEC-DATA-ENCRYPT-001` | high |  |
| `SEC-DATA-EXPORT-001` | medium |  |
| `SEC-DATA-MASK-001` | medium |  |
| `SEC-DATA-PII-001` | critical | mandatory |
| `SEC-DATA-PII-002` | high |  |
| `SEC-DATA-RETAIN-001` | medium |  |
| `SEC-DEP-AUDIT-001` | high |  |
| `SEC-DEP-LOCK-001` | medium |  |
| `SEC-DEP-PIN-001` | medium |  |
| `SEC-DEP-REVIEW-001` | low |  |
| `SEC-HDR-CORS-001` | critical |  |
| `SEC-HDR-CSP-001` | high |  |
| `SEC-HDR-FRAME-001` | medium |  |
| `SEC-HDR-HSTS-001` | high |  |
| `SEC-HDR-REFERRER-001` | low |  |
| `SEC-HDR-TYPE-001` | medium |  |
| `SEC-INJ-CMD-001` | critical | mandatory |
| `SEC-INJ-CMD-002` | high |  |
| `SEC-INJ-CSRF-001` | critical | mandatory |
| `SEC-INJ-DESER-001` | critical | mandatory |
| `SEC-INJ-HEADER-001` | high |  |
| `SEC-INJ-LDAP-001` | high |  |
| `SEC-INJ-LOG-001` | medium |  |
| `SEC-INJ-PATH-001` | critical |  |
| `SEC-INJ-REDIR-001` | high |  |
| `SEC-INJ-SQL-001` | critical | mandatory |
| `SEC-INJ-SQL-002` | critical |  |
| `SEC-INJ-SQL-003` | high |  |
| `SEC-INJ-SSRF-001` | critical | mandatory |
| `SEC-INJ-SSTI-001` | critical |  |
| `SEC-INJ-XSS-001` | critical | mandatory |
| `SEC-INJ-XSS-002` | critical |  |
| `SEC-INJ-XSS-003` | high |  |
| `SEC-INJ-XSS-004` | high |  |
| `SEC-RATE-API-001` | high |  |
| `SEC-RATE-BATCH-001` | medium |  |
| `SEC-RATE-LOGIN-001` | high |  |
| `SEC-RATE-QUERY-001` | medium |  |
| `SEC-RATE-UPLOAD-001` | high |  |
| `SEC-UNI-003` | high |  |
| `SEC-VAL-ALLOW-001` | high |  |
| `SEC-VAL-ENCODING-001` | high |  |
| `SEC-VAL-FILE-001` | critical | mandatory |
| `SEC-VAL-LENGTH-001` | high |  |
| `SEC-VAL-RANGE-001` | medium |  |
| `SEC-VAL-REGEX-001` | medium |  |
| `SEC-VAL-SERVER-001` | critical | mandatory |
| `SEC-VAL-TYPE-001` | high |  |

## testing

| Rule | Severity | Flags |
|---|---|---|
| `TEST-ASSERT-001` | high |  |
| `TEST-ASSERT-002` | medium |  |
| `TEST-CI-001` | medium |  |
| `TEST-COVERAGE-001` | medium |  |
| `TEST-EDGE-001` | high |  |
| `TEST-EDGE-002` | medium |  |
| `TEST-EDGE-003` | medium |  |
| `TEST-EXIST-001` | high |  |
| `TEST-EXIST-002` | high |  |
| `TEST-FIXTURE-001` | medium |  |
| `TEST-FIXTURE-002` | medium |  |
| `TEST-INT-001` | medium |  |
| `TEST-ISOLATE-001` | high |  |
| `TEST-ISOLATE-002` | high |  |
| `TEST-ISOLATE-003` | medium |  |
| `TEST-MOCK-001` | medium |  |
| `TEST-MOCK-002` | medium |  |
| `TEST-NAME-001` | medium |  |
| `TEST-PERF-001` | low |  |
| `TEST-REGRESSION-001` | high |  |
| `TEST-SNAPSHOT-001` | low |  |
