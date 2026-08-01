# Contributing to Writ

Rules in the Writ knowledge graph are governed by a structured authoring process. This document defines the workflow for adding, editing, and deprecating rules in a multi-author environment.

## Adding a rule

1. Run `writ add` to enter the interactive authoring flow.

2. The tool runs these gate checks in order, refusing to continue on any hard failure:

   - **ID collision.** `check_id_collision` queries Neo4j for the proposed `rule_id`. If it already exists, the flow aborts with `RuleIdCollisionError` (use `writ edit` instead, or pick a new ID). This runs before schema validation so re-used IDs fail fast and `MERGE` cannot silently overwrite an existing node.

   - **Schema validation.** Pydantic check against `writ.graph.schema.Rule`. Malformed rules are rejected before any graph write.

   - **Redundancy.** Any existing rule with at least 0.95 cosine similarity is flagged. This is advisory: you may proceed, but consider whether the new rule adds distinct value.

   - **Novelty and specificity.** Covered by the redundancy threshold plus reviewer judgment on `trigger` specificity.

   - **Conflict.** `CONFLICTS_WITH` edges to the new rule are reported after edge creation.

3. After the gate passes, the tool runs the new rule's text through the retrieval pipeline and presents relationship suggestions: the top five similar rules, offered as candidates for `DEPENDS_ON`, `SUPPLEMENTS`, or `RELATED_TO` edges.

4. Accept or reject each suggested relationship. No edges are created automatically.

5. Submit a PR containing the `writ add` output and any edge decisions for review.

## Editing a rule

1. Run `writ edit <rule_id>` to load the current rule and modify fields.
2. The same validation, redundancy, and conflict checks run on the updated text.
3. Edits use `MERGE` (idempotent). Running the same edit twice produces no change.
4. Submit a PR with the edit for review.

## Deprecating a rule

1. Do not delete rules. Instead, create a `SUPERSEDES` edge from the replacement rule to the deprecated rule.
2. The deprecated rule remains in the graph for audit and historical reference.
3. Run `writ add` for the replacement rule, then create the `SUPERSEDES` edge when prompted.

## Resolving conflicts

`CONFLICTS_WITH` edges require human resolution. When two rules conflict:

1. Both rules remain in the graph with the `CONFLICTS_WITH` edge visible.
2. The conflict appears in `writ validate` output and the `/conflicts` API endpoint.
3. The domain owner for the conflicting rules is responsible for resolution: deprecate one rule via `SUPERSEDES`, amend one rule, or document the conflict as intentional (for example, domain-specific exceptions).
4. Automatic merge of conflicting rules is never performed.

## Review process

- All rule additions and edits require PR review.
- The PR should include the `writ add` or `writ edit` terminal output showing validation results, suggestions, and any warnings.
- The reviewer verifies that relationship decisions are reasonable and that redundancy or conflict warnings were addressed.
- Domain-specific rules should be reviewed by someone familiar with that domain.

## AI proposed rules

When an AI agent (typically Claude during a Work mode session) discovers a recurring pattern that does not yet have a rule, it can submit a candidate via `POST /propose`. These rules go through the same structural gate as human additions, plus a few extra constraints:

- Authority is force-set to `ai-provisional`. The proposer cannot bypass this.
- Confidence is force-set to `speculative`.
- If the rule is mandatory, it must declare a `mechanical_enforcement_path`. Otherwise it cannot be enforced and would just be advisory in disguise.

Use `writ review` to triage AI proposed rules:

- `writ review` (no argument) lists every rule with `authority = ai-provisional`.
- `writ review <rule_id>` shows full detail plus the origin context (the task description and query that triggered the proposal).
- `writ review <rule_id> --promote` moves the rule to `authority = ai-promoted` and `confidence = peer-reviewed`.
- `writ review <rule_id> --reject` deletes the rule from the graph.
- `writ review <rule_id> --downweight` pins confidence to `speculative`.
- `writ review --stats` prints counts grouped by authority.

Promotion is a deliberate human act. The system does not promote on its own; frequency tracking only changes the runtime ranking weight, not the stored authority.

## Monthly review

Run on the first Monday of each month. Goal: turn the friction log into actionable graduation, trim, and rubric-refinement decisions.

### Cadence

1. Capture the month's signal:

   ```bash
   writ analyze-friction --rule-effectiveness --since 30 --json > review-$(date +%Y-%m).json
   writ analyze-friction --skill-usage --since 60
   writ analyze-friction --playbook-compliance --since 30
   writ analyze-friction --graduation-candidates
   writ analyze-friction --trim-candidates --since 90
   writ analyze-friction --quality-judge-false-positives --since 30
   ```

