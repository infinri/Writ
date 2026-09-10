# Anthropic plugin marketplace submission packet

Maintainer reference for submitting `writ@writ` to the official Anthropic plugin marketplace. The submission URL is https://clau.de/plugin-directory-submission, which is what the community repo's own close-external-prs bot tells submitters to use; an earlier version of this doc gave https://claude.ai/settings/plugins/submit, which is not the address their automation advertises. The positioning appendix at the bottom absorbs the former PROMOTIONAL-BRIEF.md.

> **Listing status: MEASURED 2026-09-10, and re-checkable in one command.**
> This block used to be an external claim carrying its own warning that no command in this
> tree could confirm it. That is why it sat five weeks stale and was nearly used as the basis
> for a README change. It is now a measurement with the command that produced it:
>
> ```
> gh api repos/anthropics/claude-plugins-community/contents/.claude-plugin/marketplace.json \
>   -H "Accept: application/vnd.github.raw"
> ```
>
> That manifest holds 2282 entries. Exactly one matches `writ`, pinned by explicit sha to
> `d3c33d3e35adc30dc4448d0829801f5a8a9138da`, with a description advertising
> "726x context reduction" and "276 rules across 12 domains".
>
> **THE PIN RESOLVES, which is worse than "install-broken".** `git cat-file -t` in this clone
> reports "no such commit", because the history scrub orphaned it locally, but
> `gh api repos/infinri/Writ/commits/d3c33d3e...` returns it as "Release v1.0.0". So
> `claude plugin install writ@claude-community` does NOT fail to fetch. It succeeds and
> delivers v1.0.0, which predates every 1.5.1 install fix. A failed fetch produces a bug
> report; a silent four-month-old install produces a bad first impression with no signal at
> all. The earlier wording invited a reader to assume the route was inert. It is not.
>
> **Every advertised number is wrong**, and by more than age. Against the tracked
> `writ-corpus.cypher` today: 288 rules, not 276; 32 mandatory, not 30; 16 rule domains, not
> 12. The `726x` figure is not refreshed here and is not carried into the copy below, for the
> reason `SCALE_BENCHMARK_RESULTS.md` states about its own successor figure: a multiplier of
> that shape is measured against pasting a 1.19-million-token corpus into every message, "a
> theoretical ceiling, not something anyone does", and against a realistic hand-curated
> instructions file the per-turn cost is roughly comparable.
>
> **THE ROOT CAUSE IS A FREEZE, NOT NEGLECT, and it changes the ask.** There IS an auto-bump
> bot, `.github/workflows/bump-plugin-shas.yml`, which opens a per-entry PR to advance each
> listing's sha. `writ` is one of 49 slugs in `.github/freeze-shas.txt`, whose header says:
>
> > "Point-in-time snapshot (2026-06-13) of entries that genuinely fail 'validate-plugins' at
> > upstream HEAD (manifest lost at new SHA, or a field the bump surfaces as an error).
> > Freezing avoids bumping them into a red PR ... remove a slug once its upstream is fixed to
> > let it freshen again."
>
> "Manifest lost at new SHA" is this repo's history scrub: the bumper looked for
> `.claude-plugin/marketplace.json` at the new commit, did not find it, and held the entry at
> v1.0.0 rather than open a perpetually-red PR. So the four-month drift is a switched-off
> bump, not an unnoticed listing.
>
> **The freeze condition no longer holds.** Verified 2026-09-10: the manifest is present at
> public `main` (`e6086594`, pushed 2026-08-15) at 1057 bytes, parses, and declares plugin
> `writ`; `tests/plugin/test_plugin_validate_cli.py` passes locally. That is exactly the state
> the freeze file names as grounds for removal.
>
> **Nothing lifts it automatically.** `bump-plugin-shas.yml` only READS the list, and
> `owner-liveness-sweep.yml` states "nothing is edited or removed". Removal is a manual edit
> inside Anthropic's pipeline. The community repo is a nightly READ-ONLY MIRROR of that
> pipeline, so `freeze-shas.txt` cannot be reached by a pull request; external PRs are
> auto-closed unless the author has write access.
>
> **Use the URL their own automation advertises**, not the one this doc used to give. The
> close-external-prs bot routes submitters to https://clau.de/plugin-directory-submission.
>
> **Practical conclusion, unchanged and reinforced.** Do not promote `writ@claude-community`
> anywhere, least of all on the README's first screen, until the freeze is lifted and a bump
> lands. The working route is the self-hosted one the README already documents:
> `claude plugin marketplace add infinri/Writ`.
>
> **Sequencing that matters:** a de-freeze bumps the entry to whatever `main` holds at that
> moment. Public `main` is 2026-08-15 while local work runs well ahead of it, so PUSH FIRST
> or the bump lands on a mid-August tree. Ask for the de-freeze after the push, not before.

