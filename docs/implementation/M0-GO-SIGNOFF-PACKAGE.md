# M0 development-GO signoff package

**Status (2026-11-07): SIGNED.** Decision commit `38e2d1f` (CI 42319056546); owner attestation by Lucio Saldivar ([comment](https://github.com/infinri/Writ/commit/38e2d1f31fb11a145cb7a109934e85b018dd1d60#commitcomment-208565071)), verified read-only; acceptance commit records D-014 ACCEPTED. Prepared by Claude on 2026-11-06; Claude did not sign or post the attestation.

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

Revised 2026-11-06 per owner instruction: the decision commit **records** the decision as pending; acceptance is recorded only after the attestation is verified.

1. **Decision commit (Claude prepares, owner authorizes the push).** Fills the Decision table with the section 1 text marked RECORDED, PENDING OWNER ATTESTATION; adopts the 16 dispositions; keeps D-014 PROPOSED; sets M0-06 IN REVIEW. Cites the evidence baseline `6007d09` and run 42308217716 only, never its own SHA or CI. Pushed **alone** (no later commit in the same push) so that CI runs on exactly this commit.
2. **CI on the decision commit** must pass (both jobs of `v2-docs.yml`).
3. **Owner attestation, performed by the owner personally** through a channel this Claude Code session cannot use on the owner's behalf:
   - preferred: a comment on the decision commit posted by the owner in the GitHub web UI (`https://github.com/infinri/Writ/commit/<decision SHA>`, comment box at the bottom of the page); or
   - a tag signed with a key whose use requires the owner (passphrase or hardware key) and that Claude does not operate.
   The attestation text (template below) names the decision commit SHA and its CI run URL; those exist by then, so nothing is circular.
4. **Verification by the session (read-only).** Claude reads the comment through the GitHub API and checks: author login `infinri`; created in the web UI after the decision commit's CI completed; body names the decision SHA and a successful CI run of it; scope text matches section 1. Claude reports what it read; it does not post, edit or react to the comment.
5. **Acceptance commit.** Only after step 4 passes: D-014 ACCEPTED citing the attestation URL; M0-05c and M0-06 DONE; M0 closed **for development only**; release and production stay NO-GO; M1 tasks and deferred risk-mitigation tasks active; TEST-EVIDENCE records the decision commit, its CI run and the attestation link. This commit's own CI is checked before M1 code lands but is not recorded in any further commit.

Residual limitation: a `gh api` call from this session could post a comment as `infinri`; step 4 cannot cryptographically distinguish that from a web UI comment. The safeguard is procedural: this session never posts comments or reactions, and the owner should confirm in the session that the comment was posted personally. A signed tag (second channel) removes that limitation.

Why not a plain commit: on this workstation, commits made by Claude carry the owner's git identity (`Lucio`), commit and tag signing are not configured, and the `gh` CLI acts as the owner's GitHub account. A commit or `gh` action from the session therefore proves nothing about who decided.

### Attestation template (owner posts it; Claude does not)

> **Writ V2 M0 development GO, owner attestation.** I, *(name)*, on *(date)*, made the decision recorded in commit *(decision commit SHA)* on `v2/architecture-rebuild`, whose CI run *(URL)* passed. Evidence baseline: `6007d09`, run 42308217716. Scope: M1 development only; golden outcomes provisional; the 26 independent human golden reviews, the 288-rule review and SR-06 remain incomplete; AI-assisted technical assessments 1 to 4 are not independent human certification; release, production deployment, privileged production workloads and unverified security claims remain prohibited; the 16 risks in M0-GO-SIGNOFF-PACKAGE section 2 are accepted as open; the release standard will be revisited by explicit decision at the release milestone. I performed this attestation myself, not through an AI session.

## 4. Files changed

| File | Decision commit (step 1) | Acceptance commit (step 5) |
|---|---|---|
| `docs/implementation/M0-GO-NO-GO.md` | Decision table filled, RECORDED, PENDING OWNER ATTESTATION; 16 risk rows "adopted by owner instruction; effective only on owner attestation" | Effective status ACCEPTED with attestation link and date |
| `docs/implementation/M0-GO-SIGNOFF-PACKAGE.md` | This two-step procedure | Status line |
| `docs/implementation/DECISION-LOG.md` | D-014 stays PROPOSED, status note | D-014 ACCEPTED citing the attestation |
| `docs/implementation/TASKS.md` | M0-06 IN REVIEW | M0-05c, M0-06 DONE; REL-01, M5-SR06, M6-RULES, V2-TEL active; M1 tasks active |
| `docs/implementation/WORK-STATE.md`, `SESSION-HANDOFF.md` | Next action: push, CI, owner attestation | M0 closed for development only, release NO-GO; next: M1 |
| `docs/implementation/TEST-EVIDENCE.md` | unchanged | Decision commit CI run and attestation link |

Unchanged in both: golden fixture and its 26 `PENDING INDEPENDENT REVIEW` statuses, the 288 rule-review rows, SR-06 status (NOT VERIFIED), V1, the M0 harness.
