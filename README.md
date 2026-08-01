# Writ

A Claude Code harness that gives every coding session two helpers: a fast librarian that picks the rules that fit the current task, and a process keeper that blocks risky writes until you have approved a plan and tests.

The librarian returns ranked results in **0.590 ms at the 95th percentile** (measured 2026-05-10 at the then-276-rule corpus; the live corpus is 287 rules today). At the 10,000-rule synthetic scale it still holds at 0.557 ms while reducing context tokens by **726 times** versus loading the whole rulebook every turn.

See [`CHANGELOG.md`](CHANGELOG.md) for the release history through v1.5.1 (plugin marketplace install, sub-agent roles at `agents/`, gate-arming fixes, enterprise logging).

## Browse the architecture in your browser

Six self-contained HTML pages under [`docs/architecture/`](docs/architecture/) render the whole system with diagrams: [system overview](docs/architecture/index.html), [data model](docs/architecture/data-model.html), [retrieval pipeline](docs/architecture/retrieval-pipeline.html), [injection channels](docs/architecture/injection-channels.html), [knowledge graph](docs/architecture/knowledge-graph.html), and [corpus round-trip](docs/architecture/corpus-roundtrip.html). Clone and open `docs/architecture/index.html` in a browser; no server needed.

## Install as a Claude Code plugin

Writ is published as a single-plugin marketplace in this repo.

**Prerequisites:** Python 3.11+, Docker (Neo4j runs in a container), `jq`, `curl`, `envsubst`.

```shell
claude plugin marketplace add infinri/Writ
claude plugin install writ@writ
```

**Find the install directory** (the next command needs it):

```shell
WRIT_DIR=$(claude plugin list --json \
  | python3 -c "import json,sys; print(next(p['installPath'] for p in json.load(sys.stdin) if p['id'].split('@')[0] == 'writ'))")
```

**One-time bootstrap.** Creates the venv at `${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}/.venv`, brings up Neo4j, seeds the rule corpus from `writ-corpus.cypher`, and starts the FastAPI daemon:

```shell
bash "$WRIT_DIR/scripts/bootstrap-plugin.sh"
```

Restart Claude Code and verify with `curl http://localhost:8765/health`.

**Patch global config (plugin mode only).** Plugin installs do not write to `~/.claude/settings.json` or `~/.claude/CLAUDE.md`, so the Bash allowlist that suppresses permission prompts and the workflow instructions are missing until you run this once (idempotent, backs up existing files, `--dry-run` to preview):

```shell
bash "$WRIT_DIR/scripts/patch-global-config.sh"
```

The plugin's hooks degrade gracefully until bootstrap completes: sessions are never blocked, and the SessionStart hook prints setup instructions while anything is missing. Full install detail, the standalone path, the systemd service, and troubleshooting live in [`docs/install.md`](docs/install.md).

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
  Stage 1: Candidate filter          < 1 ms    category-routed or domain-scoped corpus
  Stage 2: BM25 keyword (Tantivy)    < 2 ms    top 50 candidates
  Stage 3: ANN vector (hnswlib)      < 3 ms    top 10 candidates
  Stage 4: Graph traversal           < 3 ms    adjacency cache for DEPENDS_ON,
                                                SUPPLEMENTS, CONFLICTS_WITH, ...
  Stage 5: Two-pass ranking          < 1 ms    reciprocal rank + context budget
                                              total p95 budget: 10 ms
