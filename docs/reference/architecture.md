# Architecture

Contributor-facing system design: the spine `README.md` and `HANDBOOK.md` reference. Every load-bearing claim is anchored to a file or a command you can run; where this page and the code disagree, the code wins. Counts are from the source tree and corpus dump of 2026-07-31.

> **North star.** Writ relocates oversight; it does not remove it. The gate is the product. Writ never automates self-approval: a human token-gates any write of canon. Every "the agent could otherwise..." seam below is closed by a human-held token, not by trust.

## 1. The four runtime pieces

| Piece | What it is | Where |
|---|---|---|
| **FastAPI daemon** | The single HTTP service all hooks talk to: retrieval, session state, gates, self-authoring. Binds `127.0.0.1:8765`, 49 endpoints, no auth (localhost only). | `writ/server/` (package: `__init__.py` app + lifespan, `models.py`, `routes/*.py`) |
| **Hooks + session state machine** | 40 bash hooks intercept the Claude Code tool lifecycle (44 registrations, 12 events, `hooks/hooks.json`); a Python package owns mode/phase/budget/gate state per session. | `hooks/`, `writ/session/` |
| **Neo4j canonical store** | The graph is the source of truth for rules, methodology, and decision-memory records. Docker container `writ-neo4j`, bolt 7687. | `writ/graph/db/` (mixin package composing `Neo4jConnection`) |
| **The CLI** | Operator surface: ingest, export, reconcile, validate, author, query, doctor, logs, decision memory. | `writ/cli.py` (Typer), plus the hook-facing `bin/lib/writ-session.py` facade |

**Canonical vs derived.** Neo4j is canonical. The tracked artifact is `writ-corpus.cypher` (portable dump; `writ import-cypher` wipes and replays it; install and CI seed from it). `bible/` is a derived, gitignored, local export; `writ reconcile` makes source win over graph and is the *only* prune path (ingest is upsert-only).

## 2. Request flows

