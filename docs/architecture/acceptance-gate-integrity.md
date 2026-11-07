# Acceptance Gate Integrity

**Status:** requirement ACCEPTED as a core architectural requirement (owner direction 2026-11-06, [D-016](../implementation/DECISION-LOG.md)); design **APPROVED IN PRINCIPLE** by the owner 2026-11-07, not ACCEPTED until sections 6a and 12a are reviewed ([ADR-0008](../decisions/ADR-0008-acceptance-gate-integrity.md)). Documentation only. Section 12 separates what the M0 reference harness does from what is not implemented.

**Objective.** Writ acts as an independent authority for governed AI execution and verified workflow acceptance. The AI proposes actions and produces work; it cannot authorize its own execution, invent independent verification, or promote incomplete work across protected boundaries.

## 1. Normative requirement

Acceptance Gate Integrity (AGI) is a security and correctness requirement of the core, not a Capability Pack feature. In **Governed Execution** mode:

- **AGI-1 Action Authorization Gate.** No governed privileged effect executes without trusted ingress, a kernel decision on the exact operation under current authorized policy, and broker revalidation at use time with a valid single-use permit (existing INV-01..INV-09, D-008..D-012).
- **AGI-2 Acceptance and Promotion Gate.** No governed workflow enters a protected state (accepted, completed, approved, published, deployable, or any state an acceptance policy marks protected) unless (a) a current AcceptanceRecord issued by the acceptance service shows every mandatory criterion satisfied by an appropriately trusted evaluator for the exact artifact digest, and (b) the kernel permits the transition under current policy, and (c) the transition service commits it atomically against the expected state revision.
- **AGI-3 No self-certification.** No agent, model, pack or untrusted validator can produce a passing mandatory criterion, a human signoff, a policy change or an acceptance record.

A valid AcceptanceRecord never authorizes execution by itself; it is a precondition checked together with a kernel decision, exactly as an ApprovalGrant is.

## 2. Three distinct concepts, three owners

| Concept | Question | Owner | Output | Not allowed to |
|---|---|---|---|---|
| Action authorization | May this principal perform this exact operation now? | Decision kernel (pure), enforced by the broker | Decision, permit, receipt | Evaluate artifact quality |
| Artifact evaluation | Does this exact artifact satisfy these versioned criteria, on what evidence? | Registered evaluators, aggregated by the acceptance service | EvaluationRecords, one AcceptanceRecord | Decide permissions or change workflow state |
| Workflow promotion | May the workflow move from state X to protected state Y now? | Kernel decides (`workflow.transition` action); transition service (a broker operation) enforces | Transition event, receipt | Skip the kernel or the acceptance check |

There is **no second authorization authority**. A promotion is an ordinary ActionRequest of kind `workflow.transition` on resource `workflow:<tenant>/<project>/<workflow_id>`, with attributes `from_state`, `to_state`, `artifact_digest` and `acceptance_record_id`. Policy decides whether the principal may request it (deny > require_approval > permit > default deny). The acceptance check is a use-time precondition inside the broker, like grant consumption.

## 3. Operating modes

| Mode | What Writ controls | What it must say |
|---|---|---|
| **Advisory Integration** | Host hooks supply knowledge, observe activity and deny supported host actions within verified coverage | Operations outside verified coverage are UNMEDIATED. Transitions recorded in this mode are labelled `advisory`; they are never shown as governed or verified. A hook deny is not containment |
| **Governed Execution** | Privileged effects and protected transitions run only through controlled services (broker, effect adapters, mechanical runner, transition service) with filesystem, process, network and credential restrictions appropriate to each capability | Fail closed on unavailable authorization or acceptance services, invalid identity, missing or unverifiable policy, stale evidence or missing mandatory acceptance requirements |

Governed Execution requires that the agent **does not hold** the credentials or write access needed to perform the effect or the state change itself: the workflow state store, publish targets, deployment credentials and release channels are writable only by the transition service and effect adapters. Where the agent retains such access (for example a local shell with the same credentials), that path is UNMEDIATED and the product must say so.

