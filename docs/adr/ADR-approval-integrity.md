# ADR: approval integrity (an approval must have a referent, and an authority change must have an approval)

Status: accepted (approval-integrity cycle, 2026-08-28)

## Context

Two defects, one machinery.

**Defect 1.** `writ review <rule_id> --promote` elevated a rule's authority from
`ai-provisional` to `ai-promoted` behind an interactive `typer.confirm` that `echo y`
satisfies. Authority feeds retrieval ranking, so that is an authority change that could
happen with no human present, while its sibling operation (a graduation candidate to
canon, `/session/{id}/promote-candidate`) has required the agent-unforgeable gate token
since cycle 1.

**Defect 2.** The approval hook binds an approval tightly once minted (the gate on line 2,
the plan fingerprint on line 3, the single-use claim-by-rename) but constrained the MINT
not at all. Any turn whose whole prompt was `approved` minted a token and advanced
whatever gate was pending, including a turn where the user was answering a question the
agent had asked about something else. That is not hypothetical: a live session answered a
scope question with an approval word while the test-skeletons gate was pending and no
skeletons had been presented, and only a typo stopped the advance.

Writ already answered "does this approval still cover the plan it was given for", "which
gate does it authorize" and "can it be spent twice". The question nothing answered was
"was an approval REQUESTED at all".

## Decision 1: the evidence requirement sits at the MINT, and it is read from the transcript

Before an `exact` approval mints or advances, the hook requires that the immediately
preceding assistant turn asked for an approval. Adjacency is what supplies the referent:
an approval request two exchanges back is not what this turn is answering.

