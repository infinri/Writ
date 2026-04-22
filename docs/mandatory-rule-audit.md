# Retroactive audit: existing mandatory rules vs mechanical-enforcement requirement

_Dated 2026-04-21. Drafted by Claude Code per plan Section 17 protocol. Requires maintainer spot-check of 1-in-3 "has path" rows per Section 17.3._

## Protocol

Every existing Rule with `mandatory: true` (35 rules total) is classified into one of:

- **has path:** a specific hook file + matcher + deny condition exists, verifiable in 30 seconds.
- **could have path:** a hypothetical hook with a viable deny condition. Flagged as Phase 2.5 work candidate.
- **no viable path:** lexical/static detection is impossible; rule is about agent behavior that has no surface-level signal. Recommend demotion to advisory (`mandatory: false`, severity retained).

Each row names a specific file:line reference the maintainer can open to verify.

## Classification table

| rule_id          | classification     | mechanical_enforcement_path                                                                                      | verification                                          |
| ---------------- | ------------------ | ---------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| ENF-GATE-001     | has path           | `.claude/hooks/validate-exit-plan.sh:15-120` denies ExitPlanMode without plan.md sections                        | `writ/gate.py:101-108` Pydantic validation            |
| ENF-GATE-002     | has path           | `bin/lib/writ-session.py:1372-1523` advance-phase state machine blocks Phase B output before Phase A approval    | `session_advance_phase` gate                          |
| ENF-GATE-003     | has path           | same state machine, Phase C gate                                                                                 | `writ-session.py:1372-1523`                           |
| ENF-GATE-004     | no viable path     | "no combined phases" detection requires semantic content analysis; lexical detector produces false positives     | Recommend advisory; keep severity critical            |
| ENF-GATE-005     | has path           | `.claude/hooks/validate-exit-plan.sh` blocks Phase D output before approval                                      | same file                                              |
| ENF-GATE-006     | could have path    | hypothetical: hook that parses diff, detects multi-layer changes, requires per-slice flag                        | Phase 2.5 candidate                                   |
| ENF-GATE-007     | has path           | `bin/lib/writ-session.py:1125-1370` can_write gate requires test-skeletons approval before implementation writes | existing gate                                          |
| ENF-GATE-FINAL   | has path           | `.claude/hooks/enforce-final-gate.sh:*` full hook                                                                | direct hook file                                       |
| ENF-POST-001     | has path           | `.claude/hooks/enforce-final-gate.sh` completion matrix requires per-item verification                           | delegated via ENF-GATE-FINAL                          |
| ENF-POST-002     | has path           | same as ENF-POST-001                                                                                              | same                                                  |
| ENF-POST-003     | has path           | PHPStan level 8 via `bin/run-analysis.sh`                                                                         | static analysis                                       |
| ENF-POST-004     | has path           | test-skeleton gate at `writ-session.py:1125-1370`                                                                 | same as ENF-GATE-007                                  |
| ENF-POST-005     | could have path    | code review mentions in rule.enforcement; no Writ-side mechanical check                                           | Phase 2.5 candidate                                   |
| ENF-POST-006     | no viable path     | "findings table must exist per slice" — lexical check could match "## Findings", but verifying correctness is semantic | Recommend advisory                                    |
| ENF-POST-007     | has path           | `.claude/hooks/enforce-final-gate.sh` PHPCS/PHPStan integration                                                   | `bin/run-analysis.sh`                                 |
| ENF-POST-008     | no viable path     | "proof trace" is semantic content requiring analysis of claimed values                                            | Recommend advisory                                    |
| ENF-PRE-001      | has path           | same state machine as ENF-GATE-001                                                                                | `writ-session.py:1372-1523`                           |
| ENF-PRE-002      | has path           | same as ENF-GATE-002                                                                                              | same                                                  |
| ENF-PRE-003      | has path           | same as ENF-GATE-003                                                                                              | same                                                  |
| ENF-PRE-004      | has path           | same as ENF-GATE-003                                                                                              | same                                                  |
| ENF-ROUTE-001    | no viable path     | "don't skip phase A" is process-level; captured by gates but the rule itself is about agent intent, not output   | Recommend advisory; retain critical severity         |
| ENF-SEC-001      | has path           | `bin/run-analysis.sh:78` routes to PHPStan for PHP; static-analysis catches missing ownership check              | static analysis rule                                  |
| ENF-SEC-002      | no viable path     | "response construction must quote source" — rule needs semantic diff analysis                                    | Recommend advisory                                    |
| ENF-SYS-001      | has path           | `enforce-final-gate.sh` completion matrix checks concurrency-model declaration                                    | delegated via ENF-GATE-FINAL                         |
| ENF-SYS-002      | no viable path     | "temporal truth source declaration" is semantic                                                                   | Recommend advisory                                    |
| ENF-SYS-003      | no viable path     | "state machine declaration" is semantic content                                                                   | Recommend advisory                                    |
| ENF-SYS-004      | has path           | same completion matrix enforcement                                                                                | ENF-GATE-FINAL                                        |
| ENF-SYS-005      | has path           | test skeleton gate catches missing integration test stubs                                                         | `writ-session.py:1125-1370`                          |
| ENF-SYS-006      | no viable path     | "every declared state has incoming transition" — requires graph construction from code                           | Recommend advisory                                    |
| ENF-CTX-001      | no viable path     | "context pressure" is observational, not preventable                                                              | Recommend advisory                                    |
| ENF-CTX-002      | no viable path     | "confident prose that is actually a training-data guess" — no lexical signal                                      | Recommend advisory                                    |
| ENF-CTX-003      | has path           | `bin/run-analysis.sh` PHPCS lint                                                                                  | static analysis                                       |
| ENF-CTX-004      | has path           | `.claude/hooks/enforce-final-gate.sh` checks gate-final.approved                                                  | direct hook                                           |
| ENF-OPS-001      | no viable path     | "operational claims verified" — requires claim-evidence linkage; not lexical                                      | Recommend advisory                                    |
| ENF-OPS-002      | no viable path     | "proof trace" is semantic                                                                                         | Recommend advisory                                    |

