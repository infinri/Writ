# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

## Current checkpoint (2026-11-06, decision commit)

**This checkpoint is not authorization to do anything. Re-establish authorizations from the owner's newest message.**

| Item | State |
|---|---|
| Project | Writ V2, `infinri/Writ`, branch `v2/architecture-rebuild` (never `main`) |
| Last pushed and CI-verified | `e8aa771` (signoff package), run 42309913797. Local commits after it: checkpoint `df87cf4`, then this decision commit |
| Evidence baseline cited by the decision | `6007d09`, run 42308217716 |
| Milestone | M0 NOT closed. Development GO **RECORDED, PENDING OWNER ATTESTATION** (owner instruction 2026-11-06). D-014 PROPOSED. Release and production NO-GO. M1 not started |
| Working tree | Untracked `docs/audit/m0/hook-audit/` (must NOT be staged or published; pending disclosure review) |

### Remaining signoff sequence ([signoff package](M0-GO-SIGNOFF-PACKAGE.md) section 3)

1. Push the decision commit alone (owner confirms the push), so CI runs on exactly that commit.
2. CI green on the decision commit.
3. The owner posts the attestation personally (GitHub web UI comment on the decision commit, or an owner-signed tag). Claude never posts it.
4. Claude verifies the attestation read-only (author `infinri`, after CI, names the SHA and the CI run, scope matches).
5. Acceptance commit: D-014 ACCEPTED; M0-05c, M0-06 DONE; M0 closed for development only; M1 and deferred risk tasks active.

### Claude must not (without new explicit owner instruction)

Post or edit the attestation or any GitHub comment; mark D-014 ACCEPTED before step 4; start M1 implementation before step 5; run SR-06 experiments; provision credentials; change golden expected outcomes or `review_status`; fill reviewer fields; modify V1 or the M0 harness; stage `hook-audit/`; push without confirmation; touch `main`; claim release readiness, production security or host enforcement.

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
3. **Do not reverse:** D-001..D-013 (accepted); D-014 PROPOSED (decision recorded, pending owner attestation). In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 adapter-sealed broker ingress with a key-free kernel, D-011 owner policy decisions (not review verdicts), D-012 full-request fingerprints and project-scoped evidence, D-013 artifact and signoff scope.
4. **Open gates:** 26 golden cases PENDING INDEPENDENT REVIEW (REL-01 before release); 288 rules unreviewed (M6-RULES before import); SR-06 NOT EXECUTED (M5-SR06; adversarial trials only on a VM or separate machine). 16 risks open with recommended dispositions, owner signature pending.
5. **Known items:** V1 undeclared deps PyYAML and tokenizers (R-V1PKG); V1 ONNX Runtime telemetry (R-V1TEL, V2-TEL); golden case 24 misnamed; RM-01 composite effects; production key management, signed policies, trusted clocks, TOCTOU and authenticated signoff provenance assigned to M3 to M6.
6. **Prohibited until further decisions:** public release, production deployment, privileged production workloads, unverified security or enforcement claims.