Writ does not promise protection against a compromised host OS, kernel or hypervisor escape, or an agent process with independent privileged access outside Writ.

## 4. AcceptanceRecord contract (proposed for M2)

Issued only by the acceptance service; MAC or signature over the canonical digest (D-006) with the acceptance-service key, which no agent-realm component holds.

| Field | Meaning |
|---|---|
| `contract_version`, `acceptance_record_id`, `record_digest` | Identity and integrity |
| `tenant_id`, `project_id`, `task_id`, `workflow_id` | Scope; all must equal the transition request's |
| `transition` | `from_state`, `to_state` the record is valid for |
| `artifact_ref`, `artifact_digest` | Exact artifact content; recomputed from the artifact store at use time |
| `acceptance_policy_id`, `acceptance_policy_version`, `policy_bundle_sha` | Versioned criteria applied |
| `state_revision` | Workflow revision the evaluation observed |
| `criteria[]` | Per criterion: `criterion_id`, `mandatory`, `required_class` (mechanical, evidence, model_assisted, human), `status` (pass, fail, unknown, stale, waived), `evaluator_id`, `evaluator_version`, `evaluator_trust`, `evaluation_ref`, `evidence_refs[]` with digests, `attestation_ref` (runner attestation or signoff grant id), `observed_at`, `fresh_until` |
| `status` | `accepted` only if every mandatory criterion is `pass`; else `rejected`, `incomplete` or `needs_human_review` |
| `issued_at`, `expires_at`, `issuer` | Lifetime and issuing service identity |
| `mode` | `governed` or `advisory` |
| `supersedes` | Previous record for the same artifact lineage |

Relationship to existing contracts: EvaluationRecord stays the per-evaluator output. It currently lacks `tenant_id`, `project_id` and `artifact_digest` (M0 binds scope inside `evaluate()` instead, D-013); M2 must add them so each EvaluationRecord is independently bindable. SignoffGrant (D-013) becomes an approval-service artifact (M4) referenced by `attestation_ref`.

## 5. Trusted evaluators and evidence verification

**Registration.** An evaluator counts toward a mandatory criterion only when registered in the evaluator registry by an authorized human publisher (policy publish path, INV-12). An entry records `evaluator_id`, `version`, `class`, implementation digest, execution environment, attestation key id, the criteria it may satisfy, scope, owner and its test suite. Packs may **reference** registered evaluators and **propose** rubrics; they cannot register a trusted evaluator or raise an evaluator's trust (INV-23).

**Trust levels.**

| Level | Source | Can satisfy a mandatory criterion of class |
|---|---|---|
| U (untrusted) | Agent or model output, producer-claimed `verification_status`, unregistered tools, agent-run commands | none |
| MA (model-assisted) | Registered model-assisted evaluator | only `model_assisted` criteria; never mechanical, evidence or human |
| M (mechanical) | Registered check executed by the Writ-controlled runner | `mechanical` |
| E (evidence engine) | Provenance-verified source resolution (digest, excerpt, scope, freshness) | `evidence` |
| H (human) | Signoff created by the approval service from an authenticated human step-up, bound to tenant, project, task and artifact digest | `human` |

**Mechanical results.** A test or build result counts only if the mechanical runner (a broker-launched, sandboxed process with no agent-supplied credentials) executed the registered command on inputs identified by digest, and signed an attestation of: evaluator id and version, command digest, environment digest, input artifact digest, exit status, output digest, start and end time. "Tests passed" text from an agent, a CI log pasted by an agent, or a result file in an agent-writable workspace is untrusted. External CI may become a trusted source only through a registered adapter that verifies the CI provider's signed provenance for the same artifact digest.

**Source evidence.** As in M0 (D-007, D-012): sources resolve through a catalog scoped to tenant and project, content digest and verbatim excerpt must match, freshness rules apply, and producer status is ignored.

**Human signoff.** Only from the approval service (M4); the signoff names the artifact digest and acceptance policy version shown to the human.

