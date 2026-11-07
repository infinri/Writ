# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

## Current checkpoint (2026-11-07, acceptance commit)

**This checkpoint is not authorization to do anything. Re-establish authorizations from the owner's newest message.**

| Item | State |
|---|---|
| Project | Writ V2, `infinri/Writ`, branch `v2/architecture-rebuild` (never `main`) |
| M0 | Closed **for development only**. D-014 ACCEPTED 2026-11-07: decision commit `38e2d1f` (CI 42319056546), owner attestation ([comment](https://github.com/infinri/Writ/commit/38e2d1f31fb11a145cb7a109934e85b018dd1d60#commitcomment-208565071)) verified (E-015). Release and production NO-GO |
| M1 | Active: ADR-0002 bake-off, tasks M1-00..M1-08, AG-M1. Rust and Go toolchains not yet installed |
| Architecture | D-015 Control Center (spec, ADR-0007); D-016 Acceptance Gate Integrity requirement accepted, ADR-0008 PROPOSED (owner: approved in principle; must add artifact snapshots or use-time mediation, and designed vs tested vs verified guarantees, before ACCEPTED) |
| Open | 26 golden PENDING INDEPENDENT REVIEW; 288 rules unreviewed; SR-06 NOT VERIFIED; 16 risks open |
| Working tree | Untracked `docs/audit/m0/hook-audit/` (never stage or publish without separate authorization) |

### Claude must not (without new explicit owner instruction)

Choose Rust or Go before measurements; copy the Python harness into a prototype; alter golden expected outcomes or `review_status`; claim rule review, SR-06 or host enforcement; mark ADR-0008 ACCEPTED; modify V1; run SR-06 or adversarial experiments on the shared Docker daemon; stage `hook-audit/`; push without confirmation; force-push; touch `main`; treat M1 authorization as release authorization.

### First commands in a new session

```bash
git branch --show-current
git fetch -q origin && git status --short
git log --oneline -5 && git rev-parse HEAD origin/v2/architecture-rebuild
gh run list --branch v2/architecture-rebuild --workflow v2-docs.yml --limit 3
cd m0 && .venv/bin/python -m pytest -q && .venv/bin/python -m writ_m0.audit check && cd .. && python3 scripts/check-v2-docs.py
```

## Standing facts

1. **Objective:** Writ V2, a model- and domain-agnostic governance, retrieval and memory platform ([master plan](MASTER-IMPLEMENTATION-PLAN.md)).
2. **Key files:** [M0-GO-NO-GO](M0-GO-NO-GO.md), [M0-GO-SIGNOFF-PACKAGE](M0-GO-SIGNOFF-PACKAGE.md), [TASKS](TASKS.md), [DECISION-LOG](DECISION-LOG.md), [TEST-EVIDENCE](TEST-EVIDENCE.md), [SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md), `m0/` harness, `docs/contracts/`.
3. **Do not reverse:** D-001..D-013 (accepted); D-014 ACCEPTED 2026-11-07 (development only; release standard revisited at release milestone); D-015, D-016. In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 adapter-sealed broker ingress with a key-free kernel, D-011 owner policy decisions (not review verdicts), D-012 full-request fingerprints and project-scoped evidence, D-013 artifact and signoff scope.
4. **Open gates:** 26 golden cases PENDING INDEPENDENT REVIEW (REL-01 before release); 288 rules unreviewed (M6-RULES before import); SR-06 NOT EXECUTED (M5-SR06; adversarial trials only on a VM or separate machine). 16 risks open with recommended dispositions, owner signature pending.
5. **Known items:** V1 undeclared deps PyYAML and tokenizers (R-V1PKG); V1 ONNX Runtime telemetry (R-V1TEL, V2-TEL); golden case 24 misnamed; RM-01 composite effects; production key management, signed policies, trusted clocks, TOCTOU and authenticated signoff provenance assigned to M3 to M6.
6. **Prohibited until further decisions:** public release, production deployment, privileged production workloads, unverified security or enforcement claims.
