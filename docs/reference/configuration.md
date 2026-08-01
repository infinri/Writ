# Configuration

Every knob, its real location, and its default. The theme to keep straight: `writ.toml` holds exactly four sections; most values people expect in config are deliberately code constants with tests pinning them.

## writ.toml

At the install root, gitignored; `writ.toml.example` is the template. Readers in `writ/config.py`, each with a coded default; a missing or malformed file falls back silently to the caller (one warning line to stderr, a `config_resolved` metrics event recording key *names*, never values, since the same file holds credentials).

| Section | Keys | Default |
|---|---|---|
| `[neo4j]` | `uri`, `user`, `password` | `bolt://localhost:7687`, `neo4j`, `writdevpass` (**dev-only**; silently used whenever the file is absent, so override for any real deployment) |
| `[hnsw]` | `cache_dir` | `~/.cache/writ/hnsw` |
| `[bitbucket]` | `email`, `token` | none (PR sync off; the token is never logged) |
| `[logs]` | `backup_dest` | none (`writ logs backup` requires `--dest`) |

**Not in writ.toml, by design (code constants):** ranking weights (`writ/retrieval/ranking.py`), the 0.30 abstention threshold (`writ/retrieval/pipeline.py`), gate cosine thresholds 0.95/0.85 (`writ/gate.py`, `writ/graph/schema.py`), graduation thresholds 50 / 0.75 (`writ/frequency.py`), context-budget bands 2,000 / 8,000 (`writ/retrieval/ranking.py`).

## Other config files

| File | Purpose |
|---|---|
| `writ/shared/budget.json` | `default_budget=8000`, rule render costs `full=200 / standard=120 / summary=40`, `subagent_budget=null` (unlimited), `always_on_cap=5000`. Loaded at import time by `writ/session/config.py` (a missing file crashes imports, deliberately loud). |
| `bin/lib/gate-categories.json` | Write-gate exclusion globs (tests, migrations, `__init__.py`, `.claude/` config + markdown) plus framework-detection markers. Glob caution: `*` spans `/` and matches the raw path; keep patterns extension-anchored or exact filenames. |
| `bin/lib/test-paths-defaults.json` + `<project>/.claude/writ.json` | Source-to-test path mappings for the TDD gate and pending-test runner; a project file with `extends_defaults: true` overrides same-named patterns and appends the rest. |
| `hooks/hooks.json` | Hook wiring, single source; `templates/settings.json` is generated from it (`scripts/render-settings-template.py --check` detects drift). |
| `docker-compose.yml` | Neo4j only (`writ-neo4j`, ports 7474/7687, `NEO4J_AUTH` matching the config default). The daemon is never containerized. |

## Environment variables

| Variable | Effect | Default |
|---|---|---|
| `WRIT_HOST` / `WRIT_PORT` | Daemon target for every hook and CLI call | `localhost` / `8765` |
| `WRIT_CACHE_DIR` | Session-cache directory | `<install>/var/session` (never `/tmp`: systemd empties it at boot; that wipe once destroyed every session cache) |
| `WRIT_LOG_ROOT` | Typed log streams root | `<install>/var/logs` |
| `WRIT_LOG_PROJECT` | Override the per-project log scope (sanitized against path traversal) | git-derived project name, else `writ` |
| `WRIT_FRICTION_LOG` | Collapse **all** streams into one file (test isolation, single-log operators) | unset |
| `WRIT_DEBUG` | Enable the `/tmp` debug sinks (`WRIT_HOOK_LOG` names one of them) | off |
| `WRIT_NO_AUTOSTART` | Suppress hook-side daemon/Neo4j autostart | unset |
| `WRIT_ALLOW_EMBEDDING_FALLBACK=1` | Permit the sentence-transformers fallback when the ONNX model is absent | off (hard error instead) |
| `WRIT_CONTEXT_WINDOW_TOKENS` | Context-pressure reference for the statusline/watcher; validated 1,000-10,000,000 at daemon startup, warn-only | 200,000 hook-side |
| `WRIT_BLACKBOX=1` (or `~/.claude/writ-blackbox.on`) | Raw hook-payload capture to `~/.claude/writ-blackbox.jsonl` | off |
| `WRIT_READ_JUNK_GATE=enforce` | Turn the read-junk gate from observe-only into blocking; `WRIT_READ_SIZE_KB` sets the oversize bound | `observe` / 100 KB |
| `WRIT_REALIGN_CACHE=1` | Let `ensure-server` restart a daemon whose cache dir diverged | off (systemd owns restarts) |
| `CLAUDE_SESSION_ID` / `CLAUDE_JOB_DIR` | First two tiers of session-id resolution | harness-provided |

Neo4j credentials come from `writ.toml` only; there is no `WRIT_NEO4J_*` override.

## Fixed paths

Session pointer `/tmp/writ-current-session`; gate token `/tmp/writ-gate-token-<sid>` (deliberately `/tmp`: single-use, must byte-match the bash writer); ONNX model `~/.cache/writ/models/onnx/`; origin-context store `~/.cache/writ/origin_context.db`; daemon log via `writ_default_server_log()` (`$WRIT_LOG` > `$WRIT_LOG_ROOT/server.log` > `$CLAUDE_PLUGIN_DATA/server.log` > `<install>/var/logs/server.log`).
