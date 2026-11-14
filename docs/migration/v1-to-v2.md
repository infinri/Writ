# V1 to V2 migration and safe cutover

## Policy

**No in-place reinterpretation of old approvals or enforcement.** V1 remains available from main and Git history. V2 starts as a separate executable and storage namespace. This branch's legacy code is reference material, not active target architecture.

## Inventory before import (M0)

Export V1 dataset read-only: graph schema version, Cypher corpus, rule IDs/flags, authority/provenance/staleness, graph relationships, decision-memory records, human-verified documentation, retrieval golden set, session/approval tests and security negative fixtures. Record checksum, source commit, export time and import manifest. Do not migrate transient session tokens as authorization.

## Rule classification

| V1 source | V2 treatment |
|---|---|
| Human-authored relevant doctrine | Import as advisory KnowledgeRule with provenance |
| Mandatory/enforcement metadata | **Quarantine unverified** until mapped to registered V2 policy implementation and tests |
| Historic hook paths/line numbers | Do not translate into enforcement claims |
| Approval phrases and text tokens | Archive as behavioral tests, NOT grants |
| Graph relationships | Map to scoped typed edges with visibility checks |
| Prior Decisions/Recall | Import with source attribution, retention and tenant/project review |
| Retrieval behavior | Preserve gold queries, abstentions, benchmark corpora |
| Shell parser and host script | Keep as bypass regression fixture, NOT an enterprise security boundary |
| V1 unit/integration tests | Classify and port invariant-level tests, not accidental process-specific behavior |
| V1 release notes and large HTML graph assets | Keep through main/history; not normative in V2 branch |

## Staged migration procedure

1. **M0 freeze:** record source SHA, create export manifest and complete behavior inventory, no V1 production changes.
2. **M2 contracts:** make importer produce deterministic V2 objects; validate graph scopes and referential integrity.
3. **M5 shadow adapter:** fork event *observations* into V1 and V2 decisions; never perform duplicate side effects.
4. **M6 policy readiness:** all truly enforced V2 policies have test vectors and signed bundle; nonverified rules remain advisory.
5. **M7/M8 knowledge migration:** compare retrieval outputs at equal corpora; check tenant data leaks and contradiction handling.
6. **M9 operations dry run:** backup/restore local and enterprise ledger; validate audit chain continuity.
7. **M12 canary:** limited governed action types in disposable repos, measured p95 and negative controls.
8. **Cutover:** human sign-off, change freeze, explicit capability coverage, rollback conditions and maintenance plan.

## Rollback and dual-run warning

Shadow mode must not cause duplicate git pushes, writes, network actions or approvals. On privileged divergence, V1/V2 decisions are diagnostics; the predeclared canonical runtime alone may authorize the real effect. On failure, block or route to an explicitly accepted baseline. Never silently downgrade a previously strict enterprise profile.

## Historical source

Legacy ADRs, reference pages, pressure-run snapshots and HTML from main remain in Git history. Their removal from docs/ here prevents ambiguous design ownership; evidence should be cited by original commit when reused.
