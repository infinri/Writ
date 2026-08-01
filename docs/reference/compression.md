# Compression and abstractions

The summary-mode compression layer: clustering non-mandatory rules into `Abstraction` nodes. Source of truth: `writ/compression/{clusters,abstractions}.py` (import the submodules directly; the package has no facade).

## Pipeline (`run_compression`, invoked by `writ compress` or `writ import-markdown --compress`)

1. Embed every **non-mandatory** rule (`sentence-transformers`, lazily imported inside the function; it is the `[fallback]` extra, never a production dependency, so importing the module costs nothing).
2. Cluster with both HDBSCAN and k-means, pick the winner by silhouette score; HDBSCAN wins when it clusters at all. Singleton clusters are demoted to ungrouped (INV-SINGLETON). Known bound: HDBSCAN is O(n^2) worst case; the stated "bounded by domain rule count" assumption is not enforced anywhere, so very large corpora pay real clustering time.
3. Write `Abstraction` nodes (`ABS-<DOMAIN>-<NNN>`, domain = the cluster's most common member domain) plus one `ABSTRACTS` edge per member. An abstraction's `summary` is literally the statement of the centroid-nearest rule (INV-SUMMARY): no LLM, no concatenation. `compression_ratio` is a chars/4 heuristic, not a real token count.
4. Recompression is delete-then-recreate, project-scoped (INV-IDEMPOTENT).

## The artifact (the dependency-free path)

Every compression run also writes `bible/abstractions.json`, deterministically sorted so the committed file is stable. Neo4j remains canonical (ARCH-SSOT-001); the artifact is a reproducible cache: `materialize_abstractions_from_artifact` rebuilds the whole Abstraction layer from it with zero clustering and zero ML dependencies (this is what a normal ingest uses), and it refuses an artifact whose declared project mismatches the target (so a stale artifact can never cross-delete another project's layer). `writ validate` checks the artifact's rule ids against live Rules.

## Where abstractions surface

Only in **summary mode** (query budget under 2,000 tokens): `apply_context_budget` substitutes a covering abstraction's summary for grouped rules; ungrouped rules fall back to statement + trigger. Standard and full modes never use them. Abstractions are graph-derived, so they are exempt from source-parity checks and are absent until someone runs compression: a fresh install without `--compress` simply has none, and summary mode degrades gracefully.

## Failure behavior

`writ compress` exits 1 with an install hint when `sentence-transformers` is missing; `writ import-markdown --compress` warns and completes the ingest without the layer. A partial `--only` import never triggers abstraction regeneration.
