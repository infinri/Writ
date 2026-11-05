# Writ V2 test evidence

Only results actually executed are recorded under **Executed**. Planned verification lives in [TASKS](TASKS.md) and [verification-and-benchmarks](../testing/verification-and-benchmarks.md).

**Environment (all entries unless stated):** AMD Ryzen 9 7940HS (16 threads), 30 GiB RAM, Ubuntu 24.04.3, Linux 6.17.0-40-generic x86_64, Python 3.12.3, venv `m0/.venv` with jsonschema 4.23.0, pytest 8.3.4, hypothesis 6.122.3, SQLite 3.45.1. No Neo4j, no network services, no model credentials.

## Executed

### E-001: M0 audit generators and tests

- **Revision:** committed as `5791cb7`
- **Commands:** `cd m0 && .venv/bin/python -m writ_m0.audit generate`, `... check`, `.venv/bin/python -m pytest`
- **Result:** generate wrote 7 artifacts; check current; pytest 18 passed, 0 failed.
- **Observed baseline facts:** 52 hook registrations across 12 events; 288 Rule nodes (32 mandatory, 33 with mechanical_enforcement_path); 468 nodes and 1,268 edges, 0 dangling; 1,139 tracked files at `ebdafe5`.

### E-002: Full M0 harness (contracts, evaluation, decision oracle, broker, independence, packs)

- **Revision:** working tree on `fd0ce8f`, committed in the WP-D/E checkpoint commit (see `git log`)
- **Command:** `cd m0 && .venv/bin/python -m pytest`; `.venv/bin/python -m writ_m0.audit check`; `python3 scripts/check-v2-docs.py`
- **Result:** 201 passed, 0 failed (test_audit 19, test_broker 26, test_contracts 48, test_decision 36, test_evaluation 20, test_independence 52). Audit artifacts current (8 files). Docs check OK: 45 Markdown, 15 HTML, 17 schemas.
- **Concurrency:** `test_broker.py -k concurrent` repeated 15 times, 15/15 passed (16-thread grant race, 8-thread duplicate dispatch).
- **Mutation checks (manual, reverted after):** removing the date-time format checker failed 4 contract tests; removing `AND uses < max_uses` from grant consume failed 2 broker tests. Confirms those tests detect the defects they target.
- **Coverage:** not measured.

## Not yet executed

- CI: the `m0-harness` job in `.github/workflows/v2-docs.yml` has not run (branch not pushed).
- Hook failure posture on a live host (M0-01e); rule semantic review (M0-01d).
- Any Rust or Go build, and any performance benchmark.