## Pre-submission checklist

- [x] **Manifest validates**: `claude plugin validate <install>` exits 0 with no warnings (pinned by `tests/plugin/test_plugin_validate_cli.py`).
- [x] **Marketplace name not reserved**: `writ` is not on the reserved list.
- [x] **Plugin source publicly reachable**: `marketplace.json` points at `./` and the repo is public at https://github.com/infinri/Writ.
- [x] **README documents install + usage**: the "Install as a Claude Code plugin" section ships the collapsed sequence: `claude plugin marketplace add`, `claude plugin install writ@writ`, then the one absolute `bootstrap-plugin.sh` command Writ itself prints on first session (that run also patches `~/.claude` and installs the slash commands, so there is no separate patch step and no install-path lookup). Prerequisites are Python 3.11+ and Docker only; `jq`, `curl` and `envsubst` are no longer required. Full detail in `docs/install.md`.
- [x] **Agents load**: measured `Agents (5)` on Claude Code 2.1.220 with roles auto-discovered from `agents/` (commit `a56ca1e`).
- [x] **License OSI-approved**: MIT.
- [x] **No secrets in repo**: `writ.toml` is gitignored; the shipped template carries only the documented dev Neo4j default.
- [ ] **Fresh-install smoke**: dated result, needs a re-run before submitting. `tests/plugin/test_fresh_install_smoke.py` ran green with `WRIT_INTEGRATION_TESTS=1` on 2026-08-01 (clone, marketplace add, install, bootstrap, health), and bootstrap was made idempotent across container provenance the same day (a pre-existing `writ-neo4j` container is reused instead of colliding with `compose up`). That run measured the tree as it stood on 2026-08-01, not the commit being submitted, so re-run it against the submission sha and re-date this line before opening the form.
- [ ] **Screenshots captured** (below).
- [x] **Existing community listing located and its state measured**: done 2026-09-10, see the status block above. The entry exists, its sha resolves to "Release v1.0.0", and all three advertised numbers are wrong. This settles what the re-submission has to correct; it does not settle the re-submission, which only the operator can perform.
- [ ] **Push `main`**: do this BEFORE asking for the de-freeze. A bump lands on whatever `main` holds at that moment, so asking while public `main` trails local work bumps the listing to a stale tree and spends the request.
- [ ] **De-freeze requested**: the one action no command in this tree can take. `writ` is held in `.github/freeze-shas.txt`; that file is a nightly mirror of Anthropic's internal pipeline, external PRs are auto-closed, and no workflow removes a slug on its own. Ask through https://clau.de/plugin-directory-submission using the message under "De-freeze request" below. Until it lands, `writ@claude-community` serves v1.0.0 and must not be promoted.

## Listing copy

**Name**: `writ`

**Tagline** (<= 120 chars):
> Hybrid-RAG rule retrieval plus workflow gates for Claude Code: the right rules per prompt, no risky writes before an approved plan.

**Short description**:
> Writ is a Claude Code harness with two co-equal layers. A librarian retrieves the rules that fit the current task through a five-stage hybrid pipeline (BM25 + vector + graph traversal + weighted ranking, with an abstention gate) over a Neo4j knowledge graph: sub-millisecond ranked retrieval, and retrieved tokens that stay roughly flat as the rulebook grows rather than scaling with it. A process keeper of hook scripts and a session state machine enforces mode-based workflow gates: plan approval, then test skeletons, then implementation, and approval requires a token only the user's keystroke produces. 288 rules ship out of the box across 16 domains, led by security (76), code quality (45), architecture (28), testing (21), process (19) and performance (19), with authoring tooling to grow your own.