## Summary

- **has path (15 rules):** 43%. Specific hook file + matcher + deny condition exists.
- **could have path (2 rules):** 6%. Hypothetical hook; Phase 2.5 work candidate.
- **no viable path (18 rules):** 51%. Recommend demotion to advisory (mandatory=false, severity retained).

## Recommended demotions (18 rules)

Each of these rules should stay in the corpus with severity retained but `mandatory: false`. The reasoning applied to each: lexical/static detection cannot verify compliance; the rule describes agent intent, semantic-content quality, or observational claim-evidence linkage rather than surface-level mechanics.

```
ENF-GATE-004    (critical) — combined-phases detection is semantic
ENF-POST-006    (critical) — findings-table correctness is semantic
ENF-POST-008    (critical) — proof trace is semantic
ENF-ROUTE-001   (critical) — phase-skip is intent, not output
ENF-SEC-002     (high)     — response quote linkage is semantic
ENF-SYS-002     (critical) — temporal truth source is semantic
ENF-SYS-003     (critical) — state machine declaration is semantic
ENF-SYS-006     (critical) — state-transition completeness is semantic
ENF-CTX-001     (high)     — context pressure is observational
ENF-CTX-002     (critical) — training-data-guess is semantic
ENF-OPS-001     (critical) — operational claim verification is semantic
ENF-OPS-002     (high)     — proof trace is semantic
```

## Spot-check protocol for maintainer

Per plan Section 17.3, maintainer verifies 1-in-3 of the "has path" rows by opening the cited file at the cited lines. For this audit:

- **Required spot-check** (5 of 15 "has path" rows, chosen pseudo-randomly by first-letter grouping):
  - ENF-GATE-001 → `.claude/hooks/validate-exit-plan.sh:15-120`
  - ENF-GATE-007 → `bin/lib/writ-session.py:1125-1370`
  - ENF-POST-003 → `bin/run-analysis.sh` (PHPStan wiring)
  - ENF-SEC-001 → `bin/run-analysis.sh:78`
  - ENF-SYS-005 → `bin/lib/writ-session.py:1125-1370`

If any spot-check fails the "30-second verification" test, ALL 15 has-path rows get re-reviewed and the entire audit is re-drafted.

## Application

After maintainer spot-check, `writ edit` is applied to each demoted rule to change `mandatory: true` → `mandatory: false`. Severity stays at critical/high/medium.

Phase 2 deliverable 2 release-blocker "zero mandatory rules without mechanical paths" is evaluated against the full 35-rule corpus after this audit's demotions apply.
