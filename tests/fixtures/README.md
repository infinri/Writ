# Test Fixtures

This directory contains test data for Writ's test suite.

## Phase 1 (Schema Validation)

- `valid_rule.json` -- Well-formed rule with all required fields
- `valid_enf_rule.json` -- ENF-* rule with `mandatory: true`
- `missing_field_rules/` -- One JSON per required field, each missing that field
- `invalid_type_rules/` -- Wrong types for each field
- `edge_cases.json` -- Empty strings, long strings, special characters

## Phase 4 (Integrity)

- Conflict fixtures: two rules with CONFLICTS_WITH edge
- Orphan fixture: isolated rule with no edges
- Stale fixture: rule with last_validated past staleness_window
- Redundancy fixture: two near-identical rules

## Phase 5 (Retrieval Quality)

- `ground_truth_queries.json` -- 50+ human-authored queries with expected rule_ids