**Prompt turn** (`UserPromptSubmit`): `auto-approve-gate.sh` scans for an approval phrase (mints the gate token, may advance a pending gate) and writes the session id to `/tmp/writ-current-session` for the two payload-less readers only (`hooks/git/post-commit`, and `session-start-bootstrap.sh`'s pre-rotation read); nothing resolves its own identity from that file, which is one file per machine naming whichever session took a turn last. Every hook takes its session id from its own payload or declines to act. Then `writ-rag-inject.sh` ensures the daemon (flock-guarded singleton start), auto-routes an unset mode from the prompt shape, and makes **one** `POST /prompt-bundle` call. That endpoint runs the ranked query, the always-on floor, and the methodology companion in-process and returns all three rendered blocks (it replaced ~16 cold python spawns per turn). Delivery is plain stdout, which reaches the model only on `UserPromptSubmit`/`SessionStart`; every Pre/PostToolUse hook must use `additionalContext` instead (`writ/shared/delivery.py` is the empirically verified table).

**Write path** (`PreToolUse` Write/Edit/NotebookEdit): `writ-pre-write-dispatch.sh` -> `POST /pre-write-check`, one call combining the gate decision, denial escalation (deny becomes ask after repeated denials), and file-context RAG on allow. Bash-mediated writes (`>`, `tee`, `cp`, `mv`, `sed -i`, `dd`) are extracted quote-aware by `writ-bash-write-gate.sh` and checked against the same `/session/{sid}/can-write`; credential paths are denied in every mode by `_is_credential_path` (`writ/session/gates.py`), which both gates share. The same extractor also reads interpreter one-liners (`python -c`, `node -e`, `perl -e`, `ruby -e`, `php -r`, including heredoc and piped-stdin forms), so a write performed inside an interpreter is a gate decision rather than a silent allow. Documented evasion limits, listed in the hook itself: variable indirection, `eval` and base64, a path assembled from pieces, `sh -c` / `bash -c` wrappers, awk/sed program text, an interpreter reached through a variable or alias, and `python -m MODULE` (deliberately unscanned, because matching it would deny every `python -m pytest` run).

**Commit path** (`PreToolUse` Bash, added 2026-08-06): `git commit` asks for human confirmation while a `writ-reviewer` verdict with CRITICAL findings stands. The verdict is recorded by `writ-subagent-stop.sh` straight from the `SubagentStop` payload, so the agent whose code was reviewed never carries its own critic's findings. It asks rather than refuses because any override the agent could set would re-open that defect; an unparseable verdict counts as blocking, and only a fresh clean reviewer verdict lifts it. Shared parser and blocking rule: `bin/lib/review_findings.py`, also behind `POST`/`GET /session/{sid}/review-findings`.

**Stop path**: pending-test runner (blocks on failure only in implementation/complete phase), violation enforcement (work mode), verify-before-claim (unoverridden quality scores under 3), and the deterministic comms gate (em/en dash, ` -- ` in prose). Blocking Stop hooks use stderr + non-zero exit guarded by `stop_hook_active`; a Stop hook's `additionalContext` would loop the turn.

**Fail-open discipline.** Every hook is daemon-first with a subprocess fallback (`_writ_session`, `bin/lib/common.sh`) and fails open when the daemon is down; failures are logged, not silent. The deliberate fail-closed exceptions: credential-path denial, the research triangulation gate, and token validation on `/advance-phase`.

## 3. The daemon

**Lifespan order** (`writ/server/__init__.py`): Neo4j connection -> `build_pipeline` (with the abstention threshold 0.30, the one call site that enables it) -> `MethodologyTriggerIndex.build_from_db` -> analyzer clients. Indexes are pre-warmed; request handlers do no synchronous I/O.

**49 endpoints** by group: retrieval (`/query`, `/prompt-bundle`, `/always-on`, `/methodology-companion`, `/conflicts`, `/rule/{id}`, `/subagent-role/{name}`), authoring (`/propose`, `/feedback`, `/analyze`), gates (`/pre-write-check`, `/session/{sid}/advance-phase`, `/session/{sid}/promote-candidate`), 27 session-state routes under `/session/{sid}/...` (including `/session/{sid}/prompt-state`) plus `/session/format`, decision memory (`/commit/capture`, `/recall`), `/git-hooks/auto-install`, and the explorer (`/dashboard`, `/explore`, `/graph`, `/node/{id}`). Count them: `grep -rcE '@router\.(get|post)' writ/server/routes/`.

**Error contract.** Logical failures return HTTP 200 with an `error` key; 422 is Pydantic validation; a raising `/query` re-raises after emitting an exception row (hooks fail open on the 500). `POST /session/{id}/mode` is the one route that returns a real 400 (invalid mode).

**Health semantics.** `GET /health` reports rule/mandatory/category counts, route distribution, index state, startup time, cache dir, friction path. `degraded` means the index is warm but `rule_count == 0`: the daemon outlived a re-seed and its index disagrees with the DB.

**Telemetry.** A middleware emits one `daemon_request` metrics row per request (route template, not concrete path; `/health` excluded), in a `finally` so failures are recorded too. Every `/query` emits a `retrieval_result` row including the abstention signal, hit or miss, which is the data for retuning the 0.30 threshold.

**State the daemon does not own:** session caches are files under `<install>/var/session` (hooks and CLI read them directly when the daemon is down); route modules read `server._pipeline` / `server._db` as live attributes (the monkeypatch seam; never `from`-import them).

## 4. Operations

The daemon runs as a **systemd user service** in production (`scripts/install-server-service.sh`: waits for Neo4j, `Restart=on-failure`, plus the daily `writ-logs-rotate.timer`). Restart with `systemctl --user restart writ-server`, never `stop-server.sh` (it fights auto-restart). On-demand fallback: the SessionStart hook and `scripts/ensure-server.sh` share one flock-guarded singleton start routine (`scripts/lib/writ-server-lib.sh`); a healthy daemon is never restarted by them. The daemon log path has one owner, `writ_default_server_log()`: `$WRIT_LOG` > `$WRIT_LOG_ROOT/server.log` > `$CLAUDE_PLUGIN_DATA/server.log` > `<install>/var/logs/server.log`; never `/tmp` (systemd empties it at boot, the root cause of a past session-cache wipe).

## 5. Plugin packaging

- **`.claude-plugin/plugin.json`**: name, version, description, `commands` pointer. It deliberately declares **no `hooks` key** (auto-discovery of `hooks/hooks.json` at the plugin root; declaring it collides as "Duplicate hooks file detected" and every hook fails to load) and **no `agents` key** (measured on Claude Code 2.1.220: a declared agents array loads zero agents; auto-discovery of the root `agents/` directory loads all five).
- **`.claude-plugin/marketplace.json`**: the same-repo single-plugin marketplace so `claude plugin marketplace add infinri/Writ` resolves. Version must match `plugin.json` and `pyproject.toml` (a test pins all three).
- **Install paths**: marketplace plugin (venv at `${CLAUDE_PLUGIN_DATA:-~/.cache/writ}/.venv`, survives upgrades), `~/.claude/skills/writ` auto-discovery, or an undiscovered path with `--hooks` seeding from the generated `templates/settings.json`. Full detail: `docs/install.md`.

## 6. Known seams (2026-07-31)

| Seam | Status | Anchor |
|---|---|---|
| ~~Authority-preference re-ranking~~ | RESOLVED 2026-08-06: configurable via `[retrieval] authority_preference_threshold`, default still 0.0 because a gold-set sweep measured no change from enabling it (the corpus's one ai-provisional node is not semantic-routed). The tiebreak coupling is fixed: the preference now runs after the tiebreak, not before. | `writ/retrieval/pipeline.py` |
| ~~Bundle-cohesion weight~~ | RESOLVED 2026-08-06: deleted. No weight improved all three metrics, and the methodology channel it was built for shipped deterministic. See `benchmarks/RANKING-LEVERS-2026-08-06.md`. | `writ/retrieval/ranking.py` |
| `/analyze` LLM escalation | Real code; the `anthropic` SDK is not a declared dependency, so a default install gets "SDK not installed" placeholder findings and the calibration log fills with placeholders. | `writ/analysis/llm.py` |
| BM25 persistence | The HNSW index persists with checksum guards; the Tantivy index rebuilds on every daemon start. | `writ/retrieval/keyword.py` |
| `debug` log stream | Reserved retention entry, never written; debug output stays in `WRIT_DEBUG`-gated `/tmp` sinks. | `writ/session/log_rotation.py` |
| `ENFORCEMENT_CONVENTIONS` | Discoverability convention only, not validated. | `writ/graph/schema.py:70` |
| `detect_confidence_defaults` | Exists as a callable check but is not wired into `run_all_checks`. | `writ/graph/integrity/` |
| Marketplace listing | Install works end to end; community submission pending. | `docs/SUBMISSION.md` |

Closed seams worth knowing the history of: the Stage-1 route filter and the `/methodology-companion` channel both shipped (the companion is live inside `/prompt-bundle`); `authority` promotion has a live path (`promotion.py:220` stamps `ai-promoted`); the edge OR-match is registry-derived; `Abstraction` identity and edge resolution are project-scoped.

## 7. Phase history (condensed)

Phase 0 introduced data-driven `Category` routing and parity. Phase 1 added the methodology node types and the trigger index. Phase 3 added authoring governance and the structural gate. Phase 5 added friction analytics. Phase 6 added the graduation loop and the five-state provenance model. Later programs: the POL waves (dedup, god-module splits, hook latency), decision memory, the logging overhaul (typed streams), the investigation engine, and the plugin/marketplace install. Historical phase documents are context, not current state; when in doubt, trust the code and the live graph.

```bash
docker exec writ-neo4j cypher-shell -u neo4j -p writdevpass \
  "MATCH (n) WHERE n.project='writ' RETURN count(n);"
.venv/bin/python -m pytest   # 367 modules, ~5,700 tests; needs the venv interpreter
```