## 6. Protected transition service

Promotion protocol (Governed Execution):

1. Agent or runner **proposes** `workflow.transition` through sealed ingress (D-010). Proposing is all it can do.
2. Kernel evaluates the request under the current bundle: unknown transition or missing policy is DENY or UNAVAILABLE; protected transitions may additionally REQUIRE_APPROVAL.
3. Broker transition operation, in **one** ledger transaction (`BEGIN IMMEDIATE` locally; serializable in enterprise):
   1. Verify the permit and request fingerprint (D-012) and the deadline.
   2. Compare-and-set: current workflow `state_revision` equals the request's expected revision, current state equals `from_state`.
   3. Load the AcceptanceRecord; verify issuer signature and `record_digest`.
   4. Recompute the artifact digest from the artifact store; check scope, transition, artifact digest, active acceptance-policy version and bundle sha, state revision and expiry against the record.
   5. Check every mandatory criterion is `pass` from an evaluator of sufficient trust, not stale (`fresh_until`), with evaluator version and key not revoked.
   6. Consume the record for this transition (single use) and any approval grant.
   7. Append the transition event and the receipt; increment the revision.
4. Any failed check refuses with a specific code and changes nothing.

The workflow state store accepts writes only from the transition service identity. There is no "set state" API for agents, packs or the management plane; the Control Center invokes the same transition operation as a human principal and is subject to the same checks.

Composite transitions (promote and deploy in one request) are prohibited until RM-01 is resolved; each effect is a separate governed request.

## 6a. Immutable artifact snapshots and use-time mediation

Recomputing a digest at promotion time is not enough if the artifact lives in a location the agent or another process can change between evaluation, promotion and the downstream effect (publish, deploy). Governed Execution therefore requires one of these, per artifact class, declared in the acceptance policy:

1. **Content-addressed snapshot (default).** At evaluation request time the artifact service copies the artifact into a content-addressed store keyed by its digest. The store is writable only by the artifact service and readable by evaluators, the mechanical runner, the transition service and effect adapters. Evaluators and the runner receive the snapshot (read-only mount or byte stream), never the agent's working copy. The transition and every downstream effect (publish, deploy, release) consume **the snapshot by digest**, not a path. Changing the working copy after evaluation cannot change what was evaluated or what is promoted; it only creates a new artifact needing its own record.
2. **Use-time mediation (when a snapshot is impractical, for example a large repository).** The effect adapter resolves the artifact at use time through a mediated, non-following path (no symlink traversal outside the canonical root, open-by-handle, then hash the opened content) and compares the digest under the same handle it then uses for the effect. Only digest-and-use under one handle counts; check-then-reopen does not.
3. **External systems** (registries, deployment targets) are trusted only if they accept an immutable digest reference (for example an image digest) and the adapter verifies the digest the target reports after the effect; otherwise the step is labelled UNVERIFIED.

Rules: the AcceptanceRecord binds the snapshot digest (`artifact_digest`) and snapshot id; a record bound to a mutable path is invalid in Governed Execution; snapshots are retained at least until every record and receipt referencing them expires. Tests: AG-NEG-11 working copy modified after evaluation, promoted bytes equal the evaluated snapshot; AG-ADV-17 symlink or path swap between check and use under use-time mediation is refused; AG-CON-04 concurrent writer during snapshot creation yields either the old or the new digest, never a mixed snapshot bound to either. Relates to the M3 TOCTOU risk (INV-14) and RM-01.

## 7. Invalidation and reevaluation

| Trigger | Effect |
|---|---|
| Artifact content changes (digest differs) | Record unusable; reevaluate |
| Evidence source digest changes or `fresh_until` passes | Affected criteria `stale`; record unusable if any mandatory criterion is stale |
| Acceptance policy version, rubric or bundle changes | Record unusable unless the new policy explicitly declares the old version equivalent (signed, human-published) |
| Evaluator version or attestation key revoked | Criteria from it become `unknown` |
| Workflow state revision changes | Record unusable for the old revision |
| Scope change (task moved, project archived) | Record unusable |
| Signoff grant revoked or expired | Human criterion `unknown` |