NO REDUCTION MULTIPLIER IN THE COPY, deliberately. The live listing leads with "726x" and an
earlier draft here carried "749x". `SCALE_BENCHMARK_RESULTS.md` measures that class of figure
against pasting the entire 10,000-rule corpus (1.19 million tokens) into every message, and
says so itself: "a theoretical ceiling, not something anyone does, since no context window
holds it." Against the realistic comparison, a hand-curated instructions file of about 5,000
tokens, the per-turn cost is roughly comparable while covering the whole shipped rulebook.
The flatness is the honest claim; the multiplier is a number about a baseline nobody uses.

(Counts DERIVED from `writ-corpus.cypher`, the tracked canonical dump, on 2026-09-10:
288 rules, 32 mandatory, 16 rule domains. Re-derive rather than copy this line if the corpus
has moved. Do not count `bible/`: it is a gitignored derived export holding only 160
rule-shaped files, and citing it is how one fact became two published numbers before.)

**Long description**: mirror README "The problem" + "What Writ does about it" verbatim (kept current there; do not fork the text here).

**Category**: Development workflows. **Keywords**: mirror `.claude-plugin/plugin.json`, which as of 2026-08-14 holds `claude-code`, `rag`, `rules`, `enforcement`, `neo4j`, `fastapi`, `hooks`, `workflow`, `code-quality`, `ai-tooling`, `governance`, `guardrails`, `knowledge-graph`, `tdd`. That list is kept byte-identical in `pyproject.toml` and `.claude-plugin/marketplace.json`; read it from the file rather than from this line if the two ever disagree.

**Author**: Lucio Saldivar, https://github.com/infinri (replace the listing email with a public-facing alias before submitting).

**URLs**: repo / issues / README / CHANGELOG under https://github.com/infinri/Writ.

## De-freeze request

Send this through https://clau.de/plugin-directory-submission AFTER pushing `main`, and
re-read the two facts it asserts before sending, because both decay: the public `main` sha,
and whether `writ` is still listed in `.github/freeze-shas.txt`.

> The `writ` entry in the community marketplace is held in `.github/freeze-shas.txt` from the
> 2026-06-13 snapshot of entries that failed `validate-plugins` at upstream HEAD. In our case
> the cause was "manifest lost at new SHA": a history rewrite moved
> `.claude-plugin/marketplace.json`, so `bump-plugin-shas.yml` could not resolve it at the new
> commit and correctly held the entry rather than opening a red PR.
>
> That condition no longer holds. `.claude-plugin/marketplace.json` is present at
> `github.com/infinri/Writ` on `main`, parses, and declares the `writ` plugin;
> `claude plugin validate` exits 0 against it. Per the freeze file's own guidance, "remove a
> slug once its upstream is fixed to let it freshen again", could you remove `writ` from the
> freeze list so the bump workflow can advance it?
>
> The currently pinned sha `d3c33d3e35adc30dc4448d0829801f5a8a9138da` is "Release v1.0.0" from
> 2026-05-10, so the listing installs a build that predates every install fix since. Its
> description is also stale: it advertises 276 rules across 12 domains, where the shipped
> corpus is 288 rules across 16 domains, and it leads with a "726x context reduction" figure
> we no longer publish, because our own benchmark notes record it as measured against a
> theoretical baseline nobody uses. Refreshed copy is in
> `docs/marketplace/SUBMISSION.md` under "Listing copy" if it helps, though the freeze removal
> is the part that matters; once the bump runs, the copy question resolves itself on the next
> submission cycle.

## Screenshots (capture before opening the form)

1. Rule injection: a session showing the `--- WRIT RULES ---` block on a real prompt.
2. Gate denial: a Write blocked with `[ENF-GATE-PLAN]` before plan approval.
3. `writ status` / `curl localhost:8765/health` showing the live corpus (288 rules, 32 mandatory as derived from the tracked dump on 2026-09-10, warm index; capture whatever the live daemon actually reports rather than these figures).
4. The `/dashboard` friction analytics view, or the `/explore` graph explorer.
5. Optional: the architecture pages under `docs/architecture/`.

Dark theme, 14-16pt font, redact personal paths.

## Procedure

