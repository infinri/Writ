# Errata

Corrections to figures this project has published. Two kinds of error end up here.

**Errata with a consumer** stay in the README as well, because someone may have cited the
wrong number and needs to see the correction where they found the claim: the retrieval
nondeterminism disclosure and the monthly-review event count are both in that category.

**Errata without a consumer** live only here. Nobody was misled by them, and confessing
every one of them in the README teaches a reader to discount the self-criticism that does
matter. That trade is the reason this file exists.

---

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
