# M0-01d: semantic review of the 288 V1 rules

**Status:** prepared; **0 of 288 rules reviewed.** Generated once by `python -m writ_m0.audit.rule_review` (run from `m0/`); edit the CSV files directly. The generator refuses to overwrite this directory.

## What to decide per rule

- `reviewed_disposition`: advisory, obsolete, duplicate, conflicting, candidate_enforced or unverified.
- `intent_survives_in_v2`: yes/no. Obsolete V1 mechanisms often carry intent worth keeping.
- `v2_destination`: retrieval registry (advisory), a Capability Pack (name it), a policy statement, or discard.
- `enforcement_verified`: yes only if you confirmed a live V1 enforcement point actually blocks; otherwise no or n/a. A mechanical path or code reference is not proof (finding A-07).
- `conflicts_or_duplicates`: rule ids, checked against the related-rule list and your own reading.
- `reviewer`, `reviewed_at`, `rationale`: required for every reviewed row.

Claude must not fill reviewer columns. Batches are domain-grouped, at most 25 rules each.

| Batch | Domains | Rules | Reviewed | Status |
|---|---|---|---|---|
| [batch-01](batch-01.md) | api-design | 12 | 0 | not started |
| [batch-02](batch-02.md) | architecture | 25 | 0 | not started |
| [batch-03](batch-03.md) | architecture | 3 | 0 | not started |
| [batch-04](batch-04.md) | code-quality | 25 | 0 | not started |
| [batch-05](batch-05.md) | code-quality, communication, database | 24 | 0 | not started |
| [batch-06](batch-06.md) | documentation | 8 | 0 | not started |
| [batch-07](batch-07.md) | enforcement | 19 | 0 | not started |
| [batch-08](batch-08.md) | frameworks, languages, meta-authoring | 23 | 0 | not started |
| [batch-09](batch-09.md) | performance | 19 | 0 | not started |
| [batch-10](batch-10.md) | process, research | 23 | 0 | not started |
| [batch-11](batch-11.md) | scaling | 10 | 0 | not started |
| [batch-12](batch-12.md) | security | 25 | 0 | not started |
| [batch-13](batch-13.md) | security | 25 | 0 | not started |
| [batch-14](batch-14.md) | security | 25 | 0 | not started |
| [batch-15](batch-15.md) | security, testing | 22 | 0 | not started |
