# Anthropic plugin marketplace submission packet

Maintainer reference for submitting `writ@writ` to the official Anthropic plugin marketplace at https://claude.ai/settings/plugins/submit. The positioning appendix at the bottom absorbs the former PROMOTIONAL-BRIEF.md.

## Pre-submission checklist

- [x] **Manifest validates**: `claude plugin validate <install>` exits 0 with no warnings (pinned by `tests/plugin/test_plugin_validate_cli.py`).
- [x] **Marketplace name not reserved**: `writ` is not on the reserved list.
- [x] **Plugin source publicly reachable**: `marketplace.json` points at `./` and the repo is public at https://github.com/infinri/Writ.
- [x] **README documents install + usage**: the "Install as a Claude Code plugin" section ships the install + bootstrap + patch-global-config sequence; full detail in `docs/install.md`.
- [x] **Agents load**: measured `Agents (5)` on Claude Code 2.1.220 with roles auto-discovered from `agents/` (commit `a56ca1e`).
- [x] **License OSI-approved**: MIT.
- [x] **No secrets in repo**: `writ.toml` is gitignored; the shipped template carries only the documented dev Neo4j default.
- [ ] **Fresh-install smoke on a clean machine**: run `tests/plugin/test_fresh_install_smoke.py` with `WRIT_INTEGRATION_TESTS=1` (clone, marketplace add, install, bootstrap, health), or walk the README steps in a throwaway VM. A green default suite does not prove this; the smoke test is env-gated.
- [ ] **Screenshots captured** (below).

## Listing copy

**Name**: `writ`

**Tagline** (<= 120 chars):
> Hybrid-RAG rule retrieval plus workflow gates for Claude Code: the right rules per prompt, no risky writes before an approved plan.

**Short description**:
> Writ is a Claude Code harness with two co-equal layers. A librarian retrieves the rules that fit the current task through a five-stage hybrid pipeline (BM25 + vector + graph traversal + weighted ranking, with an abstention gate) over a Neo4j knowledge graph: sub-millisecond ranked retrieval, roughly flat retrieved tokens as the corpus grows (726x reduction versus prompt-stuffing at 10,000 rules, measured). A process keeper of 37 hook scripts and a session state machine enforces mode-based workflow gates: plan approval, then test skeletons, then implementation, and approval requires a token only the user's keystroke produces. 287 rules ship out of the box across security, clean code, architecture, testing, performance, and process, with authoring tooling to grow your own.

**Long description**: mirror README "The problem" + "What Writ does about it" verbatim (kept current there; do not fork the text here).

**Category**: Development workflows. **Keywords**: mirror `plugin.json`.

**Author**: Lucio Saldivar, https://github.com/infinri (replace the listing email with a public-facing alias before submitting).

**URLs**: repo / issues / README / CHANGELOG under https://github.com/infinri/Writ.

## Screenshots (capture before opening the form)

1. Rule injection: a session showing the `--- WRIT RULES ---` block on a real prompt.
2. Gate denial: a Write blocked with `[ENF-GATE-PLAN]` before plan approval.
3. `writ status` / `curl localhost:8765/health` showing the live corpus (287 rules, 33 mandatory, warm index).
4. The `/dashboard` friction analytics view, or the `/explore` graph explorer.
5. Optional: the architecture pages under `docs/architecture/`.

Dark theme, 14-16pt font, redact personal paths.

## Procedure

1. Log in at https://claude.ai/settings/plugins/submit and fill the form from the copy above; the marketplace source is `github.com/infinri/Writ` (marketplace name `writ` is declared in `.claude-plugin/marketplace.json`).
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
| CTO / VP Eng | Ranked retrieval instead of context stuffing, enforceable plan-first/test-first discipline, and an AI-proposes-human-promotes evolution model. 726x context reduction at 10k rules, zero per-project config. |
| Tech lead | One shared rulebook across every repo, graph-aware retrieval, hook-enforced gates, typed sub-agent roles. |
| Engineer | Drop the plugin in. The right rules appear per turn; "approved" advances the workflow; writes are gated until then. |
| Corpus maintainer | Rules live in a graph with explicit relationships; AI proposals arrive gated and provisional; friction analytics tell you what to graduate or trim. |
| Adversarial reviewer | The mandatory floor is structural, not weighted; gate advance requires a token only the human's keystroke produces; every load-bearing contract is pinned by a test. |
