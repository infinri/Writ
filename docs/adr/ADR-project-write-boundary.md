# ADR: an approved plan authorizes writes to ITS project, plus what its `## Files` declares

Status: accepted (cycle N, 2026-09-02)

## Context
The write gate was measured directly, one envelope at a time, before anything was designed.
Before approval every path denies with `[ENF-GATE-PLAN]`. After both work gates are approved
every path allows, including `/etc/passwd-probe.txt` and a source file in an unrelated
checkout. So the gate was path-blind in both directions, and the real gap is the
POST-approval one: an approved plan for this repo authorized a write to any absolute path on
the filesystem.

Three facts framed the design. First, the plan already declares its write surface in
`## Files` and the write gate never read it. Second, sub-agents skipped the gate entirely
through the `subagent_bypass` arm, and that arm justifies itself by the orchestrator having
already cleared a human approval gate; that approval was granted FOR A PLAN, IN A PROJECT,
so the bypass's own justification names a boundary it did not enforce. Third,
`docs/adr/ADR-role-write-scope.md` had already recorded why `writ-implementer` declares no
role scope: "its scope is the approved plan's `## Files` list, which is per-dispatch data
rather than a property of the role". The project boundary is the enforceable half of that
sentence.

The gap is an AUTHORITY gap, not a knowledge gap. The agent does not need to be told it is
writing outside the project; it needs to be unable to. A warning here would have left the
measured behaviour exactly as measured.

## Decision
One predicate, `writ/session/project_boundary.py`, wired into `writ/session/gates.py` at
three call sites. A write is in bounds when its resolved path is inside the resolved project
root, OR inside the OS scratch zone, OR equal to a path the approved plan's `## Files`
section declares as an absolute (or `~`-prefixed) path.

The predicate guards ONLY arms that were about to ALLOW: the exclusion allow and the
both-approved allow in `_check_work_gate`, and a new arm in `_check_exempt_write` between
the role-scope arm and the blanket sub-agent allow. It can convert an allow into a deny and
never the reverse, so no pre-existing refusal changes its tag and every pre-approval
measurement is unchanged envelope for envelope.

The load-bearing reason to derive the escape from the plan rather than from any allowlist is
that IT CANNOT BE SELF-GRANTED. `locators.plan_md_hash` fingerprints plan.md's bytes with
only checkbox ticks normalized away, and `mode_engine.approved_gates_for_plan` counts an
approval only for the plan it was granted against. So adding a `## Files` bullet changes the
digest, drops both gates, and `[ENF-GATE-DRIFT]` blocks every write until the user replies
`approved` again. Widening the boundary therefore requires the user's word MECHANICALLY,
not by convention. A `gate-categories.json` entry or a new cache field would have neither
property.

`kind` is one parameter with three values (`approved`, `pre_approval`, `dispatch`), and it
selects both whether the plan's declaration is honored and which action the refusal names.
No booleans, and three texts rather than one with a placeholder, because the three available
actions are genuinely different: before approval the declaration is not yet binding, after
approval it is one edit plus one word from the user, and inside a dispatch the child holds
neither lever.

Sub-agents DEFER to a declared role scope BY ARM ORDER: `_check_role_scope_write` runs
first and returns non-None for any declared list, empty included, so `path_in_scope` stays
the sole judge and the new arm never reads `role_write_scope` at all. That preserves the
role-scope ADR's property that the scope is a function of the role alone. The arm requires
`cache_source == "subagent_start"`, and its root comes from the PARENT's cache through
`parent_session_id`, because a sub-agent cache deliberately does not stamp `project_root`.

## Alternatives considered
- **Containment alone, with no escape.** Rejected on evidence, not on principle: the five
  categories of out-of-project write were enumerated BEFORE choosing, and one of them is
  real and routine. Four are already exempted by an arm that runs first (the Writ install's
  own tree, `~/.claude/settings*.json`, the basename-matched `plan.md` / `capabilities.md`
  allow, and the session cache); the fifth is the harness scratchpad and pytest's temp dirs,
  which every agent is instructed to use. Containment alone would refuse paths Writ itself
  writes on every run.
- **A separate "escape" declaration**, a new section or a config file. Rejected because it
  collapses into the chosen design with no new grammar. `templates/plan-template.md` line 15
  documents `## Files` paths as relative to the repo root, so a relative bullet resolves
  INSIDE the boundary and can widen nothing; the only bullets that can widen it are already
  the absolute and `~`-prefixed ones. A second declaration format would have added a second
  parser and a second thing to keep in sync for zero added expressiveness.