```

Each retriever covers a blind spot the others have. BM25 catches exact keyword matches. Vectors catch paraphrase ("SQL" versus "database query"). Graph traversal catches rules that share neither but are causally related. The two-pass ranker fuses everything with severity, confidence, and graph-proximity weights, and an abstention gate returns nothing rather than injecting noise when the best raw cosine falls below 0.30.

**The enforcement layer (the process keeper).** 37 hook scripts under `hooks/scripts/`, wired into Claude Code via `hooks/hooks.json` (41 registrations across 12 hook events), plus one statusLine script, a session state machine in `writ/session/`, slash commands, and 5 sub-agent role files under `agents/`. The state machine owns mode, phase, and gate state; hooks are thin clients that delegate to it.

**Mandatory rules (the architectural invariant).** Rules with `mandatory: true` (33 in the live corpus, spanning ENF-* enforcement rules and SEC-*/PERF-*/SCALE-* invariants) are excluded from the retrieval pipeline at index build time. They reach the agent out of band through the `/always-on` endpoint with its own 5,000-token budget, enforced by a corpus integrity check. No change to ranking weights, embedding model, BM25 tuning, or graph traversal can cause an enforcement rule to disappear from agent context.

## Quick start (standalone)

```bash
git clone <writ-repo> ~/.claude/skills/writ
cd ~/.claude/skills/writ
bash scripts/bootstrap.sh
```

The bootstrap script handles everything: prerequisite checks, virtualenv, dependency install, global config rendered into `~/.claude/`, rule and agent symlinks, Neo4j via Docker Compose, corpus ingestion, and daemon startup. Idempotent.

Verify:

```bash
writ status
# {"status":"healthy","rule_count":287,"mandatory_count":33,"index_state":"warm",...}

writ query "controller contains SQL query"
# Mode: full | Candidates: 14 | Latency: 0.3ms
# 1. [0.984] SEC-INJ-SQL-001  Parameterized queries only...
```

Open Claude Code in any project. Type a prompt. You should see a `[Writ: ...]` status line and a `--- WRIT RULES ---` block with the rules that apply to what you are doing.

**Editing rules directly.** The corpus ships as `writ-corpus.cypher`, a single portable dump, not as a tracked `bible/` directory. To hand-edit a rule instead of going through `writ add`/`writ edit`: `writ export` materializes `bible/` locally from the live graph, edit the markdown, `writ import-markdown` pushes it back into Neo4j, then `writ export-cypher writ-corpus.cypher` refreshes the shipped dump before committing.

## What you experience

When Claude is doing read-only work (asking questions, debugging, reviewing), Writ injects relevant rules and stays out of the way. When Claude is in Work mode and tries to write code before you have approved a plan, the write is denied with a clear reason like:

```
[ENF-GATE-PLAN] Write blocked. Approve plan.md first.
```

You write the plan, you say "approved," the gate opens. The next gate (test skeletons) blocks code that has no tests pointing at it. Same pattern: write the tests, approve, gate opens. After both gates clear, Claude writes the implementation freely.

Approval cannot be self-served. Advancing a gate requires a one-time token written to `/tmp/writ-gate-token-${SESSION_ID}` only when the user actually types an approval phrase. If Claude tries to advance the gate itself, the token is missing and the call is denied (logged as `agent_self_approval_blocked`).

## The mode system

| Mode         | Purpose                                                | Gating |
|--------------|--------------------------------------------------------|--------|
| Conversation | Discussion, brainstorming, questions                   | None |
| Investigate  | Evidence-grounded audit, exploration, research         | Coverage and citation tracking; web research requires two independent sources before synthesis |
| Debug        | Investigating a specific problem                       | Source edits blocked until `debug.md` records a root cause |
| Review       | Evaluating code against rules                          | None |
| Work         | Building or modifying code                             | Two approval gates before implementation |

In Work mode, two gates apply:
1. **`phase-a`** validates `plan.md` against four required sections (`## Files`, `## Analysis`, `## Rules Applied`, `## Capabilities`) and verifies every cited rule ID against rules actually loaded in the session.
2. **`test-skeletons`** requires at least one test file with real assertions before production code is written.

## Performance

Live system measurement (2026-05-10, 276-rule corpus at the time, ONNX runtime, warm indexes; 500 samples per stage on 10 representative queries):

| Stage             | Median   | p95      | Budget  | Headroom at p95 |
|-------------------|---------:|---------:|--------:|----------------:|
| End to end        | 0.338 ms | 0.590 ms | 10.0 ms | 17x             |

