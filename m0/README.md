# M0 reference harness

**Status:** M0 tooling only. Not the V2 kernel, not shipped, not imported by production code ([D-001](../docs/implementation/DECISION-LOG.md)). Its job is to make the contract and invariant expectations executable so the M1 Rust and Go prototypes have an oracle ([parity suite](../docs/implementation/M1-PARITY-SUITE.md)).

| Module | Role | Invariants exercised |
|---|---|---|
| `writ_m0/audit/` | V1 baseline audit generators (pinned main commit, via git) | evidence for A-01..A-10 |
| `contracts.py` | Schema registry with strict date-time checking | contract validation |
| `canonical.py` | Canonical JSON and `sha256:` digests (D-006) | INV-04, INV-14 |
| `evaluation.py` | Domain-neutral evidence evaluator | INV-24, INV-25 |
| `reference_packs.py` | Evidence-kind checks owned by the three reference packs | P-15, P-17 |
| `decision.py` | Pure decision oracle (D-008) | INV-03..05, INV-10, INV-13, INV-17 |
| `ingress.py` | Adapter-sealed broker ingress (D-010) | INV-02, INV-05, T1 |
| `broker.py` | Ledger, approvals, single-use permits, receipts | INV-01, INV-06..09 |
| `hosts.py` | Host adapters, principal proofs, canonical targets | INV-02, INV-14, INV-19, INV-22, P-01 |
| `providers.py`, `runner.py` | Stub model adapters and managed runner | INV-21, INV-26, P-13, P-18, P-19 |
| `packs.py` | Pack verification and task instantiation | INV-23, P-16 |

Generated audit evidence lives in [docs/audit/m0](../docs/audit/m0/SUMMARY.md).

```bash
uv venv m0/.venv -p 3.12
uv pip install -p m0/.venv/bin/python -r m0/requirements.txt
cd m0
.venv/bin/python -m pytest
.venv/bin/python -m writ_m0.audit check
```