- **An allowlist in `gate-categories.json` or a new cache field.** Rejected on the
  self-grant property above, which is the whole reason enforcement here is safe.
- **Reusing `plan_harvest._extract_files`** to parse the declaration. Rejected: it also
  scans prose bullets ANYWHERE in the document and admits reason-less bullets, so a path
  merely MENTIONED in `## Analysis`, or a bullet phase-a would itself have rejected, would
  widen the boundary. The two annotated-bullet regexes are imported from
  `approval_workflow` instead, so the boundary can never widen to a shape the approval gate
  refuses. Both halves of that rejection are executed as mutations, not argued.
- **Warning instead of refusing.** Rejected under Enforce-not-Inform. An advisory nobody
  reads is indistinguishable from nothing, and it would have left the measured gap open. The
  deadlock risk that normally argues for advisory is bounded here because the escape is a
  mechanism the user already operates once per cycle: no new command, no flag, no config
  file.
- **A literal `/tmp/claude-<uid>` prefix for the scratch zone.** Rejected as a name
  recognizer. The mechanism is "ephemeral storage the OS designates", so the zone is
  `tempfile.gettempdir()`, resolved ONCE at `mode set` and stamped into the session cache,
  not resolved at write time (see the divergence bullet in Consequences). That same
  reasoning is what later ruled out a hardcoded `/tmp` FALLBACK for a cache carrying no
  stamp: a default spelled as a name is the same name recognizer wearing a different hat.
- **Threading the calling process's temp directory through the `can-write` payload**, the
  way `skill_dir` is threaded. Rejected: it makes the daemon trust a value the caller
  supplies. Today an agent-influenced `TMPDIR` can only reach the decision during the
  daemon-unreachable fallback window; threading it would extend that trust to the daemon
  path too, so the bypass would be available whenever the daemon is UP as well. That trades
  a narrow outage-gated hole for a permanent one.
- **Glob patterns for containment.** Rejected outright. This repo has already paid for
  exclusion globs where `*` spans `/` and matches the raw path, so a directory-shaped
  pattern exempts any depth and `..` escapes it. Containment is a resolved-prefix comparison
  and the declared set is compared by equality.

## Consequences
- **Accepted cost, and the honest consequence of re-running the probe.** Post-approval,
  `/etc/passwd-probe.txt` flips from ALLOW to DENY, and so does any other project's source
  path. `/tmp/outside-the-repo.py` stays ALLOW because it is the scratch zone. All three
  pre-approval measurements are unchanged, tag for tag. Replayed through the CLI surface
  against the real repo, seven for seven.
- **`~/.claude/CLAUDE.md` and `~/.claude/memory/` are now refused** from a Writ session, with
  the escape named. `~/.claude/agents/*.md` are symlinks into `<skill>/agents`, so `realpath`
  lands inside the Writ repo and they stay writable from a Writ session while being refused
  from another project. Both are the intended tightening.
- **The exempt zone is skipped when the project root is itself inside it.** A project living
  in the temp directory would otherwise exempt every sibling beside it. That guard is also
  what keeps the predicate's own test file non-vacuous: pytest's `tmp_path` is inside the
  temp directory, so a fixture rooting a project there has the exemption inert by
  construction and its "outside" sibling denies on containment alone. Without the guard most
  of that file would pass for the wrong reason, so it is asserted in BOTH directions with an
  explicitly monkeypatched zone.
- **Cost per write, measured rather than asserted.** The dominant case, an in-project write,
  costs one `resolve_project_root` marker walk, two `realpath` calls and one prefix
  comparison, and ZERO extra file reads: the plan is opened only after containment and the
  scratch zone have both failed. Pinned by a test that patches `locators._find_plan_md` to
  raise on any call beyond the one `approved_gates_for_plan` already needs.
- **Resolution is `realpath` on BOTH sides.** Resolving only the target would refuse
  legitimate writes under a project root recorded by its symlink spelling; resolving neither
  would let `<root>/<symlink-out>/../x.py` through, because `normpath` collapses `..`
  lexically while the OS follows the symlink. A relative envelope path joins the RESOLVED
  ROOT and never `os.getcwd()`, because the daemon's cwd is Writ's own install dir (which
  carries `.git`, `pyproject.toml` AND a `plan.md`), so resolving against the process cwd
  would give the daemon and the CLI fallback two different answers for one write.
- **The recorded root can only widen, never narrow.** `mode_engine._mode_set` stamps
  `project_root` as `os.getcwd()` with no marker walk, so setting a mode from a subdirectory
  records the subdirectory; the marker walk runs in `boundary_root` and only moves UP.