1. Log in at https://clau.de/plugin-directory-submission and fill the form from the copy above; the marketplace source is `github.com/infinri/Writ` (marketplace name `writ` is declared in `.claude-plugin/marketplace.json`).
2. Submit, note the confirmation, and expect a review cadence of days.
3. Post-acceptance: cross-link the listing from the README; re-sync listing copy if Anthropic edits it in review.
4. On rejection: capture the reason verbatim, file an issue, address, resubmit.

---

# Appendix: positioning (former PROMOTIONAL-BRIEF.md, corrected 2026-07-31)

**Elevator pitch.** Writ gives every Claude Code session two helpers: a librarian that picks the rules that fit the current task in well under a millisecond, and a process keeper that blocks risky writes until you have approved a plan and tests. Self-approval is structurally impossible: advancing a gate requires a single-use token that only the user's typed approval mints.

**Key differentiators** (each verified against the current source tree):

1. Sub-millisecond ranked retrieval over a Neo4j-backed graph: candidate filter, BM25 (Tantivy), ANN vector (hnswlib over ONNX MiniLM), O(1) adjacency-cache graph enrichment, two-pass weighted ranking, plus a raw-cosine abstention gate that injects nothing rather than noise.
2. Graph-aware bundle expansion no skill-file approach can represent: `DEPENDS_ON` / `SUPPLEMENTS` / `CONFLICTS_WITH` neighbors surface with the rules they relate to.
3. A structural mandatory floor: enforcement rules are excluded from the ranked indexes at build time and injected out of band on their own budget; no ranking change can drop them. Guaranteed by structure, validated by `writ validate`.
4. A mode + gate state machine tied to declared intent: five modes; Work mode gates plan and test-skeleton approval before implementation; the gate token cannot be self-served (`agent_self_approval_blocked` is a logged, refused event).
5. AI rule proposal through a five-check structural gate, force-stamped provisional; frequency-driven graduation (a statistical flip, never an approval); an informed human promotion gate with edit-at-gate.
6. Sub-agent discipline: five typed roles auto-discovered by the plugin loader, generic dispatches rewritten to the right role, workers isolated per agent id with unlimited RAG, the orchestrator's own injection suppressed.
7. Decision memory: every commit's files join the approved plan and the rules the AI was shown; `writ recall` briefs future sessions; `writ pr sync` posts per-file reasons to the PR.
8. Pre-computation throughout: every index pre-warmed in memory, HNSW persisted with checksum guards, sticky rule ordering for prompt-cache stability.
9. Zero per-project configuration: one shared graph, project-scoped isolation, hooks detect the project's language from marker files.

**By the numbers**: quote README's Performance section (dated measurements) and `SCALE_BENCHMARK_RESULTS.md`; do not fork numbers into this file. Corpus and surface counts live in the generated `docs/reference/` pages.

**Use cases**: multi-language enterprise codebases (path-derived per-file rule injection); AI-discovered pattern capture (propose -> graduate -> promote instead of losing observations in transcripts); orchestrated sub-agent builds with human-held gates; pressure-tested rule authoring (RED-GREEN-REFACTOR applied to documentation); friction-driven retrospectives (tune the corpus from data, not anecdote).

**Competitive positioning.** Versus context stuffing: linear token cost and drowned signal versus flat retrieved tokens. Versus static skill files: point-in-time bundles with no relationships versus query-driven retrieval over a graph. Versus per-repo rules-as-code: no cross-project propagation versus one shared, project-scoped graph. Versus LLM-as-validator on every diff: cost and drift versus pattern-first analysis with optional escalation. Versus rules-in-system-prompt: cache pollution versus a bounded, varying per-turn block.

**TL;DR by audience**:

| Audience | Pitch |
|---|---|
| CTO / VP Eng | Ranked retrieval instead of context stuffing, enforceable plan-first/test-first discipline, and an AI-proposes-human-promotes evolution model. 749x context reduction at 10k rules, zero per-project config. |
| Tech lead | One shared rulebook across every repo, graph-aware retrieval, hook-enforced gates, typed sub-agent roles. |
| Engineer | Drop the plugin in. The right rules appear per turn; "approved" advances the workflow; writes are gated until then. |
| Corpus maintainer | Rules live in a graph with explicit relationships; AI proposals arrive gated and provisional; friction analytics tell you what to graduate or trim. |
| Adversarial reviewer | The mandatory floor is structural, not weighted; gate advance requires a token only the human's keystroke produces; every load-bearing contract is pinned by a test. |
