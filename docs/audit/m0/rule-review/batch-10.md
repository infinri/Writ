# Rule review batch-10: process, research (23 rules)

Source: `writ-corpus.cypher` at `ebdafe5450b201339f81e717ed104e6bcfd3fe3e`. Proposed dispositions are the automated first pass (D-004), not verdicts. Record decisions in [batch-10.csv](batch-10.csv).

## ENF-PROC-BRAIN-001

- **Domain / severity / mandatory:** process / critical / true
- **Statement:** In Work mode, no code-producing action is permitted before a design artifact exists and has been approved by the user. Applies to every project regardless of perceived simplicity.
- **Trigger:** In Work mode: when the agent attempts any code-producing tool call (Write, Edit, Bash with destructive verbs) before a design has been approved for the current task.
- **V1 enforcement text:** .claude/hooks/validate-exit-plan.sh + writ-session.py phase state machine check session.mode == 'work' AND session.design_approved == true before any code-producing tool call.
- **V1 mechanical path:** .claude/hooks/validate-exit-plan.sh + writ-session.py phase state machine (resolves at baseline: no)
- **Rule id referenced in V1 code:** 1 file(s): `writ/export.py`
- **Rule id referenced in V1 tests:** 6 file(s)
- **Related rules (conflict candidates):** PROC-PLAN-001
- **Proposed disposition:** obsolete (names retired V1 mechanism 'Work mode'; intent needs manual review); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PROC-DEBUG-001

- **Domain / severity / mandatory:** process / high / false
- **Statement:** Advisory rule: observe runtime data first (logs, traces, live queries), read code only after it narrows the target, and cite the root cause in the same response as the fix. Applies in work mode, not only debug.
- **Trigger:** When the agent is diagnosing a failure in any mode, before it reads code and before it proposes a fix.
- **V1 enforcement text:** Advisory only, surfaced as part of the always-on bundle in work and debug (the process-domain strip removes it elsewhere). No deny condition on this rule. Its mechanical counterpart is the runtime-lens read gate, _can_read_code_check in writ/session/gates.py, which denies Read and Grep of source files while debug.md lacks Evidence and Narrowing. That gate stays scoped to the declared runtime lens and is deliberately NOT widened to work mode, where there is no debug.md and most work is not a runtime investigation; the advisory widens, the gate does not. Friction-logged when the agent claims success on a fix without evidence.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 3 file(s): `writ/export.py`, `writ/server/routes/query.py`, `writ/session/gates.py`
- **Rule id referenced in V1 tests:** 8 file(s)
- **Related rules (conflict candidates):** PROC-INCIDENT-001, TEST-REGRESSION-001
- **Proposed disposition:** obsolete (names retired V1 mechanism 'work mode'; intent needs manual review); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PROC-FIXLOOP-001

- **Domain / severity / mandatory:** process / high / false
- **Statement:** A fix loop is bounded: five rounds per task, one fix dispatch plus one scoped re-review each. Rounds 1-3 resume the same implementer with the findings verbatim; rounds 4-5 dispatch a FRESH implementer on the same model carrying the full evidence (brief or plan path, failing test output, every open finding, prior fix reports), with Fable only when the user explicitly asks. At the cap the controller adjudicates every open finding into one of three recorded dispositions and never re-dispatches again.
- **Trigger:** When a dispatched review returns findings and the controller is about to send them back to an implementer, or when a fix loop has already run rounds without a clean review.
- **V1 enforcement text:** Controller discipline in PBK-PROC-SDD-001. No mechanical gate: the round counter lives in the controller's own ledger, and no hook can see it.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 2 file(s): `writ/export.py`, `writ/graph/methodology_ingest.py`
- **Rule id referenced in V1 tests:** 5 file(s)
- **Related rules (conflict candidates):** ENF-PROC-SDD-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PROC-PLAN-001

