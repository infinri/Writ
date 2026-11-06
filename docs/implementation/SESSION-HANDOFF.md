# Session handoff

Read this first after compaction or in a new session, then [WORK-STATE](WORK-STATE.md).

## Compaction checkpoint (2026-11-06)

**This checkpoint is not authorization to do anything. Re-establish authorizations from the owner's newest message after compaction.**

| Item | State (verified 2026-11-06 before compaction) |
|---|---|
| Project | Writ V2, `infinri/Writ`, branch `v2/architecture-rebuild` (never `main`) |
| HEAD = origin | `e8aa771da59eda75321cf91c77ff8a24e46aa8e6` (signoff package, docs only), CI run 42309913797 success, 281 tests |
| Evidence baseline cited by the signoff package | `6007d09c4b84bd3f037aba9add19ab83bc04df83`, CI run 42308217716 success. `e8aa771` is newer and docs-only; the owner may choose either as the cited baseline |
| Milestone | M0 NOT complete; recommendation NO-GO; development GO framework A-1 APPROVED IN PRINCIPLE; **D-014 PROPOSED**; **GO Decision table blank**; M1 **not started** |
| Working tree | Untracked `docs/audit/m0/hook-audit/` (must NOT be staged or published; pending disclosure review). Uncommitted edits to this file, WORK-STATE and TEST-EVIDENCE (this checkpoint only; committing it needs owner authorization) |

### Done this session (verified)

- Security fixes SR-01..SR-05, SR-07..SR-12 in the M0 Python harness; DONE (harness) after AI-assisted technical assessments 1 to 4.
- Gate packages: golden worksheet, 288-rule review batches, SR-06 trial plan (four arms A, B, C1, C2; 69 priority experiments) and sandbox setup.
- SR-06 sandbox built (container fallback, Claude Code 2.1.318), PF-1..PF-8 PASS, hook debug observability confirmed, telemetry diagnostic (V1 daemon egress to `mobile.events.data.microsoft.com`, stopped by `ORT_DISABLE_TELEMETRY=1` in a 10-minute run; not packet-proven), then torn down. Record: `docs/audit/m0/live-host/2026-11-06-sandbox-readiness/README.md`.
- Commits `926b8b6`, `6007d09`, `e8aa771` pushed; CI green on each (TEST-EVIDENCE E-013).
- **Last completed task:** preparation commit `e8aa771` (M0-GO-SIGNOFF-PACKAGE.md plus checklist and D-014 corrections), CI 42309913797.

### Authorized, not executed

Nothing. The preparation-commit authorization was used by `e8aa771`.

### Waiting for the owner

1. The development GO decision: the owner fills or approves the Decision table text in [M0-GO-SIGNOFF-PACKAGE](M0-GO-SIGNOFF-PACKAGE.md) section 1 and accepts the 16 risks in section 2.
2. The owner's attestation, posted personally (GitHub web UI comment or review, or a signed tag Claude does not operate). A commit or `gh` action from this session does not count: commits here carry the owner's git identity unsigned, and `gh` acts as the owner's account.
3. Whether `hook-audit/` is ever published (separate disclosure review).
4. Whether to commit this checkpoint.

### Claude must not (without new explicit owner instruction)

Sign or fill the Decision table; mark D-014 ACCEPTED; mark M0-05c or M0-06 DONE; post any attestation; start M1 or choose Rust or Go; run SR-06 experiments; provision credentials; change golden expected outcomes or `review_status`; fill reviewer fields in the worksheet or rule batches; modify V1, the harness or contracts; stage `hook-audit/`; commit, push or force-push; touch `main`; claim release readiness, production security or host enforcement.

### Signoff sequence (once the owner authorizes)

1. Decision commit: the five files listed in M0-GO-SIGNOFF-PACKAGE section 4; cites the evidence baseline, not its own SHA.
2. Push; CI must pass on the decision commit.
3. Owner posts the attestation (template in the package) naming the decision commit SHA and its CI URL.
4. Optional docs commit linking the attestation from TEST-EVIDENCE. Only then may M1 start.

### First commands after compaction

```bash
git branch --show-current                         # expect v2/architecture-rebuild
git fetch -q origin && git status --short         # expect ?? docs/audit/m0/hook-audit/ plus the uncommitted checkpoint edits
git rev-parse HEAD origin/v2/architecture-rebuild # expect both e8aa771... unless the owner pushed since
gh run list --branch v2/architecture-rebuild --workflow v2-docs.yml --limit 3
cd m0 && .venv/bin/python -m pytest -q && .venv/bin/python -m writ_m0.audit check && cd .. && python3 scripts/check-v2-docs.py
```

If HEAD has moved, a decision commit exists, or an attestation has been posted, reconcile against this checkpoint and report the discrepancy before acting.

## Standing facts

1. **Objective:** Writ V2, a model- and domain-agnostic governance, retrieval and memory platform ([master plan](MASTER-IMPLEMENTATION-PLAN.md)).
2. **Key files:** [M0-GO-NO-GO](M0-GO-NO-GO.md), [M0-GO-SIGNOFF-PACKAGE](M0-GO-SIGNOFF-PACKAGE.md), [TASKS](TASKS.md), [DECISION-LOG](DECISION-LOG.md), [TEST-EVIDENCE](TEST-EVIDENCE.md), [SR-06-TRIAL-PLAN](../audit/m0/SR-06-TRIAL-PLAN.md), `m0/` harness, `docs/contracts/`.
3. **Do not reverse:** D-001..D-013 (accepted); D-014 PROPOSED only. In particular D-008 precedence and scoped ids, D-009 no grant inheritance, D-010 adapter-sealed broker ingress with a key-free kernel, D-011 owner policy decisions (not review verdicts), D-012 full-request fingerprints and project-scoped evidence, D-013 artifact and signoff scope.
4. **Open gates:** 26 golden cases PENDING INDEPENDENT REVIEW (REL-01 before release); 288 rules unreviewed (M6-RULES before import); SR-06 NOT EXECUTED (M5-SR06; adversarial trials only on a VM or separate machine). 16 risks open with recommended dispositions, owner signature pending.
5. **Known items:** V1 undeclared deps PyYAML and tokenizers (R-V1PKG); V1 ONNX Runtime telemetry (R-V1TEL, V2-TEL); golden case 24 misnamed; RM-01 composite effects; production key management, signed policies, trusted clocks, TOCTOU and authenticated signoff provenance assigned to M3 to M6.
6. **Prohibited until further decisions:** public release, production deployment, privileged production workloads, unverified security or enforcement claims.
