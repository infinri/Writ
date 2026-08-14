# ADR: Session and project isolation, and a tripwire for the external cause

Status: accepted in part (session and project isolation cycle, 2026-08-11). Parts 1
through 4b are landed; Part 5 (Memory audit) and Part 6 (automatic mode routing) are
PENDING and are named as such at the end of this record.

## Context
Two symptoms opened this cycle. First, one Claude Code session read another's state:
the user's magento harness project reported this repo's `work` mode, and a later test
run left the machine-wide session pointer naming a session that never existed. Second,
text the user typed in one place appeared inside a sub-agent's turn somewhere else,
which was written up at the time as a cross-project CONTENT leak in Writ.

The first symptom was Writ's and had two mechanisms, both of which answered "which
session am I" with a guess. The second symptom was NOT Writ's, and establishing that
reshaped the plan more than any fix in it did (Decision 4).

The landed work is these commits, oldest first:

- `dbd800a` Make an approval mint a token bound to the gate and plan it authorized
- `19d2e6d` Refuse an unbound token on promote-candidate too, not just on advance
- `34f975f` Resolve a session from its payload or refuse, never from a machine-wide guess
- `ce64155` Scope a gate approval to the session that earned it
- `e246f67` Scope retrieval by node type, so records stay private and doctrine stays universal
- `3ef514a` Scope the enrichment neighbours too, and stop the registry cache stampeding
- `90645f4` Close the ranking-path scope gap, prove the symlink refusal, make three docs true

`f8f4eb4` (Act on the mode hint mid-session instead of computing it and discarding it)
predates the cycle and is discussed only because it was twice accused of causing a
symptom it did not cause (Decision 9). The Part 4 tripwire and the Part 4b telemetry
attribution fix land on the same branch after the commits above; no hash is claimed for
them here.

## Decision

### 1. Session identity comes from the hook payload, or the caller is refused
`resolve_current_session_id()` had four tiers and its own docstring called tier 3
"shared-global" and tier 4 "racy". Tier 3 read `/tmp/writ-current-session`, ONE file per
machine that every session's UserPromptSubmit hook overwrites. Tier 4 took the newest
`writ-session-*.json` by mtime out of a cache directory that every project shares. Both
tiers are deleted. What remains is the id the payload carries, by way of
`CLAUDE_SESSION_ID` or the `CLAUDE_JOB_DIR` basename, and an explicit `--session`
argument on the CLI paths. With neither available the answer is None and the caller
refuses with exit 2 and a message that names the explicit form.

The deciding property is the DIRECTION of failure. Both deleted tiers returned a
confident wrong answer, indistinguishable at the call site from a right one, and both
did so in production. A refusal is loud, cheap to act on, and cannot approve a stranger's
gate. `writ doctor` re-implemented tier 4 locally with its own
`max(glob(...), key=os.path.getmtime)`, so it was fixed in the same pass; deleting the
tier from the resolver alone would have left `writ doctor` reporting a guess.

The accepted cost is stated plainly because it is user-facing: an approval no longer
survives a session end. The user chose that trade in their own words, preferring to
re-approve than to have something interfere.

The pointer FILE and both of its writers (`writ-rag-inject.sh` and `auto-approve-gate.sh`;
the first plan knew of only one) stay, because two readers hold no Claude Code payload to
read an id from: `hooks/git/post-commit`, which git invokes with no envelope ever, and
`session-start-bootstrap.sh`, which needs the PRE-rotation id that by definition is not in
the current payload. Deleting the write would have pushed both onto the mtime glob, which
is strictly racier than the pointer.

### 2. An approval is scoped to the session that earned it, and its token is bound
The artifact moves from `<project_root>/.claude/gates/<gate>.approved` to
`<project_root>/.claude/gates/<session_id>/<gate>.approved`, contents unchanged. Before
this, the session id was the file's CONTENTS and no reader checked it, so two sessions in
one repo shared one approval set: A's approval read as B's, and B running `mode set`
deleted A's files through `_clear_gate_artifacts`. That deletion is the reported
interference.