Synthetic scale curve (2026-04-13, from `SCALE_BENCHMARK_RESULTS.md`):

| Corpus      | E2E p95   | Tokens stuffed | Tokens retrieved | Reduction |
|-------------|----------:|---------------:|-----------------:|----------:|
| 80 rules    | 0.278 ms  | 13,876         | 3,155            | 4.4x      |
| 500 rules   | 0.359 ms  | 63,003         | 1,600            | 39.4x     |
| 1,000 rules | 0.399 ms  | 121,473        | 1,602            | 75.8x     |
| 10,000 rules| 0.557 ms  | 1,174,142      | 1,617            | **726.1x**|

Retrieval quality against the 193-query ground-truth corpus (47 ambiguous, expanded 2026-07-17). The floors are regression gates the build fails below, deliberately set under the measured values, not quality targets:

| Metric                                      | Floor   | Measured (2026-07-17) |
|---------------------------------------------|---------|------------------------|
| MRR at 5 (ambiguous queries, n=47)          | >= 0.45 | ~0.57                  |
| Hit rate at 5 (all 193 queries)             | >= 0.75 | ~0.78                  |
| Domain hit rate top-5                       | >= 0.90 | passing                |
| nDCG at 10                                  | >= 0.65 | passing                |
| Methodology MRR at 5 (n=40, signed off)     | >= 0.78 | 0.8583 (v1.1.0 run)    |
| Methodology hit rate                        | >= 0.90 | 1.0000 (v1.1.0 run)    |

Full numbers and the real-versus-synthetic investigation in `SCALE_BENCHMARK_RESULTS.md`. Architectural detail in `HANDBOOK.md`.

## Relationship to Agent Skills

Anthropic's Agent Skills standard, originated by Anthropic and now maintained as an open spec at [agentskills.io](https://agentskills.io), solves a specific problem well. Skills live as directories with a `SKILL.md` entry point; at session start the agent pre-loads each skill's `name` and `description` into its system prompt. This is **progressive disclosure**: the agent sees just enough to know when a skill applies, without the body of the skill consuming context until needed. The design optimizes for a small system-prompt footprint, agent-side relevance decisions, no per-skill load cost, and low-friction authoring.

Writ targets a different problem. Writ is built around an enforcement-grade rule corpus, currently 287 rules and designed to scale into the thousands, where:

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

Writ models skills, playbooks, rationalizations, and forbidden-response sets as nodes in a hybrid-RAG knowledge graph (Neo4j-backed), retrieved by the same five-stage pipeline that surfaces rules: candidate filter, BM25 keyword (Tantivy), ANN vector (hnswlib), graph traversal over a pre-computed adjacency cache, weighted ranking. The agent does not match descriptions; the pipeline matches text plus tool state plus filesystem context.

Two architectural splits make this practical:

- **Retrieval-on-demand for the bulk of the corpus.** Ranked results in **0.590 ms at p95** at the live corpus; at 10,000 rules, **0.557 ms p95**. Retrieved tokens stay roughly flat (around 1,600) regardless of corpus size, while context stuffing scales linearly (13,876 tokens at 80 rules to 1,174,142 at 10,000). Reduction: **52x at the live corpus, 726x at 10,000 rules.**
- **Always-on bundle for the mandatory floor.** Mandatory rules and forbidden-response nodes load every turn through a dedicated endpoint with its own 5,000-token budget. They are excluded from the retrieval pipeline at index build time, so no ranking change can cause an enforcement rule to drop out of agent context.

Thirty-seven hook scripts read filesystem changes, tool calls, and session state, and invoke retrieval with those signals attached. Methodology arrives in the agent's context based on observable state, not on whether the agent recognized the trigger from prompt text alone.

### Boundary

Agent Skills is the right answer for small skill counts, human-authored discrete behaviors, contexts where agent-side matching against descriptions is acceptable, and authoring workflows that prioritize zero-config installation.