Invalidation is enforced **at use time** by recomputation in step 3, not by relying on delivery of invalidation events; background invalidation only updates displays and queues reevaluation.

## 8. Trusted versus untrusted evaluation sources

| Untrusted (never satisfies a mandatory non-model criterion) | Trusted when registered and attested |
|---|---|
| Model output, including a model reviewer's "verified" | Mechanical runner attestations |
| Agent-reported test results, logs, screenshots, files in agent-writable paths | Evidence engine with scoped, digest-checked sources |
| Producer `verification_status` fields | Approval-service signoffs |
| Retrieved documents, memories, graph relationships | External CI provenance through a registered verifying adapter |
| Unregistered or revoked evaluators; evaluator output that fails schema validation | Registered evaluator versions whose keys are current |

## 9. Failure behavior, audit and recovery

| Condition | Outcome (Governed) | Audit event |
|---|---|---|
| Evaluator unavailable or times out | Criterion `unknown`; transition refused `ACCEPTANCE_INCOMPLETE` | `evaluation.unavailable` |
| Evaluator output malformed or schema-invalid | Criterion `unknown`; output discarded | `evaluation.output_invalid` |
| Acceptance service unavailable | Transition `UNAVAILABLE`; nothing committed | `acceptance.unavailable` |
| Record signature or digest invalid | Refused `ACCEPTANCE_RECORD_INVALID` | `acceptance.record_rejected` |
| Binding mismatch (artifact, policy, scope, revision) | Refused `ACCEPTANCE_BINDING_MISMATCH` | `acceptance.binding_mismatch` |
| Stale criterion | Refused `ACCEPTANCE_STALE` | `acceptance.stale` |
| Concurrent transition lost the CAS | Refused `REVISION_CONFLICT` | `transition.conflict` |
| Record already consumed | Refused `ACCEPTANCE_REPLAYED` | `acceptance.replay_refused` |
| Audit store unavailable | Refused; nothing committed | none possible; health alarm |
| Success | Committed | `transition.committed` with record, permit and grant ids |

Recovery: reevaluate and request a new record; failed or stale criteria remain visible. **Waivers** exist only for criteria the acceptance policy declares waivable; a waiver is a human approval (M4) recorded as `waived` with the approver, never relabelled `pass`; criteria derived from security invariants are never waivable.

## 10. Adversarial resistance

Boundary column: **P** = designed to be prevented by Writ's trusted execution boundary in Governed Execution; **D** = detected or labelled, not prevented; **O** = outside Writ's control. Every row is at the **Designed** level (section 12a); none is a tested or verified claim yet.

| # | Threat | Control | Boundary | Tests |
|---|---|---|---|---|
| 1 | Prompt injection overriding policy | Policy in signed bundles; retrieved text is data (INV-11) | P | AG-ADV-01 |
| 2 | Fabricated test results | Only runner attestations count (INV-35) | P | AG-NEG-03 |
| 3 | Forged human approval | Approval service plus step-up; transcript never a credential (INV-02) | P | AG-NEG-05 |
| 4 | Agent evidence marked verified | Producer status ignored; trust level from registry (INV-34) | P | AG-NEG-04 |
| 5 | Artifact replaced after validation | Immutable snapshot consumed by digest, or digest-and-use under one handle (section 6a); digest recomputed at use time (INV-33) | P (designed) | AG-NEG-01, AG-NEG-11, AG-ADV-17 |
| 6 | Approval or evaluation replay | Single-use consume of record and grant | P | AG-CON-02 |
| 7 | Cross-project or cross-tenant confusion | Scope bindings on record, permit and evidence | P | AG-NEG-02 |
| 8 | Unknown tools or alternate execution routes | Default deny for governed profile; unmediated routes labelled | P for broker-owned routes; D or O for routes the agent can reach without Writ | AG-ADV-08 |
| 9 | Agent edits its own acceptance requirements | Requirements are policy; publish path human-only (INV-37) | P | AG-ADV-09 |
| 10 | Policy downgrade or bundle substitution | Signed bundles (M6), version binding, monotonic version check | P after M6; dev bundle only before | AG-ADV-10 |
| 11 | Evaluator unavailable or malformed | Fail closed (INV-38) | P | AG-FAIL-01, 02 |
| 12 | Bypass of the transition endpoint | State store writable only by the transition service | P when the agent lacks store credentials; O if it holds them | AG-ADV-12 |
| 13 | Concurrent or stale transitions | CAS on state revision in one transaction | P | AG-CON-01 |
| 14 | Composite operation smuggling sub-effects | Composite requests prohibited until RM-01 | P | AG-NEG-08 |
| 15 | Memories or graph treated as authority | Retrieval has no authority path (INV-11) | P | AG-ADV-15 |
| 16 | Compromised or untrusted validator | Registry, key revocation, version pinning; quorum for high-stakes criteria | D (a registered evaluator whose key is compromised can lie until revoked) | AG-ADV-16 |

