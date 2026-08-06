# Writ

A Claude Code harness that gives every coding session two helpers: a process keeper that blocks risky writes until you have approved a plan and tests, and a fast librarian that picks the rules that fit the current task.

The process keeper's core property is that **the agent cannot approve its own work**. A gate advances only on a token minted from the user's own typed approval; the server requires and consumes that token (one approval, one advance); and an agent's attempt to self-approve is refused and logged as `agent_self_approval_blocked` in the audit stream. Retrieval speed is table stakes -- moving the oversight decision out of the agent is the product.

The librarian, for the record: ranked results in **0.52 ms at the 95th percentile** (measured 2026-08-05 at the live 287-rule corpus), holding at 0.83 ms at the 10,000-rule synthetic scale while reducing context tokens by **749 times** versus loading the whole rulebook every turn.

The enforcement claims are auditable in this repo, not asserted. [`PSR-008`](docs/pressure-runs/PSR-008/analysis.md) is a complete adversarial pressure run against a real session: the verbatim task prompt, the full transcript, every hook decision as JSONL, and each targeted rule graded held / bypassed / unclear (9 criteria, 8 held, one designed-in failure documented honestly). The [first monthly operational review](docs/monthly-reviews/2026-05.md) is built from 7,058 logged events by the system's own audit stream. Details in [Evidence](#evidence-pressure-runs-and-operational-reviews).

See [`CHANGELOG.md`](CHANGELOG.md) for the release history through v1.6.0 (re-measured benchmarks with environment disclosure, hook-system audit and hardening, force-swap coverage, the Claude Code 2.1.220 black-box refresh).

## Browse the architecture in your browser