`<session_id>` is now a path component, so it is validated as `^[A-Za-z0-9._-]{1,128}$`,
which rejects separators and `..`. Writers refuse an invalid id loudly and write nothing;
readers treat it as "no approval", which fails CLOSED to gate-pending. The realpath
containment check already in `_clear_gate_artifacts` was extended over the new session
subdirectory, because a symlinked `<sid>` directory is the same escape the symlinked
`gates` directory was, and that one was live.

Separately, an approval now MINTS a token bound to both the gate it approved and a
fingerprint of the plan it authorized (`dbd800a`), and `promote-candidate` refuses an
unbound token exactly as `advance` already did (`19d2e6d`). Token PATHS are unchanged at
`/tmp/writ-gate-token-<session_id>`: they were already per session by name, which is why
the interference was scoped to the artifacts and to `_derive_phase`.

The path is deliberately built in two languages. Five readers are bash, and one of them,
`writ-rag-inject.sh`, sits on the per-prompt hot path where a python spawn costs about
19.5 ms, so the shape must be constructible in pure shell (a concatenation plus a `case`
glob for the charset check). That is a duplicated seam, and this repo has been bitten by
one before, so the mitigation is the one the precedent supplies: a parity test that runs
`writ_gate_dir` in bash and `gate_dir` in python over the same inputs, including a
rejected id, and compares bytes.

### 3. Retrieval is scoped by node TYPE, not by project tag
This is the counterintuitive decision in the cycle and it needs its full reasoning.
`pipeline.py` computed `allowed_projects = {project, "_shared"} if project is not None
else None`, and `project=None` disabled the confidentiality filter entirely. The obvious
repair is to make the project tag mandatory and filter on it. That repair would have been
catastrophic and would have tested GREEN: all 287 Rule nodes and every methodology node
in the graph carry `project: "writ"`, so tag-scoping doctrine would have delivered ZERO
rules to every project except this one, while an isolation test asserting "project A does
not see project B's nodes" passed happily.

So the axis is the node's TYPE, which is intrinsic, rather than any node's data.
`writ/retrieval/node_scope.py` holds `DOCTRINE_NODE_TYPES = {"Rule", "Skill", "Playbook",
"Technique", "AntiPattern", "ForbiddenResponse"}` and `is_visible(node_type, node_project,
caller_project)`. Doctrine is universal by design and passes regardless of its project
tag, which makes the catastrophic outcome structurally impossible. A record (Memory,
Decision, FileChange, Commit) passes only when `node_project in {caller_project,
"_shared"}`. An unknown type is treated as a record.

The allowlist is WRITTEN OUT rather than derived, and that is the deciding argument. The
set is explicit and small, so an unanticipated node type falls to the record side and
fails closed: it surfaces as a retrieval gap a person notices. Every alternative shape
fails OPEN for the new type and surfaces as nothing at all.

With no caller project the result is the whole doctrine corpus and zero records of any
project, which is a completeness degradation rather than a leak. That is why the two
remaining unscoped `pipeline.query` callers (`authoring.py` dedup suggestions and
`writ query` in `cli.py`) were left alone: both want doctrine and neither should ever see
records. When the project cannot be resolved the answer is likewise doctrine-only, not a
hard failure, because the retrieval path is fail-open by construction end to end and a
raised error would block a turn for a condition that is NORMAL on any unregistered
project. The resolution failure itself must not be quiet, so the unresolved root rides
the existing `retrieval_result` telemetry row.

`test_no_project_is_search_all_backward_compatible` inverted under this change. That is a
deliberate contract change and not drift: its own file header calls search-all "a no-op at
single-project", there are now ten projects in the graph, and the condition the clause was
written for stopped holding, which turned the clause itself into the defect.