Outside Writ's control: host OS compromise; OS, kernel or container escape; agents with independent privileged credentials; a registered evaluator implementation that is wrong; humans who approve carelessly; effects on systems that never route through Writ.

## 11. Test plan (proposed)

| Group | Tests |
|---|---|
| Positive | AG-POS-01 all mandatory criteria pass from trusted evaluators, kernel allows, transition commits once; AG-POS-02 waivable criterion waived by authorized human is recorded `waived` |
| Negative | AG-NEG-11 working copy changed after evaluation, promoted bytes equal the snapshot; AG-NEG-01 artifact digest changed after evaluation; AG-NEG-02 tenant, project or task mismatch on record, evidence or signoff; AG-NEG-03 agent-supplied test result file; AG-NEG-04 producer `verified` evidence; AG-NEG-05 transcript "approved"; AG-NEG-06 model-assisted pass on a mechanical criterion; AG-NEG-07 expired or stale criterion; AG-NEG-08 composite transition plus effect; AG-NEG-09 acceptance policy version changed; AG-NEG-10 record without valid issuer signature |
| Differential | AG-DIF-01 Rust and Go binding checks agree on every fixture and on generated single-field mutations; AG-DIF-02 identical decisions for `workflow.transition` across hosts and packs |
| Adversarial | AG-ADV-01 injected policy text; AG-ADV-08 alternate route to state store; AG-ADV-09 pack or agent edits acceptance policy; AG-ADV-10 downgraded bundle; AG-ADV-12 direct state-store write with agent credentials; AG-ADV-15 memory claiming authority; AG-ADV-16 revoked evaluator key; AG-ADV-17 path or symlink swap between check and use |
| Failure | AG-FAIL-01 evaluator timeout; AG-FAIL-02 malformed output; AG-FAIL-03 acceptance service down; AG-FAIL-04 audit store down; AG-FAIL-05 crash between CAS and receipt |
| Concurrency | AG-CON-01 16 concurrent promotions on one revision, exactly one commits; AG-CON-02 replay of a consumed record; AG-CON-03 promotion racing an artifact update; AG-CON-04 concurrent writer during snapshot creation |
| Property | AG-PROP-01 any single-field mutation of a bound field refuses; AG-PROP-02 adding untrusted criteria never changes `rejected` or `incomplete` to `accepted` |

Adversarial tests run only in disposable environments (threat model rule).

## 12. Implementation status

| Element | State |
|---|---|
| Kernel decisions, sealed ingress, single-use grants and permits, request fingerprints, deadline checks | **Reference harness** (`m0/writ_m0/`, Python, non-production; SR-01..SR-12 except SR-06) |
| `evaluate()`: artifact and evidence scope, digest and excerpt checks, producer status ignored, model-assisted cannot pass, signoff bound to artifact digest | **Reference harness** (D-007, D-012, D-013); SignoffGrant provenance is caller-asserted |
| AcceptanceRecord, acceptance service, evaluator registry, mechanical runner attestations, transition service, invalidation, waivers | **Not implemented** |
| Governed Execution restrictions (sandboxed effect adapters, credential isolation) | **Not implemented** |
| Host enforcement of any kind | **Not verified** (SR-06) |