The evidence is read by a new stdlib-only `bin/lib/approval_evidence.py`, path-loaded by
the hook exactly as `approval_match.py` is, from the `transcript_path` field Claude Code
puts on every hook payload. A closed marker set ("say approved", "reply approved", "type
approved", "approved to proceed") is matched case-insensitively after normalization, so
"Say \`approved\` to proceed" and "Reply **approved** when ready" both count. Membership
in that set has one test: the phrase's only ordinary use is asking the user for the
approval word.

**Alternatives considered.**

- An agent-declared "I presented it" marker. Rejected: it fails in the honest case that
  matters, because an agent that BELIEVES it presented the artifact would set the marker.
  Infrastructure-side transcript evidence is better because it records what was actually
  put in front of the user rather than what the agent thinks it did.

  **It is NOT unforgeable, and the residual is disclosed here rather than left implied.**
  MEASURED by triggering the gate, not reasoned about: a Write to the live transcript at
  `~/.claude/projects/<slug>/<session>.jsonl` is ALLOWED. `writ-state-write-gate.sh` has no
  rule for that path and the work gate covers project-local paths, so an agent with ordinary
  Write or Bash access can append a forged assistant row carrying a marker phrase before the
  evidence reader looks. Against a deliberately dishonest agent, that is exactly as
  fabricable as a self-declared flag, so "strictly better" would be false.

  What survives is a real but narrower difference, and it is one of AUDITABILITY rather than
  unforgeability: forging requires a write to a path outside the project, which is an
  unusual, deliberate act that leaves a `write_attempt` row, whereas setting a self-declared
  marker would be an ordinary part of the agent's own operation and indistinguishable from
  correct behaviour. THE THREAT MODEL THIS CYCLE ADDRESSES IS MISCOMMUNICATION, not an
  adversarial agent, and it closes that case completely. Extending write protection to the
  transcript path is the follow-up if the threat model ever widens; it is deliberately not
  done here, because gating a file Claude Code itself writes needs its own measurement.
- Requiring the request to name the specific gate (plan.md for phase-a, "skeleton" for
  test-skeletons). Rejected: it would multiply false negatives across legitimate wording
  to defend a case with no observed instance, and the cross-gate direction is already the
  token's line 2. Stated as the residual, not as free.
- Widening or narrowing `is_approval`. Rejected as out of scope: the user is never asked
  to avoid a normal English word. `approved` keeps its exact meaning and its exact power;
  what changes is that an approval with no request in front of it produces one question
  instead of a silent advance.

**Accepted trade-offs.** The residual is that an agent which emits the ritual phrase
without presenting anything passes the check. That is the same residual plan.md already
carries (the agent chooses what it writes), it is out of reach of any prompt-side check,
and it is not the failure that happened. The fail direction is absolute: no
`transcript_path`, an unreadable or missing file, a transcript with no assistant row, and
a preceding turn with no marker all produce no mint, no advance, and the existing
embedded-tier treatment. Absence is never an advance.

That fail-closed direction has a failure mode of its own, and it is the reason for a
fourth tier. If `transcript_path` were ever absent for a whole session, every approval
would ask forever and no gate could ever advance: a refusal whose message names no way out
is a deadlock, not a control. So `approval_match.OVERRIDE_PHRASE = "approved anyway"` is
matched on whole-prompt equality exactly as `REPLAN_PHRASE` is, checked BEFORE the embedded
test (that ordering is load-bearing for the same reason it is for replan: the phrase
contains the word `approved` and would otherwise classify as embedded). The override tier
runs the exact arm with the evidence step waived and records `approval_evidence_override`
in the audit stream, so a systemic transcript failure that forces users onto the override
is measurable rather than invisible. An environment-variable escape was rejected in its
place: it would be invisible to the user in the moment they need it.

The `replan` tier keeps its existing, stricter treatment and gains nothing. It requires no
evidence, because a whole-prompt destructive phrase IS its own referent, and because
`cmd_reopen_planning` re-reads mode, phase and the pending gate authoritatively before it
resets anything.

## Decision 2: the token check for `--promote` lives IN THE CLI, not behind a daemon route

`typer.confirm` is DELETED from the promote path and replaced by the same gate token the
candidate route requires. The check is performed by the CLI command itself.

**Alternatives considered.**

- Route `--promote` through a new daemon route and check there. Rejected for three
  reasons. (a) The credential is a FILE, not a daemon object: `writ/session/gate_token.py`
  is pure filesystem and its own docstring already names a CLI call site
  (`cmd_advance_phase`) as one of the three legitimate consumers, so a CLI-side check is
  the established pattern rather than a new one. (b) `writ review` opens its own Neo4j
  connection and never speaks to the daemon, and the session cache and the token file are
  local, so surface, approve, promote stays authorizable end to end with the daemon
  stopped. Routing it through the daemon would make an authority change impossible in
  exactly the state where a maintainer is most likely to be repairing things, and would
  put a second Neo4j write path in the daemon for a command that already has one. (c) The
  route would gain nothing in security: the refusal decision reads a file and a cache,
  both equally readable from either process.
- Keep `typer.confirm` alongside the token. Rejected: it would require an interactive TTY
  on the very flow being built (the agent runs the command after the human types
  `approved`), so it would deadlock the fix, and a confirm that `echo y` satisfies adds no
  safety, which is measured rather than assumed.

**Accepted trade-offs.** A route-layer-only check would have left the local command as an
unguarded internal path to the same authority write (SEC-AUTHZ-RBAC-001), so putting the
check in the command is the stronger placement as well as the workable one. `--reject` and
`--downweight` keep their confirms and are deliberately out of scope: neither elevates
authority, and `--reject` deletes an `ai-provisional` proposal the agent itself authored,
which is an agent discarding its own unratified draft rather than writing to canon.

With the CLI gated, the cheapest remaining route to an unapproved authority change was for
the agent to mint its own token through the python API in one Bash call. The bash write
gate's state guard already refused commands naming `writ-gate-token`, `auto-approve-gate`
and `reopen-planning`, but not the underscored module symbol, so the guard was blind to
that path. The symbol joins the pattern list. The cost is the same cost every pattern there
carries: a Bash command that merely mentions it is refused unless it is plain read-only
inspection, so prose naming it goes through a file (`git commit -F <file>`).

## Decision 3: the rule binding is a FIFTH token line, not a reuse of line 4

`mint_gate_token` writes a fifth line: the rule a promotion approval authorizes. The
promote path compares it against the rule being promoted and refuses on a mismatch.

**Alternatives considered.**

- Reuse line 4 (the candidate binding) with a namespaced value such as `rule:ENF-X-001`.
  Rejected: conflating a graduation candidate id with a Rule id in one field would leave
  the next reader unable to tell WHICH object a token authorizes, and the whole point of
  the binding is that the answer is unambiguous.
- Bind nothing and rely on the cache's `pending_review_rule_id` alone. Rejected: the cache
  is agent-writable and the token is not, so the binding has to live on the credential.

**Accepted trade-offs.** `_line()` is already bounds-safe, so a four-line token reads line
5 as empty: a phase advance passes empty and still matches (every token already on disk
keeps advancing), and a rule promotion passes a real id against that empty value and is
refused, which is the correct direction for a credential minted before the binding it is
being asked to carry.

The new field forces one more decision. If the session cache could hold a pending
candidate AND a pending rule at once, a single `approved` would mint a token carrying two
promotion credentials. The claim would still spend it once, so "one approval, one action"
holds, but the human's approval could be spent on whichever of the two objects the agent
chose to act on first. So each surfacing clears the other: the CLI clears
`pending_candidate_id` when it records a rule, and `/session/{id}/promotion-review` clears
`pending_review_rule_id` when it records a candidate. One surfaced object at a time.

## Decision 4: one snapshot read of the token file per authorization

`writ review` (`_authorize_rule_action`) and `/session/{id}/promote-candidate` read the
token file once, through `read_gate_snapshot`, and take the secret, the binding, the rule
or candidate, and the approver identity from that one read before claiming. The public
`read_gate_*` readers remain, as projections of the same snapshot.

**Why.** Both paths used to read the file once per check. When two processes raced on one
token, the winner's claim could delete the file between two of the loser's reads, so the
loser was refused as `gate_token_unbound`, `gate_token_rule_mismatch` or
`gate_token_candidate_mismatch` instead of as the race it lost, and the identity recorded
could come from a different read than the bytes that were checked. With one read, a loser
is refused either at that read (`agent_self_approval_blocked`, the file was already gone)
or at the claim (`rule_promotion_claim_lost` on the CLI, "already spent" on the route).

**No extra check at the claim.** `claim_gate_token` already guarantees what a re-mint
between snapshot and claim needs. The rename lets exactly one process win, the winner
re-checks lines 2 to 5 from the claimed bytes, and the supplied secret must equal the
claimed line 1. A re-mint writes a fresh secret, so a claim made from the old snapshot
fails and nothing is written; and because the claimed secret must equal the snapshot's,
the snapshot's identity lines belong to the token that was actually spent.

**Alternatives considered.**

- Extend the change to the advance route and to `cmd_advance_phase` and the replan path,
  which have the same multi-read shape. Deferred to a separate change: their losers are
  not covered by the failure this fixes, and their tests synchronize on `read_gate_token`.

**Accepted trade-offs.** A claim that wins the rename but fails the secret comparison
still spends the re-minted file, so the user approves again; that property is unchanged
and stays out of scope. The unreadable-file instrumentation (`session.gate_token.read`)
moves from `read_gate_token` into the shared line reader, so it still fires once for a
present but unreadable file and never for an absent one.

## Decision 5: the advance and replan paths take one snapshot, and the claim checks the secret first

This closes the two items Decision 4 deferred.

- The advance route (`/session/{id}/advance-phase`), `cmd_advance_phase` and
  `cmd_reopen_planning` read the token file once, through `read_gate_snapshot`, and take
  the secret, the binding and the bound gate named in a refusal from that one read. The
  binding comparison is `snapshot_binding_refusal`, a pure function over the snapshot;
  `_binding_refusal` delegates to it, so there is still exactly one comparison. A race
  loser on these paths is now refused at its single read or at the claim ("already
  consumed" on the advance paths, `token_claimed` on the replan path), not as
  `gate_token_unbound` or `token_not_replan`. Messages, events, fields, `token_spent`
  values and consume behavior are unchanged.
- `claim_gate_token` checks the supplied secret, as well as the binding, against its
  pre-check read, and returns False without renaming when either fails. A caller holding a
  stale secret no longer spends a token re-minted after it read the file (for example the
  user approving again while a slow validator run was still in flight). The pre-check is
  still one read of the file, so a successful advance or re-open reads it twice before the
  rename, and the claimed bytes are still re-checked after it.

**Alternative considered.** Restoring the file after a claim that wins the rename but
fails the comparison. That would close the remaining window below, but it puts a write
back into the one step whose job is to make spending atomic. Not done.

**Accepted residuals.**

- The window between the claim's pre-check read and its rename is still open: a re-mint
  landing exactly there is spent by the stale caller. It is microseconds wide, against the
  seconds a validator run takes.
- Spend-on-rejection stays unconditional. The advance route consumes the token when the
  target gate's validator rejects the artifact, and the replan refusals that consume
  (`state_unknown`, `not_work_mode`, `not_implementation`, `gate_pending`) delete the file
  without comparing secrets, so a stale request rejected there can still remove a
  re-minted file.

## Consequences

- Six existing test modules that drove the hook with a bare `approved` now state their
  precondition with a transcript fixture. That is the honest cost of tightening a
  precondition; no global "evidence off" env var was introduced, because a suite-wide
  opt-out would mean nothing ever exercises the default.
- The interpreter budget does not move. The evidence read is one python start and it sits
  INSIDE the exact tier, after the tier gate; the `none` tier, which is every turn but
  one, starts exactly as many interpreters as it did before.
- Twelve events are registered in `STREAM_MAP` as `audit`, including the four
  pre-existing binding refusals that had been falling to the friction default. One family,
  one stream: leaving those four on friction while their new sibling is audited would mean
  a reader has to know which of the family is compliance-grade and which is signal.
- Two refusals deliberately write NO row. The unresolvable-session refusal would key a row
  to no session, which lands in the "unknown" bucket measured on 2026-08-11 as a defect
  worth 372 misfiled rows, and it is already visible on stderr with a non-zero exit. And
  the evidence-missing turn does not also write the existing `approval_pattern_match`
  friction row, because that row's `outcome` vocabulary describes what an advance attempt
  did and no advance was attempted.
- A Neo4j failure after the claim spends the approval and the user must approve again.
  That matches the advance route's stated contract ("the rejection spent the prior
  approval") and is preferred to holding a live canon-adjacent credential open across a
  failed write.
