# ADR: a sub-agent's write scope is declared by its ROLE and stamped at dispatch

Status: accepted (cycle M, 2026-08-28)

## Context
Every sub-agent whose cache was created by `SubagentStart` got an unconditional write
allow (`writ/session/gates.py`, the `subagent_bypass` arm). The justification was real: a
worker is dispatched by an orchestrator that already cleared a human approval gate, and
gates exist to stop the master writing code before plan approval, not to re-police
sanctioned workers. What the justification did NOT establish is that a worker may write
ANY path. `writ-explorer` and `writ-reviewer` both carry Bash and no Write, and their own
prompts say "Only observe and report" and "Never edit files"; that boundary was advertised
in prose and enforced nowhere. Claude Code's `tools:` field is not the boundary either: it
controls which TOOLS exist, says nothing about WHICH PATHS, and does not cover a
Bash-mediated write (`echo x > src/thing.py`) at all.

The requirement, and the whole reason this is an ADR rather than a comment: the child's
scope must be a function of WHAT IT IS, never of WHO DISPATCHED IT. "The parent's gates,
narrowed by the role" is the same words with the opposite property, because under it a
parent who approves more widens the child, so authority still inherits.

## Decision
A `SubagentRole` node declares `write_scope: list[str] | None`. It is authored in
`bible/methodology/ROL-*.md` frontmatter (where `dispatched_by` already lives), imported
into Neo4j, and exported into the tracked `writ-corpus.cypher`. At dispatch,
`seed_subagent_cache` fetches it ONCE over the daemon's existing
`GET /subagent-role/{name}` and stamps it into the child's session cache. At write time,
a new arm in `_check_exempt_write` reads it FROM THE CACHE and refuses a path the role's
patterns do not match.

The arm reads exactly four cache fields: `agent_type`, `role_source`, `cache_source`,
`role_write_scope` (plus the pre-existing `is_subagent`). It never reads `mode`,
`current_phase`, `gates_approved`, `parent_session_id` or `project_root`. That is pinned as
a PROPERTY over the cross product of parent states, not as a list of cases.

`None` and `[]` are DIFFERENT FACTS, everywhere on this path: `None` means the role
declares no scope and the sub-agent keeps the write authority it has always had, `[]` means
the role declares it writes nothing and every path is refused. Nothing coalesces one into
the other, in the model default, the Cypher projection, the route response, or the cache
schema.

## Alternatives considered
- **Resolve the scope from the graph at write time.** Rejected on correctness, not cost.
  The write gate runs in TWO processes: the daemon route, which holds a db handle, and a
  CLI subprocess (`writ-session.py can-write`, which the Bash write gate shells out to when
  the daemon is unreachable), which does not. A write-time lookup would deny in one and
  allow in the other, so the decision would depend on which process happened to evaluate
  it. The session cache is the one thing both processes read from the same file.
- **Generate a static scope map into the repo** (the `bin/lib/gate-categories.json` shape).
  It would make enforcement daemon-independent, at the cost of a second source of truth for
  a governance fact. This repo has already paid for dual storage of one fact more than once.
- **A `ScopeSource` abstraction** (graph source versus static source). Rejected with the
  static map itself; `writ/session/role_scope.py` is two module-level functions, so there is
  no interface to express.
- **Add `write_scope` to `agents/*.md` front matter.** Rejected: that front matter is a
  CLAUDE CODE contract, and Claude Code ignores unknown keys, so the line would advertise a
  harness-enforced boundary the harness knows nothing about, which is the exact class of
  false advertisement this cycle exists to remove.
- **A feature flag.** A flag defaulting off is enforcement nobody runs; a flag defaulting on
  is a documented bypass. The narrowing (an observed role AND a `subagent_start` cache AND a
  declared scope) is the scope control instead.

## Consequences
- **Accepted cost: enforcement is only as live as the graph read at dispatch time.** A
  dispatch made while the daemon is down or slow stamps nothing and is unenforced. That is
  the correct degradation, because the alternative is refusing a worker on the strength of a
  record we failed to read, and it is visible rather than silent: the cache's
  `role_scope_source` is empty, and `writ doctor`'s `subagent-role-scope-coverage` reports
  how many roles declare a scope and names those that do not.
- **The write path costs nothing new.** Per write: one dict lookup, one `os.path.realpath`,
  and at most a dozen `re.fullmatch` calls. No graph query, no HTTP call, no extra file read
  (the gate already reads the session cache once per request). Pinned by a test that patches
  the dispatch-time fetcher to RAISE and still asserts both verdicts.
- **The scope constrains the KIND of file, not the tree.** A sub-agent cache deliberately
  carries no `project_root` (stamping the parent's project would make every mode rotation
  look contested), so a role scope cannot be anchored to a project: `*_test.*` also matches
  `/etc/nginx/conf_test.conf`. This is strictly tighter than the unconditional allow it
  replaces, and the credential-path deny still runs ahead of everything.
- **Matching judges the RESOLVED path.** These globs match the raw path and `*` spans `/`,
  which is safe for an exclusion list and unsafe for an allowlist, so `realpath` runs first
  and `<in-scope-dir>/../../elsewhere.py` and an out-of-scope symlink are both refused.
- **On the Writ repo itself the feature is nearly inert**, because the skill-dir exemption
  still runs FIRST (you cannot require a gate's permission to edit the gate). Dogfooding
  will show almost no denials; the enforcement is real when a governed sub-agent writes into
  an ordinary project.
- **One intended tightening beyond the obvious:** `_check_special_files` runs AFTER the
  exemptions, so a role with a declared scope is now judged for `plan.md` and
  `capabilities.md` too. An explorer can no longer write `capabilities.md`, which today's
  blanket allow permits, and that is why the planner's scope names both artifacts.
- **`writ-implementer` declares no scope, deliberately.** Its scope is the approved plan's
  `## Files` list, which is per-dispatch data rather than a property of the role, so no
  static glob can state it truthfully and an approximation would be a boundary that looks
  enforced and is not. The doctor names it as undeclared.
- **A role-scope refusal is not counted as a gate denial.** `_log_gate_denial` increments
  `denial_counts`, which feeds the escalation machinery that tells the user which gate to
  approve; a role boundary is not approvable, so counting it there would manufacture a
  remedy that does not exist. It emits one `write_attempt` row with
  `gate_status=role_scope_deny` instead.