- **Absence abstains, everywhere.** An empty or relative recorded root, a sub-agent with no
  `parent_session_id`, and a parent with no project recorded all keep today's decision. A
  missing record must not harden into a refusal nobody asked for. A recorded root of `/`
  contains everything; both degenerate cases are stated AND tested, so neither is accidental.
- **A boundary refusal is not counted as a gate denial.** `_log_gate_denial` increments
  `denial_counts`, which feeds the escalation that tells the user which PENDING GATE to
  approve, and the remedy here is an edited plan plus a fresh approval. One `write_attempt`
  row with `gate_status=project_boundary_deny` instead. Same reasoning, same shape as the
  role-scope deny.
- **The exclusion allow had to be guarded too**, and this is not defensive: `_matches_any`
  lets `*` span `/`, so `*/tests/*` is satisfied by `/any/other/project/tests/x.py`. Left
  unguarded it would have been a one-line bypass of the whole feature, reachable before any
  approval.
- **Non-work modes are deliberately out of scope.** `conversation`, `review` and
  `investigate` allow all writes and `debug` gates on a root cause. None of them has a plan,
  so none has a declared surface to derive a boundary from or an escape to name, and a
  refusal with no named action is a deadlock. The boundary applies where an approved plan is
  the authority, which is a statement about the authority SOURCE and not about a mode name.
- **`## Files` is NOT enforced file by file inside the project.** The implementer
  legitimately writes files a plan under-specifies (`__init__.py`, `conftest.py`, the plan
  itself), so a bullet-by-bullet check would refuse routine work today.
- **The bash gate's non-local filter is now CLOSED, and the cost it predicted was paid and
  then fixed.** It formerly sent only cwd-contained targets to the gate and dropped the rest,
  so a Bash-mediated write outside the hook's cwd never reached this predicate: measured, a
  heredoc write succeeded to a path the Write tool had refused seconds earlier. A confinement
  enforced on one of two doors is not a confinement, so the producer now emits an `outside`
  row and the consumer feeds it to the same `can-write` round trip a project-local target
  takes. The prediction recorded here was exact: doing that converted routine PRE-approval
  scratch writes into `[ENF-GATE-PLAN]` refusals whose named action, approve the plan, does
  not unblock writing a scratch file, which is the "refusal names no way out" defect. That
  cost was accepted for one cycle and then removed by the scratch-zone allow arm in
  `_check_work_gate`, placed AFTER the drift check and immediately before `[ENF-GATE-PLAN]`.
  Placing it beside the exclusions arm was rejected: it would have widened the drift state,
  a deliberately loud stop signal, and the defect measured lived entirely in the
  pre-approval window. An `exclusions` entry in `gate-categories.json` was rejected too, on
  the same self-grant property as the boundary escape and because `_matches_any` treats `*`
  as `.*`, so it spans `/`. The arm resolves its root through `boundary_root` and
  `resolve_target` exactly as this predicate does, and inherits the empty-root abstain: a
  session with no recorded project keeps refusing.
- **The scratch zone was a PER-PROCESS answer, and is now a stamped session value.**
  `in_scratch_zone` resolved `os.path.realpath(tempfile.gettempdir())` fresh on every call.
  The write gate runs in two OS processes (the daemon, and the CLI subprocess the Bash gate
  shells out to when the daemon is unreachable), and `tempfile.gettempdir()` is derived from
  `TMPDIR` per process, so the two doors could answer differently for one path. Measured
  twice: through the real gate on identical session state (`can_write=false` with
  `[ENF-GATE-PLAN]` under no `TMPDIR`, which is the daemon's environment read from
  `/proc/<pid>/environ`, versus `can_write=true` under `TMPDIR=/var/tmp/...`), and directly
  on the predicate. Two preconditions make it real and both were measured: the alternate
  directory must EXIST and be writable, or `gettempdir()` silently falls through its
  candidate list to `/tmp`, and it must sit OUTSIDE the daemon's zone, because a directory
  nested under `/tmp` is also inside `/tmp` and the two processes then agree by accident.
  The fix follows `project_root`'s own precedent exactly: `mode_engine._apply_mode_set`
  stamps `cache["scratch_zone"]` beside `cache["project_root"]`, and both doors read that
  stamp through `project_boundary.scratch_zone` instead of the environment.
  `in_scratch_zone(target, root, zone)` is a pure three-argument predicate and the module
  imports no `tempfile` at all, which is what its own docstring already claimed for every
  other predicate in it. The process that declares the mode is the one whose answer wins,
  because it is already the authority for `project_root`: a caller who can move the entire
  boundary by choosing a cwd is not further constrained by also choosing a zone.
