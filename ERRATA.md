# Errata

Corrections to figures this project has published. Two kinds of error end up here.

**Errata with a consumer** stay in the README as well, because someone may have cited the
wrong number and needs to see the correction where they found the claim: the retrieval
nondeterminism disclosure and the monthly-review event count are both in that category.

**Errata without a consumer** live only here. Nobody was misled by them, and confessing
every one of them in the README teaches a reader to discount the self-criticism that does
matter. That trade is the reason this file exists.

---

## 2026-08-14: the published corpus counts stopped matching the corpus

Three related figures, all corrected on 2026-08-14, all checkable against
`writ-corpus.cypher` in this repository.

- **The rule total.** README, HANDBOOK, `docs/reference/graph-schema.md` and the
  marketplace listing copy all published 287 rules in the present tense. The dump holds
  288. The 288th arrived with the corpus commit dated 2026-08-14, so the published figure
  was correct until the same day it was corrected; it is recorded here because a document
  and the artifact it describes shipped from the same commit disagreeing.
- **The census.** HANDBOOK's shipped-corpus table and the schema reference's dump
  paragraph both published a 2026-07-31 reading of 464 nodes and 731 edges. That reading
  was accurate for the dump as it stood then. The edge count diverged on 2026-08-06 and
  never came back: today's dump holds 468 nodes and 1,268 edges, so for eight days the
  number a reader was most likely to cite as "the graph" understated it by more than five
  hundred edges. Per-type rows drifted with it (`RELATED_TO` 69 to 512, `BELONGS_TO` 331
  to 401, Skill 15 to 16, Technique 11 to 14).
- **The mandatory count.** The schema reference published "287 of which 33 mandatory".
  The dump held 32 mandatory rules on the date that census was labelled and at every corpus
  commit since. README carried the same 33 and was corrected to 32 on 2026-08-08; the
  schema reference was not, so from that date the project shipped both figures at once and
  one of them disagreed with the artifact they were both describing.

**These have a consumer.** Anyone who cited 287 as the current rule total, quoted the
464-node / 731-edge census as the shipped graph, or took 33 as the size of the mandatory
floor cited something this tree does not say. The mandatory figure is the worst of the
three: it disagreed with the dump on the day it was written, it outlived the correction
that fixed the same number in the README, and the floor is the claim this project asks
readers to trust most. No standing README disclosure is added for these, because each wrong
figure is replaced in the sentence that carried it: a reader who goes back to where they
found the number finds the corrected one in its place. The nondeterminism entry below needs
a standing note for the opposite reason, that no current sentence can tell a reader the
column they cited was a sample.

## 2026-08-14: the handbook contradicted itself on how many sub-agent roles ship

HANDBOOK section 8 said Writ ships "five named roles" and listed five. Section 9's census
table in the same document said `SubagentRole 7`, and the schema reference repeated the 7.
The corpus holds 5.

The 7 was a true count of the dump it was taken from. The corpus commit dated 2026-08-03
records five, and the census table was never re-counted, so the document disagreed with
itself for eleven days. Section 8 was the correct half.

**This one has a consumer.** A reader who took the census table at its word could have
cited seven typed roles, and the table is the more citable of the two halves precisely
because it reads as a machine-produced count. The correction lands in that table, and the
README never published a role count, so nothing is added there.

## 2026-08-14: the package description named an algorithm the code disclaims

`pyproject.toml` described the pipeline as "BM25 + ANN + graph + RRF", and
`.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` both spelled it out as
"reciprocal-rank fusion". The ranking stage is not Reciprocal Rank Fusion. It is plain
reciprocal rank, `1/(rank+1)`, followed by a weighted linear sum, and `normalize_ranks` in
`writ/retrieval/ranking.py` says so in its own docstring. `tests/test_rrf_label_accuracy.py`
exists to ban the bare acronym, but its scan list covers three Python files and no manifest,
so the label the code refuses to make survived in the three files that carry the project's
description outward.

**This one has a consumer.** The description is what `claude plugin install` shows and what
a reader of the marketplace entry sees, and it was queued to ship to PyPI unchanged at the
next publish. Anyone who read it learned a wrong fact about the ranking stage from the most
authoritative-looking place to learn it, a manifest. The correction lands in the three
manifests themselves; the README never used the label, so nothing is added there.

It is also the second time a `pyproject.toml` field has shipped a claim matching neither
the README nor the code: the 2026-08-08 entry below records the same file carrying a hook
count that agreed with nothing. One field drifting is a miss; two is a surface nobody is
reading. All three manifests now carry an identical corrected description and an identical
keyword list, so a future drift in one is visible as a difference from the other two.

## 2026-08-08: the status block was stale

The Status section read `v1.6.0 (2026-08-01)` and claimed "every benchmark re-measured",
in a document whose benchmarks had since been re-measured on 08-05 and 08-06 and whose
header already pointed at a v1.7.0 changelog.

No consumer: a stale version heading misleads nobody into citing a wrong figure. Recorded
because a document arguing that stale claims are the failure mode should keep its own
count of them.

## 2026-08-08: published figures that disagreed with their own sources

Four, all corrected in v1.7.0 and all checkable against artifacts already in this repo.

- The opening token-cost figures read 13,876 and 1,174,142 tokens. `SCALE_BENCHMARK_RESULTS.md`
  and the README's own scale table both say 14,738 and 1,190,649. The document contradicted
  itself three sections apart.
- The monthly-review citation described 7,058 as "logged events in the window". The review
  records its starting line count as "not captured", so no within-window count exists; 7,058
  is the cumulative line count at the close. **This one has a consumer:** anyone who quoted
  it as a within-window event volume quoted something that was never measured.
- Test-suite counts read 367 modules and ~5,700 tests against an actual 398 modules and 7,137
  collected at the time of the correction (2026-08-08); the suite has grown since, so treat both
  figures as dated. They were stale in three files at once.
- `pyproject.toml`'s package description carried a hook count matching neither the README nor
  the wiring, and it ships to PyPI independently of any README edit.

## 2026-08-06: retrieval numbers published before the pipeline was deterministic

Every retrieval-quality column published before 2026-08-06 was one draw from a distribution
rather than a measurement. A `set` union merging BM25 and vector candidates left iteration
order to per-process randomization, and that order decided tie-breaks through scoring and the
budget cut. Thirty of 193 gold queries returned a different top-5 set depending on the seed.

**This one has a consumer**, so it is also disclosed in the README: anyone who cited a
pre-08-06 column cited a sample, not a value. The corrected figures landed at the low end of
the old spread.
