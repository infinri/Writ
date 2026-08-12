<!-- GENERATED FILE - do not edit. Source: writ/cli.py + writ/session/cli_dispatch.py. Regenerate with `make docs` (scripts/render-docs.py). -->


# CLI reference

Every `writ` command, generated from the Typer app. Run `writ <command> --help` for flags and arguments.

| Command | Description |
|---|---|
| `writ add` | Add a new rule to the graph with relationship suggestion and validation |
| `writ analyze-friction` | Summarize workflow-friction.log: event counts, hook p95s, top rules, gate activity |
| `writ audit-session` | Per-session timeline + summary from workflow-friction.log |
| `writ compress` | Cluster rules into abstraction nodes for compressed retrieval |
| `writ corpus-footprint` | No-API corpus footprint: rank per-rule bloat (WASTE) cut-candidates. Proposes, never applies |
| `writ doctor` | Run the operability self-diagnostic; exit non-zero if any check fails |
| `writ edit` | Edit an existing rule in the graph |
| `writ efficacy-ab` | NUMERATOR harness: run a matched-task A/B and score cost + defect-caught |
| `writ export` | Regenerate Markdown from graph. Overwrites output directory |
| `writ export-cypher` | Dump the whole graph as a portable Cypher replay script |
| `writ feedback` | Record positive or negative feedback for a rule (hook integration) |
| `writ git-hooks bootstrap` | Register the writ project bound to its remote_url before first auto-register |
| `writ git-hooks install` | Install the Writ post-commit git hook into a repo (removes the retired prepare-commit-msg block) |
| `writ git-hooks uninstall` | Remove the Writ git-hook block from a repo (preserving other content) |
| `writ harvest` | Harvest git commits + transcript plans into decision-memory records |
| `writ import-cypher` | Rebuild the graph from a Cypher dump script produced by export-cypher |
| `writ import-markdown` | Import bible content (Rules + methodology) into the graph. Validates schema. Triggers export |
| `writ logs backup` | Copy the compressed archive generations to an off-root destination |
| `writ logs list` | List projects under the log root with their streams and archive counts |
| `writ logs rotate` | Rotate, compress, prune, and sweep the Writ log streams (the P2 backstop) |
| `writ logs stats` | Print per-stream live line/byte counts, archive count, and ts range |
| `writ logs tail` | Print the last N events of a stream (newest last), fail-open on missing |
| `writ memory audit` | Report Memory records whose project scope is wrong or unprovable. Repairs nothing |
| `writ memory backfill` | Upsert every existing memory file, then tombstone the ones whose file is gone |
| `writ memory list` | List a project's mirrored memories, most-recently-updated first |
| `writ migrate` | One-time migration of existing rules into graph |
| `writ pr sync` | Post the captured per-file reasons as file-level comments on the PR review |
| `writ propose` | Propose an AI-generated rule. Runs structural gate before ingestion |
| `writ prune` | Detect graph nodes absent from the bible markdown (parity violations) |
| `writ query` | CLI rule query for testing retrieval quality |
| `writ recall` | Read back the project's recent rule-grounded decisions from memory |
| `writ reconcile` | Make the graph match the source-of-truth: delete stale nodes/edges and clear stale props |
| `writ review` | Review AI-proposed rules. List, inspect, promote, reject, or downweight |
| `writ role-prompt` | Print the graph-canonical prompt template for a SubagentRole |
| `writ serve` | Start Writ service. Pre-warms indexes into memory |
| `writ status` | Health check: rule count, index status, last ingestion, stale rules |
| `writ token-audit` | FOOTPRINT observer (WRIT-TOKEN-BLUEPRINT P0): per-session token COST from a CC transcript |
| `writ transcript audit` | Report user turns that mix a bare text element with a tool_result element |
| `writ validate` | Run integrity checks: conflicts, orphans, staleness, redundancy |

## Session CLI (`bin/lib/writ-session.py`)

The hook-facing dispatcher; hooks call it when the daemon is unreachable. Simple commands take exactly `<session_id>`; complex commands parse their own flags.

| Kind | Subcommands |
|---|---|
| simple | `aggregate-findings`, `auto-feedback`, `check-escalation`, `clear-pending-violations`, `clear-rules-for-compaction`, `coverage`, `coverage-map`, `coverage-rollup`, `current-phase`, `lens`, `pending-violations`, `read`, `reset-after-compaction`, `staleness-check`, `synthesis-gate`, `triangulation-gate` |
| complex | `add-pending-violation`, `advance-phase`, `can-read-code`, `can-write`, `carry-forward-mode`, `format`, `invalidate-gate`, `metrics`, `mode`, `partition-scope`, `record-analysis`, `scope-estimate`, `should-skip`, `update` |
