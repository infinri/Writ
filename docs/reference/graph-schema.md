# Graph schema, corpus, and integrity

The knowledge-graph contract: node types, edges, ingest and export round-trip, reconcile, validators, and the rule-authoring lifecycle. Source of truth: `writ/graph/schema.py` (models and registries), `writ/graph/db/` (store mixins), `writ/graph/ingest.py` + `methodology_ingest.py` (parse and write), `writ/graph/integrity/` (checks).

## 1. Node types

**13 types**, registered once in `NODE_TYPE_MODELS` / `NODE_ID_FIELDS` (`schema.py`); adding a type is one registry edit. Ids match `RULE_ID_PATTERN`; methodology types enforce a prefix at the Pydantic boundary.

| Type | Id field (prefix) | Retrieval |
|---|---|---|
| Rule | `rule_id` (any) | ranked pipeline (non-mandatory) or always-on floor (mandatory) |
| Skill / Playbook / Technique / AntiPattern | `skill_id` (SKL-) / `playbook_id` (PBK-) / `technique_id` (TEC-) / `antipattern_id` (ANT-) | companion channel + ranked pipeline |
| ForbiddenResponse | `forbidden_id` (FRB-, `always_on` defaults true) | always-on bundle + ranked pipeline |
| Category | `category_id` (CAT-) | never retrieved; routing data (`routes` must be non-empty, values from the 7-value `RouteValue` enum) |
| Phase / Rationalization / PressureScenario / WorkedExample | PHA- / RAT- / PSC- / EXM- | graph-neighbor surfacing only |
| SubagentRole | `role_id` (ROL-) | `GET /subagent-role/{name}` only |
| Abstraction | `abstraction_id` (ABS- by convention, unenforced) | summary-mode substitution |

**Rule fields** (`schema.py:221-257`): identity and content (`domain`, `severity`, `scope`, `trigger`, `statement`, `violation`, `pass_example`, `enforcement`, `rationale`, `body`), governance (`mandatory`, `always_on`, `confidence`, `authority`, `provenance`, `graduated_via`, `mechanical_enforcement_path`), routing (`applicability_scope`, `trigger_keywords`), and runtime counters (`times_seen_positive/negative`, `last_seen`, `evidence`, `staleness_window`, `last_validated`).

**Decision-memory records** (`Decision`, `FileChange`, `Commit`, plus the `:Project` registry) are deliberately absent from every retrieval registry: they can never enter RAG. All are stamped `provenance="record"` at write time; content-hash ids make capture idempotent.

**Three orthogonal governance axes.** `provenance` (where the words are in their lifecycle: `hand-authored`, `proposed`, `graduation_pending`, `graduated`, `record`), `authority` (who vouches: `human`, `ai-provisional`, `ai-promoted`), `source_origin` (what created the node: `ingest` vs `graph-authored`; the reconcile-protection bit). Caveat for queries: the ~128 seed-script-created rules carry neither `source_origin` nor `provenance` properties; never assume every Rule has them.

**Domains and tags.** `VALID_DOMAINS` is a closed 17-value set enforced only by the integrity check, not at the model boundary. Live data has mixed casing (`security` and `Security` coexist); normalize before comparing. Tags are normalized lowercase-and-sorted at the schema boundary so BM25 never indexes `TDD` and `tdd` as distinct terms. Two intentional default asymmetries: `Rule.evidence` defaults to the original-bible marker while methodology nodes default to `peer-reviewed` (`schema.py:237` vs `:386`).

**Constraints** (`db/schema_store.py`, applied idempotently on every ingest): composite `(id_field, project)` uniqueness per label, `Project.name` unique (this constraint is what makes concurrent project registration race-safe), plus domain and record-lookup indexes.

## 2. Edges