- **Domain / severity / mandatory:** process / high / true
- **Statement:** plan.md artifacts must contain no placeholder content, exact file paths, and complete code blocks. Gate 5 Tier 1 (structural) denies writes with placeholder text in any plan section.
- **Trigger:** When a plan.md artifact is written and contains placeholder content (TBD, TODO, 'similar to N') or fails structural quality gate.
- **V1 enforcement text:** Gate 5 Tier 1 via hooks/scripts/validate-exit-plan.sh: lexical match against the placeholder blocklist (TBD, TODO, fill in, appropriate, similar to, as needed, placeholder), denied at PreToolUse. Gate 5 Tier 2 is SELF-scored, not independent: hooks/scripts/writ-quality-judge.sh (feature-flag gated) emits a self-review directive via PostToolUse additionalContext, the session that wrote the plan scores its own artifact against the rubric and POSTs the score to /session/{sid}/quality-judgment, and hooks/scripts/writ-verify-before-claim.sh enforces the recorded score when a completion is claimed. No external model reads the plan.
- **V1 mechanical path:** hooks/scripts/validate-exit-plan.sh (Tier 1, denies the write) + hooks/scripts/writ-quality-judge.sh (Tier 2, self-scored directive) (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 1 file(s): `writ/export.py`
- **Rule id referenced in V1 tests:** 5 file(s)
- **Related rules (conflict candidates):** PROC-PLAN-001
- **Proposed disposition:** obsolete (names retired V1 mechanism 'plan.md'; intent needs manual review); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PROC-PRIORITY-001

- **Domain / severity / mandatory:** process / high / false
- **Statement:** Advisory rule: instruction priority is user/project instructions (CLAUDE.md, direct requests) > Writ methodology nodes > default model behavior. When a surfaced rule conflicts with an explicit user instruction, the user wins; follow the instruction and note the override. Methodology overrides only the default behavior, never an explicit human instruction.
- **Trigger:** When a surfaced Writ methodology rule appears to conflict with an explicit user or project instruction (CLAUDE.md, AGENTS.md, or a direct request), and you must decide which to follow.
- **V1 enforcement text:** Advisory only -- surfaced when a conflict is detected in context. No deny condition; no reliable lexical detector for 'is this a conflict?'. Friction-logged when an explicit user instruction is overridden by methodology.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/export.py`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** none
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PROC-SDD-001

- **Domain / severity / mandatory:** process / high / false
- **Statement:** Within a single review, the spec-compliance pass must complete (and pass) before the code-quality pass. Spec-first catches wrong-thing-built before effort is spent polishing it.
- **Trigger:** When the reviewer evaluates a diff and would judge code-quality before confirming spec-compliance.
- **V1 enforcement text:** Agent-prompt discipline in writ-reviewer (two ordered passes). No mechanical gate.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/export.py`
- **Rule id referenced in V1 tests:** 3 file(s)
- **Related rules (conflict candidates):** ENF-PROC-FIXLOOP-001, PROC-REVIEW-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PROC-TDD-001

- **Domain / severity / mandatory:** process / critical / true
- **Statement:** Production code requires a failing test before implementation. Gate denies Write/Edit to src/** paths without corresponding tests/** file containing lexical test markers.
- **Trigger:** In Work mode: when a Write or Edit to a production source file is attempted without a corresponding test file containing assertions.
- **V1 enforcement text:** validate-test-file.sh: on PreToolUse Write matching src/**/*.{py,js,ts,php}, find corresponding test file, check for lexical assertion markers (assert|expect|should|test_). Deny if missing.
- **V1 mechanical path:** .claude/hooks/validate-test-file.sh (resolves at baseline: no)
- **Rule id referenced in V1 code:** 4 file(s): `bin/lib/manual_test_grant.py`, `hooks/scripts/validate-test-file.sh`, `hooks/scripts/writ-manual-test-grant.sh`, `writ/export.py`
- **Rule id referenced in V1 tests:** 19 file(s)
- **Related rules (conflict candidates):** ENF-GATE-007, PROC-TEST-001, TEST-REGRESSION-001
- **Proposed disposition:** unverified (V1 binding path absent at baseline: .claude/hooks/validate-test-file.sh); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PROC-VERIFY-001

- **Domain / severity / mandatory:** process / critical / true
- **Statement:** Completion claims require fresh verification evidence in the same message. TodoWrite completion denied without verification_evidence set in session state.
- **Trigger:** When the agent attempts to mark a TodoWrite item complete, or when the session's Stop hook fires and completion claims exist without verification evidence.
- **V1 enforcement text:** writ-verify-before-claim.sh on Stop (work mode): surfaces (stderr, exit 1; loop-safe, never additionalContext on Stop) any artifact whose Gate 5 quality self-review scored < 3 and was not overridden. The PreToolUse TodoWrite gate was removed (#1) -- TodoWrite does not exist in CC 2.1.183 and TaskUpdate fires no hook. The 'tests must pass' half of verification is enforced by writ-run-pending-tests (ENF-TEST-001); unverified-claim phrasing by FRB-COMMS-002. NOTE: statement/violation/pass_example still reference TodoWrite -- flagged for human re-authoring.
- **V1 mechanical path:** hooks/scripts/writ-verify-before-claim.sh (resolves at baseline: yes)
- **Rule id referenced in V1 code:** 3 file(s): `hooks/scripts/writ-memory-policy-guard.sh`, `hooks/scripts/writ-verify-before-claim.sh`, `writ/export.py`
- **Rule id referenced in V1 tests:** 7 file(s)
- **Related rules (conflict candidates):** ENF-POST-006, ENF-TEST-001
- **Proposed disposition:** obsolete (names retired V1 mechanism 'TodoWrite'; intent needs manual review); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## ENF-PROC-WORKTREE-001

- **Domain / severity / mandatory:** process / high / true
- **Statement:** Project-local worktree directories must be gitignored. Bash gate denies 'git worktree add' commands targeting non-ignored repo-local paths.
- **Trigger:** When the agent runs git worktree add <path> where <path> is inside the repo tree and is not listed in .gitignore.
- **V1 enforcement text:** writ-worktree-safety.sh on PreToolUse Bash matching 'git worktree add': parse target path, check .gitignore, deny if project-local and not ignored.
- **V1 mechanical path:** .claude/hooks/writ-worktree-safety.sh (resolves at baseline: no)
- **Rule id referenced in V1 code:** 2 file(s): `hooks/scripts/writ-worktree-safety.sh`, `writ/export.py`
- **Rule id referenced in V1 tests:** 8 file(s)
- **Related rules (conflict candidates):** none
- **Proposed disposition:** unverified (V1 binding path absent at baseline: .claude/hooks/writ-worktree-safety.sh); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## PROC-BRANCH-001

- **Domain / severity / mandatory:** process / low / false
- **Statement:** Feature branches are named with a ticket/issue reference: `bug/ORD-1234-negative-quantity`, `feat/ORD-1500-tenant-scoping`. Ad-hoc branch names without a tracking reference are violations.
- **Trigger:** When creating a feature branch.
- **V1 enforcement text:** Repository branch-naming convention. PR template checks. Related: PROC-INCIDENT-001.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PROC-INCIDENT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## PROC-CHANGELOG-001

- **Domain / severity / mandatory:** process / medium / false
- **Statement:** User-facing changes are documented in a changelog or release-notes file. The audience is end users / API consumers / customers, not engineers. Behavior changes, new features, breaking changes, deprecations are all logged.
- **Trigger:** When releasing user-facing changes.
- **V1 enforcement text:** Release-process tooling (changesets, conventional-changelog, knope) generates from commits.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** API-BREAKING-001, PROC-COMMIT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## PROC-COMMIT-001

- **Domain / severity / mandatory:** process / medium / false
- **Statement:** Commit messages follow a conventional format: a short subject line (`type: subject` or just a clear summary), and a body for non-trivial changes describing the why. Single-line drive-by messages on substantive changes are violations.
- **Trigger:** When writing commit messages.
- **V1 enforcement text:** Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** PROC-CHANGELOG-001, PROC-REVIEW-001, SEC-VAL-RANGE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## PROC-DEPLOY-001

- **Domain / severity / mandatory:** process / high / false
- **Statement:** Production deploys go through a CI/CD pipeline. Manual deploys (scp, ssh, kubectl apply) are forbidden for production. The pipeline runs tests, builds artifacts, applies migrations, and rolls out via the deployment strategy.
- **Trigger:** When deploying code to production.
- **V1 enforcement text:** CI/CD platform (GitHub Actions, GitLab CI, ArgoCD, Spinnaker).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ARCH-MIGRATION-001, PROC-ROLLBACK-001, SCALE-MIGRATE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## PROC-ENV-001

- **Domain / severity / mandatory:** process / medium / false
- **Statement:** Production credentials are never shared in code, chat (Slack, Teams, email), tickets, or other text artifacts. They live exclusively in the secret management service. Sharing a credential is a security incident; the credential is rotated.
- **Trigger:** When handling production credentials.
- **V1 enforcement text:** Slack DLP scanning. Security training. Incident response on credential exposure.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** SEC-CRYPTO-KEY-001, SEC-CRYPTO-KEY-002
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## PROC-INCIDENT-001

- **Domain / severity / mandatory:** process / medium / false
- **Statement:** Post-incident review produces action items with owners and deadlines. Action items are tracked to completion; the incident is not closed until they are. Blameless: the review focuses on systemic causes, not individual error.
- **Trigger:** When an incident occurs in production.
- **V1 enforcement text:** Postmortem template + tracking. Incident-management platform (PagerDuty, Incident.io, FireHydrant). Related: ENF-PROC-DEBUG-001.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-PROC-DEBUG-001, PROC-BRANCH-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## PROC-PLAN-001

- **Domain / severity / mandatory:** process / high / false
- **Statement:** Work mode requires a written plan before any production code is written. The plan covers files to be touched, an analysis of constraints, the rules that apply, and the capabilities to be tested. ENF-PROC-PLAN-001 and ENF-PROC-BRAIN-001 mechanically enforce this for the Writ workflow; the policy itself is universal.
- **Trigger:** When starting any task that involves modifying code.
- **V1 enforcement text:** ENF-PROC-PLAN-001 (format check) + ENF-PROC-BRAIN-001 (design before code) gates. Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/graph/integrity/_common.py`
- **Rule id referenced in V1 tests:** 12 file(s)
- **Related rules (conflict candidates):** ARCH-LAYER-001, ENF-PROC-BRAIN-001, ENF-PROC-PLAN-001, PROC-REVIEW-001, SEC-AUTHZ-TENANT-001
- **Proposed disposition:** obsolete (names retired V1 mechanism 'Work mode'; intent needs manual review); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## PROC-REVIEW-001

- **Domain / severity / mandatory:** process / medium / false
- **Statement:** Code is reviewed by at least one other person or a reviewer agent before merge. Auto-merge on green CI is permitted only for trivial mechanical changes (dependency updates, formatter pushes) and is configured per-repo.
- **Trigger:** When merging a change to a shared branch.
- **V1 enforcement text:** Repository branch protection rule.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 0 file(s)
- **Related rules (conflict candidates):** ENF-PROC-SDD-001, PROC-COMMIT-001, PROC-PLAN-001, TEST-SNAPSHOT-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## PROC-ROLLBACK-001

- **Domain / severity / mandatory:** process / high / false
- **Statement:** Deployment strategy supports rollback within minutes: blue-green, canary, feature flag, or `helm rollback` / `kubectl rollout undo`. A deploy that cannot be reverted produces extended outages on every bad change.
- **Trigger:** When designing the deployment system.
- **V1 enforcement text:** Deployment-platform config review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ARCH-MIGRATION-002, PROC-DEPLOY-001, SCALE-MIGRATE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## PROC-TEST-001

- **Domain / severity / mandatory:** process / high / false
- **Statement:** Test skeletons are written and approved before the implementation. Tests carry the contract; the implementation conforms to them. ENF-PROC-TDD-001 mechanically enforces this in the Writ workflow.
- **Trigger:** When implementing production code that is not a one-shot script.
- **V1 enforcement text:** ENF-PROC-TDD-001 (failing test before src/ write) gate. Code review.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** ENF-PROC-TDD-001, TEST-EXIST-001
- **Proposed disposition:** obsolete (names retired V1 mechanism 'Test skeletons'; intent needs manual review); pack affinity: workflow-core
- **Reviewer verdict:** _not reviewed_

## RESEARCH-CITE-001

- **Domain / severity / mandatory:** research / high / false
- **Statement:** Every external claim carries a citation: a `ref` recorded in the session citation ledger (the INV-2 `citation_log`), with the supporting excerpt. A claim with no citation you can point to is dropped, not softened. No claim from memory.
- **Trigger:** When stating an external fact in an investigation finding or synthesis.
- **V1 enforcement text:** The citation ledger is presence-checked: a finding's `ref` must resolve to a captured citation. Presence proves capture, never truth (the honest ceiling).
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** ENF-OPS-001, RESEARCH-CORROBORATE-001, RESEARCH-SOURCE-001
- **Proposed disposition:** obsolete (names retired V1 mechanism 'INV-2'; intent needs manual review); pack affinity: research-investigation
- **Reviewer verdict:** _not reviewed_

## RESEARCH-CORROBORATE-001

- **Domain / severity / mandatory:** research / high / false
- **Statement:** A decision-driving factual claim is corroborated by two or more INDEPENDENT sources. Sources that derive from a common origin (articles quoting one press release) count as one. A single source is a lead, not a fact.
- **Trigger:** When a factual claim gathered during investigation will drive a decision or an assertion in the synthesis.
- **V1 enforcement text:** Enforced (INV-7a) by the `triangulation-gate` command: it counts INDEPENDENT source domains among the citation-ledger url rows and returns `blocked` until >=2 corroborate (fail-closed when zero sources are captured). The verdict is a hard stop, not a nudge.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 1 file(s): `writ/session/investigations.py`
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** RESEARCH-CITE-001, RESEARCH-SOURCE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: research-investigation
- **Reviewer verdict:** _not reviewed_

## RESEARCH-SOURCE-001

- **Domain / severity / mandatory:** research / high / false
- **Statement:** A claim rests on the most authoritative source reachable, not the first hit. Primary sources (the spec, the code, the dataset, the original announcement) outrank secondary descriptions, which outrank tertiary aggregations. If a primary source is one click away, the secondary one is a lead, not a citation.
- **Trigger:** When citing a source to support a claim during investigation (research, audit, or exploration).
- **V1 enforcement text:** Review. The citation ledger records each source's `ref`; a reviewer checks that decision-driving claims cite primary/authoritative sources, not aggregators.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 1 file(s)
- **Related rules (conflict candidates):** RESEARCH-CITE-001, RESEARCH-CORROBORATE-001, RESEARCH-STALENESS-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: research-investigation
- **Reviewer verdict:** _not reviewed_

## RESEARCH-STALENESS-001

- **Domain / severity / mandatory:** research / medium / false
- **Statement:** Time-sensitive facts record the retrieval date or the version they describe. Fast-moving domains (prices, APIs, releases, security advisories) are stale by default; re-verify before relying on a previously captured source.
- **Trigger:** When citing a source for a time-sensitive or version-dependent claim.
- **V1 enforcement text:** Enforced (INV-7a) by the `staleness-check` command: each url citation carries an `excerpt_hash`, and a source re-captured with a changed hash is flagged `drifted` (its content moved since the earlier fetch). A reviewer re-verifies a drifted source before relying on it.
- **V1 mechanical path:** none
- **Rule id referenced in V1 code:** 0 file(s)
- **Rule id referenced in V1 tests:** 2 file(s)
- **Related rules (conflict candidates):** PERF-CACHE-002, RESEARCH-SOURCE-001
- **Proposed disposition:** advisory (guidance only; no enforcement claim); pack affinity: research-investigation
- **Reviewer verdict:** _not reviewed_
