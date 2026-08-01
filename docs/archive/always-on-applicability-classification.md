# Always-on applicability classification + filter design

> **HISTORICAL RECORD (archived 2026-07-31), do not read as current.** Two claims below are
> superseded: (1) the doc says the `WRIT_ALWAYS_ON_FILTER` default flip is pending; the filter
> has since shipped **default ON**. (2) "37 always-on rules" conflates
> the node flag with endpoint reachability: only 6 Rules carry `always_on: true`; the other rows
> reach `/always-on` because they are `mandatory` (the endpoint predicate is `mandatory OR
> always_on`). Current behavior: `docs/reference/retrieval.md` section 4. This file is kept for
> the design rationale (routing-as-node-data, the fail-open principle, the measured 37->10 prompt
> and 37->5 write reductions) and as provenance for `scripts/migrate_always_on_applicability.py`.

Goal: stop re-injecting all 37 always-on rules (3,556 tokens) on every UserPromptSubmit. Inject
each rule only at the moment its WHEN matches. No weights: applicability decides, not ranking
(WRIT-BLUEPRINT 3.5). Fail-open: when unsure, inject.

Measured today (2.1.183): `/always-on` returns 37 rules, 3,556 tokens. At roughly 67 prompts per
session that is about 238K tokens re-injected. The classification below shows that 32 of the 37
rules are context-conditional and only 5 are truly per-turn.

## The injection points (from the black-box map)

| Scope | Hook (injection surface) | Match context |
|---|---|---|
| `universal` | UserPromptSubmit additionalContext | always (every turn) |
| `prompt` | UserPromptSubmit additionalContext | trigger_keywords vs the prompt text |
| `write` | PreToolUse(Write\|Edit) additionalContext | trigger_keywords vs file_path + content |
| `bash` | PreToolUse(Bash) additionalContext | trigger_keywords vs the command |
| `stop` | Stop additionalContext | always at turn end (response/verification discipline) |

A rule may carry more than one scope. The matcher is the existing `MethodologyTriggerIndex`
logic (`floor ∪ push ∪ pull`, whole-word, deterministic), applied to Rules, with the pull tail
NOT budget-dropped (applicability, not ranking).

## Per-turn ceiling

Inject every turn only `universal` + `stop`-comms: ENF-COMMS-OUTPUT-001 (294), ENF-COMMS-001 (91),
FRB-COMMS-001 (100), FRB-COMMS-002 (62) = 547 tokens. The remaining 3,009 tokens defer to the
write/bash/stop moment and inject only the matching rules.

Per-prompt always-on cost: 3,556 to about 547, an 85% cut. Over 67 prompts: about 238K to about
37K, saving roughly 200K tokens per session on this channel. Deferred rules still inject, but only
the few that match the specific write, so the write-time channel stays small.

## Classification (37 rules)

`gated` = a hard server-side gate already enforces this, so injection is advisory and a missed
inject is still caught at the gate (lower risk). `advisory` = injection is the only enforcement,
so its applicability match must fail open.

| Rule | tokens | scope | trigger_keywords (match context) | enforcement |
|---|---|---|---|---|
| ENF-COMMS-OUTPUT-001 | 294 | universal | (none) | advisory |
| ENF-COMMS-001 | 91 | stop | (response to review/correction) | gated (enforce-violations) |
| FRB-COMMS-001 | 100 | stop | (response to review/correction) | gated (enforce-violations) |
| FRB-COMMS-002 | 62 | stop | (success/pass/complete claims) | gated (verify-before-claim) |
| ENF-CTX-003 | 96 | write | factory, repository, Model::load, ORM | advisory |
| SCALE-STATELESS-001 | 100 | write | server, handler, worker, session, global, request | advisory |
| ENF-POST-003 | 59 | write | interface, implements, abstract, signature | advisory |
| ENF-SEC-001 | 108 | write | endpoint, route, controller, resolver, CLI command | advisory |
| PERF-QUERY-001 | 97 | write | loop, query, foreach, select_related, prefetch, N+1 | advisory |
| SEC-AUTH-HASH-001 | 90 | write | password, hash, bcrypt, argon2, scrypt, credential | advisory |
| SEC-AUTH-TOKEN-001 | 113 | write | token, session, csrf, api key, reset, verification | advisory |
| SEC-AUTHZ-DEFAULT-001 | 77 | write | endpoint, route, permission, decorator, policy | advisory |
| SEC-AUTHZ-ENFORCE-001 | 87 | write | route, resolver, handler, authorization, auth | advisory |
| SEC-AUTHZ-IDOR-001 | 118 | write | find, params, current_user, owner, record id | advisory |
| SEC-AUTHZ-MASS-001 | 107 | write | request body, permit, strong parameters, mass assignment | advisory |
| SEC-CRYPTO-KEY-001 | 97 | write | api key, secret, password, private key, token | advisory |
| SEC-CRYPTO-RAND-001 | 105 | write | random, nonce, salt, key, iv, uuid, token | advisory |
| SEC-DATA-PII-001 | 120 | write | log, email, phone, ssn, address, pii, dob | advisory |
| SEC-INJ-CMD-001 | 99 | write | subprocess, exec, shell, system, popen, shell_exec | advisory |
| SEC-INJ-CSRF-001 | 96 | write | post, put, patch, delete, csrf, form | advisory |
| SEC-INJ-DESER-001 | 107 | write | pickle, unserialize, yaml.load, marshal, deserialize | advisory |
| SEC-INJ-SQL-001 | 74 | write | sql, query, select, insert, update, execute, where | advisory |
| SEC-INJ-SSRF-001 | 148 | write | url, request, webhook, fetch, http, outbound | advisory |
| SEC-INJ-XSS-001 | 87 | write | html, render, template, jsx, escape, innerhtml | advisory |
| SEC-VAL-FILE-001 | 101 | write | upload, file, mime, attachment, avatar | advisory |
| SEC-VAL-SERVER-001 | 92 | write | request, validation, input, payload, webhook | advisory |
| ENF-GATE-006 | 85 | write (path) | .claude/handoffs/ | gated |
| ENF-POST-006 | 72 | write (path) | .claude/handoffs/ | gated |
| ENF-PROC-PLAN-001 | 77 | write (path) | plan.md | gated (Gate 5) |
| ENF-GATE-007 | 85 | write + work | (work mode, pre-impl) | gated |
| ENF-PROC-BRAIN-001 | 86 | write + work | (work mode, pre-design) | gated |
| ENF-PROC-TDD-001 | 75 | write + work | src path without test | gated |
| ENF-PROC-WORKTREE-001 | 63 | bash | git worktree add | gated (bash gate) |
| ENF-TEST-001 | 81 | stop | (tests written, not run) | gated (run-pending-tests) |
| ENF-PROC-VERIFY-001 | 77 | stop | (TodoWrite complete, claims) | gated |
| ENF-POST-007 | 53 | stop/write | static analysis, phpstan, lint | advisory |
| ENF-PROC-DEBUG-001 | 77 | debug-mode | (debug mode floor) | advisory |

