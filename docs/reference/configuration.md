# Configuration

Every knob, its real location, and its default. The theme to keep straight: `writ.toml` holds five sections; most values people expect in config are deliberately code constants with tests pinning them.

## writ.toml

At the install root, gitignored; `writ.toml.example` is the template. Readers in `writ/config.py`, each with a coded default; a missing or malformed file falls back silently to the caller (one warning line to stderr, a `config_resolved` metrics event recording key *names*, never values, since the same file holds credentials).

| Section | Keys | Default |
|---|---|---|
| `[neo4j]` | `uri`, `user`, `password` | `bolt://localhost:7687`, `neo4j`, `writdevpass` (**dev-only**; silently used whenever the file is absent, so override for any real deployment) |
| `[hnsw]` | `cache_dir` | `~/.cache/writ/hnsw` |
| `[bitbucket]` | `email`, `token` | none (PR sync off; the token is never logged) |
| `[logs]` | `backup_dest` | none (`writ logs backup` requires `--dest`) |
| `[egress]` | `allow_hosts` | `localhost`, `127.0.0.1`, `::1`, `[::1]`, `$WRIT_HOST` (the Bash egress guard's allowlist; configured hosts are unioned with those defaults and with `WRIT_EGRESS_ALLOW_HOSTS`, compared on host only with the port ignored) |

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
| `WRIT_CACHE_DIR` | Root for everything a session writes: the session cache, the pending-test markers `writ-mark-pending-test.sh` leaves for the Stop hook, and the per-file lint logs from `validate-file.sh`. Those last two used to resolve against the install directory regardless of this variable, so an isolated run still wrote into the live checkout. | `<install>/var/session` (never `/tmp`: systemd empties it at boot; that wipe once destroyed every session cache) |
| `WRIT_LOG_ROOT` | Typed log streams root | `<install>/var/logs` |
| `WRIT_LOG_PROJECT` | Override the per-project log scope (sanitized against path traversal) | git-derived project name, else `writ` |
| `WRIT_FRICTION_LOG` | Collapse **all** streams into one file (test isolation, single-log operators) | unset |
| `WRIT_DEBUG` | Enable the `/tmp` debug sinks (`WRIT_HOOK_LOG` names one of them) | off |
| `WRIT_NO_AUTOSTART` | Suppress hook-side daemon/Neo4j autostart | unset |
| `WRIT_ALLOW_EMBEDDING_FALLBACK=1` | Permit the sentence-transformers fallback when the ONNX model is absent | off (hard error instead) |
| `WRIT_EGRESS_ALLOW_HOSTS` | Comma-separated extra hosts the Bash egress guard never asks about; unioned with `[egress] allow_hosts` | unset (loopback + `$WRIT_HOST` only) |
| `WRIT_CONFIG_PATH` | Config file `get_egress_allow_hosts()` reads when called with no explicit path (the hook-side test seam; the other readers keep the fixed install-root location) | `<install>/writ.toml` |
| `WRIT_CONTEXT_WINDOW_TOKENS` | Context-pressure reference for the statusline/watcher; validated 1,000-10,000,000 at daemon startup, warn-only | 200,000 hook-side |
| `WRIT_BLACKBOX=1` (or `~/.claude/writ-blackbox.on`) | Raw hook-payload capture to `~/.claude/writ-blackbox.jsonl` | off |
| `WRIT_READ_JUNK_GATE=enforce` | Turn the read-junk gate from observe-only into blocking; `WRIT_READ_SIZE_KB` sets the oversize bound | `observe` / 100 KB |
| `WRIT_REALIGN_CACHE=1` | Let `ensure-server` restart a daemon whose cache dir diverged | off (systemd owns restarts) |
| `WRIT_NEO4J_URI` / `WRIT_NEO4J_USER` / `WRIT_NEO4J_PASSWORD` | Point one process at a different Neo4j instance without editing the shared `writ.toml`; wins over the file | unset (falls through to `[neo4j]`, then the coded defaults) |
| `WRIT_TEST_GRAPH=1` | Mark the connected instance disposable, permitting a whole-graph wipe. Required **together with** a `WRIT_NEO4J_URI` on a different `(host, port)`; neither alone is enough, because the marker records intent and cannot verify the target. **Set by `tests/conftest.py` at import for every suite run**, and by the CI test job at job level; an operator sets it by hand only when running against a scratch instance outside the suite | unset (wipes refused) |
| `WRIT_TEST_NO_ISOLATION=1` | Opt the test suite out of graph isolation: conftest forces no connection variables, runs no preflight, keeps the end-of-suite corpus restore, and `scripts/test-graph.sh` becomes a no-op. The suite then runs against whatever `writ.toml` configures, which on a developer machine is the live graph, and every `disposable_graph`-gated test skips. Exists so a machine with no docker daemon can still run the half of the suite that never touches Neo4j | unset (the suite isolates) |
| `CLAUDE_SESSION_ID` / `CLAUDE_JOB_DIR` | The only sources of session identity. There is no third tier: a hook that cannot read an id from the payload records a `critical_error` and declines to act rather than synthesizing one (`writ_require_session`, `bin/lib/common.sh`). | harness-provided |

`get_production_neo4j_uri()` deliberately ignores `WRIT_NEO4J_URI` and reads `writ.toml` only. It defines what the destructive-wipe guard treats as production, and a getter the caller can redirect could not answer that question: if the override fed both sides of the comparison, setting it would make every instance look non-production. See `writ/graph/db/_safety.py` and [testing](testing.md#the-suite-runs-against-its-own-neo4j-instance).

## Fixed paths

Gate approvals `<project_root>/.claude/gates/<session_id>/<gate>.approved` (one python source, `writ/session/locators.py::gate_dir`; `writ_gate_dir` in `bin/lib/common.sh` is the byte-identical bash mirror; removed at session end, so approvals never cross sessions); gate token `/tmp/writ-gate-token-<sid>` (deliberately `/tmp`: single-use, must byte-match the bash writer); session pointer `/tmp/writ-current-session` (still written on each prompt, but **no longer an identity source**, because it names whichever session on this machine took a turn most recently; the only two readers left are `hooks/git/post-commit`, which git never hands a Claude Code payload, and `session-start-bootstrap.sh`, which wants the PRE-rotation id that by definition is not in the payload); ONNX model `~/.cache/writ/models/onnx/`; origin-context store `~/.cache/writ/origin_context.db`; daemon log via `writ_default_server_log()` (`$WRIT_LOG` > `$WRIT_LOG_ROOT/server.log` > `$CLAUDE_PLUGIN_DATA/server.log` > `<install>/var/logs/server.log`).
