# Writ

A Claude Code harness that gives every coding session two helpers: a fast librarian that picks the rules that fit the current task, and a process keeper that blocks risky writes until you have approved a plan and tests.

At the live 276-rule production corpus (post Phase 1-5 public-rulebook expansion), the librarian returns ranked results in **0.590 ms at the 95th percentile**. At the 10,000-rule synthetic scale, it still holds at 0.557 ms while reducing context tokens by **726 times** versus loading the whole rulebook every turn.

See [`CHANGELOG.md`](CHANGELOG.md) for the v1.2.0 release notes (proactive context-window management, hook hot-path latency consolidation, friction-log mode canonicalization, PostToolUse banner fix) and the v1.0.0 + v1.0.1 + v1.1.0 history of capabilities shipped.

## Browse the architecture in your browser

Four self-contained HTML pages render the whole system. No clone or install required: open any link below and walk the diagrams. A sticky top nav inside each page cross-links to the other three, so you can flip between layers without coming back to the README.

- **[Complete overview](https://infinri.github.io/Writ/writ-complete-overview.html)**: seven diagrams (system block, session flowchart, query sequence, pipeline funnel, mode/phase state machine, knowledge graph excerpt, rule-evolution lifecycle) covering the full system end to end. Start here.
- **[Architecture layers](https://infinri.github.io/Writ/writ-architecture-flowchart.html)**: eight tabbed layer cards (pipeline, weighting, mandatory-vs-retrieved split, knowledge graph, mode system, phase gates, enforcement layer, evolution model) with plain-English summaries and technical drill-in.
- **[Workflow flowchart](https://infinri.github.io/Writ/writ-workflow-flowchart.html)**: long-ways START-to-END SVG process flowchart of one Writ-enforced coding session, with rectangles for steps, diamonds for decisions, and dashed loop-back arrows for the iterate-until-it-passes gates. The description and step detail stay anchored in a sticky left rail.
- **[Commands reference](https://infinri.github.io/Writ/writ-commands.html)**: every CLI command, every `writ-session.py` subcommand, and every HTTP endpoint, tabbed by group with live filter.

These four pages live at the repo root (`writ-complete-overview.html`, `writ-architecture-flowchart.html`, `writ-workflow-flowchart.html`, `writ-commands.html`) and are served by GitHub Pages from `main`. Clone and open locally for offline browsing, or follow the links above straight from a browser tab.

## Install as a Claude Code plugin

Writ is published as a single-plugin marketplace in this repo.

**Prerequisites**

- Python 3.11 or newer
- Docker (Neo4j runs in a container)
- `jq`, `curl`, `envsubst`

**Install**

```shell
claude plugin marketplace add infinri/Writ
claude plugin install writ@writ
```

**Find the install directory.** The next two commands need the path the plugin was installed to. Read it from `claude plugin list --json`, whose entry for Writ carries an `installPath`:

```shell
WRIT_DIR=$(claude plugin list --json \
  | python3 -c "import json,sys; print(next(p['installPath'] for p in json.load(sys.stdin) if p['id'].split('@')[0] == 'writ'))")
echo "$WRIT_DIR"
```

**One-time bootstrap.** Creates the venv at `${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}/.venv`, brings up Neo4j, seeds the rule corpus from `writ-corpus.cypher`, and starts the FastAPI daemon:

```shell
bash "$WRIT_DIR/scripts/bootstrap-plugin.sh"
```

Restart Claude Code. Verify with:

```shell
curl http://localhost:8765/health
# {"status":"healthy"}
```

**Patch global config (plugin mode only).** Plugin installs do not write to `~/.claude/settings.json` or `~/.claude/CLAUDE.md`. The Writ-specific Bash allowlist that suppresses permission prompts for read-only and onboarding commands is missing, and the mandatory-workflow instructions that Writ relies on are not installed either. Run this once after bootstrap to bring `~/.claude/` up to the state a standalone install would produce:

```shell
bash "$WRIT_DIR/scripts/patch-global-config.sh"
# Use --dry-run first to preview the diff.
```

The script does two things: merges the cross-mode allow and deny entries into `~/.claude/settings.json` (existing ordering preserved), and renders `templates/CLAUDE.md` into `~/.claude/CLAUDE.md`. Both steps are idempotent (no-op when already in sync) and back up any pre-existing file before writing. Standalone-install users do not need to run it; `scripts/bootstrap.sh` already produces the same output.

The plugin's hooks degrade gracefully until bootstrap completes. The SessionStart hook prints clear setup instructions on every fresh session where any prerequisite is missing, but the session itself is never blocked.

The standalone install path at `~/.claude/skills/writ/` remains supported; see "Quick start" below if you prefer that mode.

## The problem

Three things break when you give a coding agent a large rulebook the obvious way (paste it all into the prompt):

1. **Token cost grows with the rulebook, not the work.** At 80 rules: about 13,876 tokens of rule text every turn. At 10,000 rules: 1,174,142 tokens. Cache hit rates collapse, latency climbs, the bill scales with the rulebook.
2. **Relevance degrades.** A model handed every rule treats none of them as load bearing. Specific rules drown in generic ones.
3. **Workflow discipline has nowhere to live.** Static skill files can describe a process; they cannot enforce it. Telling the model to write tests first does not stop it from writing the implementation first.

## What Writ does about it

Two layers, sharing a Neo4j-backed knowledge graph:

**The knowledge layer (the librarian).** A FastAPI service on `localhost:8765` that runs a five-stage hybrid retrieval pipeline:

```
Query text
  Stage 1: Domain Filter            < 1 ms     domain-scoped corpus
  Stage 2: BM25 keyword (Tantivy)   < 2 ms     top 50 candidates
  Stage 3: ANN vector (hnswlib)     < 3 ms     top 10 candidates
  Stage 4: Graph traversal          < 3 ms     adjacency cache for DEPENDS_ON,
                                                 SUPPLEMENTS, CONFLICTS_WITH, ...
  Stage 5: Two-pass ranking         < 1 ms     reciprocal rank + context budget
                                              total p95 budget: 10 ms
```

Each retriever covers a blind spot the others have. BM25 catches exact keyword matches. Vectors catch paraphrase ("SQL" versus "database query"). Graph traversal catches rules that share neither but are causally related. The two-pass ranker fuses everything with severity, confidence, and graph-proximity weights.

**The enforcement layer (the process keeper).** 38 hook scripts under `hooks/scripts/`, all wired into Claude Code via `hooks/hooks.json` (12 registered hook events), a session state machine in `bin/lib/writ-session.py`, slash commands, and 5 sub-agent role files. The state machine owns mode, phase, and gate state; hooks are thin clients that delegate to it.

**Mandatory rules (the architectural invariant).** Rules with `mandatory: true` (30 in the live corpus, spanning ENF-* enforcement rules and SEC-*/PERF-*/SCALE-* invariants from the public rulebook) are excluded from the retrieval pipeline at index build time. They are loaded out of band by hooks with their own 5,000 token budget cap. No change to ranking weights, embedding model, BM25 tuning, or graph traversal can cause an enforcement rule to disappear from agent context.

## Quick start

```bash
git clone <writ-repo> ~/.claude/skills/writ
cd ~/.claude/skills/writ
bash scripts/bootstrap.sh
```

The bootstrap script handles everything: prerequisite checks (Python 3.11+, Docker, git, envsubst), virtualenv, dependency install, harness config rendered into `~/.claude/`, rule and agent symlinks, Neo4j container via Docker Compose, rule corpus ingestion, and Writ daemon startup. Idempotent.

Verify:

```bash
writ status
# {"status":"healthy","rule_count":276,"mandatory_count":30,"index_state":"warm",...}

writ query "controller contains SQL query"
# Mode: full | Candidates: 14 | Latency: 0.3ms
# 1. [0.984] SEC-INJ-SQL-001  Parameterized queries only...
```

Open Claude Code in any project. Type a prompt. You should see a `[Writ: ...]` status line and a `--- WRIT RULES ---` block with the rules that apply to what you are doing.

**Editing rules directly.** The corpus ships as `writ-corpus.cypher`, a single portable dump (see `writ export-cypher` / `writ import-cypher` below), not as a tracked `bible/` directory of markdown files. To hand-edit a rule instead of going through `writ add`/`writ edit`: `writ export` materializes `bible/` locally from the live graph, edit the markdown, `writ import-markdown` pushes it back into Neo4j, then `writ export-cypher writ-corpus.cypher` refreshes the shipped dump before committing.

## What you experience

When Claude is doing read-only work (asking questions, debugging, reviewing), Writ injects relevant rules and stays out of the way. When Claude is in Work mode and tries to write code before you have approved a plan, the write is denied with a clear reason like:

```
[ENF-GATE-PLAN] Write blocked. Approve plan.md first.
```

You write the plan, you say "approved," the gate opens. The next gate (test skeletons) blocks code that has no tests pointing at it. Same pattern: write the tests, approve, gate opens. After both gates clear, Claude writes the implementation freely.

Approval cannot be self-served. The slash command `/writ-approve` requires a one-time token written to `/tmp/writ-gate-token-${SESSION_ID}` only when the user actually types an approval phrase. If Claude tries to advance the gate via raw bash, the token is missing and the call is denied (logged as `agent_self_approval_blocked`).

## The mode system

| Mode         | Purpose                                            | Code generation |
|--------------|----------------------------------------------------|------------------|
| Conversation | Discussion, brainstorming, questions               | No               |
| Debug        | Investigating a specific problem (read only)       | No               |
| Review       | Evaluating code against rules (read only)          | No               |
| Work         | Building or modifying code                         | Yes (with gates) |

In Work mode, two gates apply:
1. **`phase-a`** validates `plan.md` against four required sections (`## Files`, `## Analysis`, `## Rules Applied`, `## Capabilities`) and verifies every cited rule ID against rules actually loaded in the session.
2. **`test-skeletons`** requires at least one test file with real assertions before production code is written.

## Performance

Live system measurement (2026-05-10, 276 rule production corpus post Phase 1-5 public-rulebook expansion, ONNX runtime, warm indexes; 500 samples per stage on 10 representative queries):

| Stage             | Median   | p95      | Budget  | Headroom at p95 |
|-------------------|---------:|---------:|--------:|----------------:|
| End to end        | 0.338 ms | 0.590 ms | 10.0 ms | 17x             |

Cold start (median of 3 runs): 1.72 s (pre-expansion baseline; rebuild cost scales with corpus size).

Synthetic scale curve (2026-04-13, from `SCALE_BENCHMARK_RESULTS.md`):

| Corpus     | E2E p95   | Tokens stuffed | Tokens retrieved | Reduction |
|------------|----------:|---------------:|-----------------:|----------:|
| 80 rules   | 0.278 ms  | 13,876         | 3,155            | 4.4x      |
| 500 rules  | 0.359 ms  | 63,003         | 1,600            | 39.4x     |
| 1,000 rules| 0.399 ms  | 121,473        | 1,602            | 75.8x     |
| 10,000 rules| 0.557 ms | 1,174,142      | 1,617            | **726.1x**|

Quality (v1.1.0, against the Phase 6 ground-truth corpus, 165 queries: the original 83 + 82 new queries covering the public-rulebook expansion):

| Metric                                            | Floor     | Actual         |
|---------------------------------------------------|-----------|----------------|
| MRR at 5 (ambiguous queries, n=19)                | >= 0.45   | **0.6904**     |
| Hit rate (all 165 queries)                        | >= 0.75   | **0.800**      |
| Domain hit rate top-5 (all 164 queries)           | >= 0.90   | **0.945**      |
| Methodology MRR at 5 (n=40, signed off corpus)    | >= 0.78   | 0.8583         |
| Methodology hit rate                              | >= 0.90   | 1.0000         |
| ONNX vs PyTorch ranking stability (inline 12-rule corpus, 8 queries) | identical top-1 + top-5 set | 0/8 differ |

The ambiguous-set MRR and hit-rate floors were retuned downward during the v1.0.0 Phase 1-5 expansion: the corpus grew 3.8x (72 to 276 rules) while the ambiguous-set query count remained constant at 19. The v1.1.0 release recovered ~70% of the v0->v1.0.0 gap (MRR@5 0.4886 -> 0.6904) via 4 label/rule-text fixes; remaining ~12% gap is real corpus-growth dilution. Methodology retrieval is unaffected (a separate, signed-off corpus). The v1.1.0 domain-hit-rate gate is new enforcement against the synthetic-curve concern about top-5 domain coverage degrading with corpus growth.

Full numbers in `SCALE_BENCHMARK_RESULTS.md`. Architectural detail in `HANDBOOK.md`.

## Relationship to Agent Skills

Anthropic's Agent Skills standard originated by Anthropic and now maintained as an open spec at [agentskills.io](https://agentskills.io) solves a specific problem well. Skills live as directories with a `SKILL.md` entry point; at session start the agent pre-loads each skill's `name` and `description` into its system prompt. This is **progressive disclosure**: the agent sees just enough to know when a skill applies, without the body of the skill consuming context until needed. The design optimizes for a small system-prompt footprint, agent-side relevance decisions, no per-skill load cost, and low-friction authoring.

Writ targets a different problem. Writ is built around an enforcement-grade rule corpus currently 276 rules and designed to scale into the thousands where:

- The agent must not be the matching-decision-maker between context and rule.
- Retrieval must be triggerable by filesystem and tool-call signals, not only by prompt content.
- The corpus exceeds what fits in pre-loaded descriptions even with progressive disclosure (1,174,142 tokens at 10,000 rules versus a pre-loaded budget measured in low thousands).

### Where progressive disclosure runs out

The pattern works as long as the agent's matching of descriptions to context is reliable. The matching step degrades as:

- **Skill count grows.** Each pre-loaded description costs system-prompt tokens; with hundreds of entries, descriptions blur and discrimination drops.
- **Descriptions overlap.** Two skills with semantically nearby triggers compete; the agent picks one and silently skips the other.
- **Context becomes ambiguous.** A prompt that touches several domains gives the agent multiple plausible matches; pre-loaded descriptions don't disambiguate.
- **The trigger isn't in the prompt.** A skill that needs to fire when the user opens a Python file, runs a specific command, or modifies a config can't be matched from prompt text alone.

These are not bugs in the spec. They are the boundary of what agent-side matching against pre-loaded text can do.

### Writ's alternative

Writ models skills, playbooks, rationalizations, and forbidden-response sets as nodes in a hybrid-RAG knowledge graph (Neo4j-backed), retrieved by the same five-stage pipeline that surfaces rules: domain filter, BM25 keyword (Tantivy), ANN vector (hnswlib), graph traversal over a pre-computed adjacency cache, weighted ranking. The agent does not match descriptions; the pipeline matches text plus tool state plus filesystem context.

Two architectural splits make this practical:

- **Retrieval-on-demand for the bulk of the corpus.** A query against 276 rules returns ranked results in **0.590 ms at p95**; at 10,000 rules, **0.557 ms p95**. Retrieved tokens stay roughly flat (around 1,600) regardless of corpus size, while context stuffing scales linearly (13,876 tokens at 80 rules to 1,174,142 tokens at 10,000). Reduction: **52x at the live corpus, 726x at 10,000 rules.**
- **Always-on bundle for the mandatory floor.** Mandatory rules and forbidden-response nodes load every turn through a dedicated endpoint with its own 5,000-token budget cap. They are excluded from the retrieval pipeline at index build time, so no ranking change can cause an enforcement rule to drop out of agent context.

Thirty hook scripts read filesystem changes, tool calls, and session state, and invoke retrieval with those signals attached. Methodology arrives in the agent's context based on observable state, not on whether the agent recognized the trigger from prompt text alone.

### Boundary

Agent Skills is the right answer for small skill counts, human-authored discrete behaviors, contexts where agent-side matching against descriptions is acceptable, and authoring workflows that prioritize zero-config installation.

Writ's model is the right answer for large enforcement-grade rule corpora, contexts where the matching decision must move out of the agent, and pipelines that must fire on tool calls and filesystem state, not just prompts.

Same problem space, different optimization frontiers.

## CLI reference

| Command | What it does |
|---|---|
| `writ serve [--host --port]` | Start the FastAPI service via uvicorn (default `localhost:8765`). |
| `writ status` | Health check via the HTTP service. |
| `writ query <text> [--domain --budget]` | Run a retrieval query. |
| `writ import-markdown [path]` | Ingest rules from `bible/` Markdown into Neo4j. Auto-exports back on success. `bible/` is not shipped/tracked; regenerate it locally via `writ export` first if you want to hand-edit. |
| `writ export [output]` | Regenerate Markdown from graph (overwrites output dir). |
| `writ export-cypher [output]` | Dump the whole graph as a portable Cypher replay script. Default `writ-corpus.cypher` -- the shipped, tracked source of the corpus. |
| `writ import-cypher [input]` | Rebuild the graph from a Cypher dump (wipes first, then replays). Default `writ-corpus.cypher`. This is what install/bootstrap and CI use to seed the corpus. |
| `writ add` | Interactive add-a-new-rule wizard. Schema validates, redundancy checks, suggests edges, writes. |
| `writ edit <rule_id>` | Edit existing rule with current values as defaults. |
| `writ validate [--review-confidence --benchmark]` | Run integrity checks (conflicts, orphans, stale, redundant, frequency). |
| `writ compress` | Cluster rules (HDBSCAN + k-means) into Abstraction nodes for summary mode. |
| `writ propose ...` | Submit AI authored rule through structural gate. |
| `writ review [rule_id] [--promote --reject --downweight --stats]` | Triage AI provisional rules. |
| `writ feedback <rule_id> <positive\|negative>` | Record feedback signal. |
| `writ import-markdown [PATH] [--only TYPE[,...]] [--dry-run]` | Import Markdown bible (Rules + methodology) into Neo4j. Default path `bible/`; `--only` filters node types; `--dry-run` validates without writing. |
| `writ migrate` | Backward-compat shim. Delegates in-process to `writ import-markdown` with defaults (full corpus, write-through). |
| `writ analyze-friction [flags]` | Analyze `workflow-friction.log`: rule effectiveness, skill usage, playbook compliance, graduation candidates, trim candidates, quality judge false positives. |
| `writ audit-session <session_id>` | Per-session timeline and summary. |
| `writ role-prompt <name>` | Print canonical SubagentRole prompt template from graph. |

## Troubleshooting

Common issues and fixes:

- **`Docker daemon not reachable`**: start Docker Desktop, or `sudo systemctl start docker` on Linux, then re-run `bootstrap.sh`.
- **`python3 version is 3.9; need >= 3.11`**: install a newer Python. `pyenv` is a clean way to manage versions without touching system Python.
- **`port 7687 already in use`**: another Neo4j instance is running. Either stop it (`docker stop <container>`) or change the `ports:` mapping in `docker-compose.yml`.
- **`Neo4j did not become reachable within 60s`**: check logs (`docker compose logs neo4j`). Common cause: insufficient memory allocated to Docker Desktop (Neo4j needs ~1 GB).
- **`daemon did not become healthy within 10s`**: check `/tmp/writ-server.log`. Usually an import error; re-run `pip install -e .` from the skill directory with the venv activated.
- **Default Neo4j credentials (`neo4j/writdevpass`)**: a development default. For any non-local use, change `NEO4J_AUTH` in `docker-compose.yml` and the matching `[neo4j]` section in `writ.toml`.

## API reference

All endpoints under `http://localhost:8765`. JSON bodies; no auth (binds localhost only). Total: 36 endpoints (11 top-level plus 25 under `/session/{id}/`).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/query` | Run the 5-stage pipeline. Body: `{query, domain?, budget_tokens?, exclude_rule_ids?, prefer_rule_ids?, node_types?}`. Returns `{rules, mode, total_candidates, latency_ms}`. |
| `POST` | `/analyze` | Run the analyzer (pattern plus optional LLM escalation) on a code snippet. |
| `GET` | `/rule/{rule_id}` | Fetch a rule, optionally with `?include_graph=true` for one-hop neighbors. |
| `POST` | `/propose` | Submit AI authored rule through the 5-check structural gate. |
| `POST` | `/feedback` | Record a positive or negative signal for a rule. |
| `POST` | `/conflicts` | Check `CONFLICTS_WITH` edges among a list of rule IDs. |
| `GET` | `/health` | Real Neo4j round-trip; reports rule count, mandatory count, index state, startup time. |
| `GET` | `/always-on` | Returns mandatory rules plus `ForbiddenResponse` nodes plus always-on Skills/Playbooks. Mode-scoped. |
| `GET` | `/subagent-role/{name}` | Resolve `writ-explorer` to its role record. |
| `POST` | `/pre-write-check` | Consolidated gate plus final-gate plus RAG check. |
| `GET` | `/dashboard` | Server-rendered HTML view of friction-log analytics. |
| `GET` `POST` | `/session/{sid}/...` | 25 routes covering mode, phase, gates, coverage, escalation, quality judgment, playbook progress, compaction, verification evidence. |

Errors come back as HTTP 200 with `{"error": "..."}` for logical failures, 422 for Pydantic validation, 5xx for unhandled DB exceptions. Clients should check the `error` key, not just status codes.

## Configuration

| File | Purpose |
|---|---|
| `writ.toml` | Service configuration: Neo4j credentials, ranking weights, embedding model, context budgets, gate thresholds. |
| `pyproject.toml` | Package metadata. Production deps: fastapi, uvicorn, neo4j, tantivy, sentence-transformers, hnswlib, pydantic, typer, rich, httpx. Entry point: `writ = "writ.cli:app"`. |
| `.claude-plugin/plugin.json` | Plugin manifest: name, version, commands, agents. Declares no `hooks` path, because `hooks/hooks.json` is auto-discovered and declaring it collides. |
| `.claude-plugin/marketplace.json` | Single-plugin marketplace so `claude plugin marketplace add infinri/Writ` resolves. Its version must match `plugin.json`. |
| `docker-compose.yml` | Single `neo4j:5` service on ports 7474 and 7687, health-checked via cypher-shell. |
| `hooks/hooks.json` | Canonical hook wiring (12 registered events over 38 scripts in `hooks/scripts/`). Auto-discovered by Claude Code; shared by the plugin and standalone install paths. |
| `bin/lib/checklists.json` | Phase exit criteria. |
| `bin/lib/gate-categories.json` | File classification glob patterns plus framework detection. |
| `writ/shared/budget.json` | Single source of truth for budget constants (default 8000, summary cost 40, standard 120, full 200, always_on_cap 5000). |

Environment variables read by hooks: `WRIT_HOST` (default `localhost`), `WRIT_PORT` (default `8765`), `WRIT_CACHE_DIR` (default `tempfile.gettempdir()`), `WRIT_FRICTION_LOG`, `WRIT_HOOK_LOG`, `WRIT_DEBUG_LOG`. Neo4j credentials are read from `writ.toml` only; there is no `WRIT_NEO4J_*` override.

## Testing

90 test files, 1,192 test functions. The end-of-suite hook in `tests/conftest.py` shells out to `writ import-cypher writ-corpus.cypher` to restore the production graph after tests run.

```bash
make test          # pytest tests/ -x -q
make bench         # pytest benchmarks/bench_targets.py -x -q
make check         # both
```

Pre-commit: `make bench` runs at `pre-push`. No formatting or lint hooks configured.

The benchmark suite has four files:
- `benchmarks/bench_targets.py` (13 contractual targets at v1.1.0, all pass/fail; was 12 pre-v1.1.0).
- `benchmarks/run_benchmarks.py` (Neo4j traversal scale at 1K and 10K nodes; wipes the graph).
- `benchmarks/scale_benchmark.py` (the synthetic 80/500/1K/10K scale curve generator; restores only Rule nodes).
- `benchmarks/methodology_bench.py` (methodology retrieval against the curated 40-query Phase 0 corpus; read only, no Neo4j changes).

## Status

**v1.2.0 (2026-05-15).** The retrieval pipeline and enforcement gates work end to end, and every number in this README is real and measured against this release.

Builds on v1.1.0 (2026-05-15) and the v1.0.0 / v1.0.1 history. v1.2.0 adds proactive context-window management (a watcher hook that emits a soft directive at 50% of the configured window and hard-blocks PreToolUse calls at 75% so the agent runs `/compact` before declaring work complete under context pressure), collapses hook-hot-path Python spawns to bring per-write latency floors into a recorded regression-floor test, repairs case-drift in the friction log's `mode` field so the dashboard groups modes cleanly, and replaces a cosmetic "0 potential issues found but unconfirmed" PostToolUse banner that had been firing on every clean Write/Edit in v1.1.0. Benchmark baselines (retrieval p95, MRR@5, hit rate, cold-start) are unchanged from v1.1.0; the v1.2.0 work targets workflow latency and observability, not retrieval quality. See `CHANGELOG.md` for the full v1.2.0 entry.

**Public out-of-the-box rulebook seeded.** 198 new universal rules across Security, Clean Code, DRY, SOLID, Architecture, Testing, Error Handling, Performance, Scaling, API Design, Process, and Documentation, plus 19 new mandatory rules each backed by a cross-language regex analyzer in `bin/run-analysis.sh`. See `out-of-the-box-rules.md` for the canonical rule list.

## Related documents

- `HANDBOOK.md` for the full architecture, including detailed graph schema, gate mechanics, and the discrepancy catalog.
- `SCALE_BENCHMARK_RESULTS.md` for the full live measurement plus the synthetic scale curve.
- `CONTRIBUTING.md` for rule authoring workflow, monthly review cadence, and AI proposal triage.
- `PROMOTIONAL-BRIEF.md` for the pitch-oriented version of this document.

## Switching from the standalone install to the plugin

The standalone install at `~/.claude/skills/writ/` will keep working; the plugin path is purely additive. If you'd rather move to the plugin path:

1. Stop the existing daemon: `bash ~/.claude/skills/writ/scripts/stop-server.sh`
2. Remove the symlinks the standalone bootstrap created: `rm -f ~/.claude/rules/writ-*.md ~/.claude/agents/writ-*.md`
3. Remove the rendered hook block from `~/.claude/settings.json` (the `permissions.allow` and `hooks` sections that reference `$HOME/.claude/skills/writ/.claude/hooks/`). Back up the file first.
4. Install the plugin as described in "Install as a Claude Code plugin" above. The Neo4j Docker volume (`writ-neo4j-data`) is shared between modes, so the rule corpus survives the switch.

The standalone-skill checkout itself can stay on disk; nothing in the plugin install path looks at it.

## Acknowledgements

Kent Beck originated the TDD vocabulary used here: red-green-refactor,
"watch it fail," and "no production code without a failing test first."

Jesse Vincent's [Superpowers](https://github.com/obra/superpowers) revealed gaps in Writ's methodology coverage, and observing that project's design choices in practice fed Writ's analysis of the Agent Skills format (see [Relationship to Agent Skills](#relationship-to-agent-skills)).

License: MIT. Authored by Lucio Saldivar.