## 12a. Guarantee levels and claim discipline

Every defense in this document is tracked at one of four levels. Documentation, the Control Center and release notes must state the level, and may claim only what that level supports.

| Level | Meaning | Allowed claim |
|---|---|---|
| **Designed** | Specified here; no running code | "Designed to prevent X" |
| **Tested (unit or reference)** | Passing tests in the reference harness or a prototype, in-process, simulated adversary | "Tested in the reference harness or prototype against X" |
| **Tested (integration and threat)** | Passing integration and adversarial tests against real services and storage in a disposable environment | "Tested against X in integration" with the environment named |
| **Verified (real host)** | Integration and threat tests pass against the real host integration (for example SR-06 class trials for Claude Code) at a named host version, with recorded evidence | "Verified on host H version V for X" |

Current level of every Acceptance Gate defense: **Designed**, except the evaluation behaviors listed in section 12 at **Tested (unit or reference)** in the Python harness. **No adversarial bypass resistance is claimed** for any component until the relevant integration and threat tests (section 11, AG-ADV and AG-CON groups, and SR-06 for hosts) pass at the integration or verified level, and the claim names the level, environment and version.

## 13. Milestone alignment and sequencing changes

| Milestone | AGI scope |
|---|---|
| M1 | Kernel semantics for `workflow.transition`; pure acceptance-binding check in both prototypes; CAS and single-use semantics in the broker scenario; differential and property tests ([M1 parity suite](../implementation/M1-PARITY-SUITE.md)). Not the evaluation platform |
| M2 | AcceptanceRecord, EvaluationRecord scope fields, evaluator registry entry, runner attestation contracts; workflow state machine with protected states; INV-32..INV-40 as contract tests |
| M3 | Transition service with CAS, single-use records, use-time recomputation; mechanical runner as a sandboxed broker effect; audit events |
| M4 | Authenticated signoff provenance; waivers as approvals |
| M5 | Host adapters; proof of which host operations are mediated; advisory labelling of host-side transitions |
| M6 | Signed acceptance policies, evaluator registry publication, pack-referenced rubrics bound to registered evaluators |
| M7, M8 | Evidence provenance catalog, freshness; retrieval and memory never authoritative |
| M9 | Control Center traceability of decisions and acceptance |
| M12 | Real adversarial, integration and failure testing before any production enforcement claim |

Sequencing changes needed to avoid insecure dependencies:

1. **Contracts before mechanics (M2).** AcceptanceRecord and evaluator trust classes must exist before any service emits acceptance results, otherwise early services would invent ad hoc formats.
2. **Mechanical runner sandbox moves into M3** (with the broker), not M5: trusting mechanical results requires that the runner is Writ-controlled. Until then, mechanical criteria can only be `unknown`.
3. **Human criteria stay `unknown` until M4.** M3 may ship the transition service only with human-class criteria failing closed; no interim signoff shortcut.
4. **Until signed bundles (M6), acceptance policies come only from the development bundle** and are labelled UNVERIFIED; no production promotion claim.
5. **RM-01 resolved by "prohibit composite requests" from M1 through M3** unless an ADR binds each sub-effect earlier.
6. **No host adapter (M5) may offer a promotion shortcut** that writes workflow state; host-side transitions are advisory.

## 14. Control Center integration

Requirements CC-A08, CC-A09, CC-R07 and CC-R08 in the [Control Center specification](../control-center/README.md) cover inspection of attempts, authorization, applicable acceptance requirements, criterion results, evidence actually verified (with trust level), approval state, the reason a workflow was blocked or promoted, and unmediated paths; and the authoring, simulation and guarded publication of acceptance requirements.