Six self-contained HTML pages render the whole system with diagrams, served live on GitHub Pages: [system overview](https://infinri.github.io/Writ/docs/architecture/index.html), [data model](https://infinri.github.io/Writ/docs/architecture/data-model.html), [retrieval pipeline](https://infinri.github.io/Writ/docs/architecture/retrieval-pipeline.html), [injection channels](https://infinri.github.io/Writ/docs/architecture/injection-channels.html), [knowledge graph explorer](https://infinri.github.io/Writ/docs/architecture/knowledge-graph.html), and [corpus round-trip](https://infinri.github.io/Writ/docs/architecture/corpus-roundtrip.html). (The source lives under [`docs/architecture/`](docs/architecture/); cloning and opening `index.html` locally works too, no server needed.)

## Install as a Claude Code plugin

Writ is published as a single-plugin marketplace in this repo.

**Prerequisites:** Python 3.11+ and Docker (Neo4j runs in a container). Nothing else. `jq` and `curl` are used when present and fall back to Python when absent.

**1. Install the plugin:**

```shell
claude plugin marketplace add infinri/Writ
claude plugin install writ@writ
```

**2. Open Claude Code once.** Writ detects the un-bootstrapped install and prints one absolute command on its own line, ready to paste:

```shell
bash /path/it/prints/scripts/bootstrap-plugin.sh
```

**3. Run it, then restart Claude Code.** That one script creates the venv at `${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}/.venv`, brings up Neo4j, exports the embedding model, seeds the rule corpus from `writ-corpus.cypher`, starts the FastAPI daemon, patches `~/.claude/settings.json` (the Bash allowlist that suppresses permission prompts, plus the Writ statusLine) and `~/.claude/CLAUDE.md`, and installs the slash commands. It is idempotent, backs up anything it replaces, and re-running it after an update is the whole update procedure.

You should then see a `[Writ: ...]` status line and a `--- WRIT RULES ---` block on your next prompt.

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

**Adversarial review (the independence thesis).** The review claim is not a reviewer headcount; it is independence. The reviewer role runs with fresh context, judges a SHA-scoped diff, inherits none of the implementer's session history, and carries no Write tool: the implementer's framing never reaches it, and it can only report what it finds, never quietly fix it. Isolation is what makes a second opinion a second opinion.

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

## Decision memory: the repo remembers why files changed

Every commit made under Writ is captured into the same Neo4j graph as the rulebook: the decision behind it (title, rationale, the approved plan's per-file reasons), each file change with the rules the AI was shown and the rules it cited, and the commit itself, linked by typed edges. Capture is a post-commit git hook that never blocks a commit and fails open when the daemon is down; `writ harvest` backfills history.

That record then plays back in three places:

- **`writ recall`** compiles recent decisions into a token-budgeted digest, and a once-per-session briefing injects the top of it into your first prompt, so a new session starts knowing what was decided and why.
- **`writ pr sync`** posts one comment per changed file on the open Bitbucket pull request: the captured reason for the change, the rules the AI was shown, and the rules it cited. Idempotent (it updates its own comments), so reviewers see the "why" next to the diff instead of reconstructing it.
- **Git notes**: the same per-file reasons are written to `refs/notes/writ-decisions`, a plain-git channel that travels with the repository and needs no server.

This feature family is adapted from concepts pioneered by **JolliAI**: capturing AI development decisions as durable records, replaying them into new sessions, and pushing them onto commits and open PRs. Writ's recall eviction policy is adapted from Jolli's ContextCompiler (the policy, not the code; see `writ/session/recall.py`), and adds Writ's own twist: every decision stays grounded in the governing rule IDs, which are never evicted from the digest. Full detail: [`docs/reference/decision-memory.md`](docs/reference/decision-memory.md).

## Performance

Live system measurement (2026-08-01, 287-rule corpus, ONNX runtime, warm indexes; steady-state samples over 10 representative queries):

| Stage             | Median   | p95      | Budget  | Headroom at p95 |
|-------------------|---------:|---------:|--------:|----------------:|
| End to end        | 0.4 ms   | 0.6 ms   | 10.0 ms | 17x             |

Scale curve (synthetic tiers 2026-08-01; live-corpus row measured in place 2026-08-05, same
harness math and latency workload, read-only -- ingest and cold-start columns do not apply
to a pre-existing corpus):

| Corpus              | E2E p95   | Tokens stuffed | Tokens retrieved | Reduction |
|---------------------|----------:|---------------:|-----------------:|----------:|
| 80 rules            | 0.383 ms  | 14,738         | 2,056            | 7.2x      |
| **287 rules (live)**| 0.524 ms  | 52,870         | 2,000            | **26.4x** |
| 500 rules           | 0.486 ms  | 79,491         | 1,898            | 41.9x     |
| 1,000 rules         | 0.662 ms  | 137,990       | 1,686            | 81.8x     |
| 10,000 rules        | 0.827 ms  | 1,190,649      | 1,590            | **748.8x**|

On the harder 193-query ground-truth workload (real queries, not the 5-query latency set)
the live corpus measures 1.02 ms p95 / 0.43 ms median (2026-08-05, 579 timed queries).

All of the above was measured on a single mid-range developer laptop and an *uncapped* Neo4j container (512M pagecache); absolute numbers will differ on smaller machines. The full disclosure is the "Measurement environment" section of `SCALE_BENCHMARK_RESULTS.md`.

Retrieval quality against the 193-query ground-truth corpus (47 ambiguous, expanded 2026-07-17). The floors are regression gates the build fails below, deliberately set under the measured values, not quality targets:

| Metric                                      | Floor   | 2026-08-01 | 2026-08-05 |
|---------------------------------------------|---------|-----------:|-----------:|
| **Hit rate at 5 (index-eligible, n=169)**   | >= 0.90 | --         | **0.9290** |
| Always-on targets delivered (n=21)          | 21/21   | --         | 21/21      |
| Routed-channel targets verified (n=3)       | 3/3     | --         | 3/3        |
| Hit rate at 5 (all 193, continuity)         | >= 0.75 | 0.7824     | 0.8187     |
| MRR at 5 (ambiguous queries, n=47)          | >= 0.45 | 0.5681     | 0.6167     |
| Domain hit rate top-5                       | >= 0.90 | 0.9323     | 0.9534     |
| nDCG at 10                                  | >= 0.65 | 0.7071     | 0.7332     |
| Methodology MRR at 5 (n=40, signed off)     | >= 0.78 | 0.8271     | --         |
| Methodology hit rate                        | >= 0.90 | 0.9500     | --         |

The split (2026-08-05) is a metric-definition change, disclosed as such: rank metrics are
only meaningful for targets the ranked index can contain. 21 ground-truth queries target
mandatory rules, which the architectural invariant above deliberately excludes from
retrieval and delivers always-on every turn -- for those the verified guarantee is bundle
membership, not rank. 3 more target process rules whose category routes them to the
state/action/pull channels instead of semantic retrieval. The all-193 number stays in the
table so the definition change cannot quietly inflate the headline.

The 2026-08-05 column follows the miss triage (`benchmarks/MISS-TRIAGE-2026-08-05.md`): five
rules gained the vocabulary their queries actually use, and four ground-truth entries the
triage judged defective were corrected (one pointed at a rule that does not exist). Both
changes move the metric, so both are disclosed: of the 42 misses, 3 were fixture
corrections, 5 were converted by the corpus edits (2 of the 5 targeted edits did not
convert), and 1 query regressed from the re-index. Net 42 -> 35 misses.

**How to read these numbers.** A retrieval benchmark measures the pair (test set, retriever), never the retriever alone. The 47-query *ambiguous tier* exists for exactly that reason: those queries deliberately withhold the rule's own vocabulary and describe symptoms, narratives, or adjacent concepts, so they measure whether retrieval works when the asker does not already know the answer's words. A hit rate of 1.000 on a self-authored query set is a property of the test, not the retriever: whoever writes both the rules and the queries can trivially make every query contain its answer's vocabulary. That is why the numbers above are published with the miss list (`benchmarks/MISS-TRIAGE-2026-08-05.md` classifies all 42 failures by cause, including the ones that are our own fixture's defects), and why the floors are set beneath the measured values as regression gates rather than presented as achievements.

Full numbers in `SCALE_BENCHMARK_RESULTS.md`. Architectural detail in `HANDBOOK.md`.

## Relationship to Agent Skills

Anthropic's Agent Skills standard, originated by Anthropic and now maintained as an open spec at [agentskills.io](https://agentskills.io), solves a specific problem well. Skills live as directories with a `SKILL.md` entry point; at session start the agent pre-loads each skill's `name` and `description` into its system prompt. This is **progressive disclosure**: the agent sees just enough to know when a skill applies, without the body of the skill consuming context until needed. The design optimizes for a small system-prompt footprint, agent-side relevance decisions, no per-skill load cost, and low-friction authoring.

Writ targets a different problem. Writ is built around an enforcement-grade rule corpus, currently 287 rules and designed to scale into the thousands, where:

- The agent must not be the matching-decision-maker between context and rule.
- Retrieval must be triggerable by filesystem and tool-call signals, not only by prompt content.
- The corpus exceeds what fits in pre-loaded descriptions even with progressive disclosure (1,190,649 tokens at 10,000 rules versus a pre-loaded budget measured in low thousands).

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

- **Retrieval-on-demand for the bulk of the corpus.** Ranked results in **0.52 ms at p95** at the live 287-rule corpus (re-measured 2026-08-05; the harder 193-query ground-truth workload measures 1.02 ms p95); at 10,000 rules, **0.83 ms p95**. Retrieved tokens stay roughly flat (around 1,600-2,000) regardless of corpus size, while context stuffing scales linearly (14,738 tokens at 80 rules to 1,190,649 at 10,000). Reduction: **26.4x at the live corpus** (52,870 domain-rule tokens vs 2,000 retrieved), **749x at 10,000 rules**. An earlier README revision said 56x at the live corpus; that number does not reproduce with the harness's own arithmetic and is corrected here.
- **Measured injection cost, not a budget.** Across 67 real sessions and 891 turns of logged injections (all sources: per-prompt, per-read, per-write): **mean 537 tokens per turn, median 600, p95 1,360, max 5,440**. The 5,000-token always-on cap and 8,000-token retrieval budget are ceilings; the measured spend is what a turn actually costs.
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
| `writ.toml` | `[neo4j]` credentials, `[hnsw]` cache dir, `[bitbucket]` credentials, `[logs]` backup destination, `[egress] allow_hosts` (extra hosts the Bash egress guard never asks about). Gitignored; `writ.toml.example` is the template. Missing file falls back to dev defaults. |
| `pyproject.toml` | Package metadata. Production deps: fastapi, uvicorn, neo4j, tantivy, hnswlib, httpx, pydantic, typer, rich, onnxruntime. `sentence-transformers` is the optional `[fallback]` extra, not a production dep. |
| `.claude-plugin/plugin.json` | Plugin manifest. Deliberately declares no `hooks` key (auto-discovery of `hooks/hooks.json`; declaring it collides) and no `agents` key (declaring one loads zero agents; auto-discovery of `agents/` loads all 5). |
| `.claude-plugin/marketplace.json` | Single-plugin marketplace so `claude plugin marketplace add infinri/Writ` resolves. Version must match `plugin.json`. |
| `docker-compose.yml` | Single `neo4j:5` service on 7474/7687, health-checked. The daemon itself runs natively. |
| `hooks/hooks.json` | Canonical hook wiring: 41 registrations across 12 events over 37 scripts. Auto-discovered; single source for the generated `templates/settings.json`. |
| `bin/lib/gate-categories.json` | Gate exclusion globs plus framework detection. |
| `writ/shared/budget.json` | Budget constants: default 8000, rule costs 200/120/40, always-on cap 5000. |

Common environment variables: `WRIT_HOST`/`WRIT_PORT` (daemon target), `WRIT_CACHE_DIR` (session caches, default `<install>/var/session`), `WRIT_LOG_ROOT` (typed log streams, default `<install>/var/logs`), `WRIT_FRICTION_LOG` (collapse all streams to one file), `WRIT_DEBUG` (debug sinks, default off), `WRIT_NO_AUTOSTART` (suppress daemon autostart), `WRIT_ALLOW_EMBEDDING_FALLBACK=1` (permit the sentence-transformers path when ONNX is absent), `WRIT_EGRESS_ALLOW_HOSTS` (comma-separated hosts the Bash egress guard never asks about, unioned with `writ.toml [egress] allow_hosts`). Neo4j credentials are read from `writ.toml` only.

**Availability posture, and the strict switch.** A daemon outage does not ungate writes: the gate hooks fall back to a local subprocess that reads the same session state, so plan and test gates hold with the daemon down. The residue -- a check that cannot be answered even locally -- fails open by default (the availability decision) or fails closed with **`WRIT_STRICT=1`**, which denies any write whose gate could not be evaluated and says why (`ENF-STRICT-001`). Auditors who consider fail-open disqualifying set one environment variable instead of arguing with a documentation link.

## Testing

367 test modules, ~5,700 collected tests. Roughly half exercise a live Neo4j and skip only when the graph is genuinely unreachable; an empty-but-reachable graph fails loudly instead of skipping. The suite runs against a dedicated daemon port (8799) and isolated cache/log directories, and restores the production corpus from `writ-corpus.cypher` when it finishes.

```bash
make test          # pytest tests/ -x -q
make bench         # pytest benchmarks/bench_targets.py -x -q
make check         # both, plus writ validate
```

Pre-commit: `make bench` runs at `pre-push`.

## Evidence: pressure runs and operational reviews

For auditors (and anyone deciding whether to trust an enforcement tool), Writ keeps its verification artifacts in the repository rather than asserting them:

- **[`docs/pressure-runs/`](docs/pressure-runs/)**: adversarial scenario runs against real Claude Code sessions. Each completed run ships the verbatim task prompt, the full session transcript, the friction-log delta (every hook decision as JSONL), and a graded `analysis.md` scoring each targeted rule as held / bypassed / unclear. [`PSR-008`](docs/pressure-runs/PSR-008/analysis.md) is a complete example: 9 scored criteria, 8 passed, one designed-in failure documented honestly, and two real findings filed from the run. The methodology is in the [runbook](docs/pressure-runs/README.md).
- **[`docs/monthly-reviews/`](docs/monthly-reviews/)**: recurring operational reviews built from the friction log via `writ analyze-friction`: per-rule activation counts, denial stick rates, rationalization counts, skill usage, and playbook compliance, with keep/revise/trim actions per rule. [`2026-05.md`](docs/monthly-reviews/2026-05.md) is the first completed review (7,058 logged events in the window).

Both artifact sets are produced by the system's own audit stream, not written after the fact.

## Status

**v1.6.0 (2026-08-01).** Every benchmark re-measured on disclosed hardware; documentation rebuilt from a full code read; the 37-script hook system audited end to end (silent-failure fixes, force-swap coverage, fail-open posture documented as the specification); Claude Code contract re-pinned to 2.1.220. Installs end to end as a Claude Code plugin (verified `Agents (5)` on 2.1.220). Every number in this README is either measured and dated, or derived from the current source tree.

## Related documents

- [`HANDBOOK.md`](HANDBOOK.md): the operator manual (modes, gates, approval model, sub-agents, corpus).
- [`docs/install.md`](docs/install.md): full install detail for both install paths, the systemd service, and troubleshooting.
- [`SCALE_BENCHMARK_RESULTS.md`](SCALE_BENCHMARK_RESULTS.md): the full live measurement plus the synthetic scale curve.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): rule authoring workflow, review cadence, AI proposal triage.

## Acknowledgements
Jesse Vincent's [Superpowers](https://github.com/obra/superpowers) revealed gaps in Writ's methodology coverage, and observing that project's design choices in practice fed Writ's analysis of the Agent Skills format (see [Relationship to Agent Skills](#relationship-to-agent-skills)).

The decision-memory feature family (capturing decisions to the graph, session recall, and pushing per-file reasons onto commits and open PRs) is adapted from concepts pioneered by JolliAI; the recall eviction policy in particular is adapted from Jolli's ContextCompiler (see [Decision memory](#decision-memory-the-repo-remembers-why-files-changed)).

License: MIT. Authored by Lucio Saldivar.