Counts: universal 1, stop-comms 3, write (content) 22, write (path) 3, write+work 3, bash 1,
stop 3, debug-mode 1. So 5 inject every turn (universal + stop-comms), 32 defer to context.

Risk note: 18 of the 32 deferred rules are already hard-gated, so a missed inject is still caught
at the gate. The 14 advisory-only deferred rules (the SEC-* content rules, PERF, ENF-CTX, SCALE,
ENF-POST-003) are the ones whose enforcement IS the injection, so their write-time match must fail
open (broad keywords, inject on any partial match).

## Design

1. Routing data on Rules. Add `applicability_scope` (list) and `trigger_keywords` (list) to the
   always-on Rule nodes, exactly like methodology nodes carry `floor_modes`/`trigger_keywords`.
   This keeps routing as node-declared data (no central table), per the project invariant.
2. Endpoint. Extend `/always-on` to accept `scope`, `prompt`, `path`, `content_keywords`, and
   `event`, and return only rules whose scope matches the call site and whose trigger_keywords
   match the context (or that are universal). Reuse `MethodologyTriggerIndex` matching; do NOT
   budget-drop (applicability, not ranking). Keep the current blanket behavior when the new
   filter flag is off.
3. Hooks. UserPromptSubmit injects universal + prompt-matches. PreToolUse(Write|Edit) injects
   write-matches (path + content). PreToolUse(Bash) injects bash-matches. Stop injects stop-scope.
   Each is additionalContext, which the black-box map confirms as a live injection surface.
4. Flag. `WRIT_ALWAYS_ON_FILTER` (default off). Off = today's blanket inject (no enforcement
   change). On = applicability-scoped. This lets us A/B with the black box before committing.
5. Fail-open. If scope or keywords are missing on a rule, treat it as universal (inject every
   turn) so no rule silently disappears. The `writ validate` stranded-rule invariant is extended
   to assert every always-on rule has a non-empty scope.

## Measurement (black box)

With the flag off then on over an identical prompt set, compare `always_on_tokens` from the
friction `always_on_inject` events (cli.py already aggregates these) and confirm via the
blackbox JSONL that every previously-injected rule still reaches the agent at its context moment
(no enforcement regression).

## Slices

1. Data + endpoint filter (flag-guarded, default off) + tests. DONE.
2. UserPromptSubmit filtered path (universal + prompt) behind the flag. DONE.
3. Write context injection behind the flag (PreToolUse Write|Edit). DONE. (Stop dropped: a Stop-hook
   additionalContext is treated as a turn-block, so stop-discipline rules stay fail-open universal.)
4. Measure with the black box; flip the flag default once parity holds. PARTIAL: measured below;
   keyword tuning + a flag-on parity session remain before flipping the default.

## Live measurement (build 2.1.183, flag exercised via direct endpoint calls)

| Call | rules | tokens |
|---|---|---|
| blanket `/always-on` (flag off, today) | 37 | 3,556 |
| `/always-on?at=prompt` (per-turn set) | 10 | 793 |
| `/always-on?at=write` (SQL+password content) | 5 | 450 (SQL, hash, crypto-key, perf, val-server) |
| `/always-on?at=write` (plain prose) | 0-1 | ~0 |

Per-prompt always-on cost drops 3,556 to 793, a 78% cut (about 184K tokens per 67-prompt session).
Write-time injection surfaces only the rules matching the code being written.

Known refinement (fail-open-safe, not an enforcement gap): some `trigger_keywords` over-match common
English (`file`, `input`, `update`), so an unrelated write can pull a rule. This wastes write-time
tokens but never drops enforcement. Tighten per rule, measured, like SEC-INJ-SQL-001 was
(`sql, cursor, parameterized, sql injection, db.query`). The work-gated ENF-PROC rules and comms
rules are intentionally left fail-open universal (gated and/or YAML methodology nodes).
