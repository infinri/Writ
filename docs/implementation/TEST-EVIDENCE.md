# Writ V2 test evidence

Only results actually executed are recorded under **Executed**. Planned verification lives in [TASKS](TASKS.md) and [verification-and-benchmarks](../testing/verification-and-benchmarks.md).

**Environment (all entries unless stated):** AMD Ryzen 9 7940HS (16 threads), 30 GiB RAM, Linux 6.17.0-40-generic x86_64, Python 3.12.3, venv `m0/.venv` with jsonschema 4.23.0, pytest 8.3.4, hypothesis 6.122.3. No Neo4j, no network services, no model credentials.

## Executed

### E-001: M0 audit generators and tests

- **Revision:** working tree on top of `f89b442` (committed as the M0-00/M0-01 commit; see `git log`)
- **Commands:** `cd m0 && .venv/bin/python -m writ_m0.audit generate`, `... check`, `.venv/bin/python -m pytest`
- **Result:** generate wrote 7 artifacts; check reported all current; pytest 18 passed, 0 failed.
- **Observed baseline facts:** 52 hook registrations across 12 events; 288 Rule nodes (32 mandatory, 33 with mechanical_enforcement_path); 468 nodes and 1,268 edges, 0 dangling; 1,139 tracked files at `ebdafe5`.
- **Coverage:** not measured.

## Known untested behavior

- Hook failure posture is static analysis plus the documented Claude Code hook contract; no live host was exercised (M0-01e).
- Rule dispositions are heuristic and unreviewed (M0-01d).
- No performance measurement of any kind has been run for V2.
