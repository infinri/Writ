# M0 reference harness

**Status:** M0 tooling only. Not the V2 kernel, not shipped, not imported by production code ([D-001](../docs/implementation/DECISION-LOG.md)).

| Path | Purpose |
|---|---|
| `writ_m0/audit/` | V1 baseline audit generators reading the pinned main commit through Git |
| `tests/` | Pytest suite, isolated from the V1 test configuration |

Generated audit evidence lives in [docs/audit/m0](../docs/audit/m0/SUMMARY.md).

```bash
uv venv m0/.venv -p 3.12
uv pip install -p m0/.venv/bin/python -r m0/requirements.txt
cd m0
.venv/bin/python -m pytest
.venv/bin/python -m writ_m0.audit check
```