Writ's model is the right answer for large enforcement-grade rule corpora, contexts where the matching decision must move out of the agent, and pipelines that must fire on tool calls and filesystem state, not just prompts.

Same problem space, different optimization frontiers.

## CLI reference

The most-used commands; run `writ --help` for the complete list.

| Command | What it does |
|---|---|
| `writ serve [--host --port]` | Start the FastAPI service (default `localhost:8765`). |
| `writ status` | Health check via the HTTP service. |
| `writ query <text> [--domain --budget --local]` | Run a retrieval query (server first, in-process fallback). |
| `writ import-cypher [input]` | Rebuild the graph from the Cypher dump (wipes first). Default `writ-corpus.cypher`; this is what install and CI use. |
| `writ export-cypher [output]` | Dump the whole graph as the portable, tracked Cypher script. |
| `writ import-markdown [path] [--only --dry-run --compress]` | Ingest `bible/` markdown (rules + methodology) into Neo4j. |
| `writ export [output]` | Regenerate `bible/` markdown from the graph. |
| `writ reconcile [--bible-dir --project]` | Delete graph nodes/edges/props absent from source (the only prune path). |
| `writ validate [--benchmark]` | Run the ~30 corpus integrity checks. |
| `writ add` / `writ edit <rule_id>` | Interactive rule authoring with schema, redundancy, and conflict checks. |
| `writ propose ...` / `writ review [rule_id]` | AI rule proposal through the structural gate; human triage. |
| `writ compress` | Cluster rules into Abstraction nodes for summary mode. |
| `writ doctor [--fix --net]` | 13 install health checks; `--fix` repairs 6 of them. |
| `writ logs [tail\|stats\|list\|rotate\|backup]` | Read and manage the typed log streams. |
| `writ harvest [--since]` / `writ recall` | Decision-memory backfill from git + transcripts; recall briefing. |
| `writ pr sync` | Post captured file-change reasons as Bitbucket PR comments. |
| `writ analyze-friction [flags]` / `writ audit-session <sid>` | Friction-log analytics and per-session timelines. |

## API reference