- **An absent stamp FAILS CLOSED, and there is no live fallback.** `scratch_zone("")`
  returns `""` and the exemption is off for that session. Three reasons: the divergence can
  only convert a DENY into an ALLOW, so absence belongs on the deny side; absence here
  removes an EXEMPTION rather than the only JUDGE, so containment, the project's own memory
  directory and the approved plan's `## Files` still decide the write and this is not the
  deadlock `boundary_root`'s abstain avoids; and a hardcoded default is barred by the
  name-recognizer rejection above. It reaches only a cache carrying a `project_root` but no
  `scratch_zone`, which is a cache written before the field existed; any `mode set` or
  `mode init` re-stamps. Because `_SCRATCH` ("paths under the OS temporary directory need no
  declaration") is FALSE in that state, `boundary_refusal` takes the resolved zone as a
  REQUIRED parameter and selects a sentence that names the repair instead, so the reader is
  not sent in a circle by a refusal that names no way out.
- **The pre-existing basename-only `plan.md` / `capabilities.md` allow is untouched.** It
  lets a write to another project's `plan.md` through. Orthogonal, and fixing it changes a
  different arm.
- **The role-scope deferral holds by ARM ORDER, and an explicit guard for it was removed
  as WRONG in its one non-redundant state.** A second `isinstance(role_write_scope, list) ->
  abstain` read in the boundary arm was written first, then measured, and the measurement
  comes with its harness because without one a claim like this cannot be checked. THE STATE
  IS NOT REACHABLE THROUGH THE SHIPPED CORPUS. It was reached only with
  `role_scope.fetch_declared_scope` PATCHED to return a list. Unpatched, the real seeder
  stamps `None` for every spelling tried (`'unknown '`, `' unknown'`, `'   '`, `'unknown'`),
  so the reachable count is zero, for two independent reasons: the fetcher strips the role
  itself before issuing the request, so `'   '` answers None with no round trip and the
  padded spellings collapse to `'unknown'`, and no `SubagentRole` node is named `unknown`
  (the corpus declares five, all `writ-*`), so that request carries no `write_scope`. An
  earlier revision of this bullet and of the code comment said "constructible through the
  real seeder" without the patch qualifier; that was an overstatement and this is the
  correction at the source.
  What the patched measurement DID establish, and it is why the guard is gone: the two guard
  sets diverge in one respect, `subagent_seed._declared_scope` comparing the RAW role to
  `unknown` while the role-scope arm compares the STRIPPED role, and on that constructed
  cache the guard deferred to a judge that had ALREADY ABSTAINED. Nothing judged the path,
  and the verdict went from DENY (confined to the parent's project) to ALLOW (unbounded),
  both directions measured. A guard whose only non-redundant effect is to remove the last
  judge is worse than no guard however the input arrived, so it goes regardless of
  reachability. Removing it restores the arm's own stated principle, that an unobservable
  role names no boundary and therefore supplies none, leaving the project the child was
  dispatched for as the applicable authority.
  What would make the state reachable: a role node named `unknown`, or those two guards
  otherwise disagreeing about which roles are judgeable. They already disagree up to
  whitespace, which is BENIGN today because both real callers pre-strip, so it is a latent
  trap for a future direct caller of `seed_subagent_cache` rather than a live hole. It is a
  defect in that comparison (one of the two should strip, or both should share one
  predicate), filed rather than fixed here because it lives in a module this cycle does not
  touch and fixing it changes what the seeder stamps. The edge's BEHAVIOUR is deliberately
  not pinned, because specifying the symptom of a defect as intended behaviour would outlive
  and discourage its fix; what is pinned instead is the property the deletion created, that
  the boundary arm's verdict does not depend on `role_write_scope` at all
  (`TestBoundaryArmIgnoresRoleWriteScope`), which stays true and stays desirable whatever
  the seeder later does.
- **`writ doctor` gains no check.** The existing `subagent-role-scope-coverage` finding for
  `writ-implementer` is unchanged and still correct: its scope remains per-dispatch data. The
  boundary constrains the TREE it may write, not which files within it.
- **A same-path pair is what proves the declaration is load-bearing.** The two obvious
  capabilities (an undeclared out-of-root path denies; a declared one allows) used different
  paths at different depths, so a predicate that allowed deeper paths for a reason having
  nothing to do with the plan would satisfy BOTH and neither would notice. A pair holding
  ONE path at ONE depth and varying only the bullet was added for exactly that, and the
  depth-driven mutation was executed: capabilities 1 and 4 both stay GREEN under it and only
  the pair goes red.
