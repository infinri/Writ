# Retrieval miss triage: the 42 misses behind hit-rate@5 = 0.7824

Measured 2026-08-05 on the production 287-rule corpus (reproduces the published
2026-08-01 number exactly: same rate, same 42 miss ids). Every miss was classified
by reading the expected rule's full text against the query and the five returned
candidates. One primary cause per miss.

## Counts by cause

| Cause | Count | Reading |
|---|---|---|
| ranking | 31 | the right rule's text matches the query; a wrong rule outscores it |
| ambiguity / debatable ground truth | 5 | returned answers are defensible or the fixture label is wrong |
| vocabulary-gap | 5 | the rule's text genuinely lacks the query's terms |
| corpus-gap | 1 | the expected rule id does not exist (Q93: SEC-INJ-REGEX-001) |

## The two systemic patterns inside the 31 ranking misses

1. **Sibling-rule collisions** (~2/3 of ranking misses): near-duplicate ids in the
   same family win on a shared token while meaning something else
   (SEC-AUTH-TOKEN-001 loses to -002 on the word "secure"; SEC-AUTH-HASH-001 to
   -002; SEC-INJ-XSS-001 to SSTI-001; CLEAN-DEAD-001 to CLEAN-RETURN-001 on the
   word "return").
2. **The Magento cluster as a false-positive magnet** (~7 misses): FW-M2-* rules
   outscore the correct generic rule on unrelated queries (incident review Q166 at
   0.97, N+1 Q148, CSRF Q90, IDOR Q52/Q98, decorators Q99, side effects Q195) with
   no real vocabulary overlap. This is a pipeline/scoring artifact worth its own
   investigation, not per-rule text edits.

## Ground-truth fixture defects found (fixture corrections pending review)

- Q93 expects SEC-INJ-REGEX-001, which does not exist anywhere in the corpus;
  SEC-VAL-REGEX-001 covers ReDoS explicitly and was returned #1 at 0.92. Dataset
  defect, not a retrieval defect.
- Q42 and Q65 expect FW-M2-RT-002, whose text contains no concurrency or
  crash/partial-state language; ENF-SYS-003 and ERR-GRACEFUL-002 are the textually
  correct answers.
- Q43 expects FW-M2-RT-005 (queue XML declarations) for an idempotency question;
  SCALE-QUEUE-002 is the exact match and was returned #1.
- Q41 (CLEAN-ERR-001 for "graceful degradation") is debatable; ERR-FALLBACK-001
  reads as the more natural answer and was returned at rank 2.

Any fixture correction changes the published metric and will be made transparently
(before/after both reported), not silently.

## Highest-leverage corpus edits for the 5 vocabulary gaps

1. SEC-INJ-SQL-001: add "positional placeholder", "? / :name / %s", and the word
   "injection" itself (absent from the text today). Closes Q10, strengthens Q1.
2. SEC-AUTHZ-MASS-001: add the literal phrase "mass assignment" (today it exists
   only in the rule id). Closes Q100.
3. DRY-CONFIG-001: add a REST/GraphQL multi-surface drift example. Closes Q56.
4. SEC-INJ-XSS-003: add innerHTML/DOM vocabulary and disambiguate "comment"
   (user comments vs code comments). Closes Q179.
5. ERR-HANDLE-002: add a symptom-oriented bridge sentence ("postmortem shows only
   the wrapping exception; the original cause is gone"). Closes Q175.

Full per-miss table preserved in the session record; per-miss justifications were
verified against writ-corpus.cypher rule text, not summaries.
