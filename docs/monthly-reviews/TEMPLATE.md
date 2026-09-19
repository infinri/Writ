# Writ monthly review: YYYY-MM

Reviewer: <name>
Period: YYYY-MM-01 through YYYY-MM-DD
Friction log line count at start: <N>
Friction log line count at end: <N>

## Rule effectiveness

`writ analyze-friction --rule-effectiveness --since 30`

| rule_id | activations | stick rate | rationalizations | action |
|---|---:|---:|---:|---|
| ENF-X-001 |  |  |  | keep / revise / trim |

Notes:

## Skill usage

`writ analyze-friction --skill-usage --since 60`

| skill_id | loads | completion rate | action |
|---|---:|---:|---|
| SKL-A |  |  | keep / deprecate |

Notes:

## Playbook compliance

`writ analyze-friction --playbook-compliance --since 30`

| playbook_id | runs | compliant | common skips | action |
|---|---:|---:|---|---|
| PBK-A |  |  |  | keep / refine |

Notes:

## Graduation candidates

`writ analyze-friction --graduation-candidates`

| rule_id | days stable | current | recommended | decision |
|---|---:|---|---|---|
| ENF-Y-001 |  | probationary | canonical | promote / hold |

## Trim candidates

`writ analyze-friction --trim-candidates --since 90`

| entity | type | activations | last seen | decision |
|---|---|---:|---|---|
| ENF-Z-001 | rule |  |  | trim / consolidate / keep |

## Quality-judge false positives

`writ analyze-friction --quality-judge-false-positives --since 30`

| rubric | fails | overrides | rate | action |
|---|---:|---:|---:|---|
| plan-specificity |  |  |  | refine / keep |

## Decisions taken this month

- (one bullet per concrete change: rule edited, rule deprecated, rubric refined, etc.)

## Action items for next month

- [ ] (item)