### 4. The cross-project CONTENT leak was not Writ's, and no capability claims it is closed
Recorded prominently, because it reframed the plan. The leaked text entered at line 139 of
a sub-agent transcript as a bare `text` element sitting as a sibling of an unrelated Bash
`tool_result` inside a `role:"user"` message. Every genuine Writ injection in the same file
arrives as an attachment element carrying `hook_additional_context` or `hook_success`, a
structurally different shape, and Writ never writes into transcript files at all. The
distinctive typo in the leaked phrase also appears in the PARENT session's transcript. So
the user typed that message into the parent session and Claude Code delivered it into the
sub-agent's pending turn. This is the harness's queued-input mechanic: not retrieval, not
`recall.py`, not `list_memories`, and not `resolve_project_for_cwd`, whose own default bug
points the other way (it would label another project's query as this one rather than pull
another project's content in).

Three consequences. Every deliverable premised on Writ leaking records across projects was
DROPPED. No capability asserts that leak is closed, because a test asserting isolation we
never broke would make the real, external cause invisible, which is worse than no test.
And the retrieval fail-open of Decision 3 is re-described honestly: it leaked DOCTRINE,
meaning Rule and methodology nodes, across projects. That is lower severity than first
stated, and it is still a real fail-open on a filter whose own comment called it "the
anti-leak guarantee".

### 5. The tripwire is wired, not CLI-only, because the measurement said rare and not benign
Writ cannot prevent a Claude Code delivery bug, but it can refuse to let a recurrence be
invisible. The predicate is structural: a `role:"user"` message whose content array holds a
bare text element alongside a tool_result element. In a well-formed dispatch, a user
message carrying tool results carries only tool results.

Wiring was made CONDITIONAL on a measurement, because a tripwire whose false-positive rate
nobody has measured is an alarm that gets ignored. The measurement ran on 2026-08-11 over
130 local transcript files and about 10,446 `role:"user"` messages carrying list content.
The predicate matched TWO, roughly 0.02 percent, and NEITHER was a benign harness sentinel.
Rare and not benign, therefore wired into `hooks/scripts/writ-subagent-stop.sh`.

`KNOWN_BENIGN_SENTINELS` therefore ships EMPTY. The constant exists so that a future
measured sentinel has one obvious home; inventing entries the corpus never produced would
have been a claim the measurement does not support, so the test monkeypatches the allowlist
rather than asserting its contents.

One of the two matches is the real occurrence from Decision 4: line 139 of a sub-agent
transcript in this project dated 2026-08-10, digest
`307cf9a4f87abed1ea67d5e50828d7e5e73d08f1`, length 132, one text element and one
tool_result element. It was re-found here by structural search rather than from memory, and
the corpus scan and the shipped CLI produced the same digest through different code paths,
so the agreement is independent rather than circular. The second match is the same shape in
a PARENT transcript dated 2026-08-07. It is related and out of scope: the hook reads only
sub-agent files, and a hook that also read parent transcripts would inspect every
legitimate user turn in the session.

Two constraints on the hook, both load-bearing. It must NEVER emit `additionalContext`,
because Claude Code treats a Stop-family hook's `additionalContext` as a turn BLOCK, which
is a known live incident in this repo, and a tripwire that blocks turns on a heuristic is
worse than the thing it detects. And the finding carries no foreign text: file, line,
timestamp, element counts, a sha1 digest and a length, with the text shown only when an
operator runs the CLI with `--show-text` against a transcript already on their own disk.
Copying the foreign content into a project-local log would relocate the leak rather than
report it. Findings are filed against the PARENT session, the same choice
`writ-subagent-stop.sh` already makes for a reviewer's verdict, so a sub-agent is never the
courier for a finding about itself, and they reach the critical stream an operator reads.
Output is capped at 5 findings while `findings_total` preserves the true count, so a
pathological transcript cannot turn one completion into thousands of rows, and the cap is
not silent. Cost measured at 0.04 to 0.05 s on a 725 KB, 164-line transcript and on the
largest sub-agent transcript on this host (2.1 MB).

### 6. Three design corrections the same measurement forced
The plan intended to DERIVE the sub-agent transcript path as
`<dirname(transcript_path)>/<parent_session_id>/subagents/agent-<agent_id>.jsonl`. The
captured payloads corrected that three times over.

First, the payload already carries `agent_transcript_path`, the sub-agent's own file as
resolved by Claude Code, in 42 of 42 captured payloads. Writ reads that key rather than
computing what the harness already told it. Second, the derived formula is WRONG for a real
24 percent of dispatches: workflow fan-outs nest one level deeper, at
`<parent>/subagents/workflows/wf_<id>/agent-<id>.jsonl`, which is 10 of those 42 payloads.
A resolver that knew only the flat shape would have missed a quarter of this repo's own
sub-agents. The derivation survives as a documented FALLBACK behind the payload key, with a
glob for the nested shape behind that. The resolver also refuses to scan when the candidate
it picked is the same FILE as `transcript_path`, because on a build where those keys collapse,
scanning the PARENT transcript would flag every legitimate user turn (this project's parent
transcript genuinely holds one such match, so the guard protects against a real false alarm,
not a hypothetical one). That comparison is on RESOLVED paths, not on the two strings: review
proved the string form missed a collapse expressed through a symlink alias, and let the hook
scan the parent transcript. The guard lives in the resolver rather than in the hook's shell so
it applies to every arm and is unit-testable; an earlier shell-side version also had the side
effect of never invoking the resolver at all when the payload key was absent, which made the
documented fallback unreachable. Third,
sub-agent transcripts are NOT durable: Claude Code removes them after a session ends, so
SubagentStop is the only reliable read window, and a retroactive CLI audit of an old session
that finds nothing is behaving correctly rather than failing.

One correction to the plan's own arithmetic: the capture file holds 42 payload rows, not the
50 an earlier paragraph of the plan claimed.

### 7. The environment variable that looks like the fix, and is not
This answers an operational item, probed on Claude Code 2.1.227. Neither
`CLAUDE_SESSION_ID` nor `CLAUDE_JOB_DIR` is ever exported into the Bash tool environment, so
BOTH tiers the resolver still reads are unreachable from a Bash tool call. That inverts the
assumption the refusal message was drafted against.

`CLAUDE_CODE_SESSION_ID` IS exported, and in a main session it equals the id hooks write
state under, which makes it look exactly like the missing fix. It is deliberately not read.
Probed from inside a real sub-agent it still holds the PARENT's id, while Writ keys a
sub-agent by its `agent_id`, so reading it would let a sub-agent resolve to its parent and
approve or clear the parent's gates: precisely the class `34f975f` closed.
`CLAUDE_CODE_CHILD_SESSION=1` is a flag rather than an id, and it is set in the main session
too, so it cannot separate parent from child either.

Conclusion: the refusal stands, the remedy is the explicit `--session` argument (which the
message now leads with), and `cache.py` carries a comment recording why the
available-looking variable is not read, so the next reader does not "fix" it.

### 8. Part 4b: telemetry attribution, an incomplete sweep rather than a new class
While verifying that a tripwire finding could be attributed to the parent session, the
surrounding telemetry turned out not to be attributable at all. Buffered rows key on
`${SESSION_ID:-${HOOK_SESSION_ID:-}}`, and a hook that sets neither files its rows under the
literal session id `unknown`. `var/session/writ-events-unknown.buf` holds 445 such rows: 372
from `writ-comms-output-gate` (197 `hook_execution` plus 175 `gate_decision`), 56 from
`writ-debug-code-gate`, and 17 from `session-start-bootstrap`. The comms gate is the bulk of
it because it fires on every assistant response.

This is an INCOMPLETE SWEEP of a defect already fixed and documented in a sibling hook on
2026-08-08, not a new class, and that is the argument for fixing it inside this cycle rather
than filing it. `writ-debug-code-gate.sh` already carries the fix plus a comment describing
this exact bug, and its own rows corroborate that the fix works: they STOP on 2026-08-08
while the comms gate's rows kept arriving. The pattern, the rationale and the evidence all
existed; two callers were simply missed.

The fix copies the worked example and its constraint. Identity comes from the payload, with
`agent_id` first so a sub-agent's rows are never filed under its parent, and an id the
payload did not carry stays EMPTY rather than being synthesized. An unattributed row is a
visible gap; an invented id is a silently wrong record, which is the thesis of this whole
cycle. The forward guard is a TEST, not a comment, because the comment already existed and
did not stop the same bug living on in a sibling hook.

### 9. `f8f4eb4` did NOT cause the other project's mode-setting symptom
The user reported, of another project, that "mode setting is now done manually as opposed to
automatically". An earlier diagnosis blamed `f8f4eb4` for it, twice, and was wrong both
times; this ADR records the correction rather than only the conclusion.

The diff is the evidence. `f8f4eb4` touches `hooks/scripts/writ-rag-inject.sh` with 61
insertions and 0 deletions, and 37 files with 1160 insertions against 17 deletions overall.
It removed no routing path. It CREATED mid-session routing: before it, the mode hint was
computed on every prompt and thrown away once any mode existed, which is why five weeks of
logs held zero `change_type=switch` rows. A commit that added the only mid-session routing
that has ever existed cannot be the reason routing stopped happening.

What actually causes the reported symptom is still open, and it is Part 6's job. The
strongest candidate on the evidence so far is eligibility: `mode init` no-ops whenever a mode
is already set, and the mid-session `case` fires only between `work` and `investigate`, so a
session sitting in conversation, debug or review is never routed again for the rest of its
life. That hypothesis is not yet discriminated from five others, so no fix is claimed here.

### 10. Part 5: the audit's headline check could not fail, so a second check was added
`writ memory audit` ships read-only, and the reason it needed rescoping is worth recording,
because the first design would have shipped a green light that proved nothing.

The motivation is real and confirmed in code. `create_memory` does
`MERGE (m:Memory {name, project}) SET m += $props` with NO `ON CREATE` / `ON MATCH` split, so
a second call matching an existing node overwrites `body`, `description` and `path` outright.
Last writer wins, no versioning, no conflict detection. A Memory filed under the wrong project
is therefore a DATA-LOSS risk, not merely a mis-scoped record.

The check this part was scoped around cannot detect that. It compared a node's stored `project`
against the project derived from its stored `path`, but both values are written in the same
call from the same derivation, and the two derivation parsers
(`decision_memory.py::_project_from_memory_path` and
`memory_capture.py::derive_project_from_memory_path`) turn out to be ALGORITHMICALLY IDENTICAL,
differing only in returning `""` versus `None` for an unresolvable path. So the premise that
those two might disagree was wrong. Measured over the live graph: 202 Memory nodes, 0 empty
projects, 0 empty paths, 0 mismatches. The check ships anyway as an invariant guard, because
`create_memory` takes `project` and `path` as independent keyword arguments and enforces no
relationship between them, so a future caller can still break the pair. Its test therefore
CONSTRUCTS an inconsistent node rather than scanning production, which would have passed
against an empty implementation.

One check was added, and it is the one that fired: the audit also compares the graph against
the DISK. For each node it reports whether the stored `path` still exists, and where it does,
whether the project derived from that LIVE path still equals the stored `project`. A moved or
renamed project directory is how a node ends up filed under a project that no longer describes
it, and that is invisible to a comparison of two properties written at the same instant. On the
first live run this reported one node: `audit_probe_test`, filed under
`project='writ-audit-fake-project'`, whose recorded path points into a scratchpad directory that
no longer exists.

HONESTY ABOUT THAT EVIDENCE, corrected after review. That node is OUR OWN TEST DEBRIS, not an
organically occurring orphan. Its `updated_at` reads 2026-08-08 while its path contains THIS
cycle's scratchpad session id, and `writ-audit-fake-project` is already named as observed in this
cycle's archived plan, so it was written by an earlier probe of this very cycle and left live in
production. It therefore proves the DISK DRIFT bucket FUNCTIONS end to end against real graph
data, and it does NOT establish that this class of drift occurs in normal use. Citing it as an
organic discovery would be circular. The honest state of the question is that the bucket is the
only one capable of firing, it has been exercised against production, and the wild rate is
unmeasured because the corpus is clean apart from our own litter.

THE OPERATIONAL ANSWER, recorded as this cycle required: run against the live graph,
`examined=202 mismatch=0 empty=0 disk_drift=1 collision=0`. Two corrections come with it. The
plan said 191 Memory nodes and the working notes said 199; the real count is 202, while the
Rule count of 287 was accurate. And NO repair was performed by the audit: at that moment the
orphan above was still in the graph, because repair is a write and does not belong to a
read-only command.

UPDATE, after that run: the audit still performs no repair, but the one node it reported was
afterwards cleared as a separate, deliberate operator action, on the reasoning that a new
detector whose only standing finding is known test litter is one people learn to ignore. It was
scoped by both key properties and proven surgical: Memory total 202 to 201, Rule count unchanged
at 287, and a sha256 over the 201 survivors identical to the pre-clearing fingerprint of the
non-target nodes, `209db28ef96d8e11`. The audit now reports
`examined=201 mismatch=0 empty=0 disk_drift=0 collision=0`, collision scan complete, so a future
finding is news rather than noise. The cost: the demonstration that this bucket fires against
real data now survives only in this record and in the tests.

Read-only is proven two ways rather than asserted. Structurally, the test stubs the database
accessor with an object exposing only `list_all_memories`, so any attempted write raises rather
than being caught by an assertion someone could later weaken. Empirically, a sha256 over every
Memory node's `(name, project, body, path, updated_at)` was byte-identical across a real run,
`4c8e85dd19440bd4` before and after. A node COUNT was deliberately rejected as sufficient
proof, since an in-place property overwrite leaves the count unchanged, and overwriting a body
is precisely the failure this part exists to detect.

The slug collision is real but has not happened. Claude Code encodes a working directory by
replacing `/` with `-` and does not escape hyphens already present, so `/home/u/foo/bar` and a
real directory named `/home/u/foo-bar` both encode to `-home-u-foo-bar`, which would make every
memory from either collide on the MERGE key. No pair collides on this machine today; the
detector exists so the condition is visible before it costs a body.

## Alternatives considered
- Keep the pointer tier behind a staleness check, or keep the mtime scan as a last resort.
  Rejected: a staleness check narrows the window in which a confident wrong answer is
  returned without changing that the answer is a guess, and the two observed failures (a
  foreign project reading this session's mode, a pointer naming a nonexistent session) both
  fit inside a narrowed window.
- Delete the pointer file and its two writers outright. Rejected: `hooks/git/post-commit`
  and `session-start-bootstrap.sh` have no payload to read, so deletion would move them to
  the racier mtime glob rather than removing the risk.
- Read `CLAUDE_CODE_SESSION_ID`, which is genuinely exported. Rejected in Decision 7: inside
  a sub-agent it names the PARENT, which would let a sub-agent approve or clear its parent's
  gates.
- A filename prefix, `<sid>-<gate>.approved`, instead of a session subdirectory. Rejected
  for one reason that outweighs the extra mkdir: under a prefix scheme a correct clear must
  filter a `*.approved` glob by session, so any later edit that widens that glob silently
  deletes other sessions' approvals. Under a subdirectory the clear cannot reach another
  session even if written carelessly.
- Migrate existing flat approval artifacts into the new shape. Rejected: approvals no longer
  survive a session end by decision, so there is nothing whose survival is worth migrating.
  They are ABANDONED, and `_clear_gate_artifacts` sweeps `<gate_dir>/*.approved` at the top
  level under the same containment rules at every re-arm, which is idempotent and needs no
  migration step. The direction of failure decides it: a swept file makes a reader this plan
  missed report "gate pending", which over-blocks, whereas a surviving flat file would report
  "approved" for every session in the repo, which is the bug.
- Scope retrieval by the project TAG, mandatory everywhere. Rejected in Decision 3: every
  Rule and methodology node is tagged `project: "writ"`, so this delivers zero doctrine to
  every other project while isolation tests pass.
- Re-tag all 287 Rule nodes to `_shared`. Rejected: `bible/` is the source of truth and
  ingest is upsert-only with no prune, so the next import re-writes `project: "writ"` unless
  the authoring side changes in lockstep. That is two coordinated changes, one of which is a
  partially-appliable data migration.
- Add the corpus project to every caller's allowed set. Rejected outright: it hardcodes this
  project's name into a confidentiality filter and grants every project read access to
  everything tagged `writ`, records included.
- Fail loud when the caller's project cannot be resolved. Rejected: unresolvable is NORMAL
  for any project not yet registered, and the retrieval path is fail-open by construction, so
  a hard error would block turns. Doctrine-only plus the unresolved root on the
  `retrieval_result` row keeps the degradation visible without blocking.
- Put the tripwire in `writ/session/harvester.py`, which already globs sub-agent transcripts.
  Rejected: the rglob that reaches `subagents/` runs only during `writ harvest`, a manual
  backfill, while the post-commit path imports a collector whose glob is top-level and never
  sees a sub-agent file. A tripwire there would fire rarely and never at the moment the shape
  appears.
- Ship the tripwire as a CLI scan only. Rejected BY the measurement, which was the
  pre-committed decision rule: had the shape proven common and benign, the CLI plus this
  finding would have been the whole deliverable. Two matches in about 10,446 messages with
  zero benign sentinels selected wiring instead.
- Have the tripwire report through `additionalContext`. Rejected: Claude Code treats a
  Stop-family hook's `additionalContext` as a turn block.
- Store the foreign text in the finding so an operator can read it later. Rejected: that
  relocates the leak into a project-local log. Digest, length and structural counts only.
- Assert in a test that cross-project record isolation is now closed. Rejected in Decision 4:
  Writ never broke that isolation, and such a test would hide the external cause.
- Fix the `unknown` telemetry attribution with another comment, or synthesize an id for
  payloads that carry none, or file it as separate work. All three rejected in Decision 8:
  the comment already existed and did not prevent the recurrence, a synthesized id is a
  silently wrong record, and filing it separately ignores that the fix, its rationale and its
  corroborating evidence were already in the tree.

## Consequences
- An approval does not survive a session end, and two sessions in one repo now hold
  independent approvals. The user accepted the first cost explicitly, preferring to re-approve
  over the risk of interference.
- Implementing the legacy sweep inside a live approved session drops that session's own flat
  artifact, so re-approving this repo's live cycle is carried as an operational step rather
  than discovered as a surprise.
- Diagnostic CLI paths (`writ-session.py mode set` with no sid, `bin/audit-region.sh`,
  `bin/check-gates.sh`) now exit 2 instead of guessing. The mitigation is that mode is
  normally set by the auto-router inside `writ-rag-inject.sh`, which holds the payload id, so
  the refusal is confined to paths a human or an operator drives.
- A retrieval caller that passes no project receives the full doctrine corpus and zero
  records. That is a deliberate completeness degradation and it is what makes the remaining
  unscoped callers safe to leave alone.
- Project resolution happens server-side in the daemon, where the `:Project` registry already
  lives, so it adds zero Neo4j queries per retrieval request (worst case one `MATCH
  (p:Project)` per daemon start or registry invalidation) and no python spawn per hook.
  Resolving client-side per hook would have cost a round trip plus a python start per prompt
  per channel.
- The gate-directory shape is now duplicated in bash and python by design, held together by a
  byte-parity test over the same inputs including a rejected id. This repo has been bitten by
  an unpinned duplicated seam before.
- `_clear_gate_artifacts`'s containment refusal was proven by MUTATION rather than by a green
  test: an earlier version of that test was deleted for passing before the fix existed,
  because the old cleanup had no session-subdirectory concept and never walked the path, so
  "the victim survived" proved nothing. The shipped pair fails under exactly the mutation each
  assertion claims to catch.
- The attachment arm of the tripwire predicate is DEFENSIVE and the module says so: real Writ
  injections arrive as TOP-LEVEL jsonl records, never as elements inside a user message's
  content list, so they cannot match today. The arm guards a nested shape Claude Code could
  adopt.
- Fixing the Claude Code queued-input misdelivery is out of scope and remains unfixed. This
  cycle detects and records it; it cannot repair it.
- The full suite shares one live graph with the daemon and has a documented `EntityNotFound`
  flake that appears only in full-suite runs. A failure of that shape in the retrieval tests
  is the known flake, not this cycle's regression, and it is not fixed here.

## Pending
ONE part of this cycle has NOT been implemented, and nothing above should be read as covering
it. (Part 5 is now implemented and its result is recorded in Decision 10; the repair of the one
orphan it found is deliberately NOT done and remains a separate data cycle.)

- Part 6, automatic mode routing, is not written. It will decide which of six discriminable
  mechanisms explains the other project's manual mode setting, and therefore whether the fix
  is a `mode_source` provenance field that makes an auto-set mode re-routable while leaving an
  explicit one alone, a reordering of the auto-route block above the skip early-exit, a change
  in `writ-prompt-parse.py`, or no defect at all because no prompt ever classified. Decision 9
  settles only what did NOT cause it.
