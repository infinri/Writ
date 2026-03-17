# Contributing to Writ

Rules in the Writ knowledge graph are governed by a structured authoring process. This document defines the workflow for adding, editing, and deprecating rules in a multi-author environment.

## Adding a Rule

1. Run `writ add` to enter the interactive authoring flow.
2. The tool validates your input against the Pydantic schema and rejects malformed rules before they reach the graph.
3. After validation, the tool runs the new rule's text through the retrieval pipeline and presents:
   - **Relationship suggestions**: top-5 similar rules as candidates for DEPENDS_ON, SUPPLEMENTS, or RELATED_TO edges.
   - **Redundancy warnings**: any existing rule with >= 0.95 cosine similarity is flagged. You may proceed, but consider whether the new rule adds distinct value.
   - **Conflict warnings**: if a CONFLICTS_WITH path exists to the new rule via accepted edges.
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
