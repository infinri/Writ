# M0 development-GO signoff package (prepared, not signed)

**Status: PREPARED FOR OWNER APPROVAL. Nothing in this file is a decision.** Prepared by Claude on 2026-11-06. The owner signs; Claude does not sign, impersonate the owner, invent verdicts or mark D-014 ACCEPTED.

| Input | Value |
|---|---|
| Evidence baseline | commit `6007d09c4b84bd3f037aba9add19ab83bc04df83`, CI run [42308217716](https://github.com/infinri/Writ/actions/runs/42308217716): both jobs success, 281 tests passed, audit 8 files current, docs and schema checks OK |
| Recommendation | GO WITH ACCEPTED RISKS, M1 development only (AI-assisted technical assessment; not independent human certification) |
| Framework | [M0-GO-NO-GO](M0-GO-NO-GO.md) amendment A-1, approved in principle; [D-014](DECISION-LOG.md) PROPOSED |

## 1. Exact decision text (owner to adopt, edit or decline)

For the Decision table in [M0-GO-NO-GO](M0-GO-NO-GO.md):

| Field | Prepared value |
|---|---|
| Decision | GO WITH ACCEPTED RISKS, scoped to M1 development only |
| Owner | *(owner's full name)* |
| Date | *(date of signing)* |
| Evidence baseline | `6007d09c4b84bd3f037aba9add19ab83bc04df83`, CI run 42308217716 |
| Decision commit and its CI | Recorded in the owner attestation after push (section 3); not in this commit |
| Accepted risks and scope | See the statement below and the 16 risk rows in section 2 |

Statement:

> I, *(owner's full name)*, authorize Writ V2 M1 development under amendment A-1 on the evidence baseline `6007d09`. Permitted: M1 Rust and Go prototype development, the ADR-0002 bake-off, and parity testing. The 26 golden expected outcomes are a provisional oracle only. The 26 independent human golden reviews, the semantic review of the 288 V1 rules and the SR-06 live-host verification remain **incomplete**. AI-assisted technical assessments 1 to 4 are not independent human certification. Public release, production deployment, privileged production workloads and any security or enforcement claim that has not been verified remain prohibited. I accept the 16 risks below with the dispositions, milestones and required negative tests stated; none is closed by this decision. The release standard, including independent human golden review, will be revisited by an explicit recorded decision at the release milestone and is not waived here.

## 2. Risk dispositions for acceptance (16)

All remain open. Accepting them authorizes M1 development only.

| # | Risk | Disposition | Milestone | Required negative tests before closure |
|---|---|---|---|---|
| 1 | R-G1 no independent human golden verdicts | Accept for M1; review stays unresolved (REL-01) | Release | Independent verdicts on all 26; parity re-run on any correction |
| 2 | R-ORACLE provisional oracle inherited by both prototypes | Mitigate in M1 | M1 | Independent property tests, adversarial cases and cross-implementation checks beyond parity |
| 3 | R-G2 288 V1 rules uncurated | Accept for M1; no unreviewed V1 rule imported as enforced policy | M6/M7/M12 | INV-20 negative control; per-rule fixtures before import |
| 4 | R-G3 real host behavior unobserved (SR-06) | Accept for M1; SR-06 completed before any host-enforcement claim | M5 | 69 priority experiments incl. NC-1, then full SR-06 acceptance |
| 5 | R-SB sandbox on shared rootful Docker | Accept non-adversarial preparation only; adversarial and fault-injection trials on a VM or separate machine | M5 | Trials only under stronger isolation |
| 6 | R-V1PKG undeclared V1 dependencies | Mitigate before migration tooling release | M12 | Install from a verified lock |
| 7 | R-V1TEL V1 daemon telemetry egress | Investigated; prevent undeclared telemetry in V2 (V2-TEL) | M7 | Allowlist-proxy egress test in CI |
| 8 | In-process ingress and permit keys (SR-01, SR-07) | Mitigate | M3/M5 | Forged seal and permit refused after key rotation |
| 9 | SignoffGrant provenance caller-asserted (SR-12) | Mitigate | M4 | Forged in-scope signoff refused without an approval-service record |
| 10 | Unsigned policy bundles | Mitigate | M6 | Damaged signature and downgraded bundle refused (INV-05, INV-12) |
| 11 | Composite multi-effect requests (RM-01) | Mitigate | M1/M3 | Swapped sub-effect target under one approval refused |
| 12 | SQLite ledger durability limits (E-008) | Mitigate | M3/M9 | Crash during commit; network filesystem excluded or tested |
| 13 | Use-time TOCTOU, no OS effect mediation | Mitigate | M3 | Symlink and path substitution between decision and use refused (INV-14) |
| 14 | No trusted clocks | Mitigate | M3 | Skewed or rolled-back clock cannot extend a grant or deadline |
| 15 | Claude Code hooks advisory, crash behavior unobserved | Mitigate with R-G3 | M5 | As row 4 |
| 16 | Golden case 24 id misnamed (cosmetic) | Accept; rename after review links are reconciled | M0 follow-up | n/a |

## 3. Decision and attestation procedure (no self-reference)

1. **Decision commit.** Fill the Decision table with the text in section 1; set D-014 to ACCEPTED *citing the owner attestation*; update TASKS (M0-05c, M0-06), WORK-STATE and SESSION-HANDOFF. This commit cites the evidence baseline `6007d09` and run 42308217716 only. It contains no SHA or CI URL of itself. Push normally.
2. **CI on the decision commit** must pass. M1 may not start before it does.
3. **Owner attestation, performed by the owner personally** through a channel this Claude Code session cannot use on the owner's behalf:
   - preferred: a comment on the decision commit or a review on a pull request, posted by the owner in the GitHub web UI; or
   - a tag signed with a key whose use requires the owner (passphrase or hardware key) and that Claude does not operate.
   The attestation text (template below) names the decision commit SHA and its CI run URL; those exist by then, so nothing is circular.
4. **Optional follow-up evidence record.** If a repository record of the attestation is wanted, one documentation commit may add the attestation link to TEST-EVIDENCE. Its own CI is not recorded anywhere; no further commits chase CI URLs.

Why not a plain commit: on this workstation, commits made by Claude carry the owner's git identity (`Lucio`), commit and tag signing are not configured, and the `gh` CLI acts as the owner's GitHub account. A commit or `gh` action from the session therefore proves nothing about who decided.

### Attestation template (owner posts it; Claude does not)

> **Writ V2 M0 development GO, owner attestation.** I, *(name)*, on *(date)*, made the decision recorded in commit *(decision commit SHA)* on `v2/architecture-rebuild`, whose CI run *(URL)* passed. Evidence baseline: `6007d09`, run 42308217716. Scope: M1 development only; golden outcomes provisional; the 26 independent human golden reviews, the 288-rule review and SR-06 remain incomplete; AI-assisted technical assessments 1 to 4 are not independent human certification; release, production deployment, privileged production workloads and unverified security claims remain prohibited; the 16 risks in M0-GO-SIGNOFF-PACKAGE section 2 are accepted as open; the release standard will be revisited by explicit decision at the release milestone. I performed this attestation myself, not through an AI session.

## 4. Files changed at signing (decision commit)

| File | Change |
|---|---|
| `docs/implementation/M0-GO-NO-GO.md` | Decision table filled from section 1; "owner signature pending" in all 16 risk rows replaced by the owner's name and date |
| `docs/implementation/DECISION-LOG.md` | D-014 status PROPOSED to ACCEPTED, citing the decision date and "owner attestation linked from TEST-EVIDENCE" (no SHA of itself) |
| `docs/implementation/TASKS.md` | M0-05c DONE with the decision date; M0-06 DONE (decision recorded, attestation pending until posted); REL-01, M5-SR06, M6-RULES, V2-TEL become active with their milestones |
| `docs/implementation/WORK-STATE.md` | Milestone: M0 closed for development under accepted risks, release NO-GO; next action: await CI and attestation, then start M1 |
| `docs/implementation/SESSION-HANDOFF.md` | Same as WORK-STATE; do-not-reverse adds D-014 and the release-gate revisit |

Unchanged at signing: golden fixture and its 26 `PENDING INDEPENDENT REVIEW` statuses, the 288 rule-review rows, SR-06 status (NOT VERIFIED), V1, the M0 harness.