**24 types** (`db/_common.py`, `ALLOWED_EDGE_TYPES`): 17 corpus edges (`DEPENDS_ON`, `PRECEDES`, `CONFLICTS_WITH`, `SUPPLEMENTS`, `SUPERSEDES`, `RELATED_TO`, `ABSTRACTS`, `TEACHES`, `COUNTERS`, `DEMONSTRATES`, `DISPATCHES`, `GATES`, `PRESSURE_TESTS`, `CONTAINS`, `ATTACHED_TO`, `BELONGS_TO`, `INVOKES`) and 7 record edges (`HAS_DECISION`, `HAS_CHANGE`, `HAS_COMMIT`, `MOTIVATED_BY`, `GOVERNED_BY`, `INCLUDES`, `REALIZES`), created only through per-type wrappers with an endpoint allowlist.

Directed invariants (`writ validate` fails on violation): `DISPATCHES` targets `SubagentRole` only; `INVOKES` never targets `SubagentRole` (it means "apply inline, one level"); `TEACHES` never originates from a `Rule`. Denormalized-field parity: `AntiPattern.counter_nodes` must equal its `COUNTERS` edges, `SubagentRole.dispatched_by` its incoming `DISPATCHES`.

**Authored vs derived.** Front-matter nodes declare edges in an `edges:` list; RULE-START blocks in an `### Edges` section (`- TYPE: ID`). Derived at ingest, never hand-declared: `RELATED_TO` from rule-id mentions in prose (the declared-edges section is stripped first so targets don't double as prose references), `BELONGS_TO` from the category field and the category tree. `ABSTRACTS` is machine-written by `writ compress`. Edge endpoints resolve via an OR-match derived from `NODE_ID_FIELDS` (never a hand-list), scoped within the project so a cross-project id reference dangles instead of silently binding.

## 3. Ingest and the round-trip

Three source formats, strict precedence (`ingest.py`): YAML front-matter (one node per file; `node_type` inferred from the id field), `<!-- NODE START type=X id=Y -->` markers (multi-node), legacy `<!-- RULE START: id -->` (Rule). Schema validation runs before reachability checks so a missing field is never masked; dual-location duplicates dedupe with the front-matter copy winning; constraints are applied fail-loud before every batch write.

**Round-trip contracts:**
- `SECTION_HEADERS` (`schema.py`) is the single export-to-ingest header map; a mismatch silently loses fields.
- `GRAPH_ONLY_FIELDS` (`export.py:39`) are stripped on export and re-derived on ingest (`confidence`, `authority`, `times_seen_*`, `evidence`, `staleness_window`, `last_validated`, `source_origin`; `provenance` omitted only at its default). `mandatory` and `mechanical_enforcement_path` round-trip via explicit `**Mandatory**:` / `**Mechanical_Enforcement_Path**:` metadata lines, deliberately not graph-only.
- Reconcile's property-clear operates only on `MANAGED_PROP_NAMES` = all model fields minus `RUNTIME_EXEMPT_PROPS` (`schema.py:742`), so counters and runtime fields can never be wiped by a source that omits them.
- **Dual-location rules**: 13 ids (`_METHODOLOGY_CANONICAL_RULE_IDS`, `export.py`) export to `bible/methodology/<ID>.md` and are excluded from domain `rules.md`. Hand-editing them is safe; new graduated canon must be added to the list or export produces a duplicate.

**Reconcile** (`methodology_ingest.reconcile`): upsert, then three prune phases in order: delete nodes absent from the oracle, delete stale edges, clear stale managed props. Project-scoped; exempts graph-first provenance (`proposed`, `graduation_pending`) and `record` everywhere (one frozenset, `PARITY_EXEMPT_PROVENANCE`, keyed by seven call sites). Refuses an empty oracle. Full-corpus-only is caller discipline, not code-enforced: reconciling a partial source deletes everything the partial omits.

**The dump.** `writ export-cypher` writes `writ-corpus.cypher` deterministically (staged `_dump_id`, sorted, stripped at the end); `writ import-cypher` wipes first because the CREATEs are not idempotent. 2026-07-31 dump: 464 nodes (Rule 287 of which 33 mandatory, Abstraction 62, Category 22, Phase 20, Skill 15, Playbook 15, AntiPattern 13, Technique 11, SubagentRole 7, Rationalization 4, WorkedExample 3, PressureScenario 3, ForbiddenResponse 2), 732 edges (BELONGS_TO 331, ABSTRACTS 186, RELATED_TO 69, COUNTERS 29, TEACHES 24, ...).

## 4. Integrity checks

`writ validate` runs ~30 checks via `IntegrityChecker.run_all_checks` (`writ/graph/integrity/`). Construction gotcha: `IntegrityChecker(driver, database)` takes a raw Neo4j `AsyncDriver`, not a `Neo4jConnection` (`integrity/__init__.py:66`). The exit code is data-driven: any truthy finding gates unless its key is in the explicit non-gating set. Adding a check auto-gates; adding an *advisory* check requires adding its key to `_NON_GATING` or it will fail builds.

- **Structural**: conflicts, orphans (all labels; Abstraction exempt), dangling dispatched roles, staleness, near-duplicates (embedding cosine >= 0.95; *raises* when sentence-transformers is absent so "did not run" is never mistaken for "clean").
- **Reachability**: stranded mandatory (the union predicate `mandatory OR always_on` in `predicates.py`, shared by endpoint and validator), ranked-exclusion set equality, always-on budget breach (5,000-token cap on the summary render), floor completeness (node-declared `floor_modes` must exactly match the expected fixture), trigger-keyword parity (keywords must appear verbatim in the node's own text), push reachability, action vocabulary closure (6 known actions).
- **Parity**: node presence, edge parity, prop parity, and a bidirectional value-level field-drift check for methodology nodes; all provenance-exempt as above.
- **Content**: code examples must parse (PASS examples reject deprecated Pydantic-v1 idioms), domain enum, forbidden-phrase uniqueness, no shared verbatim code blocks, enforceable-severity coupling (critical/high with an enforcement path must be mandatory).
- **Artifacts**: `bible/abstractions.json` rule-id freshness.
- **Advisory, never gates**: redundancy unavailability, unreviewed count, frequency staleness, graduation flags.

## 5. How rules grow

1. **Propose** (`writ propose` / `POST /propose` -> `structural_gate`, five ordered checks: schema, mechanical-enforcement policy for mandatory, specificity, redundancy/novelty cosine bands 0.95/0.85, conflicts). Accepted proposals are force-stamped `authority="ai-provisional"`, `confidence="speculative"`, land graph-first (`source_origin="graph-authored"`, `provenance="proposed"`), and their origin context is stored write-once (`~/.cache/writ/origin_context.db`).
2. **Graduate**: at 50+ observations with >= 0.75 positive ratio (`writ/frequency.py`), `proposed` flips to `graduation_pending`, race-guarded, changing nothing else. The threshold is a plain ratio: there is deliberately no Wilson interval or statistical smoothing anywhere in the graduation path.
3. **Promote** (`writ review --promote` -> token-gated `/promote-candidate`): the review artifact surfaces content plus canon-fit (conflicts, same-category members, high-similarity neighbors) so approval is informed. Approve as-is or edit at the gate (edits re-run the structural gate; a materially changed statement resets the observation counters). The node is stamped `provenance="graduated"`, `authority="ai-promoted"` (`promotion.py:220`), and exported to `bible/methodology/`.

Interactive authoring (`writ add` / `writ edit`) pre-checks id collisions before MERGE would silently update, warns when a graph-only edge on a source-backed rule would be reconciled away, and `downweight` deliberately works at any authority (only promote/reject are gated by `assert_ai_provisional`, which raises the named `IllegalAuthorityTransitionError` without touching the graph). Manual promotion caps confidence at `peer-reviewed` (0.6); the higher tiers (`production-validated` 0.8, `battle-tested` 1.0) are reachable only through empirical graduation, never a command.
