# Logging and observability

The shipped logging system: typed streams, one router, two-tier rotation, durable audit. Source of truth: `writ/shared/logging.py` (router, classification, paths), `writ/session/log_rotation.py` (sweep), `log_read.py` / `log_backup.py` (read and backup surfaces).

## Streams

One directory per project under `<install>/var/logs/` (`WRIT_LOG_ROOT` overrides; project scope from git identity, `WRIT_LOG_PROJECT` overrides, sanitized against traversal). `STREAM_MAP` classifies ~40 event names; an unmapped event defaults to `friction`, never dropped.

| Stream | Holds | Retention |
|---|---|---|
| `audit.jsonl` | The oversight record: `write_attempt`, `gate_decision` (allow AND deny), `gate_denial`, `mode_change`, `phase_advance`, `agent_self_approval_blocked`, `candidate_promoted`, `quality_judgment`, `memory_policy_deny`, `verification_evidence`, `citation_recorded`, `committed_file_not_in_plan`, `read_blocked`, exit-plan events, `session_end` | 365 days |
| `friction.jsonl` | Worth fixing: `repeated_denial`, `hallucinated_rule_ids`, approval pattern hits/misses, `subagent_type_fallback`, `*_failed` fallbacks, compaction boundaries | 365 days |
| `metrics.jsonl` | Volume telemetry: `hook_execution`, `daemon_request`, `retrieval_result`, `hnsw_cache`, `config_resolved`, `rag_query`, `always_on_inject`, sub-agent lifecycle, playbook steps | 90 days |
| `errors.jsonl` | `exception` rows from `emit_exception` (bounded tracebacks, 2,000 chars) | 365 days |

A `debug` retention entry exists but the stream is deliberately unbuilt: debug output stays in `WRIT_DEBUG`-gated `/tmp` sinks (a recorded decision, not an omission).

## The router

`emit(event, ...)` is the single write path for Python and bash alike (bash goes through `bin/lib/friction-append.py`; hooks buffer decision + timing rows and emit once from an EXIT trap that covers every exit path including `set -e` aborts). Contract: `emit` never raises; a failed primary write degrades to `<root>/_fallback.jsonl` so a security event is never dropped; `json.dumps(default=str)` means a non-JSON field can't turn a log call into a crash. `WRIT_FRICTION_LOG`, when set, collapses every stream into that one file (the test-isolation path). Each line: `{ts, session, mode, event, ...fields}`.

## Rotation, retention, backup

Two tiers so nothing slips through: the router rolls any stream file at 50 MB before appending (stat-only check, fail-open), and the daily systemd timer (`writ logs rotate`, installed by `install-server-service.sh`) rotates anything oversize or pre-today, gzips archive generations, prunes past retention, and sweeps aged `/tmp` scratch. Classification is strictly by filename shape, never parent directory, so a project literally named `archive` can't have its live streams mistaken for archives (a real regression this guards). Two root files are deliberately unmanaged: `_fallback.jsonl` (rotating the failure catcher would be self-defeating) and `calibration.jsonl` (bounded at the writer, 100 lines).

Analyzers read archives plus live files (`read_streams` unions every generation, oldest first), so rotation never blinds `writ analyze-friction`, `writ audit-session`, or `writ metrics`; that blindness was a real found-and-fixed regression. `writ logs backup [--dest]` copies gzipped generations elsewhere, idempotent and self-pruning against recursive nesting.

## Read surfaces

`writ logs tail --stream <s> -n N` (bounded backward read), `writ logs stats` (live line/byte counts, archive counts, timestamp range), `writ logs list` (projects and streams), `GET /dashboard` (HTML render of the same analyzer lenses; delegates, never recomputes). Daemon stdout under systemd lives in journald: `journalctl --user -u writ-server`.

## Caveats

The Writ self-repo's pre-2026-07 legacy `workflow-friction.log` is ~86% test-synthetic; real project streams are clean. `calibration.jsonl` holds placeholder LLM verdicts unless the `anthropic` SDK was installed (see `docs/reference/architecture.md`, known seams).
