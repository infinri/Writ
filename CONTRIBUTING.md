# Contributing to Writ

Rules in the Writ knowledge graph are governed by a structured authoring process. This document defines the workflow for adding, editing, and deprecating rules in a multi-author environment.

## Adding a Rule

1. Run `writ add` to enter the interactive authoring flow.
2. The tool runs these gate checks in order, refusing to continue on any hard failure:
   - **ID collision**: `check_id_collision` queries Neo4j for the proposed `rule_id`. If it already exists, the flow aborts with `RuleIdCollisionError` (use `writ edit` instead, or pick a new ID). This runs before schema validation so re-used IDs fail fast and MERGE cannot silently overwrite an existing node.
   - **Schema validation**: Pydantic model check against `writ.graph.schema.Rule`; malformed rules are rejected before any graph write.
   - **Redundancy**: any existing rule with >= 0.95 cosine similarity is flagged. Advisory -- you may proceed, but consider whether the new rule adds distinct value.
   - **Novelty / specificity**: covered by the redundancy threshold plus reviewer judgment on `trigger` specificity.
   - **Conflict**: CONFLICTS_WITH edges to the new rule (via accepted relationships) are reported after edge creation.
3. After the gate passes, the tool runs the new rule's text through the retrieval pipeline and presents relationship suggestions (top-5 similar rules as candidates for DEPENDS_ON, SUPPLEMENTS, or RELATED_TO edges).
4. Accept or reject each suggested relationship. No edges are created automatically.
5. Submit a PR containing the `writ add` output and any edge decisions for review.

## Editing a Rule

1. Run `writ edit <rule_id>` to load the current rule and modify fields.
2. The same validation, redundancy, and conflict checks run on the updated text.
3. Edits use MERGE (idempotent) -- running the same edit twice produces no change.
4. Submit a PR with the edit for review.

## Deprecating a Rule

1. Do not delete rules. Instead, create a SUPERSEDES edge from the replacement rule to the deprecated rule.
2. The deprecated rule remains in the graph for audit and historical reference.
3. Run `writ add` for the replacement rule, then create the SUPERSEDES edge when prompted.

## Resolving Conflicts

CONFLICTS_WITH edges require human resolution. When two rules conflict:

1. Both rules remain in the graph with the CONFLICTS_WITH edge visible.
2. The conflict appears in `writ validate` output and the `/conflicts` API endpoint.
3. The domain owner for the conflicting rules is responsible for resolution: either one rule is deprecated (SUPERSEDES), one is amended, or the conflict is documented as intentional (e.g., domain-specific exceptions).
4. Automatic merge of conflicting rules is never performed.

## Review Process

- All rule additions and edits require PR review.
- The PR should include the `writ add` or `writ edit` terminal output showing validation results, suggestions, and any warnings.
- The reviewer verifies that relationship decisions are reasonable and that redundancy/conflict warnings were addressed.
- Domain-specific rules should be reviewed by someone familiar with that domain.