All endpoints under `http://localhost:8765`. JSON bodies; no auth (binds localhost only). Total: 45 endpoints: 11 query/corpus routes, 3 gate routes, 24 session-state routes under `/session/{id}/`, 2 decision-memory, 1 git-hooks, 4 explorer.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/query` | Run the 5-stage pipeline. Returns `{rules, mode, total_candidates, latency_ms, abstain_signal}`. |
| `POST` | `/prompt-bundle` | One warm call combining broad query + always-on + methodology companion (the per-prompt hot path). |
| `GET`  | `/always-on` | Mandatory rules plus ForbiddenResponse nodes plus always-on Skills/Playbooks; mode- and injection-point-scoped. |
| `POST` | `/methodology-companion` | Deterministic workflow-state methodology matching (floor/push/pull channels). |
| `POST` | `/analyze` | Pattern analysis of a code snippet (LLM escalation only if the `anthropic` SDK is installed). |
| `GET`  | `/rule/{rule_id}` | Fetch a rule, optionally with one-hop graph context. |
| `POST` | `/propose` / `POST /feedback` / `POST /conflicts` | Rule proposal gate, feedback signals, conflict checks. |
| `GET`  | `/health` | Neo4j round-trip; rule count, mandatory count, index state, cache dir. |
| `POST` | `/pre-write-check` | Consolidated write gate + escalation + file-context RAG. |
| `POST` | `/session/{sid}/advance-phase` | Token-gated gate advance (the anti-self-approval surface). |
| `GET`  | `/dashboard` / `GET /explore` / `GET /graph` | Friction analytics HTML and the graph explorer. |

Errors come back as HTTP 200 with `{"error": "..."}` for logical failures, 422 for Pydantic validation, 5xx for unhandled exceptions. Clients should check the `error` key, not just status codes.

## Configuration

| File | Purpose |
|---|---|
| `writ.toml` | `[neo4j]` credentials, `[hnsw]` cache dir, `[bitbucket]` credentials, `[logs]` backup destination. Gitignored; `writ.toml.example` is the template. Missing file falls back to dev defaults. |
| `pyproject.toml` | Package metadata. Production deps: fastapi, uvicorn, neo4j, tantivy, hnswlib, httpx, pydantic, typer, rich, onnxruntime. `sentence-transformers` is the optional `[fallback]` extra, not a production dep. |
| `.claude-plugin/plugin.json` | Plugin manifest. Deliberately declares no `hooks` key (auto-discovery of `hooks/hooks.json`; declaring it collides) and no `agents` key (declaring one loads zero agents; auto-discovery of `agents/` loads all 5). |
| `.claude-plugin/marketplace.json` | Single-plugin marketplace so `claude plugin marketplace add infinri/Writ` resolves. Version must match `plugin.json`. |
| `docker-compose.yml` | Single `neo4j:5` service on 7474/7687, health-checked. The daemon itself runs natively. |
| `hooks/hooks.json` | Canonical hook wiring: 41 registrations across 12 events over 37 scripts. Auto-discovered; single source for the generated `templates/settings.json`. |
| `bin/lib/gate-categories.json` | Gate exclusion globs plus framework detection. |
| `writ/shared/budget.json` | Budget constants: default 8000, rule costs 200/120/40, always-on cap 5000. |

Common environment variables: `WRIT_HOST`/`WRIT_PORT` (daemon target), `WRIT_CACHE_DIR` (session caches, default `<install>/var/session`), `WRIT_LOG_ROOT` (typed log streams, default `<install>/var/logs`), `WRIT_FRICTION_LOG` (collapse all streams to one file), `WRIT_DEBUG` (debug sinks, default off), `WRIT_NO_AUTOSTART` (suppress daemon autostart), `WRIT_ALLOW_EMBEDDING_FALLBACK=1` (permit the sentence-transformers path when ONNX is absent). Neo4j credentials are read from `writ.toml` only.

## Testing

367 test modules, ~5,700 collected tests. Roughly half exercise a live Neo4j and skip only when the graph is genuinely unreachable; an empty-but-reachable graph fails loudly instead of skipping. The suite runs against a dedicated daemon port (8799) and isolated cache/log directories, and restores the production corpus from `writ-corpus.cypher` when it finishes.

```bash
make test          # pytest tests/ -x -q
make bench         # pytest benchmarks/bench_targets.py -x -q
make check         # both, plus writ validate
```

Pre-commit: `make bench` runs at `pre-push`.

## Status

**v1.5.1 (2026-07-31).** Installable end to end as a Claude Code plugin: marketplace manifest restored, sub-agent roles auto-discovered from `agents/` (verified `Agents (5)` on Claude Code 2.1.220), hook gate-arming fixes, and the enterprise logging program (typed streams, rotation, retention, `writ logs`) complete. Every number in this README is either measured and dated, or derived from the current source tree.

## Related documents

- [`HANDBOOK.md`](HANDBOOK.md): the operator manual (modes, gates, approval model, sub-agents, corpus).
- [`docs/install.md`](docs/install.md): full install detail for both install paths, the systemd service, and troubleshooting.
- [`SCALE_BENCHMARK_RESULTS.md`](SCALE_BENCHMARK_RESULTS.md): the full live measurement plus the synthetic scale curve.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): rule authoring workflow, review cadence, AI proposal triage.

## Acknowledgements

Kent Beck originated the TDD vocabulary used here: red-green-refactor,
"watch it fail," and "no production code without a failing test first."

Jesse Vincent's [Superpowers](https://github.com/obra/superpowers) revealed gaps in Writ's methodology coverage, and observing that project's design choices in practice fed Writ's analysis of the Agent Skills format (see [Relationship to Agent Skills](#relationship-to-agent-skills)).

License: MIT. Authored by Lucio Saldivar.