2. Triage rule effectiveness:
   - Stick rate below 50 percent: trigger too broad, rationalization unaddressed, or rule wrong. File an issue or revise the rule.
   - Stick rate at 85 percent or higher with low rationalization: graduation candidate (Step 4).

3. Process trim candidates:
   - Rules with fewer than 5 activations in 90 days and 0 denials: deprecate or consolidate.
   - Skills with fewer than 2 loads in 60 days: deprecate.
   - Document the rationale on the issue.

4. Process graduation candidates:
   - Promote eligible rules to canonical tier in the graph.
   - Update authority and confidence fields via `writ edit`.

5. Refine quality judge rubrics:
   - Override rate above 25 percent on any rubric: the rubric is a false-positive generator. Edit the rubric prose in `hooks/scripts/writ-quality-judge.sh`.

6. File the review notes in `docs/monthly-reviews/YYYY-MM.md` (copy from `TEMPLATE.md`).

### Why monthly

Friction signal is noisy at the per-session level. A 30, 60, or 90 day rolling window smooths over pressure runs, one-off pressure tests, and individual exploratory sessions. Acting on a week of data tunes against noise; acting on a month of data tunes against signal.

## Regression floors and benchmark gates

Regression floors and benchmark thresholds are only meaningful when CI enforces them. **A gate that exists in a file but is not run by a workflow does not exist.**

The history that produced this rule: v1.0.0 reported the contractual benchmark suite as passing, but `bench_targets.py` had drifted from the regression floors actually enforced at release time, no CI workflow ran the bench, and the pre-push hook in `.pre-commit-config.yaml` was never installed locally. Two files asserted different thresholds against the same ground truth; neither was load-bearing. The v1.1.0 work re-established the gate by consolidating thresholds into `tests/fixtures/regression_floors.py`, recalibrating budgets to measured values with the basis recorded in the source comment, fixing the install path so the bench measures the production ONNX code path, and adding a hard-blocking `.github/workflows/pr.yml` that runs `make test` and `make bench` against a Neo4j service container on every PR.

When adding a new threshold, budget, or regression floor:

1. **Confirm a workflow runs the test that asserts it, on every PR, and is required for merge.** Inspect `.github/workflows/pr.yml`. If your new threshold is in a file that workflow already invokes (`tests/`, `benchmarks/bench_targets.py`), the workflow will pick it up. If not, the workflow needs a step that does.
2. **If no such workflow exists, add it in the same PR.** Do not add the threshold first and the enforcement later. The intermediate state (file exists, workflow does not run it) is structurally the same as the v1.0.0 drift this rule was written to prevent.
3. **Reviewers should reject PRs that add unenforced gates.** An assertion in a file that no required check runs is documentation of intent, not enforcement of intent. They are different things and the file format does not distinguish them.

Floor and threshold locations:

- **Retrieval-quality floors** (MRR@5 ambiguous-set, hit-rate) live in `tests/fixtures/regression_floors.py`. Both `tests/test_graph_proximity.py` and `benchmarks/bench_targets.py` import from there. Do not declare floors locally in either file.
- **Performance budgets** (cold-start, BM25 p95, vector p95, ranking p95, end-to-end p95, integrity check, ingestion, memory) live in `benchmarks/bench_targets.py` with a measurement-basis comment on the cold-start budget (the most recently calibrated one) as the model. When adding a new performance budget, include the comment block: measurement date, corpus size, code path, observed distribution, reasoning for the chosen value. "Feels generous" is not a reason; "feels tight" is not a reason. The next maintainer needs the evidence, not the intuition.

When raising or lowering an existing floor or budget:

- Re-measure with the methodology in the existing comment (same corpus, same code path).
- Append a row to the phase-by-phase history docstring in `tests/fixtures/regression_floors.py` (for floors) or update the measurement-basis comment block (for budgets). The history is append-only; do not delete prior rows even when superseded.
- Land the change in the same PR as whatever justifies it. A standalone "tighten the floor" PR without an enforcing measurement is the same pattern as the v1.0.0 drift.

## Related documents

- `HANDBOOK.md` covers the architecture and the structural gate in detail; precise contracts live under `docs/reference/` (graph-schema, session-and-gates, testing).
- `docs/monthly-reviews/TEMPLATE.md` is the template for monthly review notes.
- `plan.md` format requirements are validated by `hooks/scripts/validate-exit-plan.sh` on `/plan` exit: required sections are `## Files`, `## Analysis`, `## Rules Applied`, `## Capabilities`.
