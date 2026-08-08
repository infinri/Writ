# Writ Codebase: Development Guide

The code map for anyone (human or agent) modifying Writ's own source. Facts here are derived from the tree as of 2026-07-31; when in doubt, the code wins. Deep contracts live in `docs/reference/`.

## Layout

| Area | Role | Change risk |
|---|---|---|
| `writ/retrieval/` | The 5-stage pipeline (`pipeline.py`, ~900 lines), ranking constants (`ranking.py`), ONNX + HNSW (`embeddings.py`), Tantivy (`keyword.py`), adjacency cache (`traversal.py`), the deterministic companion (`trigger_index.py`), bundle render helpers (`prompt_bundle.py`, `always_on_filter.py`) | HIGH: every query. Run `make bench` after changes. |
| `writ/graph/schema.py` | Every Pydantic model + the single-source registries (`NODE_TYPE_MODELS`, `NODE_ID_FIELDS`, `SECTION_HEADERS`, `MANAGED_PROP_NAMES`) | HIGH: cascades to ingest, export, reconcile, API. |
| `writ/graph/db/` | `Neo4jConnection` composed from 9 store mixins (a package, not a file) | HIGH: all data access. |
| `writ/graph/` | `ingest.py` (parsing), `methodology_ingest.py` (write pipeline + reconcile oracle), `integrity/` (checker mixins), `export.py` counterpart at top level | HIGH for reconcile/parity paths. |
| `writ/server/` | FastAPI package: `__init__.py` (app, lifespan, global state), `models.py`, `routes/` (6 modules) | HIGH: hook contract. Routes read `server._pipeline` etc. as live attributes; never from-import them. |
| `writ/session/` | The state machine, 30+ modules (POL-6 split); `bin/lib/writ-session.py` is a re-export facade loaded both as a CLI and via importlib by the server | HIGH: gates and modes. Keep it acyclic; modules never import the facade. |
| `writ/analysis/`, `writ/compression/`, `writ/shared/` | Friction analytics, abstraction clustering, the logging router / budget / delivery tables | Moderate. |
| `hooks/hooks.json` + `hooks/scripts/` | 44 registrations, 12 events, 40 scripts (+1 statusline script) | HIGH: exit codes are the gate mechanism. |
| `bin/lib/common.sh` | The shared hook library (`load_hook_env`, `_writ_session`, exit-trap instrumentation, cache-dir resolver) | HIGH: every hook sources it. |
| `scripts/` | Bootstrap/deploy, migrations (one-shot, already applied), measurement tools, `render-docs.py` | Low, but `render-settings-template.py` and `render-docs.py` have `--check` drift modes. |

## Invariants that must hold

1. **No sync I/O in the query hot path**: indexes are pre-warmed at lifespan; `pipeline.query()` touches memory only.
2. **Mandatory rules never enter the ranked indexes** (`predicates.py` is the single source for both selection and validation).
3. **Weights sum to 1.0** (`RankingWeights.validate`); they are code constants in `ranking.py`, not configuration.
4. **`loaded_rule_ids` doubles as the ranked exclude list**; always-on rules are tracked separately or they become unretrievable.
5. **Gate tokens: claiming is consuming** (atomic rename); validate before claiming so a validator error never spends the token.
6. **Session caches only mutate under `mutate_cache`** (flock, reentrant); no hand-rolled read-modify-write.
7. **`(id, project)` composite identity everywhere**; edge endpoints resolve within the project; the OR-match derives from `NODE_ID_FIELDS`.
8. **Embeddings are 384-dim MiniLM via ONNX**; the sentence-transformers path exists only behind `WRIT_ALLOW_EMBEDDING_FALLBACK=1`.
9. **Graduation is a plain ratio** (50 observations, 0.75 positive); deliberately no statistical interval.
10. **Docs are not a test surface**: never assert on markdown prose; generated pages drift-check via `make docs-check`, not pytest.
11. **Stop hooks block via stderr + exit code**, never `additionalContext` (turn-block loop); Pre/PostToolUse deliver to the model only via `additionalContext`.

## Configuration reality

`writ.toml` has five sections only (`[neo4j]`, `[hnsw]`, `[bitbucket]`, `[logs]`, `[egress]` -- the last one commented out in `writ.toml.example`, since `tests/test_w4_remainder_dead.py` pins the example's PARSED sections to the original four); everything else is code constants with tests pinning them. There is no generic `WRIT_`-prefix override mechanism; the real env vars are enumerated in `docs/reference/configuration.md`. Budget constants live in `writ/shared/budget.json` (edit the JSON, both Python and bash load it).

## Testing directives

- Run everything with the venv interpreter: `.venv/bin/python -m pytest` (system Python lacks onnxruntime). 367 modules, ~5,700 tests.
- After touching `writ/`: `make test`. After touching retrieval, ranking, or schema: also `make bench` (14 contractual targets; floors live in `tests/fixtures/regression_floors.py`, history is append-only).
- Roughly half the suite needs a reachable Neo4j; an empty-but-reachable graph fails by design (the anti-masking contract in `tests/_corpus.py`).
- The suite isolates itself at conftest import: port 8799, mkdtemp `WRIT_CACHE_DIR`, `WRIT_NO_AUTOSTART=1`, redirected logs; it restores the corpus from `writ-corpus.cypher` at session end. The full trap list: `docs/reference/testing.md`.
- After changing `hooks/hooks.json`: `make docs && scripts/render-settings-template.py` (both templates are generated); a new event mapping needs a fresh Claude Code session to register.
- After changing server routes or retrieval code on a live install: `systemctl --user restart writ-server`.

## Where the deep contracts live

`docs/reference/architecture.md` (system + seams), `graph-schema.md` (nodes/edges/reconcile/integrity), `retrieval.md` (stages + constants), `session-and-gates.md` (cache/modes/gates/token), `configuration.md`, `logging.md`, `decision-memory.md`, `testing.md`, `compression.md`, plus the generated `cli.md` / `http-api.md` / `hooks.md` / `rulebook.md`.
